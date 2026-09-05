"""Receive-pack orchestration around the testable forge components."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import identity
from errors import ForgeError
from failpoints import checkpoint
from git import (
    clean_env,
    commit_metadata,
    git,
    output,
    quarantine_objects,
    ref,
    submitted_commits,
    tree_oid,
    verify_no_extra_objects,
)
from layout import ZERO_OID, canonical_repo, intake_repo
from projection import visible_advance_fast as visible_advance
from reconciliation import reconcile_commits
from artifacts import build_canonical_pack, build_submission_pack, upload_pack
from materialization import ensure_canonical_materialized, ensure_view_materialized
from repository import AlreadyCommitted, Candidate, Repository
from wal import idempotency_key_hash, record_fingerprint
from state import audit_event, load_state_sections
from validation import parse_updates, validate_ref_update


MAX_PUSH_COMMITS = int(os.environ.get("BASTION_MAX_PUSH_COMMITS", "4096"))
MAX_PUSH_OBJECTS = int(os.environ.get("BASTION_MAX_PUSH_OBJECTS", "100000"))
MAX_OBJECT_BYTES = int(os.environ.get("BASTION_MAX_OBJECT_BYTES", str(64 << 20)))


@dataclass(frozen=True)
class ReceiveContext:
    transport: str
    principal_id: str | None
    remote_addr: str | None


def receive_context(name: str) -> ReceiveContext:
    """Trusted wrapper context; clients cannot influence these keys."""
    declared = os.environ.get("BASTION_VIEW")
    if declared and declared != name:
        raise ForgeError(
            "receive context does not match the target repository", "integrity"
        )
    return ReceiveContext(
        os.environ.get("BASTION_TRANSPORT") or "local",
        os.environ.get("BASTION_PRINCIPAL_ID") or None,
        os.environ.get("BASTION_REMOTE_ADDR") or None,
    )


def transaction_id(name: str, old: str, new: str) -> str:
    return hashlib.sha256(f"{name}\0{old}\0{new}".encode()).hexdigest()


def transaction_parent(root: Path) -> Path:
    """Shared parent for private receive workspaces (mode 0700, never blindly cleared)."""
    parent = root / "transaction-objects"
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass
    return parent


def new_transaction_workspace(root: Path, name: str, old: str, new: str) -> Path:
    """Create a new private candidate workspace with an exclusive suffix.

    The name may contain a diagnostic request hash, but uniqueness comes from
    the exclusive ``mkdtemp`` suffix rather than from ``(view, old, new)``.
    Every publication attempt — including a manifest-CAS retry of the same
    event — owns a different workspace. Callers must never ``rmtree`` a path
    before exclusively creating and owning it.
    """
    parent = transaction_parent(root)
    digest = transaction_id(name, old, new)
    return Path(tempfile.mkdtemp(prefix=f"txn-{digest}-", dir=parent))


def cleanup_transaction_workspace(path: Path | str | None) -> None:
    """Idempotently remove only the workspace captured by one candidate."""
    if path is None:
        return
    try:
        target = Path(path)
    except Exception:
        return
    shutil.rmtree(target, ignore_errors=True)


def cleanup_stale_transaction_workspaces(root: Path, max_age_seconds: float = 3600.0) -> int:
    """Remove only workspaces proven not to belong to a live process.

    Crash leftovers are unreachable garbage. Startup never blindly clears the
    shared parent; this bounded pass removes only ``txn-*`` directories whose
    owner process is dead (via ``owner.json``) or that are older than
    ``max_age_seconds`` without an owner record. Returns the removal count.
    """
    from store import _linux_boot_id, _owner_alive, host_forge_id

    parent = root / "transaction-objects"
    if not parent.is_dir() or parent.is_symlink():
        return 0
    try:
        local_host = host_forge_id(root)
    except Exception:
        local_host = None
    try:
        local_boot = _linux_boot_id()
    except Exception:
        local_boot = None
    removed = 0
    now = time.time()
    try:
        entries = list(parent.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if not entry.is_dir() or entry.is_symlink():
                continue
            if not entry.name.startswith("txn-"):
                continue
            owner_file = entry / "owner.json"
            if owner_file.is_file():
                try:
                    import json as _json

                    owner = _json.loads(owner_file.read_text()).get("owner", "")
                except Exception:
                    owner = ""
                if owner and _owner_alive(owner, local_host, local_boot):
                    continue
            else:
                try:
                    age = now - entry.stat().st_mtime
                except OSError:
                    continue
                if age < max_age_seconds:
                    continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed


def candidate_environment(canonical: Path, directory: Path) -> dict[str, str]:
    env = clean_env()
    env["GIT_OBJECT_DIRECTORY"] = str(directory)
    env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(canonical / "objects")
    return env


def enforce_pack_limits(repo: Path, old: str, new: str, env: dict[str, str]) -> None:
    """Reject abusive packs before any validation work begins."""
    count = output(repo, "rev-list", "--count", f"{old}..{new}", env=env)
    try:
        if int(count) > MAX_PUSH_COMMITS:
            raise ForgeError("push exceeds the commit depth limit", "limits")
    except ValueError as error:
        raise ForgeError("push depth could not be measured", "limits") from error
    quarantined = sorted(quarantine_objects(env))
    if len(quarantined) > MAX_PUSH_OBJECTS:
        raise ForgeError("push exceeds the object count limit", "limits")
    if quarantined:
        query = ("".join(f"{oid}\n" for oid in quarantined)).encode()
        listing = git(
            repo,
            "cat-file",
            "--batch-check=%(objectname) %(objecttype) %(objectsize)",
            data=query,
            env=env,
            check=False,
        ).stdout
        seen = 0
        for line in listing.splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[1] in {b"blob", b"tree", b"commit", b"tag"}:
                seen += 1
                if int(fields[2]) > MAX_OBJECT_BYTES:
                    raise ForgeError(
                        "push contains an oversized object", "limits"
                    )
        if seen != len(quarantined):
            raise ForgeError(
                "quarantined objects could not be inspected", "integrity"
            )


def validate_commit_metadata(
    state: dict,
    destination: Path,
    commits: list[str],
    principal_id: str,
    hook_env: dict[str, str],
    received_at: int,
) -> None:
    """Enforce author binding and timestamp bounds before any promotion."""
    deadline = received_at + identity.TIMESTAMP_SKEW_SECONDS
    for submitted_oid in commits:
        email, author_ts, committer_ts = commit_metadata(destination, submitted_oid, hook_env)
        identity.require_author_binding(state, principal_id, email)
        if author_ts > deadline or committer_ts > deadline:
            raise ForgeError("submission carries a future-dated commit")


def prepare_submission(
    root: Path,
    name: str,
    view: dict,
    descriptor: dict,
    old: str,
    new: str,
    hook_env: dict[str, str],
    state: dict | None = None,
    canonical: Path | None = None,
    received_at: int | None = None,
):
    destination = intake_repo(root, name)
    canonical = canonical or canonical_repo(root)
    commits = submitted_commits(destination, old, new, hook_env)
    enforce_pack_limits(destination, old, new, hook_env)
    verify_no_extra_objects(destination, new, hook_env)
    if state is not None:
        if received_at is None:
            raise ForgeError("submission is missing a captured receive time")
        validate_commit_metadata(
            state, destination, commits, view["principal_id"], hook_env, received_at
        )
    canonical_old = ref(canonical, view["canonical_ref"])
    if git(
        canonical,
        "merge-base",
        "--is-ancestor",
        view["canonical_frontier"],
        canonical_old,
        check=False,
    ).returncode:
        raise ForgeError("canonical history no longer contains the view frontier")
    visible_advance(
        canonical,
        view["canonical_frontier"],
        canonical_old,
        destination,
        descriptor,
        tree_oid(destination, old, hook_env),
    )

    # Each call owns a distinct private workspace; concurrent identical
    # receives never share temporary object storage.
    transaction_objects = new_transaction_workspace(root, name, old, new)
    try:
        try:
            from store import process_identity as _process_identity

            owner = _process_identity()
        except Exception:
            owner = "unknown"
        try:
            import json as _json

            (transaction_objects / "owner.json").write_text(
                _json.dumps({"owner": owner, "at": time.time()})
            )
        except OSError:
            pass
    except Exception:
        pass
    candidate_env = candidate_environment(canonical, transaction_objects)
    try:
        transition = reconcile_commits(
            canonical,
            destination,
            commits,
            canonical_old,
            old,
            descriptor,
            hook_env,
            candidate_env,
        )
    except Exception:
        cleanup_transaction_workspace(transaction_objects)
        raise
    return transition


def hook_pre_receive(root: Path, name: str, updates: bytes) -> int:
    """CLI hook entry: rejections become a stable category plus the
    client-safe message, and are recorded in the audit log."""
    try:
        pre_receive(root, name, updates)
    except ForgeError as error:
        category = error.category or "policy"
        transport = os.environ.get("BASTION_TRANSPORT") or "local"
        remote = os.environ.get("BASTION_REMOTE_ADDR") or "-"
        try:
            from state import audit_event

            audit_event(
                root,
                "receive.reject",
                f"{name}/-",
                f"{transport} {remote} {category}",
            )
        except Exception:
            pass
        print(f"bastion: push rejected [{category}]", file=sys.stderr)
        print(f"bastion: {error}", file=sys.stderr)
        return 1
    return 0


def pre_receive(root: Path, name: str, updates: bytes) -> None:
    parsed = parse_updates(updates)
    context = receive_context(name)
    # Receive resolution reads repository metadata plus only the submitting
    # principal metadata required for author validation; it does not load
    # transactions, changes, jobs, teams, roles, or unrelated descriptors.
    # The captured authorization descriptor still comes from immutable
    # repository truth.
    control = load_state_sections(root, ["principals"])
    if control.get("repository_state") != "ready":
        raise ForgeError("forge initialization is incomplete", "integrity")
    repository = Repository.open(root)
    idempotency_key = os.environ.get("BASTION_IDEMPOTENCY_KEY")
    idempotency_hash = (
        idempotency_key_hash(repository.repository_id, idempotency_key)
        if idempotency_key
        else None
    )

    # Every build return path records the event_id of the WAL record that the
    # returned sequence refers to, so the audit below never needs a full replay.
    published_event_id: list[str] = []

    def build(manifest, replay):
        active = replay.active_views.get(name)
        if active is None:
            raise ForgeError("exactly one authorized branch may be updated")
        if context.principal_id != active["principal_id"]:
            raise ForgeError("authenticated receive principal does not match the WAL view", "policy")
        source = ensure_view_materialized(root, name, manifest, replay, repository)
        checkpoint(root, "wal.source_materialized")
        view = {
            "principal_id": active["principal_id"],
            "descriptor_id": active["descriptor_id"],
            "descriptor_generation": active["descriptor_generation"],
            "projection_epoch": active["projection_epoch"],
            "canonical_ref": manifest.canonical_ref,
            "projected_ref": active["projected_ref"],
            "canonical_frontier": source.cursor["canonical_frontier"],
            "projected_tip": source.projected_tip,
        }
        descriptor = repository._descriptor(active["descriptor_id"])
        if len(parsed) != 1:
            raise ForgeError("exactly one authorized branch may be updated")
        old, new, updated_ref = parsed[0]
        if updated_ref != active["projected_ref"]:
            raise ForgeError("branch update is not authorized")
        if old == ZERO_OID or new == ZERO_OID:
            raise ForgeError(
                "branch creation, deletion, and stale or force updates are not supported"
            )
        request = (name, active["projected_ref"], old, new)
        if idempotency_hash is not None:
            committed = replay.idempotency.get(idempotency_hash)
            if committed is not None:
                committed_event = replay.events.get(committed["event_id"])
                if committed_event is None:
                    raise ForgeError(
                        "idempotency index references a missing WAL event", "integrity"
                    )
                sequence = committed_event["seq"]
                record = replay.records[sequence - 1]
                payload = record["payload"]
                if (payload["view_id"], payload["projected_ref"], payload["projected_old"], payload["projected_new"]) != request:
                    raise ForgeError("idempotency key was already used for a different request")
                published_event_id.append(committed["event_id"])
                return AlreadyCommitted(sequence)
        old_requests = replay.requests.get(request, [])
        if old_requests and source.projected_tip != old:
            if idempotency_hash is not None:
                raise ForgeError(
                    "a new idempotency key cannot be attached to a committed request"
                )
            published_event_id.append(old_requests[-1])
            return AlreadyCommitted(replay.events[old_requests[-1]]["seq"])
        old, new, _updated_ref = validate_ref_update(view, parsed)
        canonical = ensure_canonical_materialized(root, manifest, repository)
        hook_env = os.environ.copy()
        received_at = int(time.time())
        transition = prepare_submission(
            root, name, view, descriptor, old, new, hook_env, state=control, canonical=canonical, received_at=received_at
        )
        for mapping in transition.mappings:
            change_id = mapping["change_id"]
            if not change_id:
                continue
            claim = replay.change_claims.get(change_id)
            if claim is not None and (
                claim["owner_principal_id"] != active["principal_id"]
                or claim["canonical_ref"] != manifest.canonical_ref
            ):
                cleanup_transaction_workspace(transition.candidate_object_dir)
                raise ForgeError("Change-Id belongs to another principal or branch")
        checkpoint(root, "wal.validation_complete")
        canonical_pack = build_canonical_pack(
            canonical, transition.canonical_old, transition.canonical_new,
            candidate_environment(canonical, transition.candidate_object_dir),
        )
        checkpoint(root, "wal.canonical_pack_built")
        receiving_intake = Path(
            os.environ.get("BASTION_INTAKE_REPO", str(intake_repo(root, name)))
        )
        submission_pack = build_submission_pack(receiving_intake, old, new, hook_env)
        checkpoint(root, "wal.submission_pack_built")
        canonical_ref = canonical_pack.reference("packs/canonical")
        submission_ref = submission_pack.reference("packs/submissions")
        payload = {
            "idempotency_key_hash": idempotency_hash,
            "view_id": name,
            "principal_id": active["principal_id"],
            "descriptor_id": active["descriptor_id"],
            "descriptor_generation": active["descriptor_generation"],
            "projection_epoch": active["projection_epoch"],
            "canonical_ref": manifest.canonical_ref,
            "canonical_old": transition.canonical_old,
            "canonical_new": transition.canonical_new,
            "projected_ref": active["projected_ref"],
            "projected_old": old,
            "projected_new": new,
            "canonical_frontier_old": active["canonical_frontier"],
            "canonical_pack": canonical_ref,
            "submission_pack": submission_ref,
            "mappings": transition.mappings,
            "received_at": received_at,
            "transport": context.transport,
            "remote_identity": context.remote_addr,
        }

        def upload_canonical(target: Repository) -> None:
            if upload_pack(target, "packs/canonical", canonical_pack) != canonical_ref:
                raise ForgeError("canonical pack upload metadata changed", "integrity")
            checkpoint(root, "wal.canonical_pack_uploaded")

        def upload_submission(target: Repository) -> None:
            if upload_pack(target, "packs/submissions", submission_pack) != submission_ref:
                raise ForgeError("submission pack upload metadata changed", "integrity")
            checkpoint(root, "wal.submission_pack_uploaded")

        candidate_objects = transition.candidate_object_dir
        candidate = Candidate(
            record_fingerprint(repository.repository_id, "submission", payload),
            payload,
            (upload_canonical, upload_submission),
            lambda: (
                canonical_pack.cleanup(),
                submission_pack.cleanup(),
                cleanup_transaction_workspace(candidate_objects),
            ),
        )
        published_event_id.append(candidate.event_id)
        return candidate

    sequence = repository.publish(build)
    checkpoint(root, "wal.git_success_returning")
    try:
        # The last entry belongs to the attempt that produced this sequence;
        # earlier entries belong to attempts that lost the manifest CAS race.
        audit_event(
            root,
            "receive.accept",
            f"{name}/{context.principal_id}",
            f"{context.transport} {context.remote_addr or '-'} seq={sequence}",
            published_event_id[-1],
        )
    except Exception:
        pass
