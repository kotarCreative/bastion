"""DB-API adapter for the private Cloudflare Durable Object SQL binding.

Python holds only a transaction's parameterized statement log. Queries replay
that log in a DO transaction which is always rolled back; commit executes it
once atomically with a revision fence and durable retry receipt. No SQLite
database, account state, or SQL table snapshot is stored on Container disk.
"""

from __future__ import annotations

import json
import base64
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Iterable

from errors import ForgeError

DEFAULT_ENDPOINT = "http://catalog.bastion.internal"
MAX_RESPONSE = 8 * 1024 * 1024
FRAME_BYTES = 512 * 1024
STAGE_THRESHOLD = 1024 * 1024


def _encode(value) -> bytes:
    return json.dumps(value, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode()


class CatalogClient:
    def __init__(self, endpoint: str | None = None, *, token: str | None = None):
        self.endpoint = endpoint or os.environ.get("BASTION_CATALOG_ENDPOINT", DEFAULT_ENDPOINT)
        parsed = urllib.parse.urlsplit(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ForgeError("invalid Durable Object endpoint", "storage")
        # The virtual host is intercepted by ContainerProxy, never an HTTP
        # environment proxy. The endpoint is server configuration, not input.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.token = token  # Only used by the isolated authenticated live test.

    def call(self, path: str, body: dict) -> dict:
        data = _encode(body)
        if len(data) > MAX_RESPONSE:
            raise ForgeError("Durable Object transaction exceeds size limit", "storage")
        for attempt in range(2):
            headers = {"Content-Type": "application/json", "User-Agent": "bastion-container/1"}
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
            request = urllib.request.Request(
                self.endpoint.rstrip("/") + path, data=data,
                headers=headers, method="POST",
            )
            try:
                with self.opener.open(request, timeout=30) as response:
                    encoded = response.read(MAX_RESPONSE + 1)
                if len(encoded) > MAX_RESPONSE:
                    raise ForgeError("Durable Object result exceeds size limit", "storage")
                return json.loads(encoded)
            except urllib.error.HTTPError as error:
                error.close()
                if error.code == 409:
                    raise ForgeError("Durable Object transaction revision conflict", "storage") from None
                raise ForgeError("Durable Object storage unavailable", "storage") from None
            except (OSError, ValueError):
                # Only transport failures retry. Commits carry the same ID and
                # exact body, so a response lost after durable commit is safe.
                if attempt:
                    raise ForgeError("Durable Object storage unavailable", "storage") from None
        raise AssertionError("unreachable")

    def status(self) -> dict:
        return self.call("/status", {})

    def commit(self, revision: int, statements: list[dict], *, root: dict | None = None) -> int:
        body = {"id": uuid.uuid4().hex, "revision": revision, "statements": statements}
        if root is not None:
            body["root"] = root
        return self.call("/commit", body)["revision"]

    def commit_many(self, revision: int, statements: Iterable[dict], *, root: dict | None = None) -> int:
        transaction = StagedTransaction(self, revision)
        try:
            transaction.append(statements)
            return transaction.commit([], root=root)
        except BaseException:
            transaction.discard()
            raise


class StagedTransaction:
    """Immutable, digest-chained frames. Business tables change only at commit."""

    def __init__(self, client: CatalogClient, revision: int):
        self.client = client
        self.revision = revision
        self.reference = {"id": uuid.uuid4().hex, "count": 0, "digest": ""}

    def _send(self, frame: bytes):
        self.reference = self.client.call("/stage", {
            "id": self.reference["id"], "revision": self.revision,
            "sequence": self.reference["count"], "previous": self.reference["digest"],
            "chunk": base64.b64encode(frame).decode("ascii"),
        })

    def append(self, statements: Iterable[dict]):
        frame = bytearray()
        for statement in statements:
            encoded = _encode(statement) + b"\n"
            offset = 0
            while offset < len(encoded):
                length = min(FRAME_BYTES - len(frame), len(encoded) - offset)
                frame.extend(encoded[offset:offset + length])
                offset += length
                if len(frame) == FRAME_BYTES:
                    self._send(bytes(frame))
                    frame.clear()
        if frame:
            self._send(bytes(frame))

    def commit(self, statements: list[dict], *, root: dict | None = None) -> int:
        body = {"id": self.reference["id"], "revision": self.revision, "statements": statements}
        if self.reference["count"]:
            body["staged"] = self.reference
        if root is not None:
            body["root"] = root
        return self.client.call("/commit", body)["revision"]

    def discard(self):
        try:
            self.client.call("/discard", {"id": self.reference["id"]})
        except ForgeError:
            # The DO alarm collects abandoned frames after 24 hours even if
            # an outage prevents explicit cleanup. Preserve the original error.
            pass


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self.rowcount = -1
        self.rows = []

    def execute(self, statement: str, parameters=()):
        current = {"sql": statement, "params": list(parameters)}
        writing = not statement.lstrip().upper().startswith("SELECT")
        if writing and not self.connection.writable:
            raise ForgeError("Durable Object write requires a writable transaction", "storage")
        current_size = len(_encode(current))
        revision = self.connection.revision
        if revision is None:
            revision = self.connection.client.status()["revision"]
        if self.connection.pending and self.connection.pending_size + current_size > STAGE_THRESHOLD:
            self.connection.flush_pending()
        body = {
            "revision": revision, "pending": self.connection.pending,
            "statement": current,
        }
        if self.connection.staged:
            body["staged"] = self.connection.staged.reference
        result = self.connection.client.call("/query", body)
        self.rows = list(result["rows"])
        while result.get("more", False):
            body["offset"] = len(self.rows)
            result = self.connection.client.call("/query", body)
            self.rows.extend(result["rows"])
        self.description = [(name, None, None, None, None, None, None) for name in result["columns"]]
        self.rowcount = result["rowcount"]
        if writing:
            self.connection.pending.append(current)
            self.connection.pending_size += current_size
        return self

    def fetchall(self):
        return self.rows

    def close(self):
        self.rows = []


class _Connection:
    def __init__(self, client: CatalogClient):
        self.client = client
        self.revision = None
        self.pending = []
        self.writable = False
        self.root_missing = True
        self.pending_size = 0
        self.staged = None

    def flush_pending(self):
        if self.staged is None:
            self.staged = StagedTransaction(self.client, self.revision)
        self.staged.append(self.pending)
        self.pending = []
        self.pending_size = 0

    def cursor(self):
        return _Cursor(self)

    def execute(self, statement, parameters=()):
        return self.cursor().execute(statement, parameters)

    def close(self):
        if self.staged:
            self.staged.discard()
            self.staged = None
        self.pending = []
        self.pending_size = 0
        self.revision = None
        self.writable = False


class DurableObjectsBackend:
    kind = "durable_objects"
    param = "?"

    def __init__(self, root: Path, client: CatalogClient | None = None):
        self._root = root
        self.client = client or CatalogClient()

    def connect(self):
        return _Connection(self.client)

    def shutdown(self, connection):
        connection.close()

    def begin(self, connection):
        self.begin_write(connection)

    def _begin(self, connection, writable):
        if connection.revision is not None:
            raise ForgeError("Durable Object transaction already active", "storage")
        status = self.client.status()
        connection.revision = status["revision"]
        connection.root_missing = status["root"] is None
        connection.pending = []
        connection.writable = writable

    def begin_write(self, connection):
        self._begin(connection, True)

    def begin_read(self, connection):
        self._begin(connection, False)

    def commit(self, connection):
        if connection.revision is None:
            raise ForgeError("Durable Object transaction is not active", "storage")
        if connection.pending or connection.staged:
            root = None
            config_path = self._root / "repository.json"
            if connection.root_missing and config_path.is_file():
                # The first catalog data commit includes its configuration.
                # A crash before the state-commit observer cannot orphan it.
                root = {"repository": config_path.read_text(), "phase": "bootstrapping"}
            if connection.staged:
                connection.staged.commit(connection.pending, root=root)
                connection.staged = None
            else:
                self.client.commit(connection.revision, connection.pending, root=root)
        self.rollback(connection)

    def rollback(self, connection):
        connection.close()

    def create_schema(self, connection):
        from store import _sqlite_ddl
        for statement in _sqlite_ddl():
            connection.execute(statement)

    def size_bytes(self):
        return self.client.status()["size"]

    def dump(self, destination: Path):
        """Export a consistent portable SQLite backup, never a live database."""
        import sqlite3
        from store import _sqlite_ddl, _index_ddl, DOCUMENT_TABLES, HTTP_TOKENS_TABLE
        revision = self.client.status()["revision"]
        target = sqlite3.connect(destination)
        try:
            for statement in [*_sqlite_ddl(), *_index_ddl()]:
                target.execute(statement)
            for table in ("forge_meta", "audit_log", HTTP_TOKENS_TABLE, *DOCUMENT_TABLES):
                request = {"revision": revision, "table": table}
                while True:
                    result = self.client.call("/export", request)
                    if result["done"]:
                        break
                    columns = ",".join(result["columns"])
                    placeholders = ",".join("?" for _ in result["columns"])
                    target.executemany(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", result["rows"])
                    request["after"] = result["after"]
            target.commit()
        except BaseException:
            target.close()
            destination.unlink(missing_ok=True)
            raise
        finally:
            target.close()
