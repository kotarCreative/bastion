"""Fork-aware, scoped cold projection builder and branch-scope helpers."""

from __future__ import annotations

from permissions import authorized_branch_refs
from errors import ForgeError
from pathlib import Path
import shutil
import tempfile

from artifacts import build_full_refs_pack
from git import ZERO_OID, git, make_commit, author_environment, has_exact_object_closure
from projection import project_history_fast, project_tree, IncrementalProjector, synthetic_commit
from validation import visible


def authorized_frontiers(descriptor: dict, canonical_refs: dict[str, str]) -> dict[str, str]:
    """Return the deterministic readable ref -> canonical-tip snapshot."""
    return {ref: canonical_refs[ref] for ref in authorized_branch_refs(descriptor, canonical_refs)}


def selected_default(descriptor: dict, projected_refs: dict[str, str]) -> str | None:
    """Choose the policy default when readable, otherwise the first readable ref."""
    default = descriptor.get("canonical_ref")
    if default in projected_refs:
        return default
    return min(projected_refs, default=None)


def build_scoped_projection(
    root: Path, view_id: str, canonical: Path, destination: Path, manifest,
    replay, repository, descriptor: dict,
) -> dict[str, str]:
    """Build all projected branch tips privately, then publish only authorized closure.

    The destination must be a new empty bare generation. No activation or cursor
    metadata is written here; callers own those lifecycle operations.
    """
    import os
    from artifacts import install_pack

    active = replay.active_views[view_id]
    epoch_start = replay.records[active["epoch_start_seq"] - 1]
    start_payload = epoch_start["payload"]
    pinned = dict(active["epoch_branches"])
    stage = Path(tempfile.mkdtemp(prefix="bastion-branch-stage-", dir=destination.parent))
    try:
        git(destination, "rev-parse", "--git-dir")  # require initialized destination
        if git(destination, "for-each-ref", "--format=%(refname)").stdout.strip() or git(
            destination, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)"
        ).stdout.strip():
            raise ForgeError("scoped projection destination is not empty", "integrity")
        git(stage, "init", "--bare", "--quiet")
        tips: dict[str, str] = {}
        if epoch_start["kind"] == "view_create":
            for ref, tip in sorted(pinned.items()):
                projected, _, _ = project_history_fast(canonical, stage, ZERO_OID, tip, ZERO_OID, descriptor)
                tips[ref] = projected
        elif epoch_start["kind"] == "policy_transition":
            anchors: dict[str, str] = {}
            for ref, tip in sorted(pinned.items()):
                tree = project_tree(canonical, stage, tip, descriptor)
                if tip not in anchors:
                    anchors[tip] = make_commit(stage, tree, ZERO_OID, b"projected policy epoch\n", author_environment(canonical, tip, projected=True))
                tips[ref] = anchors[tip]
        else:
            raise ForgeError("invalid view epoch start", "integrity")

        branch_frontiers = dict(pinned)
        for record in replay.records[active["epoch_start_seq"]:]:
            kind, payload = record["kind"], record["payload"]
            if kind == "branch_create":
                source, target = payload["source_ref"], payload["branch_ref"]
                if source in tips:
                    tips[target] = tips[source]
                    branch_frontiers[target] = payload["canonical_tip"]
            elif kind == "branch_submission" and payload["view_id"] == view_id:
                target, source = payload["canonical_ref"], payload["source_ref"]
                if (payload["descriptor_id"] != active["descriptor_id"] or
                    payload["projection_epoch"] != active["projection_epoch"]):
                    raise ForgeError("branch submission belongs to a different view epoch", "integrity")
                if source is not None:
                    if source not in tips or branch_frontiers.get(source) != payload["canonical_old"] or payload["canonical_old"] != payload["canonical_frontier_old"]:
                        raise ForgeError("branch submission source frontier is stale", "integrity")
                    if not payload["mappings"]:
                        if payload["canonical_ref"] in tips:
                            raise ForgeError("branch submission target already exists", "integrity")
                        tips[target] = tips[source]
                    else:
                        if payload["projected_old"] != tips[source]:
                            raise ForgeError("branch submission does not extend its target", "integrity")
                        tips[target] = tips[source]
                        install_pack(repository, payload["submission_pack"], stage / "objects")
                        tips[target] = payload["projected_new"]
                    branch_frontiers[target] = payload["canonical_new"]
                else:
                    if target not in tips or payload["projected_old"] != tips[target] or branch_frontiers.get(target) != payload["canonical_old"]:
                        raise ForgeError("branch update source evidence does not extend its target", "integrity")
                    install_pack(repository, payload["submission_pack"], stage / "objects")
                    tips[target] = payload["projected_new"]
                    branch_frontiers[target] = payload["canonical_new"]
            elif kind == "branch_submission":
                target = payload["canonical_ref"]
                source = payload["source_ref"]
                if target not in tips and source is not None:
                    if source not in tips:
                        continue
                    tips[target] = tips[source]
                    branch_frontiers[target] = branch_frontiers[source]
                if target not in tips:
                    continue
                items = payload["mappings"]
                if items:
                    tips[target], _ = _apply_mappings(canonical, stage, descriptor, items, tips[target])
                    branch_frontiers[target] = payload["canonical_new"]
            elif kind in ("canonical_update", "submission"):
                if kind == "submission" and payload.get("view_id") == view_id:
                    if payload.get("projection_epoch") != active["projection_epoch"] or payload.get("descriptor_id") != active["descriptor_id"]:
                        raise ForgeError("submission belongs to a different view epoch", "integrity")
                    target = payload.get("canonical_ref", descriptor["canonical_ref"])
                    if target not in tips or tips[target] != payload["projected_old"]:
                        raise ForgeError("source evidence does not extend materialized view", "integrity")
                    install_pack(repository, payload["submission_pack"], stage / "objects")
                    tips[target] = payload["projected_new"]
                    branch_frontiers[target] = payload.get("canonical_new", payload.get("canonical_tip", branch_frontiers[target]))
                else:
                    target = payload.get("canonical_ref", descriptor["canonical_ref"])
                    items = payload.get("commits", payload.get("mappings", []))
                    if target in tips and items:
                        old_tree = git(stage, "rev-parse", f"{tips[target]}^{{tree}}").stdout.decode().strip()
                        projector = IncrementalProjector(canonical, stage, descriptor)
                        entries = projector.base_entries(tips[target])
                        old_canonical = branch_frontiers.get(target)
                        for item in items:
                            oid = item["canonical"]
                            entries, tree = projector.advance_commit(entries, old_canonical, oid)
                            if tree != old_tree:
                                tips[target] = synthetic_commit(canonical, stage, oid, tree, tips[target], item.get("change_id"))
                                old_tree = tree
                            old_canonical = oid
                            branch_frontiers[target] = oid
                        if payload.get("canonical_new"):
                            branch_frontiers[target] = payload["canonical_new"]

        allowed = authorized_branch_refs(descriptor, manifest.canonical_refs)
        missing = sorted(set(allowed) - set(tips))
        if missing:
            raise ForgeError("authorized branch has no projected frontier", "integrity")
        projected = {ref: tips[ref] for ref in allowed}
        if projected:
            git(stage, "update-ref", "refs/bastion/export", next(iter(projected.values())))
            pack = build_full_refs_pack(stage, list(projected.values()))
            try:
                pack_dir = destination / "objects" / "pack"
                pack_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(pack.pack_path, pack_dir / pack.pack_path.name)
                shutil.copyfile(pack.idx_path, pack_dir / pack.idx_path.name)
            finally:
                pack.cleanup()
            for ref, tip in projected.items():
                git(destination, "update-ref", ref, tip)
        if git(destination, "fsck", "--full", "--strict").returncode or not has_exact_object_closure(destination):
            raise ForgeError("scoped projection has incomplete or unauthorized object closure", "integrity")
        return projected
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _apply_mappings(canonical: Path, destination: Path, descriptor: dict, mappings: list[dict], tip: str) -> tuple[str, object]:
    """Project a submitted canonical mapping range with normal synthetic commits."""
    projector = IncrementalProjector(canonical, destination, descriptor)
    entries = projector.base_entries(tip)
    from git import tree_oid
    previous_tree = tree_oid(destination, tip)
    old = git(canonical, "rev-parse", f"{mappings[0]['canonical']}^").stdout.decode().strip()
    for item in mappings:
        oid = item["canonical"]
        entries, tree = projector.advance_commit(entries, old, oid)
        if tree != previous_tree:
            tip = synthetic_commit(canonical, destination, oid, tree, tip, item.get("change_id"))
            previous_tree = tree
        old = oid
    return tip, entries
