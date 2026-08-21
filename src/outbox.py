"""Pure durable outbox and projection-job state transitions."""

from __future__ import annotations

import copy
import time


def event_id(transaction_id: str) -> str:
    return f"canonical.committed:{transaction_id}"


def job_id(transaction_id: str, view: str) -> str:
    return f"project:{transaction_id}:{view}"


def enqueue_canonical_committed(
    state: dict, transaction: dict, ready: bool = False
) -> None:
    events = state.setdefault("outbox", {})
    identifier = event_id(transaction["id"])
    events.setdefault(
        identifier,
        {
            "id": identifier,
            "type": "canonical.committed",
            "transaction_id": transaction["id"],
            "sequence": transaction["sequence"],
            "canonical_to": transaction["canonical_new"],
            "target_views": sorted(transaction.get("target_descriptors", {})),
            "target_descriptors": copy.deepcopy(
                transaction.get("target_descriptors", {})
            ),
            "status": "pending" if ready else "blocked",
        },
    )
    if ready and events[identifier]["status"] == "blocked":
        events[identifier]["status"] = "pending"
    transaction["event_id"] = identifier


def dispatch_outbox(state: dict) -> None:
    jobs = state.setdefault("projection_jobs", {})
    for event in sorted(
        state.setdefault("outbox", {}).values(),
        key=lambda item: (item["sequence"], item["id"]),
    ):
        if event["status"] != "pending":
            continue
        assignments = event.get("target_descriptors", {})
        for view in event["target_views"]:
            assignment = assignments.get(view)
            if not assignment:
                raise KeyError(f"event target lacks a descriptor assignment: {view}")
            identifier = job_id(event["transaction_id"], view)
            jobs.setdefault(
                identifier,
                {
                    "id": identifier,
                    "event_id": event["id"],
                    "transaction_id": event["transaction_id"],
                    "view": view,
                    "descriptor_id": assignment["descriptor_id"],
                    "descriptor_generation": assignment["descriptor_generation"],
                    "sequence": event["sequence"],
                    "canonical_to": event["canonical_to"],
                    "status": "pending",
                    "attempts": 0,
                    "last_error": None,
                    "next_attempt_at": 0,
                },
            )
        event["status"] = "dispatched"


def requeue_expired_jobs(state: dict, now: float | None = None) -> None:
    now = time.time() if now is None else now
    for job in state.setdefault("projection_jobs", {}).values():
        if job["status"] in {"running", "ready"} and job.get(
            "lease_expires_at", 0
        ) <= now:
            job["status"] = "retry"
            job["lease_token"] = None
            job["next_attempt_at"] = now


def next_job(
    state: dict,
    now: float | None = None,
    excluded: set[str] | None = None,
) -> dict | None:
    now = time.time() if now is None else now
    jobs = state.setdefault("projection_jobs", {})
    candidates = sorted(
        (
            job
            for job in jobs.values()
            if job["status"] in {"pending", "retry"}
            and job.get("next_attempt_at", 0) <= now
            and job["id"] not in (excluded or set())
        ),
        key=lambda item: (item["sequence"], item["id"]),
    )
    for candidate in candidates:
        blocked = any(
            other["view"] == candidate["view"]
            and other["sequence"] < candidate["sequence"]
            and other["status"] != "completed"
            for other in jobs.values()
        )
        if not blocked:
            return candidate
    return None


def view_has_unfinished_jobs(state: dict, view: str) -> bool:
    return any(
        job["view"] == view and job["status"] != "completed"
        for job in state.setdefault("projection_jobs", {}).values()
    )


def update_transaction_status(state: dict, transaction_id: str) -> None:
    transaction = state["transactions"][transaction_id]
    jobs = [
        job
        for job in state.setdefault("projection_jobs", {}).values()
        if job["transaction_id"] == transaction_id
    ]
    if jobs and not all(job["status"] == "completed" for job in jobs):
        transaction["status"] = "fanout_pending"
        return
    transaction["status"] = "published"
