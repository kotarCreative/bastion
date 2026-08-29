"""Disposable Git cache generations reconstructed from a manifest/WAL chain."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from artifacts import install_pack
from errors import ForgeError
from git import (
    author_environment,
    clean_env,
    git,
    has_exact_object_closure,
    make_commit,
    ref,
    run,
    tree_oid,
)
from layout import (
    ZERO_OID,
    canonical_repo,
    intake_repo,
    materialization_lock,
    materializations_dir,
    view_repo,
)
from projection import project_history, project_tree, synthetic_commit
from repository import Repository
from wal import Manifest, ReplayState, canonical_json


REJECT_ALL_HOOK = (
    "#!/bin/sh\n"
    "echo 'bastion: pushes are accepted only through the authenticated intake boundary' >&2\n"
    "exit 1\n"
)

_EXPECTED_VIEW_CURSORS: dict[tuple[str, str], dict] = {}


@dataclass(frozen=True)
class MaterializedView:
    path: Path
    cursor: dict

    @property
    def projected_tip(self) -> str:
        return self.cursor["projected_tip"]


@contextmanager
def _flock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_cursor(path: Path, cursor: dict) -> None:
    target = path / "bastion-cursor.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(canonical_json(cursor))
    with temporary.open("rb") as output:
        os.fsync(output.fileno())
    os.replace(temporary, target)
    _fsync_directory(path)


def _write_executable(path: Path, contents: str) -> None:
    try:
        if path.read_text() == contents and path.stat().st_mode & 0o777 == 0o755:
            return
    except OSError:
        pass
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(contents)
        temporary.chmod(0o755)
        with temporary.open("rb") as output:
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_cursor(path: Path) -> dict | None:
    try:
        raw = (path / "bastion-cursor.json").read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    return value if canonical_json(value) == raw else None


def _resolved_symlink(path: Path) -> Path | None:
    if not path.is_symlink():
        return None
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def _activate(root: Path, link: Path, generation: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
    os.symlink(str(generation), temporary)
    try:
        os.replace(temporary, link)
        _fsync_directory(link.parent)
        if link.parent != root:
            _fsync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def _branch(reference: str) -> str:
    prefix = "refs/heads/"
    if not reference.startswith(prefix):
        raise ForgeError("materialization requires a branch ref", "integrity")
    return reference[len(prefix) :]


def _uses_repository_hooks(path: Path) -> bool:
    configured = git(path, "config", "--get-all", "core.hooksPath", check=False)
    return configured.returncode == 0 and configured.stdout.decode().splitlines() == [
        "hooks"
    ]


def initialize_view_generation(path: Path, branch: str) -> None:
    run("git", "init", "--bare", "-q", "-b", branch, str(path), env=clean_env())
    for key, value in (
        ("receive.denyDeletes", "true"),
        ("receive.denyNonFastForwards", "true"),
        ("uploadpack.allowAnySHA1InWant", "false"),
        ("uploadpack.allowReachableSHA1InWant", "false"),
        ("uploadpack.allowTipSHA1InWant", "false"),
        ("core.logAllRefUpdates", "false"),
        ("core.hooksPath", "hooks"),
        ("extensions.worktreeConfig", "false"),
    ):
        git(path, "config", key, value)
    git(path, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    hooks = path / "hooks"
    _write_executable(hooks / "pre-receive", REJECT_ALL_HOOK)
    for unsafe in (
        path / "objects" / "info" / "alternates",
        path / "info" / "grafts",
        path / "shallow",
    ):
        unsafe.unlink(missing_ok=True)


def initialize_intake(path: Path, active_generation: Path, branch: str) -> Path:
    """Create one receive quarantine repository with exactly one alternate."""
    run("git", "init", "--bare", "-q", "-b", branch, str(path), env=clean_env())
    for key, value in (
        ("receive.denyDeletes", "true"),
        ("receive.denyNonFastForwards", "true"),
        ("receive.maxInputSize", os.environ.get("BASTION_MAX_PACK_BYTES", str(256 * 1024 * 1024))),
        ("core.logAllRefUpdates", "false"),
        ("core.hooksPath", "hooks"),
        ("extensions.worktreeConfig", "false"),
    ):
        git(path, "config", key, value)
    info = path / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "alternates").write_text(f"{(active_generation / 'objects').resolve()}\n")
    return path


def _install_intake_hooks(root: Path, view_id: str, intake: Path) -> None:
    entrypoint = Path(__file__).resolve().parent.parent / "forge.py"
    python = shlex.quote(sys.executable)
    command = shlex.quote(str(entrypoint))
    root_arg = shlex.quote(str(root.resolve()))
    view_arg = shlex.quote(view_id)
    target = intake / "hooks" / "pre-receive"
    contents = (
        "#!/bin/sh\n"
        "BASTION_INTAKE_REPO=$(pwd -P) || exit 1\n"
        "export BASTION_INTAKE_REPO\n"
        f"exec {python} {command} pre-receive {root_arg} {view_arg}\n"
    )
    _write_executable(target, contents)


def _canonical_cursor(manifest: Manifest) -> dict:
    return {
        "applied_seq": manifest.head_seq,
        "head_record_key": manifest.wal_head.key,
        "head_publication_id": manifest.wal_head.publication_id,
        "canonical_ref": manifest.canonical_ref,
        "canonical_tip": manifest.canonical_tip,
    }


def _valid_canonical(path: Path, manifest: Manifest) -> bool:
    cursor = _read_cursor(path)
    if cursor != _canonical_cursor(manifest):
        return False
    if ref(path, manifest.canonical_ref) != manifest.canonical_tip or not _uses_repository_hooks(path):
        return False
    hook = path / "hooks" / "pre-receive"
    try:
        if hook.read_text() != REJECT_ALL_HOOK or hook.stat().st_mode & 0o777 != 0o755:
            return False
    except OSError:
        return False
    return git(path, "fsck", "--full", "--strict", check=False).returncode == 0


def ensure_canonical_materialized(
    root: Path, manifest: Manifest, repository: Repository | None = None
) -> Path:
    repository = repository or Repository.open(root)
    link = canonical_repo(root)
    with _flock(materialization_lock(root)):
        active = _resolved_symlink(link)
        if active is not None and _valid_canonical(active, manifest):
            return active
        generation = (
            materializations_dir(root)
            / "canonical"
            / f"{manifest.head_seq}-{uuid.uuid4().hex}.git"
        )
        generation.parent.mkdir(parents=True, exist_ok=True)
        initialize_view_generation(generation, _branch(manifest.canonical_ref))
        try:
            for pack in manifest.canonical_packs:
                install_pack(repository, pack, generation / "objects")
            git(generation, "update-ref", manifest.canonical_ref, manifest.canonical_tip)
            git(generation, "symbolic-ref", "HEAD", manifest.canonical_ref)
            if git(generation, "fsck", "--full", "--strict", check=False).returncode:
                raise ForgeError("canonical materialization failed Git fsck", "integrity")
            _write_cursor(generation, _canonical_cursor(manifest))
            _activate(root, link, generation)
            return generation
        except Exception:
            shutil.rmtree(generation, ignore_errors=True)
            raise


def _view_cursor(
    manifest: Manifest, view_id: str, active: dict, projected_tip: str
) -> dict:
    return {
        "view_id": view_id,
        "applied_seq": manifest.head_seq,
        "head_record_key": manifest.wal_head.key,
        "head_publication_id": manifest.wal_head.publication_id,
        "projection_epoch": active["projection_epoch"],
        "descriptor_id": active["descriptor_id"],
        "descriptor_generation": active["descriptor_generation"],
        "canonical_frontier": active["canonical_frontier"],
        "projected_tip": projected_tip,
    }


def _valid_view(path: Path, cursor: dict, projected_ref: str) -> bool:
    if _read_cursor(path) != cursor or ref(path, projected_ref) != cursor["projected_tip"]:
        return False
    for unsafe in (
        path / "objects" / "info" / "alternates",
        path / "info" / "grafts",
        path / "shallow",
    ):
        if unsafe.exists():
            return False
    if not _uses_repository_hooks(path):
        return False
    refs = git(path, "for-each-ref", "--format=%(refname)", check=False)
    if refs.returncode or refs.stdout.decode().splitlines() != [projected_ref]:
        return False
    hook = path / "hooks" / "pre-receive"
    try:
        if hook.read_text() != REJECT_ALL_HOOK or hook.stat().st_mode & 0o777 != 0o755:
            return False
    except OSError:
        return False
    return (
        git(path, "fsck", "--full", "--strict", check=False).returncode == 0
        and has_exact_object_closure(path)
    )


def _record_at(state: ReplayState, seq: int) -> dict:
    try:
        return state.records[seq - 1]
    except IndexError as error:
        raise ForgeError("active view epoch has no WAL start record", "integrity") from error


def _apply_canonical_commits(
    canonical: Path,
    destination: Path,
    descriptor: dict,
    commits: list[dict],
    projected_tip: str,
) -> str:
    previous_tree = tree_oid(destination, projected_tip)
    for item in commits:
        canonical_oid = item["canonical"]
        projected_tree = project_tree(canonical, destination, canonical_oid, descriptor)
        if projected_tree != previous_tree:
            projected_tip = synthetic_commit(
                canonical, destination, canonical_oid, projected_tree, projected_tip, item.get("change_id")
            )
            previous_tree = projected_tree
    return projected_tip


def ensure_view_materialized(
    root: Path,
    view_id: str,
    manifest: Manifest,
    replay_state: ReplayState,
    repository: Repository | None = None,
) -> MaterializedView:
    repository = repository or Repository.open(root)
    active = replay_state.active_views.get(view_id)
    if active is None:
        raise ForgeError("view is not active in the committed WAL", "policy")
    cache_key = (str(root.resolve()), view_id)
    link = view_repo(root, view_id)
    with _flock(materialization_lock(root, view_id)):
        cached_cursor = _EXPECTED_VIEW_CURSORS.get(cache_key)
        expected_cursor = None
        if cached_cursor is not None and cached_cursor == _view_cursor(
            manifest, view_id, active, cached_cursor["projected_tip"]
        ):
            expected_cursor = dict(cached_cursor)
        active_path = _resolved_symlink(link)
        if (
            active_path is not None
            and expected_cursor is not None
            and _valid_view(active_path, expected_cursor, active["projected_ref"])
        ):
            return MaterializedView(active_path, expected_cursor)
        canonical = ensure_canonical_materialized(root, manifest, repository)
        generation = (
            materializations_dir(root)
            / "views"
            / view_id
            / str(active["projection_epoch"])
            / f"{manifest.head_seq}-{uuid.uuid4().hex}.git"
        )
        generation.parent.mkdir(parents=True, exist_ok=True)
        branch = _branch(active["projected_ref"])
        initialize_view_generation(generation, branch)
        try:
            start = _record_at(replay_state, active["epoch_start_seq"])
            descriptor = repository._descriptor(active["descriptor_id"])
            projected_tip = ZERO_OID
            if start["kind"] == "view_create":
                projected_tip, _frontier, _anchors = project_history(
                    canonical, generation, ZERO_OID,
                    start["payload"]["canonical_frontier"], ZERO_OID, descriptor
                )
            elif start["kind"] == "policy_transition":
                frontier = start["payload"]["canonical_frontier"]
                projected_tree = project_tree(canonical, generation, frontier, descriptor)
                projected_tip = make_commit(
                    generation,
                    projected_tree,
                    ZERO_OID,
                    b"projected policy epoch\n",
                    author_environment(canonical, frontier, projected=True),
                )
            else:
                raise ForgeError("active view epoch starts with an invalid WAL event", "integrity")
            for record in replay_state.records[active["epoch_start_seq"] :]:
                payload = record["payload"]
                if record["kind"] == "submission":
                    if payload["view_id"] == view_id and (
                        payload["projection_epoch"] == active["projection_epoch"]
                        and payload["descriptor_id"] == active["descriptor_id"]
                    ):
                        if projected_tip != payload["projected_old"]:
                            raise ForgeError("source evidence does not extend materialized view", "integrity")
                        install_pack(repository, payload["submission_pack"], generation / "objects")
                        projected_tip = payload["projected_new"]
                    else:
                        projected_tip = _apply_canonical_commits(
                            canonical, generation, descriptor, payload["mappings"], projected_tip
                        )
                elif record["kind"] == "canonical_update":
                    projected_tip = _apply_canonical_commits(
                        canonical, generation, descriptor, payload["commits"], projected_tip
                    )
                elif record["kind"] == "policy_transition" and any(
                    item["view_id"] == view_id for item in payload["views"]
                ):
                    raise ForgeError("active view epoch continued after another policy transition", "integrity")
            if projected_tip == ZERO_OID:
                raise ForgeError("view materialization produced no projected commit", "integrity")
            git(generation, "update-ref", active["projected_ref"], projected_tip)
            cursor = _view_cursor(manifest, view_id, active, projected_tip)
            if git(generation, "fsck", "--full", "--strict", check=False).returncode:
                raise ForgeError("view materialization failed Git fsck", "integrity")
            _write_cursor(generation, cursor)
            _EXPECTED_VIEW_CURSORS[cache_key] = dict(cursor)
            _activate(root, link, generation)
            from failpoints import checkpoint

            checkpoint(root, "wal.secondary_materialized")
            return MaterializedView(generation, dict(cursor))
        except Exception:
            shutil.rmtree(generation, ignore_errors=True)
            raise


def ensure_intake_materialized(root: Path, materialized_view: MaterializedView) -> Path:
    view_id = materialized_view.cursor["view_id"]
    link = intake_repo(root, view_id)
    # The branch is encoded by the only advertised ref in the view generation.
    refs = git(materialized_view.path, "for-each-ref", "--format=%(refname)").stdout.decode().splitlines()
    if len(refs) != 1:
        raise ForgeError("materialized view has an invalid ref inventory", "integrity")
    branch = _branch(refs[0])
    projected_ref = refs[0]
    target_tip = ref(materialized_view.path, projected_ref)
    if target_tip == ZERO_OID:
        raise ForgeError("materialized view is missing its projected tip", "integrity")
    expected_alternate = f"{(materialized_view.path / 'objects').resolve()}\n"
    with _flock(materialization_lock(root, view_id)):
        repository = Repository.open(root)
        manifest, _version, _replay = repository.load()
        cursor = materialized_view.cursor
        if (
            cursor["applied_seq"] != manifest.head_seq
            or cursor["head_record_key"] != manifest.wal_head.key
            or cursor["head_publication_id"] != manifest.wal_head.publication_id
        ):
            raise ForgeError("materialized view was superseded", "conflict")
        active = _resolved_symlink(link)
        if active is not None and _read_cursor(active) == cursor:
            alternate = active / "objects" / "info" / "alternates"
            advertised = git(active, "for-each-ref", "--format=%(refname)", check=False)
            if (
                alternate.is_file()
                and alternate.read_text() == expected_alternate
                and ref(active, projected_ref) == target_tip
                and _uses_repository_hooks(active)
                and not advertised.returncode
                and advertised.stdout.decode().splitlines() == [projected_ref]
            ):
                _install_intake_hooks(root, view_id, active)
                return active
        generation = (
            materializations_dir(root)
            / "intakes"
            / view_id
            / f"{cursor['applied_seq']}-{uuid.uuid4().hex}.git"
        )
        generation.parent.mkdir(parents=True, exist_ok=True)
        try:
            initialize_intake(generation, materialized_view.path, branch)
            _install_intake_hooks(root, view_id, generation)
            git(generation, "update-ref", projected_ref, target_tip)
            _write_cursor(generation, cursor)
            _activate(root, link, generation)
            return generation
        except Exception:
            shutil.rmtree(generation, ignore_errors=True)
            raise
