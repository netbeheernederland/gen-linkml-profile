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
        name = f"{name} |{value}|" if value else name

        node_type = node.get("@type", "")
        if isinstance(node_type, list):
            node_type = node_type[0] if node_type else ""
        node_type = str(node_type).replace("cim:", "")
        node_code = node_id if full_id else node_id[-8:]

        return f"[{node_type}] {name} ({node_code})"

    def show(
        self,
        start_id: str,
        max_depth: int = 3,
        max_children: int = 5,
        full_id: bool = False,
        recursive: bool = False,
        exclude_types=None,
    ):
        if start_id is None:
            start_id = next(iter(self.nodes), None)
            if start_id is None:
                raise ValueError("The node collection is empty")

        if start_id not in self.nodes:
            # Preserve the original convenience of accepting a partial ID.
            start_id = next(
                (node_id for node_id in self.nodes if start_id in node_id),
                None,
            )

        if start_id is None:
            raise ValueError("No matching start_id found")

        self.build_tree(
            start_id=start_id,
            max_depth=max_depth,
            max_children=max_children,
            full_id=full_id,
            recursive=recursive,
            exclude_types=exclude_types,
        ).show()

    def build_tree(
        self,
        start_id: str,
        max_depth: int = 3,
        max_children: int = 5,
        full_id: bool = False,
        recursive: bool = False,
        exclude_types=None,
    ) -> Tree:
        if start_id not in self.nodes:
            raise ValueError(f"Unknown start_id: {start_id}")

        exclude_types = set(exclude_types or [])
        tree = Tree()
        root_id = str(uuid4())

        tree.create_node(self.label(start_id, full_id), root_id)

        queue = deque([(start_id, root_id, 0)])
        expanded_subtrees = {start_id}

        def node_type(node_id):
            node = self.nodes.get(node_id, {})
            value = node.get("@type", "")
            if isinstance(value, list):
                value = value[0] if value else ""
            return value

        def is_eligible(node_id):
            return node_id in self.nodes and node_type(node_id) not in exclude_types

        def can_expand(node_id):
            return is_eligible(node_id) and node_id not in expanded_subtrees

        def expand(target_id, parent_id, depth):
            if not can_expand(target_id):
                return False

            child_id = str(uuid4())
            tree.create_node(
                self.label(target_id, full_id),
                child_id,
                parent=parent_id,
            )
            expanded_subtrees.add(target_id)
            queue.append((target_id, child_id, depth))
            return True

        def add_relationship_group(relationships, label, parent_id, depth):
            candidates = [
                target_id
                for target_id, _ in relationships
                if can_expand(target_id)
            ]
            if not candidates:
                return

            total = len(candidates)
            selected = candidates[:max_children]
            group_label = (
                f"{label} (showing {max_children} of {total})"
                if total > max_children
                else label
            )
            group_id = str(uuid4())
            tree.create_node(group_label, group_id, parent=parent_id)

            for target_id in selected:
                expand(target_id, group_id, depth + 1)

        while queue:
            node_id, parent_id, depth = queue.popleft()

            if depth >= max_depth:
                if recursive:
                    relationships = (
                        self.relations.outgoing(node_id)
                        + self.relations.incoming(node_id)
                    )
                    for target_id, _ in relationships:
                        if is_eligible(target_id):
                            tree.create_node(
                                f"↩ {self.label(target_id, full_id)}",
                                str(uuid4()),
                                parent=parent_id,
                            )
                            break
                continue

            node = self.nodes[node_id]
            add_relationship_group(
                self.relations.incoming(node_id), "IN", parent_id, depth
            )
            add_relationship_group(
                self.relations.outgoing(node_id), "OUT", parent_id, depth
            )

        return tree
