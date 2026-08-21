from __future__ import annotations

import base64
import gzip
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

import forge
from errors import ForgeError
from http_server import (
    add_token,
    decode_body,
    load_tokens,
    parse_authorization,
    parse_route,
    parse_service,
)


def command(*args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, check=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


class RouteTest(unittest.TestCase):
    def valid(self, path: str, view: str, target: str) -> None:
        self.assertEqual(parse_route(path), (view, target))

    def invalid(self, path: str) -> None:
        self.assertIsNone(parse_route(path))

    def test_valid_routes(self) -> None:
        self.valid("/alice.git/info/refs", "alice", "info/refs")
        self.valid("/alice.git/git-upload-pack", "alice", "git-upload-pack")
        self.valid("/bob.git/git-receive-pack", "bob", "git-receive-pack")
        self.valid("/team.a_x-1.git/info/refs", "team.a_x-1", "info/refs")
        self.valid("/ALICE.git/info/refs", "ALICE", "info/refs")
        self.valid("/a.git.git/info/refs", "a.git", "info/refs")
        self.valid(f"/{'b' * 64}.git/git-upload-pack", "b" * 64, "git-upload-pack")

    def test_invalid_routes(self) -> None:
        for path in (
            "",
            "/",
            "/alice.git",
            "/alice.git/",
            "/alice.git/HEAD",
            "/alice.git/info/refs/x",
            "//alice.git/info/refs",
            "/views/alice.git/info/refs",
            "/../x.git/info/refs",
            "/./x.git/info/refs",
            "/-lead.git/info/refs",
            "/.dot.git/info/refs",
            "/a b.git/info/refs",
            "/a$ign.git/info/refs",
            f"/{'b' * 65}.git/info/refs",
            "/alice.git/info",
            "/alice.git/GIT-UPLOAD-PACK",
        ):
            self.invalid(path)

    def test_encoded_traversal_decodes_to_rejected_path(self) -> None:
        for raw in (
            "/%2e%2e/x.git/info/refs",
            "/x.git%2f%2e%2e/info/refs",
            "/%2f%2fetc.git/info/refs",
            "/x%00.git/info/refs",
        ):
            self.invalid(unquote(raw))


class ServiceTest(unittest.TestCase):
    def test_valid_services(self) -> None:
        self.assertEqual(parse_service("service=git-upload-pack"), "git-upload-pack")
        self.assertEqual(parse_service("service=git-receive-pack"), "git-receive-pack")
        self.assertEqual(
            parse_service("service=git-upload-pack&client=git/2.42"),
            "git-upload-pack",
        )

    def test_invalid_services(self) -> None:
        for query in (
            "",
            "service=",
            "service=dumb",
            "service=git-upload-pack&service=git-receive-pack",
            "foo=bar",
            "SERVICE=git-upload-pack",
        ):
            self.assertIsNone(parse_service(query))


class AuthorizationTest(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(parse_authorization(basic("user", "token")), "token")
        self.assertEqual(parse_authorization(basic("", "token")), "token")
        self.assertEqual(parse_authorization(basic("us er", "to:ken")), "to:ken")

    def test_bearer(self) -> None:
        self.assertEqual(parse_authorization("Bearer token"), "token")
        self.assertEqual(parse_authorization("bearer token"), "token")

    def test_invalid(self) -> None:
        for header in (
            None,
            "",
            "Basic",
            "Basic ",
            "Basic !!!",
            "Basic " + base64.b64encode(b"user").decode(),
            "Basic " + base64.b64encode(b"user:").decode(),
            "Bearer ",
            "Digest zzz",
        ):
            self.assertIsNone(parse_authorization(header))


class BodyTest(unittest.TestCase):
    def test_passthrough(self) -> None:
        self.assertEqual(decode_body(b"payload", None), b"payload")
        self.assertEqual(decode_body(b"payload", "identity"), b"payload")

    def test_gzip(self) -> None:
        compressed = gzip.compress(b"payload")
        self.assertEqual(decode_body(compressed, "gzip"), b"payload")
        self.assertEqual(decode_body(compressed, "GZIP"), b"payload")

    def test_invalid(self) -> None:
        from http_server import HttpError

        with self.assertRaises(HttpError) as raised:
            decode_body(b"not gzip", "gzip")
        self.assertEqual(raised.exception.status, 400)
        with self.assertRaises(HttpError):
            decode_body(b"payload", "br")


class TokenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        command("git", "init", "-q", "-b", "main", str(source))
        environment = dict(
            os.environ,
            GIT_AUTHOR_NAME="Fixture",
            GIT_AUTHOR_EMAIL="fixture@example.test",
            GIT_COMMITTER_NAME="Fixture",
            GIT_COMMITTER_EMAIL="fixture@example.test",
        )
        command("git", "-C", str(source), "commit", "-q", "--allow-empty", "-m", "initial", env=environment)
        forge.init_forge(self.root / "forge", source, "main")
        forge.add_view(self.root / "forge", "alice", ["visible"], ["visible"], "main")
        self.forge = self.root / "forge"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_add_and_load(self) -> None:
        first = add_token(self.forge, "alice")
        second = add_token(self.forge, "alice")
        self.assertNotEqual(first, second)
        tokens = load_tokens(self.forge)
        self.assertEqual(tokens, {first: "alice", second: "alice"})
        data = json.loads((self.forge / "tokens.json").read_text())
        self.assertEqual(data, {first: "alice", second: "alice"})

    def test_unknown_view_rejected_without_state_change(self) -> None:
        with self.assertRaises(ForgeError):
            add_token(self.forge, "carol")
        self.assertFalse((self.forge / "tokens.json").exists())

    def test_revocation_takes_effect(self) -> None:
        token = add_token(self.forge, "alice")
        (self.forge / "tokens.json").unlink()
        self.assertEqual(load_tokens(self.forge), {})


if __name__ == "__main__":
    unittest.main()
