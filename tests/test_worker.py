"""Best-effort projection hints derived from committed WAL lag."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import forge
from errors import ForgeError
from state import load_state, save_state
from tests.identity_harness import add_view
from tests.tree_harness import build_commit, command
from worker import (
    LEASE_SECONDS,
    MAX_JOB_ATTEMPTS,
    claim_projection_job,
    complete_projection_job,
    drain_projection_jobs,
    fail_projection_job,
)


class WorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        source = base / "source"
        command("git", "init", "-q", "-b", "main", source)
        build_commit(
            source,
            None,
            {b"shared/base.txt": (b"100644", b"base\n")},
            b"initial\n",
            1_700_000_000,
        )
        self.root = base / "forge"
        forge.init_forge(self.root, source, "main")
        add_view(self.root, "alice", ["shared"], ["shared"])
        add_view(self.root, "bob", ["shared"], ["shared"])

    def test_completion_materializes_target_manifest_and_clears_lease(self) -> None:
        claimed = claim_projection_job(self.root, now=1)
        self.assertIsNotNone(claimed)

        complete_projection_job(self.root, claimed)

        state = load_state(self.root)
        job = state["projection_jobs"][claimed["id"]]
        self.assertEqual("completed", job["status"])
        self.assertIsNone(job["lease_token"])
        self.assertEqual(
            state["last_indexed_seq"], state["views"][claimed["view"]]["materialized_seq"]
        )

    def test_deleted_jobs_are_rediscovered_from_wal_lag(self) -> None:
        state = load_state(self.root)
        state["projection_jobs"] = {}
        state["views"] = {}
        save_state(self.root, state)

        claimed = claim_projection_job(self.root, now=1)

        self.assertIsNotNone(claimed)
        self.assertTrue(claimed["id"].startswith("project:"))

    def test_failure_records_retry_without_blocking_synchronous_fetch(self) -> None:
        claimed = claim_projection_job(self.root, now=10)
        fail_projection_job(self.root, claimed, RuntimeError("temporary"), now=10)
        job = load_state(self.root)["projection_jobs"][claimed["id"]]
        self.assertEqual("retry", job["status"])
        self.assertEqual(70, job["next_attempt_at"])

        forge.sync_view(self.root, claimed["view"])
        self.assertTrue(forge.view_repo(self.root, claimed["view"]).is_symlink())

    def test_expired_running_job_is_reclaimed(self) -> None:
        first = claim_projection_job(self.root, now=1)
        second = claim_projection_job(self.root, now=1 + LEASE_SECONDS + 1)
        self.assertIsNotNone(second)
        if second["id"] == first["id"]:
            self.assertNotEqual(first["lease_token"], second["lease_token"])

    def test_new_target_resets_retry_budget_and_stale_error(self) -> None:
        claimed = claim_projection_job(self.root, now=10)
        fail_projection_job(self.root, claimed, RuntimeError("old target failure"), now=10)
        state = load_state(self.root)
        old_target = state["projection_jobs"][claimed["id"]]["target_seq"]
        state["projection_jobs"][claimed["id"]]["attempts"] = MAX_JOB_ATTEMPTS - 1
        save_state(self.root, state)
        add_view(self.root, "carol", ["shared"], ["shared"])

        retried = claim_projection_job(self.root, now=11)

        self.assertEqual(claimed["id"], retried["id"])
        job = load_state(self.root)["projection_jobs"][claimed["id"]]
        self.assertGreater(job["target_seq"], old_target)
        self.assertEqual(1, job["attempts"])
        self.assertIsNone(job["last_error"])
        fail_projection_job(self.root, retried, RuntimeError("new target failure"), now=11)
        self.assertEqual(
            "retry", load_state(self.root)["projection_jobs"][claimed["id"]]["status"]
        )

    def test_new_target_does_not_revoke_an_active_lease(self) -> None:
        claimed = claim_projection_job(self.root, now=10)
        before = load_state(self.root)["projection_jobs"][claimed["id"]].copy()
        add_view(self.root, "carol", ["shared"], ["shared"])

        claim_projection_job(self.root, now=11)

        job = load_state(self.root)["projection_jobs"][claimed["id"]]
        self.assertEqual("running", job["status"])
        self.assertEqual(before["target_seq"], job["target_seq"])
        self.assertEqual(claimed["lease_token"], job["lease_token"])
        self.assertEqual(1, job["attempts"])

    def test_drain_materializes_every_active_view(self) -> None:
        self.assertGreaterEqual(drain_projection_jobs(self.root), 1)
        state = load_state(self.root)
        for view in ("alice", "bob"):
            self.assertEqual(
                state["last_indexed_seq"], state["views"][view]["materialized_seq"]
            )
        self.assertIsNone(claim_projection_job(self.root))


if __name__ == "__main__":
    unittest.main()
