"""Best-effort WAL projection worker; jobs are observability hints only."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from errors import ForgeError
from materialization import ensure_intake_materialized, ensure_view_materialized
from repository import Repository
from state import audit_event, locked, save_state


LEASE_SECONDS = 60
RETRY_SECONDS = 60
MAX_JOB_ATTEMPTS = 8


def _discover(state: dict, manifest, replay, now: float) -> None:
    jobs = state.setdefault("projection_jobs", {})
    for view_id, active in replay.active_views.items():
        indexed = state.setdefault("views", {}).get(view_id, {})
        applied = indexed.get("materialized_seq", 0)
        if applied >= manifest.head_seq:
            continue
        identifier = f"project:{view_id}"
        job = jobs.get(identifier)
        if job is None:
            jobs[identifier] = {
                "view": view_id,
                "target_seq": manifest.head_seq,
                "status": "pending",
                "attempts": 0,
                "next_attempt_at": now,
                "last_error": None,
                "lease_token": None,
                "lease_expires_at": None,
            }
            continue
        if job["target_seq"] < manifest.head_seq:
            if job["status"] == "running" and (job["lease_expires_at"] or 0) > now:
                continue
            job["target_seq"] = manifest.head_seq
            job["status"] = "pending"
            job["attempts"] = 0
            job["next_attempt_at"] = now
            job["last_error"] = None
            job["lease_token"] = None
            job["lease_expires_at"] = None


def claim_projection_job(root: Path, now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    repository = Repository.open(root)
    manifest, _version, replay = repository.load()
    with locked(root) as state:
        _discover(state, manifest, replay, now)
        for view_id in sorted(replay.active_views):
            job = state["projection_jobs"].get(f"project:{view_id}")
            if job is None:
                continue
            if job["status"] == "running" and (job["lease_expires_at"] or 0) <= now:
                job["status"] = "retry"
                job["lease_token"] = None
                job["lease_expires_at"] = None
                job["next_attempt_at"] = now
            if job["status"] not in {"pending", "retry"} or job["next_attempt_at"] > now:
                continue
            token = uuid.uuid4().hex
            job["status"] = "running"
            job["attempts"] += 1
            job["lease_token"] = token
            job["lease_expires_at"] = now + LEASE_SECONDS
            save_state(root, state)
            return {"id": f"project:{view_id}", "view": view_id, "lease_token": token}
        save_state(root, state)
    return None


def complete_projection_job(root: Path, claimed: dict) -> None:
    repository = Repository.open(root)
    manifest, _version, replay = repository.load()
    materialized = ensure_view_materialized(root, claimed["view"], manifest, replay, repository)
    ensure_intake_materialized(root, materialized, manifest)
    with locked(root) as state:
        job = state["projection_jobs"].get(claimed["id"])
        if not job or job.get("lease_token") != claimed["lease_token"]:
            return
        entry = state["views"].setdefault(claimed["view"], {})
        entry["materialized_seq"] = manifest.head_seq
        entry["projected_tip"] = materialized.projected_tip
        entry["last_materialization_error"] = None
        job["status"] = "completed"
        job["lease_token"] = None
        job["lease_expires_at"] = None
        job["last_error"] = None
        job["next_attempt_at"] = None
        save_state(root, state)


def fail_projection_job(root: Path, claimed: dict, error: Exception, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with locked(root) as state:
        job = state["projection_jobs"].get(claimed["id"])
        if not job or job.get("lease_token") != claimed["lease_token"]:
            return
        job["lease_token"] = None
        job["lease_expires_at"] = None
        job["last_error"] = str(error)
        if job["attempts"] >= MAX_JOB_ATTEMPTS:
            job["status"] = "dead"
            job["next_attempt_at"] = None
            audit_event(root, "job.dead_letter", claimed["id"], str(error))
        else:
            job["status"] = "retry"
            job["next_attempt_at"] = now + RETRY_SECONDS
        state.setdefault("views", {}).setdefault(claimed["view"], {})[
            "last_materialization_error"
        ] = str(error)
        save_state(root, state)


def run_projection_job(root: Path, now: float | None = None) -> bool:
    claimed = claim_projection_job(root, now)
    if claimed is None:
        return False
    try:
        complete_projection_job(root, claimed)
    except Exception as error:
        fail_projection_job(root, claimed, error, now)
    return True


def drain_projection_jobs(root: Path, limit: int | None = None) -> int:
    completed = 0
    while limit is None or completed < limit:
        if not run_projection_job(root):
            break
        completed += 1
    return completed


def cancel_projection_job(root: Path, job_id_value: str) -> None:
    with locked(root) as state:
        job = state.get("projection_jobs", {}).get(job_id_value)
        if job is None:
            raise ForgeError(f"unknown projection job: {job_id_value}")
        if job["status"] == "running" and (job["lease_expires_at"] or 0) > time.time():
            raise ForgeError("projection job is running under an active lease")
        if job["status"] == "completed":
            raise ForgeError("projection job already completed")
        job["status"] = "cancelled"
        job["lease_token"] = None
        job["lease_expires_at"] = None
        job["next_attempt_at"] = None
        save_state(root, state)


def requeue_projection_job(root: Path, job_id_value: str) -> None:
    with locked(root) as state:
        job = state.get("projection_jobs", {}).get(job_id_value)
        if job is None:
            raise ForgeError(f"unknown projection job: {job_id_value}")
        if job["status"] not in {"cancelled", "dead", "retry"}:
            raise ForgeError(f"projection job is not requeueable: {job['status']}")
        job["status"] = "pending"
        job["attempts"] = 0
        job["next_attempt_at"] = time.time()
        job["last_error"] = None
        save_state(root, state)
