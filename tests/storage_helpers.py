"""Shared helpers for asserting on store-backed forge state."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from state import load_state
from repository import Repository


def read_document(root: Path) -> dict:
    """Load the full forge document from the durable store."""
    return load_state(root)


def document_bytes(root: Path) -> bytes:
    """Canonical semantic snapshot of the control-plane document."""
    return json.dumps(read_document(root), sort_keys=True, separators=(",", ":")).encode()


def repository_state(root: Path):
    """Load the authoritative manifest and replayed WAL state."""
    manifest, _version, replay = Repository.open(root).load()
    return manifest, replay


def wal_records(root: Path, kind: str | None = None) -> list[dict]:
    records = repository_state(root)[1].records
    return [record for record in records if kind is None or record["kind"] == kind]


def submission_record(root: Path, view: str, old: str, new: str) -> dict:
    for record in reversed(wal_records(root, "submission")):
        payload = record["payload"]
        if (
            payload["view_id"] == view
            and payload["projected_old"] == old
            and payload["projected_new"] == new
        ):
            return record
    raise AssertionError("submission is absent from the committed WAL")


def repository_snapshot(root: Path) -> tuple[bytes, tuple[tuple[str, int, str], ...]]:
    """Snapshot committed truth plus immutable uploads for rejection checks."""
    repository = Repository.open(root)
    manifest = repository.get("manifest.json").body
    objects = tuple(
        sorted(
            (meta.key, meta.size, meta.version.value)
            for meta in repository.store.list_prefix(f"{repository.prefix}/")
        )
    )
    return manifest, objects
