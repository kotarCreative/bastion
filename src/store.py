"""Durable control-plane store with SQLite, PostgreSQL, and Durable Objects backends."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import stat
import threading
import tempfile
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from durable_store import DurableObjectsBackend

from errors import ForgeError
from object_store_s3 import MAX_OBJECT_SIZE
from object_store import ObjectStoreError

LEASE_TTL_SECONDS = 60.0
LEASE_WAIT_SECONDS = 30.0
LEASE_POLL_SECONDS = 0.05

# PostgreSQL process-local pool bounds (spec 014: lifecycle).
PG_POOL_MAX_DEFAULT = 8
PG_POOL_MAX_MIN = 1
PG_POOL_MAX_LIMIT = 64
PG_POOL_CHECKOUT_DEFAULT = 5.0
PG_POOL_CHECKOUT_MIN = 0.1
PG_POOL_CHECKOUT_LIMIT = 30.0

# Restore safety bounds (spec 014: backup and restore safety).
RESTORE_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
RESTORE_MAX_TOTAL_BYTES = 1024 * 1024 * 1024
RESTORE_MAX_NDJSON_LINE_BYTES = 8 * 1024 * 1024
RESTORE_MAX_ROWS_PER_TABLE = 1_000_000


def _positive_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        raise ForgeError(f"invalid {name} configuration")
    if not minimum <= value <= maximum:
        raise ForgeError(f"invalid {name} configuration")
    return value


def _positive_float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        raise ForgeError(f"invalid {name} configuration")
    if not minimum <= value <= maximum:
        raise ForgeError(f"invalid {name} configuration")
    return value


def pg_pool_max() -> int:
    return _positive_int_env(
        "BASTION_PG_POOL_MAX", PG_POOL_MAX_DEFAULT, PG_POOL_MAX_MIN, PG_POOL_MAX_LIMIT
    )


def pg_pool_checkout_seconds() -> float:
    return _positive_float_env(
        "BASTION_PG_POOL_TIMEOUT",
        PG_POOL_CHECKOUT_DEFAULT,
        PG_POOL_CHECKOUT_MIN,
        PG_POOL_CHECKOUT_LIMIT,
    )


def _sanitize_backend_error(error: Exception) -> str:
    """Stable storage message that never carries DSNs or credentials."""
    text = str(error)
    # Strip anything resembling a URI with credentials/host before surfacing.
    text = re.sub(r"postgres(?:ql)?://\S+", "postgres://<redacted>", text)
    text = re.sub(r"password=\S+", "password=<redacted>", text)
    return text[:200]


def _storage_error(message: str, error: Exception | None = None) -> ForgeError:
    if error is None:
        return ForgeError(message, "storage")
    return ForgeError(f"{message}", "storage")


# Process-local SQL trace sink for bounded-hot-path verification. When set to
# a list, every issued statement text is appended (never parameters). Tests
# assert query shapes and counts; production paths never enable it.
QUERY_TRACE: list[str] | None = None


def _trace_statement(statement: str) -> None:
    if QUERY_TRACE is not None:
        QUERY_TRACE.append(" ".join(statement.split()))

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

# Digest-only hosted bearer tokens (spec 013). This table is deliberately
# outside DOCUMENT_TABLES/_STATE_SECTIONS so the generic control-plane
# document shape stays stable; tokens are accessed through dedicated
# transactional methods that atomically append audit rows.
HTTP_TOKENS_TABLE = "http_tokens"

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
    if kind not in {"sqlite", "postgres", "durable_objects"}:
        raise ForgeError(f"unknown storage backend: {kind!r}")
    if kind == "postgres" and not record.get("dsn"):
        raise ForgeError("postgres backend record lacks a dsn")
    return record


def _load_table_readonly_sqlite(path: Path, table: str) -> dict[str, dict]:
    import sqlite3

    if not path.exists():
        raise ForgeError("forge state is not initialized")
    uri = f"file:{path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30.0)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"SELECT key, doc, checksum FROM {table}"
        ).fetchall()
        result: dict[str, dict] = {}
        for row in rows:
            parsed = json.loads(row["doc"])
            if _checksum(parsed) != row["checksum"]:
                raise ForgeError(f"{table} row failed its integrity checksum")
            result[row["key"]] = parsed
        return result
    finally:
        connection.close()


def _load_table_readonly_postgres(dsn: str, table: str) -> dict[str, dict]:
    try:
        import psycopg
    except ImportError as error:
        raise ForgeError(
            'the postgres backend requires psycopg 3: pip install "psycopg[binary]"'
        ) from error
    connection = psycopg.connect(dsn, autocommit=False)
    try:
        connection.execute("BEGIN READ ONLY")
        rows = connection.execute(
            f"SELECT key, doc, checksum FROM {table}"
        ).fetchall()
        result: dict[str, dict] = {}
        for row in rows:
            parsed = json.loads(row["doc"])
            if _checksum(parsed) != row["checksum"]:
                raise ForgeError(f"{table} row failed its integrity checksum")
            result[row["key"]] = parsed
        return result
    finally:
        connection.close()


def load_principals_readonly(root: Path) -> dict[str, dict]:
    """Load only immutable principal rows without writes, leases, or schema changes."""
    record = read_backend_record(root)
    if record["backend"] == "sqlite":
        return _load_table_readonly_sqlite(root / "store.sqlite3", "principals")
    if record["backend"] == "durable_objects":
        store = open_store(root)
        try:
            return store.load_sections(["principals"])["principals"]
        finally:
            store.close()
    dsn = _PostgresBackend._scoped_dsn(record["dsn"], root)
    return _load_table_readonly_postgres(dsn, "principals")


def load_forge_meta_readonly(root: Path) -> dict:
    """Load only the immutable forge_meta row without writes, leases, or schema changes."""
    record = read_backend_record(root)
    if record["backend"] == "sqlite":
        import sqlite3

        path = root / "store.sqlite3"
        if not path.exists():
            raise ForgeError("forge state is not initialized")
        uri = f"file:{path}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=30.0)
        try:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT doc, checksum FROM forge_meta WHERE id = 1"
            ).fetchall()
            if not rows:
                raise ForgeError("forge state is not initialized")
            parsed = json.loads(rows[0]["doc"])
            if _checksum(parsed) != rows[0]["checksum"]:
                raise ForgeError("forge meta row failed its integrity checksum")
            return parsed
        finally:
            connection.close()
    if record["backend"] == "durable_objects":
        store = open_store(root)
        try:
            return store.load_sections([])
        finally:
            store.close()
    try:
        import psycopg
    except ImportError as error:
        raise ForgeError(
            'the postgres backend requires psycopg 3: pip install "psycopg[binary]"'
        ) from error
    dsn = _PostgresBackend._scoped_dsn(record["dsn"], root)
    connection = psycopg.connect(dsn, autocommit=False)
    try:
        connection.execute("BEGIN READ ONLY")
        rows = connection.execute(
            "SELECT doc, checksum FROM forge_meta WHERE id = 1"
        ).fetchall()
        if not rows:
            raise ForgeError("forge state is not initialized")
        parsed = json.loads(rows[0]["doc"])
        if _checksum(parsed) != rows[0]["checksum"]:
            raise ForgeError("forge meta row failed its integrity checksum")
        return parsed
    finally:
        connection.close()


def database_uri(root: Path, database: str | None = None) -> str:
    if database:
        return database
    return os.environ.get("BASTION_DATABASE", "")


def _sqlite_ddl() -> list[str]:
    return [
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
        f"""CREATE TABLE IF NOT EXISTS {HTTP_TOKENS_TABLE} (
            key TEXT PRIMARY KEY,
            doc TEXT NOT NULL,
            version INTEGER NOT NULL,
            checksum TEXT NOT NULL)""",
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
        f"""CREATE TABLE IF NOT EXISTS {HTTP_TOKENS_TABLE} (
            key TEXT PRIMARY KEY,
            doc TEXT NOT NULL,
            version INTEGER NOT NULL,
            checksum TEXT NOT NULL)""",
    ] + [
        f"""CREATE TABLE IF NOT EXISTS {table} (
            key TEXT PRIMARY KEY,
            doc TEXT NOT NULL,
            version INTEGER NOT NULL,
            checksum TEXT NOT NULL)"""
        for table in DOCUMENT_TABLES
    ]
    return ";\n".join(statements)


def _index_ddl() -> list[str]:
    return [
        "CREATE UNIQUE INDEX IF NOT EXISTS audit_log_event_id "
        "ON audit_log(event_id) WHERE event_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS storage_locks_owner ON storage_locks(owner)",
    ]


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
        self.begin_write(connection)

    def begin_write(self, connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    def begin_read(self, connection) -> None:
        # Deferred read transaction: no BEGIN IMMEDIATE writer reservation.
        connection.execute("BEGIN")

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
        # Online backup snapshot; the snapshot begins with no leases without
        # changing the live source (storage_locks is ephemeral coordination).
        source = self._sqlite3.connect(self._path)
        try:
            target = self._sqlite3.connect(destination)
            try:
                source.backup(target)
                target.execute("DELETE FROM storage_locks")
                target.commit()
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


def _pg_connect_direct(dsn: str, *, autocommit: bool = False):
    """Connect to the exact scoped database; never provisions or leaks DSNs."""
    try:
        import psycopg
    except ImportError as error:
        raise ForgeError(
            'the postgres backend requires psycopg 3: pip install "psycopg[binary]"'
        ) from error
    try:
        return psycopg.connect(dsn, autocommit=autocommit)
    except Exception as error:
        raise ForgeError("postgres database is unavailable", "storage") from None


class _PgPool:
    """Bounded process-local pool for one exact scoped runtime DSN."""

    def __init__(self, dsn: str, maximum: int):
        self._dsn = dsn
        self._max = maximum
        self._available: queue.Queue = queue.Queue()
        self._created = 0
        self._lock = threading.Lock()

    def checkout(self, timeout: float):
        try:
            candidate = self._available.get_nowait()
        except queue.Empty:
            candidate = None
        if candidate is not None:
            if not getattr(candidate, "closed", False):
                try:
                    candidate.rollback()
                except Exception:
                    pass
                if not getattr(candidate, "closed", False):
                    return candidate
            with self._lock:
                self._created -= 1
        with self._lock:
            if self._created < self._max:
                self._created += 1
                create = True
            else:
                create = False
        if create:
            try:
                return _pg_connect_direct(self._dsn)
            except Exception:
                with self._lock:
                    self._created -= 1
                raise
        try:
            connection = self._available.get(timeout=timeout)
        except queue.Empty:
            raise ForgeError("postgres connection pool is saturated", "storage")
        if getattr(connection, "closed", False):
            with self._lock:
                self._created -= 1
            return self.checkout(max(timeout, 0.0))
        try:
            connection.rollback()
        except Exception:
            pass
        if getattr(connection, "closed", False):
            with self._lock:
                self._created -= 1
            return self.checkout(max(timeout, 0.0))
        return connection

    def checkin(self, connection, *, valid: bool = True) -> None:
        if not valid or getattr(connection, "closed", False):
            try:
                connection.close()
            except Exception:
                pass
            with self._lock:
                self._created = max(0, self._created - 1)
            return
        try:
            connection.rollback()
        except Exception:
            try:
                connection.close()
            except Exception:
                pass
            with self._lock:
                self._created = max(0, self._created - 1)
            return
        self._available.put(connection)


_PG_POOLS: dict[str, _PgPool] = {}
_PG_POOLS_LOCK = threading.Lock()


_RESTORE_TABLE_ALLOWLIST = (
    "forge_meta",
    *DOCUMENT_TABLES,
    HTTP_TOKENS_TABLE,
    "audit_log",
)


def _restore_limits() -> dict[str, int]:
    """Qualified restore bounds; unsafe or non-integer config fails fast."""

    def _limit(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except ValueError:
            raise ForgeError(f"invalid {name} configuration")
        if isinstance(value, bool) or value <= 0:
            raise ForgeError(f"invalid {name} configuration")
        return value

    return {
        "artifact": _limit(
            "BASTION_RESTORE_MAX_ARTIFACT_BYTES", RESTORE_MAX_ARTIFACT_BYTES
        ),
        "total": _limit("BASTION_RESTORE_MAX_TOTAL_BYTES", RESTORE_MAX_TOTAL_BYTES),
        "line": _limit("BASTION_RESTORE_MAX_NDJSON_LINE_BYTES", RESTORE_MAX_NDJSON_LINE_BYTES),
        "rows": _limit("BASTION_RESTORE_MAX_ROWS_PER_TABLE", RESTORE_MAX_ROWS_PER_TABLE),
    }


def _read_restore_tables(tables_dir: Path) -> dict[str, list[dict]]:
    """Parse and fully validate one backend's table exports before mutation.

    Filenames are never interpolated into SQL: callers choose tables from the
    fixed code-owned allowlist and use identifier composition. Unknown table
    artifacts are never tolerated.
    """
    limits = _restore_limits()
    try:
        if tables_dir.is_symlink() or not tables_dir.is_dir():
            raise ForgeError("database backup directory is invalid", "integrity")
        names = sorted(item.name for item in tables_dir.iterdir())
    except ForgeError:
        raise
    except OSError as error:
        raise ForgeError("database backup directory is unreadable", "integrity") from error
    expected = sorted(f"{table}.ndjson" for table in _RESTORE_TABLE_ALLOWLIST)
    legacy_expected = sorted(
        f"{table}.ndjson"
        for table in _RESTORE_TABLE_ALLOWLIST
        if table != HTTP_TOKENS_TABLE
    )
    # Pre-013 backups predate the digest-only token table; accept them as
    # empty token sets rather than rejecting a previously valid backup.
    allow_missing_tokens = names == legacy_expected
    if names != expected and not allow_missing_tokens:
        raise ForgeError("database backup table set is unexpected", "integrity")
    total = 0
    result: dict[str, list[dict]] = {}
    for table in _RESTORE_TABLE_ALLOWLIST:
        if table == HTTP_TOKENS_TABLE and allow_missing_tokens:
            result[table] = []
            continue
        path = tables_dir / f"{table}.ndjson"
        try:
            metadata = os.stat(path, follow_symlinks=False)
        except OSError as error:
            raise ForgeError("database backup artifact is missing", "integrity") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ForgeError("database backup artifact is a symlink", "integrity")
        if not stat.S_ISREG(metadata.st_mode):
            raise ForgeError("database backup artifact is not a regular file", "integrity")
        if metadata.st_size > limits["artifact"]:
            raise ForgeError("database backup artifact is oversized", "integrity")
        total += metadata.st_size
        if total > limits["total"]:
            raise ForgeError("database backup exceeds its total budget", "integrity")
        try:
            with path.open("rb") as source:
                raw = source.read(limits["artifact"] + 1)
        except OSError as error:
            raise ForgeError("database backup artifact is unreadable", "integrity") from error
        if len(raw) > limits["artifact"]:
            raise ForgeError("database backup artifact is oversized", "integrity")
        rows: list[dict] = []
        seen_keys: set[object] = set()
        for raw_line in raw.split(b"\n"):
            if not raw_line.strip():
                continue
            if len(raw_line) > limits["line"]:
                raise ForgeError("database backup line is oversized", "integrity")
            try:
                row = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ForgeError("database backup row is malformed", "integrity") from error
            if table == "audit_log":
                rows.append(_validate_restore_audit_row(row))
            else:
                rows.append(_validate_restore_document_row(table, row, seen_keys))
            if len(rows) > limits["rows"]:
                raise ForgeError("database backup table exceeds its row budget", "integrity")
        if table == "forge_meta":
            if len(rows) != 1:
                raise ForgeError("database backup metadata row is invalid", "integrity")
        result[table] = rows
    return result


def _validate_restore_document_row(
    table: str, row: object, seen_keys: set[object]
) -> dict:
    if not isinstance(row, dict) or set(row) != {"key", "doc", "version", "checksum"}:
        raise ForgeError("database backup row has an invalid shape", "integrity")
    key = row["key"]
    document = row["doc"]
    version = row["version"]
    checksum = row["checksum"]
    if table == "forge_meta":
        if key != 1:
            raise ForgeError("database backup metadata row is invalid", "integrity")
    elif not isinstance(key, str) or not key:
        raise ForgeError("database backup row has an invalid key", "integrity")
    if not isinstance(document, dict):
        raise ForgeError("database backup row has an invalid document", "integrity")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
    ):
        raise ForgeError("database backup row has an invalid version", "integrity")
    if (
        not isinstance(checksum, str)
        or len(checksum) != 64
        or any(c not in "0123456789abcdef" for c in checksum)
    ):
        raise ForgeError("database backup row has an invalid checksum", "integrity")
    if _checksum(document) != checksum:
        raise ForgeError("database backup row failed its checksum", "integrity")
    marker = ("forge_meta", 1) if table == "forge_meta" else (table, key)
    if marker in seen_keys:
        raise ForgeError("database backup row key is duplicated", "integrity")
    seen_keys.add(marker)
    return {"key": key, "doc": document, "version": version, "checksum": checksum}


def _validate_restore_audit_row(row: object) -> dict:
    if (
        not isinstance(row, dict)
        or set(row) != {"seq", "kind", "subject", "detail", "at", "event_id"}
    ):
        raise ForgeError("database backup audit row has an invalid shape", "integrity")
    seq = row["seq"]
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise ForgeError("database backup audit row has an invalid sequence", "integrity")
    for field in ("kind", "subject", "detail"):
        if not isinstance(row[field], str):
            raise ForgeError("database backup audit row has an invalid field", "integrity")
    if not isinstance(row["at"], (int, float)) or isinstance(row["at"], bool):
        raise ForgeError("database backup audit row has an invalid timestamp", "integrity")
    if row["event_id"] is not None and not isinstance(row["event_id"], str):
        raise ForgeError("database backup audit row has an invalid event", "integrity")
    return {
        "seq": row["seq"],
        "kind": row["kind"],
        "subject": row["subject"],
        "detail": row["detail"],
        "at": float(row["at"]),
        "event_id": row["event_id"],
    }


def _pg_pool_for(dsn: str) -> _PgPool:
    with _PG_POOLS_LOCK:
        pool = _PG_POOLS.get(dsn)
        if pool is None:
            pool = _PgPool(dsn, pg_pool_max())
            _PG_POOLS[dsn] = pool
        return pool


class _PostgresBackend:
    kind = "postgres"
    param = "%s"

    def __init__(
        self,
        dsn: str,
        root: Path | None = None,
        *,
        pooled: bool = True,
        admin_dsn: str | None = None,
    ):
        self._raw_dsn = dsn
        self._root = root.resolve() if root is not None else None
        self._dsn = self._scoped_dsn(dsn, root)
        self._pooled = pooled
        self._admin_dsn = admin_dsn
        self._pooled_connection = False

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

    def _admin_target(self, admin_dsn: str | None = None) -> str:
        """One-shot administrative DSN; never persisted."""
        if admin_dsn or self._admin_dsn:
            return admin_dsn or self._admin_dsn or ""
        parts = urllib.parse.urlsplit(self._dsn)
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, "/postgres", parts.query, "")
        )

    def provision_database(self, admin_dsn: str | None = None) -> bool:
        """Create the root-scoped database (init/restore only, never open).

        Returns True when this call created the database and False when it
        already existed.
        """
        try:
            import psycopg
        except ImportError as error:
            raise ForgeError(
                'the postgres backend requires psycopg 3: pip install "psycopg[binary]"'
            ) from error
        parts = urllib.parse.urlsplit(self._dsn)
        dbname = parts.path.lstrip("/")
        if not dbname:
            raise ForgeError("postgres runtime DSN has no database", "storage")
        admin = self._admin_target(admin_dsn)
        try:
            connection = psycopg.connect(admin, autocommit=True)
        except Exception as error:
            raise ForgeError("postgres database is unavailable", "storage") from None
        try:
            # Identifier composition even though dbname is derived locally.
            from psycopg import sql as _sql

            try:
                connection.execute(
                    _sql.SQL("CREATE DATABASE {}").format(_sql.Identifier(dbname))
                )
                return True
            except Exception as error:
                state = str(getattr(error, "sqlstate", ""))
                if "42P04" not in state and "already exists" not in _sanitize_backend_error(error):
                    raise ForgeError("postgres database is unavailable", "storage") from None
                return False
        finally:
            try:
                connection.close()
            except Exception:
                pass

    def connect(self):
        # Ordinary access connects directly to the existing scoped database
        # through the runtime DSN and issues no CREATE DATABASE.
        if self._pooled:
            pool = _pg_pool_for(self._dsn)
            self._pooled_connection = True
            try:
                return pool.checkout(pg_pool_checkout_seconds())
            except ForgeError:
                self._pooled_connection = False
                raise
        self._pooled_connection = False
        return _pg_connect_direct(self._dsn)

    def connect_direct(self):
        self._pooled_connection = False
        return _pg_connect_direct(self._dsn)

    def shutdown(self, connection) -> None:
        if self._pooled and self._pooled_connection:
            self._pooled_connection = False
            try:
                _pg_pool_for(self._dsn).checkin(connection, valid=True)
            except Exception:
                try:
                    connection.close()
                except Exception:
                    pass
            return
        try:
            connection.close()
        except Exception:
            pass

    def invalidate(self, connection) -> None:
        if self._pooled and self._pooled_connection:
            self._pooled_connection = False
            try:
                _pg_pool_for(self._dsn).checkin(connection, valid=False)
            except Exception:
                pass
            return
        try:
            connection.close()
        except Exception:
            pass

    def begin(self, connection) -> None:
        self.begin_write(connection)

    def begin_write(self, connection) -> None:
        pass

    def begin_read(self, connection) -> None:
        try:
            status = connection.info.transaction_status
            from psycopg.pq import TransactionStatus as _TS

            idle = status == _TS.IDLE
        except Exception:
            idle = True
        if idle:
            try:
                connection.execute("START TRANSACTION READ ONLY")
            except Exception:
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
        # One read-only REPEATABLE READ snapshot so document rows and audit
        # rows represent one database snapshot. storage_locks is ephemeral
        # coordination state and is never backed up.
        destination_dir.mkdir(parents=True, exist_ok=True)
        connection = self.connect_direct()
        try:
            from psycopg import sql as _sql

            connection.execute("START TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            try:
                tables = [
                    "forge_meta",
                    *DOCUMENT_TABLES,
                    HTTP_TOKENS_TABLE,
                    "audit_log",
                ]
                for table in tables:
                    identifier = _sql.Identifier(table)
                    with connection.cursor(name=f"bastion_dump_{table}") as cursor:
                        cursor.itersize = 1000
                        if table == "forge_meta":
                            cursor.execute(
                                _sql.SQL(
                                    "SELECT id AS key, doc, version, checksum FROM {}"
                                ).format(identifier)
                            )
                        elif table == "audit_log":
                            cursor.execute(
                                _sql.SQL(
                                    "SELECT seq, kind, subject, detail, at, event_id FROM {} "
                                    "ORDER BY seq"
                                ).format(identifier)
                            )
                        else:
                            cursor.execute(
                                _sql.SQL(
                                    "SELECT key, doc, version, checksum FROM {}"
                                ).format(identifier)
                            )
                        with (destination_dir / f"{table}.ndjson").open("w") as output:
                            for record in cursor:
                                if table == "audit_log":
                                    seq, kind, subject, detail, at, event_id = record
                                    output.write(
                                        json.dumps(
                                            {
                                                "seq": seq,
                                                "kind": kind,
                                                "subject": subject,
                                                "detail": detail,
                                                "at": at,
                                                "event_id": event_id,
                                            },
                                            sort_keys=True,
                                        )
                                        + "\n"
                                    )
                                else:
                                    key, doc, version, checksum = record
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
            finally:
                try:
                    connection.rollback()
                except Exception:
                    pass
        finally:
            self.close_connection(connection)

    def close_connection(self, connection) -> None:
        try:
            connection.close()
        except Exception:
            pass

    def size_bytes(self) -> int:
        connection = self.connect_direct()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_database_size(current_database())")
                return int(cursor.fetchone()[0])
        finally:
            self.close_connection(connection)

    def restore(self, tables_dir: Path) -> None:
        # All table deletion and insertion occurs in one transaction; any
        # parse, checksum, constraint, or database error rolls back the
        # complete restore.
        rows = _read_restore_tables(tables_dir)
        connection = self.connect_direct()
        try:
            self.create_schema(connection)
            try:
                from psycopg import sql as _sql

                with connection.cursor() as cursor:
                    for table in [
                        "forge_meta",
                        *DOCUMENT_TABLES,
                        HTTP_TOKENS_TABLE,
                        "audit_log",
                    ]:
                        cursor.execute(
                            _sql.SQL("DELETE FROM {}").format(_sql.Identifier(table))
                        )
                    # Restored databases begin with no leases.
                    cursor.execute("DELETE FROM storage_locks")
                    for table, table_rows in rows.items():
                        identifier = _sql.Identifier(table)
                        if table == "forge_meta":
                            for row in table_rows:
                                cursor.execute(
                                    _sql.SQL(
                                        "INSERT INTO {} (id, doc, version, checksum) "
                                        "VALUES (%s, %s, %s, %s)"
                                    ).format(identifier),
                                    (
                                        1,
                                        json.dumps(row["doc"], sort_keys=True),
                                        row["version"],
                                        row["checksum"],
                                    ),
                                )
                        elif table == "audit_log":
                            for row in table_rows:
                                cursor.execute(
                                    _sql.SQL(
                                        "INSERT INTO {} (seq, kind, subject, detail, at, event_id) "
                                        "VALUES (%s, %s, %s, %s, %s, %s)"
                                    ).format(identifier),
                                    (
                                        row["seq"],
                                        row["kind"],
                                        row["subject"],
                                        row["detail"],
                                        row["at"],
                                        row["event_id"],
                                    ),
                                )
                        else:
                            for row in table_rows:
                                cursor.execute(
                                    _sql.SQL(
                                        "INSERT INTO {} (key, doc, version, checksum) "
                                        "VALUES (%s, %s, %s, %s)"
                                    ).format(identifier),
                                    (
                                        row["key"],
                                        json.dumps(row["doc"], sort_keys=True),
                                        row["version"],
                                        row["checksum"],
                                    ),
                                )
                    # Advance the audit sequence transactionally so its next
                    # value is strictly greater than every restored sequence.
                    audit_rows = rows.get("audit_log", [])
                    peak = max((row["seq"] for row in audit_rows), default=0)
                    cursor.execute(
                        "SELECT pg_get_serial_sequence('audit_log', 'seq')"
                    )
                    sequence_row = cursor.fetchone()
                    sequence_name = sequence_row[0] if sequence_row else None
                    if sequence_name:
                        cursor.execute("SELECT setval(%s, %s, true)", (sequence_name, max(peak, 1)))
                self.commit(connection)
            except ForgeError:
                self.rollback(connection)
                raise
            except Exception as error:
                self.rollback(connection)
                raise ForgeError("postgres restore failed", "storage") from None
        finally:
            self.close_connection(connection)


def _linux_boot_id() -> str | None:
    for candidate in ("/proc/sys/kernel/random/boot_id",):
        try:
            value = Path(candidate).read_text().strip()
        except OSError:
            continue
        if value:
            return value
    return None


def _pid_start(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
    except OSError:
        return None
    return fields[21] if len(fields) > 21 else None


def host_forge_id(root: Path | None) -> str | None:
    """Random host-local forge identifier shared by processes using one root.

    Atomically created with mode ``0600``; never copied into backups. When no
    reliable local identity can be established, returns ``None`` and the
    dead-process optimization stays disabled (expiry remains authoritative).
    """
    if root is None:
        return None
    try:
        resolved = root.resolve()
    except OSError:
        return None
    path = resolved / ".host-id"
    try:
        existing = path.read_text().strip()
        if existing and len(existing) == 32 and all(
            c in "0123456789abcdef" for c in existing
        ):
            return existing
    except OSError:
        pass
    candidate = uuid.uuid4().hex
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            return path.read_text().strip() or None
        except OSError:
            return None
    except OSError:
        return None
    try:
        os.write(fd, candidate.encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return candidate


def owner_identity(root: Path | None = None) -> str:
    """Build a lease owner token with host, boot, pid, and start time.

    Format: ``<token>:<host>:<boot>:<pid>:<start>``. Legacy three-part tokens
    (``<token>:<pid>:<start>``) remain readable for migration.
    """
    token = uuid.uuid4().hex
    host = host_forge_id(root) or "unknown-host"
    boot = _linux_boot_id() or "unknown-boot"
    pid = os.getpid()
    start = _pid_start(pid) or "unknown-start"
    return f"{token}:{host}:{boot}:{pid}:{start}"


def process_identity() -> str:
    """Return this process's legacy ``pid:start`` identity for lease ownership."""
    start = _pid_start(os.getpid())
    if start is None:
        raise ForgeError("cannot establish local process identity", "storage")
    return f"{os.getpid()}:{start}"


def _parse_owner(owner: str) -> dict | None:
    """Parse new- and legacy-format owner tokens; ``None`` means unreadable."""
    parts = owner.rsplit(":", 4)
    if len(parts) == 5:
        token, host, boot, pid_text, start = parts
        try:
            return {"token": token, "host": host, "boot": boot, "pid": int(pid_text), "start": start}
        except ValueError:
            return None
    legacy = owner.rsplit(":", 2)
    if len(legacy) == 3:
        token, pid_text, start = legacy
        try:
            return {"token": token, "host": None, "boot": None, "pid": int(pid_text), "start": start}
        except ValueError:
            return None
    # Workspace format from process_identity(): "<pid>:<start>".
    two_part = owner.rsplit(":", 1)
    if len(two_part) == 2:
        pid_text, start = two_part
        try:
            return {"token": "", "host": None, "boot": None, "pid": int(pid_text), "start": start}
        except ValueError:
            return None
    return None


def _owner_alive(owner: str, local_host: str | None = None, local_boot: str | None = None) -> bool:
    """True when the lease owner token's process is still running.

    The dead-process shortcut is local-only: an owner from another or an
    unknown host/boot is considered live until expiry, and a coincident local
    PID may not steal it early. Legacy tokens without host/boot fall back to
    the PID/start check. Unparseable tokens are conservatively live.
    """
    parsed = _parse_owner(owner)
    if parsed is None:
        return True
    if parsed["host"] is not None:
        # New-format token: require a reliable local identity match.
        if local_host is None or local_boot is None:
            return True
        if parsed["host"] in {"unknown-host", ""} or parsed["boot"] in {"unknown-boot", ""}:
            return True
        if parsed["host"] != local_host or parsed["boot"] != local_boot:
            return True
    current = _pid_start(parsed["pid"])
    if current is None:
        return False
    return current == parsed["start"]


class Store:
    """One open connection to a forge's durable state."""

    def __init__(self, backend: _SqliteBackend | _PostgresBackend | DurableObjectsBackend):
        self.backend = backend
        self.connection = backend.connect()

    def close(self) -> None:
        self.backend.shutdown(self.connection)

    def bootstrap(self) -> None:
        begin = getattr(self.backend, "begin_write", self.backend.begin)
        begin(self.connection)
        try:
            self.backend.create_schema(self.connection)
            rows = self._fetchall("SELECT doc FROM forge_meta WHERE id = 1", ())
            if not rows:
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
            self._ensure_indexes()
            self.backend.commit(self.connection)
        except ForgeError:
            self.backend.rollback(self.connection)
            raise
        except Exception as error:
            self.backend.rollback(self.connection)
            raise ForgeError(f"cannot initialize storage schema: {error}") from error

    def _ensure_indexes(self) -> None:
        for statement in _index_ddl():
            self._execute(statement, ())

    def _insert_placeholder_free(self, head: str, values: tuple) -> None:
        placeholders = ", ".join([self.backend.param] * len(values))
        self._execute(f"{head} VALUES ({placeholders})", tuple(values))
        return None

    def _dialect_sql(self, statement: str) -> str:
        if self.backend.param == "?":
            return statement
        return statement.replace("?", "%s")

    def _cursor_rows(self, statement: str, parameters: tuple) -> list[dict]:
        _trace_statement(statement)
        cursor = self.connection.cursor()
        try:
            cursor.execute(self._dialect_sql(statement), parameters)
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def _rowcount(self, statement: str, parameters: tuple) -> int:
        _trace_statement(statement)
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

    def load_sections(self, sections: list[str]) -> dict:
        """Read only the requested control-plane sections plus forge_meta.

        Hot paths use this checksummed, indexed accessor instead of the bulk
        ``load_document`` path. Unknown section names fail closed.
        """
        for section in sections:
            if section not in _STATE_SECTIONS:
                raise ForgeError(f"unknown control-plane section: {section}")
        begin_read = getattr(self.backend, "begin_read", self.backend.begin)
        begin_read(self.connection)
        try:
            document = self._read_meta_locked()
            for section in sections:
                table = _STATE_SECTIONS[section]
                document[section] = self._read_table_locked(table)
            self.backend.rollback(self.connection)
            return document
        except ForgeError:
            self.backend.rollback(self.connection)
            raise
        except Exception as error:
            self.backend.rollback(self.connection)
            raise ForgeError("cannot read forge state", "storage") from None

    def iter_section_where_field(self, section: str, field: str, value: str) -> dict:
        """Rows whose JSON ``field`` equals ``value``, filtered by the backend.

        Adding unrelated catalog rows never causes a full-table read: the
        database evaluates the JSON predicate per row and returns only
        matches.
        """
        if section not in _STATE_SECTIONS:
            raise ForgeError(f"unknown control-plane section: {section}")
        table = _STATE_SECTIONS[section]
        begin_read = getattr(self.backend, "begin_read", self.backend.begin)
        begin_read(self.connection)
        try:
            if self.backend.param == "?":
                rows = self._fetchall(
                    f"SELECT key, doc FROM {table} "
                    f"WHERE json_extract(doc, {self.backend.param}) = {self.backend.param}",
                    (f"$.{field}", value),
                )
            else:
                rows = self._fetchall(
                    f"SELECT key, doc FROM {table} "
                    f"WHERE doc::jsonb->>{self.backend.param} = {self.backend.param}",
                    (field, value),
                )
            self.backend.rollback(self.connection)
            return {row["key"]: json.loads(row["doc"]) for row in rows}
        except ForgeError:
            self.backend.rollback(self.connection)
            raise
        except Exception as error:
            self.backend.rollback(self.connection)
            raise ForgeError(f"cannot read {section}: {error}") from error

    def iter_section_where_member(self, section: str, member: str) -> dict:
        """Rows whose JSON ``members`` array contains ``member``."""
        if section not in _STATE_SECTIONS:
            raise ForgeError(f"unknown control-plane section: {section}")
        table = _STATE_SECTIONS[section]
        begin_read = getattr(self.backend, "begin_read", self.backend.begin)
        begin_read(self.connection)
        try:
            if self.backend.param == "?":
                rows = self._fetchall(
                    f"SELECT key, doc FROM {table} WHERE EXISTS ("
                    f"SELECT 1 FROM json_each(json_extract(doc, {self.backend.param})) "
                    f"WHERE json_each.value = {self.backend.param})",
                    ("$.members", member),
                )
            else:
                rows = self._fetchall(
                    f"SELECT key, doc FROM {table} "
                    f"WHERE doc::jsonb->'members' @> jsonb_build_array({self.backend.param})",
                    (member,),
                )
            self.backend.rollback(self.connection)
            return {row["key"]: json.loads(row["doc"]) for row in rows}
        except ForgeError:
            self.backend.rollback(self.connection)
            raise
        except Exception as error:
            self.backend.rollback(self.connection)
            raise ForgeError(f"cannot read {section}: {error}") from error

    def load_row(self, section: str, key: str) -> dict | None:
        """Fetch one checksummed row by primary key (indexed direct lookup)."""
        if section not in _STATE_SECTIONS:
            raise ForgeError(f"unknown control-plane section: {section}")
        table = _STATE_SECTIONS[section]
        begin_read = getattr(self.backend, "begin_read", self.backend.begin)
        begin_read(self.connection)
        try:
            rows = self._fetchall(
                f"SELECT doc, checksum FROM {table} "
                f"WHERE key = {self.backend.param}",
                (key,),
            )
            self.backend.rollback(self.connection)
            if not rows:
                return None
            parsed = json.loads(rows[0]["doc"])
            if _checksum(parsed) != rows[0]["checksum"]:
                raise ForgeError(
                    f"{table}.{key} failed its integrity checksum"
                )
            return parsed
        except ForgeError:
            raise
        except Exception as error:
            raise ForgeError("cannot read forge state", "storage") from None

    def _read_meta_locked(self) -> dict:
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
        return dict(parsed)

    def _read_table_locked(self, table: str) -> dict:
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
        return bucket

    def load_document(self) -> dict:
        """Materialize the current control-plane document, verifying checksums."""
        begin_read = getattr(self.backend, "begin_read", self.backend.begin)
        begin_read(self.connection)
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

    def load_document_with_versions(self) -> tuple[dict, dict]:
        """Load the document plus per-row (version, checksum) baseline map.

        The version map enables ABA-safe optimistic concurrency: a row that
        changed and reverted to identical content still carries a different
        version and must conflict.
        """
        begin_read = getattr(self.backend, "begin_read", self.backend.begin)
        begin_read(self.connection)
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
            versions: dict[str, dict[str, tuple[int, str]]] = {
                "__meta__": {"__meta__": (meta["version"], meta["checksum"])}
            }
            for section, table in _STATE_SECTIONS.items():
                rows = self._fetchall(
                    f"SELECT key, doc, version, checksum FROM {table}", ()
                )
                bucket: dict = {}
                vmap: dict[str, tuple[int, str]] = {}
                for row in sorted(rows, key=lambda item: item["key"]):
                    parsed = json.loads(row["doc"])
                    if _checksum(parsed) != row["checksum"]:
                        raise ForgeError(
                            f"{table}.{row['key']} failed its integrity checksum"
                        )
                    bucket[row["key"]] = parsed
                    vmap[row["key"]] = (row["version"], row["checksum"])
                document[section] = bucket
                versions[section] = vmap
            if document.get("canonical_branch") is None:
                raise ForgeError("forge state is not initialized")
            self.backend.rollback(self.connection)
            return document, versions
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
        baseline_versions: dict | None = None,
    ) -> dict | None:
        """Persist changed aggregates in one short optimistic transaction.

        ``baseline`` is the document this session loaded (post-recovery);
        rows present there but absent from ``state`` are deleted. Rows that
        appeared concurrently under other scopes are never touched.
        ``baseline_versions`` optionally carries the per-row
        (version, checksum) map captured at load time for ABA-safe
        conflict detection.

        Returns the exact post-commit outcome (per-section row versions
        written and keys deleted) so the caller can refresh its session
        baseline from the versions it just committed, or ``None`` when the
        save failed before any committed write.
        """
        outcome: dict = {"sections": {}, "deleted": {}, "meta": None}
        begin_write = getattr(self.backend, "begin_write", self.backend.begin)
        begin_write(self.connection)
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
            baseline_meta_section = None
            baseline_meta_versions = None
            if baseline is not None:
                baseline_meta_section = {
                    "__meta__": {key: baseline[key] for key in required_meta},
                }
                if baseline_versions is not None:
                    baseline_meta_versions = baseline_versions.get("__meta__")
            # Saving one changed aggregate queries and writes only changed or
            # deleted keys plus lease/meta rows: unchanged tables are never
            # inventoried.
            if baseline is None or desired_meta != {
                key: baseline[key] for key in required_meta
            }:
                self._save_table(
                    "forge_meta",
                    {"__meta__": desired_meta},
                    id_column=True,
                    baseline_section=baseline_meta_section,
                    baseline_versions_section=baseline_meta_versions,
                    outcome=outcome,
                )
            for section, table in _STATE_SECTIONS.items():
                desired_section = state.get(section, {})
                if baseline is not None:
                    baseline_section = baseline.get(section, {})
                    if desired_section == baseline_section:
                        continue
                    deletable = set(baseline_section)
                    versions_section = (
                        baseline_versions.get(section)
                        if isinstance(baseline_versions, dict)
                        else None
                    )
                else:
                    deletable = None
                    baseline_section = None
                    versions_section = None
                self._save_table(
                    table,
                    desired_section,
                    False,
                    deletable,
                    baseline_section,
                    versions_section,
                    outcome=outcome,
                    section=section,
                )
            if failpoint_root is not None:
                from failpoints import checkpoint

                checkpoint(failpoint_root, "state.temporary_fsynced")
            self.backend.commit(self.connection)
            if failpoint_root is not None:
                from failpoints import checkpoint

                checkpoint(failpoint_root, "state.replaced")
                checkpoint(failpoint_root, "state.directory_fsynced")
            return outcome
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
        baseline_versions_section: dict | None = None,
        outcome: dict | None = None,
        section: str | None = None,
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
        elif baseline_section is not None:
            # Scoped write: inventory only rows this session could create,
            # update, or delete instead of scanning the whole table. This
            # keeps one-record incremental recovery bounded by the change.
            changed_keys = {
                key
                for key, document in desired.items()
                if document != baseline_section.get(key) or key not in baseline_section
            }
            if deletable_keys is None:
                intended_deletes: set[str] = set()
            else:
                intended_deletes = (set(baseline_section) - set(desired)) & set(
                    deletable_keys
                )
            fetch_keys = sorted(changed_keys | intended_deletes)
            existing = {}
            # Bounded IN batches so large deltas never exceed placeholder limits.
            for index in range(0, len(fetch_keys), 500):
                batch = fetch_keys[index : index + 500]
                placeholders = ", ".join([self.backend.param] * len(batch))
                rows = self._fetchall(
                    f"SELECT key, version, checksum FROM {table} "
                    f"WHERE key IN ({placeholders})",
                    tuple(batch),
                )
                for row in rows:
                    existing[row["key"]] = (row["version"], row["checksum"])
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
        def record_write(key: str, version: int, checksum: str) -> None:
            if outcome is None:
                return
            if id_column:
                outcome["meta"] = (version, checksum)
                return
            assert section is not None
            outcome["sections"].setdefault(section, {})[key] = (version, checksum)

        for key, document in desired_items:
            digest = _checksum(document)
            encoded = json.dumps(document, sort_keys=True)
            current = existing.get(key)
            baseline_document = (
                baseline_section.get(key) if baseline_section is not None else None
            )
            baseline_digest = (
                _checksum(baseline_document)
                if baseline_document is not None
                else None
            )
            baseline_version_entry = (
                baseline_versions_section.get(key)
                if baseline_versions_section is not None
                else None
            )
            if current is None:
                if (
                    baseline_section is not None
                    and key in baseline_section
                    and not id_column
                ):
                    # The caller observed this row at load time but it is gone
                    # now: a concurrent delete. Fail rather than resurrecting
                    # stale content.
                    raise ForgeError(
                        f"optimistic concurrency conflict on {table}.{key}"
                    )
                # When the scoped fetch was used, absence from `existing`
                # means the row is truly absent (we inventoried every changed
                # key). Insert directly. When a full inventory was used, the
                # same holds. Concurrent creation that committed before our
                # inventory appears as `current is not None` below and fails.
                if id_column:
                    self._insert_placeholder_free(
                        "INSERT INTO forge_meta (id, doc, version, checksum)",
                        (1, encoded, 1, digest),
                    )
                    record_write(key, 1, digest)
                else:
                    try:
                        self._insert_placeholder_free(
                            f"INSERT INTO {table} (key, doc, version, checksum)",
                            (key, encoded, 1, digest),
                        )
                    except Exception as error:
                        # A concurrent creator committed between our scoped
                        # inventory and our insert: fail rather than
                        # overwriting. Only duplicate-key signals map to a
                        # conflict; other storage failures propagate.
                        message = _sanitize_backend_error(error).lower()
                        if (
                            "unique" in message
                            or "duplicate" in message
                            or "already exists" in message
                            or "primary key" in message
                        ):
                            raise ForgeError(
                                f"optimistic concurrency conflict on {table}.{key}"
                            ) from None
                        raise
                    record_write(key, 1, digest)
                continue
            old_version, old_checksum = current
            if baseline_section is not None and not id_column and key not in baseline_section:
                # The caller never observed this row but it now exists: a
                # concurrent creation. Two stale creators must not let the
                # second overwrite the first.
                raise ForgeError(
                    f"optimistic concurrency conflict on {table}.{key}"
                )
            if old_checksum == digest:
                # Same content but a different version still means a
                # concurrent change committed and possibly reverted (ABA):
                # require the baseline version to match when it is known.
                if baseline_version_entry is not None:
                    base_version, base_checksum = baseline_version_entry
                    if (old_version, old_checksum) != (base_version, base_checksum):
                        raise ForgeError(
                            f"optimistic concurrency conflict on {table}.{key}"
                        )
                continue
            if baseline_version_entry is not None:
                base_version, base_checksum = baseline_version_entry
                if (old_version, old_checksum) != (base_version, base_checksum):
                    raise ForgeError(
                        f"optimistic concurrency conflict on {table}.{key}"
                    )
            elif baseline_digest is not None and old_checksum != baseline_digest:
                # Row changed after this caller loaded its baseline: a stale
                # scoped write must fail rather than overwrite the change.
                raise ForgeError(
                    f"optimistic concurrency conflict on {table}.{key}"
                )
            match_clause = "id = 1" if id_column else f"key = {self.backend.param}"
            values = (
                (encoded, old_version + 1, digest)
                if id_column
                else (encoded, old_version + 1, digest, key)
            )
            updated = self._execute(
                f"UPDATE {table} SET doc = {self.backend.param}, "
                f"version = {self.backend.param}, checksum = {self.backend.param} "
                f"WHERE {match_clause} AND version = {self.backend.param} "
                f"AND checksum = {self.backend.param}",
                (*values, old_version, old_checksum),
            )
            if updated != 1:
                raise ForgeError(f"optimistic concurrency conflict on {table}.{key}")
            record_write(key, old_version + 1, digest)
        if id_column:
            return
        # Rows vanish only when this session observed them at load time and
        # they are gone now; rows added concurrently under other scopes stay.
        # Deletes are conditional on the loaded baseline version+checksum so
        # a stale delete (including an ABA revert) fails instead of removing
        # a concurrent change.
        doomed = (
            set(existing)
            if deletable_keys is None
            else set(existing) & set(deletable_keys)
        ) - set(desired)
        for key in sorted(doomed):
            predicate: tuple = (key,)
            statement = (
                f"DELETE FROM {table} WHERE key = {self.backend.param}"
            )
            if (
                baseline_versions_section is not None
                and key in baseline_versions_section
            ):
                base_version, base_checksum = baseline_versions_section[key]
                statement += (
                    f" AND version = {self.backend.param}"
                    f" AND checksum = {self.backend.param}"
                )
                predicate = (key, base_version, base_checksum)
            elif baseline_section is not None and key in baseline_section:
                expected = _checksum(baseline_section[key])
                statement += f" AND checksum = {self.backend.param}"
                predicate = (key, expected)
            removed = self._execute(statement, predicate)
            if removed != 1:
                raise ForgeError(f"optimistic concurrency conflict on {table}.{key}")
            if outcome is not None and section is not None:
                outcome["deleted"].setdefault(section, set()).add(key)

    # -- leases -------------------------------------------------------------

    def acquire_leases(
        self, scopes: list[str], owner: str, root: Path | None = None
    ) -> float:
        # Normalize duplicate/unsorted caller input so all contenders lock
        # scopes in the same order.
        ordered = sorted(set(scopes))
        if not ordered:
            raise ForgeError("cannot acquire an empty storage lease set")
        deadline = time.time() + LEASE_WAIT_SECONDS
        local_root = root
        if local_root is None:
            backend_root = getattr(self.backend, "_root", None)
            if isinstance(backend_root, Path):
                local_root = backend_root
            else:
                backend_path = getattr(self.backend, "_path", None)
                if isinstance(backend_path, Path):
                    local_root = backend_path.parent
        local_host = host_forge_id(local_root) if isinstance(local_root, Path) else None
        local_boot = _linux_boot_id()
        begin_write = getattr(self.backend, "begin_write", self.backend.begin)
        sqlite_backend = self.backend.param == "?"
        while True:
            begin_write(self.connection)
            try:
                now = time.time()
                busy: list[str] = []
                decisions: list[tuple[str, dict | None]] = []
                for scope in ordered:
                    if self.backend.kind == "postgres":
                        row = self._fetchone(
                            "SELECT owner, expires_at FROM storage_locks "
                            f"WHERE scope = {self.backend.param} FOR UPDATE",
                            (scope,),
                        )
                    else:
                        # SQLite BEGIN IMMEDIATE already holds the writer lock,
                        # so this locked read is the conflict check.
                        row = self._fetchone(
                            "SELECT owner, expires_at FROM storage_locks "
                            f"WHERE scope = {self.backend.param}",
                            (scope,),
                        )
                    decisions.append((scope, row))
                for scope, row in decisions:
                    if row is None:
                        self._execute(
                            "INSERT INTO storage_locks "
                            "(scope, owner, heartbeat_at, expires_at) VALUES "
                            f"({self.backend.param}, {self.backend.param}, "
                            f"{self.backend.param}, {self.backend.param})",
                            (scope, owner, now, now + LEASE_TTL_SECONDS),
                        )
                        continue
                    if row["expires_at"] <= now or not _owner_alive(
                        row["owner"], local_host, local_boot
                    ):
                        # Conditional replacement while holding the conflicting
                        # row lock: never an unlocked read plus an
                        # unconditional conflict update.
                        if sqlite_backend:
                            updated = self._execute(
                                "UPDATE storage_locks SET owner = "
                                f"{self.backend.param}, heartbeat_at = "
                                f"{self.backend.param}, expires_at = "
                                f"{self.backend.param} WHERE scope = "
                                f"{self.backend.param} AND owner = "
                                f"{self.backend.param} AND expires_at = "
                                f"{self.backend.param}",
                                (
                                    owner,
                                    now,
                                    now + LEASE_TTL_SECONDS,
                                    scope,
                                    row["owner"],
                                    row["expires_at"],
                                ),
                            )
                        else:
                            updated = self._execute(
                                "UPDATE storage_locks SET owner = "
                                f"{self.backend.param}, heartbeat_at = "
                                f"{self.backend.param}, expires_at = "
                                f"{self.backend.param} WHERE scope = "
                                f"{self.backend.param} AND owner = "
                                f"{self.backend.param}",
                                (owner, now, now + LEASE_TTL_SECONDS, scope, row["owner"]),
                            )
                        if updated != 1:
                            busy.append(scope)
                    else:
                        busy.append(scope)
                if not busy:
                    self.backend.commit(self.connection)
                    return now + LEASE_TTL_SECONDS
                self.backend.rollback(self.connection)
            except ForgeError:
                try:
                    self.backend.rollback(self.connection)
                except Exception:
                    pass
                raise
            except Exception as error:
                try:
                    self.backend.rollback(self.connection)
                except Exception:
                    pass
                message = _sanitize_backend_error(error).lower()
                transient = (
                    "locked" in message
                    or "serialization" in message
                    or "deadlock" in message
                    or "lock timeout" in message
                    or "could not serialize" in message
                )
                if not transient or time.time() >= deadline:
                    # Never leak backend exception text to remote callers.
                    raise ForgeError("cannot acquire storage lease", "storage")
            if time.time() >= deadline:
                break
            time.sleep(LEASE_POLL_SECONDS)
        requested = ", ".join(ordered)
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
        self.heartbeat_leases([scope], owner, expires_at)

    def heartbeat_leases(
        self, scopes: list[str], owner: str, expires_at: float
    ) -> None:
        """Verify and update the exact scope set in one explicit transaction.

        Discovering only the scopes that still happen to carry an owner token
        is insufficient: the caller supplies the exact expected set, the
        implementation updates exactly that count, and commits before
        returning. If any expected scope is absent or has a different owner,
        every update rolls back and lease loss is reported.
        """
        ordered = sorted(set(scopes))
        if not ordered:
            raise ForgeError("cannot heartbeat an empty storage lease set")
        begin_write = getattr(self.backend, "begin_write", self.backend.begin)
        begin_write(self.connection)
        try:
            now = time.time()
            for scope in ordered:
                updated = self._execute(
                    "UPDATE storage_locks SET heartbeat_at = "
                    + f"{self.backend.param}, expires_at = {self.backend.param} "
                    + f"WHERE scope = {self.backend.param} AND owner = {self.backend.param}",
                    (now, expires_at, scope, owner),
                )
                if updated != 1:
                    self.backend.rollback(self.connection)
                    raise ForgeError(f"storage lease lost: {scope}")
            self.backend.commit(self.connection)
        except ForgeError:
            try:
                self.backend.rollback(self.connection)
            except Exception:
                pass
            raise
        except Exception as error:
            try:
                self.backend.rollback(self.connection)
            except Exception:
                pass
            raise ForgeError("cannot heartbeat storage lease", "storage") from None

    def release_leases(self, scopes: list[str], owner: str) -> None:
        ordered = sorted(set(scopes))
        begin_write = getattr(self.backend, "begin_write", self.backend.begin)
        begin_write(self.connection)
        try:
            for scope in ordered:
                self._execute(
                    f"DELETE FROM storage_locks WHERE scope = {self.backend.param} "
                    f"AND owner = {self.backend.param}",
                    (scope, owner),
                )
            self.backend.commit(self.connection)
        except Exception as error:
            try:
                self.backend.rollback(self.connection)
            except Exception:
                pass
            raise ForgeError("cannot release storage lease", "storage") from None

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

    # -- digest-only hosted tokens (spec 013) ----------------------------------

    def _insert_audit_locked(
        self, kind: str, subject: str, detail: str, event_id: str | None = None
    ) -> None:
        self._execute(
            "INSERT INTO audit_log (kind, subject, detail, at, event_id) VALUES "
            f"({self.backend.param}, {self.backend.param}, {self.backend.param}, "
            f"{self.backend.param}, {self.backend.param})",
            (kind, subject, detail, time.time(), event_id),
        )

    def create_http_token(self, record: dict) -> None:
        """Insert one token row and its audit row in a single transaction."""
        from tokens import validate_record

        validated = validate_record(record)
        encoded = json.dumps(validated, sort_keys=True)
        checksum = _checksum(validated)
        begin_write = getattr(self.backend, "begin_write", self.backend.begin)
        begin_write(self.connection)
        try:
            self._insert_placeholder_free(
                f"INSERT INTO {HTTP_TOKENS_TABLE} (key, doc, version, checksum)",
                (validated["token_id"], encoded, 1, checksum),
            )
            # Audit rows never carry digests or secrets.
            self._insert_audit_locked(
                "token.issue",
                f"{validated['view_id']}/{validated['token_id']}",
                f"scopes={','.join(validated['scopes'])}",
            )
            self.backend.commit(self.connection)
        except ForgeError:
            self.backend.rollback(self.connection)
            raise
        except Exception as error:
            self.backend.rollback(self.connection)
            message = _sanitize_backend_error(error).lower()
            if (
                "unique" in message
                or "duplicate" in message
                or "already exists" in message
                or "primary key" in message
            ):
                raise ForgeError("token identifier collision") from None
            raise ForgeError("cannot issue token", "storage") from None

    def load_http_token(self, token_id: str) -> dict | None:
        begin_read = getattr(self.backend, "begin_read", self.backend.begin)
        begin_read(self.connection)
        try:
            rows = self._fetchall(
                f"SELECT doc, checksum FROM {HTTP_TOKENS_TABLE} "
                f"WHERE key = {self.backend.param}",
                (token_id,),
            )
            self.backend.rollback(self.connection)
        except ForgeError:
            raise
        except Exception as error:
            raise ForgeError("cannot read token", "storage") from None
        if not rows:
            return None
        try:
            parsed = json.loads(rows[0]["doc"])
        except json.JSONDecodeError as error:
            raise ForgeError("token row is corrupt", "integrity") from error
        if _checksum(parsed) != rows[0]["checksum"]:
            raise ForgeError("token row failed its integrity checksum", "integrity")
        from tokens import validate_record

        return validate_record(parsed)

    def list_http_tokens(self, view_id: str | None = None) -> list[dict]:
        begin_read = getattr(self.backend, "begin_read", self.backend.begin)
        begin_read(self.connection)
        try:
            rows = self._fetchall(
                f"SELECT key, doc, checksum FROM {HTTP_TOKENS_TABLE} ORDER BY key",
                (),
            )
            self.backend.rollback(self.connection)
        except ForgeError:
            raise
        except Exception as error:
            raise ForgeError("cannot list tokens", "storage") from None
        from tokens import validate_record

        result: list[dict] = []
        for row in rows:
            try:
                parsed = json.loads(row["doc"])
            except json.JSONDecodeError as error:
                raise ForgeError("token row is corrupt", "integrity") from error
            if _checksum(parsed) != row["checksum"]:
                raise ForgeError("token row failed its integrity checksum", "integrity")
            validated = validate_record(parsed)
            if view_id is not None and validated["view_id"] != view_id:
                continue
            result.append(validated)
        return result

    def revoke_http_token(self, token_id: str) -> bool:
        """Mark one token revoked and audit it atomically; False if unknown."""
        import time as _time

        from tokens import validate_record

        begin_write = getattr(self.backend, "begin_write", self.backend.begin)
        begin_write(self.connection)
        try:
            rows = self._fetchall(
                f"SELECT doc, version, checksum FROM {HTTP_TOKENS_TABLE} "
                f"WHERE key = {self.backend.param}",
                (token_id,),
            )
            if not rows:
                self.backend.rollback(self.connection)
                return False
            try:
                parsed = json.loads(rows[0]["doc"])
            except json.JSONDecodeError as error:
                raise ForgeError("token row is corrupt", "integrity") from error
            if _checksum(parsed) != rows[0]["checksum"]:
                raise ForgeError("token row failed its integrity checksum", "integrity")
            validated = validate_record(parsed)
            if validated["revoked_at"] is not None:
                self.backend.rollback(self.connection)
                return True
            validated["revoked_at"] = int(_time.time())
            encoded = json.dumps(validated, sort_keys=True)
            checksum = _checksum(validated)
            updated = self._execute(
                f"UPDATE {HTTP_TOKENS_TABLE} SET doc = {self.backend.param}, "
                f"version = {self.backend.param}, checksum = {self.backend.param} "
                f"WHERE key = {self.backend.param} "
                f"AND version = {self.backend.param}",
                (encoded, int(rows[0]["version"]) + 1, checksum, token_id, rows[0]["version"]),
            )
            if updated != 1:
                raise ForgeError("token revocation conflict")
            self._insert_audit_locked(
                "token.revoke",
                f"{validated['view_id']}/{validated['token_id']}",
                f"scopes={','.join(validated['scopes'])}",
            )
            self.backend.commit(self.connection)
            return True
        except ForgeError:
            try:
                self.backend.rollback(self.connection)
            except Exception:
                pass
            raise
        except Exception as error:
            try:
                self.backend.rollback(self.connection)
            except Exception:
                pass
            raise ForgeError("cannot revoke token", "storage") from None

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


def open_store(root: Path, *, use_pool: bool = True) -> Store:
    """Open ordinary runtime access to an already-provisioned database.

    Never provisions databases and never touches the administrative database:
    a missing scoped database produces one stable storage error.
    """
    record = read_backend_record(root)
    if record["backend"] == "sqlite":
        path = root / "store.sqlite3"
        if not path.exists():
            raise ForgeError("forge state is not initialized", "storage")
        return Store(_SqliteBackend(path))
    if record["backend"] == "durable_objects":
        from durable_store import DurableObjectsBackend
        return Store(DurableObjectsBackend(root))
    return Store(_PostgresBackend(record["dsn"], root, pooled=use_pool))


def initialize_store(
    root: Path,
    database: str | None = None,
    *,
    admin_database: str | None = None,
) -> Store:
    """Create the backend record and current schema for a fresh forge.

    Database provisioning is an initialization operation, never an ordinary
    connection side effect. ``admin_database`` is a separately supplied
    one-shot administrative DSN used to create the root-scoped database; it
    is never persisted. When omitted for PostgreSQL, the scoped database must
    already have been provisioned. The persisted runtime DSN is expected to
    use a role that lacks ``CREATEDB``.
    """
    uri = database_uri(root, database)
    admin_uri = os.environ.get("BASTION_ADMIN_DATABASE") or admin_database
    if uri.startswith("postgres://") or uri.startswith("postgresql://"):
        record = {"backend": "postgres", "dsn": uri}
    elif uri == "durable_objects":
        record = {"backend": "durable_objects"}
    elif uri in ("", "sqlite"):
        record = {"backend": "sqlite"}
    else:
        raise ForgeError(f"unsupported database URI: {uri!r}")
    if record["backend"] == "sqlite":
        store = Store(_SqliteBackend(root / "store.sqlite3"))
    elif record["backend"] == "durable_objects":
        from durable_store import DurableObjectsBackend
        store = Store(DurableObjectsBackend(root))
    else:
        backend = _PostgresBackend(record["dsn"], root, pooled=False)
        # Provisioning happens here (init path) and never in open_store, and
        # only when an explicit administrative DSN is supplied. Without one,
        # the scoped database must already have been provisioned; the connect
        # below then either succeeds or raises one stable storage error
        # without ever contacting a derived admin database.
        if admin_uri:
            try:
                backend.provision_database(admin_uri)
            except ForgeError:
                # No admin rights or other provisioning failure: require the
                # scoped database to have been provisioned already.
                pass
        store = Store(backend)
    try:
        store.bootstrap()
    except Exception:
        try:
            store.close()
        except Exception:
            pass
        raise
    write_backend_record(root, record)
    return store


def backup_forge(root: Path, destination: Path) -> Path:
    """Write a consistent, quiesced backup directory and return its path.

    Catalog, token, and derived-index writers take the ``global`` lease, so
    acquiring it blocks them for the snapshot.  Receives publish through the
    manifest CAS without the lease, so quiescence is additionally verified
    after the database export: the pinned manifest generation must be
    unchanged and the fence must come from the database snapshot actually
    exported.  A racing publication or an index ahead of the pinned WAL
    refuses the backup instead of shipping a snapshot that would fail its
    first catalog write after restore.
    """
    store = open_store(root)
    try:
        document = store.load_document()
        if document.get("repository_state") != "ready":
            raise ForgeError("forge initialization is incomplete")
        owner = owner_identity(root)
        store.acquire_leases(["global"], owner, root)
        try:
            return _backup_repository(root, destination, store)
        finally:
            store.release_leases(["global"], owner)
    finally:
        store.close()


def _sqlite_database_fence(path: Path) -> dict:
    """Read the fence from the exported SQLite image itself."""
    import sqlite3

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT doc FROM forge_meta WHERE id = 1"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ForgeError("backup database has no forge metadata", "integrity")
    meta = json.loads(row[0])
    return {
        "repository_id": meta.get("repository_id"),
        "last_indexed_seq": int(meta.get("last_indexed_seq", 0)),
        "canonical_branch": meta.get("canonical_branch"),
    }


def _postgres_database_fence(tables_dir: Path) -> dict:
    """Read the fence from the exported forge_meta table snapshot."""
    source = tables_dir / "forge_meta.ndjson"
    line = source.read_text().splitlines()[0] if source.exists() else ""
    if not line:
        raise ForgeError("backup database has no forge metadata", "integrity")
    record = json.loads(line)
    meta = record["doc"]
    return {
        "repository_id": meta.get("repository_id"),
        "last_indexed_seq": int(meta.get("last_indexed_seq", 0)),
        "canonical_branch": meta.get("canonical_branch"),
    }


def _database_fence(destination: Path, backend_kind: str) -> dict:
    if backend_kind == "sqlite":
        return _sqlite_database_fence(destination / "database.sqlite3")
    return _postgres_database_fence(destination / "tables")


def restore_forge(
    backup: Path,
    new_root: Path,
    database: str | None = None,
    *,
    admin_database: str | None = None,
    repair_derived: bool = False,
    object_store: str | None = None,
    s3_region: str | None = None,
    s3_expected_bucket_owner: str | None = None,
) -> Path:
    """Reconstruct a forge from a backup directory into ``new_root``.

    A snapshot whose database index is ahead of its pinned WAL is refused
    unless ``repair_derived`` explicitly rebuilds derived rows from the
    WAL.  The repair never touches authoritative catalog or token state.
    """
    manifest_path = backup / "manifest.json"
    try:
        raw = manifest_path.read_bytes()
        if len(raw) > RESTORE_MAX_ARTIFACT_BYTES:
            raise ForgeError("backup manifest is oversized", "integrity")
        manifest = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ForgeError("cannot read backup manifest", "integrity") from error
    if manifest.get("format") != "bastion-wal-backup":
        raise ForgeError("backup format is unsupported")
    if new_root.exists() and any(new_root.iterdir()):
        raise ForgeError(f"restore target is not empty: {new_root}")
    return _restore_repository(
        backup,
        new_root,
        database,
        manifest,
        admin_database,
        repair_derived,
        object_store,
        s3_region,
        s3_expected_bucket_owner,
    )


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _backup_repository(
    root: Path, destination: Path, store: Store
) -> Path:
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
            descriptor_ids.add(payload["descriptor_id"])
            pack_refs.append(payload["canonical_pack"])
        elif record["kind"] == "policy_transition":
            descriptor_ids.update(item["new_descriptor_id"] for item in payload["views"])
    for pack in pack_refs:
        keys.add(pack["pack_key"])
        keys.add(pack["idx_key"])
    keys.update(f"descriptors/{identifier}.json" for identifier in descriptor_ids)
    backend_kind = "sqlite" if store.backend.kind == "durable_objects" else store.backend.kind
    if backend_kind not in {"sqlite", "postgres"}:
        raise ForgeError("unknown storage backend", "storage")
    destination.mkdir(parents=True)
    copied: list[dict] = []
    artifacts: list[dict] = []
    try:
        for key in sorted(keys):
            if key == "manifest.json":
                body = manifest_body
            else:
                object_meta = repository.head(key)
                if object_meta is None:
                    raise ForgeError("backup references a missing object", "integrity")
                if object_meta.size > MAX_OBJECT_SIZE:
                    raise ForgeError("backup object exceeds the qualified size limit", "integrity")
                body = repository.get(key).body
            target = destination / "objects" / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            copied.append({"key": key, "sha256": sha256(body), "size": len(body)})
        # Configuration artifacts: repository.json plus a sanitized backend
        # kind record. The source backend.json is never copied because a
        # PostgreSQL DSN may contain credentials and network location.
        # The host-local forge identifier is never copied into backups.
        config_body = (root / "repository.json").read_bytes()
        (destination / "repository.json").write_bytes(config_body)
        artifacts.append(
            {
                "category": "config",
                "path": "repository.json",
                "size": len(config_body),
                "sha256": sha256(config_body),
            }
        )
        kind_body = canonical_json({"backend": backend_kind})
        (destination / "backend-kind.json").write_bytes(kind_body)
        artifacts.append(
            {
                "category": "config",
                "path": "backend-kind.json",
                "size": len(kind_body),
                "sha256": sha256(kind_body),
            }
        )
        # Spec 013: backups carry only token digests inside the pinned
        # control-plane snapshot (sqlite image or postgres table exports).
        # Legacy raw tokens.json is never backed up.
        if backend_kind == "sqlite":
            store.backend.dump(destination / "database.sqlite3")
            size, digest = _hash_file(destination / "database.sqlite3")
            artifacts.append(
                {
                    "category": "database",
                    "path": "database.sqlite3",
                    "size": size,
                    "sha256": digest,
                }
            )
        else:
            store.backend.dump(destination / "tables")
            for table_file in sorted((destination / "tables").glob("*.ndjson")):
                size, digest = _hash_file(table_file)
                artifacts.append(
                    {
                        "category": "database",
                        "path": f"tables/{table_file.name}",
                        "size": size,
                        "sha256": digest,
                    }
                )
        # The fence comes from the database snapshot actually exported, not
        # from an independently loaded earlier document.  Quiescence is then
        # verified: the pinned manifest generation must be unchanged and the
        # exported index must not be ahead of the pinned WAL.
        fence = _database_fence(destination, backend_kind)
        fresh_repository = Repository.open(root)
        fresh_manifest, _fresh_version, _fresh_replay = fresh_repository.load(use_cache=False)
        if (
            fresh_manifest.head_seq != manifest.head_seq
            or fresh_manifest.wal_head.key != manifest.wal_head.key
        ):
            raise ForgeError(
                "backup raced a repository publication; retry when the forge is quiet",
                "conflict",
            )
        if fence["repository_id"] != repository.repository_id:
            raise ForgeError(
                "backup database belongs to a different repository", "integrity"
            )
        if fence["canonical_branch"] != manifest.canonical_ref.split("/")[-1]:
            raise ForgeError(
                "backup database branch differs from the pinned manifest", "integrity"
            )
        if fence["last_indexed_seq"] > manifest.head_seq:
            raise ForgeError(
                "backup database index is ahead of the pinned WAL", "integrity"
            )
        for entry in copied:
            artifacts.append(
                {
                    "category": "object",
                    "path": f"objects/{entry['key']}",
                    "size": entry["size"],
                    "sha256": entry["sha256"],
                }
            )
        source_config = json.loads(config_body)
        source_kind = source_config.get("object_store", {}).get("kind")
        if source_kind not in {"filesystem", "s3", "artifacts"}:
            raise ForgeError("repository configuration has an unsupported object store", "integrity")
        metadata = {
            "format": "bastion-wal-backup",
            "backup_format_version": 3,
            "created_at": time.time(),
            "repository_id": repository.repository_id,
            "backend": backend_kind,
            "source_object_store_kind": source_kind,
            "head_seq": manifest.head_seq,
            "wal_head": manifest.wal_head.to_dict(),
            "last_indexed_seq": fence["last_indexed_seq"],
            "quiesced": True,
            "manifest_sha256": sha256(manifest_body),
            "objects": copied,
            "artifacts": sorted(artifacts, key=lambda item: item["path"]),
        }
        (destination / "manifest.json").write_bytes(canonical_json(metadata))
        return destination
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _read_backup_object(root: Path, key: str, expected_size: object) -> bytes:
    if (
        not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 0
        or expected_size > MAX_OBJECT_SIZE
    ):
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


def _read_backup_file(backup: Path, relative: str, expected: dict) -> bytes:
    """Bounded regular-file, no-symlink read of a config/database artifact."""
    limits = _restore_limits()
    size = expected.get("size")
    digest = expected.get("sha256")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or size > limits["artifact"]
    ):
        raise ForgeError("WAL backup artifact has an invalid size", "integrity")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ForgeError("WAL backup artifact has an invalid digest", "integrity")
    if not isinstance(relative, str) or not relative.isascii():
        raise ForgeError("WAL backup artifact path is invalid", "integrity")
    if relative.startswith("/") or ".." in relative.split("/"):
        raise ForgeError("WAL backup artifact path is invalid", "integrity")
    path = backup / relative
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise ForgeError("WAL backup artifact is missing", "integrity") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise ForgeError("WAL backup artifact path contains a symlink", "integrity")
    if not stat.S_ISREG(metadata.st_mode):
        raise ForgeError("WAL backup artifact is not a regular file", "integrity")
    if metadata.st_size != size:
        raise ForgeError("WAL backup artifact size differs", "integrity")
    try:
        with path.open("rb") as source:
            body = source.read(size + 1)
    except OSError as error:
        raise ForgeError("WAL backup artifact is unreadable", "integrity") from error
    if len(body) != size:
        raise ForgeError("WAL backup artifact size changed while reading", "integrity")
    if hashlib.sha256(body).hexdigest() != digest:
        raise ForgeError("WAL backup artifact hash differs", "integrity")
    return body


def _validated_backup_manifest(metadata: dict) -> dict:
    if not isinstance(metadata, dict):
        raise ForgeError("WAL backup manifest has an invalid shape", "integrity")
    if metadata.get("format") != "bastion-wal-backup":
        raise ForgeError("backup format is unsupported")
    # Explicit format version distinguishes legacy pre-inventory backups
    # (version 1, no "artifacts" key) from current backups (version 2 with
    # an exact artifact inventory). A current-format manifest with a
    # stripped inventory must fail rather than fall back to unverified
    # legacy reads.
    raw_version = metadata.get("backup_format_version")
    has_artifacts_key = "artifacts" in metadata
    if raw_version is None:
        is_legacy = not has_artifacts_key
        if not is_legacy:
            # Unversioned but inventoried: treat as current (pre-version
            # backups always wrote an inventory) and require exact entries.
            is_legacy = False
    elif raw_version == 1:
        is_legacy = True
        if has_artifacts_key:
            raise ForgeError("WAL backup manifest has an invalid shape", "integrity")
    elif raw_version == 2:
        is_legacy = False
        if not has_artifacts_key:
            raise ForgeError("WAL backup manifest has an invalid shape", "integrity")
    elif raw_version == 3:
        is_legacy = False
        if not has_artifacts_key or metadata.get("source_object_store_kind") not in {"filesystem", "s3", "artifacts"}:
            raise ForgeError("WAL backup manifest has an invalid shape", "integrity")
    else:
        raise ForgeError("backup format is unsupported")
    backend = metadata.get("backend", "sqlite")
    if backend not in {"sqlite", "postgres"}:
        # Backups predating the kind record restore only to sqlite targets.
        backend = "sqlite"
    if not isinstance(metadata.get("repository_id"), str):
        raise ForgeError("WAL backup manifest has an invalid shape", "integrity")
    if not isinstance(metadata.get("head_seq"), int):
        raise ForgeError("WAL backup manifest has an invalid shape", "integrity")
    objects = metadata.get("objects", [])
    artifacts = metadata.get("artifacts", [])
    if not isinstance(objects, list) or not isinstance(artifacts, list):
        raise ForgeError("WAL backup manifest has an invalid shape", "integrity")
    if not is_legacy and not artifacts:
        raise ForgeError("WAL backup artifact inventory is missing", "integrity")
    for entry in artifacts:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"category", "path", "size", "sha256"}
            or entry.get("category") not in {"object", "config", "database"}
            or not isinstance(entry.get("path"), str)
            or not entry["path"].isascii()
            or entry["path"].startswith("/")
            or ".." in entry["path"].split("/")
        ):
            raise ForgeError("WAL backup manifest has an invalid shape", "integrity")
    if raw_version == 3 and set(metadata) - {
        "format", "backup_format_version", "created_at", "repository_id", "backend",
        "source_object_store_kind", "head_seq", "wal_head", "last_indexed_seq", "quiesced",
        "manifest_sha256", "objects", "artifacts",
    }:
        raise ForgeError("WAL backup manifest has an invalid shape", "integrity")
    return {**metadata, "backend": backend, "_legacy_backup": is_legacy}


def _pg_database_exists(dsn: str) -> bool:
    """Whether the scoped database is reachable.

    Only a definitive "invalid catalog" (SQLSTATE 3D000 / missing-database)
    reports absence. Any other failure (auth, network, permissions, missing
    driver) conservatively reports presence so callers never misclassify a
    pre-existing database as absent and later drop it.
    """
    try:
        import psycopg
    except ImportError:
        return True
    try:
        connection = psycopg.connect(dsn)
    except Exception as error:
        state = str(getattr(error, "sqlstate", "") or "")
        message = str(error).lower()
        if state == "3D000":
            return False
        if 'database "' in message and "does not exist" in message:
            return False
        return True
    try:
        connection.close()
    except Exception:
        pass
    return True


def _pg_database_exists_admin(admin_dsn: str, dbname: str) -> bool | None:
    """Check catalog presence via an administrative connection.

    Returns True/False, or None when the admin endpoint is unreachable so
    callers can avoid destructive decisions on ambiguous evidence.
    """
    try:
        import psycopg
    except ImportError:
        return None
    if not dbname:
        return None
    try:
        connection = psycopg.connect(admin_dsn, autocommit=True)
    except Exception:
        return None
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
            return cursor.fetchone() is not None
    except Exception:
        return None
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _pg_drop_database(admin_dsn: str, scoped_dsn: str) -> None:
    try:
        import psycopg
        from psycopg import sql as _sql
    except ImportError:
        return
    parts = urllib.parse.urlsplit(scoped_dsn)
    dbname = parts.path.lstrip("/")
    if not dbname:
        return
    try:
        connection = psycopg.connect(admin_dsn, autocommit=True)
    except Exception:
        return
    try:
        connection.execute(
            _sql.SQL("DROP DATABASE IF EXISTS {}").format(_sql.Identifier(dbname))
        )
    except Exception:
        pass
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _restore_repository(
    backup: Path,
    new_root: Path,
    database: str | None,
    metadata: dict,
    admin_database: str | None = None,
    repair_derived: bool = False,
    object_store: str | None = None,
    s3_region: str | None = None,
    s3_expected_bucket_owner: str | None = None,
) -> Path:
    from object_store_fs import FilesystemObjectStore
    from object_store import Create, validate_key
    from object_store_s3 import S3ObjectStore, S3ObjectStoreConfig, parse_s3_uri
    from object_store_artifacts import ArtifactsObjectStore, ArtifactsObjectStoreConfig, parse_artifacts_uri
    from repository import Repository
    from materialization import ensure_canonical_materialized, ensure_intake_materialized, ensure_view_materialized
    from wal import canonical_json

    if new_root.exists() and any(new_root.iterdir()):
        raise ForgeError(f"restore target is not empty: {new_root}")
    manifest = _validated_backup_manifest(metadata)
    # Target connection configuration arrives separately; the backup records
    # only the backend kind. Cross-backend restore fails before mutation.
    uri = database_uri(new_root, database)
    if uri.startswith("postgres://") or uri.startswith("postgresql://"):
        target_kind = "postgres"
        record = {"backend": "postgres", "dsn": uri}
    else:
        target_kind = "sqlite"
        record = {"backend": "sqlite"}
    if manifest["backend"] != target_kind:
        raise ForgeError("backup restores only to the same backend kind", "integrity")
    admin_uri = os.environ.get("BASTION_ADMIN_DATABASE") or admin_database
    # ---- verify every artifact before creating or modifying any target ----
    artifacts: dict[str, dict] = {}
    for entry in manifest.get("artifacts", []):
        if entry["path"] in artifacts:
            raise ForgeError("WAL backup artifact is duplicated", "integrity")
        artifacts[entry["path"]] = entry
    is_legacy = bool(manifest.get("_legacy_backup"))
    if not is_legacy:
        # Current-format backups require an exact inventory: repository.json,
        # backend-kind.json, and the database image/tables must all be
        # hash-verified. A stripped inventory must fail, never fall back to
        # unverified legacy reads.
        for required in ("repository.json", "backend-kind.json"):
            if required not in artifacts:
                raise ForgeError("WAL backup artifact inventory is incomplete", "integrity")
        if (backup / "tokens.json").exists() and "tokens.json" not in artifacts:
            raise ForgeError("WAL backup contains an unlisted artifact", "integrity")
    if is_legacy:
        try:
            config_body = _read_backup_file(
                backup,
                "repository.json",
                artifacts["repository.json"],
            )
        except (KeyError, ForgeError):
            # True legacy backup without an inventory entry: bounded safe
            # read without a recorded digest.
            config_body = _read_legacy_config(backup / "repository.json")
    else:
        config_body = _read_backup_file(
            backup,
            "repository.json",
            artifacts["repository.json"],
        )
    try:
        config = json.loads(config_body)
    except json.JSONDecodeError as error:
        raise ForgeError("WAL backup configuration is malformed", "integrity") from error
    if not isinstance(config, dict) or not {
        "repository_id", "canonical_branch", "object_store"
    }.issubset(config):
        raise ForgeError("WAL backup configuration has an invalid shape", "integrity")
    if config.get("repository_config_version") not in {None, 2}:
        raise ForgeError("WAL backup configuration version is unsupported", "integrity")
    if not isinstance(config.get("canonical_branch"), str) or not config["canonical_branch"]:
        raise ForgeError("WAL backup configuration has an invalid branch", "integrity")
    if not isinstance(config.get("object_store"), dict):
        raise ForgeError("WAL backup configuration has an invalid object store", "integrity")
    if canonical_json(config) != config_body:
        raise ForgeError("WAL backup configuration is not canonical JSON", "integrity")
    if config.get("repository_id") != manifest.get("repository_id"):
        raise ForgeError("WAL backup repository ID is inconsistent", "integrity")
    source_kind = config.get("object_store", {}).get("kind")
    if source_kind not in {"filesystem", "s3", "artifacts"}:
        raise ForgeError("WAL backup source object store is invalid", "integrity")
    source_store = config["object_store"]
    if config.get("repository_config_version") is None:
        if (
            source_kind != "filesystem"
            or set(config) != {"repository_id", "canonical_branch", "object_store"}
            or set(source_store) != {"kind", "path"}
            or not isinstance(source_store.get("path"), str)
            or not source_store.get("path")
        ):
            raise ForgeError("WAL backup configuration has an invalid shape", "integrity")
    elif config.get("repository_config_version") == 2:
        if set(config) != {
            "canonical_branch", "object_store", "repository_config_version", "repository_id"
        }:
            raise ForgeError("WAL backup configuration has an invalid shape", "integrity")
        if source_kind == "filesystem":
            if (
                set(source_store) != {"kind", "path"}
                or not isinstance(source_store.get("path"), str)
                or not source_store.get("path")
            ):
                raise ForgeError("WAL backup configuration has an invalid object store", "integrity")
        elif source_kind == "s3":
            if set(source_store) != {
                "bucket", "expected_bucket_owner", "kind", "prefix", "region",
                "server_side_encryption",
            }:
                raise ForgeError("WAL backup configuration has an invalid object store", "integrity")
            try:
                S3ObjectStoreConfig(
                    bucket=source_store["bucket"],
                    expected_bucket_owner=source_store["expected_bucket_owner"],
                    region=source_store["region"],
                    prefix=source_store["prefix"],
                    server_side_encryption=source_store["server_side_encryption"],
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ForgeError("WAL backup configuration has an invalid object store", "integrity") from error
        else:
            if set(source_store) != {"kind", "remote", "branch", "token_env"}:
                raise ForgeError("WAL backup configuration has an invalid object store", "integrity")
            try:
                ArtifactsObjectStoreConfig(
                    remote=source_store["remote"],
                    branch=source_store["branch"],
                    token_env=source_store["token_env"],
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ForgeError("WAL backup configuration has an invalid object store", "integrity") from error
    if manifest.get("backup_format_version") == 3 and manifest.get("source_object_store_kind") != source_kind:
        raise ForgeError("WAL backup source object store is inconsistent", "integrity")
    if is_legacy:
        if "backend-kind.json" in artifacts:
            kind_body = _read_backup_file(backup, "backend-kind.json", artifacts["backend-kind.json"])
            try:
                kind_record = json.loads(kind_body)
            except json.JSONDecodeError as error:
                raise ForgeError("WAL backup backend record is malformed", "integrity") from error
            if kind_record.get("backend") != manifest["backend"]:
                raise ForgeError("WAL backup backend record is inconsistent", "integrity")
    else:
        kind_body = _read_backup_file(backup, "backend-kind.json", artifacts["backend-kind.json"])
        try:
            kind_record = json.loads(kind_body)
        except json.JSONDecodeError as error:
            raise ForgeError("WAL backup backend record is malformed", "integrity") from error
        if kind_record.get("backend") != manifest["backend"]:
            raise ForgeError("WAL backup backend record is inconsistent", "integrity")
    backup_objects = backup / "objects"
    try:
        if backup_objects.is_symlink() or not backup_objects.is_dir():
            raise ForgeError("WAL backup object directory is invalid", "integrity")
    except ForgeError:
        raise
    except OSError as error:
        raise ForgeError("WAL backup object directory is invalid", "integrity") from error
    object_entries = manifest.get("objects", [])
    for entry in object_entries:
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
    _reject_unlisted_object_files(backup_objects, object_entries)
    target_s3_config = None
    target_artifacts_config = None
    if object_store is None:
        if s3_region is not None or s3_expected_bucket_owner is not None:
            raise ForgeError("S3 options require an explicit S3 object store")
        target_object_store_path = new_root / "object-store"
    elif object_store.startswith("artifacts+https://"):
        if s3_region is not None or s3_expected_bucket_owner is not None:
            raise ForgeError("S3 options are invalid for Artifacts storage")
        try:
            target_artifacts_config = parse_artifacts_uri(object_store)
        except ValueError as error:
            raise ForgeError(str(error)) from error
        target_object_store_path = None
    elif object_store.startswith("s3://"):
        if not s3_region or not s3_expected_bucket_owner:
            raise ForgeError("S3 region and expected bucket owner are required")
        try:
            target_s3_config = parse_s3_uri(
                object_store,
                region=s3_region,
                expected_bucket_owner=s3_expected_bucket_owner,
            )
        except ValueError as error:
            raise ForgeError(str(error)) from error
        target_object_store_path = None
    else:
        if s3_region is not None or s3_expected_bucket_owner is not None:
            raise ForgeError("S3 options are invalid for filesystem storage")
        if object_store == "filesystem":
            target_object_store_path = new_root / "object-store"
        elif object_store.startswith("filesystem:") and object_store[len("filesystem:") :]:
            raw = object_store[len("filesystem:") :]
            candidate = Path(raw)
            if not candidate.is_absolute():
                if (
                    not raw.isascii()
                    or "\\" in raw
                    or "\x00" in raw
                    or any(part in {"", ".", ".."} for part in raw.split("/"))
                ):
                    raise ForgeError("object store path has an unsafe component")
            target_object_store_path = candidate if candidate.is_absolute() else new_root / candidate
        else:
            raise ForgeError("object store must be filesystem[:PATH], an s3:// URI, or an artifacts+https:// URI")
    target_store = None
    if target_s3_config is not None:
        try:
            target_store = S3ObjectStore.from_config(target_s3_config)
            target_store.verify_bucket()
            if target_store.head(f"repos/{config['repository_id']}/manifest.json") is not None:
                raise ForgeError("restore target manifest already exists", "conflict")
            if target_store.list_prefix(f"repos/{config['repository_id']}/"):
                raise ForgeError("restore target S3 prefix is not unused", "conflict")
        except ForgeError:
            raise
        except ObjectStoreError as error:
            raise ForgeError("object storage is unavailable", "storage") from error
    if target_artifacts_config is not None:
        try:
            with tempfile.TemporaryDirectory(prefix="bastion-artifacts-restore-check-") as check_dir:
                probe_store = ArtifactsObjectStore.from_config(
                    target_artifacts_config,
                    cache_dir=Path(check_dir) / "artifacts-store.git",
                )
                if probe_store.list_prefix(f"repos/{config['repository_id']}/"):
                    raise ForgeError("restore target Artifacts repository namespace is not unused", "conflict")
        except ForgeError:
            raise
        except ObjectStoreError as error:
            raise ForgeError("object storage is unavailable", "storage") from error
    tables_dir = backup / "tables"
    if target_kind == "sqlite":
        if is_legacy:
            if (backup / "database.sqlite3").exists() or "database.sqlite3" in artifacts:
                if "database.sqlite3" in artifacts:
                    _read_backup_file(
                        backup, "database.sqlite3", artifacts["database.sqlite3"]
                    )
                else:
                    # True legacy backup: bounded regular-file check without
                    # a recorded digest.
                    _read_legacy_config(backup / "database.sqlite3")
            else:
                raise ForgeError("WAL backup database image is missing", "integrity")
        else:
            if "database.sqlite3" not in artifacts:
                raise ForgeError("WAL backup database image is unlisted", "integrity")
            _read_backup_file(
                backup, "database.sqlite3", artifacts["database.sqlite3"]
            )
        if tables_dir.exists():
            raise ForgeError("WAL backup has an unexpected table export", "integrity")
    else:
        if (backup / "database.sqlite3").exists():
            raise ForgeError("WAL backup has an unexpected database image", "integrity")
        # Fully parse and shape-check every table before target mutation.
        _read_restore_tables(tables_dir)
        for table in _RESTORE_TABLE_ALLOWLIST:
            expected = artifacts.get(f"tables/{table}.ndjson")
            if expected is None:
                if table == HTTP_TOKENS_TABLE and not (tables_dir / f"{table}.ndjson").exists():
                    continue
                raise ForgeError("WAL backup table is unlisted", "integrity")
            _read_backup_file(backup, f"tables/{table}.ndjson", expected)
        _reject_unlisted_table_files(tables_dir, set(_RESTORE_TABLE_ALLOWLIST))
    # ---- mutate the target; any failure removes the partial target ----
    created_database = False
    if target_kind == "postgres":
        probe = _PostgresBackend(record["dsn"], new_root, pooled=False)
        dbname = urllib.parse.urlsplit(probe._dsn).path.lstrip("/")
        if admin_uri:
            admin_target = probe._admin_target(admin_uri)
            pre_exists = _pg_database_exists_admin(admin_target, dbname)
            if pre_exists is True:
                created_database = False
            elif pre_exists is False:
                created_database = bool(probe.provision_database(admin_uri))
            else:
                # Admin check inconclusive: attempt provisioning but only
                # record creation when CREATE actually created the database
                # (already-exists reports False and never arms the drop).
                try:
                    created_database = bool(probe.provision_database(admin_uri))
                except ForgeError:
                    created_database = False
                    raise
        else:
            # No explicit admin DSN: never contact a derived admin database.
            # The scoped database must already exist; the restore below fails
            # naturally when it does not, without ever arming a drop of a
            # pre-existing database.
            created_database = False
    try:
        new_root.mkdir(parents=True, exist_ok=True)
        (new_root / "views").mkdir(exist_ok=True)
        (new_root / "intake").mkdir(exist_ok=True)
        from repository import write_repository_config

        write_repository_config(
            new_root,
            config["repository_id"],
            config["canonical_branch"],
            target_object_store_path,
            s3_config=target_s3_config,
            artifacts_config=target_artifacts_config,
        )
        # Spec 013: raw tokens.json is never restored. Legacy backups may
        # still ship the file; verify it when listed, then drop it so the
        # restored root starts without legacy bearer state (digest tokens
        # live in the database).
        if "tokens.json" in artifacts:
            _read_backup_file(backup, "tokens.json", artifacts["tokens.json"])
        if target_store is None:
            if target_artifacts_config is not None:
                target_store = ArtifactsObjectStore.from_config(
                    target_artifacts_config,
                    cache_dir=new_root / "materializations" / "artifacts-store.git",
                )
            else:
                assert target_object_store_path is not None
                target_store = FilesystemObjectStore(target_object_store_path)
        ordered_entries = sorted(object_entries, key=lambda entry: entry["key"] == "manifest.json")
        for entry in ordered_entries:
            key = entry["key"]
            body = _read_backup_object(backup_objects, key, entry.get("size"))
            if hashlib.sha256(body).hexdigest() != entry.get("sha256"):
                raise ForgeError("WAL backup object hash differs", "integrity")
            try:
                target_store.put(f"repos/{config['repository_id']}/{key}", body, mode=Create())
            except ObjectStoreError as error:
                raise ForgeError("object storage is unavailable", "storage") from error
        if target_kind == "sqlite":
            write_backend_record(new_root, record)
            try:
                _SqliteBackend(new_root / "store.sqlite3").restore(backup / "database.sqlite3")
                _clear_sqlite_leases(new_root / "store.sqlite3")
            except ForgeError:
                raise
            except Exception as error:
                raise ForgeError("database restore failed", "storage") from None
        else:
            backend = _PostgresBackend(record["dsn"], new_root, pooled=False)
            backend.restore(tables_dir)
            write_backend_record(new_root, record)
        repository = Repository.open(new_root, store=target_store)
        loaded, _version, replay = repository.load()
        if loaded.head_seq != manifest.get("head_seq"):
            raise ForgeError("WAL backup manifest head differs", "integrity")
        # Validate the restored database/WAL fence and repository identity
        # before accepting the target.  Behind-head derived rows are rebuilt
        # from the pinned WAL by recovery; an ahead-of-truth index must fail
        # cleanly or be explicitly repaired by rebuilding derived rows only.
        from state import load_state_sections

        restored_meta = load_state_sections(new_root, [])
        if restored_meta.get("repository_id") != manifest.get("repository_id"):
            raise ForgeError(
                "backup database repository identity differs from the manifest",
                "integrity",
            )
        if restored_meta.get("canonical_branch") != config["canonical_branch"]:
            raise ForgeError(
                "backup database branch differs from the restored repository",
                "integrity",
            )
        restored_indexed = int(restored_meta.get("last_indexed_seq", 0))
        if restored_indexed > loaded.head_seq:
            if not repair_derived:
                raise ForgeError(
                    "backup index is ahead of its WAL; the snapshot is inconsistent",
                    "integrity",
                )
            repair_derived_state(new_root)
        ensure_canonical_materialized(new_root, loaded, repository)
        for view in sorted(replay.active_views):
            materialized = ensure_view_materialized(new_root, view, loaded, replay, repository)
            ensure_intake_materialized(new_root, materialized, loaded)
        return new_root
    except Exception:
        shutil.rmtree(new_root, ignore_errors=True)
        if created_database and target_kind == "postgres":
            probe = _PostgresBackend(record["dsn"], new_root, pooled=False)
            _pg_drop_database(probe._admin_target(admin_uri), probe._dsn)
        raise


def _read_legacy_config(path: Path) -> bytes:
    limits = _restore_limits()
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise ForgeError("WAL backup configuration is missing", "integrity") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise ForgeError("WAL backup configuration path contains a symlink", "integrity")
    if not stat.S_ISREG(metadata.st_mode):
        raise ForgeError("WAL backup configuration is not a regular file", "integrity")
    if metadata.st_size > limits["artifact"]:
        raise ForgeError("WAL backup configuration is oversized", "integrity")
    try:
        return path.read_bytes()
    except OSError as error:
        raise ForgeError("WAL backup configuration is unreadable", "integrity") from error


def _reject_unlisted_object_files(backup_objects: Path, entries: list[dict]) -> None:
    listed = set()
    for entry in entries:
        key = entry.get("key")
        if isinstance(key, str):
            listed.add(key)
    for candidate in backup_objects.rglob("*"):
        try:
            if candidate.is_symlink():
                raise ForgeError("WAL backup object path contains a symlink", "integrity")
            if not candidate.is_file():
                continue
            key = candidate.relative_to(backup_objects).as_posix()
        except ForgeError:
            raise
        except OSError as error:
            raise ForgeError("WAL backup object directory is invalid", "integrity") from error
        if key not in listed:
            raise ForgeError("WAL backup contains an unlisted object file", "integrity")


def _reject_unlisted_table_files(tables_dir: Path, allowed: set[str]) -> None:
    try:
        names = [item.name for item in tables_dir.iterdir()]
    except OSError as error:
        raise ForgeError("database backup directory is unreadable", "integrity") from error
    for name in names:
        if name.startswith("."):
            continue
        if not name.endswith(".ndjson") or name[:-7] not in allowed:
            raise ForgeError("database backup table is unlisted", "integrity")


def repair_derived_state(new_root: Path) -> None:
    """Rebuild WAL-derived rows after an explicit ahead-of-truth repair.

    Only derived state is reset: the views index, transactions, changes,
    and projection jobs are dropped and ``last_indexed_seq`` rewinds so the
    pinned WAL repopulates them.  Authoritative catalog rows, tokens, and
    the audit log are never discarded or invented here.
    """
    from state import load_state, locked, save_state

    store = open_store(new_root)
    try:
        document = store.load_document()
        for section in ("views", "transactions", "changes", "projection_jobs"):
            document[section] = {}
        document["last_indexed_seq"] = 0
        store.save_document(document, [], new_root)
    finally:
        store.close()
    with locked(new_root):
        pass


def _clear_sqlite_leases(path: Path) -> None:
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        connection.execute("DELETE FROM storage_locks")
        connection.commit()
    finally:
        connection.close()
