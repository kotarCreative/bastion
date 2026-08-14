"""Forge administration and view-synchronization use cases."""

from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path

from errors import ForgeError
from git import clean_env, git, output, ref, run, tree_entries
from layout import ZERO_OID, canonical_repo, view_repo
from projection import project_history
from state import (
    has_unconfirmed_source,
    is_ancestor,
    locked,
    record_change_representation,
    save_state,
    source_publication_pending,
)
from validation import normalize_prefix, validate_view_name


def init_forge(root: Path, source: Path, branch: str) -> None:
    if run("git", "check-ref-format", "--branch", branch, check=False).returncode:
        raise ForgeError(f"invalid branch name: {branch}")
    if os.pathsep in str(root.resolve()):
        raise ForgeError(
            f"forge root cannot contain the object-path separator {os.pathsep!r}"
        )
    if root.exists() and any(root.iterdir()):
        raise ForgeError(f"forge root is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    (root / "views").mkdir()
    (root / "state.lock").touch()
    run("git", "clone", "--bare", "--no-local", str(source), str(canonical_repo(root)))
    if output(canonical_repo(root), "rev-parse", "--show-object-format") != "sha1":
        raise ForgeError("this experiment currently supports SHA-1 repositories only")
    canonical_ref = f"refs/heads/{branch}"
    if ref(canonical_repo(root), canonical_ref) == ZERO_OID:
        raise ForgeError(f"source has no branch named {branch}")
    save_state(
        root,
        {
            "version": 1,
            "canonical_branch": branch,
            "views": {},
            "transactions": {},
            "changes": {},
            "outbox": {},
            "projection_jobs": {},
            "next_transaction_sequence": 1,
        },
    )


def install_hooks(root: Path, name: str, entrypoint: Path) -> None:
    hooks = view_repo(root, name) / "hooks"
    command = shlex.quote(str(entrypoint.resolve()))
    root_arg = shlex.quote(str(root.resolve()))
    view_arg = shlex.quote(name)
    scripts = {
        "pre-receive": (
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {command} "
            f"pre-receive {root_arg} {view_arg}\n"
        ),
        "post-receive": (
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {command} "
            f"post-receive {root_arg} {view_arg}\n"
        ),
    }
    for filename, contents in scripts.items():
        target = hooks / filename
        target.write_text(contents)
        target.chmod(0o755)


def sync_view_locked(
    root: Path, state: dict, name: str, check_active: bool = True
) -> None:
    view = state["views"].get(name)
    if not view:
        raise ForgeError(f"unknown view: {name}")
    if check_active and source_publication_pending(root, state, name):
        raise ForgeError("view has an active receive transaction")
    if check_active and has_unconfirmed_source(state):
        raise ForgeError("canonical history contains an unconfirmed source publication")
    if check_active:
        from outbox import view_has_unfinished_jobs

        if view_has_unfinished_jobs(state, name):
            raise ForgeError("view has pending projection work")
    plan = build_projection_plan(root, name, view)
    publish_projection_plan(root, state, plan)


def build_projection_plan(
    root: Path,
    name: str,
    view: dict,
    canonical_tip: str | None = None,
) -> dict:
    canonical = canonical_repo(root)
    destination = view_repo(root, name)
    canonical_tip = canonical_tip or ref(canonical, view["canonical_ref"])
    if canonical_tip == ZERO_OID:
        raise ForgeError("canonical branch is empty")
    frontier = view["canonical_frontier"]
    if frontier != ZERO_OID and run(
        "git",
        "--git-dir",
        str(canonical),
        "merge-base",
        "--is-ancestor",
        frontier,
        canonical_tip,
        env=clean_env(),
        check=False,
    ).returncode:
        raise ForgeError("canonical history was rewritten")
    old_tip = view["projected_tip"]
    projected_tip, frontier, anchors = project_history(
        canonical,
        destination,
        frontier,
        canonical_tip,
        old_tip,
        view["read"],
    )
    return {
        "view": name,
        "canonical_ref": view["canonical_ref"],
        "projected_ref": view["projected_ref"],
        "old_frontier": view["canonical_frontier"],
        "new_frontier": frontier,
        "old_tip": old_tip,
        "new_tip": projected_tip,
        "anchors": anchors,
    }


def publish_projection_plan(root: Path, state: dict, plan: dict) -> None:
    name = plan["view"]
    view = state["views"].get(name)
    if not view:
        raise ForgeError(f"unknown view: {name}")
    if view["canonical_frontier"] != plan["old_frontier"] or view[
        "projected_tip"
    ] != plan["old_tip"]:
        if view["canonical_frontier"] == plan["new_frontier"] and view[
            "projected_tip"
        ] == plan["new_tip"]:
            return
        raise ForgeError("projection plan is stale")
    destination = view_repo(root, name)
    canonical = canonical_repo(root)
    canonical_tip = ref(canonical, plan["canonical_ref"])
    if canonical_tip == ZERO_OID or not is_ancestor(
        canonical, plan["new_frontier"], canonical_tip
    ):
        raise ForgeError("canonical history changed while projecting view")
    actual_tip = ref(destination, view["projected_ref"])
    if plan["new_tip"] != plan["old_tip"] and actual_tip != plan["new_tip"]:
        if actual_tip == plan["old_tip"] or is_ancestor(
            destination, actual_tip, plan["new_tip"]
        ):
            git(
                destination,
                "update-ref",
                view["projected_ref"],
                plan["new_tip"],
                actual_tip,
            )
        else:
            raise ForgeError("projected ref changed outside synchronization")
    view["anchors"].update(plan["anchors"])
    for projected_oid, anchor in plan["anchors"].items():
        if anchor["change_id"]:
            record_change_representation(
                state,
                anchor["change_id"],
                name,
                projected_oid,
                anchor["canonical"],
                "synthetic",
            )
    view["projected_tip"] = plan["new_tip"]
    view["canonical_frontier"] = plan["new_frontier"]


def add_view(
    root: Path,
    name: str,
    read: list[str],
    write: list[str],
    branch: str,
    entrypoint: Path,
) -> None:
    validate_view_name(name)
    read = sorted(set(normalize_prefix(value) for value in read))
    write = sorted(set(normalize_prefix(value) for value in write))
    if not read:
        raise ForgeError("a view needs at least one readable path prefix")
    with locked(root) as state:
        if name in state["views"]:
            raise ForgeError(f"view already exists: {name}")
        if branch != state["canonical_branch"]:
            raise ForgeError(
                "this experiment supports the configured canonical branch only"
            )
        destination = view_repo(root, name)
        run("git", "init", "--bare", "-q", "-b", branch, str(destination))
        for key, value in (
            ("receive.denyDeletes", "true"),
            ("receive.denyNonFastForwards", "true"),
            ("uploadpack.allowAnySHA1InWant", "false"),
            ("uploadpack.allowReachableSHA1InWant", "false"),
            ("uploadpack.allowTipSHA1InWant", "false"),
        ):
            git(destination, "config", key, value)
        state["views"][name] = {
            "principal": name,
            "canonical_ref": f"refs/heads/{branch}",
            "projected_ref": f"refs/heads/{branch}",
            "read": read,
            "write": write,
            "canonical_frontier": ZERO_OID,
            "projected_tip": ZERO_OID,
            "anchors": {},
        }
        install_hooks(root, name, entrypoint)
        sync_view_locked(root, state, name)
        save_state(root, state)


def sync_view(root: Path, name: str) -> None:
    with locked(root) as state:
        sync_view_locked(root, state, name)
        save_state(root, state)


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


def update_canonical_ref_locked(
    root: Path,
    state: dict,
    expected: str,
    new: str,
    create_event: bool,
) -> None:
    canonical = canonical_repo(root)
    canonical_ref = f"refs/heads/{state['canonical_branch']}"
    actual = ref(canonical, canonical_ref)
    if actual != expected:
        raise ForgeError("canonical ref changed before administrative update")
    validate_canonical_update(canonical, expected, new)
    transaction = None
    if create_event:
        sequence = state.setdefault("next_transaction_sequence", 1)
        state["next_transaction_sequence"] = sequence + 1
        transaction_id = f"canonical:{sequence}:{new}"
        transaction = {
            "id": transaction_id,
            "kind": "canonical_update",
            "sequence": sequence,
            "canonical_ref": canonical_ref,
            "canonical_old": expected,
            "canonical_new": new,
            "pending_views": sorted(state["views"]),
            "status": "prepared",
        }
        state["transactions"][transaction_id] = transaction
        save_state(root, state)
    git(canonical, "update-ref", canonical_ref, new, expected)
    if transaction:
        from outbox import enqueue_canonical_committed

        enqueue_canonical_committed(state, transaction, ready=True)
        transaction["status"] = (
            "fanout_pending" if transaction["pending_views"] else "published"
        )
        save_state(root, state)


def update_canonical_ref(root: Path, expected: str, new: str) -> None:
    """Serialize and enqueue an administrative canonical fast-forward."""
    with locked(root) as state:
        update_canonical_ref_locked(root, state, expected, new, create_event=True)
