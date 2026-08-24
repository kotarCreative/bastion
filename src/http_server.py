"""Smart HTTP Git endpoint with per-view token authorization."""

from __future__ import annotations

import base64
import binascii
import gzip
import json
import os
import re
import secrets
import subprocess
import sys
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from errors import ForgeError
from git import clean_env
from layout import intake_repo, view_repo
from state import load_state


ROUTE = re.compile(
    r"/(?P<view>[A-Za-z0-9][A-Za-z0-9._-]{0,63})\.git/"
    r"(?P<target>info/refs|git-upload-pack|git-receive-pack)"
)
SERVICES = ("git-upload-pack", "git-receive-pack")
SERVICE_COMMANDS = {"git-upload-pack": "upload-pack", "git-receive-pack": "receive-pack"}
SERVICE_TIMEOUTS = {"git-upload-pack": 300, "git-receive-pack": 600}
UPLOAD_MAX_BODY = 32 << 20

from service import MAX_PACK_BYTES as RECEIVE_MAX_BODY
GZIP_MAX_RATIO = 32
PROTOCOL_HEADER = re.compile(r"version=2(=[0-9]+)?")
SUBPROCESS_TIMEOUT = 600

BODY_401 = b"authentication required"
BODY_404 = b"not found"
BODY_405 = b"method not allowed"
BODY_400 = b"bad request"
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
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    value = value.strip()
    if not value:
        return None
    scheme = scheme.lower()
    if scheme == "bearer":
        return value
    if scheme == "basic":
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
        _username, separator, password = decoded.partition(":")
        if not separator or not password:
            return None
        return password
    return None


def decode_body(
    body: bytes, encoding: str | None, maximum: int | None = None
) -> bytes:
    if not encoding or encoding.lower() == "identity":
        return body
    if encoding.lower() == "gzip":
        try:
            decoded = gzip.decompress(body)
        except (OSError, EOFError, zlib.error):
            raise HttpError(400, BODY_400)
        cap = min(maximum or (1 << 30), GZIP_MAX_RATIO * max(len(body), 1))
        if len(decoded) > cap:
            raise HttpError(400, BODY_400)
        return decoded
    raise HttpError(400, BODY_400)


def pkt_line(payload: bytes) -> bytes:
    return f"{len(payload) + 4:04x}".encode() + payload


def tokens_path(root: Path) -> Path:
    return root / "tokens.json"


def load_tokens(root: Path) -> dict[str, str]:
    try:
        data = json.loads(tokens_path(root).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        raise ForgeError(f"cannot read forge tokens: {error}") from error
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in data.items()
    ):
        raise ForgeError("forge tokens are malformed")
    return data


def add_token(root: Path, view: str) -> str:
    from state import locked

    state = load_state(root)
    if view not in state["views"]:
        raise ForgeError(f"unknown view: {view}")
    token = secrets.token_urlsafe(32)
    target = tokens_path(root)
    with locked(root, recoverer=lambda _root, _state: None):
        tokens = load_tokens(root)
        tokens[token] = view
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(tokens, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, target)
    from state import audit_event

    audit_event(root, "token.issue", f"{view}/-", "transport=http")
    return token


def service_repo(root: Path, view: str, service: str) -> tuple[Path, Path | None]:
    """Resolve a routed view to its backing repository.

    Uploads read from published view storage; receives target the isolated
    intake repository whose only object source is the sibling view store.
    """
    if service == "git-upload-pack":
        return view_repo(root, view), None
    return intake_repo(root, view), view_repo(root, view) / "objects"


def run_service(
    repo: Path,
    service: str,
    advertise: bool,
    payload: bytes,
    protocol: str | None,
    extra_env: dict[str, str] | None = None,
    alternates: Path | None = None,
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
    try:
        return subprocess.run(
            arguments,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=SERVICE_TIMEOUTS[service],
        )
    except subprocess.TimeoutExpired as error:
        raise HttpError(500, b"git service timed out") from error


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
        try:
            self._handle(method)
        except HttpError as error:
            self.close_connection = True
            self._respond(error.status, error.body, "text/plain; charset=utf-8", error.headers)
        except Exception:
            import traceback

            traceback.print_exc(file=sys.stderr)
            self.close_connection = True
            self._respond(500, b"internal error", "text/plain; charset=utf-8")

    def _handle(self, method: str) -> None:
        split = urlsplit(self.path)
        route = parse_route(unquote(split.path))
        if route is None:
            raise HttpError(404, BODY_404)
        view, target = route
        credential = parse_authorization(self.headers.get("Authorization"))
        if credential is None:
            raise HttpError(401, BODY_401, {"WWW-Authenticate": 'Basic realm="bastion"'})
        expected_method = "GET" if target == "info/refs" else "POST"
        if method != expected_method:
            raise HttpError(405, BODY_405)
        tokens = load_tokens(self.root)
        if credential not in tokens:
            raise HttpError(401, BODY_401, {"WWW-Authenticate": 'Basic realm="bastion"'})
        if tokens[credential] != view:
            raise HttpError(404, BODY_404)
        service = target if target != "info/refs" else parse_service(split.query)
        if service is None:
            raise HttpError(404, BODY_404)
        repo, alternates = service_repo(self.root, view, service)
        if not repo.is_dir():
            raise HttpError(404, BODY_404)
        protocol = self.headers.get("Git-Protocol")
        if target == "info/refs":
            completed = run_service(repo, service, True, b"", protocol, None, alternates)
            if completed.returncode:
                raise HttpError(500, completed.stderr[:4096])
            body = pkt_line(f"# service={service}\n".encode()) + b"0000" + completed.stdout
            content_type = f"application/x-{service}-advertisement"
            extra = dict(CACHE_HEADERS)
        else:
            raw = self._read_body(service)
            payload = decode_body(
                raw,
                self.headers.get("Content-Encoding"),
                UPLOAD_MAX_BODY if service == "git-upload-pack" else RECEIVE_MAX_BODY,
            )
            extra_env = {
                "BASTION_VIEW": view,
                "BASTION_TRANSPORT": "http",
                "BASTION_REMOTE_ADDR": self.client_address[0],
            }
            principal = self._principal_for(view)
            if principal:
                extra_env["BASTION_PRINCIPAL_ID"] = principal
            idempotency_key = self.headers.get("Idempotency-Key")
            if idempotency_key and target == "git-receive-pack":
                if len(idempotency_key) > 256 or not idempotency_key.strip():
                    raise HttpError(400, b"invalid Idempotency-Key")
                extra_env["BASTION_IDEMPOTENCY_KEY"] = idempotency_key
            completed = run_service(repo, target, False, payload, protocol, extra_env, alternates)
            if completed.returncode:
                raise HttpError(500, completed.stderr[:4096])
            body = completed.stdout
            content_type = f"application/x-{target}-result"
            extra = {}
        self._respond(200, body, content_type, extra)

    def _principal_for(self, view: str) -> str | None:
        try:
            state = load_state(self.root)
        except ForgeError:
            return None
        entry = state["views"].get(view)
        return entry.get("principal_id") if entry else None

    def _read_body(self, service: str) -> bytes:
        maximum = UPLOAD_MAX_BODY if service == "git-upload-pack" else RECEIVE_MAX_BODY
        transfer_encoding = self.headers.get("Transfer-Encoding", "")
        if "chunked" in transfer_encoding.lower():
            return self._read_chunked(maximum)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HttpError(400, BODY_400)
        if length < 0 or length > maximum:
            raise HttpError(400, BODY_400)
        return self.rfile.read(length) if length else b""

    def _read_chunked(self, maximum: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            line = self.rfile.readline(1024).strip()
            try:
                size = int(line.split(b";", 1)[0], 16)
            except ValueError:
                raise HttpError(400, BODY_400)
            if size == 0:
                while True:
                    trailer = self.rfile.readline(1024)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            total += size
            if total > maximum:
                raise HttpError(400, BODY_400)
            chunks.append(self.rfile.read(size))
            self.rfile.read(2)
        return b"".join(chunks)

    def _respond(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class BastionServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], root: Path):
        self.root = root
        super().__init__(address, BastionRequestHandler)


def make_server(root: Path, host: str = "127.0.0.1", port: int = 8000) -> BastionServer:
    load_state(root)
    return BastionServer((host, port), root)


def serve(root: Path, host: str, port: int) -> None:
    server = make_server(root, host, port)
    print(f"bastion: serving http://{host}:{server.server_port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
