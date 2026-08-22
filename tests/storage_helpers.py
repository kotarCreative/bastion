"""Shared helpers for asserting on store-backed forge state."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from state import load_state


def read_document(root: Path) -> dict:
    """Load the full forge document from the durable store."""
    return load_state(root)


def document_bytes(root: Path) -> bytes:
    """Canonical semantic snapshot standing in for legacy file bytes."""
    return json.dumps(read_document(root), sort_keys=True, separators=(",", ":")).encode()
