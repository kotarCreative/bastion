"""Backup/restore coverage for the operator-only canonical branch foundation."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import forge
from tests.identity_harness import provision_admin
from service import create_branch
from store import backup_forge, restore_forge
from tests.storage_helpers import repository_state
from tests.tree_harness import TreeFixture


class BranchBackupTest(unittest.TestCase):
    def test_backup_fence_preserves_slash_containing_default_branch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="branch-backup-slash-") as raw:
            base = Path(raw)
            source = base / "source"
            source.mkdir()
            root = base / "forge"
            subprocess.run(
                ["git", "init", "-q", "-b", "release/main", str(source)],
                check=True,
            )
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.test"], check=True)
            (source / "tracked.txt").write_text("test\n")
            subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "initial"], check=True)

            forge.init_forge(root, source, "release/main")
            principal = provision_admin(root)
            create_branch(root, "feature", principal)
            backup = base / "backup"
            backup_forge(root, backup)
            restored = base / "restored"
            restore_forge(backup, restored)
            manifest, _replay = repository_state(restored)
            self.assertEqual(manifest.canonical_ref, "refs/heads/release/main")
            self.assertIn("refs/heads/feature", manifest.canonical_refs)

    def test_branch_and_captured_descriptor_survive_pinned_backup_restore(self) -> None:
        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        created = create_branch(fixture.root, "release/v1", fixture.admin_principal)
        self.assertTrue(created["created"])

        backup = fixture.directory / "branch-backup"
        backup_forge(fixture.root, backup)
        pinned, replay = repository_state(fixture.root)
        event = next(
            record for record in replay.records
            if record["kind"] == "branch_create"
        )
        descriptor_id = event["payload"]["descriptor_id"]
        self.assertNotIn(
            descriptor_id,
            {view["descriptor_id"] for view in replay.active_views.values()},
            "fixture must exercise a descriptor reachable only from branch_create",
        )
        copied = {item["key"] for item in json.loads(
            (backup / "manifest.json").read_text()
        )["objects"]}
        self.assertIn(f"descriptors/{descriptor_id}.json", copied)

        restored = fixture.directory / "branch-restored"
        restore_forge(backup, restored)
        restored_manifest, restored_replay = repository_state(restored)
        self.assertEqual(restored_manifest.canonical_refs, pinned.canonical_refs)
        self.assertEqual(
            restored_manifest.canonical_refs["refs/heads/release/v1"],
            created["tip"],
        )
        self.assertEqual(
            next(r for r in restored_replay.records if r["kind"] == "branch_create")["payload"]["descriptor_id"],
            descriptor_id,
        )
        forge.check_invariants(restored)


if __name__ == "__main__":
    unittest.main()
