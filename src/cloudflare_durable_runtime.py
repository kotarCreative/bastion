"""Cloudflare runtime with an authoritative Durable Object catalog."""

from __future__ import annotations

import base64
import json
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path

from durable_store import CatalogClient


def _root_config(root: Path, phase: str) -> dict:
    return {"repository": (root / "repository.json").read_text(), "phase": phase}


def _save_config(root: Path, client: CatalogClient, phase: str) -> None:
    client.commit(client.status()["revision"], [], root=_root_config(root, phase))


def import_snapshot(client: CatalogClient, payload: dict) -> dict:
    """Stage an old SQLite snapshot, then publish it in one DO transaction.

    This is the only runtime path which opens SQLite. It reads the legacy
    backup; subsequent startup and all request storage use the DO SQL API.
    """
    from store import _sqlite_ddl, _index_ddl, DOCUMENT_TABLES, HTTP_TOKENS_TABLE

    status = client.status()
    if status["root"] is not None or status["revision"] != 0:
        raise RuntimeError("refusing to import into an existing Durable Object catalog")
    config = {"repository": payload["repository"], "phase": payload["phase"]}
    with tempfile.TemporaryDirectory(prefix="bastion-catalog-import-") as temporary:
        path = Path(temporary) / "legacy.sqlite3"
        path.write_bytes(base64.b64decode(payload["database"], validate=True))
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("legacy control database failed integrity check")
            def statements():
                for sql in [*_sqlite_ddl(), *_index_ddl()]:
                    yield {"sql": sql, "params": []}
                for table in ("forge_meta", "audit_log", HTTP_TOKENS_TABLE, *DOCUMENT_TABLES):
                    cursor = connection.execute(f"SELECT * FROM {table}")
                    columns = ",".join(column[0] for column in cursor.description)
                    placeholders = ",".join("?" for _ in cursor.description)
                    for row in cursor:
                        yield {"sql": f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", "params": list(row)}

            client.commit_many(0, statements(), root=config)
        finally:
            connection.close()
    return config


class DurableObjectsRuntime:
    """Requests commit directly to DO storage; no per-request snapshots."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()

    def __call__(self, environ, start_response):
        with self.lock:
            return self.app(environ, start_response)


def build_app(root: Path, artifacts_store):
    from cloudflare_runtime import _read_snapshot, bootstrap_root
    from demo_server import create_app
    from state import locked, observe_state_commits
    from store import Store, open_store, write_backend_record

    client = CatalogClient()
    status = client.status()  # Storage outages never trigger a fresh bootstrap.
    config = status["root"]
    if config is None and status["revision"] == 0:
        payload, _version = _read_snapshot(artifacts_store)
        if payload is not None:
            config = import_snapshot(client, payload)
        elif artifacts_store.list_prefix(""):
            raise RuntimeError("Artifacts contains repository data without a control snapshot")
    elif config is None:
        # Schema initialization can commit before repository.json exists.
        # Resume only an empty initializing catalog, never orphaned data.
        from durable_store import DurableObjectsBackend
        store = Store(DurableObjectsBackend(root, client))
        try:
            meta = store.load_sections([])
        finally:
            store.close()
        if meta["repository_id"] is not None or artifacts_store.list_prefix(""):
            raise RuntimeError("Durable Object catalog data lacks repository configuration")
        shutil.rmtree(root, ignore_errors=True)

    if config is not None:
        # Everything on disk is reconstructible. In particular no live SQLite
        # database is restored, even when migrating an old snapshot.
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        (root / "repository.json").write_text(config["repository"])
        write_backend_record(root, {"backend": "durable_objects"})
        store = open_store(root)
        try:
            store.backend.begin_write(store.connection)
            store.connection.execute("DELETE FROM storage_locks")
            store.backend.commit(store.connection)
        finally:
            store.close()
    else:
        root.mkdir(parents=True, exist_ok=True)
        if any(root.iterdir()):
            raise RuntimeError("unrestored local root exists without Durable Object configuration")

    if config is None or config["phase"] == "bootstrapping":
        with observe_state_commits(root, lambda: _save_config(root, client, "bootstrapping")):
            bootstrap_root(root)
        _save_config(root, client, "ready")
    # Artifacts WAL remains repository truth. A crash between WAL publication
    # and catalog indexing is repaired before serving any authorization read.
    with locked(root):
        pass
    return DurableObjectsRuntime(create_app(root))
