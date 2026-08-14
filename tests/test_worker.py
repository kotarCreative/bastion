from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import forge
from service import build_projection_plan
from state import save_state
from worker import (
    claim_projection_job,
    complete_projection_job,
    drain_projection_jobs,
    fail_projection_job,
)
from errors import ForgeError


def command(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ,
    )
    if result.returncode:
        raise AssertionError(f"{' '.join(args)}\n{result.stderr}")
    return result.stdout.strip()


class WorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        command("git", "init", "-q", "-b", "main", str(source))
        command("git", "config", "user.name", "Fixture", cwd=source)
        command("git", "config", "user.email", "fixture@example.test", cwd=source)
        (source / "shared").mkdir()
        (source / "shared" / "base.txt").write_text("base\n")
        command("git", "add", ".", cwd=source)
        command("git", "commit", "-q", "-m", "initial", cwd=source)
        self.forge = self.root / "forge"
        forge.init_forge(self.forge, source, "main")
        forge.add_view(self.forge, "alice", ["shared"], ["shared"], "main")
        forge.add_view(self.forge, "bob", ["shared"], ["shared"], "main")

        canonical = forge.canonical_repo(self.forge)
        admin = self.root / "admin"
        command("git", "clone", "-q", str(canonical), str(admin))
        command("git", "config", "user.name", "Admin", cwd=admin)
        command("git", "config", "user.email", "admin@example.test", cwd=admin)
        (admin / "shared" / "new.txt").write_text("new\n")
        command("git", "add", ".", cwd=admin)
        command("git", "commit", "-q", "-m", "new", cwd=admin)
        command("git", "push", "-q", "origin", "main", cwd=admin)

        state = self.state()
        canonical_tip = forge.ref(canonical, "refs/heads/main")
        alice = state["views"]["alice"]
        tx = {
            "id": "worker-tx",
            "view": "alice",
            "principal": "alice",
            "sequence": 1,
            "canonical_ref": alice["canonical_ref"],
            "projected_ref": alice["projected_ref"],
            "canonical_frontier_old": alice["canonical_frontier"],
            "canonical_old": canonical_tip,
            "canonical_new": canonical_tip,
            "projected_old": alice["projected_tip"],
            "projected_new": alice["projected_tip"],
            "mappings": [],
            "pending_views": ["bob"],
            "status": "fanout_pending",
        }
        state["transactions"][tx["id"]] = tx
        state["outbox"] = {
            "event": {
                "id": "event",
                "type": "canonical.committed",
                "transaction_id": tx["id"],
                "sequence": 1,
                "canonical_to": canonical_tip,
                "target_views": ["bob"],
                "status": "pending",
            }
        }
        save_state(self.forge, state)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def state(self) -> dict:
        return json.loads((self.forge / "state.json").read_text())

    def test_worker_builds_outside_lock_and_completes(self) -> None:
        claimed = claim_projection_job(self.forge)
        self.assertEqual("running", self.state()["projection_jobs"][claimed["id"]]["status"])
        plan = build_projection_plan(
            self.forge,
            claimed["view"],
            claimed["view_state"],
            claimed["canonical_to"],
        )
        complete_projection_job(self.forge, claimed, plan)
        state = self.state()
        self.assertEqual("completed", state["projection_jobs"][claimed["id"]]["status"])
        self.assertIsNone(state["projection_jobs"][claimed["id"]]["lease_token"])
        self.assertNotIn("lease_expires_at", state["projection_jobs"][claimed["id"]])
        self.assertNotIn("plan", state["projection_jobs"][claimed["id"]])
        self.assertEqual("published", state["transactions"]["worker-tx"]["status"])
        self.assertEqual(
            claimed["canonical_to"], state["views"]["bob"]["canonical_frontier"]
        )

    def test_already_moved_ref_is_adopted_after_crash(self) -> None:
        claimed = claim_projection_job(self.forge)
        plan = build_projection_plan(
            self.forge,
            claimed["view"],
            claimed["view_state"],
            claimed["canonical_to"],
        )
        remote = forge.view_repo(self.forge, "bob")
        forge.git(
            remote,
            "update-ref",
            "refs/heads/main",
            plan["new_tip"],
            plan["old_tip"],
        )
        complete_projection_job(self.forge, claimed, plan)
        self.assertEqual(plan["new_tip"], self.state()["views"]["bob"]["projected_tip"])

    def test_failure_retries_without_blocking_state_access(self) -> None:
        claimed = claim_projection_job(self.forge)
        fail_projection_job(self.forge, claimed, RuntimeError("temporary failure"))
        state = self.state()
        job = state["projection_jobs"][claimed["id"]]
        self.assertEqual("retry", job["status"])
        self.assertIn("temporary failure", job["last_error"])
        forge.sync_view(self.forge, "alice")

    def test_drain_dispatches_outbox_and_processes_job(self) -> None:
        self.assertEqual(1, drain_projection_jobs(self.forge))
        self.assertEqual("published", self.state()["transactions"]["worker-tx"]["status"])

    def test_viewless_administrative_transaction_does_not_break_claim(self) -> None:
        state = self.state()
        state["transactions"]["admin"] = {
            "id": "admin",
            "kind": "canonical_update",
            "sequence": 0,
            "canonical_ref": "refs/heads/main",
            "canonical_old": "1" * 40,
            "canonical_new": "2" * 40,
            "pending_views": [],
            "status": "published",
        }
        save_state(self.forge, state)
        self.assertIsNotNone(claim_projection_job(self.forge))

    def test_completion_accepts_its_token_after_lease_time(self) -> None:
        claimed = claim_projection_job(self.forge, now=1)
        plan = build_projection_plan(
            self.forge,
            claimed["view"],
            claimed["view_state"],
            claimed["canonical_to"],
        )
        complete_projection_job(self.forge, claimed, plan)
        self.assertEqual(
            "completed", self.state()["projection_jobs"][claimed["id"]]["status"]
        )

    def test_already_covered_completion_clears_active_lease_fields(self) -> None:
        from state import load_state, recover

        state = load_state(self.forge)
        recover(self.forge, state)
        job = next(iter(state["projection_jobs"].values()))
        view = state["views"][job["view"]]
        view["canonical_frontier"] = job["canonical_to"]
        job["lease_token"] = "retained-token"
        job["lease_expires_at"] = 123
        job["plan"] = {"retained": "plan"}
        save_state(self.forge, state)
        self.assertIsNone(claim_projection_job(self.forge))
        completed = self.state()["projection_jobs"][job["id"]]
        self.assertEqual("completed", completed["status"])
        self.assertIsNone(completed["lease_token"])
        self.assertNotIn("lease_expires_at", completed)
        self.assertNotIn("plan", completed)

    def test_canonical_rewrite_invalidates_plan(self) -> None:
        claimed = claim_projection_job(self.forge)
        plan = build_projection_plan(
            self.forge,
            claimed["view"],
            claimed["view_state"],
            claimed["canonical_to"],
        )
        canonical = forge.canonical_repo(self.forge)
        parent = forge.output(canonical, "rev-parse", "main^")
        current = forge.ref(canonical, "refs/heads/main")
        forge.git(canonical, "update-ref", "refs/heads/main", parent, current)
        with self.assertRaisesRegex(ForgeError, "canonical history changed"):
            complete_projection_job(self.forge, claimed, plan)

    def test_receive_started_after_claim_defers_completion(self) -> None:
        claimed = claim_projection_job(self.forge)
        plan = build_projection_plan(
            self.forge,
            claimed["view"],
            claimed["view_state"],
            claimed["canonical_to"],
        )
        state = self.state()
        view = state["views"]["bob"]
        state["transactions"]["active-bob"] = {
            "id": "active-bob",
            "view": "bob",
            "canonical_ref": view["canonical_ref"],
            "projected_ref": view["projected_ref"],
            "canonical_old": claimed["canonical_to"],
            "canonical_new": claimed["canonical_to"],
            "projected_old": view["projected_tip"],
            "projected_new": "f" * 40,
            "status": "canonical_committed",
            "mappings": [],
            "sequence": 2,
        }
        save_state(self.forge, state)
        complete_projection_job(self.forge, claimed, plan)
        job = self.state()["projection_jobs"][claimed["id"]]
        self.assertEqual("retry", job["status"])
        self.assertIn("receive transaction", job["last_error"])


if __name__ == "__main__":
    unittest.main()
