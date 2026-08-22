"""Durable document store with SQLite and PostgreSQL backends.

Implements `.specs/009-production-transaction-storage.md`: every aggregate is
a table of JSON documents carrying an optimistic `version` and a SHA-256
`checksum`; sessions hold lease rows instead of filesystem locks; saves are
short transactions guarded by version checks; backups, restore, idempotency
records, audit entries, and observability snapshots live here.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import urllib.parse
import uuid
from pathlib import Path

from errors import ForgeError

STORAGE_SCHEMA_VERSION = 1
LEASE_TTL_SECONDS = 60.0
LEASE_WAIT_SECONDS = 30.0
LEASE_POLL_SECONDS = 0.05

DOCUMENT_TABLES = (
    "principals",
    "teams",
    "roles",
    "role_bindings",
    "view_descriptors",
    "views",
    "transactions",
    "changes",
    "outbox_events",
    "projection_jobs",
)

_STATE_SECTIONS = {
    "principals": "principals",
    "teams": "teams",
    "roles": "roles",
    "role_bindings": "role_bindings",
    "view_descriptors": "view_descriptors",
    "views": "views",
    "transactions": "transactions",
    "changes": "changes",
    "outbox": "outbox_events",
    "projection_jobs": "projection_jobs",
}


def _checksum(document: dict) -> str:
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def backend_record_path(root: Path) -> Path:
    return root / "backend.json"


def write_backend_record(root: Path, record: dict) -> None:
    target = backend_record_path(root)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, target)


def read_backend_record(root: Path) -> dict:
    try:
        record = json.loads(backend_record_path(root).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ForgeError(f"cannot read forge backend record: {error}") from error
    kind = record.get("backend")
    if kind not in {"sqlite", "postgres"}:
        raise ForgeError(f"unknown storage backend: {kind!r}")
    if kind == "postgres" and not record.get("dsn"):
        raise ForgeError("postgres backend record lacks a dsn")
    return record


def database_uri(root: Path, database: str | None = None) -> str:
    if database:
        return database
    return os.environ.get("BASTION_DATABASE", "")


def _sqlite_ddl() -> list[str]:
    return [
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS storage_locks (
            scope TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            heartbeat_at REAL NOT NULL,
            expires_at REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS forge_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            doc TEXT NOT NULL,
            version INTEGER NOT NULL,
            checksum TEXT NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS audit_log (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            subject TEXT NOT NULL,
            detail TEXT NOT NULL,
            at REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS idempotency_keys (
            key TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            outcome TEXT NOT NULL,
            recorded_at REAL NOT NULL)""",
    ] + [
        f"""CREATE TABLE IF NOT EXISTS {table} (
            key TEXT PRIMARY KEY,
            doc TEXT NOT NULL,
            version INTEGER NOT NULL,
            checksum TEXT NOT NULL)"""
        for table in DOCUMENT_TABLES
    ]


def _postgres_ddl() -> str:
    statements = [
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at DOUBLE PRECISION NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS forge_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            doc TEXT NOT NULL,
            version INTEGER NOT NULL,
            checksum TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS storage_locks (
            scope TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            heartbeat_at DOUBLE PRECISION NOT NULL,
            expires_at DOUBLE PRECISION NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS audit_log (
            seq BIGSERIAL PRIMARY KEY,
            kind TEXT NOT NULL,
            subject TEXT NOT NULL,
            detail TEXT NOT NULL,
            at DOUBLE PRECISION NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS idempotency_keys (
            key TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            outcome TEXT NOT NULL,
            recorded_at DOUBLE PRECISION NOT NULL)""",
    ] + [
        f"""CREATE TABLE IF NOT EXISTS {table} (
            key TEXT PRIMARY KEY,
            doc TEXT NOT NULL,
            version INTEGER NOT NULL,
            checksum TEXT NOT NULL)"""
        for table in DOCUMENT_TABLES
    ]
    return ";\n".join(statements)


class _SqliteBackend:
    kind = "sqlite"
    param = "?"

    def __init__(self, path: Path):
        import sqlite3

        self._path = path
        self._sqlite3 = sqlite3
        self._connection: sqlite3.Connection | None = None

    def connect(self):
        connection = self._sqlite3.connect(
            self._path, timeout=30.0, isolation_level=None
        )
        connection.row_factory = self._sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        self._connection = connection
        return connection

    def shutdown(self, connection) -> None:
        connection.close()

    def begin(self, connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    def commit(self, connection) -> None:
        connection.execute("COMMIT")

    def rollback(self, connection) -> None:
        try:
            connection.execute("ROLLBACK")
        except self._sqlite3.Error:
            pass

    def create_schema(self, connection) -> None:
        for statement in _sqlite_ddl():
            connection.execute(statement)

    def dump(self, destination: Path) -> None:
        source = self._sqlite3.connect(self._path)
        try:
            target = self._sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()

    def restore(self, backup_file: Path) -> None:
        shutil.copyfile(backup_file, self._path)

    def size_bytes(self) -> int:
        main = self._path.stat().st_size if self._path.exists() else 0
        wal = self._path.with_suffix(".sqlite3-wal")
        extra = wal.stat().st_size if wal.exists() else 0
        return main + extra


class _PostgresBackend:
    kind = "postgres"
    param = "%s"

    def __init__(self, dsn: str, root: Path | None = None):
        self._dsn = self._scoped_dsn(dsn, root)

    @staticmethod
    def _scoped_dsn(dsn: str, root: Path | None) -> str:
        """Give every forge root its own database on the shared server."""
        if root is None:
            return dsn
        digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
        parts = urllib.parse.urlsplit(dsn)
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, f"/bastion_{digest}", parts.query, "")
        )

    def _ensure_database(self) -> None:
        try:
            import psycopg
        except ImportError as error:
            raise ForgeError(
                'the postgres backend requires psycopg 3: pip install "psycopg[binary]"'
            ) from error
        parts = urllib.parse.urlsplit(self._dsn)
        dbname = parts.path.lstrip("/")
        admin = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, "/postgres", parts.query, "")
        )
        try:
            connection = psycopg.connect(admin, autocommit=True)
        except Exception as error:
            raise ForgeError(f"cannot connect to postgres: {error}") from error
        try:
            connection.execute(f'CREATE DATABASE "{dbname}"')
        except Exception as error:
            if "42P04" not in str(getattr(error, "sqlstate", "")) and "already exists" not in str(error):
                self.close_connection(connection)
                raise ForgeError(f"cannot create postgres database: {error}") from error
        finally:
            self.close_connection(connection)

    def connect(self):
        try:
            import psycopg
        except ImportError as error:
            raise ForgeError(
                'the postgres backend requires psycopg 3: pip install "psycopg[binary]"'
            ) from error
        self._ensure_database()
        try:
            connection = psycopg.connect(self._dsn, autocommit=False)
        except Exception as error:
            raise ForgeError(f"cannot connect to postgres: {error}") from error
        return connection

    def shutdown(self, connection) -> None:
        connection.close()

    def begin(self, connection) -> None:
        pass
    def commit(self, connection) -> None:
        connection.commit()

    def rollback(self, connection) -> None:
        try:
            connection.rollback()
        except Exception:
            pass

    def create_schema(self, connection) -> None:
        with connection.cursor() as cursor:
            cursor.execute(_postgres_ddl())

    def dump(self, destination_dir: Path) -> None:
        destination_dir.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            tables = [("forge_meta", "id AS key"), *[(t, "key") for t in DOCUMENT_TABLES]]
            for table, key_expression in tables:
                with connection.cursor(
                    name=f"bastion_dump_{table}"
                ) as cursor:
                    cursor.itersize = 1000
                    cursor.execute(
                        f"SELECT {key_expression}, doc, version, checksum FROM {table}"
                    )
                    with (destination_dir / f"{table}.ndjson").open("w") as output:
                        for key, doc, version, checksum in cursor:
                            output.write(
                                json.dumps(
                                    {
                                        "key": key,
                                        "doc": json.loads(doc),
                                        "version": version,
                                        "checksum": checksum,
                                    },
                                    sort_keys=True,
                                )
                                + "\n"
                            )
            with connection.cursor(name="bastion_dump_locks") as cursor:
                cursor.execute(
                    "SELECT scope, owner, heartbeat_at, expires_at FROM storage_locks"
                )
                with (destination_dir / "storage_locks.ndjson").open("w") as output:
                    for row in cursor:
                        output.write(json.dumps(row, sort_keys=True) + "\n")
        finally:
            self.close_connection(connection)

    def close_connection(self, connection) -> None:
        connection.close()

    def size_bytes(self) -> int:
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_database_size(current_database())")
                return int(cursor.fetchone()[0])
        finally:
            self.close_connection(connection)

    def restore(self, tables_dir: Path) -> None:
        connection = self.connect()
        try:
            self.create_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM schema_migrations")
                cursor.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, %s)",
                    (STORAGE_SCHEMA_VERSION, time.time()),
                )
            self.commit(connection)
            for ndjson in sorted(tables_dir.glob("*.ndjson")):
                table = ndjson.stem
                if table == "storage_locks":
                    continue
                with connection.cursor() as cursor:
                    cursor.execute(f"DELETE FROM {table}")
                    for line in ndjson.read_text().splitlines():
                        if not line.strip():
                            continue
                        row = json.loads(line)
                        if table == "forge_meta":
                            cursor.execute(
                                "INSERT INTO forge_meta (id, doc, version, checksum) "
                                "VALUES (%s, %s, %s, %s)",
                                (
                                    1,
                                    json.dumps(row["doc"], sort_keys=True),
                                    row["version"],
                                    row["checksum"],
                                ),
                            )
                        else:
                            cursor.execute(
                                f"INSERT INTO {table} (key, doc, version, checksum) "
                                f"VALUES (%s, %s, %s, %s)",
                                (
                                    row["key"],
                                    json.dumps(row["doc"], sort_keys=True),
                                    row["version"],
                                    row["checksum"],
                                ),
                            )
            self.commit(connection)
        except Exception:
            self.rollback(connection)
            raise
        finally:
            self.close_connection(connection)


def _owner_alive(owner: str) -> bool:
    """True when the lease owner token's process is still running.

    Owner tokens embed ``:<pid>:<start-time>`` so a crashed holder's leases
    become stealable immediately instead of waiting for TTL expiry.
    """
    try:
        _token, pid_text, start = owner.rsplit(":", 2)
        pid = int(pid_text)
    except ValueError:
        return True
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
    except OSError:
        return False
    return len(fields) > 21 and fields[21] == start


def process_identity() -> str:
    """Return this process's ``pid:start`` identity for lease ownership."""
    fields = Path(f"/proc/{os.getpid()}/stat").read_text().split()
    return f"{os.getpid()}:{fields[21]}"


class Store:
    """One open connection to a forge's durable state."""

    def __init__(self, backend: _SqliteBackend | _PostgresBackend):
        self.backend = backend
        self.connection = backend.connect()

    def close(self) -> None:
        self.backend.shutdown(self.connection)

    def bootstrap(self) -> None:
        self.backend.begin(self.connection)
        try:
            self.backend.create_schema(self.connection)
            rows = self._fetchall("SELECT version FROM schema_migrations", ())
            versions = {row["version"] for row in rows}
            if not versions:
                self._insert_placeholder_free(
                    "INSERT INTO schema_migrations (version, applied_at)",
                    (STORAGE_SCHEMA_VERSION, time.time()),
                )
                self._save_table(
                    "forge_meta",
                    {"__meta__": {"canonical_branch": None}},
                    id_column=True,
                )
            elif versions != {STORAGE_SCHEMA_VERSION}:
                found = ", ".join(str(item) for item in sorted(versions))
                raise ForgeError(
                    f"storage schema version {found} is incompatible with this build"
                )
            self.backend.commit(self.connection)
        except ForgeError:
            self.backend.rollback(self.connection)
            raise
        except Exception as error:
            self.backend.rollback(self.connection)
            raise ForgeError(f"cannot initialize storage schema: {error}") from error

    def _insert_placeholder_free(self, head: str, values: tuple) -> None:
        placeholders = ", ".join([self.backend.param] * len(values))
        self._execute(f"{head} VALUES ({placeholders})", tuple(values))
        return None

    def _dialect_sql(self, statement: str) -> str:
        if self.backend.param == "?":
            return statement
        return statement.replace("?", "%s")

    def _cursor_rows(self, statement: str, parameters: tuple) -> list[dict]:
        cursor = self.connection.cursor()
        try:
            cursor.execute(self._dialect_sql(statement), parameters)
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def _rowcount(self, statement: str, parameters: tuple) -> int:
        cursor = self.connection.cursor()
        try:
            cursor.execute(self._dialect_sql(statement), parameters)
            return cursor.rowcount
        finally:
            cursor.close()

    def _execute(self, statement: str, parameters: tuple) -> int:
        return self._rowcount(statement, parameters)

    def _fetchone(self, statement: str, parameters: tuple) -> dict | None:
        rows = self._cursor_rows(statement, parameters)
        return rows[0] if rows else None

    def _fetchall(self, statement: str, parameters: tuple) -> list[dict]:
        return self._cursor_rows(statement, parameters)

    def load_document(self) -> dict:
        """Materialize the legacy nested state dict, verifying checksums."""
        self.backend.begin(self.connection)
        try:
            meta_rows = self._fetchall(
                "SELECT doc, version, checksum FROM forge_meta WHERE id = ?", (1,)
            )
            if len(meta_rows) > 1:
                raise ForgeError("forge meta table is corrupt")
            document: dict = {}
            if meta_rows:
                meta = meta_rows[0]
                parsed = json.loads(meta["doc"])
                if _checksum(parsed) != meta["checksum"]:
                    raise ForgeError("forge meta row failed its integrity checksum")
                document.update(parsed)
            document.setdefault("canonical_branch", None)
            document.setdefault("next_transaction_sequence", 1)
            for section, table in _STATE_SECTIONS.items():
                rows = self._fetchall(
                    f"SELECT key, doc, checksum FROM {table}", ()
                )
                bucket: dict = {}
                for row in sorted(rows, key=lambda item: item["key"]):
                    parsed = json.loads(row["doc"])
                    if _checksum(parsed) != row["checksum"]:
                        raise ForgeError(
                            f"{table}.{row['key']} failed its integrity checksum"
                        )
                    bucket[row["key"]] = parsed
                document[section] = bucket
            if document.get("canonical_branch") is None:
                raise ForgeError("forge state is not initialized")
            self.backend.rollback(self.connection)
            return document
            return document
        except json.JSONDecodeError as error:
            self.backend.rollback(self.connection)
            raise ForgeError(f"stored documents are not valid JSON: {error}") from error
        except ForgeError:
            self.backend.rollback(self.connection)
            raise
        except Exception as error:
            self.backend.rollback(self.connection)
            raise ForgeError(f"cannot read forge state: {error}") from error

    def save_document(
        self,
        state: dict,
        leases: list[tuple[str, str]],
        failpoint_root: Path | None,
        baseline: dict | None = None,
    ) -> None:
        """Persist changed aggregates in one short optimistic transaction.

        ``baseline`` is the document this session loaded (post-recovery);
        rows present there but absent from ``state`` are deleted. Rows that
        appeared concurrently under other scopes are never touched.
        """
        self.backend.begin(self.connection)
        try:
            now = time.time()
            for scope, owner in leases:
                updated = self._execute(
                    "UPDATE storage_locks SET heartbeat_at = "
                    + f"{self.backend.param}, expires_at = {self.backend.param} "
                    + f"WHERE scope = {self.backend.param} AND owner = {self.backend.param}",
                    (now, now + LEASE_TTL_SECONDS, scope, owner),
                )
                if updated != 1:
                    raise ForgeError(f"storage lease lost: {scope}")
            desired_meta = {
                "canonical_branch": state.get("canonical_branch") or "",
                "next_transaction_sequence": state.get(
                    "next_transaction_sequence", 1
                ),
            }
            self._save_table(
                "forge_meta",
                {"__meta__": desired_meta},
                id_column=True,
                baseline_section=(
                    {
                        "__meta__": {
                            "canonical_branch": baseline.get("canonical_branch"),
                            "next_transaction_sequence": baseline.get(
                                "next_transaction_sequence", 1
                            ),
                        }
                    }
                    if baseline is not None
                    else None
                ),
            )
            for section, table in _STATE_SECTIONS.items():
                deletable = None
                baseline_section = None
                if baseline is not None:
                    deletable = set(baseline.get(section, {}))
                    baseline_section = baseline.get(section, {})
                self._save_table(
                    table,
                    state.get(section, {}),
                    False,
                    deletable,
                    baseline_section,
                )
            if failpoint_root is not None:
                from failpoints import checkpoint

                checkpoint(failpoint_root, "state.temporary_fsynced")
            self.backend.commit(self.connection)
            if failpoint_root is not None:
                from failpoints import checkpoint

                checkpoint(failpoint_root, "state.replaced")
                checkpoint(failpoint_root, "state.directory_fsynced")
        except ForgeError:
            self.backend.rollback(self.connection)
            raise
        except Exception as error:
            self.backend.rollback(self.connection)
            raise ForgeError(f"cannot persist forge state: {error}") from error

    def _save_table(
        self,
        table: str,
        desired: dict,
        id_column: bool = False,
        deletable_keys: set[str] | None = None,
        baseline_section: dict | None = None,
    ) -> None:
        if id_column:
            seeded = self._fetchall(
                f"SELECT version, checksum FROM {table} WHERE id = 1", ()
            )
            existing = (
                {"__meta__": (seeded[0]["version"], seeded[0]["checksum"])}
                if seeded
                else {}
            )
        else:
            existing = {
                row["key"]: (row["version"], row["checksum"])
                for row in self._fetchall(
                    f"SELECT key, version, checksum FROM {table}", ()
                )
            }
        if id_column:
            desired_items: list[tuple[str, dict]] = [
                ("__meta__", next(iter(desired.values())))
            ]
        else:
            desired_items = sorted(desired.items())
        # A scoped session writes only rows it actually mutated relative to
        # its post-recovery baseline; untouched sibling rows owned by other
        # scopes are never rewritten with stale snapshot content.
        if baseline_section is not None:
            desired_items = [
                (key, document)
                for key, document in desired_items
                if key == "__meta__"
                or document != baseline_section.get(key)
                or key not in baseline_section
            ]
        for key, document in desired_items:
            digest = _checksum(document)
            encoded = json.dumps(document, sort_keys=True)
            current = existing.get(key)
            if current is None:
                if id_column:
                    self._insert_placeholder_free(
                        "INSERT INTO forge_meta (id, doc, version, checksum)",
                        (1, encoded, 1, digest),
                    )
                else:
                    self._insert_placeholder_free(
                        f"INSERT INTO {table} (key, doc, version, checksum)",
                        (key, encoded, 1, digest),
                    )
                continue
            old_version, old_checksum = current
            if old_checksum == digest:
                continue
            match_clause = "id = 1" if id_column else f"key = {self.backend.param}"
            values = (
                (encoded, old_version + 1, digest)
                if id_column
                else (encoded, old_version + 1, digest, key)
            )
            updated = self._execute(
                f"UPDATE {table} SET doc = {self.backend.param}, "
                f"version = {self.backend.param}, checksum = {self.backend.param} "
                f"WHERE {match_clause} AND version = {self.backend.param}",
                (*values, old_version),
            )
            if updated != 1:
                raise ForgeError(f"optimistic concurrency conflict on {table}.{key}")
        if id_column:
            return
        # Rows vanish only when this session observed them at load time and
        # they are gone now; rows added concurrently under other scopes stay.
        doomed = (
            set(existing)
            if deletable_keys is None
            else set(existing) & set(deletable_keys)
        ) - set(desired)
        for key in sorted(doomed):
            self._execute(
                f"DELETE FROM {table} WHERE key = {self.backend.param}", (key,)
            )

    # -- leases -------------------------------------------------------------

    def acquire_leases(self, scopes: list[str], owner: str) -> float:
        deadline = time.time() + LEASE_WAIT_SECONDS
        upsert = (
            "INSERT INTO storage_locks (scope, owner, heartbeat_at, expires_at) "
            f"VALUES ({self.backend.param}, {self.backend.param}, {self.backend.param}, {self.backend.param}) "
            "ON CONFLICT (scope) DO UPDATE SET owner = EXCLUDED.owner, "
            "heartbeat_at = EXCLUDED.heartbeat_at, expires_at = EXCLUDED.expires_at"
        )
        while True:
            self.backend.begin(self.connection)
            try:
                now = time.time()
                busy: list[str] = []
                for scope in scopes:
                    row = self._fetchone(
                        "SELECT owner, expires_at FROM storage_locks "
                        f"WHERE scope = {self.backend.param}",
                        (scope,),
                    )
                    if (
                        row is None
                        or row["expires_at"] <= now
                        or not _owner_alive(row["owner"])
                    ):
                        self._execute(upsert, (scope, owner, now, now + LEASE_TTL_SECONDS))
                    else:
                        busy.append(scope)
                if not busy:
                    self.backend.commit(self.connection)
                    return now + LEASE_TTL_SECONDS
                self.backend.rollback(self.connection)
            except Exception as error:
                self.backend.rollback(self.connection)
                message = str(error).lower()
                transient = "locked" in message or "serialization" in message
                if not transient or time.time() >= deadline:
                    raise ForgeError(
                        f"cannot acquire storage lease: {error}"
                    ) from error
            if time.time() >= deadline:
                break
            time.sleep(LEASE_POLL_SECONDS)
        requested = ", ".join(scopes)
        raise ForgeError(f"could not acquire storage lease within deadline: {requested}")

    def verify_leases(self, scopes: list[str], owner: str) -> None:
        for scope in scopes:
            row = self._fetchone(
                "SELECT owner FROM storage_locks WHERE scope = "
                f"{self.backend.param}",
                (scope,),
            )
            if row is None or row["owner"] != owner:
                raise ForgeError(f"storage lease lost: {scope}")

    def scopes_for_owner(self, owner: str) -> list[str]:
        return [
            row["scope"]
            for row in self._fetchall(
                "SELECT scope FROM storage_locks WHERE owner = "
                f"{self.backend.param} ORDER BY scope",
                (owner,),
            )
        ]

    def heartbeat_lease(self, scope: str, owner: str, expires_at: float) -> None:
        updated = self._execute(
            "UPDATE storage_locks SET heartbeat_at = "
            + f"{self.backend.param}, expires_at = {self.backend.param} "
            + f"WHERE scope = {self.backend.param} AND owner = {self.backend.param}",
            (time.time(), expires_at, scope, owner),
        )
        if updated != 1:
            raise ForgeError(f"storage lease lost: {scope}")

    def release_leases(self, scopes: list[str], owner: str) -> None:
        self.backend.begin(self.connection)
        try:
            for scope in scopes:
                self._execute(
                    f"DELETE FROM storage_locks WHERE scope = {self.backend.param} "
                    f"AND owner = {self.backend.param}",
                    (scope, owner),
                )
            self.backend.commit(self.connection)
        except Exception as error:
            self.backend.rollback(self.connection)
            raise ForgeError(f"cannot release storage lease: {error}") from error

    # -- idempotency --------------------------------------------------------

    def lookup_idempotency(self, key: str) -> dict | None:
        row = self._fetchone(
            "SELECT fingerprint, outcome FROM idempotency_keys WHERE key = "
            f"{self.backend.param}",
            (key,),
        )
        if row is None:
            return None
        return {
            "fingerprint": json.loads(row["fingerprint"]),
            "outcome": json.loads(row["outcome"]),
        }

    def record_idempotency(self, key: str, fingerprint: dict, outcome: dict) -> None:
        self.backend.begin(self.connection)
        try:
            existing = self._fetchone(
                "SELECT fingerprint FROM idempotency_keys WHERE key = "
                f"{self.backend.param}",
                (key,),
            )
            if existing is not None:
                if json.loads(existing["fingerprint"]) != fingerprint:
                    raise ForgeError(
                        "idempotency key was already used for a different request"
                    )
                self.backend.commit(self.connection)
                return
            self._execute(
                "INSERT INTO idempotency_keys (key, fingerprint, outcome, recorded_at) "
                f"VALUES ({self.backend.param}, {self.backend.param}, {self.backend.param}, {self.backend.param})",
                (
                    key,
                    json.dumps(fingerprint, sort_keys=True),
                    json.dumps(outcome, sort_keys=True),
                    time.time(),
                ),
            )
            self.backend.commit(self.connection)
        except ForgeError:
            self.backend.rollback(self.connection)
            raise
        except Exception as error:
            self.backend.rollback(self.connection)
            raise ForgeError(f"cannot record idempotency key: {error}") from error

    # -- audit and observability --------------------------------------------

    def audit(self, kind: str, subject: str, detail: str) -> None:
        self.backend.begin(self.connection)
        try:
            self._execute(
                "INSERT INTO audit_log (kind, subject, detail, at) VALUES "
                f"({self.backend.param}, {self.backend.param}, {self.backend.param}, {self.backend.param})",
                (kind, subject, detail, time.time()),
            )
            self.backend.commit(self.connection)
        except Exception as error:
            self.backend.rollback(self.connection)
            raise ForgeError(f"cannot append audit entry: {error}") from error

    def recent_audit(self, limit: int = 20) -> list[dict]:
        rows = self._fetchall(
            "SELECT seq, kind, subject, detail, at FROM audit_log "
            f"ORDER BY seq DESC LIMIT {int(limit)}",
            (),
        )
        return rows

    def stats(self) -> dict:
        counts: dict[str, dict[str, int]] = {}
        for label, table in (("transactions", "transactions"), ("events", "outbox_events"), ("jobs", "projection_jobs")):
            rows = self._fetchall(f"SELECT doc FROM {table}", ())
            bucket: dict[str, int] = {}
            for row in rows:
                status = json.loads(row["doc"]).get("status", "?")
                bucket[status] = bucket.get(status, 0) + 1
            counts[label] = dict(sorted(bucket.items()))
        leases = self._fetchall(
            "SELECT scope, owner, expires_at FROM storage_locks ORDER BY scope", ()
        )
        return {
            "backend": self.backend.kind,
            "schema_version": STORAGE_SCHEMA_VERSION,
            "counts": counts,
            "leases": [
                {
                    "scope": row["scope"],
                    "owner": row["owner"],
                    "expires_in": round(row["expires_at"] - time.time(), 3),
                }
                for row in leases
            ],
            "dead_letters": counts.get("jobs", {}).get("dead", 0),
            "size_bytes": self.backend.size_bytes(),
        }


def open_store(root: Path) -> Store:
    record = read_backend_record(root)
    if record["backend"] == "sqlite":
        return Store(_SqliteBackend(root / "store.sqlite3"))
    return Store(_PostgresBackend(record["dsn"], root))


def initialize_store(root: Path, database: str | None = None) -> Store:
    """Create the backend record and empty schema for a fresh or migrated forge."""
    uri = database_uri(root, database)
    if uri.startswith("postgres://") or uri.startswith("postgresql://"):
        record = {"backend": "postgres", "dsn": uri}
    elif uri in ("", "sqlite"):
        record = {"backend": "sqlite"}
    else:
        raise ForgeError(f"unsupported database URI: {uri!r}")
    store = (
        Store(_SqliteBackend(root / "store.sqlite3"))
        if record["backend"] == "sqlite"
        else Store(_PostgresBackend(record["dsn"], root))
    )
    store.bootstrap()
    write_backend_record(root, record)
    return store


def backup_forge(root: Path, destination: Path) -> Path:
    """Write a consistent backup directory and return its path."""
    import git as forge_git

    store = open_store(root)
    document = store.load_document()
    destination.mkdir(parents=True, exist_ok=False)
    try:
        manifest = {
            "created_at": time.time(),
            "schema_version": STORAGE_SCHEMA_VERSION,
            "backend": store.backend.kind,
            "document": document,
            "refs": {},
        }
        canonical = root / "canonical.git"
        repos_dir = destination / "repos"
        repos_dir.mkdir()
        shutil.copytree(canonical, repos_dir / "canonical.git", symlinks=True)
        views_dir = root / "views"
        if views_dir.exists():
            shutil.copytree(views_dir, repos_dir / "views", symlinks=True)
        for name in sorted(document.get("views", {})):
            view_repo = repos_dir / "views" / f"{name}.git"
            projected_ref = document["views"][name]["projected_ref"]
            manifest["refs"][f"view:{name}"] = forge_git.ref(view_repo, projected_ref)
        canonical_ref = f"refs/heads/{document['canonical_branch']}"
        manifest["refs"]["canonical"] = forge_git.ref(canonical, canonical_ref)
        if store.backend.kind == "sqlite":
            store.backend.dump(destination / "database.sqlite3")
        else:
            store.backend.dump(destination / "tables")
        (destination / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        store.close()
    return destination


def restore_forge(backup: Path, new_root: Path, database: str | None = None) -> Path:
    """Reconstruct a forge from a backup directory into ``new_root``."""
    manifest_path = backup / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ForgeError(f"cannot read backup manifest: {error}") from error
    if manifest.get("schema_version") != STORAGE_SCHEMA_VERSION:
        raise ForgeError(
            f"backup schema version {manifest.get('schema_version')} is unsupported"
        )
    if new_root.exists() and any(new_root.iterdir()):
        raise ForgeError(f"restore target is not empty: {new_root}")
    new_root.mkdir(parents=True)
    (new_root / "views").mkdir(exist_ok=True)
    shutil.copytree(backup / "repos" / "canonical.git", new_root / "canonical.git", symlinks=True)
    if (backup / "repos" / "views").exists():
        shutil.copytree(
            backup / "repos" / "views", new_root / "views", symlinks=True, dirs_exist_ok=True
        )
    uri = database_uri(new_root, database)
    if uri.startswith("postgres://") or uri.startswith("postgresql://"):
        record = {"backend": "postgres", "dsn": uri}
    else:
        record = {"backend": "sqlite"}
    if record["backend"] == "sqlite":
        fresh = _SqliteBackend(new_root / "store.sqlite3")
        connection = fresh.connect()
        try:
            fresh.create_schema(connection)
        finally:
            fresh.shutdown(connection)
        write_backend_record(new_root, record)
        _SqliteBackend(new_root / "store.sqlite3").restore(backup / "database.sqlite3")
    else:
        backend = _PostgresBackend(record["dsn"], new_root)
        backend.restore(backup / "tables")
        write_backend_record(new_root, record)
    restored = open_store(new_root)
    try:
        document = restored.load_document()
    finally:
        restored.close()
    expected = manifest.get("document")
    loaded = json.loads(json.dumps(document, sort_keys=True))
    normalized = json.loads(json.dumps(expected, sort_keys=True)) if expected else None
    if normalized is not None and loaded != normalized:
        raise ForgeError("restored document differs from the backup manifest")
    import git as forge_git

    for marker, tip in sorted(manifest.get("refs", {}).items()):
        if marker == "canonical":
            actual = forge_git.ref(
                new_root / "canonical.git",
                f"refs/heads/{document['canonical_branch']}",
            )
        else:
            name = marker.split(":", 1)[1]
            actual = forge_git.ref(
                new_root / "views" / f"{name}.git",
                document["views"][name]["projected_ref"],
            )
        if actual != tip:
            raise ForgeError(f"restored ref differs from the backup manifest: {marker}")
    return new_root
