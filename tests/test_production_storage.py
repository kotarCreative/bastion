"""Legacy JSON import, job lifecycle, and backup/restore disaster recovery."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import forge
from errors import ForgeError
from store import backup_forge, restore_forge
from invariants import check_invariants
from outbox import MAX_JOB_ATTEMPTS, fail_job, retry_delay
from state import load_state, save_state
from store import backend_record_path, initialize_store, open_store
from tests.storage_helpers import read_document
from worker import (
    cancel_projection_job,
    claim_projection_job,
    complete_projection_job,
    fail_projection_job,
    requeue_projection_job,
    run_projection_job,
)


def sample_document() -> dict:
    return {
        "canonical_branch": "main",
        "principals": {"alice": {"id": "alice", "kind": "user"}},
        "teams": {},
        "roles": {},
        "role_bindings": {},
        "view_descriptors": {},
        "views": {
            "alice": {
                "principal_id": "alice",
                "descriptor_id": "vdesc-1",
                "descriptor_generation": 1,
                "canonical_ref": "refs/heads/main",
                "projected_ref": "refs/heads/main",
                "canonical_frontier": "0" * 40,
                "projected_tip": "0" * 40,
                "anchors": {},
            }
        },
        "transactions": {},
        "changes": {},
        "outbox": {},
        "projection_jobs": {},
        "next_transaction_sequence": 7,
    }


class MigrationTest(unittest.TestCase):
    def _legacy_forge(self) -> tuple[Path, Path]:
        temporary = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup_directory, temporary)
        root = temporary / "forge"
        root.mkdir()
        legacy = sample_document()
        (root / "state.json").write_text(json.dumps(legacy, indent=2, sort_keys=True) + "\n")
        return root, temporary

    @staticmethod
    def _cleanup(directory: Path) -> None:
        import shutil

        shutil.rmtree(directory, ignore_errors=True)

    def test_migration_imports_document_and_renames_legacy_file(self) -> None:
        from service import migrate_storage

        root, _ = self._legacy_forge()
        migrate_storage(root)
        self.assertFalse((root / "state.json").exists())
        self.assertTrue((root / "state.json.imported").exists())
        self.assertTrue(backend_record_path(root).exists())
        migrated = read_document(root)
        self.assertEqual("main", migrated["canonical_branch"])
        self.assertEqual(7, migrated["next_transaction_sequence"])
        self.assertIn("alice", migrated["views"])

    def test_corrupt_input_leaves_no_artifacts(self) -> None:
        from service import migrate_storage

        root, _ = self._legacy_forge()
        (root / "state.json").write_text("{not json")
        with self.assertRaises(ForgeError):
            migrate_storage(root)
        self.assertFalse(backend_record_path(root).exists())
        self.assertFalse((root / "store.sqlite3").exists())
        self.assertTrue((root / "state.json").exists())

    def test_existing_backend_record_refuses_remigration(self) -> None:
        from service import migrate_storage

        root, _ = self._legacy_forge()
        initialize_store(root).close()
        with self.assertRaises(ForgeError):
            migrate_storage(root)


class RetryScheduleTest(unittest.TestCase):
    def test_backoff_grows_and_caps(self) -> None:
        self.assertEqual(60, retry_delay(1))
        self.assertEqual(120, retry_delay(2))
        self.assertEqual(240, retry_delay(3))
        self.assertEqual(480, retry_delay(4))
        self.assertEqual(600, retry_delay(5))
        self.assertEqual(600, retry_delay(50))

    def test_fail_job_moves_to_dead_after_max_attempts(self) -> None:
        job = {"attempts": MAX_JOB_ATTEMPTS, "status": "running", "lease_token": "t"}
        now = 1_000.0
        dead = fail_job(job, "persistent failure", now)
        self.assertTrue(dead)
        self.assertEqual("dead", job["status"])
        self.assertIsNone(job["next_attempt_at"])
        self.assertIsNone(job["lease_token"])
        self.assertNotIn("lease_expires_at", job)

    def test_fail_job_schedules_retry_before_cap(self) -> None:
        job = {"attempts": 1, "status": "running", "lease_token": "t"}
        dead = fail_job(job, "temporary failure", 1_000.0)
        self.assertFalse(dead)
        self.assertEqual("retry", job["status"])
        self.assertEqual(1_000.0 + 60, job["next_attempt_at"])


def _cleanup_directory(directory: Path) -> None:
    import shutil

    shutil.rmtree(directory, ignore_errors=True)


class JobLifecycleTest(unittest.TestCase):
    def _forge_with_dead_job_state(self) -> tuple[Path, dict]:
        temporary = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup_directory, temporary)
        root = temporary / "forge"
        root.mkdir()
        initialize_store(root).close()
        import subprocess

        (root / "views").mkdir()
        subprocess.run(
            ["git", "init", "--bare", "-q", str(root / "views" / "ghost.git")],
            check=True,
        )
        state = sample_document()
        state["transactions"]["t1"] = {
            "id": "t1",
            "status": "fanout_pending",
            "pending_views": ["ghost"],
            "view_errors": {},
        }
        state["projection_jobs"] = {
            "project:t1:ghost": {
                "id": "project:t1:ghost",
                "event_id": "e",
                "transaction_id": "t1",
                "view": "ghost",
                "descriptor_id": "vdesc-1",
                "descriptor_generation": 1,
                "sequence": 1,
                "canonical_to": "0" * 40,
                "status": "pending",
                "attempts": 0,
                "last_error": None,
                "next_attempt_at": 0,
                "max_attempts": MAX_JOB_ATTEMPTS,
            }
        }
        save_state(root, state)
        return root, state

    def _drive_to_dead(self, root: Path) -> dict:
        """Claim-and-fail until the ghost-view job dead-letters."""
        worker = __import__("worker")
        now = 1_700_000_000.0
        for _ in range(MAX_JOB_ATTEMPTS * 4):
            state = load_state(root)
            job = state["projection_jobs"]["project:t1:ghost"]
            if job["status"] == "dead":
                return job
            claimed = claim_projection_job(root, now=now)
            if claimed is None:
                pending_since = job.get("next_attempt_at") or 0
                now = max(now + 1, pending_since + 1)
                continue
            with patch.object(worker.time, "time", return_value=now):
                fail_projection_job(root, claimed, RuntimeError("boom"))
        self.fail("job never dead-lettered")

    def test_unknown_view_eventually_dead_letters(self) -> None:
        root, _ = self._forge_with_dead_job_state()
        self._drive_to_dead(root)
        state = load_state(root)
        job = state["projection_jobs"]["project:t1:ghost"]
        self.assertEqual(MAX_JOB_ATTEMPTS, job["attempts"])
        self.assertIn("ghost", state["transactions"]["t1"]["view_errors"])
        store = open_store(root)
        audit_kinds = [entry["kind"] for entry in store.recent_audit(5)]
        store.close()
        self.assertIn("job.dead_letter", audit_kinds)

    def test_cancel_and_requeue_round_trip_with_audit(self) -> None:
        root, _ = self._forge_with_dead_job_state()
        self._drive_to_dead(root)
        cancel_target = "project:t1:ghost"
        cancel_projection_job(root, cancel_target)
        state = load_state(root)
        self.assertEqual("cancelled", state["projection_jobs"][cancel_target]["status"])
        # Cancelling twice is an idempotent no-op.
        cancel_projection_job(root, cancel_target)
        requeue_projection_job(root, cancel_target)
        state = load_state(root)
        job = state["projection_jobs"][cancel_target]
        self.assertEqual("pending", job["status"])
        self.assertEqual(0, job["attempts"])
        self.assertNotIn("ghost", state["transactions"]["t1"].get("view_errors", {}))
        with self.assertRaises(ForgeError):
            requeue_projection_job(root, cancel_target)  # already pending
        store = open_store(root)
        kinds = [entry["kind"] for entry in store.recent_audit(10)]
        store.close()
        self.assertIn("job.cancelled", kinds)
        self.assertIn("job.requeued", kinds)

    def test_cancel_rejects_running_job_under_active_lease(self) -> None:
        temporary = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup_directory, temporary)
        root = temporary / "forge"
        root.mkdir()
        initialize_store(root).close()
        import subprocess

        (root / "views").mkdir()
        subprocess.run(
            ["git", "init", "--bare", "-q", str(root / "views" / "ghost.git")],
            check=True,
        )
        state = sample_document()
        state["transactions"]["t1"] = {
            "id": "t1",
            "status": "fanout_pending",
            "pending_views": [],
            "view_errors": {},
        }
        future = time.time() + 3_600
        state["projection_jobs"] = {
            "project:t1:alice": {
                "id": "project:t1:alice",
                "event_id": "e",
                "transaction_id": "t1",
                "view": "alice",
                "descriptor_id": "vdesc-1",
                "descriptor_generation": 1,
                "sequence": 1,
                "canonical_to": "0" * 40,
                "status": "running",
                "attempts": 1,
                "last_error": None,
                "next_attempt_at": 0,
                "lease_token": "live-token",
                "lease_expires_at": future,
            }
        }
        save_state(root, state)
        with self.assertRaises(ForgeError):
            cancel_projection_job(root, "project:t1:alice")
        state = load_state(root)
        self.assertEqual("running", state["projection_jobs"]["project:t1:alice"]["status"])


class BackupRestoreTest(unittest.TestCase):
    def test_backup_restore_reconstructs_rows_refs_and_jobs(self) -> None:
        from tests.tree_harness import TreeFixture, build_commit, command
        from tests.identity_harness import ADMIN_PRINCIPAL
        import os as os_module

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        client = fixture.clients["alice"]
        build_commit(
            Path(client),
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
        manifest = json.loads((destination / "manifest.json").read_text())
        self.assertIn("canonical", manifest["refs"])
        self.assertIn("view:alice", manifest["refs"])

        restore_forge(destination, restored_root)

        check_invariants(restored_root)
        original_doc = read_document(fixture.root)
        restored_doc = read_document(restored_root)
        self.assertEqual(original_doc["canonical_branch"], restored_doc["canonical_branch"])
        self.assertEqual(original_doc["views"], restored_doc["views"])
        self.assertEqual(original_doc["transactions"], restored_doc["transactions"])
        self.assertEqual(original_doc["outbox"], restored_doc["outbox"])
        self.assertEqual(original_doc["projection_jobs"], restored_doc["projection_jobs"])
        self.assertEqual(original_doc["changes"], restored_doc["changes"])

        # The restored clone serves identical history to clients.
        clone = base / "clone"
        command(
            "git",
            "clone",
            "-q",
            "--no-local",
            os_module.fsdecode(restored_root / "views" / "alice.git"),
            os_module.fsdecode(clone),
        )
        original_tip = command(
            "git", "-C", client, "rev-parse", "HEAD"
        ).stdout.strip()
        restored_tip = command("git", "-C", clone, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(original_tip, restored_tip)

    def test_restore_refuses_nonempty_target_and_bad_schema(self) -> None:
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

        manifest_path = destination / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["schema_version"] += 99
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(ForgeError):
            restore_forge(destination, base / "fresh")


if __name__ == "__main__":
    unittest.main()
