"""Real-process concurrency schedules for the version-1 forge."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import worker
from process_harness import (
    Controller,
    Fixture,
    assert_equivalent,
    authoritative_snapshot,
    skip_unless_linux,
    stable_check,
)


@skip_unless_linux
class ProcessConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()
        self.addCleanup(self.fixture.cleanup)
        self.controller = Controller(self.fixture, self)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _git_push(self, checkout: Path, checkpoint: str | None = None) -> object:
        return self.controller.run_actor(
            ["git", "push", "-q", "origin", "main"],
            checkpoint=checkpoint,
            cwd=checkout,
        )

    def _worker(self, now: int, checkpoint: str | None = None) -> object:
        return self.controller.run_actor(
            [
                sys.executable,
                str(self.controller.ACTOR_PY),
                "worker",
                str(self.fixture.forge),
                str(now),
            ],
            checkpoint=checkpoint,
        )

    def _sync(self, name: str) -> object:
        return self.controller.run_actor(
            [sys.executable, str(self.controller.FORGE_PY), "sync", str(self.fixture.forge), name]
        )

    # ------------------------------------------------------------------
    # P1: Same-view shared-visible competing pushes
    # ------------------------------------------------------------------

    def test_same_view_competing_pushes_have_one_winner(self) -> None:
        alice2 = self.fixture.extra_clone("alice", "second")
        change_a = "Iaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        change_b = "Ibbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

        old = self.fixture.state()["views"]["alice"]["projected_tip"]
        self.fixture.client_commit(
            "alice", "shared/a.txt", b"alice a\n", "alice first", 1_700_000_000, change_a
        )
        self.fixture.commit_in(
            alice2, "shared/b.txt", b"alice b\n", "alice second", 1_700_000_100, change_b
        )

        winner = self.controller.start_actor(
            ["git", "push", "-q", "origin", "main"],
            checkpoint="source.validation_complete",
            cwd=self.fixture.alice,
        )
        self.addCleanup(self.controller.cleanup, winner)
        self.controller.wait_marker(winner)

        loser = self.controller.start_actor(
            ["git", "push", "-q", "origin", "main"],
            cwd=alice2,
        )
        self.addCleanup(self.controller.cleanup, loser)

        self.controller.resume(winner)
        self.controller.finish(winner)
        self.controller.finish(loser)

        self.assertEqual(0, winner.returncode, self.controller.diagnostic(winner))
        self.assertNotEqual(0, loser.returncode)

        self.fixture.settle()
        stable_check(self.fixture.forge, self)

        state = self.fixture.state()
        winner_new = self.fixture.state()["views"]["alice"]["projected_tip"]
        self.assertNotEqual(old, winner_new)
        self.assertIn(change_a, state["changes"])
        self.assertNotIn(change_b, state["changes"])

    # ------------------------------------------------------------------
    # P2: Cross-view hidden-disjoint competing pushes
    # ------------------------------------------------------------------

    def test_cross_view_hidden_disjoint_pushes_are_both_accepted(self) -> None:
        self.fixture.client_commit(
            "alice", "team-a/a.txt", b"team a\n", "alice team", 1_700_000_000
        )
        self.fixture.client_commit(
            "bob", "team-b/a.txt", b"team b\n", "bob team", 1_700_000_100
        )

        alice_actor = self.controller.start_actor(
            ["git", "push", "-q", "origin", "main"],
            checkpoint="source.validation_complete",
            cwd=self.fixture.alice,
        )
        self.addCleanup(self.controller.cleanup, alice_actor)
        self.controller.wait_marker(alice_actor)

        bob_actor = self.controller.start_actor(
            ["git", "push", "-q", "origin", "main"],
            cwd=self.fixture.bob,
        )
        self.addCleanup(self.controller.cleanup, bob_actor)

        self.controller.resume(alice_actor)
        self.controller.finish(alice_actor)
        self.controller.finish(bob_actor)

        self.assertEqual(0, alice_actor.returncode, self.controller.diagnostic(alice_actor))

        if bob_actor.returncode != 0:
            retry = self.controller.run_actor(
                ["git", "push", "-q", "origin", "main"],
                cwd=self.fixture.bob,
            )
            self.assertEqual(0, retry.returncode, self.controller.diagnostic(retry))

        self.fixture.settle()
        stable_check(self.fixture.forge, self)

        canonical_tip = self.fixture.state()["views"]["alice"]["canonical_frontier"]
        self.assertEqual(
            canonical_tip,
            self.fixture.state()["views"]["bob"]["canonical_frontier"],
        )

    # ------------------------------------------------------------------
    # P3: Canonical publisher dies while a competitor waits
    # ------------------------------------------------------------------

    def test_killed_canonical_publisher_blocks_competitor(self) -> None:
        change_a = "Icccccccccccccccccccccccccccccccccccccccc"
        self.fixture.client_commit(
            "alice",
            "shared/alice.txt",
            b"alice shared\n",
            "alice shared",
            1_700_000_000,
            change_a,
        )
        old_alice = self.fixture.state()["views"]["alice"]["projected_tip"]

        alice_actor = self.controller.start_actor(
            ["git", "push", "-q", "origin", "main"],
            checkpoint="source.canonical_ref_published",
            cwd=self.fixture.alice,
        )
        self.addCleanup(self.controller.cleanup, alice_actor)
        self.controller.wait_marker(alice_actor)

        self.fixture.client_commit(
            "bob",
            "shared/bob.txt",
            b"bob shared\n",
            "bob shared",
            1_700_000_100,
        )
        bob_actor = self.controller.start_actor(
            ["git", "push", "-q", "origin", "main"],
            cwd=self.fixture.bob,
        )
        self.addCleanup(self.controller.cleanup, bob_actor)

        self.controller.kill(alice_actor)
        self.controller.finish(alice_actor)
        self.controller.finish(bob_actor)

        self.assertNotEqual(0, alice_actor.returncode)
        self.assertNotEqual(0, bob_actor.returncode)

        # Recovery observes the canonical CAS and stale receiver marker.
        self.fixture.recover()

        # Alice's source ref is still old.
        self.assertEqual(
            old_alice,
            self.fixture.view_repo("alice").joinpath("refs", "heads", "main").read_text().strip(),
        )

        # Exact retry confirms the existing transaction.
        retry = self.controller.run_actor(
            ["git", "push", "-q", "origin", "main"],
            cwd=self.fixture.alice,
        )
        self.assertEqual(0, retry.returncode, self.controller.diagnostic(retry))

        # Bob must now fetch and rebase.
        self.fixture.settle()
        stable_check(self.fixture.forge, self)

    # ------------------------------------------------------------------
    # P4: Source publication barrier under real processes
    # ------------------------------------------------------------------

    def test_unconfirmed_source_barrier_blocks_probes(self) -> None:
        change_a = "Idddddddddddddddddddddddddddddddddddddddd"
        self.fixture.client_commit(
            "alice",
            "shared/barrier.txt",
            b"barrier\n",
            "alice barrier",
            1_700_000_000,
            change_a,
        )

        alice_actor = self.controller.start_actor(
            ["git", "push", "-q", "origin", "main"],
            checkpoint="source.canonical_state_persisted",
            cwd=self.fixture.alice,
        )
        self.addCleanup(self.controller.cleanup, alice_actor)
        self.controller.wait_marker(alice_actor)

        self.controller.kill(alice_actor)
        self.controller.finish(alice_actor)

        # Bob probe: must be current to hit the forge barrier rather than Git's
        # local stale check.
        self.fixture.client_commit(
            "bob", "shared/probe.txt", b"probe\n", "bob probe", 1_700_000_100
        )
        bob_push = self.controller.run_actor(
            ["git", "push", "-q", "origin", "main"],
            cwd=self.fixture.bob,
        )
        worker_probe = self._worker(1_700_000_200)
        sync_probe = self._sync("bob")

        self.assertNotEqual(0, bob_push.returncode)
        self.assertEqual(0, worker_probe.returncode)
        self.assertNotEqual(0, sync_probe.returncode)

        # Exact Alice retry releases the barrier.
        retry = self.controller.run_actor(
            ["git", "push", "-q", "origin", "main"],
            cwd=self.fixture.alice,
        )
        self.assertEqual(0, retry.returncode, self.controller.diagnostic(retry))

        self.fixture.settle()
        stable_check(self.fixture.forge, self)

    # ------------------------------------------------------------------
    # P5: Source ref moved before confirmation
    # ------------------------------------------------------------------

    def test_source_ref_moved_before_confirmation_is_recovered(self) -> None:
        self.fixture.client_commit(
            "alice",
            "shared/confirmed.txt",
            b"confirmed\n",
            "alice confirmed",
            1_700_000_000,
        )

        alice_actor = self.controller.start_actor(
            ["git", "push", "-q", "origin", "main"],
            checkpoint="source.ref_published",
            cwd=self.fixture.alice,
        )
        self.addCleanup(self.controller.cleanup, alice_actor)
        self.controller.wait_marker(alice_actor)

        submitted = self.fixture.state()["views"]["alice"]["projected_tip"]

        self.controller.kill(alice_actor)
        self.controller.finish(alice_actor)
        # Post-receive hook failure does not make git push fail; the source ref
        # has already been moved by receive-pack before the hook ran.

        # The source ref already moved, but persisted state is still
        # pre-confirmation.
        actual_ref = self.fixture.view_repo("alice").joinpath(
            "refs", "heads", "main"
        ).read_text().strip()
        self.assertNotEqual(submitted, actual_ref)

        self.fixture.recover()
        self.fixture.settle()
        stable_check(self.fixture.forge, self)

        # Idempotent second recovery.
        self.fixture.recover()
        stable_check(self.fixture.forge, self)

    # ------------------------------------------------------------------
    # P6: Two workers, one job
    # ------------------------------------------------------------------

    def test_two_workers_cannot_claim_same_leased_job(self) -> None:
        self.fixture.admin_update(
            "shared/job.txt", b"job\n", "admin visible", 1_700_000_000
        )
        # Complete Alice's job, leaving Bob's job pending.
        self.assertTrue(self.fixture.run_worker(1_700_000_000))

        worker_a = self.controller.start_actor(
            [
                sys.executable,
                str(self.controller.ACTOR_PY),
                "worker",
                str(self.fixture.forge),
                str(1_700_000_000),
            ],
            checkpoint="worker.claim_persisted",
        )
        self.addCleanup(self.controller.cleanup, worker_a)
        self.controller.wait_marker(worker_a)

        worker_b = self.controller.run_actor(
            [
                sys.executable,
                str(self.controller.ACTOR_PY),
                "worker",
                str(self.fixture.forge),
                str(1_700_000_000),
            ]
        )
        self.assertEqual(0, worker_b.returncode)

        self.controller.resume(worker_a)
        self.controller.finish(worker_a)
        self.assertEqual(0, worker_a.returncode, self.controller.diagnostic(worker_a))

        self.fixture.recover()
        stable_check(self.fixture.forge, self)

        job = next(
            job
            for job in self.fixture.state()["projection_jobs"].values()
            if job["view"] == "bob"
        )
        self.assertEqual("completed", job["status"])
        self.assertEqual(1, job["attempts"])

    # ------------------------------------------------------------------
    # P7: Two workers, independent views
    # ------------------------------------------------------------------

    def test_two_workers_progress_independent_views(self) -> None:
        self.fixture.admin_update(
            "shared/indep.txt", b"indep\n", "admin indep", 1_700_000_000
        )

        worker_a = self.controller.start_actor(
            [
                sys.executable,
                str(self.controller.ACTOR_PY),
                "worker",
                str(self.fixture.forge),
                str(1_700_000_000),
            ],
            checkpoint="worker.claim_persisted",
        )
        self.addCleanup(self.controller.cleanup, worker_a)
        self.controller.wait_marker(worker_a)

        # Worker A has claimed the first sorted view job (alice).  Worker B
        # should be able to claim and complete bob's job while A is stopped.
        worker_b = self.controller.run_actor(
            [
                sys.executable,
                str(self.controller.ACTOR_PY),
                "worker",
                str(self.fixture.forge),
                str(1_700_000_000),
            ]
        )
        self.assertEqual(0, worker_b.returncode)

        self.controller.resume(worker_a)
        self.controller.finish(worker_a)
        self.assertEqual(0, worker_a.returncode, self.controller.diagnostic(worker_a))

        self.fixture.recover()
        stable_check(self.fixture.forge, self)

        for name in ("alice", "bob"):
            job = next(
                job
                for job in self.fixture.state()["projection_jobs"].values()
                if job["view"] == name
            )
            self.assertEqual("completed", job["status"])

    # ------------------------------------------------------------------
    # P8: Receive versus claimed projection
    # ------------------------------------------------------------------

    def test_receive_cannot_publish_through_unfinished_projection(self) -> None:
        self.fixture.admin_update(
            "team-b/bob-only.txt", b"bob only\n", "admin bob-only", 1_700_000_000
        )
        # Complete Alice's hidden-only job.
        self.assertTrue(self.fixture.run_worker(1_700_000_000))

        bob_worker = self.controller.start_actor(
            [
                sys.executable,
                str(self.controller.ACTOR_PY),
                "worker",
                str(self.fixture.forge),
                str(1_700_000_000),
            ],
            checkpoint="worker.claim_persisted",
        )
        self.addCleanup(self.controller.cleanup, bob_worker)
        self.controller.wait_marker(bob_worker)

        # Bob push must be rejected because Bob has unfinished projection work.
        self.fixture.client_commit(
            "bob", "shared/bob-receive.txt", b"bob\n", "bob receive", 1_700_000_100
        )
        bob_push = self.controller.run_actor(
            ["git", "push", "-q", "origin", "main"],
            cwd=self.fixture.bob,
        )
        self.assertNotEqual(0, bob_push.returncode)

        # Alice push with a change hidden from Bob may proceed.
        self.fixture.client_commit(
            "alice",
            "team-a/alice-hidden.txt",
            b"alice hidden\n",
            "alice hidden",
            1_700_000_200,
        )
        alice_push = self.controller.run_actor(
            ["git", "push", "-q", "origin", "main"],
            cwd=self.fixture.alice,
        )
        self.assertEqual(0, alice_push.returncode, self.controller.diagnostic(alice_push))

        self.controller.resume(bob_worker)
        self.controller.finish(bob_worker)
        self.assertEqual(0, bob_worker.returncode, self.controller.diagnostic(bob_worker))

        self.fixture.settle()
        stable_check(self.fixture.forge, self)

    # ------------------------------------------------------------------
    # P9: Projected ref publisher dies
    # ------------------------------------------------------------------

    def test_killed_projected_ref_publisher_is_adopted(self) -> None:
        self.fixture.admin_update(
            "shared/adopt.txt", b"adopt\n", "admin adopt", 1_700_000_000
        )
        # Complete Alice's job, leaving Bob's job pending.
        self.assertTrue(self.fixture.run_worker(1_700_000_000))

        worker_actor = self.controller.start_actor(
            [
                sys.executable,
                str(self.controller.ACTOR_PY),
                "worker",
                str(self.fixture.forge),
                str(1_700_000_000),
            ],
            checkpoint="projection.ref_published",
        )
        self.addCleanup(self.controller.cleanup, worker_actor)
        self.controller.wait_marker(worker_actor)

        plan_new_tip = self.fixture.view_repo("bob").joinpath(
            "refs", "heads", "main"
        ).read_text().strip()
        self.assertNotEqual("0" * 40, plan_new_tip)

        self.controller.kill(worker_actor)
        self.controller.finish(worker_actor)
        self.assertNotEqual(0, worker_actor.returncode)

        # State still describes the old tip; the durable plan is ready.
        state = self.fixture.state()
        bob_view = state["views"]["bob"]
        self.assertNotEqual(plan_new_tip, bob_view["projected_tip"])

        # Reclaim after deterministic lease expiry.
        self.fixture.recover()
        self.assertTrue(
            self.fixture.run_worker(1_700_000_000 + worker.LEASE_SECONDS + 1)
        )
        self.fixture.recover()
        stable_check(self.fixture.forge, self)

        self.assertEqual(
            plan_new_tip,
            self.fixture.state()["views"]["bob"]["projected_tip"],
        )


if __name__ == "__main__":
    unittest.main()
