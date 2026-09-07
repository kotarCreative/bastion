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
    output,
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
from projection import project_history_fast, project_tree, synthetic_commit
from repository import Repository
from wal import Manifest, ReplayState, canonical_json


REJECT_ALL_HOOK = (
    "#!/bin/sh\n"
    "echo 'bastion: pushes are accepted only through the authenticated intake boundary' >&2\n"
    "exit 1\n"
)

_EXPECTED_VIEW_CURSORS: dict[tuple[str, str], dict] = {}

# In-memory validation registrations bound to (receipt, active generation).
# Normal requests perform lightweight identity checks against the registered
# generation instead of repeating fsck/closure walks. ``forge.py sync`` and
# ``forge.py check`` bypass the fast path via ``full_validation=True``.
_VIEW_REGISTRATIONS: dict[tuple[str, str], tuple[dict, str]] = {}
_CANONICAL_REGISTRATIONS: dict[str, tuple[dict, str]] = {}

# Deterministic operation counters for bounded-hot-path verification.
_COUNTS = {
    "full_view_validations": 0,
    "light_view_checks": 0,
    "view_generations_built": 0,
    "suffix_view_rebuilds": 0,
    "full_canonical_validations": 0,
    "light_canonical_checks": 0,
    "canonical_generations_built": 0,
}


def materialization_counts() -> dict[str, int]:
    return dict(_COUNTS)


def reset_materialization_counts() -> None:
    for key in _COUNTS:
        _COUNTS[key] = 0


def clear_materialization_registrations() -> None:
    _VIEW_REGISTRATIONS.clear()
    _CANONICAL_REGISTRATIONS.clear()
    _EXPECTED_VIEW_CURSORS.clear()


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


def _intake_receive_checks(path: Path) -> bool:
    """Whether receive-side object checking stays configured on an intake."""
    result = git(path, "config", "--bool", "--get", "receive.fsckObjects", check=False)
    return not result.returncode and result.stdout.decode().strip() == "true"


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
        ("receive.fsckObjects", "true"),
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
    """Canonical content identity: the projected tip, not the WAL head.

    Canonical materialization is a pure function of ``canonical_tip``;
    unrelated WAL appends (view creation, policy transitions, hidden-only
    canonical commits) do not change canonical content and must not
    invalidate the generation.
    """
    return {
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


def _light_valid_canonical(path: Path, manifest: Manifest) -> bool:
    """Cursor/ref/hook identity checks without fsck (registered generations)."""
    cursor = _read_cursor(path)
    if cursor != _canonical_cursor(manifest):
        return False
    if ref(path, manifest.canonical_ref) != manifest.canonical_tip:
        return False
    if not _uses_repository_hooks(path):
        return False
    hook = path / "hooks" / "pre-receive"
    try:
        if hook.read_text() != REJECT_ALL_HOOK or hook.stat().st_mode & 0o777 != 0o755:
            return False
    except OSError:
        return False
    return True


def _registered_canonical_packs(registered) -> tuple[str, ...]:
    return registered[2] if registered is not None and len(registered) > 2 else ()


def ensure_canonical_materialized(
    root: Path, manifest: Manifest, repository: Repository | None = None
) -> Path:
    repository = repository or Repository.open(root)
    link = canonical_repo(root)
    cache_key = str(root.resolve())
    with _flock(materialization_lock(root)):
        active = _resolved_symlink(link)
        registered = _CANONICAL_REGISTRATIONS.get(cache_key)
        if (
            registered is not None
            and active is not None
            and registered[0] == _canonical_cursor(manifest)
            and registered[1] == str(active)
            and _light_valid_canonical(active, manifest)
        ):
            _COUNTS["light_canonical_checks"] += 1
            return active
        if active is not None:
            _COUNTS["full_canonical_validations"] += 1
            if _valid_canonical(active, manifest):
                _CANONICAL_REGISTRATIONS[cache_key] = (
                    dict(_canonical_cursor(manifest)),
                    str(active),
                    tuple(pack["pack_key"] for pack in manifest.canonical_packs),
                )
                return active
        # A changed tip builds a private candidate that links the verified
        # immutable objects of the previous generation and installs only the
        # new packs, instead of recopying the full history.
        generation = (
            materializations_dir(root)
            / "canonical"
            / f"{manifest.head_seq}-{uuid.uuid4().hex}.git"
        )
        generation.parent.mkdir(parents=True, exist_ok=True)
        initialize_view_generation(generation, _branch(manifest.canonical_ref))
        try:
            installed = _registered_canonical_packs(registered)
            installed_keys = set(installed)
            if active is not None and installed:
                _link_tree(active / "objects", generation / "objects")
                required = [
                    pack
                    for pack in manifest.canonical_packs
                    if pack["pack_key"] not in installed_keys
                ]
            else:
                required = list(manifest.canonical_packs)
            for pack in required:
                install_pack(repository, pack, generation / "objects")
            git(generation, "update-ref", manifest.canonical_ref, manifest.canonical_tip)
            git(generation, "symbolic-ref", "HEAD", manifest.canonical_ref)
            if git(generation, "fsck", "--full", "--strict", check=False).returncode:
                raise ForgeError("canonical materialization failed Git fsck", "integrity")
            _write_cursor(generation, _canonical_cursor(manifest))
            _activate(root, link, generation)
            _COUNTS["canonical_generations_built"] += 1
            installed_keys.update(pack["pack_key"] for pack in required)
            _CANONICAL_REGISTRATIONS[cache_key] = (
                dict(_canonical_cursor(manifest)),
                str(generation),
                tuple(sorted(installed_keys)),
            )
            return generation
        except Exception:
            shutil.rmtree(generation, ignore_errors=True)
            raise


def _view_cursor(view_id: str, active: dict, projected_tip: str) -> dict:
    """View content identity, independent of unrelated WAL appends.

    Reuse is decided from the descriptor assignment, projection epoch, ref,
    and projected tip.  Unrelated records (another principal's view
    creation, hidden-only canonical commits) never invalidate a proved
    generation.
    """
    return {
        "view_id": view_id,
        "descriptor_id": active["descriptor_id"],
        "descriptor_generation": active["descriptor_generation"],
        "projection_epoch": active["projection_epoch"],
        "projected_ref": active["projected_ref"],
        "projected_tip": projected_tip,
    }


def _view_identity(active: dict) -> tuple:
    return (
        active["descriptor_id"],
        active["descriptor_generation"],
        active["projection_epoch"],
        active["projected_ref"],
    )


def _visible_changes(canonical: Path, old: str, new: str, descriptor: dict) -> bool:
    """Whether any commit in a canonical advance touches a visible path.

    Looking only at the net diff between ``old`` and ``new`` is insufficient:
    an intermediate visible change can be added and then removed before the
    final tip. That commit still changes projected history and must invalidate
    the reuse shortcut.
    """
    if old == new:
        return False
    from validation import visible

    commits = git(canonical, "rev-list", "--reverse", f"{old}..{new}").stdout.split()
    for commit in commits:
        raw = git(
            canonical,
            "diff-tree",
            "-r",
            "--root",
            "--name-only",
            "-z",
            "--no-commit-id",
            commit,
        ).stdout
        if any(path and visible(path, descriptor) for path in raw.split(b"\0")):
            return True
    return False


def _suffix_affects_view(
    canonical: Path,
    view_id: str,
    active: dict,
    descriptor: dict,
    records: list[dict] | tuple,
) -> bool:
    """Whether any WAL suffix record changes this view's projected tip."""
    for record in records:
        kind = record["kind"]
        if kind == "view_create":
            continue
        if kind == "policy_transition":
            if any(
                item["view_id"] == view_id for item in record["payload"]["views"]
            ):
                return True
            continue
        if kind == "submission":
            payload = record["payload"]
            if (
                payload["view_id"] == view_id
                and payload["projection_epoch"] == active["projection_epoch"]
                and payload["descriptor_id"] == active["descriptor_id"]
            ):
                return True
            if _visible_changes(
                canonical, payload["canonical_old"], payload["canonical_new"], descriptor
            ):
                return True
        elif kind == "canonical_update":
            payload = record["payload"]
            if _visible_changes(
                canonical, payload["canonical_old"], payload["canonical_new"], descriptor
            ):
                return True
    return False


def _link_tree(source: Path, destination: Path) -> None:
    """Hardlink every regular file of ``source`` into ``destination``.

    Verified immutable objects are reused without recopying bytes; the
    candidate becomes physically self-contained because old generations are
    never deleted (generation GC is deferred by spec).
    """
    source_root = source.resolve()
    destination_root = destination.resolve()
    for path in sorted(source_root.rglob("*")):
        if path.is_dir():
            (destination_root / path.relative_to(source_root)).mkdir(
                parents=True, exist_ok=True
            )
            continue
        if not path.is_file():
            continue
        target = destination_root / path.relative_to(source_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            continue
        try:
            os.link(path, target)
        except OSError:
            shutil.copy2(path, target)


def _light_valid_view(path: Path, cursor: dict, projected_ref: str) -> bool:
    """Cursor/ref/hook identity checks without fsck, census, or rev-list."""
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
    return True


def _valid_view(path: Path, cursor: dict, projected_ref: str) -> bool:
    _COUNTS["full_view_validations"] += 1
    if not _light_valid_view(path, cursor, projected_ref):
        return False
    return (
        git(path, "fsck", "--full", "--strict", check=False).returncode == 0
        and has_exact_object_closure(path)
    )


def _receipts_dir(root: Path) -> Path:
    return root / "materialization-receipts"


def _receipt_path(root: Path, view_id: str) -> Path:
    if not view_id or "/" in view_id or view_id in {".", ".."}:
        raise ForgeError("invalid view for materialization receipt", "integrity")
    return _receipts_dir(root) / f"{view_id}.json"


def _expected_receipt(
    manifest: Manifest,
    view_id: str,
    active: dict,
    cursor: dict,
    generation: Path,
    applied_seq: int,
    epoch_frontier: str,
) -> dict:
    """Progress receipt bound to per-view identity, not the global head.

    ``applied_seq`` is the last WAL sequence applied to the generation;
    ``epoch_frontier`` is the canonical frontier the epoch bootstrapped
    from.  Neither moves when unrelated records append, so a restart
    reuses the generation instead of reconstructing from the epoch.
    """
    return {
        "repository_id": manifest.repository_id,
        "view_id": view_id,
        "descriptor_id": active["descriptor_id"],
        "descriptor_generation": active["descriptor_generation"],
        "projection_epoch": active["projection_epoch"],
        "epoch_frontier": epoch_frontier,
        "projected_ref": active["projected_ref"],
        "projected_tip": cursor["projected_tip"],
        "applied_seq": applied_seq,
        "generation": generation.name,
    }


def _write_receipt(root: Path, view_id: str, receipt: dict) -> None:
    """Write the checksummed derived receipt only after full validation."""
    from wal import sha256 as _sha256

    body = canonical_json(receipt)
    envelope = {"receipt": receipt, "sha256": _sha256(body)}
    target = _receipt_path(root, view_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(canonical_json(envelope))
    with temporary.open("rb") as output:
        os.fsync(output.fileno())
    os.replace(temporary, target)
    _fsync_directory(target.parent)


def _read_receipt(root: Path, view_id: str) -> dict | None:
    """Return the validated receipt, or None when missing/corrupt/stale."""
    from wal import sha256 as _sha256

    try:
        raw = _receipt_path(root, view_id).read_bytes()
        envelope = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(envelope, dict) or set(envelope) != {"receipt", "sha256"}:
        return None
    receipt = envelope["receipt"]
    if not isinstance(receipt, dict):
        return None
    if canonical_json(envelope) != raw:
        return None
    if _sha256(canonical_json(receipt)) != envelope["sha256"]:
        return None
    return receipt


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
    _entries=None,
) -> tuple[str, object]:
    """Apply canonical commits incrementally (changed paths, not full trees).

    Returns the new projected tip plus the tracked projected entries so
    callers replaying a record sequence can carry entries forward instead of
    re-listing the base tree once per record.
    """
    from projection import IncrementalProjector

    if not commits:
        return projected_tip, _entries
    previous_tree = tree_oid(destination, projected_tip)
    projector = IncrementalProjector(canonical, destination, descriptor)
    entries = _entries if _entries is not None else projector.base_entries(projected_tip)
    try:
        old_canonical: str | None = output(canonical, "rev-parse", f"{commits[0]['canonical']}^").strip()
    except ForgeError:
        old_canonical = None
    for item in commits:
        canonical_oid = item["canonical"]
        entries, projected_tree = projector.advance_commit(
            entries, old_canonical, canonical_oid
        )
        if projected_tree != previous_tree:
            projected_tip = synthetic_commit(
                canonical, destination, canonical_oid, projected_tree, projected_tip, item.get("change_id")
            )
            previous_tree = projected_tree
        old_canonical = canonical_oid
    return projected_tip, entries


def ensure_view_materialized(
    root: Path,
    view_id: str,
    manifest: Manifest,
    replay_state: ReplayState,
    repository: Repository | None = None,
    *,
    full_validation: bool = False,
) -> MaterializedView:
    """Materialize one view generation, reusing validated work when safe.

    Reuse decisions bind to per-view projection identity (descriptor
    assignment, epoch, ref, projected tip) instead of the global WAL head,
    so unrelated records never rebuild a proved generation.  A changed view
    reuses the verified immutable objects of its previous generation in a
    private candidate and applies only the affecting suffix.  Full
    validation (cursor, single ref, hooks, no unsafe alternates/grafts/
    shallow state, strict fsck, exact closure) is mandatory when a
    generation is built and on the first acceptance per process.
    """
    repository = repository or Repository.open(root)
    active = replay_state.active_views.get(view_id)
    if active is None:
        raise ForgeError("view is not active in the committed WAL", "policy")
    cache_key = (str(root.resolve()), view_id)
    link = view_repo(root, view_id)
    with _flock(materialization_lock(root, view_id)):
        active_path = _resolved_symlink(link)
        registered = _VIEW_REGISTRATIONS.get(cache_key)
        applied_seq: int | None = None
        if (
            not full_validation
            and registered is not None
            and active_path is not None
            and registered[1] == str(active_path)
        ):
            registered_cursor, registered_applied = registered[0], registered[2]
            if registered_cursor == _view_cursor(
                view_id, active, registered_cursor["projected_tip"]
            ) and _light_valid_view(
                active_path, registered_cursor, active["projected_ref"]
            ):
                canonical = ensure_canonical_materialized(root, manifest, repository)
                descriptor = repository._descriptor(active["descriptor_id"])
                suffix = replay_state.records[registered_applied:]
                if not _suffix_affects_view(
                    canonical, view_id, active, descriptor, suffix
                ):
                    _COUNTS["light_view_checks"] += 1
                    _VIEW_REGISTRATIONS[cache_key] = (
                        dict(registered_cursor),
                        str(active_path),
                        manifest.head_seq,
                    )
                    return MaterializedView(active_path, dict(registered_cursor))
                applied_seq = registered_applied
        if active_path is not None:
            # Restart path: a receipt bound to per-view identity and
            # progress supplies the expected projected tip. One full
            # validation of the existing generation, then reuse when the
            # suffix since the receipt proved unrelated. Missing, corrupt,
            # stale, or contradictory receipts cause verified
            # reconstruction.
            receipt = _read_receipt(root, view_id)
            if receipt is not None:
                cursor = _view_cursor(view_id, active, receipt.get("projected_tip", ""))
                identity_matches = all(
                    receipt.get(field) == value
                    for field, value in (
                        ("repository_id", manifest.repository_id),
                        ("view_id", view_id),
                        ("descriptor_id", active["descriptor_id"]),
                        ("descriptor_generation", active["descriptor_generation"]),
                        ("projection_epoch", active["projection_epoch"]),
                        ("projected_ref", active["projected_ref"]),
                        ("generation", active_path.name),
                    )
                )
                if identity_matches and applied_seq is None:
                    if (
                        cursor["projected_tip"]
                        and _read_cursor(active_path) == cursor
                        and _valid_view(active_path, cursor, active["projected_ref"])
                    ):
                        canonical = ensure_canonical_materialized(root, manifest, repository)
                        descriptor = repository._descriptor(active["descriptor_id"])
                        suffix = replay_state.records[receipt["applied_seq"]:]
                        if not _suffix_affects_view(
                            canonical, view_id, active, descriptor, suffix
                        ):
                            _VIEW_REGISTRATIONS[cache_key] = (
                                dict(cursor),
                                str(active_path),
                                manifest.head_seq,
                            )
                            return MaterializedView(active_path, dict(cursor))
                        # Validation passed; only the affecting suffix must
                        # be built onto this verified base.
                        applied_seq = receipt["applied_seq"]
        canonical = ensure_canonical_materialized(root, manifest, repository)
        descriptor = repository._descriptor(active["descriptor_id"])
        epoch_start = _record_at(replay_state, active["epoch_start_seq"])
        epoch_frontier = epoch_start["payload"]["canonical_frontier"]
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
            projected_tip = ZERO_OID
            tracked_entries = None
            start_index = active["epoch_start_seq"]
            previous = None
            if applied_seq is not None and active_path is not None:
                # Verified suffix work: link the previous generation's
                # immutable objects into the private candidate and apply
                # only the records after its proven progress point.
                previous_tip = ref(active_path, active["projected_ref"])
                if previous_tip != ZERO_OID and applied_seq >= active["epoch_start_seq"]:
                    _link_tree(active_path / "objects", generation / "objects")
                    projected_tip = previous_tip
                    start_index = applied_seq
                    previous = active_path
            if previous is None:
                start = epoch_start
                if start["kind"] == "view_create":
                    projected_tip, _frontier, _anchors = project_history_fast(
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
            # Tracked projected entries cross record boundaries so one catch-up
            # run lists its base tree once, not once per record. Evidence
            # jumps (installed packs) invalidate the tracked entries.
            for record in replay_state.records[start_index:]:
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
                        tracked_entries = None
                    else:
                        projected_tip, tracked_entries = _apply_canonical_commits(
                            canonical, generation, descriptor, payload["mappings"], projected_tip, tracked_entries
                        )
                elif record["kind"] == "canonical_update":
                    projected_tip, tracked_entries = _apply_canonical_commits(
                        canonical, generation, descriptor, payload["commits"], projected_tip, tracked_entries
                    )
                elif record["kind"] == "policy_transition" and any(
                    item["view_id"] == view_id for item in payload["views"]
                ):
                    raise ForgeError("active view epoch continued after another policy transition", "integrity")
            if projected_tip == ZERO_OID:
                raise ForgeError("view materialization produced no projected commit", "integrity")
            git(generation, "update-ref", active["projected_ref"], projected_tip)
            cursor = _view_cursor(view_id, active, projected_tip)
            _write_cursor(generation, cursor)
            # A new generation is activated only after its full validation:
            # cursor, single ref, hooks, no unsafe state, strict fsck, and
            # exact physical-versus-reachable closure all pass first.
            if not _valid_view(generation, cursor, active["projected_ref"]):
                raise ForgeError("view materialization failed validation", "integrity")
            _activate(root, link, generation)
            _COUNTS["view_generations_built"] += 1
            if previous is not None:
                _COUNTS["suffix_view_rebuilds"] += 1
            _write_receipt(
                root,
                view_id,
                _expected_receipt(
                    manifest, view_id, active, cursor, generation, manifest.head_seq, epoch_frontier
                ),
            )
            _VIEW_REGISTRATIONS[cache_key] = (
                dict(cursor),
                str(generation),
                manifest.head_seq,
            )
            from failpoints import checkpoint

            checkpoint(root, "wal.secondary_materialized")
            return MaterializedView(generation, dict(cursor))
        except Exception:
            shutil.rmtree(generation, ignore_errors=True)
            raise


def ensure_intake_materialized(
    root: Path,
    materialized_view: MaterializedView,
    manifest: Manifest | None = None,
    replay: ReplayState | None = None,
) -> Path:
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
        if manifest is None:
            manifest, _version, replay = Repository.open(root).load()
        elif replay is None:
            _manifest, _version, replay = Repository.open(root).load()
        cursor = materialized_view.cursor
        # The captured view materialization must still match committed
        # identity: descriptor assignment, epoch, ref, and tip.  Unrelated
        # WAL appends do not supersede it; a rebuilt generation (new tip)
        # or a policy change does.
        active = replay.active_views.get(view_id)
        if (
            active is None
            or _view_identity(active)
            != _view_identity(
                {
                    "descriptor_id": cursor["descriptor_id"],
                    "descriptor_generation": cursor["descriptor_generation"],
                    "projection_epoch": cursor["projection_epoch"],
                    "projected_ref": cursor["projected_ref"],
                }
            )
            or cursor["projected_tip"] != target_tip
        ):
            raise ForgeError("materialized view was superseded", "conflict")
        active_generation = _resolved_symlink(view_repo(root, view_id))
        if active_generation is not None and active_generation != materialized_view.path:
            active_cursor = _read_cursor(active_generation)
            if (
                active_cursor is not None
                and active_cursor.get("projected_tip") != cursor["projected_tip"]
            ):
                raise ForgeError("materialized view was superseded", "conflict")
        active_intake = _resolved_symlink(link)
        if active_intake is not None and _read_cursor(active_intake) == cursor:
            alternate = active_intake / "objects" / "info" / "alternates"
            advertised = git(active_intake, "for-each-ref", "--format=%(refname)", check=False)
            if (
                alternate.is_file()
                and alternate.read_text() == expected_alternate
                and ref(active_intake, projected_ref) == target_tip
                and _uses_repository_hooks(active_intake)
                and _intake_receive_checks(active_intake)
                and not advertised.returncode
                and advertised.stdout.decode().splitlines() == [projected_ref]
            ):
                _install_intake_hooks(root, view_id, active_intake)
                return active_intake
        generation = (
            materializations_dir(root)
            / "intakes"
            / view_id
            / f"{cursor['descriptor_generation']}-{cursor['projection_epoch']}-{uuid.uuid4().hex}.git"
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
