"""Read-only global invariant checks for Bastion repositories."""

from __future__ import annotations

from pathlib import Path

from errors import ForgeError
from git import has_exact_object_closure
from layout import view_repo
from state import load_state


def _fail(code: str, subject: object, detail: str) -> None:
    raise ForgeError(f"invariant {code} failed for {subject}: {detail}")


def check_invariants(root: Path) -> None:
    """Validate repository truth and its immutable object closure."""
    from repository import Repository
    from wal import sha256

    state = load_state(root)
    if state.get("repository_state") != "ready":
        _fail("REPOSITORY", "forge_meta", "forge initialization is incomplete")
    repository = Repository.open(root)
    manifest, _version, replay = repository.load()
    if state.get("repository_id") != repository.repository_id:
        _fail(
            "REPOSITORY",
            "forge_meta",
            "repository ID differs from repository configuration",
        )
    if f"refs/heads/{state.get('canonical_branch')}" != manifest.canonical_ref:
        _fail(
            "REPOSITORY",
            "forge_meta",
            "canonical branch differs from repository configuration",
        )

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
            _fail(
                "REPOSITORY",
                pack.get("pack_key", "pack"),
                f"referenced pack is missing: {error}",
            )
        if (
            sha256(body) != pack["pack_sha256"]
            or len(body) != pack["pack_size"]
            or sha256(index) != pack["idx_sha256"]
            or len(index) != pack["idx_size"]
        ):
            _fail(
                "REPOSITORY",
                pack["pack_key"],
                "referenced pack hash or size differs",
            )

    for view_id in replay.active_views:
        cached = view_repo(root, view_id)
        if not cached.exists():
            continue
        alternates = cached.resolve() / "objects" / "info" / "alternates"
        if alternates.exists():
            _fail("ISOLATION", view_id, "active view has an alternate")
        if not has_exact_object_closure(cached):
            _fail("ISOLATION", view_id, "active view contains unreachable objects")
