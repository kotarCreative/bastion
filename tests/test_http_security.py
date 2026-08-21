from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import forge
from tests import identity_harness
from git import git as repo_git, ref
from http_server import add_token, make_server
from layout import ZERO_OID, canonical_repo, view_repo


def command(*args: str, cwd: Path | None = None, check: bool = True,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ | {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "true",
    } | (env or {})
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=merged)
    if check and result.returncode:
        raise AssertionError(f"{' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result


def git(repo: Path, *args: str, check: bool = True,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return command("git", *args, cwd=repo, check=check, env=env)


class HttpSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        command("git", "init", "-q", "-b", "main", str(self.source))
        git(self.source, "config", "user.name", "Fixture")
        git(self.source, "config", "user.email", "fixture@example.test")
        (self.source / "visible").mkdir()
        (self.source / "hidden").mkdir()
        (self.source / "visible" / "file.txt").write_text("visible 1\n")
        (self.source / "hidden" / "secret.txt").write_text("secret 1\n")
        environment = dict(
            os.environ,
            GIT_AUTHOR_DATE="1700000000 +0000",
            GIT_COMMITTER_DATE="1700000000 +0000",
        )
        git(self.source, "add", "-A")
        git(self.source, "commit", "-q", "-m", "initial mixed state", env=environment)
        self.hidden_blob = git(
            self.source, "rev-parse", "HEAD:hidden/secret.txt"
        ).stdout.strip()

        self.forge = self.root / "forge"
        forge.init_forge(self.forge, self.source, "main")
        identity_harness.add_view(self.forge, "alice", ["visible"], ["visible"], "main")
        identity_harness.add_view(self.forge, "bob", ["visible"], ["visible"], "main")

        self.alice_token = add_token(self.forge, "alice")
        self.bob_token = add_token(self.forge, "bob")
        self.server = make_server(self.forge, "127.0.0.1", 0)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        self.temporary.cleanup()

    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def basic(self, token: str) -> str:
        return "Basic " + base64.b64encode(f"user:{token}".encode()).decode()

    def request(self, path: str, method: str = "GET",
                headers: dict[str, str] | None = None,
                data: bytes | None = None) -> tuple[int, bytes, dict[str, str]]:
        request = urllib.request.Request(
            f"{self.base()}{path}", data=data, method=method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            try:
                return error.code, error.read(), dict(error.headers)
            finally:
                error.close()

    def refs_snapshot(self) -> dict[str, str]:
        snapshot = {"canonical": ref(canonical_repo(self.forge), "refs/heads/main")}
        for name in ("alice", "bob"):
            snapshot[name] = ref(view_repo(self.forge, name), "refs/heads/main")
        return snapshot

    def forge_snapshot(self) -> tuple[bytes, bytes, dict[str, str]]:
        return (
            (self.forge / "state.json").read_bytes(),
            (self.forge / "tokens.json").read_bytes(),
            self.refs_snapshot(),
        )

    # -- authentication boundaries ------------------------------------

    def test_missing_credentials_rejected_uniformly(self) -> None:
        for path, method in (
            ("/alice.git/info/refs?service=git-upload-pack", "GET"),
            ("/alice.git/git-upload-pack", "POST"),
            ("/alice.git/git-receive-pack", "POST"),
        ):
            status, body, headers = self.request(path, method)
            self.assertEqual(status, 401, path)
            self.assertEqual(body, b"authentication required")
            self.assertTrue(headers["WWW-Authenticate"].startswith('Basic realm="bastion"'))

    def test_invalid_credentials_rejected(self) -> None:
        for headers in (
            {"Authorization": "Bearer does-not-exist"},
            {"Authorization": "Basic !!!"},
            {"Authorization": "Basic " + base64.b64encode(b"user:wrong").decode()},
            {"Authorization": "Digest zzz"},
        ):
            status, _, _ = self.request(
                "/alice.git/info/refs?service=git-upload-pack", headers=headers
            )
            self.assertEqual(status, 401, headers)

    def test_revoked_token_fails_immediately(self) -> None:
        token = add_token(self.forge, "alice")
        status, _, _ = self.request(
            "/alice.git/info/refs?service=git-upload-pack",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status, 200)
        path = self.forge / "tokens.json"
        remaining = {
            key: value
            for key, value in json.loads(path.read_text()).items()
            if key != token
        }
        path.write_text(json.dumps(remaining))
        status, _, _ = self.request(
            "/alice.git/info/refs?service=git-upload-pack",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status, 401)

    # -- cross-view isolation -----------------------------------------

    def test_wrong_view_token_is_indistinguishable_from_unknown_view(self) -> None:
        probes = [
            (
                "/alice.git/info/refs?service=git-upload-pack",
                {"Authorization": f"Bearer {self.bob_token}"},
            ),
            (
                "/carol.git/info/refs?service=git-upload-pack",
                {"Authorization": f"Bearer {self.bob_token}"},
            ),
            (
                "/carol.git/info/refs?service=git-upload-pack",
                {"Authorization": f"Bearer {self.alice_token}"},
            ),
        ]
        observed = [self.request(path, headers=headers) for path, headers in probes]
        for status, body, _ in observed:
            self.assertEqual(status, 404)
            self.assertEqual(body, b"not found")
        self.assertEqual(len({body for _, body, _ in observed}), 1)

    def test_cross_view_post_endpoints_rejected(self) -> None:
        for path in ("/alice.git/git-upload-pack", "/alice.git/git-receive-pack"):
            status, body, _ = self.request(
                path,
                method="POST",
                headers={"Authorization": f"Bearer {self.bob_token}"},
                data=b"0000",
            )
            self.assertEqual(status, 404, path)
            self.assertEqual(body, b"not found")

    def test_authorized_advertisements_succeed(self) -> None:
        for service in ("git-upload-pack", "git-receive-pack"):
            status, body, headers = self.request(
                f"/alice.git/info/refs?service={service}",
                headers={"Authorization": f"Bearer {self.alice_token}"},
            )
            self.assertEqual(status, 200, service)
            self.assertEqual(headers["Content-Type"], f"application/x-{service}-advertisement")
            self.assertIn(b"# service=", body)

    # -- protocol and route hardening ---------------------------------

    def test_dumb_protocol_probes_return_not_found(self) -> None:
        for path in (
            "/alice.git/HEAD",
            "/alice.git/objects/",
            "/alice.git/config",
            "/alice.git/info/refs",
            "/alice.git/info/refs?service=dumb",
            "/alice.git/info/alternates",
        ):
            status, _, _ = self.request(
                path, headers={"Authorization": f"Bearer {self.alice_token}"}
            )
            self.assertEqual(status, 404, path)

    def test_wrong_methods_rejected(self) -> None:
        status, _, _ = self.request(
            "/alice.git/info/refs?service=git-upload-pack",
            method="POST",
            headers={"Authorization": f"Bearer {self.alice_token}"},
            data=b"",
        )
        self.assertEqual(status, 405)
        status, _, _ = self.request(
            "/alice.git/git-upload-pack",
            headers={"Authorization": f"Bearer {self.alice_token}"},
        )
        self.assertEqual(status, 405)

    def test_traversal_and_malformed_routes_rejected(self) -> None:
        for path in (
            "/..%2f/alice.git/info/refs?service=git-upload-pack",
            "/alice.git%2f%2e%2e/info/refs?service=git-upload-pack",
            "/%2e%2e/canonical.git/info/refs?service=git-upload-pack",
            "/views/alice.git/info/refs?service=git-upload-pack",
            "/alice.git/../canonical.git/info/refs?service=git-upload-pack",
            "/alice.git/info/refs%00?service=git-upload-pack",
        ):
            status, _, _ = self.request(
                path, headers={"Authorization": f"Bearer {self.alice_token}"}
            )
            self.assertEqual(status, 404, path)

    # -- hidden object isolation --------------------------------------

    def test_hidden_blob_absent_from_view_store(self) -> None:
        result = repo_git(
            view_repo(self.forge, "alice"),
            "cat-file",
            "-e",
            self.hidden_blob,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_direct_oid_want_of_hidden_object_fails(self) -> None:
        clone = self.root / "want-clone"
        url = f"http://user:{self.alice_token}@127.0.0.1:{self.port}/alice.git"
        command("git", "clone", "-q", "--no-local", url, str(clone))
        result = git(clone, "fetch", "--no-tags", url, self.hidden_blob, check=False)
        self.assertNotEqual(result.returncode, 0)
        absent = repo_git(clone, "cat-file", "-e", self.hidden_blob, check=False)
        self.assertNotEqual(absent.returncode, 0)

    def test_direct_oid_want_of_canonical_commit_fails(self) -> None:
        canonical_tip = ref(canonical_repo(self.forge), "refs/heads/main")
        clone = self.root / "want-canonical"
        url = f"http://user:{self.alice_token}@127.0.0.1:{self.port}/alice.git"
        command("git", "clone", "-q", "--no-local", url, str(clone))
        result = git(clone, "fetch", "--no-tags", url, canonical_tip, check=False)
        self.assertNotEqual(result.returncode, 0)

    # -- state immutability -------------------------------------------

    def test_failed_requests_leave_forge_state_identical(self) -> None:
        before = self.forge_snapshot()
        attempts = [
            ("/alice.git/info/refs?service=git-upload-pack", "GET", {}, None),
            ("/alice.git/info/refs?service=git-upload-pack", "GET",
             {"Authorization": f"Bearer {self.bob_token}"}, None),
            ("/alice.git/git-receive-pack", "POST",
             {"Authorization": f"Bearer {self.bob_token}"}, b"0000"),
            ("/alice.git/HEAD", "GET",
             {"Authorization": f"Bearer {self.alice_token}"}, None),
            ("/alice.git/git-upload-pack", "POST",
             {"Authorization": f"Bearer {self.alice_token}"}, b"\xff\xff\xff\xff"),
        ]
        for path, method, headers, data in attempts:
            self.request(path, method=method, headers=headers, data=data)
        self.assertEqual(self.forge_snapshot(), before)


if __name__ == "__main__":
    unittest.main()
