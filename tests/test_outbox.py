from __future__ import annotations

import unittest

from outbox import (
    dispatch_outbox,
    enqueue_canonical_committed,
    next_job,
    requeue_expired_jobs,
    update_transaction_status,
)


def transaction(identifier: str, sequence: int, views: list[str]) -> dict:
    return {
        "id": identifier,
        "sequence": sequence,
        "canonical_new": f"canonical-{sequence}",
        "target_descriptors": {
            view: {"descriptor_id": f"vdesc-{view}", "descriptor_generation": 1}
            for view in views
        },
        "pending_views": views.copy(),
        "status": "fanout_pending",
    }


class OutboxTest(unittest.TestCase):
    def test_enqueue_and_dispatch_are_idempotent(self) -> None:
        tx = transaction("tx-1", 1, ["alice", "bob"])
        state = {"transactions": {"tx-1": tx}, "outbox": {}, "projection_jobs": {}}
        enqueue_canonical_committed(state, tx, ready=True)
        enqueue_canonical_committed(state, tx, ready=True)
        dispatch_outbox(state)
        dispatch_outbox(state)
        self.assertEqual(1, len(state["outbox"]))
        self.assertEqual(2, len(state["projection_jobs"]))
        self.assertTrue(
            all(event["status"] == "dispatched" for event in state["outbox"].values())
        )

    def test_events_copy_target_assignments_into_jobs(self) -> None:
        tx = transaction("tx-1", 1, ["alice"])
        state = {"transactions": {"tx-1": tx}, "outbox": {}, "projection_jobs": {}}
        enqueue_canonical_committed(state, tx, ready=True)
        event = next(iter(state["outbox"].values()))
        self.assertEqual(
            {
                "alice": {"descriptor_id": "vdesc-alice", "descriptor_generation": 1}
            },
            event["target_descriptors"],
        )
        self.assertEqual(["alice"], event["target_views"])
        dispatch_outbox(state)
        job = state["projection_jobs"]["project:tx-1:alice"]
        self.assertEqual("vdesc-alice", job["descriptor_id"])
        self.assertEqual(1, job["descriptor_generation"])

    def test_jobs_are_ordered_independently_per_view(self) -> None:
        first = transaction("tx-1", 1, ["alice", "bob"])
        second = transaction("tx-2", 2, ["alice", "bob"])
        state = {
            "transactions": {"tx-1": first, "tx-2": second},
            "outbox": {},
            "projection_jobs": {},
        }
        enqueue_canonical_committed(state, first, ready=True)
        enqueue_canonical_committed(state, second, ready=True)
        dispatch_outbox(state)
        claimed = next_job(state, now=0)
        self.assertEqual(1, claimed["sequence"])
        self.assertEqual("alice", claimed["view"])
        claimed["status"] = "running"
        next_claimed = next_job(state, now=0)
        self.assertEqual(1, next_claimed["sequence"])
        self.assertEqual("bob", next_claimed["view"])
        next_claimed["status"] = "completed"
        later_bob = next_job(state, now=0)
        self.assertEqual(2, later_bob["sequence"])
        self.assertEqual("bob", later_bob["view"])

    def test_expired_running_job_is_requeued(self) -> None:
        state = {
            "projection_jobs": {
                "job": {
                    "status": "running",
                    "lease_expires_at": 10,
                    "lease_token": "lost-worker",
                }
            }
        }
        requeue_expired_jobs(state, now=11)
        self.assertEqual("retry", state["projection_jobs"]["job"]["status"])
        self.assertIsNone(state["projection_jobs"]["job"]["lease_token"])

    def test_transaction_completes_only_after_all_jobs(self) -> None:
        tx = transaction("tx", 1, ["alice", "bob"])
        state = {
            "transactions": {"tx": tx},
            "projection_jobs": {
                "alice": {"transaction_id": "tx", "status": "completed"},
                "bob": {"transaction_id": "tx", "status": "retry"},
            },
        }
        update_transaction_status(state, "tx")
        self.assertEqual("fanout_pending", tx["status"])
        state["projection_jobs"]["bob"]["status"] = "completed"
        update_transaction_status(state, "tx")
        self.assertEqual("published", tx["status"])

    def test_blocked_job_can_be_excluded_without_blocking_another_view(self) -> None:
        state = {
            "projection_jobs": {
                "bob": {
                    "id": "bob",
                    "view": "bob",
                    "sequence": 1,
                    "status": "pending",
                    "next_attempt_at": 0,
                },
                "alice": {
                    "id": "alice",
                    "view": "alice",
                    "sequence": 2,
                    "status": "pending",
                    "next_attempt_at": 0,
                },
            }
        }
        self.assertEqual("bob", next_job(state, now=0)["id"])
        self.assertEqual("alice", next_job(state, now=0, excluded={"bob"})["id"])


if __name__ == "__main__":
    unittest.main()
