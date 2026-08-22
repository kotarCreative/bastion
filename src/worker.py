"""Projection-job worker with short state-lock critical sections."""

from __future__ import annotations

import copy
import time
import uuid
from pathlib import Path

import identity
from errors import ForgeError
from failpoints import checkpoint
from layout import canonical_repo
from outbox import MAX_JOB_ATTEMPTS, fail_job, next_job, requeue_expired_jobs, update_transaction_status
from service import build_projection_plan, publish_projection_plan
from state import (
    audit_event,
    has_unconfirmed_source,
    is_ancestor,
    locked,
    save_state,
    source_publication_pending,
)


LEASE_SECONDS = 60
RETRY_SECONDS = 60


def claim_projection_job(root: Path, now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    claimed_snapshot: dict | None = None
    with locked(root) as state:
        requeue_expired_jobs(state, now)
        if has_unconfirmed_source(state):
            save_state(root, state)
        else:
            excluded: set[str] = set()
            while True:
                job = next_job(state, now, excluded)
                if not job:
                    save_state(root, state)
                    break
                if source_publication_pending(root, state, job["view"]):
                    excluded.add(job["id"])
                    continue
                view = state["views"].get(job["view"])
                if not view:
                    job["attempts"] += 1
                    dead = fail_job(job, "unknown projection view", now)
                    if dead:
                        transaction = state["transactions"].get(job["transaction_id"])
                        if transaction is not None:
                            transaction.setdefault("view_errors", {})[job["view"]] = (
                                "unknown projection view"
                            )
                            audit_event(
                                root,
                                "job.dead_letter",
                                job["id"],
                                f"exhausted {MAX_JOB_ATTEMPTS} attempts: unknown projection view",
                            )
                    excluded.add(job["id"])
                    continue
                if (
                    view.get("descriptor_id") != job.get("descriptor_id")
                    or view.get("descriptor_generation")
                    != job.get("descriptor_generation")
                ):
                    raise ForgeError("projection descriptor changed")
                if job["canonical_to"] == view["canonical_frontier"] or is_ancestor(
                    canonical_repo(root), job["canonical_to"], view["canonical_frontier"]
                ):
                    job["status"] = "completed"
                    job["lease_token"] = None
                    job.pop("lease_expires_at", None)
                    job["last_error"] = None
                    job.pop("plan", None)
                    transaction = state["transactions"][job["transaction_id"]]
                    transaction.get("view_errors", {}).pop(job["view"], None)
                    if job["view"] in transaction.get("pending_views", []):
                        transaction["pending_views"].remove(job["view"])
                    update_transaction_status(state, job["transaction_id"])
                    continue
                token = uuid.uuid4().hex
                job["status"] = "running"
                job["attempts"] += 1
                job["lease_token"] = token
                job["lease_expires_at"] = now + LEASE_SECONDS
                job["expected_frontier"] = view["canonical_frontier"]
                job["expected_projected_tip"] = view["projected_tip"]
                snapshot = copy.deepcopy(job)
                snapshot["view_state"] = copy.deepcopy(view)
                # Jobs finish under their captured descriptor; catalog drift
                # is dormant until an explicit policy transition.
                snapshot["descriptor"] = copy.deepcopy(
                    state["view_descriptors"][job["descriptor_id"]]
                )
                save_state(root, state)
                claimed_snapshot = snapshot
                break
    if claimed_snapshot is not None:
        checkpoint(root, "worker.claim_persisted")
    return claimed_snapshot


def fail_projection_job(
    root: Path, claimed: dict, error: Exception, now: float | None = None
) -> None:
    now = time.time() if now is None else now
    with locked(root, recoverer=lambda _root, _state: None) as state:
        job = state["projection_jobs"].get(claimed["id"])
        if not job or job.get("lease_token") != claimed["lease_token"]:
            return
        dead = fail_job(job, str(error), now)
        transaction = state["transactions"][job["transaction_id"]]
        transaction.setdefault("view_errors", {})[job["view"]] = str(error)
        update_transaction_status(state, job["transaction_id"])
        save_state(root, state)
        if dead:
            audit_event(
                root,
                "job.dead_letter",
                claimed["id"],
                f"exhausted {MAX_JOB_ATTEMPTS} attempts: {error}",
            )
        checkpoint(root, "worker.retry_persisted")


def cancel_projection_job(root: Path, job_id_value: str) -> None:
    """Cancel a queued, retrying, dead, or expired-lease job."""
    now = time.time()
    with locked(root) as state:
        requeue_expired_jobs(state, now)
        job = state["projection_jobs"].get(job_id_value)
        if not job:
            raise ForgeError(f"unknown projection job: {job_id_value}")
        if job["status"] == "completed":
            raise ForgeError("projection job already completed")
        if job["status"] == "running" and job.get("lease_expires_at", 0) > now:
            raise ForgeError("projection job is running under an active lease")
        if job["status"] == "cancelled":
            return
        previous = job["status"]
        job["status"] = "cancelled"
        job["lease_token"] = None
        job.pop("lease_expires_at", None)
        job["next_attempt_at"] = None
        transaction = state["transactions"][job["transaction_id"]]
        transaction.setdefault("view_errors", {})[job["view"]] = (
            "cancelled by operator"
        )
        update_transaction_status(state, job["transaction_id"])
        save_state(root, state)
        audit_event(root, "job.cancelled", job_id_value, f"previous status: {previous}")


def requeue_projection_job(root: Path, job_id_value: str) -> None:
    """Requeue a dead or cancelled job with attempts reset."""
    with locked(root) as state:
        job = state["projection_jobs"].get(job_id_value)
        if not job:
            raise ForgeError(f"unknown projection job: {job_id_value}")
        if job["status"] not in {"dead", "cancelled"}:
            raise ForgeError(f"projection job is not dead or cancelled: {job['status']}")
        previous = job["status"]
        job["status"] = "pending"
        job["attempts"] = 0
        job["last_error"] = None
        job["next_attempt_at"] = 0
        transaction = state["transactions"][job["transaction_id"]]
        transaction.get("view_errors", {}).pop(job["view"], None)
        update_transaction_status(state, job["transaction_id"])
        save_state(root, state)
        audit_event(root, "job.requeued", job_id_value, f"previous status: {previous}")


def complete_projection_job(
    root: Path, claimed: dict, plan: dict, now: float | None = None
) -> None:
    now = time.time() if now is None else now
    with locked(root, recoverer=lambda _root, _state: None) as state:
        job = state["projection_jobs"].get(claimed["id"])
        if not job or job.get("lease_token") != claimed["lease_token"]:
            raise ForgeError("projection job lease was lost")
        if source_publication_pending(root, state, job["view"]):
            job["status"] = "retry"
            job["lease_token"] = None
            job["last_error"] = "view started a receive transaction while projecting"
            job["next_attempt_at"] = now + RETRY_SECONDS
            save_state(root, state)
            return
        view = state["views"][job["view"]]
        identity.require_matching_descriptor(
            view, job["descriptor_id"], job["descriptor_generation"]
        )
        if (
            plan.get("descriptor_id") != job.get("descriptor_id")
            or plan.get("descriptor_generation") != job.get("descriptor_generation")
        ):
            raise ForgeError("projection descriptor changed")
        job["status"] = "ready"
        job["plan"] = plan
        save_state(root, state)
        checkpoint(root, "worker.plan_persisted")
        publish_projection_plan(root, state, plan)
        job["status"] = "completed"
        job["lease_token"] = None
        job.pop("lease_expires_at", None)
        job["last_error"] = None
        job.pop("plan", None)
        transaction = state["transactions"][job["transaction_id"]]
        transaction.get("view_errors", {}).pop(job["view"], None)
        if job["view"] in transaction.get("pending_views", []):
            transaction["pending_views"].remove(job["view"])
        update_transaction_status(state, job["transaction_id"])
        save_state(root, state)
        checkpoint(root, "worker.completion_persisted")


def run_projection_job(root: Path, now: float | None = None) -> bool:
    claimed = claim_projection_job(root, now)
    if not claimed:
        return False
    try:
        plan = claimed.get("plan") or build_projection_plan(
            root,
            claimed["view"],
            claimed["view_state"],
            claimed["descriptor"],
            claimed["canonical_to"],
        )
        complete_projection_job(root, claimed, plan, now=now)
    except Exception as error:
        fail_projection_job(root, claimed, error, now=now)
    return True


def drain_projection_jobs(root: Path, limit: int | None = None) -> int:
    completed = 0
    while limit is None or completed < limit:
        if not run_projection_job(root):
            break
        completed += 1
    return completed
