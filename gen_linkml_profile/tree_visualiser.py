# -*- coding: utf-8 -*-

from treelib import Tree
from uuid import uuid4
from collections import deque
import logging

log = logging.getLogger(__name__)


class TreeVisualiser:
    def __init__(self, nodes, relations):
        self.nodes = nodes
        self.relations = relations

    def label(self, node_id: str, full_id: bool = False) -> str:
        node = self.nodes.get(node_id, {})
        name = (
            node.get("cim:IdentifiedObject.name")
            or node.get("name")
            or node_id[-8:]
        )
        value = (
            node.get("cim:TransformerEnd.endNumber")
            or node.get("cim:ActivePower.value")
            or node.get("cim:AnalogValue.value")
            or node.get("cim:StreetAddress.postalCode")
            or node.get("cim:StreetDetail.number")
            or node.get("cim:Voltage.value")
            or node.get("nl:IdentifiedObject.europeanArticleNumberEAN")
            or None
        )
        value = (
            value["@value"]
            if isinstance(value, dict) and "@value" in value
            else value
        )
        name = f"|{value}| {name}" if value else name

        node_type = node.get("@type", "")
        if isinstance(node_type, list):
            node_type = node_type[0] if node_type else ""
        node_type = str(node_type).replace("cim:", "")
        node_code = node_id if full_id else node_id[-8:]

        return f"[{node_type}] {name} ({node_code})"

    def show(
        self,
        start_id: str = None,
        max_depth: int = 3,
        max_children: int = 5,
        full_id: bool = False,
        exclude_types=None,
        hide_connectivity: bool = False,
    ):
        if start_id is None:
            raise ValueError("start_id must be specified")

        try:
            start_id = self.nodes.find_id(start_id)
        except ValueError as exc:
            raise ValueError(f"Invalid start_id: {exc}") from exc

        if start_id is None:
            raise ValueError(f"No matching start_id found: {start_id}")

        tree = self.build_tree(
            start_id=start_id,
            max_depth=max_depth,
            max_children=max_children,
            full_id=full_id,
            exclude_types=exclude_types,
            hide_connectivity=hide_connectivity,
        )

        tree.show(sorting=False)

    def build_tree(
        self,
        start_id: str,
        max_depth: int = 3,
        max_children: int = 5,
        full_id: bool = False,
        recursive: bool = False,
        exclude_types=None,
        hide_connectivity: bool = False,
    ) -> Tree:
        if start_id not in self.nodes:
            raise ValueError(f"Unknown start_id: {start_id}")

        exclude_types = set(exclude_types or [])

        limited_types = {
            "cim:BaseVoltage",
            "cim:CoordinateSystem",
            "cim:GeographicalRegion",
        }

        hidden_types = {
            "cim:Terminal",
            "cim:ConnectivityNode",
        } if hide_connectivity else set()

        # Deze combinaties worden niet verder uitgewerkt.
        skip_child_types = {
            "cim:Substation": {"cim:SubGeographicalRegion"},
            "cim:Line": {"cim:SubGeographicalRegion"},
        }

        tree = Tree()
        root_id = str(uuid4())

        tree.create_node(self.label(start_id, full_id), root_id)

        queue = deque([(start_id, root_id, 0, None)])
        expanded_subtrees = {start_id}

        eligibility_cache = {}
        type_cache = {}

        def get_type(node_id):
            if node_id not in type_cache:
                node = self.nodes.get(node_id, {})
                value = node.get("@type", "")

                if isinstance(value, list):
                    value = value[0] if value else ""

                type_cache[node_id] = value

            return type_cache[node_id]

        def should_skip_child(parent_id, child_id):
            return (
                get_type(child_id)
                in skip_child_types.get(get_type(parent_id), set())
            )

        def is_eligible(node_id):
            if node_id not in self.nodes:
                return False

            if not exclude_types:
                return True

            if node_id not in eligibility_cache:
                eligibility_cache[node_id] = (
                    get_type(node_id) not in exclude_types
                )

            return eligibility_cache[node_id]

        def belongs_to_transformer_end(terminal_id):
            adjacent = (
                self.relations.incoming(terminal_id)
                + self.relations.outgoing(terminal_id)
            )

            return any(
                get_type(other_id) == "cim:PowerTransformerEnd"
                for other_id, _ in adjacent
            )

        def collect_targets(node_id):
            """
            Verzamel zichtbare relaties.

            Connectivity-objecten worden transparant doorlopen.
            De gevonden zichtbare objecten worden aan dezelfde
            zichtbare ouder gekoppeld.
            """
            new_nodes = []
            references = []

            seen = set()
            hidden_seen = set()
            stack = [node_id]

            while stack:
                current_id = stack.pop()

                relationships = (
                    self.relations.incoming(current_id)
                    + self.relations.outgoing(current_id)
                )

                for target_id, _ in relationships:
                    if target_id in seen:
                        continue

                    seen.add(target_id)

                    if not is_eligible(target_id):
                        continue

                    # Verberg directe Transformer -> Terminal-relaties
                    # wanneer de Terminal ook verbonden is met een
                    # PowerTransformerEnd.
                    if (
                        current_id == start_id
                        and get_type(current_id) == "cim:PowerTransformer"
                        and get_type(target_id) == "cim:Terminal"
                        and belongs_to_transformer_end(target_id)
                    ):
                        continue

                    # Gefilterde objecten worden als verwijzing getoond.
                    if should_skip_child(current_id, target_id):
                        references.append(target_id)
                        continue

                    target_type = get_type(target_id)

                    # Connectivity-objecten transparant doorlopen.
                    if target_type in hidden_types:
                        if (
                            target_id not in hidden_seen
                            and target_id not in expanded_subtrees
                        ):
                            hidden_seen.add(target_id)
                            expanded_subtrees.add(target_id)
                            stack.append(target_id)

                        continue

                    # Bestaande objecten worden als verwijzing getoond.
                    if target_id in expanded_subtrees:
                        if target_id != current_id:
                            references.append(target_id)
                    else:
                        new_nodes.append(target_id)

            return new_nodes, references

        while queue:
            node_id, parent_id, depth, parent_node_id = queue.popleft()

            if depth > 1 and get_type(node_id) in limited_types:
                continue

            new_nodes, references = collect_targets(node_id)

            if depth >= max_depth:
                if recursive:
                    for target_id in references:
                        tree.create_node(
                            f"↩ {self.label(target_id, full_id)}",
                            str(uuid4()),
                            parent=parent_id,
                        )

                continue

            selected = new_nodes[:max_children]

            for target_id in selected:
                child_id = str(uuid4())

                tree.create_node(
                    self.label(target_id, full_id),
                    child_id,
                    parent=parent_id,
                )

                expanded_subtrees.add(target_id)

                queue.append(
                    (target_id, child_id, depth + 1, node_id)
                )

            for target_id in references:
                tree.create_node(
                    f"↩ {self.label(target_id, full_id)}",
                    str(uuid4()),
                    parent=parent_id,
                )

            remaining = len(new_nodes) - len(selected)

            if remaining > 0:
                tree.create_node(
                    f"... (showing {len(selected)} of {len(new_nodes)})",
                    str(uuid4()),
                    parent=parent_id,
                )

        return tree

    def show_pattern(
        self,
        start_id: str,
        pattern: list[str],
        hide_connectivity: bool = False,
        full_id: bool = False,
        maximum_children: int = 5,
    ):
        if start_id is None:
            raise ValueError("start_id must be specified")

        try:
            start_id = self.nodes.find_id(start_id)
        except ValueError as exc:
            raise ValueError(f"Invalid start_id: {exc}") from exc

        if start_id is None:
            raise ValueError(f"No matching start_id found: {start_id}")

        tree = self.build_pattern_tree(
            start_id=start_id,
            pattern=pattern,
            hide_connectivity=hide_connectivity,
            full_id=full_id,
            maximum_children=maximum_children,
        )

        tree.show()

    def build_pattern_tree(
        self,
        start_id: str,
        pattern: list,
        hide_connectivity: bool = False,
        full_id: bool = False,
        maximum_children: int = 5,
    ) -> Tree:
        """
        Bouw een boom op basis van een expliciet patroon van CIM-types.

        Elk niveau kan één of meerdere toegestane types bevatten.
        Alleen direct verbonden objecten worden meegenomen.

        Met hide_connectivity=True worden Terminal en ConnectivityNode
        wel gevolgd, maar niet weergegeven in de boom.

        maximum_children begrenst het aantal zichtbare kinderen
        per boomnode.
        """

        if not pattern:
            raise ValueError("pattern mag niet leeg zijn")

        if maximum_children < 0:
            raise ValueError("maximum_children must be >= 0")

        levels = [
            [level] if isinstance(level, str) else list(level)
            for level in pattern
        ]

        if any(not level for level in levels):
            raise ValueError(
                "Elk pattern-niveau moet minstens één type bevatten"
            )

        if start_id not in self.nodes:
            raise ValueError(f"Unknown start_id: {start_id}")

        hidden_types = {
            "cim:Terminal",
            "cim:ConnectivityNode",
        } if hide_connectivity else set()

        type_cache = {}

        def get_type(node_id):
            if node_id not in type_cache:
                node = self.nodes.get(node_id, {})
                value = node.get("@type", "")

                if isinstance(value, list):
                    value = value[0] if value else ""

                type_cache[node_id] = value

            return type_cache[node_id]

        start_type = get_type(start_id)

        if start_type not in levels[0]:
            raise ValueError(
                f"Start object should be of type(s) {levels[0]!r}, "
                f"but is of type {start_type!r}"
            )

        def find_matches(node_id, target_types, path_ids):
            relationships = (
                self.relations.incoming(node_id)
                + self.relations.outgoing(node_id)
            )

            matches = []
            seen = set()

            for neighbor_id, _ in relationships:
                if neighbor_id in seen or neighbor_id in path_ids:
                    continue

                seen.add(neighbor_id)

                if neighbor_id not in self.nodes:
                    continue

                if get_type(neighbor_id) in target_types:
                    matches.append(neighbor_id)

            return matches

        tree = Tree()
        root_tree_id = str(uuid4())

        tree.create_node(
            self.label(start_id, full_id),
            root_tree_id,
        )

        # Aantallen per zichtbare boomnode.
        # Dit zorgt ervoor dat ook kinderen via meerdere verborgen
        # connectivity-objecten gezamenlijk worden begrensd.
        child_counts = {}
        omitted_counts = {}

        def expand(
            node_id,
            parent_tree_id,
            level_index,
            path_ids,
        ):
            if level_index >= len(levels):
                return

            matches = find_matches(
                node_id,
                set(levels[level_index]),
                path_ids,
            )

            for match_id in matches:
                match_type = get_type(match_id)
                hidden = match_type in hidden_types

                # Verborgen objecten krijgen geen eigen boomnode.
                # Hun kinderen worden aan dezelfde zichtbare ouder gekoppeld.
                child_tree_id = parent_tree_id

                if not hidden:
                    count = child_counts.get(parent_tree_id, 0)

                    if count >= maximum_children:
                        omitted_counts[parent_tree_id] = (
                            omitted_counts.get(parent_tree_id, 0) + 1
                        )
                        continue

                    child_tree_id = str(uuid4())

                    tree.create_node(
                        self.label(match_id, full_id),
                        child_tree_id,
                        parent=parent_tree_id,
                    )

                    child_counts[parent_tree_id] = count + 1

                expand(
                    match_id,
                    child_tree_id,
                    level_index + 1,
                    path_ids | {match_id},
                )

        expand(
            start_id,
            root_tree_id,
            1,
            {start_id},
        )

        # Voeg de aanduidingen voor weggelaten kinderen achteraf toe.
        for parent_tree_id, omitted in omitted_counts.items():
            tree.create_node(
                f"... ({omitted} more)",
                str(uuid4()),
                parent=parent_tree_id,
            )

        return tree
