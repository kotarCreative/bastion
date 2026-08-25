"""Conditional object-store conformance tests for WAL repository truth."""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from object_store import Create, NotModified, PreconditionFailed, Update
from object_store_fs import FilesystemObjectStore
from object_store_memory import MemoryObjectStore


class ObjectStoreTest(unittest.TestCase):
    def test_memory_create_update_range_and_conditional_delete(self) -> None:
        store = MemoryObjectStore()
        created = store.put("objects/a", b"abcdef", mode=Create())
        self.assertEqual(b"bcd", store.get("objects/a", byte_range=(1, 3)).body)
        with self.assertRaises(NotModified):
            store.get("objects/a", if_none_match=created.version)
        with self.assertRaises(PreconditionFailed):
            store.put("objects/a", b"abcdef", mode=Create())
        updated = store.put("objects/a", b"next", mode=Update(created.version))
        with self.assertRaises(PreconditionFailed):
            store.delete("objects/a", if_version=created.version)
        store.delete("objects/a", if_version=updated.version)

    def test_independent_filesystem_instances_have_one_create_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = FilesystemObjectStore(root)
            second = FilesystemObjectStore(root)
            outcomes: list[str] = []

            def create(store, body: bytes) -> None:
                try:
                    store.put("manifest.json", body, mode=Create())
                    outcomes.append("won")
                except PreconditionFailed:
                    outcomes.append("lost")

            left = threading.Thread(target=create, args=(first, b"one"))
            right = threading.Thread(target=create, args=(second, b"two"))
            left.start(); right.start(); left.join(); right.join()
            self.assertEqual(sorted(outcomes), ["lost", "won"])
            self.assertIn(first.get("manifest.json").body, {b"one", b"two"})
