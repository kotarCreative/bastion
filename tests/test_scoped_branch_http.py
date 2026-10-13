"""HTTP browser metadata behavior when v4 denies the canonical default ref."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import unittest

import forge
from http_server import add_token
from identity import empty_capability_map
from permissions import empty_permissions
from repository import Repository
from repository_server import make_server
from service import add_role, bind_role, add_view, transition_principal


class ScopedBranchHttpTest(unittest.TestCase):
    """Exercise v4 browser/session endpoints over a bound HTTP server."""

    def setUp(self) -> None:
        from tests.test_repository_server import DemoServerTest

        DemoServerTest.setUp(self)
        self._demo = DemoServerTest

    def tearDown(self) -> None:
        self._demo.tearDown(self)

    def _owner_with_pr_only_v4(self) -> tuple[str, str]:
        from service import add_principal

        add_principal(self.forge, "metadata-owner", ["metadata@example.test"])
        allow = empty_capability_map()
        allow["read"] = [""]
        allow["admin"] = [""]
        add_role(self.forge, "metadata-owner-paths", allow, empty_capability_map())
        bind_role(self.forge, "metadata-owner-paths", "principal", "metadata-owner")
        add_view(self.forge, "metadata-owner", "metadata-owner", "main")

        policy = empty_permissions()
        policy["repository"]["allow"] = ["pr_view"]
        add_role(
            self.forge, "metadata-owner-pr-only", empty_capability_map(),
            empty_capability_map(), permissions=policy,
        )
        bind_role(self.forge, "metadata-owner-pr-only", "principal", "metadata-owner")
        transition_principal(self.forge, "metadata-owner")
        return add_token(self.forge, "metadata-owner", ["ui"]), policy

    def _get(self, base: str, route: str, token: str):
        request = urllib.request.Request(
            base + route, headers={"Authorization": f"Bearer {token}"}
        )
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        with response:
            return response.status, response.read()

    def test_pr_only_v4_session_survives_empty_default_denied_projection(self) -> None:
        token, policy = self._owner_with_pr_only_v4()
        manifest, _version, _replay = Repository.open(self.forge).load()
        default_tip = manifest.canonical_tip
        server = make_server(self.forge, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        status, raw = self._get(base, "/api/v1/session", token)
        self.assertEqual(status, 200, raw)
        session = json.loads(raw)
        self.assertEqual(session["descriptor"]["permissions"], policy)
        self.assertIsNone(session["descriptor"]["projected_ref"])
        self.assertTrue(
            json.loads(self._post_check(base, token))["allowed"],
            "PR policy remains usable without any branch read grants",
        )

        # No browser endpoint may fall back to canonical HEAD in this empty
        # projection. The path-admin metadata endpoint is still usable but
        # contains neither canonical refs nor object IDs.
        for route in ("/api/v1/tree", "/api/v1/history"):
            status, body = self._get(base, route, token)
            self.assertEqual(status, 404, body)
            self.assertNotIn(default_tip.encode(), body)
            self.assertNotIn(b"refs/heads/main", body)

        status, raw_permissions = self._get(base, "/api/v1/permissions", token)
        self.assertEqual(status, 200, raw_permissions)
        self.assertNotIn(default_tip.encode(), raw_permissions)
        self.assertNotIn(b"refs/heads/main", raw_permissions)

    def _post_check(self, base: str, token: str) -> bytes:
        request = urllib.request.Request(
            base + "/api/v1/self/permissions/check",
            data=b'{"scope":"repository","action":"pr_view"}',
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read()
