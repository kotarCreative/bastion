"""Read-only global invariant checks for Bastion repositories."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import identity
from artifacts import install_pack
from errors import ForgeError
from git import (
    author_environment,
    clean_env,
    commit_metadata,
    commit_message,
    first_parent_commits,
    git,
    has_exact_object_closure,
    make_commit,
    output,
    ref,
    tree_entries,
    tree_oid,
)
from layout import ZERO_OID, view_repo
from materialization import initialize_view_generation
from projection import project_history, project_tree, synthetic_commit
from reconciliation import reconcile_commits
from store import load_forge_meta_readonly, load_principals_readonly
from validation import capability_allowed
from wal import ReplayState, canonical_json, sha256, validate_descriptor


class InvariantError(ForgeError):
    def __init__(self, code: str, subject: object, detail: str):
        super().__init__(f"invariant {code} failed for {subject}: {detail}")
        self.category = code


def _fail(code: str, subject: object, detail: str) -> None:
    raise InvariantError(code, subject, detail)


@contextlib.contextmanager
def _temp_repository(root: Path, branch: str):
    """Create a disposable bare Git repository and yield its path."""
    path = Path(tempfile.mkdtemp(prefix="bastion-check-", dir=root.parent))
    try:
        initialize_view_generation(path, branch)
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _install_canonical_packs(
    repository, replay: ReplayState, canonical: Path
) -> None:
    """Install every canonical pack referenced by the WAL into a temp repo."""
    for pack in replay.canonical_packs:
        install_pack(repository, pack, canonical / "objects")


def _require_object_type(repo: Path, oid: str, kind: str) -> None:
    result = git(repo, "cat-file", "-t", oid, check=False)
    if result.returncode or result.stdout.decode().strip() != kind:
        _fail("GRAPH", oid, f"expected {kind} object is missing or has wrong type")


def _validate_packs(repository, replay: ReplayState, manifest) -> None:
    """Verify every referenced pack object by SHA-256 and size."""
    references: list[dict] = list(manifest.canonical_packs)
    for record in replay.records:
        payload = record["payload"]
        if record["kind"] == "submission":
            references.extend((payload["canonical_pack"], payload["submission_pack"]))
        elif record["kind"] in {"canonical_update", "import"}:
            references.append(payload["canonical_pack"])
    for pack in references:
        try:
            body = repository.get(pack["pack_key"]).body
            index = repository.get(pack["idx_key"]).body
        except Exception as error:
            _fail("REPOSITORY", pack.get("pack_key", "pack"), f"referenced pack is missing: {error}")
        if (
            sha256(body) != pack["pack_sha256"]
            or len(body) != pack["pack_size"]
            or sha256(index) != pack["idx_sha256"]
            or len(index) != pack["idx_size"]
        ):
            _fail("REPOSITORY", pack["pack_key"], "referenced pack hash or size differs")


def _validate_record_graph(
    record: dict, canonical: Path, submission: Path | None = None
) -> None:
    """Verify graph-dependent endpoints in installed temporary repositories."""
    payload = record["payload"]
    kind = record["kind"]
    if kind == "import":
        _require_object_type(canonical, payload["canonical_tip"], "commit")
    elif kind == "submission":
        _require_object_type(canonical, payload["canonical_new"], "commit")
        if submission is None:
            _fail("GRAPH", record["seq"], "submission record lacks submission evidence repo")
        _require_object_type(submission, payload["projected_new"], "commit")
        canonical_range = first_parent_commits(
            canonical, payload["canonical_old"], payload["canonical_new"]
        )
        if canonical_range != [item["canonical"] for item in payload["mappings"]]:
            _fail("GRAPH", record["seq"], "submission canonical mappings are not an exact first-parent range")
        submitted_range = output(
            submission, "rev-list", "--reverse", f"{payload['projected_old']}..{payload['projected_new']}"
        ).splitlines()
        if submitted_range != [item["submitted"] for item in payload["mappings"]]:
            _fail("GRAPH", record["seq"], "submission evidence mappings are not an exact first-parent range")
    elif kind == "canonical_update":
        _require_object_type(canonical, payload["canonical_new"], "commit")
        canonical_range = first_parent_commits(
            canonical, payload["canonical_old"], payload["canonical_new"]
        )
        if canonical_range != [item["canonical"] for item in payload["commits"]]:
            _fail("GRAPH", record["seq"], "canonical update commits are not an exact first-parent range")


def _descriptor_for_record(
    repository, record: dict, descriptor_id: str | None = None
) -> dict:
    """Load and return the immutable descriptor captured by a record."""
    payload = record["payload"]
    descriptor_id = descriptor_id or payload.get("descriptor_id")
    if descriptor_id is None:
        _fail("AUTHORIZATION", record["seq"], "record is missing a descriptor reference")
    key = f"descriptors/{descriptor_id}.json"
    try:
        body = repository.get(key).body
        value = json.loads(body)
    except Exception as error:
        _fail("AUTHORIZATION", record["seq"], f"cannot load descriptor {descriptor_id}: {error}")

    if canonical_json(value) != body:
        _fail("AUTHORIZATION", descriptor_id, "descriptor object is not canonical JSON")
    descriptor = validate_descriptor(value)
    if descriptor["id"] != descriptor_id:
        _fail("AUTHORIZATION", descriptor_id, "descriptor object ID differs from its key")
    return descriptor


def _check_descriptor_provenance(
    record: dict, descriptor: dict, principal_id: str, ref_name: str
) -> None:
    if descriptor["principal_id"] != principal_id:
        _fail("AUTHORIZATION", record["seq"], "descriptor principal does not match record")
    if descriptor["canonical_ref"] != ref_name:
        _fail("AUTHORIZATION", record["seq"], "descriptor ref does not match record")


def _validate_view_create(
    record: dict, descriptor: dict, canonical_ref: str
) -> None:
    payload = record["payload"]
    if payload["descriptor_generation"] != 1 or payload["projection_epoch"] != 1:
        _fail("AUTHORIZATION", record["seq"], "view creation generation or epoch is not 1")
    _check_descriptor_provenance(
        record, descriptor, payload["principal_id"], canonical_ref
    )


def _validate_policy_transition(
    record: dict, running_views: dict[str, dict], repository, canonical_ref: str
) -> None:
    payload = record["payload"]
    expected = sorted(
        view_id for view_id, view in running_views.items()
        if view["principal_id"] == payload["principal_id"]
    )
    if [item["view_id"] for item in payload["views"]] != expected:
        _fail("AUTHORIZATION", record["seq"], "policy transition does not cover exactly the active principal views")
    for item in payload["views"]:
        active = running_views[item["view_id"]]
        if (
            active["descriptor_id"] != item["old_descriptor_id"]
            or active["descriptor_generation"] != item["old_generation"]
            or active["projection_epoch"] != item["old_projection_epoch"]
            or item["new_generation"] != item["old_generation"] + 1
            or item["new_projection_epoch"] != item["old_projection_epoch"] + 1
        ):
            _fail("AUTHORIZATION", record["seq"], "policy transition has an invalid view generation")
        new_descriptor = _descriptor_for_record(repository, record, item["new_descriptor_id"])
        _check_descriptor_provenance(
            record, new_descriptor, active["principal_id"], canonical_ref
        )
        active["descriptor_id"] = item["new_descriptor_id"]
        active["descriptor_generation"] = item["new_generation"]
        active["projection_epoch"] = item["new_projection_epoch"]


def _validate_administrative_authorization(
    record: dict, descriptor: dict, canonical: Path
) -> None:
    """Recompute per-commit admin coverage for a canonical_update."""
    payload = record["payload"]
    allow = descriptor["capabilities"]["admin"]
    restrictions = descriptor["restrictions"]["admin"]
    previous = payload["canonical_old"]
    for oid in first_parent_commits(canonical, previous, payload["canonical_new"]):
        before = tree_entries(canonical, previous)
        after = tree_entries(canonical, oid)
        for path in before.keys() | after.keys():
            if before.get(path) != after.get(path):
                if not capability_allowed(allow, restrictions, path):
                    _fail("AUTHORIZATION", record["seq"], "administrative transition lacks coverage")
        previous = oid


def _install_submission_pack(
    repository, payload: dict, submission: Path
) -> None:
    install_pack(repository, payload["submission_pack"], submission / "objects")


def _candidate_env(canonical: Path, object_dir: Path) -> dict[str, str]:
    env = clean_env()
    env["GIT_OBJECT_DIRECTORY"] = str(object_dir)
    env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(canonical / "objects")
    return env


def _validate_submission_authorization(
    record: dict, descriptor: dict, canonical: Path, submission: Path
) -> None:
    """Reconcile the submission against the captured descriptor."""
    payload = record["payload"]
    payload_mappings = payload["mappings"]
    commits = [
        item["submitted"] for item in payload_mappings
    ]
    object_dir = Path(tempfile.mkdtemp(prefix="check-objects-", dir=canonical.parent))
    try:
        candidate_env = _candidate_env(canonical, object_dir)
        submitted_env = clean_env_for_repo(submission)
        transition = reconcile_commits(
            canonical,
            submission,
            commits,
            payload["canonical_old"],
            payload["projected_old"],
            descriptor,
            submitted_env,
            candidate_env,
        )
    finally:
        shutil.rmtree(object_dir, ignore_errors=True)
    if transition.canonical_new != payload["canonical_new"]:
        _fail("AUTHORIZATION", record["seq"], "reconciled canonical tip differs from record")
    # Frozen snapshot payloads surface tuples of mapping proxies; compare
    # normalized plain containers so the reference-equivalence check is
    # structural, not container-type-sensitive.
    if [dict(item) for item in payload_mappings] != [
        dict(item) for item in transition.mappings
    ]:
        _fail("AUTHORIZATION", record["seq"], "reconciled mappings differ from record")


def clean_env_for_repo(repo: Path | None = None) -> dict[str, str]:
    env = clean_env()
    if repo is not None:
        env["GIT_DIR"] = str(repo)
    return env


def _validate_submission_metadata(
    record: dict, principals: dict[str, dict], submission: Path
) -> None:
    """Recheck author bindings and timestamps for every submitted commit."""
    payload = record["payload"]
    principal_id = payload["principal_id"]
    principal = principals.get(principal_id)
    if principal is None:
        _fail("METADATA", record["seq"], "submission principal is missing from catalog")
    declared = principal.get("emails") or []
    received_at = payload["received_at"]
    deadline = received_at + identity.TIMESTAMP_SKEW_SECONDS
    env = clean_env_for_repo(submission)
    previous = payload["projected_old"]
    for mapping in payload["mappings"]:
        submitted_oid = mapping["submitted"]
        # Verify linear first-parent chain as part of metadata ordering.
        parents = output(submission, "show", "-s", "--format=%P", submitted_oid, env=env).split()
        if parents != [previous]:
            _fail("METADATA", record["seq"], "submitted commit chain is not linear")
        email, author_ts, committer_ts = commit_metadata(submission, submitted_oid, env)
        if declared:
            address = email.decode(errors="surrogateescape").strip().lower()
            if address not in declared:
                _fail("METADATA", record["seq"], "submission is authored by an undeclared identity")
        if author_ts > deadline or committer_ts > deadline:
            _fail("METADATA", record["seq"], "submission carries a future-dated commit")
        previous = submitted_oid


def _recompute_change_claims(replay: ReplayState) -> dict[str, dict]:
    """Reconstruct Change-Id ownership from final trailers in WAL order."""
    claims: dict[str, dict] = {}
    for record in replay.records:
        payload = record["payload"]
        kind = record["kind"]
        if kind == "import":
            for item in payload["change_claims"]:
                claims[item["change_id"]] = {
                    "owner_principal_id": item["owner_principal_id"],
                    "canonical_ref": item["canonical_ref"],
                }
        elif kind == "canonical_update":
            for item in payload["commits"]:
                change_id = item["change_id"]
                if change_id and change_id not in claims:
                    claims[change_id] = {
                        "owner_principal_id": None,
                        "canonical_ref": payload["canonical_ref"],
                    }
        elif kind == "submission":
            for item in payload["mappings"]:
                change_id = item["change_id"]
                if not change_id:
                    continue
                if change_id not in claims:
                    claims[change_id] = {
                        "owner_principal_id": payload["principal_id"],
                        "canonical_ref": payload["canonical_ref"],
                    }
                elif claims[change_id]["owner_principal_id"] != payload["principal_id"]:
                    _fail("METADATA", record["seq"], "Change-Id belongs to another principal")
    return claims


def _apply_canonical_commits(
    canonical: Path,
    destination: Path,
    descriptor: dict,
    commits: list[dict],
    projected_tip: str,
) -> str:
    previous_tree = tree_oid(destination, projected_tip) if projected_tip != ZERO_OID else None
    for item in commits:
        canonical_oid = item["canonical"]
        projected_tree = project_tree(canonical, destination, canonical_oid, descriptor)
        if projected_tree != previous_tree:
            projected_tip = synthetic_commit(
                canonical, destination, canonical_oid, projected_tree, projected_tip, item.get("change_id")
            )
            previous_tree = projected_tree
    return projected_tip


def _initialize_view_repo(
    root: Path, canonical: Path, active: dict, descriptor: dict, start_record: dict
) -> Path:
    """Create a temp view repo and project its epoch start."""
    temp = Path(tempfile.mkdtemp(prefix="bastion-check-view-", dir=root.parent))
    try:
        branch = active["projected_ref"].split("/")[-1]
        initialize_view_generation(temp, branch)
        if start_record["kind"] == "view_create":
            projected_tip, _frontier, _anchors = project_history(
                canonical, temp, ZERO_OID,
                start_record["payload"]["canonical_frontier"], ZERO_OID, descriptor
            )
        elif start_record["kind"] == "policy_transition":
            frontier = start_record["payload"]["canonical_frontier"]
            projected_tree = project_tree(canonical, temp, frontier, descriptor)
            projected_tip = make_commit(
                temp,
                projected_tree,
                ZERO_OID,
                b"projected policy epoch\n",
                author_environment(canonical, frontier, projected=True),
            )
        else:
            raise InvariantError("PROJECTION", active, "active view epoch starts with an invalid WAL event")
        if projected_tip == ZERO_OID:
            raise InvariantError("PROJECTION", active, "view projection produced no commit")
        git(temp, "update-ref", active["projected_ref"], projected_tip)
        git(temp, "symbolic-ref", "HEAD", active["projected_ref"])
        return temp
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def _load_descriptor_object(repository, descriptor_id: str) -> dict:
    key = f"descriptors/{descriptor_id}.json"
    try:
        body = repository.get(key).body
        value = json.loads(body)
    except Exception as error:
        _fail("AUTHORIZATION", descriptor_id, f"cannot load descriptor: {error}")
    if canonical_json(value) != body:
        _fail("AUTHORIZATION", descriptor_id, "descriptor object is not canonical JSON")
    descriptor = validate_descriptor(value)
    if descriptor["id"] != descriptor_id:
        _fail("AUTHORIZATION", descriptor_id, "descriptor object ID differs from its key")
    return descriptor


def _advance_view_for_record(
    record: dict,
    view_id: str,
    view_state: dict,
    canonical: Path,
    repository,
) -> None:
    """Apply one record's canonical effect to a single view temp repo."""
    payload = record["payload"]
    descriptor = _load_descriptor_object(repository, view_state["descriptor_id"])
    temp = view_state["_temp_repo"]
    projected_tip = ref(temp, view_state["projected_ref"])
    if record["kind"] == "submission":
        if payload["view_id"] == view_id and (
            payload["projection_epoch"] == view_state["projection_epoch"]
            and payload["descriptor_id"] == view_state["descriptor_id"]
        ):
            if projected_tip != payload["projected_old"]:
                _fail("PROJECTION", view_id, "source evidence does not extend projected view")
            install_pack(repository, payload["submission_pack"], temp / "objects")
            projected_tip = payload["projected_new"]
        else:
            projected_tip = _apply_canonical_commits(
                canonical, temp, descriptor, payload["mappings"], projected_tip
            )
    elif record["kind"] == "canonical_update":
        projected_tip = _apply_canonical_commits(
            canonical, temp, descriptor, payload["commits"], projected_tip
        )
    elif record["kind"] == "policy_transition":
        for item in payload["views"]:
            if item["view_id"] == view_id:
                _fail("PROJECTION", view_id, "active view epoch continued after another policy transition")
    if projected_tip != ref(temp, view_state["projected_ref"]):
        git(temp, "update-ref", view_state["projected_ref"], projected_tip)


def check_invariants(root: Path) -> None:
    """Validate repository truth and its immutable object closure."""
    from repository import Repository

    # Load only immutable catalog input read-only; do not touch rebuildable rows.
    principals = load_principals_readonly(root)
    state = load_forge_meta_readonly(root)
    if state.get("repository_state") != "ready":
        _fail("REPOSITORY", "forge_meta", "forge initialization is incomplete")
    repository = Repository.open(root)
    # The independent checker never uses cached snapshots as evidence.
    manifest, _version, replay = repository.load(use_cache=False)
    if state.get("repository_id") != repository.repository_id:
        _fail("REPOSITORY", "forge_meta", "repository ID differs from repository configuration")
    if f"refs/heads/{state.get('canonical_branch')}" != manifest.canonical_ref:
        _fail("REPOSITORY", "forge_meta", "canonical branch differs from repository configuration")

    _validate_packs(repository, replay, manifest)

    view_temp_repos: dict[str, Path] = {}
    try:
        with _temp_repository(root, manifest.canonical_ref.split("/")[-1]) as canonical:
            _install_canonical_packs(repository, replay, canonical)
            for canonical_ref, tip in manifest.canonical_refs.items():
                git(canonical, "update-ref", canonical_ref, tip)
            git(canonical, "symbolic-ref", "HEAD", manifest.canonical_ref)

            running_refs: dict[str, str] = {}

            running_views: dict[str, dict] = {}
            for record in replay.records:
                payload = record["payload"]
                kind = record["kind"]
                if kind == "view_create":
                    descriptor = _descriptor_for_record(repository, record)
                    _validate_view_create(record, descriptor, manifest.canonical_ref)
                    for other in running_views.values():
                        if (
                            other["principal_id"] == payload["principal_id"]
                            and other["descriptor_id"] != payload["descriptor_id"]
                        ):
                            _fail(
                                "AUTHORIZATION",
                                record["seq"],
                                "view creation does not match the principal's committed assignment",
                            )
                    running_views[payload["view_id"]] = {
                        "principal_id": payload["principal_id"],
                        "projected_ref": payload["projected_ref"],
                        "descriptor_id": payload["descriptor_id"],
                        "descriptor_generation": payload["descriptor_generation"],
                        "projection_epoch": payload["projection_epoch"],
                        "canonical_frontier": payload["canonical_frontier"],
                    }
                    temp = _initialize_view_repo(root, canonical, running_views[payload["view_id"]], descriptor, record)
                    running_views[payload["view_id"]]["_temp_repo"] = temp
                    view_temp_repos[payload["view_id"]] = temp
                elif kind == "policy_transition":
                    _validate_policy_transition(record, running_views, repository, manifest.canonical_ref)
                    for item in payload["views"]:
                        view_id = item["view_id"]
                        active = running_views[view_id]
                        active["_temp_repo"] = view_temp_repos[view_id]
                        new_descriptor = _descriptor_for_record(repository, record, item["new_descriptor_id"])
                        temp = _initialize_view_repo(root, canonical, active, new_descriptor, record)
                        # The policy transition starts a parentless epoch at the frontier.
                        frontier = payload["canonical_frontier"]
                        descriptor = _load_descriptor_object(repository, active["descriptor_id"])
                        projected_tree = project_tree(canonical, temp, frontier, descriptor)
                        projected_tip = make_commit(
                            temp,
                            projected_tree,
                            ZERO_OID,
                            b"projected policy epoch\n",
                            author_environment(canonical, frontier, projected=True),
                        )
                        git(temp, "update-ref", active["projected_ref"], projected_tip)
                        git(temp, "symbolic-ref", "HEAD", active["projected_ref"])
                        old_temp = view_temp_repos[view_id]
                        shutil.rmtree(old_temp, ignore_errors=True)
                        view_temp_repos[view_id] = temp
                        active["_temp_repo"] = temp
                elif kind == "submission":
                    active = running_views.get(payload["view_id"])
                    if active is None:
                        _fail("AUTHORIZATION", record["seq"], "submission source view does not exist")
                    if payload["projected_ref"] != active["projected_ref"]:
                        _fail("AUTHORIZATION", record["seq"], "submission projected ref does not match active view")
                    if payload["descriptor_generation"] != active["descriptor_generation"]:
                        _fail("AUTHORIZATION", record["seq"], "submission descriptor generation does not match active view")
                    if payload["projection_epoch"] != active["projection_epoch"]:
                        _fail("AUTHORIZATION", record["seq"], "submission projection epoch does not match active view")
                    if running_refs.get(payload["canonical_ref"]) != payload["canonical_old"]:
                        _fail("GRAPH", record["seq"], "submission does not extend the independently replayed branch tip")
                    view_temp = active["_temp_repo"]
                    with _temp_repository(root, payload["projected_ref"].split("/")[-1]) as sub:
                        (sub / "objects" / "info").mkdir(parents=True, exist_ok=True)
                        (sub / "objects" / "info" / "alternates").write_text(
                            f"{(view_temp / 'objects').resolve()}\n"
                        )
                        _install_submission_pack(repository, payload, sub)
                        git(sub, "update-ref", payload["projected_ref"], payload["projected_new"])
                        _validate_record_graph(record, canonical, sub)
                        descriptor = _descriptor_for_record(repository, record)
                        _check_descriptor_provenance(
                            record, descriptor, payload["principal_id"], payload["canonical_ref"]
                        )
                        _validate_submission_authorization(record, descriptor, canonical, sub)
                        _validate_submission_metadata(record, principals, sub)
                    for view_id, active in running_views.items():
                        active["canonical_frontier"] = payload["canonical_new"]
                        _advance_view_for_record(record, view_id, active, canonical, repository)
                    running_refs[payload["canonical_ref"]] = payload["canonical_new"]
                elif kind == "canonical_update":
                    if running_refs.get(payload["canonical_ref"]) != payload["canonical_old"]:
                        _fail("GRAPH", record["seq"], "canonical update does not extend the independently replayed branch tip")
                    _validate_record_graph(record, canonical)
                    descriptor = _descriptor_for_record(repository, record)
                    _check_descriptor_provenance(
                        record, descriptor, payload["principal_id"], payload["canonical_ref"]
                    )
                    _validate_administrative_authorization(record, descriptor, canonical)
                    for view_id, active in running_views.items():
                        active["canonical_frontier"] = payload["canonical_new"]
                        _advance_view_for_record(record, view_id, active, canonical, repository)
                    running_refs[payload["canonical_ref"]] = payload["canonical_new"]
                elif kind == "import":
                    _validate_record_graph(record, canonical)
                    if running_refs:
                        _fail("GRAPH", record["seq"], "import is not the first canonical event")
                    running_refs[payload["canonical_ref"]] = payload["canonical_tip"]
                elif kind == "branch_create":
                    source = payload["source_ref"]
                    destination = payload["branch_ref"]
                    tip = payload["canonical_tip"]
                    descriptor = _descriptor_for_record(repository, record)
                    _check_descriptor_provenance(
                        record, descriptor, payload["principal_id"], manifest.canonical_ref
                    )
                    if not capability_allowed(
                        descriptor["capabilities"]["admin"],
                        descriptor["restrictions"]["admin"], b"",
                    ):
                        _fail("AUTHORIZATION", record["seq"], "branch creation requires root admin capability")
                    if running_refs.get(source) != tip:
                        _fail("GRAPH", record["seq"], "branch source does not match its pinned tip")
                    if destination in running_refs or any(
                        existing.startswith(destination + "/")
                        or destination.startswith(existing + "/")
                        for existing in running_refs
                    ):
                        _fail("AUTHORIZATION", record["seq"], "branch destination conflicts with an existing ref")
                    if not destination.startswith("refs/heads/") or git(
                        canonical, "check-ref-format", destination, check=False
                    ).returncode:
                        _fail("AUTHORIZATION", record["seq"], "branch destination is not a valid Git branch ref")
                    _require_object_type(canonical, tip, "commit")
                    running_refs[destination] = tip

            recomputed_claims = _recompute_change_claims(replay)
            if recomputed_claims != replay.change_claims:
                _fail("METADATA", "change_claims", "replayed Change-Id ownership differs from record")

            if running_refs != dict(manifest.canonical_refs):
                _fail("GRAPH", "canonical_refs", "replayed branch map differs from manifest")

            for view_id, temp in view_temp_repos.items():
                active = replay.active_views[view_id]
                refs = git(temp, "for-each-ref", "--format=%(refname)", check=False)
                if refs.returncode or refs.stdout.decode().splitlines() != [active["projected_ref"]]:
                    _fail("ISOLATION", view_id, "projected repository has unexpected refs")
                if not has_exact_object_closure(temp):
                    _fail("ISOLATION", view_id, "projected repository contains unreachable objects")
                cached = view_repo(root, view_id)
                if cached.exists():
                    if (cached.resolve() / "objects" / "info" / "alternates").exists():
                        _fail("ISOLATION", view_id, "active view has an alternate")
                    if not has_exact_object_closure(cached):
                        _fail("ISOLATION", view_id, "active view contains unreachable objects")
    finally:
        for temp in view_temp_repos.values():
            shutil.rmtree(temp, ignore_errors=True)
