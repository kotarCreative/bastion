"""Ephemeral-container runtime with an Artifacts-backed control-plane snapshot.

Cloudflare Containers can be replaced at any time, so this module never treats
the container filesystem as durable.  The SQLite catalog and its repository
configuration are checkpointed into a single private Artifacts object before a
mutating HTTP request is allowed to complete.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable

from object_store import Create, ObjectNotFound, Update


CONTROL_KEY = "control/cloudflare-root-v1.json"
SNAPSHOT_FORMAT = "bastion-cloudflare-root-v1"


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _read_snapshot(store) -> tuple[dict | None, object | None]:
    try:
        result = store.get(CONTROL_KEY)
    except ObjectNotFound:
        return None, None
    try:
        envelope = json.loads(result.body)
        if not isinstance(envelope, dict) or set(envelope) != {
            "format", "payload", "sha256"
        }:
            raise ValueError("unexpected snapshot envelope")
        payload = envelope["payload"]
        if envelope["format"] != SNAPSHOT_FORMAT or not isinstance(payload, dict):
            raise ValueError("unexpected snapshot format")
        expected = hashlib.sha256(_canonical(payload)).hexdigest()
        if not isinstance(envelope["sha256"], str) or envelope["sha256"] != expected:
            raise ValueError("snapshot checksum mismatch")
        if set(payload) != {"database", "backend", "repository", "phase"}:
            raise ValueError("unexpected snapshot payload")
        if payload["phase"] not in {"bootstrapping", "ready"}:
            raise ValueError("invalid snapshot phase")
        base64.b64decode(payload["database"], validate=True)
        for key in ("backend", "repository"):
            if not isinstance(payload[key], str):
                raise ValueError("invalid snapshot configuration")
        backend = json.loads(payload["backend"])
        repository = json.loads(payload["repository"])
        if not isinstance(backend, dict) or backend != {"backend": "sqlite"}:
            raise ValueError("hosted runtime requires a SQLite control-plane backend")
        if not isinstance(repository, dict):
            raise ValueError("invalid repository configuration")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise RuntimeError("private Artifacts control snapshot is invalid") from error
    return payload, result.meta.version


def restore_root(root: Path, payload: dict | None) -> None:
    """Replace an ephemeral root with the last complete private snapshot."""
    if payload is None:
        return
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    (root / "store.sqlite3").write_bytes(base64.b64decode(payload["database"]))
    (root / "backend.json").write_text(payload["backend"])
    (root / "repository.json").write_text(payload["repository"])
    with contextlib.closing(sqlite3.connect(root / "store.sqlite3", timeout=30)) as connection:
        try:
            connection.execute("DELETE FROM storage_locks")
            connection.commit()
        except sqlite3.OperationalError:
            # An unfinished first initialization may not yet have created it.
            pass


def checkpoint_root(root: Path, store, version=None, *, phase: str = "ready"):
    """Atomically publish the SQLite catalog and repository/backend config.

    SQLite's online backup API captures committed pages consistently without
    depending on the presence of a sidecar WAL file. A single conditional
    object-store write makes the database and both configuration records
    visible as one version.
    """
    if phase not in {"bootstrapping", "ready"}:
        raise ValueError("invalid control snapshot phase")
    database_path = root / "store.sqlite3"
    try:
        backend = json.loads((root / "backend.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("hosted runtime requires a valid SQLite backend record") from error
    if backend != {"backend": "sqlite"}:
        raise RuntimeError("Cloudflare Containers control-plane snapshots require SQLite")
    if not database_path.is_file():
        raise RuntimeError("SQLite control-plane database is missing")
    with tempfile.TemporaryDirectory(prefix="bastion-cf-snapshot-") as temporary:
        snapshot_path = Path(temporary) / "store.sqlite3"
        with contextlib.closing(sqlite3.connect(database_path, timeout=30)) as source:
            with contextlib.closing(sqlite3.connect(snapshot_path, timeout=30)) as target:
                source.backup(target)
                try:
                    target.execute("DELETE FROM storage_locks")
                except sqlite3.OperationalError:
                    pass
                target.commit()
        database_bytes = snapshot_path.read_bytes()
    payload = {
        "database": base64.b64encode(database_bytes).decode("ascii"),
        "backend": (root / "backend.json").read_text(),
        "repository": (root / "repository.json").read_text(),
        "phase": phase,
    }
    envelope = {
        "format": SNAPSHOT_FORMAT,
        "payload": payload,
        "sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
    }
    mode = Create() if version is None else Update(version)
    return store.put(CONTROL_KEY, _canonical(envelope), mode=mode).version


def _initialize_source(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "README.md").write_text(
        "# Bastion\n\nPrivate repositories are served through Bastion smart HTTP.\n"
    )
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="Bastion bootstrap",
        GIT_AUTHOR_EMAIL="bootstrap@bastion.invalid",
        GIT_COMMITTER_NAME="Bastion bootstrap",
        GIT_COMMITTER_EMAIL="bootstrap@bastion.invalid",
    )
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "Initialize Bastion"],
        check=True,
        env=env,
    )


def bootstrap_root(root: Path) -> None:
    """Create the first small repository and Clerk invitation on an empty store."""
    admin_email = os.environ.get("BASTION_BOOTSTRAP_ADMIN_EMAIL", "").strip()
    if not admin_email:
        raise RuntimeError("BASTION_BOOTSTRAP_ADMIN_EMAIL is required for first boot")
    from identity import CAPABILITIES
    from service import add_principal, add_role, add_view, bind_role, init_forge
    from identity import binding_identifier
    from repository import Repository
    from state import load_state, load_state_meta
    from errors import ForgeError
    from object_store_artifacts import ArtifactsObjectStoreConfig

    try:
        ready = load_state_meta(root).get("repository_state") == "ready"
    except ForgeError:
        ready = False
    if not ready:
        with tempfile.TemporaryDirectory(prefix="bastion-cf-source-") as source_dir:
            source = Path(source_dir) / "source"
            _initialize_source(source)
            remote = os.environ.get("BASTION_ARTIFACTS_REMOTE", "")
            if not remote:
                raise RuntimeError("BASTION_ARTIFACTS_REMOTE is required for first boot")
            init_forge(
                root,
                source,
                "main",
                artifacts_config=ArtifactsObjectStoreConfig(
                    remote=remote,
                    branch="bastion-storage",
                    token_env="BASTION_ARTIFACTS_TOKEN",
                ),
            )

    state = load_state(root)
    if "admin" not in state["principals"]:
        add_principal(root, "admin", [admin_email])
    state = load_state(root)
    if "admin-role" not in state["roles"]:
        grants = {capability: [""] for capability in CAPABILITIES}
        add_role(root, "admin-role", grants, {capability: [] for capability in CAPABILITIES})
    state = load_state(root)
    binding = binding_identifier("principal", "admin", "admin-role")
    if binding not in state["role_bindings"]:
        bind_role(root, "admin-role", "principal", "admin")
    _manifest, _version, replay = Repository.open(root).load()
    if "admin" not in replay.active_views:
        add_view(root, "admin", "admin", "main")


class DurableRuntime:
    """Serialize requests and fail closed when a durable publish fails."""

    def __init__(
        self,
        app,
        root: Path,
        store,
        *,
        initialize: Callable[[], None] | None = None,
        recovered: bool = False,
        version=None,
    ):
        self.app = app
        self.root = root
        self.store = store
        self.lock = threading.RLock()
        self.poisoned = False
        if recovered:
            self.version = version
        else:
            payload, self.version = _read_snapshot(store)
            if payload is None:
                if initialize is not None:
                    initialize()
                elif root.exists() and any(root.iterdir()):
                    raise RuntimeError("uncheckpointed local root exists without an Artifacts snapshot")
            else:
                restore_root(root, payload)
            if payload is None:
                self.version = checkpoint_root(root, store, self.version)

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        mutating = method in {"POST", "PUT", "PATCH", "DELETE"}
        with self.lock:
            if self.poisoned:
                return self._unavailable(start_response)
            captured: list[tuple[str, list[tuple[str, str]]]] = []

            def capture(status, headers, exc_info=None):
                captured[:] = [(status, headers)]
                return lambda _data: None

            try:
                from state import observe_state_commits

                with observe_state_commits(
                    self.root,
                    lambda: self._checkpoint_after_state_commit(),
                ):
                    iterable = self.app(environ, capture)
            except BaseException:
                if self.poisoned:
                    return self._unavailable(start_response)
                if mutating:
                    try:
                        self.version = checkpoint_root(self.root, self.store, self.version)
                    except Exception:
                        self.poisoned = True
                        return self._unavailable(start_response)
                raise
            if mutating:
                try:
                    self.version = checkpoint_root(self.root, self.store, self.version)
                except Exception:
                    self.poisoned = True
                    close = getattr(iterable, "close", None)
                    if close:
                        close()
                    return self._unavailable(start_response)
            if not captured:
                raise RuntimeError("Flask did not start a WSGI response")
            status, headers = captured[0]
            start_response(status, headers)
            return iterable

    @staticmethod
    def _unavailable(start_response):
        body = b"durable storage unavailable"
        start_response("503 Service Unavailable", [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("Content-Length", str(len(body))),
        ])
        return [body]

    def _checkpoint_after_state_commit(self) -> None:
        try:
            self.version = checkpoint_root(self.root, self.store, self.version)
        except Exception:
            self.poisoned = True
            raise


def build_store():
    from object_store_artifacts import ArtifactsObjectStore, ArtifactsObjectStoreConfig

    remote = os.environ.get("BASTION_ARTIFACTS_REMOTE", "")
    token = os.environ.get("BASTION_ARTIFACTS_TOKEN", "")
    if not remote or not token:
        raise RuntimeError("Artifacts remote and private write token are required")
    os.environ["BASTION_ARTIFACTS_TOKEN"] = token
    config = ArtifactsObjectStoreConfig(
        remote=remote, branch="bastion-storage", token_env="BASTION_ARTIFACTS_TOKEN"
    )
    return ArtifactsObjectStore.from_config(
        config,
        cache_dir=Path(os.environ.get("BASTION_ARTIFACTS_CACHE", "/tmp/bastion-artifacts-cache")),
    )


def build_app():
    from demo_server import create_app

    root = Path(os.environ.get("BASTION_ROOT", "/var/lib/bastion/forge"))
    store = build_store()
    if os.environ.get("BASTION_DATABASE") == "durable_objects":
        from cloudflare_durable_runtime import build_app as build_durable_app
        return build_durable_app(root, store)
    try:
        payload, version = _read_snapshot(store)
    except Exception:
        # Do not initialize a fresh catalog when remote reads fail.
        raise
    if payload is None:
        root.mkdir(parents=True, exist_ok=True)
        if any(root.iterdir()):
            raise RuntimeError("no private Artifacts snapshot exists but local root is not empty")
        if store.list_prefix(""):
            raise RuntimeError(
                "Artifacts repository already contains objects but has no private control snapshot"
            )
        version = _bootstrap_with_checkpoints(root, store, version)
    else:
        restore_root(root, payload)
        if payload["phase"] == "bootstrapping":
            # A private snapshot is written as soon as init_forge commits its
            # initializing catalog, making an interrupted first boot resumable.
            version = _bootstrap_with_checkpoints(root, store, version)
    # Loading validates the committed WAL head and makes any snapshot-ahead
    # or WAL-ahead state visible before Flask performs its startup checks.
    from state import locked

    # A snapshot can predate the last committed WAL publication if a process
    # died between those two writes. Recover the SQL indexes from repository
    # truth before any route reads its active policy or descriptors.
    with locked(root):
        pass
    version = checkpoint_root(root, store, version)
    app = create_app(root)
    return DurableRuntime(app, root, store, recovered=True, version=version)


def _bootstrap_with_checkpoints(root: Path, store, version):
    from state import observe_state_commits

    published = [version]

    def checkpoint_committed_state() -> None:
        published[0] = checkpoint_root(
            root, store, published[0], phase="bootstrapping"
        )

    with observe_state_commits(root, checkpoint_committed_state):
        bootstrap_root(root)
    return published[0]
