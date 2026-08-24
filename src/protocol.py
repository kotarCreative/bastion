"""Receive-pack orchestration around the testable forge components."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import identity
from errors import ForgeError
from failpoints import checkpoint
from git import (
    clean_env,
    commit_metadata,
    copy_object,
    git,
    output,
    quarantine_objects,
    ref,
    submitted_commits,
    tree_oid,
    verify_no_extra_objects,
)
from layout import ZERO_OID, canonical_repo, intake_repo, view_repo
from projection import visible_advance
from reconciliation import reconcile_commits
from state import (
    locked,
    reserve_change_ids,
    save_state,
    validate_change_ids,
    write_receiver_marker,
)
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


def candidate_environment(canonical: Path, directory: Path) -> dict[str, str]:
    env = clean_env()
    env["GIT_OBJECT_DIRECTORY"] = str(directory)
    env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(canonical / "objects")
    return env


def promote_objects(source: Path, canonical: Path) -> None:
    for object_path in source.rglob("*"):
        if not object_path.is_file():
            continue
        relative = object_path.relative_to(source)
        target = canonical / "objects" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            os.link(object_path, target)


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
) -> None:
    """Enforce author binding and timestamp bounds before any promotion."""
    deadline = time.time() + identity.TIMESTAMP_SKEW_SECONDS
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
) -> tuple[str, str, list[dict[str, str | None]], Path]:
    destination = intake_repo(root, name)
    canonical = canonical_repo(root)
    commits = submitted_commits(destination, old, new, hook_env)
    enforce_pack_limits(destination, old, new, hook_env)
    verify_no_extra_objects(destination, new, hook_env)
    if state is not None:
        validate_commit_metadata(
            state, destination, commits, view["principal_id"], hook_env
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

    transaction_objects = root / "transaction-objects" / transaction_id(name, old, new)
    shutil.rmtree(transaction_objects, ignore_errors=True)
    transaction_objects.mkdir(parents=True)
    candidate_env = candidate_environment(canonical, transaction_objects)
    try:
        canonical_new, mappings = reconcile_commits(
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
        shutil.rmtree(transaction_objects, ignore_errors=True)
        raise
    return canonical_old, canonical_new, mappings, transaction_objects


def copy_intake_closure(
    root: Path, name: str, old: str, new: str, env: dict[str, str]
) -> None:
    """Materialize submitted objects into the view store before ref publication."""
    intake = intake_repo(root, name)
    destination = view_repo(root, name)
    object_ids = output(
        intake, "rev-list", "--objects", "--no-object-names", new, f"^{old}", env=env
    ).splitlines()
    for oid in object_ids:
        copy_object(intake, destination, oid, source_env=env)


def publish_source_publication(
    root: Path, state: dict, name: str, view: dict, old: str, new: str
) -> None:
    """Copy the submitted closure into the view and move its ref by CAS.

    Receive-pack used to move this ref implicitly when views were push
    targets; the intake boundary performs the equivalent step explicitly
    so crash windows and retries keep their established recovery shape.
    """
    if new == ZERO_OID or old == ZERO_OID:
        raise ForgeError("source publication requires a linear branch update")
    hook_env = os.environ.copy()
    copy_intake_closure(root, name, old, new, hook_env)
    destination = view_repo(root, name)
    actual = ref(destination, view["projected_ref"])
    if actual == new:
        return
    if actual != old:
        raise ForgeError("projected ref changed outside synchronization")
    git(destination, "update-ref", view["projected_ref"], new, old)


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
    idempotency_key = os.environ.get("BASTION_IDEMPOTENCY_KEY")
    with locked(root) as state:
        view = state["views"].get(name)
        if not view:
            raise ForgeError("exactly one authorized branch may be updated")
        descriptor = identity.descriptor_for_view(state, name)
        from outbox import view_has_unfinished_jobs
        from service import repair_intake_mirror
        from state import has_unconfirmed_source, source_publication_pending

        # The intake advertisement must match the authoritative tip unless a
        # crashed receive is still being retried; in that window the mirror
        # deliberately stays at the old value so the retry CAS succeeds.
        if not source_publication_pending(root, state, name) and not (
            has_unconfirmed_source(state)
        ):
            repair_intake_mirror(root, state, name)

        # Idempotent replay is decided before ref validation so a retried
        # push whose outcome was already recorded returns that outcome
        # instead of a derived rejection.
        fingerprint = {
            "view": name,
            "old": parsed[0][0] if parsed else "",
            "new": parsed[0][1] if parsed else "",
        }
        if idempotency_key:
            from state import idempotency_lookup

            recorded = idempotency_lookup(root, idempotency_key)
            if recorded is not None:
                if recorded["fingerprint"] != fingerprint:
                    raise ForgeError(
                        "idempotency key was already used for a different request"
                    )
                transaction = state["transactions"].get(
                    recorded.get("outcome", {}).get("transaction_id", "")
                )
                if transaction and transaction["status"] in {
                    "canonical_committed",
                    "fanout_pending",
                    "published",
                }:
                    from state import is_ancestor

                    destination = view_repo(root, name)
                    actual = ref(destination, view["projected_ref"])
                    if actual != fingerprint["new"] and not is_ancestor(
                        destination, fingerprint["new"], actual
                    ):
                        publish_source_publication(
                            root,
                            state,
                            name,
                            view,
                            fingerprint["old"],
                            fingerprint["new"],
                        )
                return

        old, new, _updated_ref = validate_ref_update(view, parsed)
        retry_id = transaction_id(name, old, new)
        retry = state["transactions"].get(retry_id)
        if retry and retry["status"] == "source_unconfirmed":
            identity.require_matching_descriptor(
                view, retry["descriptor_id"], retry["descriptor_generation"]
            )
            if retry.get("principal_id") != view["principal_id"]:
                raise ForgeError("projection descriptor changed")
            write_receiver_marker(root, retry_id, os.getppid())
            retry.pop("source_error", None)
            save_state(root, state)
            publish_source_publication(root, state, name, view, old, new)
            return
        if source_publication_pending(root, state, name):
            raise ForgeError("view has an active receive transaction")
        if has_unconfirmed_source(state):
            raise ForgeError("canonical history contains an unconfirmed source publication")
        if view_has_unfinished_jobs(state, name):
            raise ForgeError("view has pending projection work; retry after synchronization")
        hook_env = os.environ.copy()
        canonical_old, canonical_new, mappings, transaction_objects = prepare_submission(
            root, name, view, descriptor, old, new, hook_env, state=state
        )
        try:
            validate_change_ids(
                state, view["principal_id"], view["canonical_ref"], mappings
            )
            promote_objects(transaction_objects, canonical_repo(root))
        finally:
            shutil.rmtree(transaction_objects, ignore_errors=True)
        checkpoint(root, "source.validation_complete")
        txid = transaction_id(name, old, new)
        sequence = state.setdefault("next_transaction_sequence", 1)
        state["next_transaction_sequence"] = sequence + 1
        transaction = {
            "id": txid,
            "view": name,
            "principal_id": view["principal_id"],
            "descriptor_id": descriptor["id"],
            "descriptor_generation": view["descriptor_generation"],
            "received_at": int(time.time()),
            "target_descriptors": {
                other_name: {
                    "descriptor_id": other["descriptor_id"],
                    "descriptor_generation": other["descriptor_generation"],
                }
                for other_name, other in sorted(state["views"].items())
                if other_name != name
            },
            "canonical_ref": view["canonical_ref"],
            "projected_ref": view["projected_ref"],
            "canonical_frontier_old": view["canonical_frontier"],
            "canonical_old": canonical_old,
            "canonical_new": canonical_new,
            "projected_old": old,
            "projected_new": new,
            "mappings": mappings,
            "sequence": sequence,
            "pending_views": sorted(
                view_name for view_name in state["views"] if view_name != name
            ),
            "status": "prepared",
        }
        write_receiver_marker(root, txid, os.getppid())
        state["transactions"][txid] = transaction
        save_state(root, state)
        if idempotency_key:
            from state import idempotency_record

            idempotency_record(
                root,
                idempotency_key,
                fingerprint,
                {"status": "prepared", "transaction_id": txid},
            )
        checkpoint(root, "source.prepared_persisted")
        from service import update_canonical_ref_locked

        update_canonical_ref_locked(
            root, state, canonical_old, canonical_new, create_event=False
        )
        checkpoint(root, "source.canonical_ref_published")
        reserve_change_ids(
            state, view["principal_id"], view["canonical_ref"], mappings
        )
        transaction["status"] = "canonical_committed"
        from outbox import enqueue_canonical_committed

        enqueue_canonical_committed(state, transaction)
        save_state(root, state)
        checkpoint(root, "source.canonical_state_persisted")
        from state import audit_event

        audit_event(
            root,
            "receive.accept",
            f"{name}/{view['principal_id']}",
            f"{context.transport} {context.remote_addr or '-'}"
            f" {old}..{new} tx={txid}",
        )
        publish_source_publication(root, state, name, view, old, new)
        checkpoint(root, "source.source_ref_published")


def post_receive(root: Path, name: str, updates: bytes) -> None:
    checkpoint(root, "source.ref_published")
    parsed = parse_updates(updates)
    with locked(root) as state:
        from service import repair_intake_mirror

        for old, new, updated_ref in parsed:
            txid = transaction_id(name, old, new)
            transaction = state["transactions"].get(txid)
            if transaction and transaction["projected_ref"] == updated_ref:
                (root / "receivers" / txid).unlink(missing_ok=True)
        repair_intake_mirror(root, state, name)
        save_state(root, state)
        checkpoint(root, "source.receiver_cleanup_persisted")
    checkpoint(root, "source.confirmation_persisted")
    from worker import drain_projection_jobs

    drain_projection_jobs(root)
