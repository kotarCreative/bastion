"""Permission-view projection engine."""

from __future__ import annotations

from pathlib import Path

from errors import ForgeError
from git import (
    author_environment,
    copy_object,
    first_parent_commits,
    make_commit,
    commit_message,
    tree_entries,
    tree_oid,
    validate_no_empty_subtrees,
    write_tree,
)
from layout import ZERO_OID
from validation import allowed
from validation import parse_change_id


def project_tree(
    canonical: Path,
    destination: Path,
    canonical_treeish: str,
    descriptor: dict,
    source_env: dict[str, str] | None = None,
    destination_env: dict[str, str] | None = None,
) -> str:
    read_prefixes = descriptor["read"]
    visible = {}
    validate_no_empty_subtrees(canonical, canonical_treeish, source_env)
    for path, entry in tree_entries(canonical, canonical_treeish, source_env).items():
        if not allowed(path, read_prefixes):
            continue
        mode, kind, oid = entry
        if kind == b"blob":
            copy_object(canonical, destination, oid, source_env, destination_env)
        elif kind != b"commit":
            raise ForgeError("unsupported canonical tree entry")
        visible[path] = (mode, kind, oid)
    return write_tree(destination, visible, destination_env)


def synthetic_commit(
    canonical: Path,
    destination: Path,
    canonical_oid: str,
    projected_tree: str,
    projected_parent: str,
    change_id: str | None = None,
) -> str:
    metadata = author_environment(canonical, canonical_oid, projected=True)
    message = b"projected canonical change\n"
    if change_id:
        message += f"\nChange-Id: {change_id}\n".encode()
    return make_commit(
        destination,
        projected_tree,
        projected_parent,
        message,
        metadata,
    )


def visible_advance(
    canonical: Path,
    frontier: str,
    tip: str,
    destination: Path,
    descriptor: dict,
    expected_tree: str,
) -> None:
    previous_tree = expected_tree
    for oid in first_parent_commits(canonical, frontier, tip):
        projected = project_tree(canonical, destination, oid, descriptor)
        if projected != previous_tree:
            raise ForgeError(
                "visible history advanced; synchronize the view, fetch, and rebase"
            )
        previous_tree = projected


def project_history(
    canonical: Path,
    destination: Path,
    frontier: str,
    canonical_tip: str,
    projected_tip: str,
    descriptor: dict,
) -> tuple[str, str, dict[str, dict[str, str]]]:
    """Project canonical commits, eliding commits whose visible tree is unchanged."""
    read_prefixes = descriptor["read"]
    previous_tree = (
        tree_oid(destination, projected_tip) if projected_tip != ZERO_OID else None
    )
    anchors: dict[str, dict[str, str]] = {}
    for canonical_oid in first_parent_commits(canonical, frontier, canonical_tip):
        projected_tree = project_tree(
            canonical, destination, canonical_oid, descriptor
        )
        if projected_tree != previous_tree:
            change_id = parse_change_id(commit_message(canonical, canonical_oid))
            projected_tip = synthetic_commit(
                canonical,
                destination,
                canonical_oid,
                projected_tree,
                projected_tip,
                change_id,
            )
            anchors[projected_tip] = {
                "canonical": canonical_oid,
                "kind": "synthetic",
                "change_id": change_id,
            }
            previous_tree = projected_tree
        frontier = canonical_oid
    return projected_tip, frontier, anchors
