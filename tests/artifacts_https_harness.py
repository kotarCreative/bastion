"""A local HTTPS Git smart-HTTP endpoint for production adapter tests.

The remote URL has the same shape as an Artifacts Git remote. Git traffic is
sent through a per-cache HTTP CONNECT proxy to a loopback TLS server, so tests
need neither DNS/hosts changes nor a relaxed certificate check. The server
validates the backing Bearer token before invoking ``git http-backend``.
"""

from __future__ import annotations

import os
import select
import socket
import ssl
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import ThreadingTCPServer
from typing import Any

from object_store_artifacts import ArtifactsObjectStore, ArtifactsObjectStoreConfig


TEST_HOST = "0123456789abcdef0123456789abcdef.artifacts.cloudflare.net"
TEST_TOKEN = "test-backing-token"
STORAGE_BRANCH = "bastion-storage"
MAX_HTTP_BODY = 256 * 1024 * 1024


def _git(*args: str, env: dict[str, str] | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args], env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"git command failed ({result.returncode}): {args[0]}")
    return result.stdout


class ArtifactsHttpsHarness:
    """Run a TLS git-http-backend plus a hostname-restricted CONNECT proxy.

    ``remote`` is the public-shaped Artifacts URL consumed by
    :class:`ArtifactsObjectStore`; ``repo`` is its actual local bare Git
    repository. Call :meth:`configure_cache` on every adapter instance,
    including instances opened in pre-receive hook subprocesses, by wrapping
    ``ArtifactsObjectStore.from_config`` in the test's patch context.
    """

    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="bastion-artifacts-https-")
        self.root = Path(self._temporary.name)
        self.project_root = self.root / "git-projects"
        self.repo = self.project_root / "git" / "test" / "runtime-e2e.git"
        self.repo.parent.mkdir(parents=True)
        _git("init", "--bare", "-q", str(self.repo))
        _git("--git-dir", str(self.repo), "config", "http.receivepack", "true")
        self.remote = f"https://{TEST_HOST}/git/test/runtime-e2e.git"
        self.config = ArtifactsObjectStoreConfig(
            remote=self.remote, branch=STORAGE_BRANCH,
        )
        self.request_log: list[dict[str, Any]] = []
        self._log_lock = threading.Lock()

        self.certificate = self.root / "test-ca.pem"
        private_key = self.root / "test-key.pem"
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(private_key), "-out", str(self.certificate),
                "-days", "2", "-subj", f"/CN={TEST_HOST}",
                "-addext", f"subjectAltName=DNS:{TEST_HOST}",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True,
        )

        harness = self

        class GitHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                # Never log arbitrary request headers or subprocess output.
                return

            def do_GET(self) -> None:
                self._run_cgi()

            def do_POST(self) -> None:
                self._run_cgi()

            def _run_cgi(self) -> None:
                authorization = self.headers.get("Authorization", "")
                authorized = authorization == f"Bearer {TEST_TOKEN}"
                with harness._log_lock:
                    harness.request_log.append({
                        "method": self.command,
                        "path": self.path,
                        "auth_passed": authorized,
                    })
                if not authorized:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", "Bearer")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
                if transfer_encoding:
                    self.send_error(400, "chunked request bodies are not supported by this fixture")
                    return
                raw_length = self.headers.get("Content-Length", "0")
                try:
                    length = int(raw_length)
                except ValueError:
                    self.send_error(400, "invalid content length")
                    return
                if length < 0 or length > MAX_HTTP_BODY:
                    self.send_error(413, "request body too large")
                    return
                body = self.rfile.read(length) if length else b""
                if len(body) != length:
                    self.send_error(400, "short request body")
                    return

                parsed_path, separator, query = self.path.partition("?")
                environment = os.environ.copy()
                environment.update({
                    "GIT_PROJECT_ROOT": str(harness.project_root),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "PATH_INFO": parsed_path,
                    "QUERY_STRING": query if separator else "",
                    "REQUEST_METHOD": self.command,
                    "SCRIPT_NAME": "",
                    "SERVER_PROTOCOL": self.protocol_version,
                    "SERVER_NAME": TEST_HOST,
                    "SERVER_PORT": "443",
                    "REMOTE_ADDR": self.client_address[0],
                    "REMOTE_USER": "bastion-test",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(length),
                })
                git_protocol = self.headers.get("Git-Protocol")
                if git_protocol:
                    environment["HTTP_GIT_PROTOCOL"] = git_protocol
                result = subprocess.run(
                    ["git", "http-backend"], env=environment, input=body,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                    timeout=120,
                )
                if result.returncode:
                    self.send_error(500, "git http-backend failed")
                    return
                header_block, delimiter, response_body = result.stdout.partition(b"\r\n\r\n")
                if not delimiter:
                    header_block, delimiter, response_body = result.stdout.partition(b"\n\n")
                if not delimiter:
                    self.send_error(500, "git http-backend returned malformed headers")
                    return

                status = 200
                response_headers: list[tuple[str, str]] = []
                for raw_header in header_block.replace(b"\r\n", b"\n").split(b"\n"):
                    if not raw_header:
                        continue
                    name, colon, value = raw_header.partition(b":")
                    if not colon:
                        self.send_error(500, "git http-backend returned malformed headers")
                        return
                    decoded_name = name.decode("ascii", "strict")
                    decoded_value = value.strip().decode("latin-1")
                    if decoded_name.lower() == "status":
                        try:
                            status = int(decoded_value.split(" ", 1)[0])
                        except ValueError:
                            self.send_error(500, "git http-backend returned invalid status")
                            return
                    else:
                        response_headers.append((decoded_name, decoded_value))
                self.send_response(status)
                has_length = False
                for name, value in response_headers:
                    lower = name.lower()
                    if lower in {"connection", "transfer-encoding", "keep-alive"}:
                        continue
                    has_length |= lower == "content-length"
                    self.send_header(name, value)
                if not has_length:
                    self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                if self.command != "HEAD" and response_body:
                    self.wfile.write(response_body)

        self._https_server = ThreadingHTTPServer(("127.0.0.1", 0), GitHandler)
        self._https_server.daemon_threads = True
        self._tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._tls_context.load_cert_chain(str(self.certificate), str(private_key))
        self._https_server.socket = self._tls_context.wrap_socket(
            self._https_server.socket, server_side=True,
        )
        self._https_port = self._https_server.server_address[1]

        class ProxyHandler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_CONNECT(self) -> None:
                host, colon, raw_port = self.path.rpartition(":")
                if not colon or host.strip("[]").lower() != TEST_HOST or raw_port != "443":
                    self.send_error(403, "CONNECT target is not the test Artifacts host")
                    return
                try:
                    upstream = socket.create_connection(("127.0.0.1", harness._https_port), timeout=5)
                except OSError:
                    self.send_error(502, "test TLS endpoint is unavailable")
                    return
                self.send_response(200, "Connection Established")
                self.end_headers()
                self.connection.setblocking(False)
                upstream.setblocking(False)
                peers = (self.connection, upstream)
                try:
                    while True:
                        readable, _writable, _exceptional = select.select(peers, [], peers, 30)
                        if _exceptional or not readable:
                            break
                        for source in readable:
                            target = upstream if source is self.connection else self.connection
                            try:
                                chunk = source.recv(65536)
                            except (BlockingIOError, OSError):
                                break
                            if not chunk:
                                return
                            target.sendall(chunk)
                finally:
                    upstream.close()

        class ProxyServer(ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._proxy_server = ProxyServer(("127.0.0.1", 0), ProxyHandler)
        self.proxy = f"http://127.0.0.1:{self._proxy_server.server_address[1]}"
        self._https_thread = threading.Thread(
            target=self._https_server.serve_forever, name="artifacts-test-https", daemon=True,
        )
        self._proxy_thread = threading.Thread(
            target=self._proxy_server.serve_forever, name="artifacts-test-proxy", daemon=True,
        )
        self._https_thread.start()
        self._proxy_thread.start()

    def configure_cache(self, store: ArtifactsObjectStore) -> ArtifactsObjectStore:
        """Route this adapter's Git HTTPS through the fixture with trusted TLS."""
        if store.remote != self.remote:
            raise ValueError("adapter remote does not match this HTTPS fixture")
        subprocess.run(
            ["git", "--git-dir", str(store._git_dir), "config", "--local", "http.proxy", self.proxy],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True,
        )
        subprocess.run(
            ["git", "--git-dir", str(store._git_dir), "config", "--local", "http.sslCAInfo", str(self.certificate)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True,
        )
        return store

    def from_config(self, config: ArtifactsObjectStoreConfig, cache_dir: Path) -> ArtifactsObjectStore:
        """Construct with the real production constructor, then configure its cache."""
        if config.remote != self.remote:
            raise ValueError("configuration remote does not match this HTTPS fixture")
        store = ArtifactsObjectStore.from_config(config, cache_dir)
        return self.configure_cache(store)

    def close(self) -> None:
        self._proxy_server.shutdown()
        self._https_server.shutdown()
        self._proxy_server.server_close()
        self._https_server.server_close()
        self._proxy_thread.join(timeout=2)
        self._https_thread.join(timeout=2)
        self._temporary.cleanup()

    def __enter__(self) -> "ArtifactsHttpsHarness":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()
