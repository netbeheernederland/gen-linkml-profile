"""
Persistent reverse relationship index.

For each reference target @id, stores (source_id, relationship_property).
The index is sorted by a 128-bit hash of the target ID and can be searched
without loading the full index into Python memory.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import mmap
import os
import struct
import tempfile
from pathlib import Path

RECORD = struct.Struct(">16sQI")  # target hash, payload offset, payload size
RECORD_SIZE = RECORD.size
U32 = struct.Struct(">I")


class RelationIndex:
    def __init__(
        self,
        nodes,
        index_path=".jsonld_relations.idx",
        payload_path=".jsonld_relations.data",
        chunk_size=200_000,
        rebuild=False,
    ):
        self.nodes = nodes
        self.index_path = Path(index_path)
        self.payload_path = Path(payload_path)
        self.chunk_size = max(1, int(chunk_size))
        self.meta_path = self.index_path.with_suffix(
            self.index_path.suffix + ".meta"
        )
        self._index_handle = None
        self._payload_handle = None
        self._index_map = None
        self._open_or_build(rebuild)

    @staticmethod
    def _hash(value: str) -> bytes:
        return hashlib.blake2b(
            value.encode("utf-8"), digest_size=16
        ).digest()

    def _signature(self):
        return "|".join(
            f"{p.resolve()}:{p.stat().st_size}:{p.stat().st_mtime_ns}"
            for p in self.nodes.files
        )

    def _open_or_build(self, rebuild):
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.payload_path.parent.mkdir(parents=True, exist_ok=True)

        signature = self._signature()
        valid = (
            self.index_path.exists()
            and self.payload_path.exists()
            and self.meta_path.exists()
            and self.meta_path.read_text(encoding="utf-8") == signature
        )

        if rebuild or not valid:
            self.build()

        self._index_handle = open(self.index_path, "rb")
        self._payload_handle = open(self.payload_path, "rb")
        if self.index_path.stat().st_size:
            self._index_map = mmap.mmap(
                self._index_handle.fileno(), 0, access=mmap.ACCESS_READ
            )

    def build(self):
        """
        Scan the effective node collection once.

        JsonLdStore.iter_items() applies the same last-file-wins rule as
        nodes[node_id], so overwritten source objects do not leave stale
        incoming relationships in this index.
        """
        runs = []
        records = []
        self.index_path.parent.mkdir(parents=True, exist_ok=True)

        tmp_payload = self.payload_path.with_suffix(
            self.payload_path.suffix + ".tmp"
        )
        tmp_index = self.index_path.with_suffix(
            self.index_path.suffix + ".tmp"
        )

        try:
            with open(tmp_payload, "wb") as payload:
                for source_id, node in self.nodes.iter_items():
                    for prop, target_id in self._references(node):
                        target_bytes = target_id.encode("utf-8")
                        source_bytes = source_id.encode("utf-8")
                        prop_bytes = prop.encode("utf-8")

                        # Payload includes the target ID for exact hash
                        # collision checking.
                        offset = payload.tell()
                        payload.write(U32.pack(len(target_bytes)))
                        payload.write(U32.pack(len(source_bytes)))
                        payload.write(U32.pack(len(prop_bytes)))
                        payload.write(target_bytes)
                        payload.write(source_bytes)
                        payload.write(prop_bytes)
                        payload_length = payload.tell() - offset

                        records.append((
                            self._hash(target_id), offset, payload_length
                        ))

                        if len(records) >= self.chunk_size:
                            runs.append(self._write_run(records))
                            records = []

            if records:
                runs.append(self._write_run(records))

            with open(tmp_index, "wb") as output:
                for record in heapq.merge(
                    *(self._read_run(run) for run in runs)
                ):
                    output.write(RECORD.pack(*record))

            tmp_payload.replace(self.payload_path)
            tmp_index.replace(self.index_path)

            self.meta_path.write_text(
                self._signature(), encoding="utf-8"
            )
        finally:
            for run in runs:
                try:
                    run.unlink()
                except FileNotFoundError:
                    pass
            for temp in (tmp_payload, tmp_index):
                try:
                    temp.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _references(node):
        for prop, value in node.items():
            if prop.startswith("@"):
                continue

            if isinstance(value, dict):
                target = value.get("@id")
                if isinstance(target, str):
                    yield prop, target

            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        target = item.get("@id")
                        if isinstance(target, str):
                            yield prop, target
                    elif isinstance(item, str):
                        # Retain the behaviour of the original visualiser.
                        yield prop, item

    def _write_run(self, records):
        records.sort()
        fd, filename = tempfile.mkstemp(
            prefix="relations-", suffix=".run",
            dir=self.index_path.parent
        )
        with os.fdopen(fd, "wb") as f:
            for record in records:
                f.write(RECORD.pack(*record))
        return Path(filename)

    @staticmethod
    def _read_run(path):
        with open(path, "rb") as f:
            while True:
                raw = f.read(RECORD_SIZE)
                if not raw:
                    return
                if len(raw) != RECORD_SIZE:
                    raise IOError(f"Truncated relation index run: {path}")
                yield RECORD.unpack(raw)

    def outgoing(self, source_id: str):
        """
        Return outgoing (target_id, property_name) relationships.

        OUT relationships are already present on the source object, so they
        are read on demand rather than duplicated in the persistent index.
        """
        node = self.nodes.get(source_id)
        if node is None:
            return []

        result = []
        for prop, value in node.items():
            if prop.startswith("@"):
                continue

            if isinstance(value, dict):
                target = value.get("@id")
                if isinstance(target, str):
                    result.append((target, prop))

            elif isinstance(value, list):
                for item in value:
                    target = None
                    if isinstance(item, dict):
                        target = item.get("@id")
                    elif isinstance(item, str):
                        target = item

                    if isinstance(target, str):
                        result.append((target, prop))

        return result

    def incoming(self, target_id: str):
        if self._index_map is None:
            return []

        digest = self._hash(target_id)
        index = self._index_map
        count = len(index) // RECORD_SIZE

        lo, hi = 0, count
        while lo < hi:
            mid = (lo + hi) // 2
            record_hash = RECORD.unpack_from(
                index, mid * RECORD_SIZE
            )[0]
            if record_hash < digest:
                lo = mid + 1
            else:
                hi = mid

        result = []
        pos = lo
        while pos < count:
            record_hash, offset, length = RECORD.unpack_from(
                index, pos * RECORD_SIZE
            )
            if record_hash != digest:
                break

            raw = os.pread(self._payload_handle.fileno(), length, offset)
            target_len, source_len, prop_len = struct.unpack_from(">III", raw, 0)
            cursor = 12
            target = raw[cursor:cursor + target_len].decode("utf-8")
            cursor += target_len
            source = raw[cursor:cursor + source_len].decode("utf-8")
            cursor += source_len
            prop = raw[cursor:cursor + prop_len].decode("utf-8")

            if target == target_id:
                result.append((source, prop))

            pos += 1

        return result

    def close(self):
        if self._index_map is not None:
            self._index_map.close()
            self._index_map = None
        if self._index_handle is not None:
            self._index_handle.close()
            self._index_handle = None
        if self._payload_handle is not None:
            self._payload_handle.close()
            self._payload_handle = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
