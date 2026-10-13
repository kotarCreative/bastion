"""Explicit portable-catalog migration utility, not a single-repo app runtime."""
from __future__ import annotations

import base64
import sqlite3
import tempfile
from pathlib import Path

from durable_store import CatalogClient


def import_snapshot(client: CatalogClient, payload: dict) -> dict:
    """Import a portable SQLite backup into an unused repository catalog.

    Existing singleton catalogs are never automatically reinterpreted as hosts.
    Operators must explicitly select an unused destination catalog.
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
