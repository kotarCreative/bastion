"""Best-effort WAL projection worker; jobs are observability hints only."""

from __future__ import annotations

import time
import uuid
from functools import partial
from pathlib import Path

from errors import ForgeError
from materialization import ensure_intake_materialized, ensure_view_materialized
from repository import Repository
from state import audit_event, locked, recover, save_state

# Bounds for one drain batch against one pinned manifest generation.
_REPOSITORY_COUNTERS = {"snapshot_acquisitions": 0, "recovery_decisions": 0, "final_probes": 0}


def worker_counters() -> dict[str, int]:
    return dict(_REPOSITORY_COUNTERS)


def reset_worker_counters() -> None:
    for key in _REPOSITORY_COUNTERS:
        _REPOSITORY_COUNTERS[key] = 0


LEASE_SECONDS = 60
RETRY_SECONDS = 60
MAX_JOB_ATTEMPTS = 8


def _target_head(manifest) -> dict:
    head = manifest.wal_head.to_dict()
    return {
        "seq": manifest.head_seq,
        "key": head["key"],
        "sha256": head["sha256"],
        "event_id": head["event_id"],
        "publication_id": head["publication_id"],
    }


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
                "target_manifest_head": _target_head(manifest),
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
            job["target_manifest_head"] = _target_head(manifest)
            job["status"] = "pending"
            job["attempts"] = 0
            job["next_attempt_at"] = now
            job["last_error"] = None
            job["lease_token"] = None
            job["lease_expires_at"] = None


def _claim_runnable(state: dict, replay, now: float, manifest, limit: int | None = None) -> list[dict]:
    """Claim immediately runnable jobs as short owner-checked transitions."""
    claimed: list[dict] = []
    for view_id in sorted(replay.active_views):
        if limit is not None and len(claimed) >= limit:
            break
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
        job["target_seq"] = manifest.head_seq
        job["target_manifest_head"] = _target_head(manifest)
        claimed.append(
            {
                "id": f"project:{view_id}",
                "view": view_id,
                "lease_token": token,
                "target_seq": manifest.head_seq,
                "target_manifest_head": _target_head(manifest),
            }
        )
    return claimed


def claim_projection_job(root: Path, now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    repository = Repository.open(root)
    manifest, _version, replay = repository.load()
    with locked(root) as state:
        _discover(state, manifest, replay, now)
        claimed = _claim_runnable(state, replay, now, manifest, limit=1)
        save_state(root, state)
        return claimed[0] if claimed else None


def _complete_claimed(
    root: Path, claimed: dict, manifest, materialized, repository=None
) -> None:
    """Complete one claim as a short owner-checked state transition.

    Completion marks only the exact target manifest head recorded at claim
    time; it never claims a newer head observed after work began.
    """
    target_seq = claimed.get("target_seq", manifest.head_seq)
    with locked(root) as state:
        job = state["projection_jobs"].get(claimed["id"])
        if not job or job.get("lease_token") != claimed["lease_token"]:
            return
        entry = state["views"].setdefault(claimed["view"], {})
        entry["materialized_seq"] = target_seq
        entry["projected_tip"] = materialized.projected_tip
        entry["last_materialization_error"] = None
        job["status"] = "completed"
        job["lease_token"] = None
        job["lease_expires_at"] = None
        job["last_error"] = None
        job["next_attempt_at"] = None
        save_state(root, state)


def complete_projection_job(root: Path, claimed: dict) -> None:
    repository = Repository.open(root)
    manifest, _version, replay = repository.load()
    materialized = ensure_view_materialized(root, claimed["view"], manifest, replay, repository)
    ensure_intake_materialized(root, materialized, manifest)
    _complete_claimed(root, claimed, manifest, materialized, repository)


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
    """Drain immediately runnable jobs with bounded historical work.

    Each batch pins one repository snapshot and performs one incremental
    derived-index recovery decision for all runnable jobs targeting that
    manifest generation. Materialization work holds no database lease or
    transaction; each claim and completion remains a short owner-checked
    state transition. Historical WAL and descriptor reads for one pinned
    generation are independent of view count.
    """
    completed = 0
    while limit is None or completed < limit:
        batch = _drain_projection_batch(
            root, None if limit is None else limit - completed
        )
        if not batch:
            break
        completed += batch
    return completed


def _batch_has_runnable(root: Path, manifest, replay, now: float) -> bool:
    """Read-only peek for lagging views without a lease or recovery.

    Deliberately coarse (any materialized view behind the pinned head):
    completed-but-stale jobs need re-targeting inside the claim session, so
    job states alone cannot prove idleness. The claim session sorts out
    leased, cancelled, and not-yet-due jobs; a spuriously entered session is
    still one short no-op transaction.
    """
    from state import load_state_sections

    try:
        state = load_state_sections(root, ["views"])
    except Exception:
        return True
    views = state.get("views", {})
    for view_id in replay.active_views:
        if views.get(view_id, {}).get("materialized_seq", 0) < manifest.head_seq:
            return True
    return False


def _drain_projection_batch(root: Path, limit: int | None) -> int:
    now = time.time()
    repository = Repository.open(root)
    # At most one complete snapshot acquisition per pinned generation.
    manifest, _version, replay = repository.load()
    _REPOSITORY_COUNTERS["snapshot_acquisitions"] += 1
    pinned = (manifest, _version, replay)
    if not _batch_has_runnable(root, manifest, replay, now):
        return 0
    # One incremental recovery decision for the whole batch.
    with locked(
        root, recoverer=partial(recover, pinned=pinned, repository=repository)
    ) as state:
        _REPOSITORY_COUNTERS["recovery_decisions"] += 1
        _discover(state, manifest, replay, now)
        claimed = _claim_runnable(state, replay, now, manifest, limit=limit)
        save_state(root, state)
    if not claimed:
        return 0
    # Materialization holds no database lease or transaction.
    results: list[tuple[dict, object, Exception | None]] = []
    for claim in claimed:
        try:
            materialized = ensure_view_materialized(
                root, claim["view"], manifest, replay, repository
            )
            ensure_intake_materialized(root, materialized, manifest)
        except Exception as error:
            results.append((claim, None, error))
        else:
            results.append((claim, materialized, None))
    done = 0
    for claim, materialized, error in results:
        if error is None:
            assert materialized is not None
            _complete_claimed(root, claim, manifest, materialized, repository)
            done += 1
        else:
            # A failed view does not roll back successful independent views
            # and retains the bounded retry/dead-letter behavior.
            fail_projection_job(root, claim, error, now)
    # One final conditional manifest probe: if publication advanced during
    # the batch, affected jobs remain or become pending for the newer
    # sequence after the current short transactions.
    _REPOSITORY_COUNTERS["final_probes"] += 1
    fresh_manifest, _fresh_version, _fresh_replay = repository.load()
    if fresh_manifest.head_seq != manifest.head_seq:
        with locked(root) as state:
            _discover(state, fresh_manifest, _fresh_replay, time.time())
            save_state(root, state)
    return done


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
