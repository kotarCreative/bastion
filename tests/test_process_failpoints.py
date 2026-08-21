"""Real-process failpoint crash matrix for the version-1 forge."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import worker

from identity_harness import ADMIN_PRINCIPAL
from process_harness import (
    Controller,
    Fixture,
    assert_equivalent,
    authoritative_snapshot,
    skip_unless_linux,
    stable_check,
)
from state import receiver_active, recover, save_state, write_receiver_marker


@skip_unless_linux
class ProcessFailpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()
        self.addCleanup(self.fixture.cleanup)
        self.controller = Controller(self.fixture, self)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _push_actor(
        self, checkout: Path, checkpoint: str | None = None, action: str = "SIGKILL"
    ) -> object:
        return self.controller.run_actor(
            ["git", "push", "-q", "origin", "main"],
            checkpoint=checkpoint,
            cwd=checkout,
            action=action,
        )

    def _worker_actor(self, now: int, checkpoint: str | None = None) -> object:
        return self.controller.run_actor(
            [
                sys.executable,
                str(self.controller.ACTOR_PY),
                "worker",
                str(self.fixture.forge),
                str(now),
            ],
            checkpoint=checkpoint,
            action="SIGKILL",
        )

    def _recover_actor(self, checkpoint: str | None = None) -> object:
        return self.controller.run_actor(
            [
                sys.executable,
                str(self.controller.ACTOR_PY),
                "recover",
                str(self.fixture.forge),
            ],
            checkpoint=checkpoint,
            action="SIGKILL",
        )

    def _admin_actor(self, old: str, new: str, checkpoint: str | None = None) -> object:
        return self.controller.run_actor(
            [
                sys.executable,
                str(self.controller.FORGE_PY),
                "update-canonical",
                str(self.fixture.forge),
                old,
                new,
                "--principal",
                identity_harness.ADMIN_PRINCIPAL,
            ],
            checkpoint=checkpoint,
            action="SIGKILL",
        )

    def _state_save_actor(self, value: int, checkpoint: str) -> object:
        return self.controller.run_actor(
            [
                sys.executable,
                str(self.controller.ACTOR_PY),
                "state-save",
                str(self.fixture.forge),
                str(value),
            ],
            checkpoint=checkpoint,
            action="SIGKILL",
        )

    def _source_control(
        self,
        name: str,
        path: str,
        contents: bytes,
        message: str,
        timestamp: int,
        change_id: str | None = None,
    ) -> dict:
        self.fixture.client_commit(name, path, contents, message, timestamp, change_id)
        checkout = self.fixture.alice if name == "alice" else self.fixture.bob
        actor = self.controller.run_actor(
            ["git", "push", "-q", "origin", "main"], cwd=checkout
        )
        self.assertEqual(0, actor.returncode, self.controller.diagnostic(actor))
        self.fixture.settle()
        return authoritative_snapshot(self.fixture.forge)

    def _admin_control(
        self, path: str, contents: bytes, message: str, timestamp: int
    ) -> tuple[str, str, dict]:
        old, new = self.fixture.admin_update(path, contents, message, timestamp)
        self.fixture.settle()
        return old, new, authoritative_snapshot(self.fixture.forge)

    def _worker_control(
        self, path: str, contents: bytes, message: str, timestamp: int
    ) -> dict:
        self.fixture.admin_update(path, contents, message, timestamp)
        self.fixture.settle()
        return authoritative_snapshot(self.fixture.forge)

    def _retry_source_push(self, name: str) -> None:
        checkout = self.fixture.alice if name == "alice" else self.fixture.bob
        actor = self.controller.run_actor(
            ["git", "push", "-q", "origin", "main"], cwd=checkout
        )
        self.assertEqual(0, actor.returncode, self.controller.diagnostic(actor))

    # ------------------------------------------------------------------
    # Source crash matrix
    # ------------------------------------------------------------------

    def test_source_validation_complete(self) -> None:
        control = self._source_control(
            "alice", "shared/a.txt", b"a\n", "source a", 1_900_000_000, "Isrcval"
        )
        fresh = Fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        fresh.client_commit(
            "alice", "shared/a.txt", b"a\n", "source a", 1_900_000_000, "Isrcval"
        )
        actor = ctrl.run_actor(
            ["git", "push", "-q", "origin", "main"],
            checkpoint="source.validation_complete",
            cwd=fresh.alice,
            action="SIGKILL",
        )
        self.assertNotEqual(0, actor.returncode)
        fresh.recover()
        self.assertEqual({}, fresh.state()["transactions"])
        self._retry_source_push_for(fresh, ctrl, "alice")
        fresh.settle()
        assert_equivalent(authoritative_snapshot(fresh.forge), control, self)

    def test_source_prepared_persisted(self) -> None:
        control = self._source_control(
            "alice", "shared/b.txt", b"b\n", "source b", 1_900_000_000, "Isrcprep"
        )
        fresh = Fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        fresh.client_commit(
            "alice", "shared/b.txt", b"b\n", "source b", 1_900_000_000, "Isrcprep"
        )
        actor = ctrl.run_actor(
            ["git", "push", "-q", "origin", "main"],
            checkpoint="source.prepared_persisted",
            cwd=fresh.alice,
            action="SIGKILL",
        )
        self.assertNotEqual(0, actor.returncode)
        fresh.recover()
        self.assertEqual("aborted", list(fresh.state()["transactions"].values())[0]["status"])
        self._retry_source_push_for(fresh, ctrl, "alice")
        fresh.settle()
        assert_equivalent(authoritative_snapshot(fresh.forge), control, self)

    def test_source_canonical_ref_published(self) -> None:
        control = self._source_control(
            "alice", "shared/c.txt", b"c\n", "source c", 1_900_000_000, "Isrccas"
        )
        fresh = Fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        fresh.client_commit(
            "alice", "shared/c.txt", b"c\n", "source c", 1_900_000_000, "Isrccas"
        )
        actor = ctrl.run_actor(
            ["git", "push", "-q", "origin", "main"],
            checkpoint="source.canonical_ref_published",
            cwd=fresh.alice,
            action="SIGKILL",
        )
        self.assertNotEqual(0, actor.returncode)
        fresh.recover()
        tx = list(fresh.state()["transactions"].values())[0]
        self.assertEqual("source_unconfirmed", tx["status"])
        self._retry_source_push_for(fresh, ctrl, "alice")
        fresh.settle()
        assert_equivalent(authoritative_snapshot(fresh.forge), control, self)

    def test_source_canonical_state_persisted(self) -> None:
        control = self._source_control(
            "alice", "shared/d.txt", b"d\n", "source d", 1_900_000_000, "Isrcstate"
        )
        fresh = Fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        fresh.client_commit(
            "alice", "shared/d.txt", b"d\n", "source d", 1_900_000_000, "Isrcstate"
        )
        actor = ctrl.run_actor(
            ["git", "push", "-q", "origin", "main"],
            checkpoint="source.canonical_state_persisted",
            cwd=fresh.alice,
            action="SIGKILL",
        )
        self.assertNotEqual(0, actor.returncode)
        fresh.recover()
        tx = list(fresh.state()["transactions"].values())[0]
        self.assertEqual("source_unconfirmed", tx["status"])
        self._retry_source_push_for(fresh, ctrl, "alice")
        fresh.settle()
        assert_equivalent(authoritative_snapshot(fresh.forge), control, self)

    def test_source_ref_published(self) -> None:
        control = self._source_control(
            "alice", "shared/e.txt", b"e\n", "source e", 1_900_000_000, "Isrcref"
        )
        fresh = Fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        fresh.client_commit(
            "alice", "shared/e.txt", b"e\n", "source e", 1_900_000_000, "Isrcref"
        )
        actor = ctrl.run_actor(
            ["git", "push", "-q", "origin", "main"],
            checkpoint="source.ref_published",
            cwd=fresh.alice,
            action="SIGKILL",
        )
        # Post-receive hook failure does not affect git push result.
        fresh.recover()
        fresh.settle()
        assert_equivalent(authoritative_snapshot(fresh.forge), control, self)

    def test_source_confirmation_persisted(self) -> None:
        control = self._source_control(
            "alice", "shared/f.txt", b"f\n", "source f", 1_900_000_000, "Isrcconfirm"
        )
        fresh = Fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        fresh.client_commit(
            "alice", "shared/f.txt", b"f\n", "source f", 1_900_000_000, "Isrcconfirm"
        )
        actor = ctrl.run_actor(
            ["git", "push", "-q", "origin", "main"],
            checkpoint="source.confirmation_persisted",
            cwd=fresh.alice,
            action="SIGKILL",
        )
        fresh.recover()
        fresh.settle()
        fresh.recover()
        assert_equivalent(authoritative_snapshot(fresh.forge), control, self)

    def test_source_receiver_cleanup_persisted(self) -> None:
        control = self._source_control(
            "alice", "shared/g.txt", b"g\n", "source g", 1_900_000_000, "Isrccleanup"
        )
        fresh = Fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        fresh.client_commit(
            "alice", "shared/g.txt", b"g\n", "source g", 1_900_000_000, "Isrccleanup"
        )
        actor = ctrl.run_actor(
            ["git", "push", "-q", "origin", "main"],
            checkpoint="source.receiver_cleanup_persisted",
            cwd=fresh.alice,
            action="SIGKILL",
        )
        fresh.recover()
        fresh.settle()
        self.assertEqual([], list((fresh.forge / "receivers").glob("*")))
        assert_equivalent(authoritative_snapshot(fresh.forge), control, self)

    def _retry_source_push_for(
        self, fixture: Fixture, ctrl: Controller, name: str
    ) -> None:
        checkout = fixture.alice if name == "alice" else fixture.bob
        actor = ctrl.run_actor(
            ["git", "push", "-q", "origin", "main"], cwd=checkout
        )
        self.assertEqual(0, actor.returncode, ctrl.diagnostic(actor))

    # ------------------------------------------------------------------
    # Administrative crash matrix
    # ------------------------------------------------------------------

    def test_admin_prepared_persisted(self) -> None:
        old, new, control = self._admin_control(
            "shared/admin.txt", b"admin\n", "admin update", 1_900_000_000
        )
        fresh = Fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        fresh_canonical_old, _ = fresh.stage_admin_commit(
            "shared/admin.txt", b"admin\n", "admin update", 1_900_000_000
        )
        actor = ctrl.run_actor(
            [
                sys.executable,
                str(ctrl.FORGE_PY),
                "update-canonical",
                str(fresh.forge),
                fresh_canonical_old,
                new,
                "--principal",
                ADMIN_PRINCIPAL,
            ],
            checkpoint="admin.prepared_persisted",
            action="SIGKILL",
        )
        self.assertNotEqual(0, actor.returncode)
        fresh.recover()
        self.assertTrue(
            all(tx["status"] == "aborted" for tx in fresh.state()["transactions"].values())
        )
        # Retry the exact same administrative update.
        retry = ctrl.run_actor(
            [
                sys.executable,
                str(ctrl.FORGE_PY),
                "update-canonical",
                str(fresh.forge),
                fresh_canonical_old,
                new,
                "--principal",
                ADMIN_PRINCIPAL,
            ]
        )
        self.assertEqual(0, retry.returncode, ctrl.diagnostic(retry))
        fresh.settle()
        assert_equivalent(authoritative_snapshot(fresh.forge), control, self)

    def test_admin_canonical_ref_published(self) -> None:
        old, new, control = self._admin_control(
            "shared/admin2.txt", b"admin2\n", "admin update 2", 1_900_000_000
        )
        fresh = Fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        fresh_canonical_old, _ = fresh.stage_admin_commit(
            "shared/admin2.txt", b"admin2\n", "admin update 2", 1_900_000_000
        )
        actor = ctrl.run_actor(
            [
                sys.executable,
                str(ctrl.FORGE_PY),
                "update-canonical",
                str(fresh.forge),
                fresh_canonical_old,
                new,
                "--principal",
                ADMIN_PRINCIPAL,
            ],
            checkpoint="admin.canonical_ref_published",
            action="SIGKILL",
        )
        self.assertNotEqual(0, actor.returncode)
        fresh.recover()
        fresh.settle()
        fresh.recover()
        assert_equivalent(authoritative_snapshot(fresh.forge), control, self)

    def test_admin_canonical_state_persisted(self) -> None:
        old, new, control = self._admin_control(
            "shared/admin3.txt", b"admin3\n", "admin update 3", 1_900_000_000
        )
        fresh = Fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        fresh_canonical_old, _ = fresh.stage_admin_commit(
            "shared/admin3.txt", b"admin3\n", "admin update 3", 1_900_000_000
        )
        actor = ctrl.run_actor(
            [
                sys.executable,
                str(ctrl.FORGE_PY),
                "update-canonical",
                str(fresh.forge),
                fresh_canonical_old,
                new,
                "--principal",
                ADMIN_PRINCIPAL,
            ],
            checkpoint="admin.canonical_state_persisted",
            action="SIGKILL",
        )
        self.assertNotEqual(0, actor.returncode)
        fresh.recover()
        fresh.settle()
        fresh.recover()
        assert_equivalent(authoritative_snapshot(fresh.forge), control, self)

    # ------------------------------------------------------------------
    # Worker crash matrix
    # ------------------------------------------------------------------

    def _worker_crash_fixture(self) -> Fixture:
        fresh = Fixture()
        fresh.admin_update(
            "shared/worker.txt", b"worker\n", "worker update", 1_900_000_000
        )
        return fresh

    def test_worker_claim_persisted(self) -> None:
        control = self._worker_control(
            "shared/worker.txt", b"worker\n", "worker update", 1_900_000_000
        )
        fresh = self._worker_crash_fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        actor = ctrl.run_actor(
            [
                sys.executable,
                str(ctrl.ACTOR_PY),
                "worker",
                str(fresh.forge),
                str(1_900_000_000),
            ],
            checkpoint="worker.claim_persisted",
            action="SIGKILL",
        )
        self.assertNotEqual(0, actor.returncode)
        fresh.recover()
        now = 1_900_000_000 + worker.LEASE_SECONDS + 1
        while fresh.run_worker(now):
            now += 1
        fresh.recover()
        assert_equivalent(authoritative_snapshot(fresh.forge), control, self)

    def test_worker_plan_persisted(self) -> None:
        control = self._worker_control(
            "shared/worker.txt", b"worker\n", "worker update", 1_900_000_000
        )
        fresh = self._worker_crash_fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        actor = ctrl.run_actor(
            [
                sys.executable,
                str(ctrl.ACTOR_PY),
                "worker",
                str(fresh.forge),
                str(1_900_000_000),
            ],
            checkpoint="worker.plan_persisted",
            action="SIGKILL",
        )
        self.assertNotEqual(0, actor.returncode)
        fresh.recover()
        now = 1_900_000_000 + worker.LEASE_SECONDS + 1
        while fresh.run_worker(now):
            now += 1
        fresh.recover()
        assert_equivalent(authoritative_snapshot(fresh.forge), control, self)

    def test_projection_ref_published(self) -> None:
        control = self._worker_control(
            "shared/worker.txt", b"worker\n", "worker update", 1_900_000_000
        )
        fresh = self._worker_crash_fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        actor = ctrl.run_actor(
            [
                sys.executable,
                str(ctrl.ACTOR_PY),
                "worker",
                str(fresh.forge),
                str(1_900_000_000),
            ],
            checkpoint="projection.ref_published",
            action="SIGKILL",
        )
        self.assertNotEqual(0, actor.returncode)
        fresh.recover()
        now = 1_900_000_000 + worker.LEASE_SECONDS + 1
        while fresh.run_worker(now):
            now += 1
        fresh.recover()
        assert_equivalent(authoritative_snapshot(fresh.forge), control, self)

    def test_worker_completion_persisted(self) -> None:
        control = self._worker_control(
            "shared/worker.txt", b"worker\n", "worker update", 1_900_000_000
        )
        fresh = self._worker_crash_fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        actor = ctrl.run_actor(
            [
                sys.executable,
                str(ctrl.ACTOR_PY),
                "worker",
                str(fresh.forge),
                str(1_900_000_000),
            ],
            checkpoint="worker.completion_persisted",
            action="SIGKILL",
        )
        self.assertNotEqual(0, actor.returncode)
        fresh.recover()
        fresh.settle()
        fresh.recover()
        assert_equivalent(authoritative_snapshot(fresh.forge), control, self)

    def test_worker_retry_persisted(self) -> None:
        control = self._lease_lost_control()
        fresh = Fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        fresh.admin_update(
            "shared/retry.txt", b"retry\n", "retry update", 1_900_000_000
        )
        # Worker A claims the first sorted job and pauses after releasing the lock.
        worker_a = ctrl.start_actor(
            [
                sys.executable,
                str(ctrl.ACTOR_PY),
                "worker",
                str(fresh.forge),
                str(1_900_000_000),
            ],
            checkpoint="worker.claim_persisted",
        )
        self.addCleanup(ctrl.cleanup, worker_a)
        ctrl.wait_marker(worker_a)

        # Worker B reclaims the same job after A's lease expires and pauses.
        worker_b = ctrl.start_actor(
            [
                sys.executable,
                str(ctrl.ACTOR_PY),
                "worker",
                str(fresh.forge),
                str(1_900_000_000 + worker.LEASE_SECONDS + 1),
            ],
            checkpoint="worker.claim_persisted",
        )
        self.addCleanup(ctrl.cleanup, worker_b)
        ctrl.wait_marker(worker_b)
        state = fresh.state()
        job = next(
            job
            for job in state["projection_jobs"].values()
            if job["status"] == "running"
        )
        job_id = job["id"]
        token = job["lease_token"]

        # Kill B while it holds the reclaimed lease.
        ctrl.kill(worker_b)
        ctrl.finish(worker_b)
        self.assertNotEqual(0, worker_b.returncode)

        # A fail actor using B's token records a retry and is killed at the
        # worker.retry_persisted boundary.
        fail_actor = ctrl.run_actor(
            [
                sys.executable,
                str(ctrl.ACTOR_PY),
                "fail",
                str(fresh.forge),
                str(1_900_000_000 + worker.LEASE_SECONDS + 1),
                job_id,
                token,
            ],
            checkpoint="worker.retry_persisted",
            action="SIGKILL",
        )
        self.assertNotEqual(0, fail_actor.returncode)

        # Clean up the original paused worker.
        ctrl.kill(worker_a)
        ctrl.finish(worker_a)

        fresh.recover()
        # Settle remaining work deterministically after B's retry deadline.
        now = 1_900_000_000 + worker.LEASE_SECONDS + 1 + worker.RETRY_SECONDS + 1
        while fresh.run_worker(now):
            now += 1
        fresh.recover()
        assert_equivalent(authoritative_snapshot(fresh.forge), control, self)

    def _lease_lost_control(self) -> dict:
        fresh = Fixture()
        fresh.admin_update(
            "shared/retry.txt", b"retry\n", "retry update", 1_900_000_000
        )
        fresh.settle()
        snapshot = authoritative_snapshot(fresh.forge)
        fresh.cleanup()
        return snapshot

    # ------------------------------------------------------------------
    # Recovery crash matrix
    # ------------------------------------------------------------------

    def test_recovery_state_persisted(self) -> None:
        control = self._source_control(
            "alice", "shared/rec.txt", b"rec\n", "recovery", 1_900_000_000, "Isrcrec"
        )
        fresh = Fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        fresh.client_commit(
            "alice", "shared/rec.txt", b"rec\n", "recovery", 1_900_000_000, "Isrcrec"
        )
        actor = ctrl.run_actor(
            ["git", "push", "-q", "origin", "main"],
            checkpoint="source.canonical_ref_published",
            cwd=fresh.alice,
            action="SIGKILL",
        )
        self.assertNotEqual(0, actor.returncode)
        rec_actor = ctrl.run_actor(
            [
                sys.executable,
                str(ctrl.ACTOR_PY),
                "recover",
                str(fresh.forge),
            ],
            checkpoint="recovery.state_persisted",
            action="SIGKILL",
        )
        self.assertNotEqual(0, rec_actor.returncode)
        fresh.recover()
        self._retry_source_push_for(fresh, ctrl, "alice")
        fresh.settle()
        fresh.recover()
        assert_equivalent(authoritative_snapshot(fresh.forge), control, self)

    # ------------------------------------------------------------------
    # Atomic state-file matrix
    # ------------------------------------------------------------------

    def test_state_temporary_fsynced(self) -> None:
        fresh = Fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        old_sequence = fresh.state()["next_transaction_sequence"]
        actor = ctrl.run_actor(
            [
                sys.executable,
                str(ctrl.ACTOR_PY),
                "state-save",
                str(fresh.forge),
                "5",
            ],
            checkpoint="state.temporary_fsynced",
            action="SIGKILL",
        )
        self.assertNotEqual(0, actor.returncode)
        self.assertEqual(old_sequence, fresh.state()["next_transaction_sequence"])
        # A subsequent unarmed save must replace the stale temp file.
        ctrl.run_actor(
            [
                sys.executable,
                str(ctrl.ACTOR_PY),
                "state-save",
                str(fresh.forge),
                "5",
            ]
        )
        self.assertEqual(old_sequence + 5, fresh.state()["next_transaction_sequence"])

    def test_state_replaced(self) -> None:
        fresh = Fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        old_sequence = fresh.state()["next_transaction_sequence"]
        actor = ctrl.run_actor(
            [
                sys.executable,
                str(ctrl.ACTOR_PY),
                "state-save",
                str(fresh.forge),
                "5",
            ],
            checkpoint="state.replaced",
            action="SIGKILL",
        )
        self.assertNotEqual(0, actor.returncode)
        data = (fresh.forge / "state.json").read_text()
        self.assertTrue(data.startswith("{"))
        parsed = fresh.state()
        self.assertEqual(old_sequence + 5, parsed["next_transaction_sequence"])
        stable_check(fresh.forge, self)

    def test_state_directory_fsynced(self) -> None:
        fresh = Fixture()
        self.addCleanup(fresh.cleanup)
        ctrl = Controller(fresh, self)
        old_sequence = fresh.state()["next_transaction_sequence"]
        actor = ctrl.run_actor(
            [
                sys.executable,
                str(ctrl.ACTOR_PY),
                "state-save",
                str(fresh.forge),
                "5",
            ],
            checkpoint="state.directory_fsynced",
            action="SIGKILL",
        )
        self.assertNotEqual(0, actor.returncode)
        parsed = fresh.state()
        self.assertEqual(old_sequence + 5, parsed["next_transaction_sequence"])
        stable_check(fresh.forge, self)
        fresh.recover()
        self.assertEqual(old_sequence + 5, fresh.state()["next_transaction_sequence"])

    # ------------------------------------------------------------------
    # Receiver marker tests
    # ------------------------------------------------------------------

    def test_receiver_marker_live_process_keeps_transaction_active(self) -> None:
        marker = self.fixture.forge / "receivers" / "live"
        write_receiver_marker(self.fixture.forge, "live", os.getpid())
        self.assertTrue(receiver_active(self.fixture.forge, "live"))

    def test_receiver_marker_dead_process_is_stale(self) -> None:
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        write_receiver_marker(self.fixture.forge, "dead", pid)
        self.assertFalse(receiver_active(self.fixture.forge, "dead"))
        self.assertFalse((self.fixture.forge / "receivers" / "dead").exists())

    def test_receiver_marker_reused_pid_with_different_start_is_stale(self) -> None:
        # Use a fabricated marker for a non-existent PID with a mismatched start.
        marker = self.fixture.forge / "receivers" / "reused"
        marker.parent.mkdir(exist_ok=True)
        marker.write_text("999999 0")
        self.assertFalse(receiver_active(self.fixture.forge, "reused"))
        self.assertFalse(marker.exists())

    def test_receiver_marker_aborted_prepared_transaction_is_removed(self) -> None:
        self.fixture.admin_update(
            "shared/abort.txt", b"abort\n", "abort marker", 1_900_000_000
        )
        state = self.fixture.state()
        txid = list(state["transactions"].keys())[0]
        state["transactions"][txid]["status"] = "prepared"
        state["transactions"][txid]["canonical_new"] = "f" * 40
        write_receiver_marker(self.fixture.forge, txid, os.getpid())
        save_state(self.fixture.forge, state)
        recover(self.fixture.forge, state)
        save_state(self.fixture.forge, state)
        self.assertFalse(marker_exists(self.fixture.forge, txid))

    def test_receiver_marker_confirmed_source_is_removed_by_recovery(self) -> None:
        self.fixture.client_commit(
            "alice",
            "shared/confirm.txt",
            b"confirm\n",
            "confirm marker",
            1_900_000_000,
            "Iconfmark",
        )
        old = self.fixture.state()["views"]["alice"]["projected_tip"]
        actor = self.controller.run_actor(
            ["git", "push", "-q", "origin", "main"],
            cwd=self.fixture.alice,
        )
        self.assertEqual(0, actor.returncode)
        state = self.fixture.state()
        txid = next(
            tx["id"]
            for tx in state["transactions"].values()
            if tx.get("projected_old") == old
        )
        # Simulate a killed post-receive by recreating the receiver marker.
        write_receiver_marker(self.fixture.forge, txid, os.getpid())
        recover(self.fixture.forge, state)
        save_state(self.fixture.forge, state)
        self.assertFalse(marker_exists(self.fixture.forge, txid))


def marker_exists(root: Path, txid: str) -> bool:
    return (root / "receivers" / txid).exists()


if __name__ == "__main__":
    unittest.main()
