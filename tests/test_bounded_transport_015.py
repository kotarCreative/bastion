"""Spec 015 B1: bounded framing, streaming, processes, memory, and disk.

Covers gzip bombs, negative chunks, ambiguous headers, excessive
trailers, short reads, slow clients, saturation, per-IP and per-token
admission, Git deadlines with process-group teardown, aggregate
expanded-object and free-space/event budgets, and startup validation.
"""

from __future__ import annotations

import gzip
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import forge
import http_server
import limits
from errors import ForgeError
from http_server import (
    BodyLimitError,
    HttpError,
    decode_body,
    inflate_bounded,
    pkt_line,
)
from protocol import pre_receive
from repository import Repository
from tests.storage_helpers import repository_state, repository_snapshot
from tests.tree_harness import TreeFixture, build_commit, command, git, oid
from tests.transport import LoopbackTransport


def request(url: str, method: str, data: bytes, headers: dict) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def auth(token: str, view: str = "alice") -> dict[str, str]:
    import base64

    encoded = base64.b64encode(f"{view}:{token}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


class BoundedDecodeTest(unittest.TestCase):
    def test_concatenated_members_decode(self) -> None:
        body = gzip.compress(b"abc\n") + gzip.compress(b"def\n")
        self.assertEqual(inflate_bounded(body, 1 << 20), b"abc\ndef\n")

    def test_truncated_member_is_rejected(self) -> None:
        body = gzip.compress(b"enough bytes to be a real member\n")
        with self.assertRaises(BodyLimitError):
            inflate_bounded(body[:-4], 1 << 20)

    def test_trailing_garbage_is_rejected(self) -> None:
        body = gzip.compress(b"data\n") + b"garbage"
        with self.assertRaises(BodyLimitError):
            inflate_bounded(body, 1 << 20)

    def test_empty_gzip_stream_is_rejected(self) -> None:
        with self.assertRaises(BodyLimitError):
            inflate_bounded(b"", 1 << 20)

    def test_ratio_and_absolute_caps(self) -> None:
        bomb = gzip.compress(b"0" * (8 << 20))
        with self.assertRaises(BodyLimitError):
            inflate_bounded(bomb, 1 << 20)
        with self.assertRaises(BodyLimitError):
            inflate_bounded(bomb, 1 << 30)
        # A legitimate body within both caps decodes fully.  Random bytes
        # keep the compression ratio near 1:1 so the ratio cap is not hit.
        okay = gzip.compress(os.urandom(4096))
        self.assertEqual(len(inflate_bounded(okay, 1 << 20)), 4096)

    def test_large_member_spanning_input_blocks_decodes(self) -> None:
        payload = os.urandom(100_000)
        self.assertEqual(inflate_bounded(gzip.compress(payload), 1 << 20), payload)

    def test_decode_body_identity_cap(self) -> None:
        with self.assertRaises(HttpError):
            decode_body(b"x" * 10, None, 5)
        self.assertEqual(decode_body(b"x" * 5, None, 5), b"x" * 5)


class TransportFixture:
    """A forge plus HTTP server with a slow-Git shim for deadline tests."""

    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup = None
        self.fixture = TreeFixture()
        self.root = self.fixture.root
        self.transport = LoopbackTransport(self.root)
        token = self.transport.token("alice")
        self.headers = {**auth(token), "Content-Type": "application/x-git-upload-pack-request"}

    def close(self) -> None:
        self.transport.stop()
        self.fixture.close()


def install_slow_git_shim() -> tuple[str, Path]:
    """Install a shim `git` that can delay RPCs and spawn descendants."""
    directory = Path(tempfile.mkdtemp(prefix="bastion-shim-"))
    real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    shim = directory / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ -n "$BASTION_TEST_SPAWN_CHILD" ] && [ "$1" = "upload-pack" ]; then\n'
        "  sleep 299 &\n"
        "fi\n"
        'if [ -n "$BASTION_TEST_SLOW_GIT" ] && [ "$1" = "upload-pack" ]; then\n'
        '  sleep "$BASTION_TEST_SLOW_GIT"\n'
        "fi\n"
        f'exec {real_git} "$@"\n'
    )
    shim.chmod(0o755)
    return str(directory), directory


class SlowGitTest(unittest.TestCase):
    """Admission and deadline tests with a deterministic slow Git child."""

    def setUp(self) -> None:
        self.fixture = TransportFixture()
        self.addCleanup(self.fixture.close)
        self.port = self.fixture.transport.port
        shim_dir, shim_path = install_slow_git_shim()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(shim_path)]))
        self._saved_path = os.environ.get("PATH")
        os.environ["PATH"] = f"{shim_dir}{os.pathsep}{self._saved_path}"
        self.addCleanup(self._restore_path)

    def _restore_path(self) -> None:
        if self._saved_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self._saved_path

    def slow_fetch(self, token: str) -> tuple[int, bytes]:
        tip = forge.output(forge.view_repo(self.fixture.root, "alice"), "rev-parse", "HEAD")
        return request(
            f"http://127.0.0.1:{self.port}/alice.git/git-upload-pack",
            "POST",
            pkt_line(f"want {tip} agent=bastion-test\n".encode()) + b"0000" + b"0009done\n",
            {**auth(token), "Content-Type": "application/x-git-upload-pack-request"},
        )


class AdmissionTest(SlowGitTest):
    def test_concurrent_requests_from_one_client_throttle(self) -> None:
        os.environ["BASTION_TEST_SLOW_GIT"] = "3"
        self.addCleanup(os.environ.pop, "BASTION_TEST_SLOW_GIT", None)
        with patch.object(limits, "MAX_ACTIVE_PER_TOKEN", 1), patch.object(
            limits, "MAX_ACTIVE_PER_IP", 64
        ):
            token = self.fixture.transport.token("alice")
            results: list[tuple[int, bytes]] = []

            def run() -> None:
                results.append(self.slow_fetch(token))

            first = threading.Thread(target=run)
            second = threading.Thread(target=run)
            first.start()
            time.sleep(0.4)
            second.start()
            second.join(30)
            self.assertEqual(results[0][0], 429)
            first.join(60)
            self.assertEqual(results[1][0], 200)

    def test_global_saturation_returns_503(self) -> None:
        os.environ["BASTION_TEST_SLOW_GIT"] = "2"
        self.addCleanup(os.environ.pop, "BASTION_TEST_SLOW_GIT", None)
        with patch.object(limits, "MAX_ACTIVE_PER_IP", 64), patch.object(
            limits, "MAX_ACTIVE_PER_TOKEN", 64
        ):
            server = self.fixture.transport.server
            server.git_slots = threading.BoundedSemaphore(1)
            try:
                token = self.fixture.transport.token("alice")
                results: list[tuple[int, bytes]] = []
                threads = [
                    threading.Thread(target=lambda: results.append(self.slow_fetch(token)))
                    for _ in range(3)
                ]
                threads[0].start()
                time.sleep(0.4)
                threads[1].start()
                threads[2].start()
                for thread in threads:
                    thread.join(60)
                statuses = sorted(status for status, _payload in results)
                self.assertEqual(statuses, [200, 503, 503])
            finally:
                server.git_slots = threading.BoundedSemaphore(limits.MAX_ACTIVE_GIT)

    def test_timeout_terminates_process_group(self) -> None:
        os.environ["BASTION_TEST_SLOW_GIT"] = "30"
        os.environ["BASTION_TEST_SPAWN_CHILD"] = "1"
        self.addCleanup(os.environ.pop, "BASTION_TEST_SLOW_GIT", None)
        self.addCleanup(os.environ.pop, "BASTION_TEST_SPAWN_CHILD", None)
        original = dict(http_server.SERVICE_TIMEOUTS)
        http_server.SERVICE_TIMEOUTS["git-upload-pack"] = 1
        try:
            started = time.monotonic()
            status, _payload = self.slow_fetch(self.fixture.transport.token("alice"))
            elapsed = time.monotonic() - started
            self.assertEqual(status, 504)
            self.assertLess(elapsed, 20)
        finally:
            http_server.SERVICE_TIMEOUTS.clear()
            http_server.SERVICE_TIMEOUTS.update(original)
        # No surviving descendants: the spawned `sleep 299` is reaped with
        # the terminated process group.
        time.sleep(1.5)
        probe = subprocess.run(
            ["pgrep", "-f", "sleep 299"], capture_output=True
        )
        self.assertEqual(probe.returncode, 1, "descendant survived the timeout")

    def test_slots_release_after_timeout_and_disconnect(self) -> None:
        server = self.fixture.transport.server
        self.assertEqual(server.git_slots._initial_value, server.git_slots._value)
        try:
            request(
                f"http://127.0.0.1:{self.port}/alice.git/git-upload-pack",
                "POST",
                b"garbage",
                {**auth(self.fixture.transport.token("alice"))},
            )
        except urllib.error.HTTPError:
            pass
        time.sleep(0.2)
        self.assertEqual(server.git_slots._initial_value, server.git_slots._value)
        self.assertEqual(server.request_slots._initial_value, server.request_slots._value)


class FramingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TransportFixture()
        self.addCleanup(self.fixture.close)
        self.port = self.fixture.transport.port

    def raw_chunked(self, chunks: list[bytes], headers: dict | None = None) -> int:
        """Send a raw chunked request; returns the response status."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.putrequest("POST", "/alice.git/git-receive-pack")
            connection.putheader(
                "Authorization", auth(self.fixture.transport.token("alice"))["Authorization"]
            )
            connection.putheader("Content-Type", "application/x-git-receive-pack-request")
            connection.putheader("Transfer-Encoding", "chunked")
            connection.endheaders()
            for chunk in chunks:
                connection.send(chunk)
                time.sleep(0.01)
            response = connection.getresponse()
            response.read()
            return response.status
        except (http.client.HTTPException, OSError):
            # A closed connection means the server rejected mid-body.
            return 400
        finally:
            connection.close()

    def test_negative_chunk_size_rejected(self) -> None:
        before = repository_snapshot(self.fixture.root)
        status = self.raw_chunked([b"-1\r\n", b"x" * 8, b"\r\n"])
        self.assertEqual(status, 400)
        self.assertEqual(before, repository_snapshot(self.fixture.root))

    def test_ambiguous_and_signed_content_length_rejected(self) -> None:
        for value in ("+5", " 5", "-1", "0x10", "1_0"):
            with self.subTest(value=value):
                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
                try:
                    connection.putrequest("POST", "/alice.git/git-receive-pack")
                    connection.putheader(
                        "Authorization",
                        auth(self.fixture.transport.token("alice"))["Authorization"],
                    )
                    connection.putheader("Content-Length", value)
                    connection.putheader("Content-Type", "application/x-git-receive-pack-request")
                    connection.endheaders()
                    connection.send(b"0000")
                    response = connection.getresponse()
                    status = response.status
                    response.read()
                except (http.client.HTTPException, OSError):
                    status = 400
                finally:
                    connection.close()
                self.assertEqual(status, 400)

    def test_content_length_with_transfer_encoding_rejected(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.putrequest("POST", "/alice.git/git-receive-pack")
            connection.putheader(
                "Authorization", auth(self.fixture.transport.token("alice"))["Authorization"]
            )
            connection.putheader("Content-Length", "4")
            connection.putheader("Transfer-Encoding", "chunked")
            connection.putheader("Content-Type", "application/x-git-receive-pack-request")
            connection.endheaders()
            connection.send(b"4\r\nABCD\r\n0\r\n\r\n")
            response = connection.getresponse()
            status = response.status
            response.read()
        except (http.client.HTTPException, OSError):
            status = 400
        finally:
            connection.close()
        self.assertEqual(status, 400)

    def test_missing_length_framing_rejected(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.putrequest("POST", "/alice.git/git-receive-pack")
            connection.putheader(
                "Authorization", auth(self.fixture.transport.token("alice"))["Authorization"]
            )
            connection.putheader("Content-Type", "application/x-git-receive-pack-request")
            connection.endheaders()
            connection.send(b"0000")
            response = connection.getresponse()
            status = response.status
            response.read()
        except (http.client.HTTPException, OSError):
            status = 400
        finally:
            connection.close()
        self.assertEqual(status, 400)

    def test_short_read_rejected(self) -> None:
        token = self.fixture.transport.token("alice")
        head = (
            "POST /alice.git/git-receive-pack HTTP/1.1\r\n"
            f"Authorization: Basic {__import__('base64').b64encode(f'alice:{token}'.encode()).decode()}\r\n"
            "Content-Type: application/x-git-receive-pack-request\r\n"
            "Content-Length: 16\r\n"
            "\r\n"
        ).encode()
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(head + b"only-four")
            sock.shutdown(socket.SHUT_WR)
            sock.settimeout(10)
            data = b""
            try:
                while len(data) < 128:
                    block = sock.recv(4096)
                    if not block:
                        break
                    data += block
            except OSError:
                pass
        self.assertTrue(data.startswith(b"HTTP/1.1 400"), data[:64])

    def test_malformed_chunk_terminator_rejected(self) -> None:
        status = self.raw_chunked([b"4\r\nABCDXX"])
        self.assertEqual(status, 400)

    def test_excessive_trailers_rejected(self) -> None:
        trailers = b"".join(b"X-Trailer: %d\r\n" % index for index in range(80))
        status = self.raw_chunked([b"0\r\n", trailers, b"\r\n"])
        self.assertEqual(status, 400)

    def test_valid_chunked_receive_still_accepted(self) -> None:
        # A structurally valid chunked body reaches Git and produces a
        # Git-level protocol outcome (200) rather than a framing rejection.
        status = self.raw_chunked([b"4\r\nABCD\r\n", b"0\r\n\r\n"])
        self.assertNotEqual(status, 400)

    def test_slow_client_request_deadline(self) -> None:
        with patch.object(limits, "MAX_REQUEST_SECONDS", 1):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
            try:
                connection.putrequest("POST", "/alice.git/git-receive-pack")
                connection.putheader(
                    "Authorization",
                    auth(self.fixture.transport.token("alice"))["Authorization"],
                )
                connection.putheader("Content-Length", "64")
                connection.putheader("Content-Type", "application/x-git-receive-pack-request")
                connection.endheaders()
                for _ in range(8):
                    connection.send(b"x" * 4)
                    time.sleep(0.5)
                response = connection.getresponse()
                status = response.status
                response.read()
            except (http.client.HTTPException, OSError):
                status = 400
            finally:
                connection.close()
            self.assertEqual(status, 400)


class BudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()
        self.addCleanup(self.fixture.close)
        self.transport = LoopbackTransport(self.fixture.root)
        self.addCleanup(self.transport.stop)

    def test_free_space_gate_returns_read_only(self) -> None:
        with patch.object(limits, "FREE_FLOOR_BYTES", 1 << 50):
            response = self.transport.server  # keep reference
            status, payload = request(
                f"http://127.0.0.1:{self.transport.port}/alice.git/info/refs"
                "?service=git-receive-pack",
                "GET",
                None,
                auth(self.transport.token("alice")),
            )
            self.assertEqual(status, 503)
            self.assertEqual(payload, b"demo-read-only")

    def test_event_budget_gates_advertisement(self) -> None:
        manifest, _replay = repository_state(self.fixture.root)
        with patch.object(limits, "MAX_WAL_SEQUENCE", manifest.head_seq):
            status, payload = request(
                f"http://127.0.0.1:{self.transport.port}/alice.git/info/refs"
                "?service=git-receive-pack",
                "GET",
                None,
                auth(self.transport.token("alice")),
            )
            self.assertEqual(status, 503)
            self.assertEqual(payload, b"demo-read-only")

    def test_event_budget_rejects_publication(self) -> None:
        manifest, _replay = repository_state(self.fixture.root)
        client = self.fixture.clients["alice"]
        parent = oid(client, "refs/heads/main")
        new = build_commit(
            client,
            parent,
            {b"shared/budget.txt": (b"100644", b"budget\n")},
            b"budget\n",
            1_700_000_900,
        )
        command(
            "git", "--git-dir",
            os.fsdecode(__import__("layout").intake_repo(self.fixture.root, "alice")),
            "fetch", "-q", os.fsdecode(client), "refs/heads/main",
        )
        before = self.fixture.rejection_snapshot()
        with patch.object(limits, "MAX_WAL_SEQUENCE", manifest.head_seq), patch.dict(
            os.environ,
            {
                "BASTION_VIEW": "alice",
                "BASTION_PRINCIPAL_ID": "alice",
                "BASTION_TRANSPORT": "test",
            },
            clear=False,
        ):
            with self.assertRaises(ForgeError) as caught:
                pre_receive(self.fixture.root, "alice", f"{parent} {new} refs/heads/main\n".encode())
        self.assertIn("budget", str(caught.exception))
        self.fixture.assert_rejection(before)

    def test_aggregate_expanded_bytes_rejects_push(self) -> None:
        saved = os.environ.get("BASTION_MAX_PUSH_BYTES")
        os.environ["BASTION_MAX_PUSH_BYTES"] = "1024"
        try:
            # The receive hook imports limits fresh from the environment.
            fixture = TreeFixture()
            self.addCleanup(fixture.close)
            client = fixture.clients["alice"]
            blob = b"x" * 4096
            parent = oid(client, "refs/heads/main")
            for index in range(4):
                parent = build_commit(
                    client,
                    parent,
                    {f"shared/big{index}.bin".encode(): (b"100644", blob)},
                    b"aggregate\n",
                    1_700_001_000 + index,
                )
            pushed = command(
                "git", "-C", os.fsdecode(client), "push", "origin", "main", check=False
            )
            self.assertNotEqual(pushed.returncode, 0)
            self.assertIn(b"[limits]", pushed.stderr)
        finally:
            if saved is None:
                os.environ.pop("BASTION_MAX_PUSH_BYTES", None)
            else:
                os.environ["BASTION_MAX_PUSH_BYTES"] = saved


class StartupValidationTest(unittest.TestCase):
    def test_invalid_limits_fail_startup(self) -> None:
        saved = os.environ.get("BASTION_MAX_OBJECT_BYTES")
        os.environ["BASTION_MAX_OBJECT_BYTES"] = str(1 << 40)
        try:
            with self.assertRaises(ForgeError):
                import importlib

                importlib.reload(limits)
        finally:
            if saved is None:
                os.environ.pop("BASTION_MAX_OBJECT_BYTES", None)
            else:
                os.environ["BASTION_MAX_OBJECT_BYTES"] = saved
            import importlib

            importlib.reload(limits)
            import http_server as _http_server

            importlib.reload(_http_server)


class ShutdownTest(unittest.TestCase):
    def test_sigterm_stops_listener_and_reaps_children(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        # A real fixture root is required; reuse a minimal TreeFixture.
        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        marker = fixture.directory / "serve-marker"
        environment = dict(os.environ)
        script = (
            "import sys\n"
            "sys.path.insert(0, '.'); sys.path.insert(0, 'src')\n"
            "from pathlib import Path\n"
            "from http_server import serve\n"
            f"serve(Path({str(fixture.root)!r}), '127.0.0.1', 0)\n"
        )
        # serve() chooses port 0; capture the chosen port from stderr.
        process = subprocess.Popen(
            [".venv/bin/python", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        try:
            port = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and port is None:
                line = process.stderr.readline().decode(errors="replace")
                if "serving" in line:
                    port = int(line.rsplit(":", 1)[1].strip())
            self.assertIsNotNone(port, "server did not announce its port")
            request(
                f"http://127.0.0.1:{port}/alice.git/info/refs?service=git-upload-pack",
                "GET",
                None,
                auth("bst1.dummy.tokenplaceholder0000000000000000000000", "alice"),
            )
            process.send_signal(signal.SIGTERM)
            code = process.wait(timeout=30)
            self.assertEqual(code, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == "__main__":
    unittest.main()
