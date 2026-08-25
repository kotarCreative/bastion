"""Global invariant checks for manifest/WAL repository truth."""

from __future__ import annotations

import json
import unittest

import forge
from errors import ForgeError
from invariants import check_invariants
from object_store import Update
from repository import Repository
from state import load_state, save_state
from tests.storage_helpers import repository_snapshot
from tests.tree_harness import TreeFixture


class WalInvariantTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()
        self.addCleanup(self.fixture.close)

    def test_healthy_forge_is_read_only(self) -> None:
        before = repository_snapshot(self.fixture.root)
        check_invariants(self.fixture.root)
        self.assertEqual(before, repository_snapshot(self.fixture.root))

    def test_missing_git_caches_are_legal_and_reconstructable(self) -> None:
        forge.canonical_repo(self.fixture.root).unlink()
        forge.view_repo(self.fixture.root, "alice").unlink()
        check_invariants(self.fixture.root)

        forge.sync_view(self.fixture.root, "alice")

        self.assertTrue(forge.canonical_repo(self.fixture.root).is_symlink())
        self.assertTrue(forge.view_repo(self.fixture.root, "alice").is_symlink())
        check_invariants(self.fixture.root)

    def test_missing_manifest_referenced_pack_is_corruption(self) -> None:
        repository = Repository.open(self.fixture.root)
        manifest, _version, _replay = repository.load()
        repository.delete(manifest.canonical_packs[0]["pack_key"])

        with self.assertRaisesRegex(ForgeError, "referenced pack is missing"):
            check_invariants(self.fixture.root)

    def test_hash_mismatched_manifest_referenced_pack_is_corruption(self) -> None:
        repository = Repository.open(self.fixture.root)
        manifest, _version, _replay = repository.load()
        key = manifest.canonical_packs[0]["pack_key"]
        current = repository.get(key)
        repository.put(key, b"corrupt", mode=Update(current.meta.version))

        with self.assertRaisesRegex(ForgeError, "hash or size differs"):
            check_invariants(self.fixture.root)

    def test_corrupt_linked_wal_record_fails_closed(self) -> None:
        repository = Repository.open(self.fixture.root)
        manifest, _version, _replay = repository.load()
        current = repository.get(manifest.wal_head.key)
        record = json.loads(current.body)
        record["created_at"] += 1
        repository.put(
            manifest.wal_head.key,
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii"),
            mode=Update(current.meta.version),
        )

        with self.assertRaisesRegex(ForgeError, "record hash differs"):
            check_invariants(self.fixture.root)

    def test_active_view_alternate_is_rejected(self) -> None:
        view = forge.view_repo(self.fixture.root, "alice").resolve()
        alternate = view / "objects" / "info" / "alternates"
        alternate.write_text(f"{forge.canonical_repo(self.fixture.root).resolve() / 'objects'}\n")

        with self.assertRaisesRegex(ForgeError, "active view has an alternate"):
            check_invariants(self.fixture.root)

    def test_missing_rebuildable_indexes_are_not_corruption(self) -> None:
        state = load_state(self.fixture.root)
        state["transactions"] = {}
        state["changes"] = {}
        state["projection_jobs"] = {}
        state["views"] = {}
        state["last_indexed_seq"] = 0
        save_state(self.fixture.root, state)

        check_invariants(self.fixture.root)

    def test_repository_id_fence_mismatch_is_rejected(self) -> None:
        state = load_state(self.fixture.root)
        state["repository_id"] = "f" * 32
        save_state(self.fixture.root, state)

        with self.assertRaisesRegex(ForgeError, "repository ID differs"):
            check_invariants(self.fixture.root)


if __name__ == "__main__":
    unittest.main()
