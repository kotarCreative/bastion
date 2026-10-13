"""Multi-repository hosting on DO catalogs and independent Artifacts branches.

Disk holds only reconstructible configuration, Git caches, and validation work.
The host catalog is distinct from every repository catalog. Initial intake bytes
are persisted before validation, so replacing a container cannot change a pinned
source or turn a retry into a different repository.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from durable_store import CatalogClient, _Connection
from errors import ForgeError
from host import Host, OPAQUE, SCHEMA_VERSION, _read_json
from object_store import Create, ObjectNotFound, PreconditionFailed
from object_store_artifacts import ArtifactsObjectStoreConfig


class _Row:
    def __init__(self, columns, values):
        self.columns, self.values = columns, values

    def keys(self):
        return self.columns

    def __getitem__(self, key):
        return self.values[self.columns.index(key)] if isinstance(key, str) else self.values[key]


class _Rows:
    def __init__(self, cursor):
        self.rows = [_Row([c[0] for c in cursor.description], row) for row in cursor.fetchall()]
        self.rowcount = cursor.rowcount

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class HostConnection:
    """SQLite-shaped host transactions backed by the existing DO SQL bridge."""
    def __init__(self, client, read_only=False):
        self.connection = _Connection(client)
        self.read_only = read_only

    def execute(self, sql, params=()):
        if self.connection.revision is None:
            self.connection.revision = self.connection.client.status()["revision"]
            self.connection.writable = not self.read_only
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            return None  # The DO revision fence supplies the transaction boundary.
        try:
            return _Rows(self.connection.execute(sql, params))
        except ForgeError as error:
            if error.category == "conflict":
                raise sqlite3.IntegrityError("catalog constraint conflict") from error
            raise

    def commit(self):
        connection = self.connection
        if connection.pending or connection.staged:
            if connection.staged:
                connection.staged.commit(connection.pending)
                connection.staged = None
            else:
                connection.client.commit(connection.revision, connection.pending)
        connection.close()

    def rollback(self):
        self.connection.close()

    def close(self):
        self.connection.close()


class CloudflareHost(Host):
    def __init__(self, root, *, client=None, artifacts_store=None, initialize=True, migrate=False):
        from cloudflare_runtime import build_store

        self.client = client or CatalogClient()
        self.catalog = self.client.scoped("host")
        self.artifacts = artifacts_store or build_store()
        self.lock = threading.RLock()
        root = Path(root).expanduser()
        if root.is_symlink() or root.exists() and not root.is_dir():
            raise ForgeError("host workspace is unsafe", "integrity")
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve(strict=True)
        if any(p.name not in {"repos", "locks", "imports"} for p in self.root.iterdir()):
            raise ForgeError("refusing to adopt a legacy or unrelated host workspace", "integrity")
        self._root_identity = (self.root.stat().st_dev, self.root.stat().st_ino)
        for name in ("repos", "locks", "imports"):
            path = self.root / name
            if path.is_symlink():
                raise ForgeError("host workspace is unsafe", "integrity")
            path.mkdir(mode=0o700, exist_ok=True)
        self._directory_ids = {name: ((self.root / name).stat().st_dev, (self.root / name).stat().st_ino)
                               for name in ("repos", "locks", "imports")}
        self.db = self.root / "host.sqlite3"  # Confinement checks only; never created.
        status = self.catalog.status()
        if status["revision"] == 0:
            if not initialize:
                raise ForgeError("host catalog does not exist", "storage")
            if any(any((self.root / name).iterdir()) for name in ("repos", "locks", "imports")):
                raise ForgeError("new catalog cannot adopt populated workspaces", "integrity")
            statements = [{"sql": sql, "params": []} for sql in self._schema_sql().split(";") if sql.strip()]
            statements.append({"sql": "INSERT INTO host_settings VALUES('storage_format','cloudflare-host-v1')", "params": []})
            self.catalog.commit(0, statements)
        with self._connect(read_only=True) as con:
            versions = con.execute("SELECT version FROM schema_info").fetchall()
            format_row = con.execute("SELECT value FROM host_settings WHERE key='storage_format'").fetchone()
            if len(versions) != 1 or versions[0][0] != SCHEMA_VERSION or not format_row or format_row[0] != "cloudflare-host-v1":
                raise ForgeError("unsupported Cloudflare host catalog", "integrity")
        configured = os.environ.get("BASTION_HOST_REPO_LIMIT")
        if configured is not None:
            if not re.fullmatch(r"[1-9][0-9]{0,5}", configured):
                raise ForgeError("invalid host repository limit")
            with self._connect() as con:
                con.execute("UPDATE host_settings SET value=? WHERE key='total_repo_limit'", (configured,))

    @contextmanager
    def _connect(self, *, read_only=False, allow_create=False):
        self._check_layout()
        connection = HostConnection(self.catalog, read_only)
        try:
            yield connection
            if not read_only:
                connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _check_local_config(self):
        # Storage comes from immutable server-controlled repo/catalog identities,
        # never ambient defaults or browser-supplied configuration.
        pass

    def _artifacts_config(self, repo_id):
        if not OPAQUE.fullmatch(repo_id):
            raise ForgeError("invalid repository identity", "integrity")
        return ArtifactsObjectStoreConfig(self.artifacts.config.remote, "bastion-repo-" + repo_id,
                                         self.artifacts.config.token_env)

    def _storage_confined(self, root, backend, config):
        expected = self._artifacts_config(root.name)
        return (backend == {"backend": "durable_objects", "catalog_id": root.name} and
                config.get("object_store") == {"kind": "artifacts", "remote": expected.remote,
                                               "branch": expected.branch, "token_env": expected.token_env} and
                not (root / "store.sqlite3").exists())

    def _check_forge_storage(self, root, branch):
        if (root / "backend.json").exists() and (root / "repository.json").exists():
            config = _read_json(root / "repository.json")
            if not self._storage_confined(root, _read_json(root / "backend.json"), config) or config.get("canonical_branch") != branch:
                raise ForgeError("repository storage identity changed", "integrity")
        for name in ("views", "intake", "materializations", "materialization-locks", "artifacts-cache"):
            path = root / name
            if path.is_symlink() or path.exists() and not path.is_dir():
                raise ForgeError("repository workspace is unsafe", "integrity")

    def _initialize_forge(self, root, source, branch):
        from service import init_forge
        from state import load_state_meta

        self._check_forge_storage(root, branch)
        if (root / "repository.json").exists() and load_state_meta(root).get("repository_state") == "ready":
            return
        init_forge(root, source, branch, database="durable_objects:" + root.name,
                   artifacts_config=self._artifacts_config(root.name))

    def prepare_repository(self, repo_id):
        if not OPAQUE.fullmatch(repo_id):
            raise ForgeError("invalid repository identity", "integrity")
        root = self.root / "repos" / repo_id
        marker = self.root / "locks" / (repo_id + ".owned")
        with self._repo_lock(repo_id):
            with self._connect(read_only=True) as con:
                row = con.execute("SELECT * FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
            if not row:
                raise ForgeError("repository is unavailable", "policy")
            if root.exists():
                self._ensure_owned_root(root, marker, repo_id, row["inputs_hash"])
                self._check_forge_storage(root, row["branch"])
                return
            config = self.client.scoped("repos/" + repo_id).status()["root"]
            if row["state"] == "ready" and config is None:
                raise ForgeError("ready repository lacks durable configuration", "integrity")
            if marker.exists():
                # Missing disk on a running instance is not a fresh workspace.
                raise ForgeError("owned repository workspace disappeared", "integrity")
            self._ensure_owned_root(root, marker, repo_id, row["inputs_hash"])
            if config is not None:
                from store import write_backend_record
                (root / "repository.json").write_text(config["repository"])
                write_backend_record(root, {"backend": "durable_objects", "catalog_id": repo_id})
                self._check_forge_storage(root, row["branch"])
                from state import load_state_meta, locked
                meta = load_state_meta(root)
                if row["state"] == "ready":
                    if meta["repository_id"] != row["repository_id"]:
                        raise ForgeError("repository catalog identity changed", "integrity")
                    with locked(root):
                        pass  # Repair derived indexes against the Artifacts WAL.

    def source_identity(self, source, branch, tip):
        # Stable across a replacement container's inode and filesystem pathname.
        return "import:" + source.parent.name + "\0" + branch + "\0" + tip

    def source_receipt(self, row, tip, info):
        return f"bundle:{row['bundle_sha256']}:{tip}"

    def retained_import_bytes(self, row):
        if row["state"] == "ready" and not (self.root / "imports" / row["import_id"]).exists():
            # Retained remote intake remains charged after disk replacement.
            return row["reserved_stage_bytes"]
        return super().retained_import_bytes(row)

    def persist_import(self, row, envelope):
        key = "imports/" + row["import_id"] + "/" + row["bundle_sha256"]
        try:
            stored = self.artifacts.get(key).body
        except ObjectNotFound:
            stage_bundle = self.root / "imports" / row["import_id"] / "bundle"
            stored = stage_bundle.read_bytes() if stage_bundle.is_file() else envelope
            if len(stored) != row["size_bytes"] or hashlib.sha256(stored).hexdigest() != row["bundle_sha256"]:
                raise ForgeError("import intake identity changed", "integrity")
            try:
                self.artifacts.put(key, stored, mode=Create())
            except PreconditionFailed:
                stored = self.artifacts.get(key).body
        if len(stored) != row["size_bytes"] or hashlib.sha256(stored).hexdigest() != row["bundle_sha256"]:
            raise ForgeError("durable import intake identity changed", "integrity")

    def prepare_import(self, row):
        if row is None:
            return None
        if row["repo_id"]:
            self.prepare_repository(row["repo_id"])
        stage = self.root / "imports" / row["import_id"]
        if stage.exists() or stage.is_symlink() or row["push_tip"] is None or row["state"] == "ready":
            return row
        import repository_import as imports
        import limits

        key = "imports/" + row["import_id"] + "/" + row["bundle_sha256"]
        try:
            envelope = self.artifacts.get(key).body
        except ObjectNotFound:
            if row["source_identity"] is not None or row["repo_id"] is not None:
                raise ForgeError("pinned import intake is missing", "integrity")
            envelope = None  # Died before receiving or durably pinning any bytes.
        if envelope is not None:
            if len(envelope) > limits.IMPORT_BUNDLE_BYTES:
                raise ForgeError("durable import exceeds configured limit", "limits")
            if len(envelope) != row["size_bytes"] or hashlib.sha256(envelope).hexdigest() != row["bundle_sha256"]:
                raise ForgeError("durable import intake identity changed", "integrity")
            tip = imports._preflight_data(envelope, row, time.monotonic() + limits.IMPORT_VALIDATION_SECONDS)
            if tip != row["push_tip"]:
                raise ForgeError("durable import tip changed", "integrity")
            if row["source_identity"] is not None and row["source_identity"] != self.source_receipt(row, tip, None):
                raise ForgeError("durable source receipt changed", "integrity")
        stage.mkdir(mode=0o700)
        info = stage.stat()
        if envelope is not None:
            (stage / "bundle").write_bytes(envelope)
        with self._connect() as con:
            con.execute("UPDATE imports SET stage_dev=?,stage_ino=?,source_dev=NULL,source_ino=NULL,work_name=NULL,work_dev=NULL,work_ino=NULL,work_repo_dev=NULL,work_repo_ino=NULL,upload_temp_name=NULL,upload_temp_dev=NULL,upload_temp_ino=NULL WHERE import_id=?",
                        (info.st_dev, info.st_ino, row["import_id"]))
        return imports.get(self, row["account_id"], row["import_id"])


def bootstrap_account(host):
    """Approve the configured initial account, never seed a repository."""
    email = os.environ.get("BASTION_BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    owner = os.environ.get("BASTION_BOOTSTRAP_OWNER", "admin")
    with host._connect(read_only=True) as con:
        configured = con.execute("SELECT value FROM host_settings WHERE key='bootstrap_account_id'").fetchone()
        accounts = con.execute("SELECT * FROM accounts").fetchall()
    if configured:
        return
    if not email:
        raise RuntimeError("BASTION_BOOTSTRAP_ADMIN_EMAIL is required for initial host onboarding")
    if not accounts:
        account_id = host.approve_account(owner, [email])
    else:
        # Resume a crash after approval, never select an arbitrary existing user.
        matches = [a for a in accounts if a["owner_name"] == owner and json.loads(a["emails"]) == [email]]
        if len(matches) != 1:
            raise ForgeError("initial host administrator does not match configuration", "integrity")
        account_id = matches[0]["account_id"]
    with host._connect() as con:
        con.execute("INSERT INTO host_settings VALUES('bootstrap_account_id',?)", (account_id,))
