"""Forge administration and view-synchronization use cases."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

import identity
from errors import ForgeError
from failpoints import checkpoint
from git import clean_env, git, output, ref, run, tree_entries
from layout import ZERO_OID
from artifacts import build_full_ref_pack, upload_pack
from materialization import (
    ensure_canonical_materialized,
    ensure_intake_materialized,
    ensure_view_materialized,
)
from repository import AlreadyCommitted, Candidate, Repository, write_repository_config
from object_store_s3 import S3ObjectStore, parse_s3_uri
from object_store_artifacts import ArtifactsObjectStore, ArtifactsObjectStoreConfig, parse_artifacts_uri
from object_store import ObjectStoreError
from state import (
    audit_event,
    is_ancestor,
    load_state,
    locked,
    save_state,
)
from validation import capability_allowed, normalize_prefix, validate_view_name
from validation import parse_change_id
from wal import event_id, record_fingerprint


def _object_store_path(root: Path, value: str) -> Path:
    if value == "filesystem":
        return root / "object-store"
    if not value.startswith("filesystem:") or not value[len("filesystem:") :]:
        raise ForgeError("object store must be filesystem or filesystem:PATH")
    raw = value[len("filesystem:") :]
    path = Path(raw)
    if not path.is_absolute():
        if (
            not raw.isascii()
            or "\\" in raw
            or "\x00" in raw
            or any(part in {"", ".", ".."} for part in raw.split("/"))
        ):
            raise ForgeError("object store path has an unsafe component")
    return path if path.is_absolute() else root / path


def init_forge(
    root: Path,
    source: Path,
    branch: str,
    database: str | None = None,
    object_store: str = "filesystem",
    *,
    admin_database: str | None = None,
    s3_region: str | None = None,
    s3_expected_bucket_owner: str | None = None,
    artifacts_config: ArtifactsObjectStoreConfig | None = None,
) -> None:
    if run("git", "check-ref-format", "--branch", branch, check=False).returncode:
        raise ForgeError(f"invalid branch name: {branch}")
    if os.pathsep in str(root.resolve()):
        raise ForgeError(
            f"forge root cannot contain the object-path separator {os.pathsep!r}"
        )
    was_nonempty = root.exists() and any(root.iterdir())
    root.mkdir(parents=True, exist_ok=True)
    (root / "views").mkdir(exist_ok=True)
    (root / "intake").mkdir(exist_ok=True)
    from store import backend_record_path, initialize_store, open_store

    if was_nonempty and not backend_record_path(root).exists():
        resumable = {
            "views", "intake", "store.sqlite3", "store.sqlite3-wal",
            "store.sqlite3-shm",
        }
        if any(item.name not in resumable for item in root.iterdir()):
            raise ForgeError(f"forge root is not empty: {root}")
    store = (
        open_store(root)
        if backend_record_path(root).exists()
        else initialize_store(root, database, admin_database=admin_database)
    )
    try:
        store.bootstrap()
    finally:
        store.close()
    try:
        state = load_state(root)
    except ForgeError as error:
        if "forge state is not initialized" not in str(error):
            raise
        state = {
            "canonical_branch": None,
            "repository_state": "initializing",
            "repository_id": None,
            "last_indexed_seq": 0,
        }
    if state.get("repository_state") != "initializing":
        if was_nonempty:
            raise ForgeError(f"forge root is not empty: {root}")
        raise ForgeError("forge initialization state is invalid", "integrity")
    persisted_branch = state.get("canonical_branch")
    if persisted_branch not in {None, branch}:
        raise ForgeError("initialization branch differs from the persisted branch")
    requested_object_store = object_store
    if requested_object_store == "filesystem":
        requested_object_store = os.environ.get("BASTION_OBJECT_STORE", requested_object_store)
    resolved_region = s3_region or os.environ.get("BASTION_S3_REGION")
    resolved_owner = s3_expected_bucket_owner or os.environ.get("BASTION_S3_EXPECTED_BUCKET_OWNER")
    s3_config = None
    object_store_path = None
    if artifacts_config is not None:
        if requested_object_store != "filesystem":
            raise ForgeError("artifacts configuration conflicts with object store URI")
    elif requested_object_store.startswith("artifacts+https://"):
        if s3_region is not None or s3_expected_bucket_owner is not None:
            raise ForgeError("S3 options are invalid for Artifacts storage")
        try:
            artifacts_config = parse_artifacts_uri(requested_object_store)
        except ValueError as error:
            raise ForgeError(str(error)) from error
    elif requested_object_store.startswith("s3://"):
        if not resolved_region or not resolved_owner:
            raise ForgeError("S3 region and expected bucket owner are required")
        try:
            s3_config = parse_s3_uri(
                requested_object_store,
                region=resolved_region,
                expected_bucket_owner=resolved_owner,
            )
        except ValueError as error:
            raise ForgeError(str(error)) from error
    else:
        if s3_region is not None or s3_expected_bucket_owner is not None:
            raise ForgeError("S3 options are invalid for filesystem storage")
        object_store_path = _object_store_path(root, requested_object_store)
    if (root / "repository.json").exists():
        try:
            repository_id = Repository.open(root).repository_id
        except ObjectStoreError as error:
            raise ForgeError("object storage is unavailable", "storage") from error
    else:
        repository_id = state.get("repository_id") or uuid.uuid4().hex
    write_repository_config(
        root,
        repository_id,
        branch,
        object_store_path,
        s3_config=s3_config,
        artifacts_config=artifacts_config,
    )
    state.update(
        {
            "canonical_branch": branch,
            "repository_state": "initializing",
            "repository_id": repository_id,
            "last_indexed_seq": state.get("last_indexed_seq", 0),
        }
    )
    for section in (
        "principals", "teams", "roles", "role_bindings", "view_descriptors",
        "views", "transactions", "changes", "projection_jobs",
    ):
        state.setdefault(section, {})
    save_state(root, state)
    try:
        repository = Repository.open(root)
    except ObjectStoreError as error:
        raise ForgeError("object storage is unavailable", "storage") from error
    if isinstance(repository.store, S3ObjectStore):
        try:
            repository.store.verify_bucket()
        except ObjectStoreError as error:
            raise ForgeError("object storage is unavailable", "storage") from error
    bootstrap = root / "materializations" / "bootstrap-source.git"
    canonical_ref = f"refs/heads/{branch}"
    try:
        manifest, _version, _replay = repository.load()
    except ForgeError as error:
        if "manifest is missing" not in str(error):
            raise
        bootstrap.parent.mkdir(parents=True, exist_ok=True)
        if bootstrap.exists():
            probe = git(
                bootstrap,
                "rev-parse",
                "--is-bare-repository",
                check=False,
            )
            if probe.returncode or probe.stdout.decode().strip() != "true":
                shutil.rmtree(bootstrap, ignore_errors=True)
        if not bootstrap.exists():
            run("git", "clone", "--bare", "--no-local", str(source), str(bootstrap), env=clean_env())
        if output(bootstrap, "rev-parse", "--show-object-format") != "sha1":
            raise ForgeError("this experiment currently supports SHA-1 repositories only")
        canonical_tip = ref(bootstrap, canonical_ref)
        if canonical_tip == ZERO_OID:
            raise ForgeError(f"source has no branch named {branch}")
        pack = build_full_ref_pack(bootstrap, canonical_ref)
        try:
            claims: dict[str, dict] = {}
            from git import commit_message, first_parent_commits

            for oid in first_parent_commits(bootstrap, ZERO_OID, canonical_tip):
                change_id = parse_change_id(commit_message(bootstrap, oid))
                if change_id:
                    claims[change_id] = {
                        "change_id": change_id,
                        "owner_principal_id": None,
                        "canonical_ref": canonical_ref,
                    }
            pack_ref = pack.reference("packs/canonical")
            payload = {
                "canonical_ref": canonical_ref,
                "canonical_tip": canonical_tip,
                "canonical_pack": pack_ref,
                "change_claims": [claims[key] for key in sorted(claims)],
            }
            repository.bootstrap(
                Candidate(
                    record_fingerprint(repository_id, "import", payload),
                    payload,
                    (lambda target: upload_pack(target, "packs/canonical", pack),),
                )
            )
        finally:
            pack.cleanup()
        manifest, _version, _replay = repository.load()
    if manifest.canonical_ref != canonical_ref:
        raise ForgeError("initialized manifest branch differs from requested branch", "integrity")
    ensure_canonical_materialized(root, manifest, repository)
    staging = root / "admin-staging.git"
    if not staging.is_dir():
        run("git", "init", "--bare", "-q", "-b", branch, str(staging), env=clean_env())
    shutil.rmtree(bootstrap, ignore_errors=True)
    state = load_state(root)
    if state.get("repository_id") != repository_id:
        raise ForgeError("initialization repository ID fence is inconsistent", "integrity")
    state["repository_state"] = "ready"
    save_state(root, state)


def _catalog_operation(root: Path, operation) -> None:
    with locked(root) as state:
        operation(state)
        save_state(root, state)


def add_principal(root: Path, principal_id: str, emails: list[str] | None = None) -> None:
    _catalog_operation(
        root, lambda state: identity.add_principal(state, principal_id, emails)
    )


def add_service_account(
    root: Path,
    service_id: str,
    owner_principal_id: str,
    emails: list[str] | None = None,
) -> None:
    _catalog_operation(
        root,
        lambda state: identity.add_service_account(
            state, service_id, owner_principal_id, emails
        ),
    )


def add_team(root: Path, team_id: str, members: list[str]) -> None:
    _catalog_operation(root, lambda state: identity.add_team(state, team_id, members))


def add_role(
    root: Path,
    role_id: str,
    allow: dict[str, list[str]],
    deny: dict[str, list[str]],
    *,
    permissions: dict | None = None,
) -> None:
    _catalog_operation(
        root,
        lambda state: identity.add_role(
            state, role_id, allow, deny, permissions=permissions
        ),
    )


def bind_role(root: Path, role_id: str, subject_kind: str, subject_id: str) -> None:
    _catalog_operation(
        root, lambda state: identity.bind_role(state, role_id, subject_kind, subject_id)
    )


def add_view(
    root: Path,
    name: str,
    principal_id: str,
    branch: str,
) -> None:
    validate_view_name(name)
    with locked(root) as state:
        if state.get("repository_state") != "ready":
            raise ForgeError("forge initialization is incomplete")
        if branch != state["canonical_branch"]:
            raise ForgeError(
                "this experiment supports the configured canonical branch only"
            )
        # Dormant policy: a principal with existing views keeps its assigned
        # descriptor until an explicit transition; otherwise bind fresh
        # resolution.
        descriptor = identity.assigned_descriptor(state, principal_id)
        if descriptor is None:
            descriptor = identity.effective_descriptor(
                state, principal_id, f"refs/heads/{branch}"
            )
        if not descriptor["capabilities"]["read"]:
            raise ForgeError("principal has no effective readable path")
        save_state(root, state)
    repository = Repository.open(root)
    repository.put_descriptor(descriptor)

    def build(manifest, replay):
        existing = replay.active_views.get(name)
        desired = {
            "principal_id": principal_id,
            "projected_ref": f"refs/heads/{branch}",
            "descriptor_id": descriptor["id"],
            "descriptor_generation": 1,
            "projection_epoch": 1,
        }
        if existing is not None:
            if all(existing[key] == value for key, value in desired.items()):
                return AlreadyCommitted(existing["epoch_start_seq"])
            raise ForgeError(f"view already exists: {name}")
        # Policy fencing: validate the principal's committed assignment on
        # this exact WAL snapshot, for every CAS retry.  An intervening
        # transition invalidates the captured descriptor, so the candidate
        # is rebuilt against the committed assignment instead of
        # publishing a view that retains revoked access.
        assigned = {
            view["descriptor_id"]
            for view in replay.active_views.values()
            if view["principal_id"] == principal_id
        }
        if len(assigned) > 1:
            raise ForgeError(
                "principal has inconsistent committed assignments", "integrity"
            )
        effective = descriptor
        if assigned:
            committed_id = next(iter(assigned))
            if committed_id != effective["id"]:
                effective = repository._descriptor(committed_id)
                if not effective["capabilities"].get("read"):
                    raise ForgeError("principal has no effective readable path")
        payload = {
            "view_id": name,
            "principal_id": principal_id,
            "projected_ref": f"refs/heads/{branch}",
            "descriptor_id": effective["id"],
            "descriptor_generation": 1,
            "projection_epoch": 1,
            "canonical_frontier": manifest.canonical_tip,
        }
        return Candidate(
            record_fingerprint(repository.repository_id, "view_create", payload), payload
        )

    repository.publish(build)
    manifest, _version, replay = repository.load()
    materialized = ensure_view_materialized(root, name, manifest, replay, repository)
    ensure_intake_materialized(root, materialized, manifest)


def invite_collaborator(
    root: Path, actor_view_id: str, principal_id: str, email: str,
    readable_paths: list[str], access: str, clerk_user_id: str | None = None,
) -> None:
    """Invite a scoped user, fenced by the owner's WAL policy."""
    if access not in {"read-only", "contributor"}:
        raise ForgeError("invalid collaborator access")
    identity.validate_identifier(principal_id)
    validate_view_name(principal_id)
    emails = identity.normalize_emails([email])
    if clerk_user_id is not None and (
        not isinstance(clerk_user_id, str) or not clerk_user_id.startswith("user_")
        or len(clerk_user_id) > 255
    ):
        raise ForgeError("invalid Clerk user identity")
    if not isinstance(readable_paths, list) or not readable_paths or len(readable_paths) > 100:
        raise ForgeError("choose between one and 100 readable paths")
    if not all(isinstance(path, str) for path in readable_paths):
        raise ForgeError("readable paths must be strings")
    paths = sorted(set(normalize_prefix(path) for path in readable_paths))
    if any("\x00" in path or len(os.fsencode(path)) > 4096 for path in paths):
        raise ForgeError("invalid readable path")
    repository = Repository.open(root)
    with locked(root) as state:
        manifest, _version, replay = repository.load()
        actor = replay.active_views.get(actor_view_id)
        if actor is None:
            raise ForgeError("unknown owner view", "policy")
        descriptor = repository._descriptor(actor["descriptor_id"])
        if not capability_allowed(descriptor["capabilities"]["admin"], descriptor["restrictions"]["admin"], b""):
            raise ForgeError("repository owner permission is required", "policy")
        require_delegable_coverage(descriptor, paths, access)
        if principal_id in state["principals"] or principal_id in replay.active_views:
            raise ForgeError("collaborator identifier already exists", "conflict")
        if clerk_user_id and any(
            item.get("clerk_user_id") == clerk_user_id for item in state["principals"].values()
        ):
            raise ForgeError("Clerk account already invited", "conflict")
        if any(set(emails).intersection(item.get("emails", [])) for item in state["principals"].values()):
            raise ForgeError("email already invited", "conflict")
        canonical = ensure_canonical_materialized(root, manifest, repository)
        for path in paths:
            if path and git(canonical, "cat-file", "-e", f"{manifest.canonical_tip}:{path}", check=False).returncode:
                raise ForgeError("readable path does not exist")
        role_id = "invite-" + uuid.uuid4().hex
        old = copy.deepcopy(state)
        identity.add_principal(state, principal_id, emails)
        if clerk_user_id:
            state["principals"][principal_id]["clerk_user_id"] = clerk_user_id
        allow = identity.empty_capability_map()
        allow["read"] = paths
        if access == "contributor":
            for capability in ("create", "modify", "delete", "mode", "symlink"):
                allow[capability] = paths
        identity.add_role(state, role_id, allow, identity.empty_capability_map())
        identity.bind_role(state, role_id, "principal", principal_id)
        fresh = identity.effective_descriptor(state, principal_id, actor["projected_ref"])
        save_state(root, state)

        def build(current_manifest, current_replay):
            current_actor = current_replay.active_views.get(actor_view_id)
            if current_actor != actor:
                raise ForgeError("repository ownership changed during invitation", "policy")
            if current_manifest.canonical_tip != manifest.canonical_tip:
                raise ForgeError("repository changed during invitation", "conflict")
            if principal_id in current_replay.active_views:
                raise ForgeError("collaborator view already exists", "conflict")
            payload = {
                "view_id": principal_id, "principal_id": principal_id,
                "projected_ref": actor["projected_ref"], "descriptor_id": fresh["id"],
                "descriptor_generation": 1, "projection_epoch": 1,
                "canonical_frontier": current_manifest.canonical_tip,
            }
            return Candidate(record_fingerprint(repository.repository_id, "view_create", payload), payload)

        try:
            repository.put_descriptor(fresh)
            repository.publish(build)
        except ForgeError as error:
            # Same pre-publication rollback policy as `set_path_privacy`: every
            # refusal raised before the manifest CAS is definitive, so the
            # half-applied catalog must not survive. Only an ambiguous
            # `storage` fault retains the desired catalog.
            if error.category in {"policy", "conflict", "limits"}:
                save_state(root, old)
            raise
        manifest, _version, replay = repository.load()
        from state import recover

        recover(root, state, pinned=(manifest, _version, replay), repository=repository)
        save_state(root, state)
        audit_event(root, "collaborator.invite", actor["principal_id"], principal_id)
    ensure_view_materialized(root, principal_id, manifest, replay, repository)


def sync_view(root: Path, name: str) -> None:
    # Explicit sync performs full validation rather than the request fast path.
    state = load_state(root)
    if state.get("repository_state") != "ready":
        raise ForgeError("forge initialization is incomplete")
    repository = Repository.open(root)
    manifest, _version, replay = repository.load()
    materialized = ensure_view_materialized(
        root, name, manifest, replay, repository, full_validation=True
    )
    ensure_intake_materialized(root, materialized, manifest)


def validate_canonical_update(canonical: Path, expected: str, new: str) -> None:
    oid = re.compile(r"^[0-9a-f]{40}$")
    if not oid.fullmatch(expected) or not oid.fullmatch(new):
        raise ForgeError("canonical updates require immutable full commit OIDs")
    if ref(canonical, new) == ZERO_OID:
        raise ForgeError("canonical update target does not exist")
    if not is_ancestor(canonical, expected, new):
        raise ForgeError("canonical non-fast-forward updates are not supported")
    from git import first_parent_commits, validate_no_empty_subtrees
    from validation import ALLOWED_MODES

    for commit_oid in first_parent_commits(canonical, expected, new):
        validate_no_empty_subtrees(canonical, commit_oid)
        for _path, (mode, kind, _oid) in tree_entries(canonical, commit_oid).items():
            if mode not in ALLOWED_MODES or kind != b"blob":
                raise ForgeError("canonical update contains an unsupported tree entry")


def _changed_paths_between(canonical: Path, expected: str, new: str) -> set[bytes]:
    before = tree_entries(canonical, expected)
    after = tree_entries(canonical, new)
    return {
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    }


def _commit_parent(canonical: Path, oid: str) -> str:
    """Return the first parent of a commit, or ZERO_OID for a root commit."""
    from layout import ZERO_OID

    parents = output(canonical, "show", "-s", "--format=%P", oid).split()
    return parents[0] if parents else ZERO_OID


DELEGATED_CAPABILITIES = {
    "read-only": ("read",),
    "contributor": ("read", "create", "modify", "delete", "mode", "symlink"),
}


def require_delegable_coverage(descriptor: dict, paths: list[str], access: str) -> None:
    """Forbid delegating visibility the granter does not itself hold.

    Root ``admin`` at ``b""`` is the repository-owner proof and authorizes the
    *act* of inviting a collaborator, including assigning the documented
    Contributor level. It is not, however, a licence to disclose repository
    content the owner cannot see: an owner restricted to a subtree must not
    mint a collaborator whose view reaches outside it, because that leaks both
    hidden paths and their contents.

    Write delegation stays under owner authority, matching the documented
    owner workflow in which a read-only owner assigns a Contributor.
    """
    read_allow = descriptor["capabilities"]["read"]
    read_restrictions = descriptor["restrictions"]["read"]
    for path in paths:
        prefix = os.fsencode(path)
        # An invitation grants the whole subtree, not just its directory
        # boundary. Checking only that boundary would let an administrator
        # bypass a nested private path by inviting someone to its parent.
        denied_descendant = any(
            (not prefix or os.fsencode(restriction).startswith(prefix + b"/"))
            and not capability_allowed(read_allow, read_restrictions, os.fsencode(restriction))
            for restriction in read_restrictions
        )
        if not capability_allowed(read_allow, read_restrictions, prefix) or denied_descendant:
            raise ForgeError(
                "cannot grant visibility you do not hold yourself", "policy"
            )


def require_admin_coverage(
    descriptor: dict, canonical: Path, expected: str, new: str
) -> None:
    """Require the captured descriptor to hold admin over every commit transition."""
    allow = descriptor["capabilities"]["admin"]
    restrictions = descriptor["restrictions"]["admin"]
    from git import first_parent_commits

    previous = expected
    for oid in first_parent_commits(canonical, expected, new):
        before = tree_entries(canonical, previous)
        after = tree_entries(canonical, oid)
        changed = before.keys() | after.keys()
        for path in changed:
            if before.get(path) != after.get(path):
                if not capability_allowed(allow, restrictions, path):
                    raise ForgeError(
                        "acting principal lacks admin coverage for the canonical update"
                    )
        previous = oid


def update_canonical_ref(root: Path, expected: str, new: str, principal_id: str) -> None:
    """Publish an administrative fast-forward from disposable staging input."""
    state = load_state(root)
    if state.get("repository_state") != "ready":
        raise ForgeError("forge initialization is incomplete")
    repository = Repository.open(root)
    canonical_ref = f"refs/heads/{state['canonical_branch']}"
    # Capture one immutable descriptor from the locked catalog snapshot before
    # any publication work begins. It remains fixed for retries and ambiguous
    # write resolution, and is uploaded as an immutable prerequisite before the
    # WAL record commits.
    captured_descriptor = identity.effective_descriptor(state, principal_id, canonical_ref)
    received_at = int(time.time())
    fingerprint_payload = {
        "principal_id": principal_id,
        "descriptor_id": captured_descriptor["id"],
        "canonical_ref": canonical_ref,
        "canonical_old": expected,
        "canonical_new": new,
    }
    wanted_fingerprint = record_fingerprint(
        repository.repository_id, "canonical_update", fingerprint_payload
    )

    def build(manifest, replay):
        existing = replay.events.get(event_id(wanted_fingerprint))
        if existing is not None:
            return AlreadyCommitted(existing["seq"])
        if manifest.canonical_tip != expected:
            raise ForgeError("canonical ref changed before administrative update", "conflict")
        canonical = ensure_canonical_materialized(root, manifest, repository)
        staging = root / "admin-staging.git"
        if not staging.is_dir():
            run("git", "init", "--bare", "-q", "-b", state["canonical_branch"], str(staging))
        info = staging / "objects" / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "alternates").write_text(f"{(canonical / 'objects').resolve()}\n")
        validate_canonical_update(staging, expected, new)
        require_admin_coverage(captured_descriptor, staging, expected, new)
        from git import commit_message, first_parent_commits

        commits = [
            {
                "canonical": oid,
                "change_id": parse_change_id(commit_message(staging, oid)),
            }
            for oid in first_parent_commits(staging, expected, new)
        ]
        from artifacts import build_canonical_pack

        pack = build_canonical_pack(staging, expected, new, clean_env())
        pack_ref = pack.reference("packs/canonical")
        payload = {
            "principal_id": principal_id,
            "descriptor_id": captured_descriptor["id"],
            "canonical_ref": canonical_ref,
            "canonical_old": expected,
            "canonical_new": new,
            "canonical_pack": pack_ref,
            "commits": commits,
            "received_at": received_at,
        }

        def upload(target: Repository) -> None:
            target.put_descriptor(captured_descriptor)
            upload_pack(target, "packs/canonical", pack)
            checkpoint(root, "wal.canonical_pack_uploaded")

        return Candidate(wanted_fingerprint, payload, (upload,), pack.cleanup)

    repository.publish(build)


def list_branches(root: Path) -> list[dict]:
    """Return canonical branch inventory from durable WAL-backed truth."""
    repository = Repository.open(root)
    manifest, _version, _replay = repository.load()
    default = manifest.canonical_ref
    return [
        {
            "branch": reference.removeprefix("refs/heads/"),
            "ref": reference,
            "tip": tip,
            "default": reference == default,
        }
        for reference, tip in sorted(manifest.canonical_refs.items())
    ]


def create_branch(
    root: Path, branch: str, principal_id: str, source_branch: str | None = None
) -> dict:
    """Create a pinned canonical branch, authorized only by root-admin policy."""
    from wal import validate_branch_ref

    if not isinstance(branch, str):
        raise ForgeError("invalid branch name: expected text")
    try:
        branch_ref = validate_branch_ref(
            branch if branch.startswith("refs/") else f"refs/heads/{branch}"
        )
    except (ForgeError, ValueError, TypeError) as error:
        raise ForgeError(f"invalid branch name: {branch}") from error
    source_name = source_branch
    if source_name is None:
        state = load_state(root)
        source_name = state.get("canonical_branch")
    if not isinstance(source_name, str):
        raise ForgeError("invalid source branch: expected text")
    try:
        source_ref = validate_branch_ref(
            source_name if source_name.startswith("refs/") else f"refs/heads/{source_name}"
        )
    except (ForgeError, ValueError, TypeError) as error:
        raise ForgeError(f"invalid source branch: {source_name}") from error
    if branch_ref == source_ref:
        raise ForgeError("branch already exists", "conflict")
    repository = Repository.open(root)
    with locked(root) as state:
        if state.get("repository_state") != "ready":
            raise ForgeError("forge initialization is incomplete")
        # Capture the immutable descriptor from one locked catalog snapshot.
        descriptor = identity.effective_descriptor(
            state, principal_id, f"refs/heads/{state['canonical_branch']}"
        )
        if not capability_allowed(
            descriptor["capabilities"]["admin"], descriptor["restrictions"]["admin"], b""
        ):
            raise ForgeError("repository owner permission is required", "policy")
    manifest, _version, replay = repository.load()
    # A prior matching commit is authoritative even if the source has since
    # moved. A destination is never silently re-pointed by a retry.
    prior = [
        event
        for event in replay.records
        if event.get("kind") == "branch_create"
        and event.get("payload", {}).get("branch_ref") == branch_ref
    ]
    if prior:
        payload = prior[-1]["payload"]
        if (
            payload.get("principal_id") == principal_id
            and payload.get("descriptor_id") == descriptor["id"]
            and payload.get("source_ref") == source_ref
        ):
            return {
                "branch": branch_ref.removeprefix("refs/heads/"),
                "ref": branch_ref,
                "tip": payload["canonical_tip"],
                "created": False,
            }
        raise ForgeError("branch already exists", "conflict")
    source_tip = manifest.canonical_refs.get(source_ref)
    if source_tip is None:
        raise ForgeError("source branch does not exist", "conflict")
    received_at = int(time.time())
    fingerprint_payload = {
        "principal_id": principal_id, "descriptor_id": descriptor["id"],
        "source_ref": source_ref, "branch_ref": branch_ref, "canonical_tip": source_tip,
    }
    fingerprint = record_fingerprint(
        repository.repository_id, "branch_create", fingerprint_payload
    )

    def build(current_manifest, current_replay):
        existing = current_replay.events.get(event_id(fingerprint))
        if existing is not None:
            return AlreadyCommitted(existing["seq"])
        if current_manifest.canonical_refs.get(source_ref) != source_tip:
            raise ForgeError("source branch changed before branch creation", "conflict")
        if branch_ref in current_manifest.canonical_refs:
            raise ForgeError("branch already exists", "conflict")
        payload = {**fingerprint_payload, "received_at": received_at}
        return Candidate(
            fingerprint,
            payload,
            (lambda target: target.put_descriptor(descriptor),),
        )

    repository.publish(build)
    return {
        "branch": branch_ref.removeprefix("refs/heads/"),
        "ref": branch_ref,
        "tip": source_tip,
        "created": True,
    }


def transition_principal(root: Path, principal_id: str) -> None:
    """Publish one epoch transition for every active view of a principal."""
    with locked(root) as state:
        if state.get("repository_state") != "ready":
            raise ForgeError("forge initialization is incomplete")
        identity.validate_identifier(principal_id)
        if principal_id not in state["principals"]:
            raise ForgeError(f"unknown principal: {principal_id}")
        fresh = identity.effective_descriptor(
            state, principal_id, f"refs/heads/{state['canonical_branch']}"
        )
        save_state(root, state)
    repository = Repository.open(root)
    repository.put_descriptor(fresh)

    def build(manifest, replay):
        active = sorted(
            (view_id, view)
            for view_id, view in replay.active_views.items()
            if view["principal_id"] == principal_id
        )
        if not active:
            raise ForgeError("principal has no views to transition")
        if all(view["descriptor_id"] == fresh["id"] for _name, view in active):
            return AlreadyCommitted(manifest.head_seq)
        payload = {
            "principal_id": principal_id,
            "canonical_frontier": manifest.canonical_tip,
            "views": [
                {
                    "view_id": name,
                    "old_descriptor_id": view["descriptor_id"],
                    "old_generation": view["descriptor_generation"],
                    "old_projection_epoch": view["projection_epoch"],
                    "new_descriptor_id": fresh["id"],
                    "new_generation": view["descriptor_generation"] + 1,
                    "new_projection_epoch": view["projection_epoch"] + 1,
                }
                for name, view in active
            ],
        }
        return Candidate(
            record_fingerprint(repository.repository_id, "policy_transition", payload), payload
        )

    repository.publish(build)
    manifest, _version, replay = repository.load()
    for name, view in sorted(replay.active_views.items()):
        if view["principal_id"] == principal_id:
            materialized = ensure_view_materialized(root, name, manifest, replay, repository)
            ensure_intake_materialized(root, materialized, manifest)


def _privacy_role_prefix(principal_id: str) -> str:
    principal_hash = hashlib.sha256(principal_id.encode()).hexdigest()[:12]
    return f"privacy-{principal_hash}-"


def managed_private_paths(state: dict, principal_id: str) -> list[str]:
    """Return paths hidden by the UI-managed direct role for a principal."""
    prefix = _privacy_role_prefix(principal_id)
    result: set[str] = set()
    for binding in state.get("role_bindings", {}).values():
        if (
            binding.get("subject_kind") != "principal"
            or binding.get("subject_id") != principal_id
            or not str(binding.get("role_id", "")).startswith(prefix)
        ):
            continue
        role = state.get("roles", {}).get(binding["role_id"])
        if role is None:
            raise ForgeError("privacy binding references an unknown role", "integrity")
        result.update(role.get("managed_private_paths", role["deny"]["read"]))
    return sorted(result)


def _replace_privacy_role(state: dict, principal_id: str, paths: list[str]) -> None:
    prefix = _privacy_role_prefix(principal_id)
    bindings = state["role_bindings"]
    removed_role_ids: set[str] = set()
    for binding_id, binding in list(bindings.items()):
        if (
            binding.get("subject_kind") == "principal"
            and binding.get("subject_id") == principal_id
            and str(binding.get("role_id", "")).startswith(prefix)
        ):
            removed_role_ids.add(str(binding.get("role_id", "")))
            del bindings[binding_id]
    # Drop superseded privacy roles that are no longer referenced, so
    # repeated edits do not accumulate orphans in state["roles"].
    if removed_role_ids:
        still_referenced = {
            str(binding.get("role_id", "")) for binding in bindings.values()
        }
        for role_id in removed_role_ids:
            if role_id not in still_referenced:
                state.get("roles", {}).pop(role_id, None)
    if not paths:
        return
    encoded = json.dumps(paths, separators=(",", ":"), ensure_ascii=True).encode()
    role_id = prefix + hashlib.sha256(encoded).hexdigest()[:32]
    if role_id not in state["roles"]:
        allow = identity.empty_capability_map()
        deny = identity.empty_capability_map()
        # Equal-specificity denial wins. Mirroring the path in allow keeps this
        # a valid role while ensuring it can never expand the target's access.
        allow["read"] = list(paths)
        deny["read"] = list(paths)
        identity.add_role(state, role_id, allow, deny)
        state["roles"][role_id]["managed_private_paths"] = list(paths)
    identity.bind_role(state, role_id, "principal", principal_id)


def set_path_privacy(
    root: Path,
    actor_view_id: str,
    target_view_id: str,
    path: str,
    private: bool,
) -> list[str]:
    """Apply one owner-authorized visibility edit and transition target views.

    A root-admin descriptor is the repository ownership proof. The catalog
    edit is rolled back if the WAL authorization fence rejects publication,
    so an authorization race cannot leave a dormant unauthorized policy.
    """
    if not isinstance(private, bool):
        raise ForgeError("private must be a boolean")
    normalized = normalize_prefix(path)
    if not normalized:
        raise ForgeError("repository path must be a file or folder")
    repository = Repository.open(root)
    materialize: list[str] = []
    with locked(root) as state:
        manifest, _version, replay = repository.load()
        actor = replay.active_views.get(actor_view_id)
        target = replay.active_views.get(target_view_id)
        if actor is None or target is None:
            raise ForgeError("unknown view")
        if actor["principal_id"] == target["principal_id"]:
            raise ForgeError("owners cannot hide repository paths from themselves")
        actor_descriptor = repository._descriptor(actor["descriptor_id"])
        if not capability_allowed(
            actor_descriptor["capabilities"]["admin"],
            actor_descriptor["restrictions"]["admin"],
            b"",
        ):
            raise ForgeError("repository owner permission is required", "policy")

        principal_id = target["principal_id"]
        # Browser identities attach only to user principals, so the browser
        # editor must not attach a UI-managed privacy role to a service
        # account view.
        if state["principals"].get(principal_id, {}).get("kind") != "user":
            raise ForgeError(
                "only user principals have browser-managed privacy", "policy"
            )

        canonical = ensure_canonical_materialized(root, manifest, repository)
        treeish = f"{manifest.canonical_tip}:{normalized}"
        if git(canonical, "cat-file", "-e", treeish, check=False).returncode:
            raise ForgeError("repository path does not exist")

        paths = set(managed_private_paths(state, principal_id))
        if private:
            paths.add(normalized)
        else:
            paths.discard(normalized)
        desired_paths = sorted(paths)
        # Do not short-circuit when the catalog already matches the request.
        # The catalog can diverge from the committed WAL (see the rollback
        # policy below), and a catalog-only comparison would answer "already
        # applied" for an edit that never reached any active view. Idempotency
        # is resolved against committed truth by `build` below, which compares
        # the freshly resolved descriptor against each active view's bound
        # descriptor and returns AlreadyCommitted only when they truly match.

        old_roles = copy.deepcopy(state["roles"])
        old_bindings = copy.deepcopy(state["role_bindings"])
        old_descriptor_ids = set(state["view_descriptors"])
        _replace_privacy_role(state, principal_id, desired_paths)
        fresh = identity.effective_descriptor(
            state, principal_id, f"refs/heads/{state['canonical_branch']}"
        )
        save_state(root, state)
        actor_descriptor_id = actor["descriptor_id"]

        def build(current_manifest, current_replay):
            current_actor = current_replay.active_views.get(actor_view_id)
            active = sorted(
                (view_id, view)
                for view_id, view in current_replay.active_views.items()
                if view["principal_id"] == principal_id
            )
            if (
                current_actor is None
                or current_actor["principal_id"] != actor["principal_id"]
                or current_actor["descriptor_id"] != actor_descriptor_id
            ):
                raise ForgeError("repository ownership changed during permission update", "conflict")
            if current_manifest.canonical_tip != manifest.canonical_tip:
                raise ForgeError("repository changed during permission update", "conflict")
            if not active:
                raise ForgeError("target principal has no active views")
            if all(view["descriptor_id"] == fresh["id"] for _name, view in active):
                return AlreadyCommitted(current_manifest.head_seq)
            payload = {
                "principal_id": principal_id,
                "canonical_frontier": current_manifest.canonical_tip,
                "views": [
                    {
                        "view_id": name,
                        "old_descriptor_id": view["descriptor_id"],
                        "old_generation": view["descriptor_generation"],
                        "old_projection_epoch": view["projection_epoch"],
                        "new_descriptor_id": fresh["id"],
                        "new_generation": view["descriptor_generation"] + 1,
                        "new_projection_epoch": view["projection_epoch"] + 1,
                    }
                    for name, view in active
                ],
            }
            return Candidate(
                record_fingerprint(repository.repository_id, "policy_transition", payload),
                payload,
            )

        try:
            repository.put_descriptor(fresh)
            repository.publish(build)
        except ForgeError as error:
            # Roll back every failure raised before the manifest CAS
            # linearization point. `limits` covers admission and budget
            # refusals (WAL sequence exhaustion, git subprocess timeouts),
            # which are raised before anything is uploaded or committed, so
            # discarding the catalog edit is definitive rather than ambiguous.
            # Only `storage` is retained: a storage fault can have an ambiguous
            # commit outcome, where keeping the desired catalog is safer.
            if error.category in {"policy", "conflict", "limits"}:
                state["roles"] = old_roles
                state["role_bindings"] = old_bindings
                if fresh["id"] not in old_descriptor_ids:
                    state["view_descriptors"].pop(fresh["id"], None)
                save_state(root, state)
            raise
        manifest, _version, replay = repository.load()
        from state import recover

        recover(root, state, pinned=(manifest, _version, replay), repository=repository)
        save_state(root, state)
        audit_event(
            root,
            "permission.update",
            f"{actor['principal_id']}/{principal_id}",
            "private=" + str(private).lower()
            + ";path_sha256=" + hashlib.sha256(os.fsencode(normalized)).hexdigest(),
        )
        materialize = [
            name
            for name, view in sorted(replay.active_views.items())
            if view["principal_id"] == principal_id
        ]

    for name in materialize:
        materialized = ensure_view_materialized(root, name, manifest, replay, repository)
        ensure_intake_materialized(root, materialized, manifest)
    return desired_paths
