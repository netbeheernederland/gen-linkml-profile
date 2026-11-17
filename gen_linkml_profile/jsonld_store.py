"""
Lazy, dictionary-like access to objects in one or more JSON-LD files.

The source files are expected to contain a top-level "@graph" array whose
elements are JSON objects with an "@id".

The on-disk index contains:
    16-byte BLAKE2b hash of @id
    8-byte source-file byte offset
    4-byte JSON object length

Index records are sorted by hash. The original JSON files are never modified.
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
from typing import Iterable, Iterator, TextIO

RECORD = struct.Struct(">16sQI")
RECORD_SIZE = RECORD.size


class JsonLdStore:
    def __init__(
        self,
        files: Iterable[str | os.PathLike | TextIO],
        index_dir: str | os.PathLike = ".jsonld_index",
        cache_size: int = 1000,
        chunk_size: int = 100_000,
    ):
        # Accept paths or already-open text/binary file handles.
        self.files = [
            Path(f) if isinstance(f, (str, os.PathLike)) else Path(f.name)
            for f in files
        ]
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.cache_size = max(0, int(cache_size))
        self.chunk_size = max(1, int(chunk_size))

        self._data_handles = []
        self._index_handles = []
        self._indexes = []
        self._closed = False

        for file_id, source in enumerate(self.files):
            index_path = self.index_dir / f"{file_id:04d}.idx"
            meta_path = self.index_dir / f"{file_id:04d}.meta"
            self._ensure_index(source, index_path, meta_path)

            data_handle = open(source, "rb")
            index_handle = open(index_path, "rb")
            self._data_handles.append(data_handle)
            self._index_handles.append(index_handle)

            if index_path.stat().st_size:
                index_map = mmap.mmap(
                    index_handle.fileno(), 0, access=mmap.ACCESS_READ
                )
            else:
                index_map = None
            self._indexes.append(index_map)

        self._cache = {}

    @staticmethod
    def _hash(node_id: str) -> bytes:
        return hashlib.blake2b(
            node_id.encode("utf-8"), digest_size=16
        ).digest()

    def _ensure_index(self, source: Path, index_path: Path, meta_path: Path):
        stat = source.stat()
        signature = (
            f"{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
        )

        if index_path.exists() and meta_path.exists():
            if meta_path.read_text(encoding="ascii") == signature:
                return

        print(f"Building JSON-LD index: {source}")
        self._build_index(source, index_path)

        tmp_meta = meta_path.with_suffix(".meta.tmp")
        tmp_meta.write_text(signature, encoding="ascii")
        tmp_meta.replace(meta_path)

    def _build_index(self, source: Path, index_path: Path):
        runs: list[Path] = []
        records = []

        try:
            with open(source, "rb") as source_file:
                if source.stat().st_size == 0:
                    raise ValueError(f"Empty JSON file: {source}")

                with mmap.mmap(
                    source_file.fileno(), 0, access=mmap.ACCESS_READ
                ) as mm:
                    graph_start = self._find_graph(mm)

                    for offset, length in self._iter_graph_objects(
                        mm, graph_start
                    ):
                        node = json.loads(mm[offset:offset + length])
                        node_id = node.get("@id")

                        if not isinstance(node_id, str) or not node_id:
                            continue

                        records.append((self._hash(node_id), offset, length))

                        if len(records) >= self.chunk_size:
                            runs.append(self._write_run(records))
                            records = []

            if records:
                runs.append(self._write_run(records))

            tmp_index = index_path.with_suffix(".idx.tmp")
            self._merge_runs(runs, tmp_index)
            tmp_index.replace(index_path)

        finally:
            for run in runs:
                try:
                    run.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _find_graph(mm: mmap.mmap) -> int:
        # This locates the top-level @graph key in the expected input format.
        marker = b'"@graph"'
        pos = mm.find(marker)
        if pos < 0:
            raise ValueError('No "@graph" key found')

        pos = mm.find(b"[", pos + len(marker))
        if pos < 0:
            raise ValueError('"@graph" is not followed by an array')

        return pos + 1

    @staticmethod
    def _iter_graph_objects(
        mm: mmap.mmap, pos: int
    ) -> Iterator[tuple[int, int]]:
        """Yield (byte_offset, byte_length) for each object in @graph."""
        size = len(mm)

        while pos < size:
            while pos < size and mm[pos] in b" \t\r\n,":
                pos += 1

            if pos >= size or mm[pos] == ord("]"):
                return

            if mm[pos] != ord("{"):
                raise ValueError(
                    f"Expected an object in @graph at byte offset {pos}"
                )

            start = pos
            depth = 0
            in_string = False
            escaped = False

            while pos < size:
                char = mm[pos]

                if in_string:
                    if escaped:
                        escaped = False
                    elif char == ord("\\"):
                        escaped = True
                    elif char == ord('"'):
                        in_string = False
                else:
                    if char == ord('"'):
                        in_string = True
                    elif char in (ord("{"), ord("[")):
                        depth += 1
                    elif char in (ord("}"), ord("]")):
                        depth -= 1
                        if depth == 0:
                            pos += 1
                            yield start, pos - start
                            break

                pos += 1
            else:
                raise ValueError("Unterminated JSON object in @graph")

    def _write_run(self, records: list[tuple[bytes, int, int]]) -> Path:
        records.sort()
        fd, filename = tempfile.mkstemp(
            prefix="jsonld-", suffix=".run", dir=self.index_dir
        )
        with os.fdopen(fd, "wb") as run_file:
            for record in records:
                run_file.write(RECORD.pack(*record))
        return Path(filename)

    @staticmethod
    def _read_run(run_path: Path):
        with open(run_path, "rb") as run_file:
            while True:
                raw = run_file.read(RECORD_SIZE)
                if not raw:
                    return
                if len(raw) != RECORD_SIZE:
                    raise IOError(f"Truncated index run: {run_path}")
                yield RECORD.unpack(raw)

    def _merge_runs(self, runs: list[Path], output_path: Path):
        with open(output_path, "wb") as output:
            for record in heapq.merge(
                *(self._read_run(run) for run in runs)
            ):
                output.write(RECORD.pack(*record))

    def _find_location(self, node_id: str):
        digest = self._hash(node_id)

        # Later input files override earlier ones, matching dict assignment.
        for file_id in range(len(self.files) - 1, -1, -1):
            index = self._indexes[file_id]
            if index is None:
                continue

            count = len(index) // RECORD_SIZE
            lo, hi = 0, count

            # Lower bound for the requested hash.
            while lo < hi:
                mid = (lo + hi) // 2
                record_hash = RECORD.unpack_from(
                    index, mid * RECORD_SIZE
                )[0]
                if record_hash < digest:
                    lo = mid + 1
                else:
                    hi = mid

            # Check every record with this hash. This also handles the
            # astronomically unlikely event of a 128-bit hash collision.
            matches = []
            pos = lo
            while pos < count:
                record_hash, offset, length = RECORD.unpack_from(
                    index, pos * RECORD_SIZE
                )
                if record_hash != digest:
                    break
                matches.append((offset, length))
                pos += 1

            # Last occurrence within a file wins.
            for offset, length in reversed(matches):
                raw = os.pread(
                    self._data_handles[file_id].fileno(), length, offset
                )
                node = json.loads(raw)
                if node.get("@id") == node_id:
                    return file_id, offset, length

        return None

    def __getitem__(self, node_id: str):
        if node_id in self._cache:
            return self._cache[node_id]

        location = self._find_location(node_id)
        if location is None:
            raise KeyError(node_id)

        file_id, offset, length = location
        raw = os.pread(
            self._data_handles[file_id].fileno(), length, offset
        )
        node = json.loads(raw)

        if self.cache_size:
            self._cache[node_id] = node
            # Simple bounded FIFO cache.
            if len(self._cache) > self.cache_size:
                self._cache.pop(next(iter(self._cache)))

        return node

    def get(self, node_id: str, default=None):
        try:
            return self[node_id]
        except KeyError:
            return default

    def __contains__(self, node_id: object) -> bool:
        if not isinstance(node_id, str):
            return False
        return self._find_location(node_id) is not None

    def iter_items(self):
        """
        Iterate over unique IDs using last-file-wins semantics.

        The per-file indexes are already sorted by hash. A merge groups equal
        hashes; only IDs in the current hash group need temporary memory.
        """
        iterators = []
        for file_id, index in enumerate(self._indexes):
            if index is None:
                continue
            iterators.append(self._iter_index(file_id, index))

        for digest, group in self._group_by_hash(heapq.merge(*iterators)):
            # Hash collisions are resolved by comparing the real @id.
            winners = {}
            for file_id, offset, length in group:
                raw = os.pread(
                    self._data_handles[file_id].fileno(), length, offset
                )
                node = json.loads(raw)
                node_id = node.get("@id")
                if not isinstance(node_id, str):
                    continue

                previous = winners.get(node_id)
                if previous is None or (file_id, offset) > previous[0]:
                    winners[node_id] = ((file_id, offset), node)

            for node_id, (_, node) in winners.items():
                yield node_id, node

    @staticmethod
    def _iter_index(file_id: int, index: mmap.mmap):
        count = len(index) // RECORD_SIZE
        for i in range(count):
            digest, offset, length = RECORD.unpack_from(
                index, i * RECORD_SIZE
            )
            yield digest, file_id, offset, length

    @staticmethod
    def _group_by_hash(records):
        from itertools import groupby
        for digest, group in groupby(records, key=lambda r: r[0]):
            yield digest, [
                (file_id, offset, length)
                for _, file_id, offset, length in group
            ]

    def items(self):
        return self.iter_items()

    def keys(self):
        for node_id, _ in self.iter_items():
            yield node_id

    def values(self):
        for _, node in self.iter_items():
            yield node

    def __iter__(self):
        return self.keys()

    def __len__(self):
        return sum(1 for _ in self.iter_items())

    def clear_cache(self):
        self._cache.clear()

    def close(self):
        if self._closed:
            return
        for index in self._indexes:
            if index is not None:
                index.close()
        for handle in self._index_handles:
            handle.close()
        for handle in self._data_handles:
            handle.close()
        self._cache.clear()
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
