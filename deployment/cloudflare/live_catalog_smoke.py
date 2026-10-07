"""Verify the isolated authenticated catalog Worker before/after a redeploy.

Deploy wrangler.smoke.jsonc with a protected BASTION_SMOKE_SECRET secrets file.
This script never targets the production Container catalog or Artifacts repo.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from durable_store import CatalogClient, DurableObjectsBackend, StagedTransaction
from store import Store, _checksum

ROW_COUNT = 100


def records():
    for index in range(ROW_COUNT):
        document = {"id": f"smoke-{index:03}", "description": "é漢字 persistent catalog " + "x" * 100_000}
        yield document["id"], json.dumps(document, ensure_ascii=False), 1, _checksum(document)


def seed(client: CatalogClient, temporary: Path):
    status = client.status()
    if status["revision"] != 0 or status["root"] is not None:
        raise RuntimeError("refusing to seed an existing catalog; use verify for redeployment")
    store = Store(DurableObjectsBackend(temporary, client))
    try:
        store.bootstrap()
    finally:
        store.close()
    transaction = StagedTransaction(client, client.status()["revision"])
    transaction.append({"sql": "INSERT INTO principals VALUES (?, ?, ?, ?)", "params": list(row)} for row in records())
    self_check = client.call("/query", {"revision": 1, "statement": {"sql": "SELECT COUNT(*) AS count FROM principals", "params": []}})
    assert self_check["rows"] == [[0]], "staging published tentative data"
    root = {"repository": '{"repository_id":"large-catalog-smoke"}', "phase": "ready"}
    assert transaction.commit([], root=root) == 2
    assert transaction.commit([], root=root) == 2, "commit retry changed the revision"


def verify(client: CatalogClient, temporary: Path):
    assert client.status()["revision"] == 2
    store = Store(DurableObjectsBackend(temporary, client))
    try:
        assert len(store.load_sections(["principals"])["principals"]) == ROW_COUNT
    finally:
        store.close()
    output = temporary / "portable.sqlite3"
    DurableObjectsBackend(temporary, client).dump(output)
    import sqlite3
    database = sqlite3.connect(output)
    try:
        assert database.execute("SELECT key, doc, version, checksum FROM principals ORDER BY key").fetchall() == list(records())
    finally:
        database.close()
    assert output.stat().st_size > 8 * 1024 * 1024
    print(json.dumps({"rows": ROW_COUNT, "backup_bytes": output.stat().st_size, "revision": 2, "verified": True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["seed", "verify"])
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--secrets-file", type=Path, required=True)
    args = parser.parse_args()
    if not args.endpoint.startswith("https://bastion-catalog-smoke.") or not args.endpoint.endswith(".workers.dev"):
        parser.error("use the isolated bastion-catalog-smoke workers.dev endpoint")
    secret = json.loads(args.secrets_file.read_text())["BASTION_SMOKE_SECRET"]
    client = CatalogClient(args.endpoint, token=secret)
    with tempfile.TemporaryDirectory(prefix="bastion-live-catalog-") as temporary:
        root = Path(temporary)
        if args.phase == "seed":
            seed(client, root)
        verify(client, root)


if __name__ == "__main__":
    main()
