#!/usr/bin/env python3
"""Publish the first private control snapshot for a restored Artifacts root."""

from __future__ import annotations

import os
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cloudflare_runtime import CONTROL_KEY, checkpoint_root
from object_store import ObjectNotFound
from object_store_artifacts import ArtifactsObjectStore
from repository import Repository
from state import locked


def main() -> int:
    if sys.argv[1:] in (['-h'], ['--help']):
        print(f"usage: {Path(sys.argv[0]).name} FORGE_ROOT")
        print("Publish the first private Artifacts control snapshot for a restored root.")
        return 0
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} FORGE_ROOT", file=sys.stderr)
        return 2
    root = Path(sys.argv[1]).resolve()
    remote = os.environ.get("BASTION_ARTIFACTS_REMOTE", "")
    token = os.environ.get("BASTION_ARTIFACTS_TOKEN", "")
    if not remote or not token:
        print("Artifacts remote and private write token are required", file=sys.stderr)
        return 2
    try:
        backend = json.loads((root / "backend.json").read_text())
    except (OSError, json.JSONDecodeError):
        print("forge root has no valid SQLite backend record", file=sys.stderr)
        return 2
    if backend != {"backend": "sqlite"}:
        print("Cloudflare Containers requires a SQLite control-plane backend", file=sys.stderr)
        return 2
    os.environ["BASTION_ARTIFACTS_TOKEN"] = token
    repository = Repository.open(root)
    store = repository.store
    if not isinstance(store, ArtifactsObjectStore):
        print("forge root is not configured with the Artifacts object store", file=sys.stderr)
        return 2
    if (
        store.config.remote != remote
        or store.config.branch != "bastion-storage"
        or store.config.token_env != "BASTION_ARTIFACTS_TOKEN"
    ):
        print("forge Artifacts config does not match the Cloudflare runtime", file=sys.stderr)
        return 2
    with locked(root):
        pass
    try:
        store.get(CONTROL_KEY)
    except ObjectNotFound:
        pass
    else:
        print("private control snapshot already exists; refusing replacement", file=sys.stderr)
        return 2
    checkpoint_root(root, store, None)
    print(f"Published the initial private control snapshot for {root}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
