"""Forge administration and view-synchronization use cases."""

from __future__ import annotations

import os
import re
import shutil
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
from state import (
    is_ancestor,
    load_state,
    locked,
    save_state,
)
from validation import capability_allowed, validate_view_name
from validation import parse_change_id
from wal import event_id, record_fingerprint


def _object_store_path(root: Path, value: str) -> Path:
    if value == "filesystem":
        return root / "object-store"
    if not value.startswith("filesystem:") or not value[len("filesystem:") :]:
        raise ForgeError("object store must be filesystem or filesystem:PATH")
    path = Path(value[len("filesystem:") :])
    return path if path.is_absolute() else root / path


def init_forge(
    root: Path,
    source: Path,
    branch: str,
    database: str | None = None,
    object_store: str = "filesystem",
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
        else initialize_store(root, database)
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
    object_store_path = _object_store_path(root, object_store)
    if (root / "repository.json").exists():
        repository_id = Repository.open(root).repository_id
    else:
        repository_id = state.get("repository_id") or uuid.uuid4().hex
    write_repository_config(root, repository_id, branch, object_store_path)
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
    repository = Repository.open(root)
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
            run("git", "clone", "--bare", "--no-local", str(source), str(bootstrap))
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
        run("git", "init", "--bare", "-q", "-b", branch, str(staging))
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
) -> None:
    _catalog_operation(root, lambda state: identity.add_role(state, role_id, allow, deny))


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
        payload = {
            "view_id": name,
            **desired,
            "canonical_frontier": manifest.canonical_tip,
        }
        return Candidate(
            record_fingerprint(repository.repository_id, "view_create", payload), payload
        )

    repository.publish(build)
    manifest, _version, replay = repository.load()
    materialized = ensure_view_materialized(root, name, manifest, replay, repository)
    ensure_intake_materialized(root, materialized)


def sync_view(root: Path, name: str) -> None:
    state = load_state(root)
    if state.get("repository_state") != "ready":
        raise ForgeError("forge initialization is incomplete")
    repository = Repository.open(root)
    manifest, _version, replay = repository.load()
    materialized = ensure_view_materialized(root, name, manifest, replay, repository)
    ensure_intake_materialized(root, materialized)


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


def require_admin_coverage(
    state: dict, principal_id: str, canonical: Path, expected: str, new: str
) -> None:
    """Require the acting principal's descriptor to hold admin over every change."""
    canonical_ref = f"refs/heads/{state['canonical_branch']}"
    descriptor = identity.effective_descriptor(state, principal_id, canonical_ref)
    allow = descriptor["capabilities"]["admin"]
    restrictions = descriptor["restrictions"]["admin"]
    for path in sorted(_changed_paths_between(canonical, expected, new)):
        if not capability_allowed(allow, restrictions, path):
            raise ForgeError(
                "acting principal lacks admin coverage for the canonical update"
            )


def update_canonical_ref(root: Path, expected: str, new: str, principal_id: str) -> None:
    """Publish an administrative fast-forward from disposable staging input."""
    state = load_state(root)
    if state.get("repository_state") != "ready":
        raise ForgeError("forge initialization is incomplete")
    repository = Repository.open(root)
    canonical_ref = f"refs/heads/{state['canonical_branch']}"
    fingerprint_payload = {
        "principal_id": principal_id,
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
        require_admin_coverage(state, principal_id, staging, expected, new)
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
            "canonical_ref": canonical_ref,
            "canonical_old": expected,
            "canonical_new": new,
            "canonical_pack": pack_ref,
            "commits": commits,
        }

        def upload(target: Repository) -> None:
            upload_pack(target, "packs/canonical", pack)
            checkpoint(root, "wal.canonical_pack_uploaded")

        return Candidate(wanted_fingerprint, payload, (upload,), pack.cleanup)

    repository.publish(build)


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
            ensure_intake_materialized(root, materialized)
