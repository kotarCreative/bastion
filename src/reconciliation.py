"""Construct canonical commits from authorized submitted transitions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from git import (
    author_environment,
    commit_message,
    copy_object,
    make_commit,
    tree_entries,
    tree_oid,
    write_tree,
)
from projection import project_tree
from validation import (
    TreeChanges,
    apply_changes,
    authorize_changes,
    changed_entries,
    parse_change_id,
)


@dataclass(frozen=True)
class Mapping:
    submitted: str
    canonical: str
    change_id: str | None

    def record(self) -> dict[str, str | None]:
        return {
            "submitted": self.submitted,
            "canonical": self.canonical,
            "change_id": self.change_id,
        }


def materialize_transition(
    canonical: Path,
    submitted: Path,
    canonical_parent: str,
    changes: TreeChanges,
    submitted_env: dict[str, str],
    candidate_env: dict[str, str],
) -> str:
    canonical_entries = tree_entries(canonical, canonical_parent, candidate_env)
    result = apply_changes(canonical_entries, changes)
    for path, entry in changes.items():
        if entry is not None:
            copy_object(
                submitted,
                canonical,
                entry[2],
                source_env=submitted_env,
                destination_env=candidate_env,
            )
    return write_tree(canonical, result, candidate_env)


def reconcile_commits(
    canonical: Path,
    submitted: Path,
    commits: list[str],
    canonical_parent: str,
    projected_parent: str,
    descriptor: dict,
    submitted_env: dict[str, str],
    candidate_env: dict[str, str],
) -> tuple[str, list[dict[str, str | None]]]:
    mappings: list[dict[str, str | None]] = []
    for submitted_oid in commits:
        parent_entries = tree_entries(submitted, projected_parent, submitted_env)
        child_entries = tree_entries(submitted, submitted_oid, submitted_env)
        changes = changed_entries(parent_entries, child_entries)
        authorize_changes(changes, descriptor)
        candidate_tree = materialize_transition(
            canonical,
            submitted,
            canonical_parent,
            changes,
            submitted_env,
            candidate_env,
        )
        message = commit_message(submitted, submitted_oid, submitted_env)
        canonical_oid = make_commit(
            canonical,
            candidate_tree,
            canonical_parent,
            message,
            author_environment(submitted, submitted_oid, submitted_env),
            candidate_env,
        )
        round_trip = project_tree(
            canonical,
            canonical,
            canonical_oid,
            descriptor,
            candidate_env,
            candidate_env,
        )
        if round_trip != tree_oid(submitted, submitted_oid, submitted_env):
            from errors import ForgeError

            raise ForgeError(
                "canonical round-trip does not reproduce the submitted tree"
            )
        mappings.append(
            Mapping(
                submitted_oid,
                canonical_oid,
                parse_change_id(message),
            ).record()
        )
        canonical_parent = canonical_oid
        projected_parent = submitted_oid
    return canonical_parent, mappings
