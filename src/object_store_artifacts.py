"""Conditional object storage backed by a Cloudflare Artifacts Git branch.

The local bare repository is only an object cache. Every operation refreshes
the configured remote branch before observing or changing an object.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
from functools import wraps
import os
import re
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from object_store import (
    AmbiguousWrite,
    Create,
    GetResult,
    NotModified,
    ObjectMeta,
    ObjectNotFound,
    PreconditionFailed,
    StorageFailure,
    Update,
    Version,
    validate_key,
)


MAX_OBJECT_SIZE = 256 * 1024 * 1024
MAX_BLOB_SIZE = 32_000_000
CHUNK_SIZE = 8 * 1024 * 1024
_ACCOUNT_HOST = re.compile(r"^[a-fA-F0-9]{32}\.artifacts\.cloudflare\.net$")
_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,62}$")
_BRANCH_DEFAULT = "bastion-storage"
_VERSION_ROOT = "objects/"


@dataclass(frozen=True)
class ArtifactsObjectStoreConfig:
    remote: str
    branch: str = _BRANCH_DEFAULT
    token_env: str = "BASTION_ARTIFACTS_TOKEN"

    def __post_init__(self) -> None:
        validate_remote(self.remote)
        validate_branch(self.branch)
        if not isinstance(self.token_env, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", self.token_env
        ):
            raise ValueError("Artifacts token environment variable name is invalid")


def validate_remote(remote: str) -> None:
    if not isinstance(remote, str):
        raise ValueError("Artifacts remote must be an HTTPS Git remote")
    parsed = urlsplit(remote)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or not _ACCOUNT_HOST.fullmatch(parsed.hostname or "")
        or parsed.query
        or parsed.fragment
        or "%" in parsed.path
    ):
        raise ValueError("Artifacts remote must be an account HTTPS Git remote")
    parts = parsed.path.split("/")
    if (
        len(parts) != 4
        or parts[0] != ""
        or parts[1] != "git"
        or not _ARTIFACT_NAME.fullmatch(parts[2])
        or not parts[3].endswith(".git")
        or not _ARTIFACT_NAME.fullmatch(parts[3][:-4])
    ):
        raise ValueError("Artifacts remote path must be /git/<namespace>/<repo>.git")


def validate_branch(branch: str) -> None:
    if not isinstance(branch, str) or not branch or not branch.isascii():
        raise ValueError("Artifacts branch must be nonempty ASCII")
    if "@{" in branch:
        raise ValueError("Artifacts branch may not use Git checkout shorthand")
    result = subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode:
        raise ValueError("Artifacts branch is not a valid Git branch name")


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._locked():
            return method(self, *args, **kwargs)
    return call


def parse_artifacts_uri(value: str) -> ArtifactsObjectStoreConfig:
    """Parse artifacts+https://<account>.artifacts.cloudflare.net/git/ns/repo.git."""
    if not isinstance(value, str) or not value.startswith("artifacts+https://"):
        raise ValueError("Artifacts object store must use artifacts+https://")
    # urlsplit treats artifacts+https as one scheme, so remove only that wrapper.
    remote = "https://" + value[len("artifacts+https://") :].split("?", 1)[0].split("#", 1)[0]
    validate_remote(remote)
    query = value[len("artifacts+https://") :].split("?", 1)
    raw_query = query[1].split("#", 1)[0] if len(query) == 2 else ""
    if "#" in value:
        raise ValueError("Artifacts object store URI must not contain a fragment")
    parameters = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=True)
    options: dict[str, str] = {}
    for key, item in parameters:
        if key not in {"branch", "token_env"} or key in options or not item:
            raise ValueError("Artifacts object store URI has unsupported query parameters")
        options[key] = item
    return ArtifactsObjectStoreConfig(
        remote=remote,
        branch=options.get("branch", _BRANCH_DEFAULT),
        token_env=options.get("token_env", "BASTION_ARTIFACTS_TOKEN"),
    )


class ArtifactsObjectStore:
    """Conditional object store whose remote Git branch is authoritative."""

    def __init__(
        self,
        config: ArtifactsObjectStoreConfig,
        cache_dir: Path,
        *,
        max_object_size: int = MAX_OBJECT_SIZE,
        _test_local_remote: bool = False,
    ) -> None:
        if not isinstance(config, ArtifactsObjectStoreConfig):
            raise TypeError("Artifacts object store requires an Artifacts configuration")
        if max_object_size <= 0 or max_object_size > MAX_OBJECT_SIZE:
            raise ValueError("invalid Artifacts object size limit")
        if not _test_local_remote:
            validate_remote(config.remote)
        self.config = config
        self.remote = config.remote
        self.branch = config.branch
        self.cache_dir = Path(cache_dir).resolve()
        self.cache_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.cache_dir, 0o700)
        self._git_dir = self.cache_dir / "artifacts-cache.git"
        self._lock_file = self.cache_dir / "artifacts-cache.lock"
        self._lock_file.touch(mode=0o600, exist_ok=True)
        os.chmod(self._lock_file, 0o600)
        self.max_object_size = max_object_size
        self.identity = f"artifacts:{config.remote}:{config.branch}"
        self.metrics = {
            "requests": 0,
            "bytes_read": 0,
            "bytes_written": 0,
            "precondition_failures": 0,
            "ambiguous_writes": 0,
            "retries": 0,
        }
        with self._locked():
            if not (self._git_dir / "HEAD").exists():
                self._git(["init", "--bare", str(self._git_dir)], cwd=None)

    @contextmanager
    def _locked(self):
        with self._lock_file.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @classmethod
    def from_config(cls, config: ArtifactsObjectStoreConfig, cache_dir: Path) -> "ArtifactsObjectStore":
        return cls(config, cache_dir)

    @classmethod
    def for_test_remote(
        cls, remote: Path | str, cache_dir: Path, *, branch: str = _BRANCH_DEFAULT,
        max_object_size: int = MAX_OBJECT_SIZE,
    ) -> "ArtifactsObjectStore":
        """Create a test-only adapter for a local bare Git remote."""
        config = object.__new__(ArtifactsObjectStoreConfig)
        object.__setattr__(config, "remote", str(remote))
        object.__setattr__(config, "branch", branch)
        object.__setattr__(config, "token_env", "BASTION_ARTIFACTS_TOKEN")
        validate_branch(branch)
        return cls(config, cache_dir, max_object_size=max_object_size, _test_local_remote=True)

    def _environment(self, *, require_token: bool = True) -> dict[str, str]:
        # Git's config environment is rebuilt so caller-provided GIT_* values
        # cannot redirect the repository, inject a header, or enable tracing.
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_TRACE": "0",
            "GIT_TRACE_PACKET": "0",
            "GIT_CURL_VERBOSE": "0",
            "GIT_CONFIG_COUNT": "0",
        })
        if not self.remote.startswith("https://") or not require_token:
            return env
        token = os.environ.get(self.config.token_env)
        if not token:
            raise StorageFailure(f"Artifacts token environment variable {self.config.token_env} is unset")
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
        })
        return env

    def _git(
        self, args: list[str], *, input_data: bytes | None = None, cwd: Path | None = None,
        check: bool = True, extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        env = self._environment(require_token=bool(args and args[0] in {"ls-remote", "fetch", "push"}))
        if cwd is None and self._git_dir.exists():
            env["GIT_DIR"] = str(self._git_dir)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        if extra_env:
            env.update(extra_env)
        self.metrics["requests"] += 1
        try:
            result = subprocess.run(
                ["git", *args], cwd=cwd, env=env, input=input_data,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=60,
            )
        except OSError as error:
            raise StorageFailure("Git is unavailable for Artifacts storage") from error
        except subprocess.TimeoutExpired as error:
            raise StorageFailure("Artifacts Git operation timed out") from error
        if check and result.returncode:
            # Git stderr can echo a remote URL or server response. Never let it
            # escape into diagnostics, since authentication must stay secret.
            raise StorageFailure(f"Artifacts Git operation failed ({result.returncode})")
        return result

    def _ref(self) -> str:
        return f"refs/heads/{self.branch}"

    def _current(self) -> str | None:
        result = self._git(["ls-remote", "--heads", self.remote, self._ref()])
        records = result.stdout.decode("ascii", "strict").splitlines()
        if not records:
            self._git(["update-ref", "-d", "refs/bastion/current"], check=False)
            return None
        fields = records[0].split("\t", 1)
        if len(fields) != 2 or fields[1] != self._ref() or not re.fullmatch(r"[0-9a-f]{40,64}", fields[0]):
            raise StorageFailure("Artifacts returned an invalid branch reference")
        old = fields[0]
        cached = self._git(
            ["rev-parse", "--verify", "refs/bastion/current"], check=False
        )
        if cached.returncode == 0 and cached.stdout.decode().strip() == old:
            return old
        fetched = self._git([
            "fetch", "--quiet", "--no-tags", self.remote,
            f"+{self._ref()}:refs/bastion/current",
        ], check=False)
        if fetched.returncode:
            # Branch may have disappeared between ls-remote and fetch. Recheck
            # the remote once; the next caller will see the new state.
            raise StorageFailure("Artifacts branch changed while fetching")
        observed = self._git(["rev-parse", "--verify", "refs/bastion/current"]).stdout.decode().strip()
        if observed != old:
            # The ref moved during the two requests. Use the commit that fetch
            # actually observed; its object graph is the read's linearization.
            old = observed
        return old

    @staticmethod
    def _paths(key: str) -> tuple[str, str]:
        encoded = hashlib.sha256(key.encode("ascii")).hexdigest()
        stem = f"{_VERSION_ROOT}{encoded}"
        return stem, f"{stem}.meta"

    @staticmethod
    def _chunk_path(stem: str, index: int) -> str:
        return f"{stem}.{index:04d}.chunk"

    def _metadata(self, key: str) -> dict | None:
        _stem, meta_path = self._paths(key)
        treeish = self._git(["rev-parse", "--verify", "refs/bastion/current^{tree}"], check=False)
        if treeish.returncode:
            return None
        entry = self._git(["ls-tree", "-z", treeish.stdout.decode().strip(), "--", meta_path]).stdout
        if not entry:
            return None
        oid = entry.split(b"\t", 1)[0].split()[-1].decode("ascii")
        raw = self._git(["cat-file", "blob", oid]).stdout
        if len(raw) > MAX_BLOB_SIZE:
            raise StorageFailure("Artifacts object metadata exceeds the blob size limit")
        try:
            meta = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise StorageFailure("Artifacts object metadata is malformed") from None
        if json.dumps(meta, sort_keys=True, separators=(",", ":")).encode("ascii") != raw:
            raise StorageFailure("Artifacts object metadata is not canonical")
        if (
            not isinstance(meta, dict)
            or set(meta) != {"chunk_sha256", "chunks", "key", "sha256", "size", "version"}
            or not isinstance(meta["key"], str) or meta["key"] != key
            or not isinstance(meta["size"], int) or isinstance(meta["size"], bool)
            or not 0 <= meta["size"] <= self.max_object_size
            or not isinstance(meta["chunks"], int) or isinstance(meta["chunks"], bool)
            or meta["chunks"] != (meta["size"] + CHUNK_SIZE - 1) // CHUNK_SIZE
            or not isinstance(meta["chunk_sha256"], list)
            or len(meta["chunk_sha256"]) != meta["chunks"]
            or any(not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in meta["chunk_sha256"])
            or not isinstance(meta["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", meta["sha256"])
            or not isinstance(meta["version"], str) or not re.fullmatch(r"[0-9a-f]{32}", meta["version"])
        ):
            raise StorageFailure("Artifacts object metadata is invalid")
        return meta

    def _read(
        self, key: str, byte_range: tuple[int, int] | None = None,
    ) -> tuple[bytes, ObjectMeta] | None:
        stem, _meta_path = self._paths(key)
        treeish = self._git(["rev-parse", "--verify", "refs/bastion/current^{tree}"], check=False)
        if treeish.returncode:
            return None
        metadata = self._metadata(key)
        if metadata is None:
            return None
        size = metadata["size"]
        if byte_range is None or size == 0:
            first, last = 0, metadata["chunks"] - 1
        else:
            first = byte_range[0] // CHUNK_SIZE
            last = byte_range[1] // CHUNK_SIZE
        chunks: list[bytes] = []
        for index in range(first, last + 1):
            path = self._chunk_path(stem, index)
            entry = self._git(["ls-tree", "-z", treeish.stdout.decode().strip(), "--", path]).stdout
            if not entry:
                raise StorageFailure("Artifacts object chunk is missing")
            oid = entry.split(b"\t", 1)[0].split()[-1].decode("ascii")
            chunk_size = int(self._git(["cat-file", "-s", oid]).stdout)
            expected_size = min(CHUNK_SIZE, size - index * CHUNK_SIZE)
            if chunk_size != expected_size or chunk_size > MAX_BLOB_SIZE:
                raise StorageFailure("Artifacts object chunk has an invalid size")
            chunk = self._git(["cat-file", "blob", oid]).stdout
            if hashlib.sha256(chunk).hexdigest() != metadata["chunk_sha256"][index]:
                raise StorageFailure("Artifacts object chunk checksum is invalid")
            chunks.append(chunk)
        data = b"".join(chunks)
        if byte_range is None:
            if hashlib.sha256(data).hexdigest() != metadata["sha256"]:
                raise StorageFailure("Artifacts object body checksum is invalid")
        else:
            data_start = first * CHUNK_SIZE
            data = data[byte_range[0] - data_start : byte_range[1] - data_start + 1]
        return data, ObjectMeta(key, size, Version(metadata["version"]))

    def _refresh(self) -> str | None:
        return self._current()

    @_serialized
    def get(
        self, key: str, *, if_none_match: Version | None = None,
        byte_range: tuple[int, int] | None = None,
    ) -> GetResult:
        validate_key(key)
        if byte_range is not None and (
            not isinstance(byte_range, tuple) or len(byte_range) != 2
            or any(not isinstance(n, int) or isinstance(n, bool) for n in byte_range)
            or byte_range[0] < 0 or byte_range[1] < byte_range[0]
        ):
            raise ValueError("invalid inclusive object byte range")
        self._refresh()
        metadata = self._metadata(key)
        if metadata is None:
            raise ObjectNotFound(key)
        if byte_range is not None and (
            metadata["size"] == 0 or byte_range[1] >= metadata["size"]
        ):
            raise ValueError("invalid inclusive object byte range")
        value = self._read(key, byte_range=byte_range)
        if value is None:
            raise ObjectNotFound(key)
        body, meta = value
        if if_none_match is not None and meta.version == if_none_match:
            raise NotModified(key)
        self.metrics["bytes_read"] += len(body)
        return GetResult(body, meta)

    @_serialized
    def head(self, key: str) -> ObjectMeta | None:
        validate_key(key)
        self._refresh()
        metadata = self._metadata(key)
        if metadata is None:
            return None
        return ObjectMeta(key, metadata["size"], Version(metadata["version"]))

    def _tree(
        self, key: str, body: bytes | None, metadata: dict | None,
        old: str | None, previous_chunks: int = 0,
    ) -> str:
        with tempfile.NamedTemporaryFile(prefix="bastion-git-index-", delete=False) as index_file:
            index_path = Path(index_file.name)
        try:
            index_env = {
                "GIT_INDEX_FILE": str(index_path),
                "GIT_WORK_TREE": str(self.cache_dir),
            }
            if old is None:
                self._git(["read-tree", "--empty"], extra_env=index_env)
            else:
                self._git(["read-tree", f"{old}^{{tree}}"], extra_env=index_env)
            stem, meta_path = self._paths(key)
            for index in range(previous_chunks):
                self._git([
                    "update-index", "--force-remove", "--", self._chunk_path(stem, index)
                ], extra_env=index_env)
            if body is None:
                self._git(["update-index", "--force-remove", "--", meta_path], extra_env=index_env)
            else:
                assert metadata is not None
                for index in range(metadata["chunks"]):
                    piece = body[index * CHUNK_SIZE : (index + 1) * CHUNK_SIZE]
                    oid = self._git(["hash-object", "-w", "--stdin"], input_data=piece).stdout.decode().strip()
                    self._git([
                        "update-index", "--add", "--cacheinfo", "100644", oid,
                        self._chunk_path(stem, index),
                    ], extra_env=index_env)
                encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("ascii")
                meta_oid = self._git(["hash-object", "-w", "--stdin"], input_data=encoded).stdout.decode().strip()
                self._git(["update-index", "--add", "--cacheinfo", "100644", meta_oid, meta_path], extra_env=index_env)
            tree = self._git(["write-tree"], extra_env=index_env).stdout.decode().strip()
            return tree
        finally:
            index_path.unlink(missing_ok=True)

    def _commit(self, tree: str, old: str | None) -> str:
        args = ["commit-tree", tree]
        if old is not None:
            args.extend(["-p", old])
        commit_env = {
            "GIT_AUTHOR_NAME": "Bastion Artifacts",
            "GIT_AUTHOR_EMAIL": "bastion-artifacts@localhost",
            "GIT_COMMITTER_NAME": "Bastion Artifacts",
            "GIT_COMMITTER_EMAIL": "bastion-artifacts@localhost",
        }
        # Keep the commit message fixed and free of caller-controlled values.
        result = self._git(args, input_data=b"Bastion object-store update\n", extra_env=commit_env)
        return result.stdout.decode("ascii").strip()

    def _push(self, commit: str, old: str | None) -> str:
        lease = f"--force-with-lease={self._ref()}:{old or ''}"
        # Resolve the configured token before starting a write. If an error
        # follows invocation, its outcome is uncertain until the remote proves
        # this commit became the branch tip.
        self._environment()
        try:
            result = self._git(["push", "--porcelain", lease, self.remote, f"{commit}:{self._ref()}"], check=False)
        except StorageFailure:
            try:
                observed = self._current()
            except StorageFailure:
                observed = None
            if observed == commit or (
                observed is not None
                and self._git(["merge-base", "--is-ancestor", commit, observed], check=False).returncode == 0
            ):
                return "ok"
            self.metrics["ambiguous_writes"] += 1
            raise AmbiguousWrite("Artifacts write outcome is unknown") from None
        if result.returncode == 0:
            return "ok"
        # An exact lease rejection proves this commit was not installed. It is
        # safe to refresh and retry the conditional operation against the head.
        diagnostics = (result.stdout + b"\n" + result.stderr).lower()
        # Git's porcelain status for an exact force-with-lease rejection is
        # explicit. Generic hook/rejection messages are not enough to prove
        # the server left the ref unchanged.
        if (
            b"[rejected] (stale info)" in diagnostics
            or b"[remote rejected] (reference already exists)" in diagnostics
            or (
                b"[remote rejected] (failed to update ref)" in diagnostics
                and (b"cannot lock ref" in diagnostics or b"but expected" in diagnostics)
            )
        ):
            return "conflict"
        try:
            observed = self._current()
        except StorageFailure:
            self.metrics["ambiguous_writes"] += 1
            raise AmbiguousWrite("Artifacts write outcome is unknown") from None
        if observed == commit or (
            observed is not None
            and self._git(["merge-base", "--is-ancestor", commit, observed], check=False).returncode == 0
        ):
            return "ok"
        self.metrics["ambiguous_writes"] += 1
        raise AmbiguousWrite("Artifacts write outcome is unknown")

    @_serialized
    def put(self, key: str, body: bytes, *, mode: Create | Update) -> ObjectMeta:
        validate_key(key)
        if not isinstance(body, bytes):
            raise TypeError("object bodies must be bytes")
        if len(body) > self.max_object_size:
            raise ValueError("object exceeds the configured size limit")
        if not isinstance(mode, (Create, Update)):
            raise TypeError("object-store puts require Create or Update")
        for attempt in range(8):
            old = self._refresh()
            current = self._metadata(key)
            if isinstance(mode, Create):
                if current is not None:
                    self.metrics["precondition_failures"] += 1
                    raise PreconditionFailed(key)
            elif current is None or Version(current["version"]) != mode.version:
                self.metrics["precondition_failures"] += 1
                raise PreconditionFailed(key)
            version = uuid.uuid4().hex
            metadata = {
                "chunk_sha256": [
                    hashlib.sha256(body[start : start + CHUNK_SIZE]).hexdigest()
                    for start in range(0, len(body), CHUNK_SIZE)
                ],
                "chunks": (len(body) + CHUNK_SIZE - 1) // CHUNK_SIZE,
                "key": key,
                "sha256": hashlib.sha256(body).hexdigest(),
                "size": len(body),
                "version": version,
            }
            if len(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("ascii")) > MAX_BLOB_SIZE:
                raise ValueError("object key metadata exceeds the Artifacts blob size limit")
            tree = self._tree(key, body, metadata, old, current["chunks"] if current else 0)
            commit = self._commit(tree, old)
            outcome = self._push(commit, old)
            if outcome == "ok":
                self.metrics["bytes_written"] += len(body)
                return ObjectMeta(key, len(body), Version(version))
            self.metrics["retries"] += 1
            # Conflict was conclusively rejected. Re-evaluate Create/Update
            # against the new branch head before preparing another commit.
        raise StorageFailure("Artifacts branch remained busy during conditional write")

    @_serialized
    def delete(self, key: str, *, if_version: Version | None = None) -> None:
        validate_key(key)
        for _attempt in range(8):
            old = self._refresh()
            current = self._metadata(key)
            if current is None:
                if if_version is not None:
                    self.metrics["precondition_failures"] += 1
                    raise PreconditionFailed(key)
                return
            if if_version is not None and Version(current["version"]) != if_version:
                self.metrics["precondition_failures"] += 1
                raise PreconditionFailed(key)
            tree = self._tree(key, None, None, old, current["chunks"])
            commit = self._commit(tree, old)
            outcome = self._push(commit, old)
            if outcome == "ok":
                return
            # Deleting the same key after a conflict must re-check the caller's
            # exact version; unrelated changes can safely retry.
        raise StorageFailure("Artifacts branch remained busy during conditional delete")

    @_serialized
    def list_prefix(self, prefix: str) -> list[ObjectMeta]:
        if not isinstance(prefix, str) or not prefix.isascii() or "\x00" in prefix:
            raise ValueError("object-store prefix must be ASCII")
        self._refresh()
        treeish = self._git(["rev-parse", "--verify", "refs/bastion/current^{tree}"], check=False)
        if treeish.returncode:
            return []
        listing = self._git(["ls-tree", "-r", "-z", treeish.stdout.decode().strip(), "--", _VERSION_ROOT]).stdout
        versions: dict[str, dict] = {}
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            meta, path = entry.split(b"\t", 1)
            name = path.decode("ascii")
            if not name.endswith(".meta"):
                continue
            oid = meta.split()[-1].decode("ascii")
            raw = self._git(["cat-file", "blob", oid]).stdout
            if len(raw) > MAX_BLOB_SIZE:
                raise StorageFailure("Artifacts object metadata exceeds the blob size limit")
            try:
                metadata = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise StorageFailure("Artifacts object metadata is malformed") from None
            if json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("ascii") != raw:
                raise StorageFailure("Artifacts object metadata is not canonical")
            key = metadata.get("key") if isinstance(metadata, dict) else None
            if not isinstance(key, str):
                raise StorageFailure("Artifacts object metadata is invalid")
            validate_key(key)
            if self._paths(key)[1] != name:
                raise StorageFailure("Artifacts object metadata key does not match its path")
            if key.startswith(prefix):
                versions[key] = metadata
        result: list[ObjectMeta] = []
        for key, metadata in versions.items():
            # Validate shape/ranges without trusting a metadata blob from the
            # remote merely because it is listed.
            size, chunks, version, checksum = (
                metadata.get("size") if isinstance(metadata, dict) else None,
                metadata.get("chunks") if isinstance(metadata, dict) else None,
                metadata.get("version") if isinstance(metadata, dict) else None,
                metadata.get("sha256") if isinstance(metadata, dict) else None,
            )
            if (
                not isinstance(metadata, dict)
                or set(metadata) != {"chunk_sha256", "chunks", "key", "sha256", "size", "version"}
                or not isinstance(size, int) or isinstance(size, bool)
                or not 0 <= size <= self.max_object_size
                or not isinstance(chunks, int) or isinstance(chunks, bool)
                or chunks != (size + CHUNK_SIZE - 1) // CHUNK_SIZE
                or not isinstance(metadata.get("chunk_sha256"), list)
                or len(metadata["chunk_sha256"]) != chunks
                or any(not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in metadata["chunk_sha256"])
                or not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{32}", version)
                or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum)
            ):
                raise StorageFailure("Artifacts object metadata is invalid")
            result.append(ObjectMeta(key, size, Version(version)))
        return sorted(result, key=lambda item: item.key)


__all__ = [
    "ArtifactsObjectStore",
    "ArtifactsObjectStoreConfig",
    "parse_artifacts_uri",
    "validate_remote",
]
