"""Durable control-plane store with SQLite and PostgreSQL backends."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
import urllib.parse
import uuid
from pathlib import Path

from errors import ForgeError

STORAGE_SCHEMA_VERSION = 2
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
        """CREATE TABLE IF NOT EXISTS storage_schema (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL,
            initialized_at REAL NOT NULL)""",
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
            at REAL NOT NULL,
            event_id TEXT NULL)""",
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
        """CREATE TABLE IF NOT EXISTS storage_schema (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL,
            initialized_at DOUBLE PRECISION NOT NULL)""",
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
            at DOUBLE PRECISION NOT NULL,
            event_id TEXT NULL)""",
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
                cursor.execute("DELETE FROM storage_schema")
                cursor.execute(
                    "INSERT INTO storage_schema (id, version, initialized_at) VALUES (1, %s, %s)",
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
            rows = self._fetchall("SELECT version FROM storage_schema WHERE id = 1", ())
            versions = {row["version"] for row in rows}
            if not versions:
                self._insert_placeholder_free(
                    "INSERT INTO storage_schema (id, version, initialized_at)",
                    (1, STORAGE_SCHEMA_VERSION, time.time()),
                )
                self._save_table(
                    "forge_meta",
                    {
                        "__meta__": {
                            "canonical_branch": None,
                            "repository_state": "initializing",
                            "repository_id": None,
                            "last_indexed_seq": 0,
                        }
                    },
                    id_column=True,
                )
            elif versions != {STORAGE_SCHEMA_VERSION}:
                found = ", ".join(str(item) for item in sorted(versions))
                raise ForgeError(
                    f"storage schema version {found} is incompatible with this build"
                )
            self._ensure_indexes()
            self.backend.commit(self.connection)
        except ForgeError:
            self.backend.rollback(self.connection)
            raise
        except Exception as error:
            self.backend.rollback(self.connection)
            raise ForgeError(f"cannot initialize storage schema: {error}") from error

    def _ensure_indexes(self) -> None:
        self._execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS audit_log_event_id "
            "ON audit_log(event_id) WHERE event_id IS NOT NULL",
            (),
        )

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
        """Materialize the current control-plane document, verifying checksums."""
        self.backend.begin(self.connection)
        try:
            meta_rows = self._fetchall(
                "SELECT doc, version, checksum FROM forge_meta WHERE id = ?", (1,)
            )
            if len(meta_rows) > 1:
                raise ForgeError("forge meta table is corrupt")
            if not meta_rows:
                raise ForgeError("forge state is not initialized")
            meta = meta_rows[0]
            parsed = json.loads(meta["doc"])
            if _checksum(parsed) != meta["checksum"]:
                raise ForgeError("forge meta row failed its integrity checksum")
            expected_meta = {
                "canonical_branch", "repository_state", "repository_id", "last_indexed_seq"
            }
            if not isinstance(parsed, dict) or set(parsed) != expected_meta:
                raise ForgeError("forge meta row has an invalid shape")
            document: dict = dict(parsed)
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
            required_meta = {
                "canonical_branch", "repository_state", "repository_id", "last_indexed_seq"
            }
            expected_fields = required_meta | set(_STATE_SECTIONS)
            if set(state) != expected_fields:
                raise ForgeError("forge state has an invalid shape")
            desired_meta = {key: state[key] for key in required_meta}
            if desired_meta["repository_state"] not in {"initializing", "ready"}:
                raise ForgeError("forge repository state is invalid")
            self._save_table(
                "forge_meta",
                {"__meta__": desired_meta},
                id_column=True,
                baseline_section=(
                    {
                        "__meta__": {
                            **{key: baseline[key] for key in required_meta},
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

    # -- audit and observability --------------------------------------------

    def audit(
        self, kind: str, subject: str, detail: str, event_id: str | None = None
    ) -> None:
        self.backend.begin(self.connection)
        try:
            self._execute(
                "INSERT INTO audit_log (kind, subject, detail, at, event_id) VALUES "
                f"({self.backend.param}, {self.backend.param}, {self.backend.param}, {self.backend.param}, {self.backend.param})",
                (kind, subject, detail, time.time(), event_id),
            )
            self.backend.commit(self.connection)
        except Exception as error:
            self.backend.rollback(self.connection)
            raise ForgeError(f"cannot append audit entry: {error}") from error

    def recent_audit(self, limit: int = 20) -> list[dict]:
        rows = self._fetchall(
            "SELECT seq, kind, subject, detail, at, event_id FROM audit_log "
            f"ORDER BY seq DESC LIMIT {int(limit)}",
            (),
        )
        return rows

    def stats(self) -> dict:
        counts: dict[str, dict[str, int]] = {}
        for label, table in (("transactions", "transactions"), ("jobs", "projection_jobs")):
            rows = self._fetchall(f"SELECT doc FROM {table}", ())
            bucket: dict[str, int] = {}
            for row in rows:
                document = json.loads(row["doc"])
                status = document.get("status")
                if (
                    status is None
                    and label == "transactions"
                    and document.get("committed") is True
                ):
                    status = "committed"
                status = status or "?"
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
    """Create the backend record and current schema for a fresh forge."""
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
    store = open_store(root)
    document = store.load_document()
    if document.get("repository_state") != "ready":
        store.close()
        raise ForgeError("forge initialization is incomplete")
    try:
        return _backup_repository(root, destination, store)
    finally:
        store.close()


def restore_forge(backup: Path, new_root: Path, database: str | None = None) -> Path:
    """Reconstruct a forge from a backup directory into ``new_root``."""
    manifest_path = backup / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ForgeError(f"cannot read backup manifest: {error}") from error
    if manifest.get("format") != "bastion-wal-backup" or manifest.get("format_version") != 1:
        raise ForgeError("backup format is unsupported")
    return _restore_repository(backup, new_root, database, manifest)


def _backup_repository(root: Path, destination: Path, store: Store) -> Path:
    """Copy the exact manifest-reachable object closure, never live caches."""
    from repository import Repository
    from wal import canonical_json, sha256

    if destination.exists():
        raise ForgeError(f"backup destination already exists: {destination}")
    repository = Repository.open(root)
    manifest, _version, replay = repository.load()
    manifest_body = canonical_json(manifest.to_dict())
    keys: set[str] = {"manifest.json"}
    pointer = manifest.wal_head.to_dict()
    while True:
        keys.add(pointer["key"])
        record = json.loads(repository.get(pointer["key"]).body)
        if record["seq"] == 1:
            break
        pointer = record["previous"]
    pack_refs: list[dict] = list(manifest.canonical_packs)
    descriptor_ids: set[str] = set()
    for record in replay.records:
        payload = record["payload"]
        if record["kind"] == "import":
            pack_refs.append(payload["canonical_pack"])
        elif record["kind"] == "view_create":
            descriptor_ids.add(payload["descriptor_id"])
        elif record["kind"] == "submission":
            descriptor_ids.add(payload["descriptor_id"])
            pack_refs.extend((payload["canonical_pack"], payload["submission_pack"]))
        elif record["kind"] == "canonical_update":
            pack_refs.append(payload["canonical_pack"])
        elif record["kind"] == "policy_transition":
            descriptor_ids.update(item["new_descriptor_id"] for item in payload["views"])
    for pack in pack_refs:
        keys.add(pack["pack_key"])
        keys.add(pack["idx_key"])
    keys.update(f"descriptors/{identifier}.json" for identifier in descriptor_ids)
    destination.mkdir(parents=True)
    copied: list[dict] = []
    try:
        for key in sorted(keys):
            body = manifest_body if key == "manifest.json" else repository.get(key).body
            target = destination / "objects" / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            copied.append({"key": key, "sha256": sha256(body), "size": len(body)})
        shutil.copy2(root / "repository.json", destination / "repository.json")
        shutil.copy2(root / "backend.json", destination / "backend.json")
        tokens = root / "tokens.json"
        if tokens.exists():
            shutil.copy2(tokens, destination / "tokens.json")
        if store.backend.kind == "sqlite":
            store.backend.dump(destination / "database.sqlite3")
        else:
            store.backend.dump(destination / "tables")
        metadata = {
            "format": "bastion-wal-backup",
            "format_version": 1,
            "created_at": time.time(),
            "repository_id": repository.repository_id,
            "head_seq": manifest.head_seq,
            "manifest_sha256": sha256(manifest_body),
            "objects": copied,
        }
        (destination / "manifest.json").write_bytes(canonical_json(metadata))
        return destination
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _read_backup_object(root: Path, key: str, expected_size: object) -> bytes:
    if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 0:
        raise ForgeError("WAL backup object has an invalid size", "integrity")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        components = key.split("/")
        for component in components[:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
        metadata = os.stat(components[-1], dir_fd=directory, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise ForgeError("WAL backup object path contains a symlink", "integrity")
        if not stat.S_ISREG(metadata.st_mode):
            raise ForgeError("WAL backup object is not a regular file", "integrity")
        descriptor = os.open(
            components[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size != expected_size:
                raise ForgeError("WAL backup object size differs", "integrity")
            with os.fdopen(descriptor, "rb") as source:
                descriptor = -1
                body = source.read(expected_size + 1)
                if len(body) != expected_size:
                    raise ForgeError("WAL backup object size changed while reading", "integrity")
                return body
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except ForgeError:
        raise
    except OSError as error:
        raise ForgeError(f"cannot safely read WAL backup object: {key}", "integrity") from error
    finally:
        os.close(directory)


def _restore_repository(backup: Path, new_root: Path, database: str | None, metadata: dict) -> Path:
    from object_store_fs import FilesystemObjectStore
    from object_store import Create, validate_key
    from repository import Repository
    from materialization import ensure_canonical_materialized, ensure_intake_materialized, ensure_view_materialized

    if new_root.exists() and any(new_root.iterdir()):
        raise ForgeError(f"restore target is not empty: {new_root}")
    try:
        config = json.loads((backup / "repository.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ForgeError(f"cannot read WAL backup configuration: {error}") from error
    if config.get("repository_id") != metadata.get("repository_id"):
        raise ForgeError("WAL backup repository ID is inconsistent", "integrity")
    new_root.mkdir(parents=True, exist_ok=True)
    (new_root / "views").mkdir()
    (new_root / "intake").mkdir()
    from repository import write_repository_config

    write_repository_config(
        new_root,
        config["repository_id"],
        config["canonical_branch"],
        new_root / "object-store",
    )
    shutil.copy2(backup / "backend.json", new_root / "backend.json")
    tokens = backup / "tokens.json"
    if tokens.exists():
        shutil.copy2(tokens, new_root / "tokens.json")
    object_root = new_root / "object-store"
    filesystem = FilesystemObjectStore(object_root)
    backup_objects = backup / "objects"
    if not backup_objects.is_dir() or backup_objects.is_symlink():
        raise ForgeError("WAL backup object directory is invalid", "integrity")
    for entry in metadata.get("objects", []):
        key = entry.get("key")
        if not isinstance(key, str):
            raise ForgeError("WAL backup has an invalid object key", "integrity")
        try:
            validate_key(key)
        except ValueError as error:
            raise ForgeError("WAL backup has an invalid object key", "integrity") from error
        body = _read_backup_object(backup_objects, key, entry.get("size"))
        if hashlib.sha256(body).hexdigest() != entry.get("sha256"):
            raise ForgeError("WAL backup object hash differs", "integrity")
        filesystem.put(f"repos/{config['repository_id']}/{key}", body, mode=Create())
    uri = database_uri(new_root, database)
    if uri.startswith("postgres://") or uri.startswith("postgresql://"):
        record = {"backend": "postgres", "dsn": uri}
    else:
        record = {"backend": "sqlite"}
    if record["backend"] == "sqlite":
        write_backend_record(new_root, record)
        _SqliteBackend(new_root / "store.sqlite3").restore(backup / "database.sqlite3")
    else:
        backend = _PostgresBackend(record["dsn"], new_root)
        backend.restore(backup / "tables")
        write_backend_record(new_root, record)
    repository = Repository.open(new_root)
    manifest, _version, replay = repository.load()
    if manifest.head_seq != metadata.get("head_seq"):
        raise ForgeError("WAL backup manifest head differs", "integrity")
    ensure_canonical_materialized(new_root, manifest, repository)
    for view in sorted(replay.active_views):
        materialized = ensure_view_materialized(new_root, view, manifest, replay, repository)
        ensure_intake_materialized(new_root, materialized)
    return new_root
