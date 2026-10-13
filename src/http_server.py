"""Smart HTTP Git endpoint with per-view token authorization."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import limits
from errors import ForgeError
from git import clean_env
from layout import intake_repo, view_repo
from state import load_state, load_state_meta
from materialization import ensure_intake_materialized, ensure_view_materialized
from repository import Repository
from tokens import has_scope


ROUTE = re.compile(
    r"/(?P<view>[A-Za-z0-9][A-Za-z0-9._-]{0,63})\.git/"
    r"(?P<target>info/refs|git-upload-pack|git-receive-pack)"
)
SERVICES = ("git-upload-pack", "git-receive-pack")
SERVICE_COMMANDS = {"git-upload-pack": "upload-pack", "git-receive-pack": "receive-pack"}
SERVICE_TIMEOUTS = {
    "git-upload-pack": limits.GIT_UPLOAD_TIMEOUT,
    "git-receive-pack": limits.GIT_RECEIVE_TIMEOUT,
}
UPLOAD_MAX_BODY = limits.UPLOAD_BODY_BYTES
RECEIVE_MAX_BODY = limits.RECEIVE_BODY_BYTES
GZIP_MAX_RATIO = limits.GZIP_MAX_RATIO
PROTOCOL_HEADER = re.compile(r"version=2(=[0-9]+)?")

# Strict framing grammar: only unambiguous decimal lengths and chunk sizes.
CONTENT_LENGTH_RE = re.compile(r"[0-9]{1,19}")
CHUNK_SIZE_RE = re.compile(r"[0-9A-Fa-f]{1,16}")
MAX_CHUNK_LINE_BYTES = 1024
TRAILER_LINE_BYTES = 1024
BODY_CHUNK_BYTES = 64 << 10

BODY_401 = b"authentication required"
BODY_404 = b"not found"
BODY_405 = b"method not allowed"
BODY_400 = b"bad request"
BODY_429 = b"throttled"
BODY_503 = b"server saturated"
BODY_504 = b"git service timed out"
CACHE_HEADERS = {
    "Expires": "Fri, 01 Jan 1980 00:00:00 GMT",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache, max-age=0, must-revalidate",
}


class HttpError(Exception):
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None):
        super().__init__(f"{status}: {body.decode(errors='replace')}")
        self.status = status
        self.body = body
        self.headers = headers or {}


def parse_route(path: str) -> tuple[str, str] | None:
    match = ROUTE.fullmatch(path)
    if not match:
        return None
    return match.group("view"), match.group("target")


def parse_service(query: str) -> str | None:
    values = parse_qs(query, keep_blank_values=True).get("service", [])
    if len(values) == 1 and values[0] in SERVICES:
        return values[0]
    return None


def parse_authorization(header: str | None) -> str | None:
    """Return the presented credential, or None when absent/malformed."""
    parsed = parse_git_authorization(header)
    if parsed is None:
        return None
    return parsed[1]


def parse_git_authorization(header: str | None) -> tuple[str | None, str] | None:
    """Return (username_or_None, credential) for Bearer/Basic auth.

    Bearer carries only the credential. Basic carries ``username:password``
    where the password is the complete credential; the username is the
    asserted view and must equal the routed view (checked by callers).
    """
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    value = value.strip()
    if not value:
        return None
    scheme = scheme.lower()
    if scheme == "bearer":
        return None, value
    if scheme == "basic":
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
        username, separator, password = decoded.partition(":")
        if not separator or not password:
            return None
        return username, password
    return None


class BodyLimitError(Exception):
    """Raised when streamed framing or decompression limits are exceeded."""


def inflate_bounded(data: bytes, decoded_limit: int) -> bytes:
    """Decode gzip incrementally under cumulative output and ratio caps.

    Handles concatenated members explicitly; truncated members and
    trailing non-gzip data are rejected.  The full expansion is never
    allocated before the caps are checked.
    """
    cap = min(decoded_limit, GZIP_MAX_RATIO * max(len(data), 1))
    if not data:
        raise BodyLimitError("empty gzip stream")
    output = bytearray()
    rest = memoryview(data)
    while rest:
        decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
        try:
            while rest:
                if len(output) >= cap:
                    raise BodyLimitError("decoded body exceeds its limit")
                size = min(len(rest), 1 << 16)
                produced = decompressor.decompress(bytes(rest[:size]), cap - len(output))
                output += produced
                consumed = size - len(decompressor.unconsumed_tail)
                rest = rest[consumed:]
                if decompressor.unconsumed_tail:
                    # The member needs more output room than remains.
                    if len(output) >= cap:
                        raise BodyLimitError("decoded body exceeds its limit")
                    continue
                if decompressor.eof:
                    # ``rest`` already starts at the first byte after the
                    # consumed input, including data after this member.
                    break
            if not decompressor.eof:
                raise BodyLimitError("truncated gzip stream")
        except zlib.error as error:
            raise BodyLimitError("malformed gzip stream") from error
        # ``rest`` points after the current input chunk; prepend bytes that
        # zlib left after the gzip member inside that chunk.
        rest = memoryview(decompressor.unused_data + rest.tobytes())
    return bytes(output)


def decode_body(
    body: bytes, encoding: str | None, maximum: int | None = None
) -> bytes:
    """Decode a complete buffered body under strict framing limits."""
    if not encoding or encoding.lower() == "identity":
        if maximum is not None and len(body) > maximum:
            raise HttpError(400, BODY_400)
        return body
    cap = maximum if maximum is not None else (1 << 30)
    try:
        return inflate_bounded(body, cap)
    except BodyLimitError as error:
        raise HttpError(400, BODY_400) from error


def pkt_line(payload: bytes) -> bytes:
    return f"{len(payload) + 4:04x}".encode() + payload


def legacy_tokens_path(root: Path) -> Path:
    return root / "tokens.json"


def has_legacy_tokens(root: Path) -> bool:
    try:
        return legacy_tokens_path(root).exists()
    except OSError:
        return False


def reject_legacy_tokens(root: Path) -> None:
    """Fail hosted startup when pre-013 raw-token state is present."""
    if has_legacy_tokens(root):
        raise ForgeError(
            "hosted root contains legacy tokens.json; "
            "remove it and issue digest-only tokens from a fresh root"
        )


def load_tokens(root: Path) -> dict[str, str]:
    """Legacy compatibility shim: no raw secrets are persisted anymore."""
    raise ForgeError("raw token map is no longer supported; use digest tokens")


def add_token(
    root: Path,
    view: str,
    scopes: list[str] | tuple[str, ...] | None = None,
    expires_in: str | None = None,
) -> str:
    """Issue one ``bst1`` credential for ``view`` and persist only its digest.

    ``scopes`` defaults to ``fetch`` + ``ui`` per spec 013; ``push`` is
    always explicit. Returns the complete credential exactly once.
    """
    import time as _time

    import tokens as _tokens
    from store import open_store

    state = load_state_meta(root)
    if state.get("repository_state") != "ready":
        raise ForgeError("forge initialization is incomplete")
    repository = Repository.open(root)
    _manifest, _version, replay = repository.load()
    if view not in replay.active_views:
        raise ForgeError(f"unknown view: {view}")
    normalized = _tokens.normalize_scopes(
        list(scopes) if scopes is not None else list(_tokens.DEFAULT_SCOPES)
    )
    expires_at = _tokens.parse_expires_in(expires_in)
    for _attempt in range(3):
        token_id = _tokens.generate_token_id()
        secret = _tokens.generate_secret()
        record = {
            "token_id": token_id,
            "secret_digest": _tokens.digest(token_id, secret),
            "view_id": view,
            "scopes": normalized,
            "created_at": int(_time.time()),
            "expires_at": expires_at,
            "revoked_at": None,
        }
        store = open_store(root)
        try:
            store.create_http_token(record)
        except ForgeError as error:
            if "collision" in str(error).lower():
                continue
            raise
        finally:
            store.close()
        return f"bst1.{token_id}.{secret}"
    raise ForgeError("token identifier collision")


def list_tokens(root: Path, view: str | None = None) -> list[dict]:
    """Return token metadata without digests or secrets."""
    from store import open_store

    store = open_store(root)
    try:
        records = store.list_http_tokens(view)
    finally:
        store.close()
    return [
        {
            "token_id": record["token_id"],
            "view_id": record["view_id"],
            "scopes": list(record["scopes"]),
            "created_at": record["created_at"],
            "expires_at": record["expires_at"],
            "revoked_at": record["revoked_at"],
        }
        for record in records
    ]


def revoke_token(root: Path, token_id: str) -> bool:
    from store import open_store

    store = open_store(root)
    try:
        return store.revoke_http_token(token_id)
    finally:
        store.close()


def authenticate_token(root: Path, credential: str | None) -> dict | None:
    """Resolve one credential to its live token record, or None.

    Unknown IDs, malformed credentials, expired timestamps, and revoked
    tokens all resolve to None so callers emit one generic 401. Digest
    comparison is constant-time with a dummy comparison for unknown IDs.
    """
    import time as _time

    import tokens as _tokens
    from store import open_store

    if not credential:
        return None
    try:
        token_id, secret = _tokens.split_credential(credential)
    except ForgeError:
        return None
    store = open_store(root)
    try:
        record = store.load_http_token(token_id)
    except ForgeError:
        # A corrupt row or a backend fault must be indistinguishable from an
        # unknown token id. Returning the storage/integrity error instead
        # would surface as a 500 and turn "this token id exists" into a
        # single-request enumeration oracle against the generic 401.
        return None
    finally:
        store.close()
    presented = _tokens.digest(token_id, secret)
    if record is None:
        _tokens.compare_digest(presented, _tokens.DUMMY_DIGEST)
        return None
    if not _tokens.compare_digest(presented, record["secret_digest"]):
        return None
    if _tokens.is_expired(record, _time.time()) or _tokens.is_revoked(record):
        return None
    return record


def service_repo(root: Path, view: str, service: str) -> tuple[Path, Path | None]:
    """Resolve a routed view to its backing repository.

    Uploads read from published view storage; receives target the isolated
    intake repository whose only object source is the sibling view store.
    """
    _repository, manifest, active, materialized = _resolve_service(root, view)
    if service == "git-upload-pack":
        return materialized.path, None
    return ensure_intake_materialized(root, materialized, manifest), materialized.path / "objects"


def _resolve_service(root: Path, view: str):
    # Fetch resolution reads repository metadata and resolves the active view
    # from the WAL snapshot; it does not load transactions, changes, jobs,
    # teams, roles, or unrelated control-plane descriptor rows. A single
    # process-local request phase reuses one store handle per phase.
    state = load_state_meta(root)
    if state.get("repository_state") != "ready":
        raise ForgeError("forge initialization is incomplete")
    repository = Repository.open(root)
    manifest, _version, replay = repository.load()
    active = replay.active_views.get(view)
    if active is None:
        raise ForgeError("view is not active in the committed WAL")
    materialized = ensure_view_materialized(root, view, manifest, replay, repository)
    return repository, manifest, active, materialized


def run_service(
    repo: Path,
    service: str,
    advertise: bool,
    payload: bytes,
    protocol: str | None,
    extra_env: dict[str, str] | None = None,
    alternates: Path | None = None,
    registry: "ProcessRegistry | None" = None,
) -> subprocess.CompletedProcess[bytes]:
    arguments = ["git", SERVICE_COMMANDS[service], "--stateless-rpc"]
    if advertise:
        arguments.append("--advertise-refs")
    arguments.append(os.fsencode(repo))
    # The server process environment is operator-owned and passes through
    # (limit tuning, failpoints). Trusted identity keys are injected last
    # so they override anything inherited.
    env = clean_env()
    if protocol and PROTOCOL_HEADER.fullmatch(protocol):
        env["GIT_PROTOCOL"] = protocol
    if alternates is not None:
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(alternates)
    if extra_env:
        env.update(extra_env)
    child = subprocess.Popen(
        arguments,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    if registry is not None:
        registry.register(child)
    try:
        stdout, stderr = child.communicate(input=payload, timeout=SERVICE_TIMEOUTS[service])
        return subprocess.CompletedProcess(arguments, child.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        _terminate_group(child)
        raise HttpError(504, BODY_504)
    finally:
        if registry is not None:
            registry.forget(child)
        _release_child(child)


def _terminate_group(child: subprocess.Popen) -> None:
    """Terminate the child's process group, wait a grace period, then kill."""
    if child.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(child.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        child.wait(timeout=limits.GIT_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(child.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        child.wait(timeout=limits.GIT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _release_child(child: subprocess.Popen) -> None:
    for stream in (child.stdin, child.stdout, child.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass
    if child.poll() is None:
        _terminate_group(child)
    try:
        child.wait(timeout=limits.GIT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


class ProcessRegistry:
    """Track owned Git process groups so shutdown can terminate them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._children: set[subprocess.Popen] = set()

    def register(self, child: subprocess.Popen) -> None:
        with self._lock:
            self._children.add(child)

    def forget(self, child: subprocess.Popen) -> None:
        with self._lock:
            self._children.discard(child)

    def terminate_all(self, grace: float | None = None) -> None:
        with self._lock:
            children = list(self._children)
        for child in children:
            _terminate_group(child)


def stream_service(
    repo: Path,
    service: str,
    body_chunks,
    protocol: str | None,
    extra_env: dict[str, str] | None = None,
    alternates: Path | None = None,
    registry: ProcessRegistry | None = None,
    on_output=None,
) -> int:
    """Run one Git RPC with streaming stdin/stdout under bounded memory.

    The request body streams from ``body_chunks`` into the child's stdin
    (bounded chunks, natural pipe backpressure); stdout streams to
    ``on_output`` in bounded blocks; stderr is drained concurrently and
    never retained beyond a bounded tail.  The child runs in its own
    process group so hooks and descendants terminate together.
    """
    arguments = ["git", SERVICE_COMMANDS[service], "--stateless-rpc"]
    arguments.append(os.fsencode(repo))
    env = clean_env()
    if protocol and PROTOCOL_HEADER.fullmatch(protocol):
        env["GIT_PROTOCOL"] = protocol
    if alternates is not None:
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(alternates)
    if extra_env:
        env.update(extra_env)
    child = subprocess.Popen(
        arguments,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    if registry is not None:
        registry.register(child)
    input_error: list[BaseException] = []

    def pump_stdin() -> None:
        try:
            for chunk in body_chunks:
                child.stdin.write(chunk)
                child.stdin.flush()
            child.stdin.close()
        except (BrokenPipeError, ValueError):
            # Git exited early; its return code decides the outcome.
            try:
                child.stdin.close()
            except (OSError, ValueError):
                pass
        except BaseException as error:  # noqa: BLE001 - recorded and re-raised
            input_error.append(error)
            try:
                child.stdin.close()
            except (OSError, ValueError):
                pass

    def drain_stderr() -> None:
        try:
            while child.stderr.read(limits.STDOUT_CHUNK_BYTES):
                pass
        except OSError:
            pass

    writer = threading.Thread(target=pump_stdin, daemon=True)
    drainer = threading.Thread(target=drain_stderr, daemon=True)
    writer.start()
    drainer.start()
    deadline = time.monotonic() + SERVICE_TIMEOUTS[service]
    try:
        if on_output is not None:
            stdout = child.stdout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("git service timed out")
                ready, _, _ = select.select([stdout], [], [], remaining)
                if not ready:
                    raise TimeoutError("git service timed out")
                # Buffered ``read(n)`` may wait for n bytes after select()
                # reports one byte available; read the descriptor directly
                # so the deadline remains effective.
                block = os.read(stdout.fileno(), limits.STDOUT_CHUNK_BYTES)
                if not block:
                    break
                on_output(block)
        else:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("git service timed out")
                ready, _, _ = select.select([child.stdout], [], [], remaining)
                if not ready:
                    raise TimeoutError("git service timed out")
                if not os.read(child.stdout.fileno(), limits.STDOUT_CHUNK_BYTES):
                    break
        child.stdin.close()
        returncode = child.wait(timeout=limits.GIT_GRACE_SECONDS)
        writer.join(timeout=limits.GIT_GRACE_SECONDS)
        drainer.join(timeout=limits.GIT_GRACE_SECONDS)
        if input_error:
            raise input_error[0]
        return returncode
    except TimeoutError:
        _terminate_group(child)
        raise HttpError(504, BODY_504)
    except HttpError:
        _terminate_group(child)
        raise
    except BaseException:
        _terminate_group(child)
        raise
    finally:
        if registry is not None:
            registry.forget(child)
        for stream in (child.stdin, child.stdout, child.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        if child.poll() is None:
            _terminate_group(child)


class BastionRequestHandler(BaseHTTPRequestHandler):
    server_version = "bastion"
    protocol_version = "HTTP/1.1"
    timeout = 60

    @property
    def root(self) -> Path:
        return self.server.root

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        self._response_started = False
        deadline = time.monotonic() + limits.MAX_REQUEST_SECONDS
        if not self.server.request_slots.acquire(blocking=False):
            self.close_connection = True
            try:
                self._respond(503, BODY_503, "text/plain; charset=utf-8")
            except OSError:
                pass
            return
        try:
            self._handle(method, deadline)
        except HttpError as error:
            self.close_connection = True
            if not self._response_started:
                try:
                    self._respond(
                        error.status,
                        error.body,
                        "text/plain; charset=utf-8",
                        error.headers,
                    )
                except OSError:
                    # A malformed or slow client may disconnect before its
                    # rejection is written. The request is already closed
                    # and any Git process has been reaped.
                    pass
            else:
                try:
                    self.wfile.flush()
                except OSError:
                    pass
        except Exception:
            import traceback

            traceback.print_exc(file=sys.stderr)
            self.close_connection = True
            if not self._response_started:
                try:
                    self._respond(500, b"internal error", "text/plain; charset=utf-8")
                except OSError:
                    pass
        finally:
            self.server.request_slots.release()

    def _handle(self, method: str, deadline: float) -> None:
        split = urlsplit(self.path)
        route = parse_route(unquote(split.path))
        if route is None:
            raise HttpError(404, BODY_404)
        view, target = route
        parsed = parse_git_authorization(self.headers.get("Authorization"))
        if parsed is None:
            raise HttpError(401, BODY_401, {"WWW-Authenticate": 'Basic realm="bastion"'})
        username, credential = parsed
        # Basic usernames assert the routed view; Bearer carries no username.
        if username is not None and username != view:
            raise HttpError(404, BODY_404)
        expected_method = "GET" if target == "info/refs" else "POST"
        if method != expected_method:
            raise HttpError(405, BODY_405)
        record = authenticate_token(self.root, credential)
        if record is None:
            raise HttpError(401, BODY_401, {"WWW-Authenticate": 'Basic realm="bastion"'})
        if record["view_id"] != view:
            # Cross-view tokens and unknown views stay indistinguishable.
            raise HttpError(404, BODY_404)
        service = target if target != "info/refs" else parse_service(split.query)
        if service is None:
            raise HttpError(404, BODY_404)
        required = "fetch" if service == "git-upload-pack" else "push"
        if not has_scope(record, required):
            if service == "git-upload-pack":
                raise HttpError(403, b"forbidden")
            # Receive advertisement and RPC both require push scope.
            if target == "info/refs":
                raise HttpError(403, b"forbidden")
            raise HttpError(403, b"forbidden")
        receive = service == "git-receive-pack"
        remote = self._client_address()
        admitted, throttle = self.server.admit(remote, record["token_id"], receive)
        if not admitted:
            self.close_connection = True
            raise HttpError(throttle, BODY_429 if throttle == 429 else BODY_503)
        try:
            self._authorized(method, target, service, view, record, deadline, receive)
        finally:
            self.server.release_admission(remote, record["token_id"], receive)

    def _authorized(
        self,
        method: str,
        target: str,
        service: str,
        view: str,
        record: dict,
        deadline: float,
        receive: bool,
    ) -> None:
        # Budgets apply before replay, materialization, or subprocess work.
        if receive and limits.free_below_floor(self.root):
            raise HttpError(503, b"repository-read-only")
        try:
            _repository, manifest, active, materialized = _resolve_service(self.root, view)
            if service == "git-upload-pack":
                repo, alternates = materialized.path, None
            else:
                repo = ensure_intake_materialized(self.root, materialized, manifest)
                alternates = materialized.path / "objects"
        except ForgeError:
            raise HttpError(404, BODY_404)
        if not repo.is_dir():
            raise HttpError(404, BODY_404)
        if (
            receive
            and target == "info/refs"
            and manifest.head_seq >= limits.MAX_WAL_SEQUENCE
        ):
            raise HttpError(503, b"repository-read-only")
        if not self.server.git_slots.acquire(blocking=False):
            self.close_connection = True
            raise HttpError(503, BODY_503)
        protocol = self.headers.get("Git-Protocol")
        try:
            if target == "info/refs":
                completed = run_service(
                    repo,
                    service,
                    True,
                    b"",
                    protocol,
                    None,
                    alternates,
                    registry=self.server.registry,
                )
                if completed.returncode:
                    raise HttpError(500, b"git service failed")
                body = pkt_line(f"# service={service}\n".encode()) + b"0000" + completed.stdout
                content_type = f"application/x-{service}-advertisement"
                extra = dict(CACHE_HEADERS)
                self._respond(200, body, content_type, extra)
                return
            extra_env = {
                "BASTION_VIEW": view,
                "BASTION_TRANSPORT": "http",
                "BASTION_REMOTE_ADDR": self._client_address(),
            }
            if service == "git-receive-pack":
                extra_env["BASTION_PRINCIPAL_ID"] = active["principal_id"]
            idempotency_key = self.headers.get("Idempotency-Key")
            if idempotency_key and target == "git-receive-pack":
                if len(idempotency_key) > 256 or not idempotency_key.strip():
                    raise HttpError(400, b"invalid Idempotency-Key")
                extra_env["BASTION_IDEMPOTENCY_KEY"] = idempotency_key
            maximum = UPLOAD_MAX_BODY if service == "git-upload-pack" else RECEIVE_MAX_BODY
            encoding = self.headers.get("Content-Encoding")
            if encoding:
                # Compressed bodies decode under deterministic cumulative
                # caps before Git starts, so bombs are rejected without
                # spawning a child and without unbounded expansion.
                encoded = bytearray()
                for chunk in self._body_chunks(maximum, deadline):
                    encoded += chunk
                    if len(encoded) > maximum:
                        raise HttpError(400, BODY_400)
                payload = decode_body(bytes(encoded), encoding, maximum)
                body_chunks = iter(
                    payload[index : index + BODY_CHUNK_BYTES]
                    for index in range(0, len(payload), BODY_CHUNK_BYTES)
                )
            else:
                body_chunks = self._body_chunks(maximum, deadline)
            started = {"headers": False, "buffer": bytearray()}
            pre_header_limit = 256 << 10

            def on_output(block: bytes) -> None:
                if not started["headers"]:
                    buffer = started["buffer"]
                    buffer += block
                    if len(buffer) <= pre_header_limit:
                        return
                    # Real output overflows the pre-header buffer: begin the
                    # streamed 200 and flush what was held.
                    self._respond_headers(
                        200, f"application/x-{target}-result", {"Connection": "close"}
                    )
                    self.close_connection = True
                    started["headers"] = True
                    self.wfile.write(bytes(buffer))
                    buffer.clear()
                    return
                self.wfile.write(block)

            returncode = stream_service(
                repo,
                target,
                body_chunks,
                protocol,
                extra_env,
                alternates,
                registry=self.server.registry,
                on_output=on_output,
            )
            if returncode:
                if started["headers"]:
                    # The status can no longer change; terminate the response.
                    raise HttpError(500, b"git service failed")
                raise HttpError(500, b"git service failed")
            if not started["headers"]:
                self._respond_headers(
                    200, f"application/x-{target}-result", {"Connection": "close"}
                )
                self.close_connection = True
                if started["buffer"]:
                    self.wfile.write(bytes(started["buffer"]))
                    started["buffer"].clear()
            self.wfile.flush()
        finally:
            self.server.git_slots.release()

    def _principal_for(self, view: str) -> str | None:
        try:
            _repository, _manifest, active, _materialized = _resolve_service(self.root, view)
        except ForgeError:
            return None
        return active["principal_id"]

    def _client_address(self) -> str:
        """Client address, trusting forwarded addresses only from the proxy.

        The rightmost forwarded entry is the one the trusted proxy itself
        appended and is the only value the client cannot choose, so it is the
        sole accepted entry. Every candidate must parse as an IP literal;
        anything else is discarded rather than used as an admission bucket key.
        """
        peer = self.client_address[0] if self.client_address else ""
        trusted = os.environ.get("BASTION_TRUSTED_PROXY", "")
        if trusted and peer == trusted:
            forwarded = self.headers.get("X-Forwarded-For", "")
            # Rightmost entry = last proxy hop the trusted proxy appended.
            candidate = forwarded.rsplit(",", 1)[-1].strip()
            if candidate:
                try:
                    parsed = ipaddress.ip_address(candidate)
                except ValueError:
                    return peer
                # Normalize so equivalent spellings share one bucket.
                return str(parsed)
        return peer

    def _check_deadline(self, deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise HttpError(400, BODY_400)

    def _read_exact(self, amount: int, deadline: float) -> bytes:
        """Read exactly ``amount`` bytes; short reads are malformed framing."""
        parts: list[bytes] = []
        remaining = amount
        while remaining:
            block = self.rfile.read(min(remaining, BODY_CHUNK_BYTES))
            if not block:
                raise HttpError(400, BODY_400)
            parts.append(block)
            remaining -= len(block)
            self._check_deadline(deadline)
        return b"".join(parts)

    def _body_chunks(self, maximum: int, deadline: float):
        """Yield bounded request-body chunks under strict framing rules."""
        headers = self.headers
        length_header = headers.get("Content-Length")
        transfer = headers.get("Transfer-Encoding", "")
        chunked = bool(transfer) and "chunked" in transfer.lower()
        if chunked and length_header is not None:
            raise HttpError(400, BODY_400)
        if chunked:
            yield from self._chunked_chunks(maximum, deadline)
            return
        if transfer:
            raise HttpError(400, BODY_400)
        if length_header is None:
            raise HttpError(400, BODY_400)
        if not CONTENT_LENGTH_RE.fullmatch(length_header.strip()):
            raise HttpError(400, BODY_400)
        total = int(length_header)
        if total > maximum:
            raise HttpError(400, BODY_400)
        remaining = total
        while remaining:
            self._check_deadline(deadline)
            block = self.rfile.read(min(remaining, BODY_CHUNK_BYTES))
            if not block:
                raise HttpError(400, BODY_400)
            remaining -= len(block)
            yield block

    def _chunked_chunks(self, maximum: int, deadline: float):
        """Yield chunked-body chunks; syntax, counts, and totals are bounded."""
        total = 0
        chunks = 0
        while True:
            line = self.rfile.readline(MAX_CHUNK_LINE_BYTES + 1)
            if len(line) > MAX_CHUNK_LINE_BYTES:
                raise HttpError(400, BODY_400)
            if not line.endswith(b"\r\n"):
                raise HttpError(400, BODY_400)
            self._check_deadline(deadline)
            size_field = line[:-2].split(b";", 1)[0]
            if not CHUNK_SIZE_RE.fullmatch(size_field.decode("ascii", "replace")):
                raise HttpError(400, BODY_400)
            size = int(size_field, 16)
            if size == 0:
                trailers = 0
                while True:
                    trailer = self.rfile.readline(TRAILER_LINE_BYTES + 1)
                    if len(trailer) > TRAILER_LINE_BYTES:
                        raise HttpError(400, BODY_400)
                    self._check_deadline(deadline)
                    if not trailer:
                        raise HttpError(400, BODY_400)
                    if trailer == b"\r\n":
                        return
                    trailers += 1
                    if trailers > limits.MAX_TRAILERS:
                        raise HttpError(400, BODY_400)
            chunks += 1
            if chunks > limits.MAX_CHUNKS:
                raise HttpError(400, BODY_400)
            total += size
            if total > maximum:
                raise HttpError(400, BODY_400)
            yield self._read_exact(size, deadline)
            terminator = self.rfile.read(2)
            if terminator != b"\r\n":
                raise HttpError(400, BODY_400)
            self._check_deadline(deadline)

    def _respond(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra: dict[str, str] | None = None,
    ) -> None:
        self._response_started = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _respond_headers(
        self, status: int, content_type: str, extra: dict[str, str] | None = None
    ) -> None:
        """Begin a streamed response; the status can no longer change."""
        self._response_started = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _write_output_block(self, block: bytes) -> None:
        self.wfile.write(block)


class BastionServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], root: Path):
        self.root = root
        limits.validate()
        self.request_slots = threading.BoundedSemaphore(limits.MAX_ACTIVE_REQUESTS)
        self.git_slots = threading.BoundedSemaphore(limits.MAX_ACTIVE_GIT)
        self.receive_slots = threading.BoundedSemaphore(limits.MAX_ACTIVE_RECEIVES)
        self.registry = ProcessRegistry()
        self.stopping = False
        self._admission_lock = threading.Lock()
        self._active_by_ip: dict[str, int] = {}
        self._active_by_token: dict[str, int] = {}
        super().__init__(address, BastionRequestHandler)

    def admit(self, address: str, token_id: str | None, receive: bool) -> tuple[bool, int]:
        """Admit one request or report 429 (per-key) / 503 (global)."""
        receive_acquired = False
        if receive and not self.receive_slots.acquire(blocking=False):
            return False, 503
        receive_acquired = receive
        try:
            with self._admission_lock:
                if self.stopping:
                    if receive_acquired:
                        self.receive_slots.release()
                    return False, 503
                ip_limit = limits.MAX_RECEIVES_PER_IP if receive else limits.MAX_ACTIVE_PER_IP
                token_limit = (
                    limits.MAX_RECEIVES_PER_TOKEN if receive else limits.MAX_ACTIVE_PER_TOKEN
                )
                if self._active_by_ip.get(address, 0) >= ip_limit:
                    if receive_acquired:
                        self.receive_slots.release()
                    return False, 429
                if token_id is not None and self._active_by_token.get(token_id, 0) >= token_limit:
                    if receive_acquired:
                        self.receive_slots.release()
                    return False, 429
                self._active_by_ip[address] = self._active_by_ip.get(address, 0) + 1
                if token_id is not None:
                    self._active_by_token[token_id] = self._active_by_token.get(token_id, 0) + 1
                return True, 0
        except BaseException:
            if receive:
                self.receive_slots.release()
            raise

    def release_admission(self, address: str, token_id: str | None, receive: bool = False) -> None:
        if receive:
            self.receive_slots.release()
        with self._admission_lock:
            remaining = self._active_by_ip.get(address, 0) - 1
            if remaining > 0:
                self._active_by_ip[address] = remaining
            else:
                self._active_by_ip.pop(address, None)
            if token_id is not None:
                remaining = self._active_by_token.get(token_id, 0) - 1
                if remaining > 0:
                    self._active_by_token[token_id] = remaining
                else:
                    self._active_by_token.pop(token_id, None)

    def begin_shutdown(self, grace: float | None = None) -> None:
        """Stop admitting, drain in flight work, then terminate Git groups."""
        self.stopping = True
        deadline = time.monotonic() + (grace if grace is not None else limits.GIT_GRACE_SECONDS)
        while time.monotonic() < deadline:
            with self._admission_lock:
                active = sum(self._active_by_ip.values())
            if active == 0:
                break
            time.sleep(0.05)
        self.registry.terminate_all()


def make_server(root: Path, host: str = "127.0.0.1", port: int = 8000) -> BastionServer:
    limits.validate()
    if load_state_meta(root).get("repository_state") != "ready":
        raise ForgeError("forge initialization is incomplete")
    reject_legacy_tokens(root)
    return BastionServer((host, port), root)


def serve(root: Path, host: str, port: int) -> None:
    server = make_server(root, host, port)
    print(f"bastion: serving http://{host}:{server.server_port}", file=sys.stderr, flush=True)
    import signal as _signal

    stopping = threading.Event()

    def _initiate_stop(_signum, _frame) -> None:
        if stopping.is_set():
            return
        stopping.set()

    for name in ("SIGTERM", "SIGINT"):
        handler = getattr(_signal, name, None)
        if handler is not None:
            try:
                _signal.signal(handler, _initiate_stop)
            except (ValueError, OSError):
                pass

    def _drain() -> None:
        stopping.wait()
        server.begin_shutdown()
        server.shutdown()

    threading.Thread(target=_drain, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.registry.terminate_all()
