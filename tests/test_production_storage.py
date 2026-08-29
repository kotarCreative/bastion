"""Repository backup and restore disaster recovery."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import forge
from errors import ForgeError
from invariants import check_invariants
from store import backup_forge, restore_forge


def _cleanup_directory(directory: Path) -> None:
    import shutil

    shutil.rmtree(directory, ignore_errors=True)


class BackupRestoreTest(unittest.TestCase):
    def test_wal_backup_reads_one_pinned_manifest_generation(self) -> None:
        from repository import Repository
        from tests.tree_harness import TreeFixture
        from unittest.mock import patch

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup_directory, base)
        original_get = Repository.get
        manifest_reads = 0

        def get(repository, key, **kwargs):
            nonlocal manifest_reads
            if key == "manifest.json":
                manifest_reads += 1
                if manifest_reads > 1:
                    raise AssertionError("backup re-read a moving manifest")
            return original_get(repository, key, **kwargs)

        with patch.object(Repository, "get", new=get):
            backup_forge(fixture.root, base / "backup")
        self.assertEqual(1, manifest_reads)

    def test_backup_restore_reconstructs_wal_and_accepts_pushes(self) -> None:
        from tests.storage_helpers import repository_state
        from tests.transport import attach_intake_push
        from tests.tree_harness import TreeFixture, build_commit, command

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        client = fixture.clients["alice"]
        build_commit(
            client,
            "HEAD",
            {b"shared/backup.txt": (b"100644", b"backup payload\n")},
            b"backup commit\n",
            1_700_000_100,
        )
        command("git", "-C", client, "push", "-q", "origin", "main")
        forge.drain_projection_jobs(fixture.root)

        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup_directory, base)
        destination = base / "backup-dir"
        restored_root = base / "restored"
        backup_forge(fixture.root, destination)
        metadata = json.loads((destination / "manifest.json").read_text())
        self.assertEqual("bastion-wal-backup", metadata["format"])
        self.assertIn("manifest.json", {item["key"] for item in metadata["objects"]})
        self.assertFalse((destination / "repos").exists())

        restore_forge(destination, restored_root)

        check_invariants(restored_root)
        original_manifest, original_replay = repository_state(fixture.root)
        restored_manifest, restored_replay = repository_state(restored_root)
        self.assertEqual(original_manifest.to_dict(), restored_manifest.to_dict())
        self.assertEqual(
            [record["event_id"] for record in original_replay.records],
            [record["event_id"] for record in restored_replay.records],
        )
        hook = restored_root / "intake" / "alice.git" / "hooks" / "pre-receive"
        self.assertTrue(hook.is_file())
        self.assertTrue(hook.stat().st_mode & 0o111)

        clone = base / "clone"
        command("git", "clone", "-q", "--no-local", restored_root / "views" / "alice.git", clone)
        attach_intake_push(restored_root, clone, "alice")
        command("git", "-C", clone, "config", "user.name", "Alice")
        command("git", "-C", clone, "config", "user.email", "alice@example.test")
        restored_tip = command(
            "git", "-C", clone, "rev-parse", "HEAD"
        ).stdout.decode().strip()
        new = build_commit(
            clone,
            restored_tip,
            {b"shared/restored.txt": (b"100644", b"restored push\n")},
            b"restored push\n",
            1_700_000_200,
        )
        command("git", "-C", clone, "push", "-q", "origin", "main")
        next_manifest, next_replay = repository_state(restored_root)
        self.assertEqual(restored_manifest.head_seq + 1, next_manifest.head_seq)
        self.assertEqual(new, next_replay.records[-1]["payload"]["projected_new"])

    def test_restore_refuses_nonempty_target_and_bad_format(self) -> None:
        from tests.tree_harness import TreeFixture

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup_directory, base)
        destination = base / "backup-dir"
        backup_forge(fixture.root, destination)

        occupied = base / "occupied"
        occupied.mkdir()
        (occupied / "file.txt").write_text("x")
        with self.assertRaises(ForgeError):
            restore_forge(destination, occupied)

        empty = base / "empty"
        empty.mkdir()
        restore_forge(destination, empty)
        check_invariants(empty)

        manifest_path = destination / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        external = base / "external-object"
        external.write_bytes(b"outside backup\n")
        linked = destination / "objects" / "unreferenced" / "external"
        linked.parent.mkdir()
        linked.symlink_to(external)
        manifest["objects"].append(
            {
                "key": "unreferenced/external",
                "sha256": hashlib.sha256(external.read_bytes()).hexdigest(),
                "size": external.stat().st_size,
            }
        )
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ForgeError, "symlink"):
            restore_forge(destination, base / "symlink-target")

        manifest["format"] = "unsupported-backup"
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(ForgeError):
            restore_forge(destination, base / "fresh")

    def test_restore_rehomes_an_absolute_object_store(self) -> None:
        from repository import Repository
        from tests.identity_harness import add_view
        from tests.storage_helpers import repository_snapshot, repository_state
        from tests.tree_harness import build_commit, command

        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup_directory, base)
        source = base / "source"
        command("git", "init", "-q", "-b", "main", source)
        build_commit(
            source,
            None,
            {b"shared/base.txt": (b"100644", b"base\n")},
            b"initial\n",
            1_700_000_000,
        )
        original = base / "original"
        external_store = base / "external-object-store"
        forge.init_forge(
            original,
            source,
            "main",
            object_store=f"filesystem:{external_store.resolve()}",
        )
        add_view(original, "alice", ["shared"], ["shared"])
        original_snapshot = repository_snapshot(original)

        backup = base / "backup"
        restored = base / "restored"
        backup_forge(original, backup)
        restore_forge(backup, restored)

        config = json.loads((restored / "repository.json").read_text())
        self.assertEqual("object-store", config["object_store"]["path"])
        self.assertEqual((restored / "object-store").resolve(), Repository.open(restored).store.root)
        self.assertNotEqual(external_store.resolve(), Repository.open(restored).store.root)
        self.assertEqual(original_snapshot, repository_snapshot(original))
        self.assertEqual(
            repository_state(original)[0].to_dict(),
            repository_state(restored)[0].to_dict(),
        )
        check_invariants(restored)


if __name__ == "__main__":
    unittest.main()
