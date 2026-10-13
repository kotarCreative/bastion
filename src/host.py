"""Local, operator-managed host catalog for independent forge roots.

This module deliberately supports only a local SQLite catalog and local
filesystem object stores. It is not a hosted account or routing service.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from errors import ForgeError

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
OPAQUE = re.compile(r"^[0-9a-f]{32}$")
RESERVED = {"api", "auth", "assets", "health", "static"}
REMOTE_ENV = ("BASTION_DATABASE", "BASTION_ADMIN_DATABASE", "BASTION_OBJECT_STORE")
SCHEMA_VERSION = 5
HOST_TOKEN_RE = re.compile(r"^bsh1\.([0-9a-f]{32})\.([A-Za-z0-9_-]{43})$")


def _public_branch_name(descriptor: dict, canonical_refs: dict[str, str]) -> str | None:
    """Choose a branch label only from refs readable by this active policy."""
    from permissions import authorized_branch_refs, transport_branch_allowed

    if descriptor.get("algorithm") == "capabilities-v3":
        default_ref = descriptor.get("canonical_ref")
        if isinstance(default_ref, str) and transport_branch_allowed(
            descriptor, "read", default_ref
        ):
            return default_ref.removeprefix("refs/heads/")
        return None
    if descriptor.get("algorithm") != "capabilities-v4":
        return None
    readable = authorized_branch_refs(descriptor, canonical_refs)
    if not readable:
        return None
    selected = descriptor.get("canonical_ref")
    if selected not in readable:
        selected = readable[0]
    return selected.removeprefix("refs/heads/")


def _slug(value: str, label: str) -> str:
    if not isinstance(value, str) or not SLUG.fullmatch(value) or value in RESERVED:
        raise ForgeError(f"invalid {label}; use a lowercase ASCII slug")
    return value


def _git(source: Path, *args: str) -> str:
    from git import clean_env
    import limits
    try:
        result = subprocess.run(["git", "-C", str(source), *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=clean_env(), timeout=limits.GIT_COMMAND_TIMEOUT,
                                check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ForgeError("cannot inspect local Git source") from error
    if result.returncode:
        raise ForgeError("cannot inspect local Git source: " + result.stderr.decode(errors="replace").strip())
    return result.stdout.decode().strip()


def _read_json(path: Path) -> dict:
    try:
        if path.is_symlink():
            raise ForgeError("repository configuration is a symlink", "integrity")
        value = json.loads(path.read_text())
    except ForgeError:
        raise
    except (OSError, json.JSONDecodeError) as error:
        raise ForgeError("cannot read local repository configuration", "integrity") from error
    if not isinstance(value, dict):
        raise ForgeError("local repository configuration has invalid shape", "integrity")
    return value


class Host:
    def __init__(self, root: Path | str, *, initialize: bool = True, migrate: bool = False):
        configured_limit = os.environ.get("BASTION_HOST_REPO_LIMIT")
        if configured_limit is not None:
            try:
                if not re.fullmatch(r"[1-9][0-9]{0,5}", configured_limit):
                    raise ValueError
                configured_limit_value = int(configured_limit)
            except ValueError as error:
                raise ForgeError("BASTION_HOST_REPO_LIMIT must be a positive integer up to 999999") from error
        else:
            configured_limit_value = 1000
        root = Path(root).expanduser()
        if not initialize and (not root.exists() or not (root / "host.sqlite3").is_file()):
            raise ForgeError("host catalog does not exist")
        if root.exists() and root.is_symlink():
            raise ForgeError("host root cannot be a symlink")
        if root.exists() and not root.is_dir():
            raise ForgeError("host root must be a directory")
        if root.exists() and ((root / "backend.json").exists() or (root / "repository.json").exists()):
            raise ForgeError("a forge root cannot be used as a host root")
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ForgeError("host root must be a directory")
        self._root_identity = (self.root.stat().st_dev, self.root.stat().st_ino)
        self._check_local_config()
        allowed = {"host.sqlite3", "host.sqlite3-wal", "host.sqlite3-shm", "host.sqlite3-journal", "repos", "locks", "imports"}
        unknown = [p.name for p in self.root.iterdir() if p.name not in allowed]
        if unknown:
            raise ForgeError("host root contains unrelated files", "integrity")
        imports_path = self.root / "imports"
        if imports_path.is_symlink() or (imports_path.exists() and not imports_path.is_dir()):
            raise ForgeError("host imports path is unsafe", "integrity")
        if imports_path.exists() and any(imports_path.iterdir()):
            database = self.root / "host.sqlite3"
            try:
                probe = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) if database.is_file() else None
                has_import_table = bool(probe and probe.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='imports'").fetchone())
                if probe: probe.close()
            except sqlite3.Error:
                has_import_table = False
            if not has_import_table:
                raise ForgeError("refusing to adopt pre-existing import staging", "integrity")
        if initialize:
            if not (self.root / "host.sqlite3").exists():
                for directory_name in ("repos", "locks", "imports"):
                    path = self.root / directory_name
                    if path.is_symlink() or (path.exists() and not path.is_dir()):
                        raise ForgeError(f"host {directory_name} path is unsafe", "integrity")
                    if path.exists() and any(
                        directory_name != "locks" or child.name != "catalog.lock"
                        for child in path.iterdir()
                    ):
                        raise ForgeError("refusing to initialize over nonempty host directories", "integrity")
            (self.root / "repos").mkdir(exist_ok=True)
            (self.root / "locks").mkdir(exist_ok=True)
            (self.root / "imports").mkdir(mode=0o700, exist_ok=True)
        elif not (self.root / "imports").exists():
            (self.root / "imports").mkdir(mode=0o700)
        for name in ("repos", "locks", "imports"):
            directory = self.root / name
            if directory.is_symlink() or not directory.is_dir():
                raise ForgeError(f"host {name} path is unsafe", "integrity")
        self._directory_ids = {name: ((self.root / name).stat().st_dev, (self.root / name).stat().st_ino)
                               for name in ("repos", "locks", "imports")}
        self.db = self.root / "host.sqlite3"
        if self.db.is_symlink() or (self.db.exists() and not self.db.is_file()):
            raise ForgeError("host database cannot be a symlink")
        if initialize or migrate:
            with self._catalog_lock(), self._connect(allow_create=True) as con:
                # Serialize first-time initialization as well as ordinary writes.
                # A second process can open the file before its schema is committed.
                con.execute("BEGIN IMMEDIATE")
                tables = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                if tables:
                    if "schema_info" not in {row[0] for row in tables}:
                        raise ForgeError("existing SQLite file is not a host catalog", "integrity")
                    versions = con.execute("SELECT version FROM schema_info").fetchall()
                    if len(versions) != 1:
                        raise ForgeError("host catalog schema is invalid", "integrity")
                    version = versions[0]
                    if version and version[0] == 1:
                        self._validate_v1_schema(con)
                        con.execute("CREATE TABLE host_sessions(session_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id), digest TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER)")
                        con.execute("CREATE TABLE host_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                        con.execute("INSERT INTO host_settings VALUES('total_repo_limit','1000')")
                        con.execute("UPDATE schema_info SET version=2")
                        version = (2,)
                    if version and version[0] == 2:
                        self._validate_v1_schema(con)
                        for table, names in {"host_sessions": {"session_id", "account_id", "digest", "created_at", "expires_at", "revoked_at"},
                                             "host_settings": {"key", "value"}}.items():
                            columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
                            if not names.issubset(columns):
                                raise ForgeError("host catalog schema is invalid", "integrity")
                        con.execute("CREATE TABLE membership_intents(intent_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repos(repo_id), account_id TEXT NOT NULL REFERENCES accounts(account_id), principal_id TEXT NOT NULL, email TEXT NOT NULL, clerk_user_id TEXT NOT NULL, inputs_hash TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','active')), created_at INTEGER NOT NULL, UNIQUE(repo_id,account_id), UNIQUE(repo_id,principal_id))")
                        con.execute("UPDATE schema_info SET version=3")
                        version = (3,)
                    if version and version[0] == 3:
                        self._validate_schema_v3(con)
                        con.execute(self._imports_ddl())
                        con.execute("UPDATE schema_info SET version=4")
                        version = (4,)
                    if version and version[0] == 4:
                        self._migrate_v4_import_columns(con)
                        self._validate_schema(con, version=4)
                        import_columns = {row[1] for row in con.execute("PRAGMA table_info(imports)")}
                        if "mode" not in import_columns:
                            con.execute("ALTER TABLE imports ADD COLUMN mode TEXT NOT NULL DEFAULT 'bundle' CHECK(mode IN ('bundle','push'))")
                        if "push_tip" not in import_columns:
                            con.execute("ALTER TABLE imports ADD COLUMN push_tip TEXT")
                        con.execute(self._import_tokens_ddl())
                        con.execute("UPDATE schema_info SET version=5")
                    self._validate_schema(con)
                else:
                    schema = self._schema_sql()
                    for statement in schema.split(";"):
                        if statement.strip():
                            con.execute(statement)
                    con.execute("UPDATE host_settings SET value=? WHERE key='total_repo_limit'", (str(configured_limit_value),))
                if configured_limit is not None:
                    con.execute("UPDATE host_settings SET value=? WHERE key='total_repo_limit'", (str(configured_limit_value),))
                import_identity = ":".join(map(str, self._directory_ids["imports"]))
                persisted_identity = con.execute("SELECT value FROM host_settings WHERE key='imports_identity'").fetchone()
                if persisted_identity and persisted_identity[0] != import_identity:
                    raise ForgeError("host import root identity changed", "integrity")
                if not persisted_identity:
                    if con.execute("SELECT count(*) FROM imports").fetchone()[0] or any((self.root / "imports").iterdir()):
                        raise ForgeError("refusing to adopt unmarked import staging", "integrity")
                    con.execute("INSERT INTO host_settings VALUES('imports_identity',?)", (import_identity,))
        self._schema_ready = True
        if self.db.exists():
            info = self.db.stat()
            self._db_identity = (info.st_dev, info.st_ino)
        if not initialize:
            with self._connect(read_only=True) as con:
                self._validate_schema(con)
                persisted_identity = con.execute("SELECT value FROM host_settings WHERE key='imports_identity'").fetchone()
                if not persisted_identity or persisted_identity[0] != ":".join(map(str, self._directory_ids["imports"])):
                    raise ForgeError("host import root identity changed", "integrity")

    @classmethod
    def _schema_sql(cls):
        return f"""
            CREATE TABLE schema_info(version INTEGER NOT NULL);
                      INSERT INTO schema_info VALUES(5);
            CREATE TABLE IF NOT EXISTS accounts(
              account_id TEXT PRIMARY KEY, owner_name TEXT NOT NULL UNIQUE,
              emails TEXT NOT NULL, clerk_user_id TEXT UNIQUE,
              approved_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
              state TEXT NOT NULL CHECK(state IN ('approved','suspended')),
              repo_limit INTEGER);
            CREATE TABLE IF NOT EXISTS repos(
              repo_id TEXT PRIMARY KEY, owner_account_id TEXT NOT NULL REFERENCES accounts(account_id),
              name TEXT NOT NULL, branch TEXT NOT NULL, source_kind TEXT NOT NULL,
              source_identity TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
              inputs_hash TEXT NOT NULL, created_at INTEGER NOT NULL,
              state TEXT NOT NULL CHECK(state IN ('reserved','provisioning','failed','ready')),
              repository_id TEXT, error TEXT, UNIQUE(owner_account_id,name));
                     CREATE TABLE IF NOT EXISTS memberships(
              repo_id TEXT NOT NULL REFERENCES repos(repo_id), account_id TEXT NOT NULL REFERENCES accounts(account_id),
              principal_id TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY(repo_id,account_id));
                     CREATE TABLE host_sessions(session_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id), digest TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER);
                     CREATE TABLE host_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                     INSERT INTO host_settings VALUES('total_repo_limit','1000');
                      CREATE TABLE membership_intents(intent_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repos(repo_id), account_id TEXT NOT NULL REFERENCES accounts(account_id), principal_id TEXT NOT NULL, email TEXT NOT NULL, clerk_user_id TEXT NOT NULL, inputs_hash TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','active')), created_at INTEGER NOT NULL, UNIQUE(repo_id,account_id), UNIQUE(repo_id,principal_id));
                         {cls._imports_ddl()};
                         {cls._import_tokens_ddl()};
                      """

    @contextmanager
    def _connect(self, *, read_only: bool = False, allow_create: bool = False):
        self._check_layout()
        if (read_only or not allow_create) and not self.db.is_file():
            raise ForgeError("host catalog does not exist", "storage")
        mode = "ro" if read_only else "rwc" if allow_create else "rw"
        uri = self.db.as_uri() + "?mode=" + mode
        try:
            con = sqlite3.connect(uri, uri=True, timeout=30, isolation_level="IMMEDIATE")
        except sqlite3.Error as error:
            raise ForgeError(f"host catalog storage failure: {error}", "storage") from error
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            if not allow_create and self.db.exists():
                self._validate_schema(con)
            if not read_only and con.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                con.execute("PRAGMA journal_mode=WAL")
            yield con
            if not read_only:
                con.commit()
        except sqlite3.Error as error:
            con.rollback()
            raise ForgeError(f"host catalog storage failure: {error}", "storage") from error
        except Exception:
            if not read_only:
                con.rollback()
            raise
        finally:
            con.close()

    def _check_layout(self):
        try:
            self._check_layout_impl()
        except OSError as error:
            raise ForgeError("host filesystem path is unavailable", "storage") from error

    def _check_layout_impl(self):
        if self.root.is_symlink() or not self.root.is_dir() or (
            self.root.stat().st_dev, self.root.stat().st_ino
        ) != self._root_identity:
            raise ForgeError("host root path changed", "integrity")
        for name in ("repos", "locks", "imports"):
            path = self.root / name
            if (path.is_symlink() or not path.is_dir() or path.resolve() != self.root / name or
                (path.stat().st_dev, path.stat().st_ino) != self._directory_ids[name]):
                raise ForgeError(f"host {name} path changed", "integrity")
        for child in (self.root / "repos").iterdir():
            if child.is_symlink() or not child.is_dir() or not OPAQUE.fullmatch(child.name):
                raise ForgeError("host repositories directory contains an unsafe entry", "integrity")
        for child in (self.root / "locks").iterdir():
            seed = re.fullmatch(r"seed-([0-9a-f]{32})\.git", child.name)
            lock_or_marker = re.fullmatch(r"([0-9a-f]{32})\.(?:lock|owned)", child.name)
            import_lock = re.fullmatch(r"import-([0-9a-f]{32})\.lock", child.name)
            marker_temp = re.fullmatch(r"[0-9a-f]{32}\.[A-Za-z0-9_-]+\.owned-tmp", child.name)
            catalog_lock = child.name == "catalog.lock"
            if (child.is_symlink() or not (seed or lock_or_marker or import_lock or marker_temp or catalog_lock) or
                (seed and not child.is_dir()) or
                ((lock_or_marker or import_lock or marker_temp or catalog_lock) and not child.is_file())):
                raise ForgeError("host locks directory contains an unsafe entry", "integrity")
        for child in (self.root / "imports").iterdir():
            if child.is_symlink() or not OPAQUE.fullmatch(child.name) or not child.is_dir():
                raise ForgeError("host imports directory contains an unsafe entry", "integrity")
        if self.db.is_symlink() or (self.db.exists() and not self.db.is_file()):
            raise ForgeError("host database path changed", "integrity")
        if self.db.exists() and hasattr(self, "_db_identity"):
            info = self.db.stat()
            if (info.st_dev, info.st_ino) != self._db_identity:
                raise ForgeError("host database identity changed", "integrity")
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(self.db) + suffix)
            if sidecar.is_symlink():
                raise ForgeError("host database sidecar cannot be a symlink", "integrity")

    @staticmethod
    def _check_local_config():
        for key in REMOTE_ENV:
            value = os.environ.get(key)
            allowed = (key == "BASTION_DATABASE" and value == "sqlite") or (
                key == "BASTION_OBJECT_STORE" and value == "filesystem")
            if value and not allowed:
                raise ForgeError(f"host local mode rejects ambient {key} configuration")
        if os.environ.get("BASTION_S3_REGION") or os.environ.get("BASTION_S3_EXPECTED_BUCKET_OWNER"):
            raise ForgeError("host local mode rejects ambient S3 configuration")

    @staticmethod
    def _ensure_seed(path: Path, branch: str):
        from git import clean_env
        import limits
        if path.is_symlink():
            raise ForgeError("seed source cannot be a symlink", "integrity")

        def run(args, *, data=None, env=None):
            try:
                return subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env=env or clean_env(), timeout=limits.GIT_COMMAND_TIMEOUT, check=False)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise ForgeError("seed repository operation failed", "storage") from error

        if not path.exists():
            result = run(["git", "init", "--bare", "-q", "-b", branch, str(path)])
            if result.returncode:
                raise ForgeError("cannot initialize seed repository", "storage")
        allowed = {"HEAD", "config", "description", "hooks", "info", "objects", "refs"}
        if not path.is_dir() or any(child.name not in allowed for child in path.iterdir()):
            raise ForgeError("seed repository has unexpected content", "integrity")
        bare = run(["git", "-C", str(path), "rev-parse", "--is-bare-repository"])
        if bare.returncode or bare.stdout.strip() != b"true":
            raise ForgeError("seed path is not a bare repository", "integrity")
        env = clean_env()
        env.update({"GIT_AUTHOR_NAME": "Bastion", "GIT_AUTHOR_EMAIL": "bastion-seed@localhost",
                    "GIT_COMMITTER_NAME": "Bastion", "GIT_COMMITTER_EMAIL": "bastion-seed@localhost",
                    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"})
        tree = run(["git", "-C", str(path), "mktree"], data=b"")
        if tree.returncode:
            raise ForgeError("cannot create seed tree", "storage")
        commit = run(["git", "-C", str(path), "commit-tree", tree.stdout.decode().strip(), "-m", "Bastion empty repository seed"], env=env)
        if commit.returncode:
            raise ForgeError("cannot create deterministic seed commit", "storage")
        expected = commit.stdout.decode().strip()
        ref_name = f"refs/heads/{branch}"
        current = run(["git", "-C", str(path), "show-ref", "--hash", ref_name])
        refs = run(["git", "-C", str(path), "for-each-ref", "--format=%(refname)"])
        if refs.returncode or any(line.decode() != ref_name for line in refs.stdout.splitlines()):
            raise ForgeError("seed repository has unexpected refs", "integrity")
        if current.returncode:
            update = run(["git", "-C", str(path), "update-ref", ref_name, expected])
            if update.returncode:
                raise ForgeError("cannot publish seed ref", "storage")
        elif current.stdout.decode().strip() != expected:
            raise ForgeError("seed ref differs from deterministic commit", "integrity")

    @staticmethod
    def _check_forge_storage(root: Path, branch: str) -> None:
        # Validate confinement before opening a store or mutating owner data,
        # rather than discovering a remote backend after those operations.
        if (root / "backend.json").exists() or (root / "backend.json").is_symlink():
            if _read_json(root / "backend.json").get("backend") != "sqlite":
                raise ForgeError("host repository backend is not local SQLite", "integrity")
        if (root / "repository.json").exists() or (root / "repository.json").is_symlink():
            config = _read_json(root / "repository.json")
            if (config.get("object_store") != {"kind": "filesystem", "path": "object-store"} or
                config.get("canonical_branch") != branch):
                raise ForgeError("host repository storage or branch is not confined", "integrity")
        for name in ("object-store", "views", "intake", "materializations", "materialization-locks"):
            path = root / name
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise ForgeError("host repository directory is not confined", "integrity")
        for name in ("store.sqlite3", "store.sqlite3-wal", "store.sqlite3-shm", "store.sqlite3-journal"):
            path = root / name
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ForgeError("host repository SQLite file is unsafe", "integrity")

    def _initialize_forge(self, root: Path, source: Path, branch: str):
        from service import init_forge
        self._check_local_config()
        self._check_forge_storage(root, branch)
        should_init = True
        if (root / "repository.json").exists():
            try:
                from state import load_state
                should_init = load_state(root).get("repository_state") != "ready"
            except ForgeError:
                should_init = True
        if should_init:
            init_forge(root, source, branch, database="sqlite", object_store="filesystem")

    def _provision_owner(self, root: Path, account, branch: str, failpoint, *, provision: bool = True):
        from service import add_principal, add_role, bind_role, add_view
        from identity import empty_capability_map
        from repository import Repository
        from state import load_state

        self._check_forge_storage(root, branch)
        emails = json.loads(account["emails"])
        role_id = "host-owner"
        grants = empty_capability_map()
        for capability in grants:
            grants[capability] = [""]
        state = load_state(root)
        principal = state.get("principals", {}).get("owner")
        if not provision:
            # A ready retry must not recreate revoked grants or missing users or
            # views. The first immutable Clerk link may still be propagated when
            # an approved account was provisioned before its first sign-in.
            role = state.get("roles", {}).get(role_id)
            binding_exists = any(
                binding.get("role_id") == role_id and binding.get("subject_kind") == "principal"
                and binding.get("subject_id") == "owner"
                for binding in state.get("role_bindings", {}).values()
            )
            if (not principal or not role or role.get("allow") != grants or
                not binding_exists or
                principal.get("clerk_user_id") not in (None, account["clerk_user_id"])):
                raise ForgeError("ready repository owner identity or grants changed", "integrity")
        if principal is None:
            add_principal(root, "owner", emails)
            state = load_state(root)
            principal = state["principals"]["owner"]
        elif principal.get("emails") != emails:
            raise ForgeError("owner email identity differs from reserved account", "integrity")
        clerk_id = account["clerk_user_id"]
        if clerk_id:
            linked_id = principal.get("clerk_user_id")
            if linked_id not in (None, clerk_id):
                raise ForgeError("local owner is linked to a different Clerk identity", "integrity")
            if linked_id is None:
                from state import locked, save_state
                with locked(root) as mutable_state:
                    current_owner = mutable_state["principals"].get("owner", {})
                    if (current_owner.get("kind") != "user" or current_owner.get("emails") != emails or
                        current_owner.get("clerk_user_id") not in (None, clerk_id) or any(
                            other_id != "owner" and other.get("clerk_user_id") == clerk_id
                            for other_id, other in mutable_state["principals"].items()
                        )):
                        raise ForgeError("owner identity changed during Clerk linking", "integrity")
                    current_owner["clerk_user_id"] = clerk_id
                    save_state(root, mutable_state)
        if role_id not in state.get("roles", {}):
            add_role(root, role_id, grants, empty_capability_map())
            state = load_state(root)
        elif state["roles"][role_id].get("allow") != grants:
            raise ForgeError("owner role differs from required full grants", "integrity")
        if not any(binding.get("role_id") == role_id and binding.get("subject_kind") == "principal"
                   and binding.get("subject_id") == "owner"
                   for binding in state.get("role_bindings", {}).values()):
            bind_role(root, role_id, "principal", "owner")
        repo = Repository.open(root)
        _manifest, _, replay = repo.load(use_cache=False)
        if "owner" not in replay.active_views:
            if not provision:
                raise ForgeError("ready repository owner view is missing", "integrity")
            add_view(root, "owner", "owner", branch)
        if failpoint:
            failpoint("owner_provisioned")

        repo = Repository.open(root)
        manifest, _, replay = repo.load(use_cache=False)
        view = replay.active_views.get("owner")
        descriptor = repo._descriptor(view["descriptor_id"]) if view else None
        state = load_state(root)
        owner = state.get("principals", {}).get("owner", {})
        binding_ok = any(binding.get("subject_kind") == "principal" and
                         binding.get("subject_id") == "owner" and binding.get("role_id") == role_id
                         for binding in state.get("role_bindings", {}).values())
        backend = _read_json(root / "backend.json")
        config = _read_json(root / "repository.json")
        confinement = self._storage_confined(root, backend, config) and repo.repository_id == state.get("repository_id")
        if (not view or view.get("principal_id") != "owner" or
            view.get("projected_ref") != f"refs/heads/{branch}" or not descriptor or
            descriptor.get("principal_id") != "owner" or
            descriptor.get("canonical_ref") != f"refs/heads/{branch}" or
            manifest.canonical_ref != f"refs/heads/{branch}" or
            any(descriptor.get("capabilities", {}).get(cap) != [""] or
                descriptor.get("restrictions", {}).get(cap) != [] for cap in grants) or
            owner.get("kind") != "user" or owner.get("emails") != emails or
            owner.get("clerk_user_id") != clerk_id or not binding_ok or not confinement or
            state.get("canonical_branch") != branch or state.get("repository_state") != "ready"):
            raise ForgeError("owner assignment verification failed", "integrity")
        return repo

    def _storage_confined(self, root, backend, config):
        return (
            backend.get("backend") == "sqlite" and
            config.get("object_store") == {"kind": "filesystem", "path": "object-store"} and
            (root / "object-store").resolve() == root / "object-store" and
            (root / "store.sqlite3").is_file() and not (root / "store.sqlite3").is_symlink()
        )

    def prepare_repository(self, repo_id):
        """Restore disposable configuration in hosted mode; local roots are durable."""

    def persist_import(self, row, envelope):
        """Local intake is already durable; hosted adapters publish it remotely."""

    def prepare_import(self, row):
        """Return the operation after restoring any remote intake workspace."""
        return row

    def source_receipt(self, row, tip, info):
        return f"bundle:{row['bundle_sha256']}:{tip}:{info.st_dev}:{info.st_ino}"

    def source_identity(self, source, branch, tip):
        return f"{source}\0{branch}\0{tip}"

    def retained_import_bytes(self, row):
        from repository_import import _ready_stage_bytes
        return _ready_stage_bytes(self, row)

    @staticmethod
    def _validate_v1_schema(con):
        required = {
            "schema_info": {"version"},
            "accounts": {"account_id", "owner_name", "emails", "clerk_user_id", "approved_at", "created_at", "state", "repo_limit"},
            "repos": {"repo_id", "owner_account_id", "name", "branch", "source_kind", "source_identity", "request_id", "inputs_hash", "created_at", "state", "repository_id", "error"},
            "memberships": {"repo_id", "account_id", "principal_id", "state"},
        }
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not set(required).issubset(tables):
                raise ForgeError("host catalog schema is invalid", "integrity")
            columns = {table: {row[1] for row in con.execute(f"PRAGMA table_info({table})")} for table in required}
            if any(not columns[table].issuperset(names) for table, names in required.items()):
                raise ForgeError("host catalog schema is invalid", "integrity")
        except sqlite3.Error as error:
            raise ForgeError("host catalog schema is invalid", "integrity") from error

    @staticmethod
    def _validate_schema(con, version=SCHEMA_VERSION):
        try:
            versions = con.execute("SELECT version FROM schema_info").fetchall()
            stored_version = versions[0] if len(versions) == 1 else None
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.Error as error:
            raise ForgeError("existing SQLite file is not a host catalog", "integrity") from error
        expected = {"schema_info", "accounts", "repos", "memberships", "host_sessions", "host_settings", "membership_intents", "imports"}
        if version >= 5:
            expected.add("import_tokens")
        if not stored_version or stored_version[0] != version or not expected.issubset(tables):
            raise ForgeError("host catalog schema is unsupported", "integrity")
        columns = {table: {row[1] for row in con.execute(f"PRAGMA table_info({table})")} for table in expected}
        required = {
            "schema_info": {"version"},
            "accounts": {"account_id", "owner_name", "emails", "clerk_user_id", "approved_at", "created_at", "state", "repo_limit"},
            "repos": {"repo_id", "owner_account_id", "name", "branch", "source_kind", "source_identity", "request_id", "inputs_hash", "created_at", "state", "repository_id", "error"},
            "memberships": {"repo_id", "account_id", "principal_id", "state"},
            "host_sessions": {"session_id", "account_id", "digest", "created_at", "expires_at", "revoked_at"},
            "host_settings": {"key", "value"},
            "membership_intents": {"intent_id", "repo_id", "account_id", "principal_id", "email", "clerk_user_id", "inputs_hash", "state", "created_at"},
            "imports": {"import_id", "account_id", "name", "branch", "request_id", "bundle_sha256", "size_bytes", "state", "source_identity", "repo_id", "error", "created_at", "stage_dev", "stage_ino", "source_dev", "source_ino", "work_name", "work_dev", "work_ino", "work_repo_dev", "work_repo_ino", "upload_temp_name", "upload_temp_dev", "upload_temp_ino", "reserved_stage_bytes"},
        }
        if version >= 5:
            required["imports"].update({"mode", "push_tip"})
            required["import_tokens"] = {"import_id", "token_id", "digest", "created_at", "expires_at"}
        if any(not required[table].issubset(columns[table]) for table in required):
            raise ForgeError("host catalog table shape is invalid", "integrity")

    @staticmethod
    def _validate_schema_v3(con):
        Host._validate_v1_schema(con)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"host_sessions", "host_settings", "membership_intents"}
        if not required.issubset(tables):
            raise ForgeError("host catalog schema is invalid", "integrity")
        for table, names in {
            "host_sessions": {"session_id", "account_id", "digest", "created_at", "expires_at", "revoked_at"},
            "host_settings": {"key", "value"},
            "membership_intents": {"intent_id", "repo_id", "account_id", "principal_id", "email", "clerk_user_id", "inputs_hash", "state", "created_at"},
        }.items():
            if not names.issubset({r[1] for r in con.execute(f"PRAGMA table_info({table})")}):
                raise ForgeError("host catalog schema is invalid", "integrity")

    @staticmethod
    def _imports_ddl():
        return "CREATE TABLE IF NOT EXISTS imports(import_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id), name TEXT NOT NULL, branch TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE, bundle_sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('awaiting-upload','uploaded','validating','provisioning','ready','failed')), source_identity TEXT, repo_id TEXT UNIQUE, error TEXT, created_at INTEGER NOT NULL, stage_dev INTEGER, stage_ino INTEGER, source_dev INTEGER, source_ino INTEGER, work_name TEXT, work_dev INTEGER, work_ino INTEGER, work_repo_dev INTEGER, work_repo_ino INTEGER, upload_temp_name TEXT, upload_temp_dev INTEGER, upload_temp_ino INTEGER, reserved_stage_bytes INTEGER NOT NULL DEFAULT 0, mode TEXT NOT NULL DEFAULT 'bundle' CHECK(mode IN ('bundle','push')), push_tip TEXT, UNIQUE(account_id,name))"

    @staticmethod
    def _import_tokens_ddl():
        return "CREATE TABLE import_tokens(import_id TEXT PRIMARY KEY REFERENCES imports(import_id), token_id TEXT NOT NULL UNIQUE, digest TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)"

    @staticmethod
    def _migrate_v4_import_columns(con):
        columns = {row[1] for row in con.execute("PRAGMA table_info(imports)")}
        required_base = {"import_id", "account_id", "name", "branch", "request_id", "bundle_sha256", "size_bytes", "state", "source_identity", "repo_id", "error", "created_at"}
        if not required_base.issubset(columns):
            raise ForgeError("host import catalog schema is invalid", "integrity")
        missing = {
            "stage_dev": "INTEGER", "stage_ino": "INTEGER", "source_dev": "INTEGER", "source_ino": "INTEGER",
            "work_name": "TEXT", "work_dev": "INTEGER", "work_ino": "INTEGER", "work_repo_dev": "INTEGER", "work_repo_ino": "INTEGER",
            "upload_temp_name": "TEXT", "upload_temp_dev": "INTEGER", "upload_temp_ino": "INTEGER",
            "reserved_stage_bytes": "INTEGER NOT NULL DEFAULT 0",
        }
        additions = {name: ddl for name, ddl in missing.items() if name not in columns}
        if additions and con.execute("SELECT count(*) FROM imports").fetchone()[0]:
            raise ForgeError("cannot safely migrate nonempty v4 import operations", "integrity")
        for name, ddl in additions.items():
            con.execute(f"ALTER TABLE imports ADD COLUMN {name} {ddl}")

    def approve_account(self, owner_name: str, emails: list[str], *, account_id: str | None = None, repo_limit: int | None = None) -> str:
        self._check_local_config()
        owner_name = _slug(owner_name, "owner name")
        if not isinstance(emails, list) or not emails or any(not isinstance(e, str) or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e) for e in emails):
            raise ForgeError("provide one or more valid verified email addresses")
        emails = sorted({e.strip().lower() for e in emails})
        account_id = account_id or uuid.uuid4().hex
        if not OPAQUE.fullmatch(account_id):
            raise ForgeError("account id must be a 32-character lowercase hex identifier")
        if repo_limit is not None and (not isinstance(repo_limit, int) or isinstance(repo_limit, bool) or repo_limit < 1):
            raise ForgeError("repo limit must be a positive integer or omitted")
        now = int(time.time())
        try:
            with self._connect() as con:
                try:
                    con.execute("INSERT INTO accounts VALUES(?,?,?,?,?,?, 'approved',?)",
                                (account_id, owner_name, json.dumps(emails), None, now, now, repo_limit))
                except sqlite3.IntegrityError as error:
                    raise ForgeError("account id or owner name already exists", "conflict") from error
        except sqlite3.IntegrityError as e:
            raise ForgeError("account id or owner name already exists", "conflict") from e
        return account_id

    def list_repos(self, owner_name: str | None = None) -> list[dict]:
        self._check_local_config()
        # Opening Host validates config and initializes schema; this read never creates repo roots.
        with self._connect(read_only=True) as con:
            rows = con.execute("SELECT r.*,a.owner_name FROM repos r JOIN accounts a ON a.account_id=r.owner_account_id" +
                               (" WHERE a.owner_name=?" if owner_name else "") + " ORDER BY a.owner_name,r.name",
                               (owner_name,) if owner_name else ()).fetchall()
        return [dict(row) for row in rows]

    def link_clerk_account(self, clerk_user_id: str, verified_emails: list[str]) -> str:
        """Link a verified Clerk identity only to one unambiguous approved account."""
        self._check_local_config()
        if not isinstance(clerk_user_id, str) or not re.fullmatch(r"user_[A-Za-z0-9_-]{1,250}", clerk_user_id):
            raise ForgeError("invalid Clerk user identity")
        if not isinstance(verified_emails, list) or not verified_emails or any(
            not isinstance(email, str) or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
            for email in verified_emails
        ):
            raise ForgeError("Clerk account has no verified email")
        verified = {email.strip().lower() for email in verified_emails}
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            linked = con.execute("SELECT account_id,state FROM accounts WHERE clerk_user_id=?", (clerk_user_id,)).fetchone()
            if linked:
                if linked["state"] != "approved":
                    raise ForgeError("linked host account is not approved", "policy")
                con.commit()
                account_id = linked["account_id"]
                self._link_local_owner_principals(account_id, clerk_user_id, verified)
                return linked["account_id"]
            matches = [row for row in con.execute("SELECT account_id,emails FROM accounts WHERE state='approved' AND clerk_user_id IS NULL")
                       if verified.intersection(json.loads(row["emails"]))]
            if len(matches) != 1:
                raise ForgeError("verified email does not identify exactly one approved account", "policy")
            con.execute("UPDATE accounts SET clerk_user_id=? WHERE account_id=?",
                        (clerk_user_id, matches[0]["account_id"]))
            con.commit()
            self._link_local_owner_principals(matches[0]["account_id"], clerk_user_id, verified)
            return matches[0]["account_id"]

    def _link_local_owner_principals(self, account_id: str, clerk_user_id: str, verified: set[str]) -> None:
        with self._connect(read_only=True) as con:
            rows = con.execute("SELECT r.repo_id FROM repos r JOIN memberships m USING(repo_id) WHERE r.owner_account_id=? AND m.account_id=? AND m.principal_id='owner' AND r.state='ready'", (account_id, account_id)).fetchall()
        from state import load_state, locked, save_state
        for row in rows:
            if not OPAQUE.fullmatch(row["repo_id"]):
                raise ForgeError("host repository id is invalid", "integrity")
            self.prepare_repository(row["repo_id"])
            root = self.root / "repos" / row["repo_id"]
            self._check_layout()
            marker = _read_json(self.root / "locks" / f"{row['repo_id']}.owned")
            info = root.stat()
            if root.is_symlink() or marker.get("repo_id") != row["repo_id"] or marker.get("root_identity") != [info.st_dev, info.st_ino]:
                raise ForgeError("host repository root identity changed", "integrity")
            state = load_state(root)
            owner = state.get("principals", {}).get("owner")
            if not owner or owner.get("kind") != "user" or not verified.intersection(owner.get("emails", [])):
                continue
            if owner.get("clerk_user_id") not in (None, clerk_user_id):
                raise ForgeError("local owner is linked to a different Clerk identity", "integrity")
            if owner.get("clerk_user_id") is None:
                with locked(root) as current:
                    principal = current.get("principals", {}).get("owner", {})
                    if (principal.get("kind") != "user" or
                        not verified.intersection(principal.get("emails", [])) or
                        principal.get("clerk_user_id") not in (None, clerk_user_id) or
                        any(pid != "owner" and item.get("clerk_user_id") == clerk_user_id
                            for pid, item in current.get("principals", {}).items())):
                        raise ForgeError("local owner identity changed during Clerk linking", "integrity")
                    principal["clerk_user_id"] = clerk_user_id
                    save_state(root, current)

    def issue_session(self, account_id: str, lifetime: int = 3600) -> str:
        if not isinstance(lifetime, int) or isinstance(lifetime, bool) or not 1 <= lifetime <= 86400:
            raise ForgeError("host session lifetime is outside supported bounds")
        secret = secrets.token_urlsafe(32)
        session_id = uuid.uuid4().hex
        now = int(time.time())
        digest = hashlib.sha256(("bastion-host-session\0" + session_id + "\0" + secret).encode()).hexdigest()
        with self._connect() as con:
            row = con.execute("SELECT state FROM accounts WHERE account_id=?", (account_id,)).fetchone()
            if not row or row["state"] != "approved":
                raise ForgeError("host account is not approved", "policy")
            con.execute("INSERT INTO host_sessions VALUES(?,?,?,?,?,NULL)",
                        (session_id, account_id, digest, now, now + lifetime))
        return f"bsh1.{session_id}.{secret}"

    def is_host_admin(self, account_id):
        with self._connect(read_only=True) as con:
            configured = con.execute("SELECT value FROM host_settings WHERE key='bootstrap_account_id'").fetchone()
            approved = con.execute("SELECT 1 FROM accounts WHERE account_id=? AND state='approved'", (account_id,)).fetchone()
        return bool(configured and configured[0] == account_id and approved)

    def authenticate_session(self, token: str) -> dict | None:
        match = HOST_TOKEN_RE.fullmatch(token) if isinstance(token, str) else None
        supplied = "bastion-host-session\0invalid\0invalid"
        if match:
            session_id, secret = match.groups()
            supplied = f"bastion-host-session\0{session_id}\0{secret}"
        digest = hashlib.sha256(supplied.encode()).hexdigest()
        try:
            with self._connect(read_only=True) as con:
                row = con.execute("SELECT s.*,a.owner_name,a.state,a.emails FROM host_sessions s JOIN accounts a USING(account_id) WHERE session_id=?", (session_id if match else "",)).fetchone()
        except ForgeError:
            return None
        row_valid = (row is not None and isinstance(row["digest"], str) and
                     re.fullmatch(r"[0-9a-f]{64}", row["digest"]) and
                     isinstance(row["created_at"], int) and isinstance(row["expires_at"], int) and
                     row["created_at"] < row["expires_at"] and isinstance(row["account_id"], str) and
                     OPAQUE.fullmatch(row["account_id"]))
        expected = row["digest"] if row_valid else hashlib.sha256(b"bastion-host-dummy").hexdigest()
        valid = secrets.compare_digest(expected, digest)
        if (not match or not row_valid or not valid or row["revoked_at"] is not None or row["expires_at"] <= int(time.time())
                or row["state"] != "approved"):
            return None
        return {key: row[key] for key in ("account_id", "owner_name", "emails")}

    def revoke_session(self, token: str) -> bool:
        match = HOST_TOKEN_RE.fullmatch(token) if isinstance(token, str) else None
        if not match:
            return False
        session_id, secret = match.groups()
        digest = hashlib.sha256(("bastion-host-session\0" + session_id + "\0" + secret).encode()).hexdigest()
        with self._connect() as con:
            cur = con.execute("UPDATE host_sessions SET revoked_at=? WHERE session_id=? AND digest=? AND revoked_at IS NULL", (int(time.time()), session_id, digest))
            return cur.rowcount == 1

    def public_repos(self, account_id: str) -> list[dict]:
        self.reconcile_membership_intents(account_id)
        with self._connect(read_only=True) as con:
            account = con.execute("SELECT * FROM accounts WHERE account_id=? AND state='approved'", (account_id,)).fetchone()
            if not account:
                return []
            rows = con.execute("SELECT r.repo_id,r.name,r.branch,r.repository_id,r.inputs_hash,a.owner_name,m.principal_id FROM repos r JOIN memberships m USING(repo_id) JOIN accounts a ON a.account_id=r.owner_account_id WHERE m.account_id=? AND m.state='active' AND r.state='ready' ORDER BY a.owner_name,r.name", (account_id,)).fetchall()
        result = []
        for row in rows:
            try:
                self.prepare_repository(row["repo_id"])
                root = self.root / "repos" / row["repo_id"]
                self._check_layout()
                if root.is_symlink() or not root.is_dir() or root.resolve(strict=True).parent != (self.root / "repos").resolve(strict=True):
                    continue
                marker = _read_json(self.root / "locks" / f"{row['repo_id']}.owned")
                root_info = root.stat()
                if (set(marker) != {"repo_id", "inputs_hash", "root_identity"} or marker.get("repo_id") != row["repo_id"] or
                    marker.get("inputs_hash") != row["inputs_hash"] or marker.get("root_identity") != [root_info.st_dev, root_info.st_ino]):
                    continue
                from repository import Repository
                from state import load_state_sections
                repository = Repository.open(root)
                _manifest, _version, replay = repository.load(use_cache=False)
                from state import load_state_sections
                state = load_state_sections(root, ["principals"])
                identity = state["principals"].get(row["principal_id"])
                views = sorted(v for v, item in replay.active_views.items() if item["principal_id"] == row["principal_id"])
                active = [replay.active_views[v] for v in views]
                root_path = root.resolve(strict=True)
                marker = _read_json(self.root / "locks" / f"{row['repo_id']}.owned")
                info = root.stat()
                owner_exception = (account_id == self._repo_owner_id(row["repo_id"]) and
                                   row["principal_id"] == "owner" and identity and
                                   identity.get("clerk_user_id") is None and
                                   sorted(identity.get("emails", [])) == sorted(json.loads(account["emails"]))
                                   and identity.get("kind") == "user")
                linked = identity and (identity.get("clerk_user_id") == account["clerk_user_id"]
                                       if account["clerk_user_id"] else owner_exception)
                descriptors = [repository._descriptor(v["descriptor_id"]) for v in active]
                descriptor_valid = all(
                    descriptor.get("principal_id") == row["principal_id"]
                    and descriptor.get("canonical_ref") == f"refs/heads/{row['branch']}"
                    for descriptor in descriptors
                )
                visible_branches = {
                    _public_branch_name(descriptor, _manifest.canonical_refs)
                    for descriptor in descriptors
                }
                # Multiple active views for one membership should not make
                # discovery guess which policy governs the public label.
                public_branch = (
                    next(iter(visible_branches)) if len(visible_branches) == 1 else None
                )
                self._check_forge_storage(root, row["branch"])
                if (not identity or identity.get("kind") != "user" or not linked or not views or not descriptor_valid or
                    any(not v.get("descriptor_id") or v.get("principal_id") != row["principal_id"] or
                        (descriptor.get("algorithm") == "capabilities-v3" and
                         v.get("projected_ref") != f"refs/heads/{row['branch']}") for v, descriptor in zip(active, descriptors)) or
                    state.get("repository_id") != row["repository_id"] or
                    state.get("repository_state") != "ready" or
                    marker.get("repo_id") != row["repo_id"] or
                    marker.get("root_identity") != [info.st_dev, info.st_ino] or
                    root_path.parent != (self.root / "repos").resolve(strict=True) or
                    not SLUG.fullmatch(row["name"]) or not SLUG.fullmatch(row["owner_name"])):
                    continue
            except ForgeError as error:
                if error.category == "storage":
                    raise
                continue
            except OSError as error:
                raise ForgeError("host repository storage is unavailable", "storage") from error
            except Exception:
                continue
            result.append({"repo_id": row["repo_id"], "owner_name": row["owner_name"], "name": row["name"],
                           "branch": public_branch, "git_url": f"/{row['owner_name']}/{row['name']}.git", "views": views})
        return result

    def reconcile_membership_intents(self, account_id: str) -> None:
        with self._connect(read_only=True) as con:
            pending = con.execute("SELECT repo_id,principal_id FROM membership_intents WHERE account_id=? AND state='pending'", (account_id,)).fetchall()
        for intent in pending:
            try:
                self.activate_membership_intent(intent["repo_id"], account_id, intent["principal_id"])
            except ForgeError:
                # Never expose a pending/corrupt mapping; WAL publication may
                # still be in flight and a later request can retry reconciliation.
                continue

    def _repo_owner_id(self, repo_id: str) -> str | None:
        with self._connect(read_only=True) as con:
            row = con.execute("SELECT owner_account_id FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
        return row[0] if row else None

    def _account_owner(self, account_id: str) -> str:
        with self._connect(read_only=True) as con:
            row = con.execute("SELECT owner_name FROM accounts WHERE account_id=? AND state='approved'", (account_id,)).fetchone()
        if not row:
            raise ForgeError("host account is unavailable", "policy")
        return row[0]

    def reserve_membership_intent(self, repo_id: str, account_id: str, principal_id: str,
                                  email: str, clerk_user_id: str, inputs_hash: str) -> dict:
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            target = con.execute("SELECT emails,state,clerk_user_id FROM accounts WHERE account_id=?", (account_id,)).fetchone()
            repo = con.execute("SELECT state FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
            if (not target or target["state"] != "approved" or target["clerk_user_id"] != clerk_user_id
                    or email.lower() not in json.loads(target["emails"]) or not repo or repo["state"] != "ready"):
                raise ForgeError("invitation target is unavailable", "policy")
            existing = con.execute("SELECT * FROM membership_intents WHERE repo_id=? AND account_id=?", (repo_id, account_id)).fetchone()
            if existing:
                if existing["inputs_hash"] != inputs_hash or existing["principal_id"] != principal_id:
                    raise ForgeError("invitation conflicts with an existing membership", "conflict")
                membership = con.execute("SELECT state FROM memberships WHERE repo_id=? AND account_id=?", (repo_id, account_id)).fetchone()
                if membership and membership["state"] != "active":
                    raise ForgeError("membership discovery has been disabled", "policy")
                return dict(existing)
            membership = con.execute("SELECT principal_id FROM memberships WHERE repo_id=? AND account_id=?", (repo_id, account_id)).fetchone()
            if membership and membership["principal_id"] != principal_id:
                raise ForgeError("host account already has a different repository identity", "conflict")
            intent_id = uuid.uuid4().hex
            con.execute("INSERT INTO membership_intents VALUES(?,?,?,?,?,?,?,'pending',?)",
                        (intent_id, repo_id, account_id, principal_id, email.lower(), clerk_user_id,
                         inputs_hash, int(time.time())))
            return dict(con.execute("SELECT * FROM membership_intents WHERE intent_id=?", (intent_id,)).fetchone())

    def activate_membership_intent(self, repo_id: str, account_id: str, principal_id: str) -> None:
        """Activate discovery only after verifying exact identity and committed WAL view."""
        with self._connect(read_only=True) as con:
            intent = con.execute("SELECT * FROM membership_intents WHERE repo_id=? AND account_id=? AND principal_id=?", (repo_id, account_id, principal_id)).fetchone()
            repo = con.execute("SELECT repository_id,inputs_hash,state FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
            account = con.execute("SELECT clerk_user_id,emails,state FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        if not intent or not repo or repo["state"] != "ready" or not account or account["state"] != "approved":
            raise ForgeError("membership intent is not active", "policy")
        self.prepare_repository(repo_id)
        root = self.root / "repos" / repo_id
        self._check_layout()
        if root.is_symlink() or not root.is_dir() or root.resolve(strict=True).parent != (self.root / "repos").resolve(strict=True):
            raise ForgeError("membership repository root is unsafe", "integrity")
        marker = _read_json(self.root / "locks" / f"{repo_id}.owned")
        root_info = root.stat()
        if (set(marker) != {"repo_id", "inputs_hash", "root_identity"} or marker.get("repo_id") != repo_id or
            marker.get("inputs_hash") != repo["inputs_hash"] or marker.get("root_identity") != [root_info.st_dev, root_info.st_ino]):
            raise ForgeError("membership repository ownership changed", "integrity")
        from repository import Repository
        from state import load_state_sections
        repository = Repository.open(root)
        _manifest, _version, replay = repository.load(use_cache=False)
        state = load_state_sections(root, ["principals"])
        principal = state["principals"].get(principal_id)
        views = [item for item in replay.active_views.values() if item["principal_id"] == principal_id]
        if (not principal or principal.get("kind") != "user" or
            principal.get("clerk_user_id") != intent["clerk_user_id"] or
            intent["clerk_user_id"] != account["clerk_user_id"] or
            intent["email"] not in principal.get("emails", []) or
            intent["email"] not in json.loads(account["emails"]) or not views or
            state.get("repository_id") != repo["repository_id"] or state.get("repository_state") != "ready" or
            any(repository._descriptor(view["descriptor_id"]).get("principal_id") != principal_id or
                view.get("projected_ref") != f"refs/heads/{state['canonical_branch']}" for view in views)):
            raise ForgeError("local invitation identity does not match host account", "integrity")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute("SELECT state FROM membership_intents WHERE intent_id=?", (intent["intent_id"],)).fetchone()
            if not current:
                raise ForgeError("membership intent disappeared", "integrity")
            target = con.execute("SELECT state,clerk_user_id FROM accounts WHERE account_id=?", (account_id,)).fetchone()
            membership = con.execute("SELECT state FROM memberships WHERE repo_id=? AND account_id=?", (repo_id, account_id)).fetchone()
            if not target or target["state"] != "approved" or target["clerk_user_id"] != intent["clerk_user_id"] or (membership and membership["state"] != "active"):
                raise ForgeError("membership is unavailable", "policy")
            cursor = con.execute("INSERT INTO memberships(repo_id,account_id,principal_id,state) VALUES(?,?,?,'active') ON CONFLICT(repo_id,account_id) DO UPDATE SET principal_id=excluded.principal_id,state='active' WHERE memberships.principal_id=excluded.principal_id",
                                 (repo_id, account_id, principal_id))
            if cursor.rowcount != 1:
                raise ForgeError("host membership identity changed", "integrity")
            con.execute("UPDATE membership_intents SET state='active' WHERE intent_id=?", (intent["intent_id"],))

    @contextmanager
    def _catalog_lock(self):
        """Serialize catalog creation before SQLite's first journal-mode change."""
        self._check_layout()
        path = self.root / "locks" / "catalog.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            raise ForgeError("cannot lock host catalog", "storage") from error
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def _repo_lock(self, repo_id: str):
        self._check_layout()
        path = self.root / "locks" / f"{repo_id}.lock"
        if path.is_symlink():
            raise ForgeError("repository lock is a symlink", "integrity")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _write_owned_marker(self, marker: Path, value: dict) -> None:
        """Publish a complete marker atomically; incomplete temp files are inert."""
        temporary = None
        try:
            fd, name = tempfile.mkstemp(
                prefix=marker.stem + ".", suffix=".owned-tmp", dir=marker.parent
            )
            temporary = Path(name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(json.dumps(value, sort_keys=True).encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, marker)
            directory_fd = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            raise ForgeError("cannot persist repository ownership marker", "storage") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _ensure_owned_root(self, root: Path, marker: Path, repo_id: str, inputs_hash: str) -> None:
        """Called under the repo lock before any forge work or retry reads."""
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ForgeError("reserved repository path is unsafe", "integrity")
        if marker.exists() or marker.is_symlink():
            value = _read_json(marker)
            if (set(value) != {"repo_id", "inputs_hash", "root_identity"} or
                value["repo_id"] != repo_id or value["inputs_hash"] != inputs_hash):
                raise ForgeError("repository ownership marker is invalid", "integrity")
        else:
            if root.exists():
                raise ForgeError("refusing to adopt a pre-existing forge root", "integrity")
            value = {"repo_id": repo_id, "inputs_hash": inputs_hash, "root_identity": None}
            self._write_owned_marker(marker, value)
        if value["root_identity"] is None:
            # A crash between mkdir and the second marker commit leaves only an
            # empty directory. Never adopt populated data in that intermediate state.
            if root.exists() and any(root.iterdir()):
                raise ForgeError("unidentified repository root contains existing data", "integrity")
            root.mkdir(exist_ok=True)
            info = root.stat()
            value["root_identity"] = [info.st_dev, info.st_ino]
            self._write_owned_marker(marker, value)
        else:
            if not root.is_dir():
                raise ForgeError("owned repository root is missing", "integrity")
            info = root.stat()
            if value["root_identity"] != [info.st_dev, info.st_ino]:
                raise ForgeError("owned repository root identity changed", "integrity")

    def create_repo(self, owner_name: str, name: str, *, request_id: str,
                   source: Path | str | None = None, branch: str = "main",
                   seed_empty: bool = False, failpoint=None, _import_id: str | None = None,
                   _publish_guard=None) -> dict:
        owner_name, name = _slug(owner_name, "owner name"), _slug(name, "repository name")
        self._check_local_config()
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id):
            raise ForgeError("invalid request id")
        if seed_empty == (source is not None):
            raise ForgeError("choose exactly one of a local import source or seeded-empty mode")
        from git import clean_env
        import limits
        try:
            valid_branch = (isinstance(branch, str) and bool(branch) and subprocess.run(
                ["git", "check-ref-format", "--branch", branch], env=clean_env(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=limits.GIT_COMMAND_TIMEOUT, check=False).returncode == 0)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ForgeError("cannot validate branch name") from error
        if not valid_branch:
            raise ForgeError("invalid branch name")
        raw_source = Path(source).expanduser() if source is not None else None
        if raw_source is not None and raw_source.is_symlink():
            raise ForgeError("source cannot be a symlink")
        try:
            source_path = raw_source.resolve(strict=True) if raw_source is not None else None
        except OSError as error:
            raise ForgeError("local Git source does not exist or cannot be accessed") from error
        if source_path is not None:
            # Import identity pins the selected branch's commit, plus canonical source path.
            tip = _git(source_path, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
            if _git(source_path, "rev-parse", "--show-object-format") != "sha1":
                raise ForgeError("only SHA-1 Git sources are supported")
            source_identity = self.source_identity(source_path, branch, tip)
            kind = "import"
        else:
            source_identity, kind = f"seeded-empty:{branch}", "seeded-empty"
        try:
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                account = con.execute("SELECT * FROM accounts WHERE owner_name=? AND state='approved'", (owner_name,)).fetchone()
                if not account:
                    raise ForgeError("unknown or unapproved host account", "policy")
                import_row = None
                if _import_id is not None:
                    import_row = con.execute("SELECT * FROM imports WHERE import_id=? AND account_id=?", (_import_id, account["account_id"])).fetchone()
                    expected_source = str(self.root / "imports" / _import_id / "source.git")
                    if source_path is None:
                        raise ForgeError("import reservation requires a source", "conflict")
                    source_stat = source_path.stat()
                    current_tip = source_identity.rsplit("\0", 1)[-1]
                    expected_import_identity = self.source_receipt(import_row, current_tip, source_stat) if import_row else None
                    if (not import_row or import_row["request_id"] != request_id or import_row["name"] != name or import_row["branch"] != branch or
                        import_row["source_identity"] != expected_import_identity or
                        (source_stat.st_dev, source_stat.st_ino) != (import_row["source_dev"], import_row["source_ino"]) or
                        str(source_path) != expected_source or import_row["state"] not in {"provisioning", "failed"}):
                        raise ForgeError("import reservation is invalid", "conflict")
                else:
                    if con.execute("SELECT 1 FROM imports WHERE request_id=? OR (account_id=? AND name=? AND repo_id IS NULL)",
                                   (request_id, account["account_id"], name)).fetchone():
                        raise ForgeError("repository request or name is already reserved", "conflict")
                immutable = json.dumps([account["account_id"], owner_name, name, branch, kind, source_identity], separators=(",", ":"))
                inputs_hash = hashlib.sha256(immutable.encode()).hexdigest()
                existing = con.execute("SELECT * FROM repos WHERE request_id=?", (request_id,)).fetchone()
                if existing:
                    if existing["inputs_hash"] != inputs_hash:
                        raise ForgeError("request id was already used with different inputs", "conflict")
                    repo_id = existing["repo_id"]
                    if import_row and import_row["repo_id"] not in (None, repo_id):
                        raise ForgeError("import repository reservation changed", "integrity")
                else:
                    if account["repo_limit"] is not None:
                        count = con.execute("SELECT (SELECT count(*) FROM repos WHERE owner_account_id=?)+(SELECT count(*) FROM imports WHERE account_id=? AND repo_id IS NULL AND import_id!=COALESCE(?,''))", (account["account_id"], account["account_id"], _import_id)).fetchone()[0]
                        if count >= account["repo_limit"]:
                            raise ForgeError("host account repository capacity is exhausted", "limits")
                    raw_total_limit = con.execute("SELECT value FROM host_settings WHERE key='total_repo_limit'").fetchone()[0]
                    if not isinstance(raw_total_limit, str) or not re.fullmatch(r"[1-9][0-9]{0,6}", raw_total_limit):
                        raise ForgeError("host repository budget setting is corrupt", "integrity")
                    total_limit = int(raw_total_limit)
                    total = con.execute("SELECT (SELECT count(*) FROM repos)+(SELECT count(*) FROM imports WHERE repo_id IS NULL AND import_id!=COALESCE(?,''))", (_import_id,)).fetchone()[0]
                    if total >= total_limit:
                        raise ForgeError("host repository capacity is exhausted", "limits")
                    repo_id = uuid.uuid4().hex
                    now = int(time.time())
                    try:
                        con.execute("INSERT INTO repos VALUES(?,?,?,?,?,?,?,?,?,'reserved',NULL,NULL)",
                                    (repo_id, account["account_id"], name, branch, kind, source_identity, request_id, inputs_hash, now))
                        if import_row:
                            con.execute("UPDATE imports SET repo_id=? WHERE import_id=? AND repo_id IS NULL", (repo_id, _import_id))
                            if con.execute("SELECT changes()").fetchone()[0] != 1:
                                raise ForgeError("import reservation conversion failed", "conflict")
                    except sqlite3.IntegrityError as error:
                        raise ForgeError("repository name or request id is already reserved", "conflict") from error
                con.commit()
        except sqlite3.IntegrityError as error:
            raise ForgeError("repository name or request id is already reserved", "conflict") from error
        if not OPAQUE.fullmatch(repo_id) or not OPAQUE.fullmatch(account["account_id"]):
            raise ForgeError("host catalog contains an invalid opaque id", "integrity")
        self.prepare_repository(repo_id)
        root = self.root / "repos" / repo_id
        # Never follow or remove an unexpected path at a reserved id.
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ForgeError("reserved repository path is unsafe", "integrity")
        marker = self.root / "locks" / f"{repo_id}.owned"
        with self._repo_lock(repo_id):
            self._check_layout()
            self._ensure_owned_root(root, marker, repo_id, inputs_hash)
            with self._connect() as con:
                row = con.execute("SELECT * FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
                if not row or not OPAQUE.fullmatch(row["repo_id"]) or not OPAQUE.fullmatch(row["owner_account_id"]):
                    raise ForgeError("host catalog contains an invalid repository or account id", "integrity")
                account = con.execute("SELECT * FROM accounts WHERE account_id=? AND state='approved'", (row["owner_account_id"],)).fetchone()
                if not account:
                    raise ForgeError("host account is no longer approved", "policy")
                was_ready = row["state"] == "ready"
                if not was_ready:
                    con.execute("UPDATE repos SET state='provisioning',error=NULL WHERE repo_id=?", (repo_id,))
            try:
                if failpoint: failpoint("reserved")
                if kind == "import":
                    current = _git(source_path, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
                    if self.source_identity(source_path, branch, current) != source_identity:
                        raise ForgeError("import source branch changed since reservation", "conflict")
                    forge_source = source_path
                else:
                    forge_source = self.root / "locks" / f"seed-{repo_id}.git"
                    self._ensure_seed(forge_source, branch)
                if failpoint: failpoint("before_forge_init")
                self._initialize_forge(root, forge_source, branch)
                if failpoint: failpoint("forge_initialized")
                from repository import Repository
                repository = self._provision_owner(root, account, branch, failpoint, provision=not was_ready)
                manifest, _, replay = repository.load(use_cache=False)
                # Normal pushes advance the tip. The initial import record, not
                # the current tip, is the immutable source-identity evidence.
                initial_tip = replay.records[0]["payload"].get("canonical_tip")
                if kind == "import" and initial_tip != source_identity.rsplit("\0", 1)[1]:
                    raise ForgeError("imported source tip differs from pinned reservation", "conflict")
                if was_ready and row["repository_id"] != repository.repository_id:
                    raise ForgeError("ready repository identity differs from host registry", "integrity")
                if _publish_guard is not None:
                    _publish_guard()
                with self._connect() as con:
                    con.execute("BEGIN IMMEDIATE")
                    current = con.execute("SELECT * FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
                    approved = con.execute("SELECT * FROM accounts WHERE account_id=? AND state='approved'", (current["owner_account_id"],)).fetchone()
                    if (current["inputs_hash"] != inputs_hash or not approved or
                        approved["clerk_user_id"] != account["clerk_user_id"]):
                        raise ForgeError("reservation or approval changed before publication", "conflict")
                    if not was_ready:
                        con.execute("INSERT OR REPLACE INTO memberships VALUES(?,?,?,'active')", (repo_id, current["owner_account_id"], "owner"))
                    con.execute("UPDATE repos SET state='ready',repository_id=?,error=NULL WHERE repo_id=?", (repository.repository_id, repo_id))
                    con.commit()
                if failpoint: failpoint("ready")
                with self._connect() as con: return dict(con.execute("SELECT * FROM repos WHERE repo_id=?", (repo_id,)).fetchone())
            except Exception as error:
                with self._connect() as con:
                    con.execute("UPDATE repos SET state='failed',error=? WHERE repo_id=? AND state!='ready'", (str(error)[:1000], repo_id))
                raise


def approve_account(host_root, owner_name, emails, **kwargs):
    return Host(host_root).approve_account(owner_name, emails, **kwargs)

def create_repo(host_root, owner_name, name, **kwargs):
    return Host(host_root).create_repo(owner_name, name, **kwargs)

def list_repos(host_root, owner_name=None):
    return Host(host_root, initialize=False).list_repos(owner_name)

def link_clerk_account(host_root, clerk_user_id, verified_emails):
    return Host(host_root, initialize=False).link_clerk_account(clerk_user_id, verified_emails)
