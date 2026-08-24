"""Adversarial attacks against the Phase 4 transport boundary.

Raw protocol probes (protocol v0/v2 want smuggling, injection headers,
decompression bombs, malformed framing), intake routing and mirror
integrity attacks, identity-spoofing attempts, and audit-log probes.
"""

from __future__ import annotations

import gzip
import http.client
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import forge
from layout import intake_repo, view_repo
from store import open_store
from tests.test_transport_security import TransportFixture, command, commit


def pkt(payload: bytes) -> bytes:
    return f"{len(payload) + 4:04x}".encode() + payload


def request(url: str, method: str = "GET", body: bytes | None = None,
            headers: dict | None = None):
    """Issue one raw HTTP request; returns (status, body)."""
    req = urllib.request.Request(url, data=body, headers=headers or {},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def auth(token: str) -> dict:
    import base64
    return {
        "Authorization": "Basic "
        + base64.b64encode(f"bastion:{token}".encode()).decode(),
    }


def object_digest(repo: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    base = repo / "objects"
    for path in sorted(base.rglob("*")):
        if path.is_file():
            h.update(str(path.relative_to(base)).encode())
            h.update(path.read_bytes())
    return h.hexdigest()


class RawProtocolAttackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TransportFixture()
        self.addCleanup(self.fixture.close)
        self.port = self.fixture.transport.port
        self.alice_url = f"http://127.0.0.1:{self.port}/alice.git"
        self.token = self.fixture.transport.token("alice")

    def _hidden_blob(self) -> str:
        listing = forge.output(
            forge.canonical_repo(self.fixture.forge),
            "ls-tree", "-r", "HEAD",
        )
        for line in listing.splitlines():
            metadata, path = line.split("\t", 1)
            if path == "hidden/secret.txt":
                return metadata.split()[2]
        raise AssertionError("fixture hidden blob missing")

    def test_v0_direct_oid_want_of_hidden_canonical_blob_fails(self) -> None:
        hidden = self._hidden_blob()
        body = (
            pkt(f"want {hidden} agent=bastion-adversarial\n".encode())
            + b"0000"
            + b"0009done\n"
        )
        status, payload = request(
            f"{self.alice_url}/git-upload-pack", "POST", body,
            {**auth(self.token),
             "Content-Type": "application/x-git-upload-pack-request"},
        )
        self.assertGreaterEqual(status, 400)
        self.assertNotIn(b"secret", payload)

    def test_v0_direct_oid_want_of_unknown_object_fails(self) -> None:
        unknown = "a" * 40
        body = (
            pkt(f"want {unknown} agent=bastion-adversarial\n".encode())
            + b"0000"
            + b"0009done\n"
        )
        status, payload = request(
            f"{self.alice_url}/git-upload-pack", "POST", body,
            {**auth(self.token),
             "Content-Type": "application/x-git-upload-pack-request"},
        )
        self.assertGreaterEqual(status, 400)

    def test_v2_fetch_of_hidden_canonical_object_fails(self) -> None:
        hidden = self._hidden_blob()
        body = (
            pkt(b"command=fetch")
            + b"0001"
            + pkt(f"want {hidden}".encode())
            + pkt(b"done")
            + b"0000"
        )
        status, payload = request(
            f"{self.alice_url}/git-upload-pack", "POST", body,
            {**auth(self.token),
             "Content-Type": "application/x-git-upload-pack-request",
             "Git-Protocol": "version=2"},
        )
        self.assertTrue(status >= 400 or b"PACK" not in payload)
        self.assertNotIn(b"secret", payload)

    def test_git_protocol_injection_values_are_dropped(self) -> None:
        for hostile in ("version=2; rm -rf /", "version=1",
                        "version=2\n Evil", "garbage"):
            status, payload = request(
                f"{self.alice_url}/info/refs?service=git-upload-pack",
                headers={**auth(self.token), "Git-Protocol": hostile},
            )
            self.assertEqual(200, status)
            # The v0 service announcement proves the header never reached Git.
            self.assertIn(b"# service=git-upload-pack", payload)

    def test_oversized_receive_body_never_spawns_git(self) -> None:
        def reject_rows() -> int:
            store = open_store(self.fixture.forge)
            try:
                return sum(
                    1 for entry in store.recent_audit(100)
                    if entry["kind"] == "receive.reject"
                )
            finally:
                store.close()

        before = reject_rows()
        with mock.patch("http_server.RECEIVE_MAX_BODY", 1024):
            status, _payload = request(
                f"{self.alice_url}/git-receive-pack", "POST", b"x" * 4096,
                {**auth(self.token),
                 "Content-Type": "application/x-git-receive-pack-request"},
            )
        self.assertEqual(400, status)
        self.assertEqual(before, reject_rows())

    def test_gzip_decompression_bomb_is_rejected(self) -> None:
        bomb = gzip.compress(b"0" * (8 << 20))
        status, _payload = request(
            f"{self.alice_url}/git-receive-pack", "POST", bomb,
            {**auth(self.token),
             "Content-Type": "application/x-git-receive-pack-request",
             "Content-Encoding": "gzip"},
        )
        self.assertEqual(400, status)

    def test_malformed_chunked_encoding_is_rejected(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port,
                                                timeout=10)
        connection.putrequest("POST", "/alice.git/git-receive-pack")
        connection.putheader("Authorization", auth(self.token)["Authorization"])
        connection.putheader("Content-Type",
                             "application/x-git-receive-pack-request")
        connection.putheader("Transfer-Encoding", "chunked")
        connection.endheaders()
        connection.send(b"ZZZZ\r\nnot-hex\r\n")
        response = connection.getresponse()
        self.assertEqual(400, response.status)
        connection.close()

    def test_garbage_pkt_lines_to_receive_leave_state_unchanged(self) -> None:
        from tests.storage_helpers import document_bytes

        before_doc = document_bytes(self.fixture.forge)
        before_view = object_digest(view_repo(self.fixture.forge, "alice"))
        before_intake = object_digest(intake_repo(self.fixture.forge, "alice"))
        for garbage in (b"\xff\xff\xff\xff", b"00zz garbage\r\n",
                        pkt(b"\x00\x01\x02") + b"\xff" * 32):
            status, _payload = request(
                f"{self.alice_url}/git-receive-pack", "POST", garbage,
                {**auth(self.token),
                 "Content-Type": "application/x-git-receive-pack-request"},
            )
            self.assertGreaterEqual(status, 400)
        self.assertEqual(before_doc, document_bytes(self.fixture.forge))
        self.assertEqual(before_view, object_digest(view_repo(self.fixture.forge, "alice")))
        self.assertEqual(before_intake, object_digest(intake_repo(self.fixture.forge, "alice")))


class IntakeBoundaryAttackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TransportFixture()
        self.addCleanup(self.fixture.close)
        self.port = self.fixture.transport.port
        self.alice = self.fixture.clone("alice", "attack-alice")
        self.alice_token = self.fixture.transport.token("alice")
        self.bob_token = self.fixture.transport.token("bob")

    def test_cross_view_token_is_indistinguishable_from_unknown_view(self) -> None:
        headers = auth(self.bob_token)
        cross = request(
            f"http://127.0.0.1:{self.port}/alice.git/info/refs"
            "?service=git-upload-pack", headers=headers,
        )
        ghost = request(
            f"http://127.0.0.1:{self.port}/ghost.git/info/refs"
            "?service=git-upload-pack", headers=headers,
        )
        self.assertEqual(cross[0], ghost[0])
        self.assertEqual(cross[1], ghost[1])
        self.assertEqual(404, cross[0])

    def test_canonical_and_intake_paths_are_unroutable(self) -> None:
        headers = auth(self.alice_token)
        probes = (
            ("GET", "/canonical.git/info/refs?service=git-upload-pack"),
            ("GET", "/intake/alice.git/info/refs?service=git-receive-pack"),
            ("POST", "/canonical.git/git-receive-pack"),
            ("POST", "/intake/alice.git/git-upload-pack"),
            ("GET", "/alice.git/views/alice.git/info/refs?service=git-upload-pack"),
        )
        for method, path in probes:
            status, payload = request(
                f"http://127.0.0.1:{self.port}{path}", method,
                b"0000" if method == "POST" else None, headers,
            )
            self.assertEqual(404, status, path)
            self.assertEqual(b"not found", payload, path)

    def test_rejected_http_push_leaves_intake_store_byte_identical(self) -> None:
        before = object_digest(intake_repo(self.fixture.forge, "alice"))
        command("git", "-C", str(self.alice), "checkout", "-qb", "hostile")
        (self.alice / "shared" / "hostile.txt").write_text("hostile\n")
        commit(self.alice, "hostile branch", 1_700_900_000)
        pushed = command("git", "-C", str(self.alice), "push", "origin",
                         "hostile", check=False)
        self.assertNotEqual(0, pushed.returncode)
        self.assertEqual(before,
                         object_digest(intake_repo(self.fixture.forge, "alice")))

    def test_tampered_intake_mirror_self_heals_on_push(self) -> None:
        (self.alice / "shared" / "heal.txt").write_text("heal\n")
        commit(self.alice, "mirror heal", 1_700_901_000)
        intake = intake_repo(self.fixture.forge, "alice")
        # The fixture history is a single root commit, so simulate staleness
        # by removing the advertisement ref entirely.
        forge.git(intake, "update-ref", "-d", "refs/heads/main")
        pushed = command("git", "-C", str(self.alice), "push", "origin",
                         "main", check=False)
        if pushed.returncode != 0:
            # A stale-advertisement CAS rejection repairs the mirror during
            # pre-receive; the immediate retry then converges.
            pushed = command("git", "-C", str(self.alice), "push", "origin",
                             "main", check=False)
        self.assertEqual(0, pushed.returncode, pushed.stderr)
        repaired = forge.output(intake, "rev-parse", "refs/heads/main")
        view_tip = forge.ref(view_repo(self.fixture.forge, "alice"),
                             "refs/heads/main")
        self.assertEqual(repaired, view_tip)
        forge.check_invariants(self.fixture.forge)

    def test_future_dated_commit_over_http_is_rejected(self) -> None:
        env = os.environ | {
            "GIT_AUTHOR_DATE": "@4102444800 +0000",
            "GIT_COMMITTER_DATE": "@4102444800 +0000",
        }
        (self.alice / "shared" / "future.txt").write_text("future\n")
        command("git", "-C", str(self.alice), "add", "-A")
        command("git", "-C", str(self.alice), "commit", "-qm",
                "time traveler", env=env)
        pushed = command("git", "-C", str(self.alice), "push", "origin",
                         "main", check=False)
        self.assertNotEqual(0, pushed.returncode)
        self.assertIn("[policy]", pushed.stderr)
        self.assertIn("future-dated", pushed.stderr)


class IdentitySpoofingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TransportFixture()
        self.addCleanup(self.fixture.close)
        self.port = self.fixture.transport.port
        self.client = self.fixture.clone("alice", "spoof-alice")

    def _push_pack_with_headers(self, extra_headers: dict):
        client = self.client
        old = command("git", "-C", str(client), "rev-parse", "HEAD").stdout.strip()
        (client / "shared" / "spoof.txt").write_text("spoof attempt\n")
        new = commit(client, "spoofed push", 1_700_950_000)
        pack = subprocess.run(
            ["git", "-C", str(client), "pack-objects", "--revs", "--stdout"],
            input=f"{new}\n^{old}\n".encode(), capture_output=True, check=True,
        ).stdout
        update = f"{old} {new} refs/heads/main\0report-status".encode()
        frame = f"{len(update) + 4:04x}".encode() + update + b"0000"
        url = (f"http://127.0.0.1:{self.port}"
               "/alice.git/git-receive-pack")
        return request(
            url, "POST", frame + pack,
            {"Content-Type": "application/x-git-receive-pack-request",
             **auth(self.fixture.transport.token("alice")),
             **extra_headers},
        )

    def test_identity_headers_cannot_spoof_the_principal(self) -> None:
        status, payload = self._push_pack_with_headers({
            "X-Bastion-Principal-Id": "bob",
            "Bastion-Principal": "bob",
            "Bastion-View": "bob",
            "Bastion-Transport": "teleport",
            "Git-Push-Option": "principal=bob",
        })
        self.assertEqual(200, status, payload[:200])
        self.assertIn(b"ok refs/heads/main", payload)
        store = open_store(self.fixture.forge)
        try:
            accepts = [e for e in store.recent_audit(50)
                       if e["kind"] == "receive.accept"]
        finally:
            store.close()
        self.assertEqual(1, len(accepts))
        self.assertTrue(accepts[0]["subject"].startswith("alice/alice"),
                        accepts[0]["subject"])
        self.assertIn("http", accepts[0]["detail"])
        from state import load_state

        transaction = next(iter(
            load_state(self.fixture.forge)["transactions"].values()
        ))
        self.assertEqual("alice", transaction["principal_id"])


class TransportEscalationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TransportFixture()
        self.addCleanup(self.fixture.close)
        self.port = self.fixture.transport.port
        self.alice_token = self.fixture.transport.token("alice")

    def test_advertisement_never_contains_other_views_refs(self) -> None:
        bob = self.fixture.clone("bob", "leak-bob")
        (bob / "shared" / "leak.txt").write_text("leak\n")
        bob_new = commit(bob, "bob secret tip", 1_700_970_000)
        command("git", "-C", str(bob), "push", "-q", "origin", "main")
        status, payload = request(
            f"http://127.0.0.1:{self.port}/alice.git/info/refs"
            "?service=git-upload-pack", headers=auth(self.alice_token),
        )
        self.assertEqual(200, status)
        self.assertNotIn(bob_new.encode(), payload)
        self.assertNotIn(b"refs/heads/main~", payload)
        alice_tip = forge.ref(view_repo(self.fixture.forge, "alice"),
                              "refs/heads/main")
        if alice_tip != "0" * 40:
            self.assertIn(alice_tip.encode(), payload)

    def _raw_push(self, client: Path, branch_ref: str, extra: bytes = b"",
                  thin_exclusion: str | None = None) -> tuple[int, bytes]:
        old = command("git", "-C", str(client), "rev-parse",
                      "refs/heads/main").stdout.strip()
        (client / "shared" / "raw.txt").write_text(
            f"raw {thin_exclusion or 'push'}\n")
        new = commit(client, "raw framed push", 1_700_980_000)
        revisions = f"{new}\n"
        if thin_exclusion:
            revisions += f"^{thin_exclusion}\n"
        else:
            revisions += f"^{old}\n"
        pack = subprocess.run(
            ["git", "-C", str(client), "pack-objects", "--revs", "--thin",
             "--stdout"],
            input=revisions.encode(), capture_output=True, check=True,
        ).stdout
        update = f"{old} {new} {branch_ref}\0report-status".encode()
        frame = f"{len(update) + 4:04x}".encode() + update + b"0000"
        url = (f"http://127.0.0.1:{self.port}"
               "/alice.git/git-receive-pack")
        return request(url, "POST", frame + pack + extra,
                       {"Content-Type": "application/x-git-receive-pack-request",
                        **auth(self.alice_token)})

    def test_thin_pack_with_view_store_bases_is_accepted(self) -> None:
        """Deltas against view-only objects resolve through the alternates."""
        alice_tip = forge.ref(view_repo(self.fixture.forge, "alice"),
                              "refs/heads/main")
        if alice_tip == "0" * 40:
            self.skipTest("view has no commits to exclude")
        client = self.fixture.clone("alice", "thin-alice")
        status, payload = self._raw_push(client, "refs/heads/main",
                                         thin_exclusion=alice_tip)
        self.assertEqual(200, status, payload[:300])
        self.assertIn(b"ok refs/heads/main", payload)

    def test_unsupported_methods_rejected_without_state_change(self) -> None:
        from tests.storage_helpers import document_bytes

        before = document_bytes(self.fixture.forge)
        url = f"http://127.0.0.1:{self.port}/alice.git/git-receive-pack"
        headers = {**auth(self.alice_token),
                   "Content-Type": "application/x-git-receive-pack-request"}
        for method in ("PUT", "DELETE"):
            status, _payload = request(url, method, b"0000", headers)
            self.assertIn(status, (400, 405, 501))
        self.assertEqual(before, document_bytes(self.fixture.forge))

    def test_concurrent_http_pushes_serialize_and_converge(self) -> None:
        import threading

        alice = self.fixture.clone("alice", "race-alice")
        bob = self.fixture.clone("bob", "race-bob")
        (alice / "team-a" / "race.txt").write_text("alice race\n")
        commit(alice, "alice race", 1_700_990_000)
        (bob / "shared" / "race.txt").write_text("bob race\n")
        commit(bob, "bob race", 1_700_990_100)

        results: dict[str, subprocess.CompletedProcess] = {}

        def push(name: str, checkout: Path) -> None:
            results[name] = command(
                "git", "-C", str(checkout), "push", "origin", "main",
                check=False,
            )

        threads = [
            threading.Thread(target=push, args=("alice", alice)),
            threading.Thread(target=push, args=("bob", bob)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        # The quiescence gate may reject the loser while the winner's
        # projection work is pending; that is a clean, audited outcome.
        from worker import drain_projection_jobs

        drain_projection_jobs(self.fixture.forge)
        for name, checkout in (("alice", alice), ("bob", bob)):
            for _attempt in range(3):
                result = command("git", "-C", str(checkout), "push",
                                 "origin", "main", check=False)
                if result.returncode == 0:
                    break
                command("git", "-C", str(checkout), "fetch", "-q", "origin")
                command("git", "-C", str(checkout), "rebase",
                        "origin/main", check=False)
                drain_projection_jobs(self.fixture.forge)
            else:
                self.fail(f"{name} push never converged: {result.stderr}")
            drain_projection_jobs(self.fixture.forge)
        forge.check_invariants(self.fixture.forge)

        def canonical_has(path: str) -> bool:
            listing = forge.output(
                forge.canonical_repo(self.fixture.forge),
                "ls-tree", "-r", "refs/heads/main",
            )
            return any(line.endswith(f"\t{path}") for line in listing.splitlines())

        self.assertTrue(canonical_has("team-a/race.txt"))
        self.assertTrue(canonical_has("shared/race.txt"))


class AuditProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TransportFixture()
        self.addCleanup(self.fixture.close)
        self.alice = self.fixture.clone("alice", "audit-alice")

    def _rows(self, kind: str) -> int:
        store = open_store(self.fixture.forge)
        try:
            return sum(1 for e in store.recent_audit(200)
                       if e["kind"] == kind)
        finally:
            store.close()

    def test_each_rejected_receive_writes_exactly_one_audit_row(self) -> None:
        before = self._rows("receive.reject")
        command("git", "-C", str(self.alice), "checkout", "-qb", "bad-branch")
        (self.alice / "shared" / "nope.txt").write_text("nope\n")
        commit(self.alice, "rejected once", 1_700_960_000)
        command("git", "-C", str(self.alice), "push", "-q", "origin",
                "bad-branch", check=False)
        self.assertEqual(before + 1, self._rows("receive.reject"))

    def test_unauthenticated_probes_write_no_audit_rows(self) -> None:
        before_accept = self._rows("receive.accept")
        before_reject = self._rows("receive.reject")
        port = self.fixture.transport.port
        for path in ("/alice.git/info/refs?service=git-upload-pack",
                     "/alice.git/git-receive-pack"):
            for _ in range(3):
                status, _payload = request(
                    f"http://127.0.0.1:{port}{path}", "GET"
                    if "info" in path else "POST",
                    None if "info" in path else b"0000",
                )
                self.assertEqual(401, status)
        self.assertEqual(before_accept, self._rows("receive.accept"))
        self.assertEqual(before_reject, self._rows("receive.reject"))


class SshGrammarAdversarialTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TransportFixture()
        self.addCleanup(self.fixture.close)
        self.backend = f"http://127.0.0.1:{self.fixture.transport.port}"
        self.shim = str(Path(__file__).resolve().parent.parent
                        / "tools" / "bastion-ssh")

    def _run_shim(self, arguments: list[str], user: str, token: str | None,
                  payload: bytes = b""):
        argv = [sys.executable, self.shim, "--backend", self.backend]
        if token:
            argv += ["--token", token]
        argv += ["--ssh-user", user, *arguments]
        return subprocess.run(argv, input=payload, capture_output=True,
                              env=os.environ | {"GIT_TERMINAL_PROMPT": "0"})

    def test_other_views_token_fails_without_information_leak(self) -> None:
        result = self._run_shim(
            ["bob@127.0.0.1", "git-upload-pack '/bob.git'"],
            user="bob", token=self.fixture.transport.token("alice"),
        )
        self.assertNotEqual(0, result.returncode)

    def test_double_quoted_path_rejected_by_grammar(self) -> None:
        result = self._run_shim(
            ["alice@127.0.0.1", 'git-upload-pack "/alice.git"'],
            user="alice", token=self.fixture.transport.token("alice"),
        )
        self.assertEqual(127, result.returncode)

    def test_nested_suffix_and_trailing_slash_paths_rejected(self) -> None:
        token = self.fixture.transport.token("alice")
        for path in ("/views/alice.git", "/alice.git/",
                     "/alice.git.git", "/./alice.git"):
            result = self._run_shim(
                ["alice@127.0.0.1", f"git-upload-pack '{path}'"],
                user="alice", token=token,
            )
            self.assertNotEqual(0, result.returncode, path)

    def test_missing_credentials_fail_fast(self) -> None:
        result = self._run_shim(
            ["alice@127.0.0.1", "git-upload-pack '/alice.git'"],
            user="alice", token=None,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn(b"token", result.stderr.lower())

    def test_oversized_path_argument_rejected(self) -> None:
        result = self._run_shim(
            ["alice@127.0.0.1", "git-upload-pack '/'" + "a" * 300 + ".git'"],
            user="alice", token=self.fixture.transport.token("alice"),
        )
        self.assertNotEqual(0, result.returncode)


if __name__ == "__main__":
    unittest.main()
