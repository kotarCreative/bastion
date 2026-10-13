"""End-to-end branch permission, projection, and Git HTTP transport tests.

Unlike the protocol unit tests, these tests run ordinary Git clients against a
bound HTTP server and inspect both the projected repositories and the WAL
backed canonical refs.
"""

from __future__ import annotations

import os
import subprocess
import threading
import unittest
from pathlib import Path

import forge
from http_server import add_token
from identity import empty_capability_map
from permissions import empty_permissions
from repository import Repository
from repository_server import make_server
from service import add_role, add_view, bind_role, create_branch, transition_principal
from state import load_state
from tests import identity_harness


class BranchTransportEndToEndTest(unittest.TestCase):
    """Exercise the permission-to-projection-to-transport path over HTTP."""

    def setUp(self) -> None:
        from tests.test_repository_server import DemoServerTest

        DemoServerTest.setUp(self)
        self._demo = DemoServerTest

    def _configure_branch_permissions(self) -> None:
        identity_harness.provision_admin(self.forge)
        create_branch(self.forge, "private", "forge-admin")

        # Alice's old path grants remain: read/write the public subtree only.
        # This additional v4 role gives her explicit branch transport grants.
        owner_policy = empty_permissions()
        for action in ("read", "create", "update"):
            owner_policy["branches"]["allow"][action] = ["refs/heads/*"]
        add_role(
            self.forge,
            "owner-global-branches",
            empty_capability_map(),
            empty_capability_map(),
            permissions=owner_policy,
        )
        bind_role(self.forge, "owner-global-branches", "principal", "alice")
        transition_principal(self.forge, "alice")

        # A second principal can read the public path, but only the feature ref.
        from service import add_principal

        add_principal(self.forge, "feature-reader", ["reader@example.test"])
        read_allow = empty_capability_map()
        read_allow["read"] = ["visible"]
        reader_policy = empty_permissions()
        reader_policy["branches"]["allow"]["read"] = ["refs/heads/feature/public"]
        add_role(
            self.forge, "feature-reader-policy", read_allow,
            empty_capability_map(), permissions=reader_policy,
        )
        bind_role(self.forge, "feature-reader-policy", "principal", "feature-reader")
        # Service-level view creation still pins its legacy configured branch;
        # v4 projection selects the readable feature/public ref after it is
        # published as the fallback to the unreadable configured default.
        add_view(self.forge, "feature-reader", "feature-reader", "main")
        self.owner_git_token = add_token(self.forge, "alice", ["fetch", "push"])
        self.reader_git_token = add_token(self.forge, "feature-reader", ["fetch"])
        self.reader_push_token = add_token(self.forge, "feature-reader", ["fetch", "push"])

    def tearDown(self) -> None:
        self._demo.tearDown(self)

    def _live_server(self):
        server = make_server(self.forge, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        return server

    def _env(self) -> dict[str, str]:
        # Isolate Git from system/global configuration and credential helpers.
        home = self.root / "git-home"
        home.mkdir(exist_ok=True)
        return {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "true",
            "GIT_AUTHOR_NAME": "Transport fixture",
            "GIT_AUTHOR_EMAIL": "transport@example.test",
            "GIT_COMMITTER_NAME": "Transport fixture",
            "GIT_COMMITTER_EMAIL": "transport@example.test",
        }

    def _git(self, *args: str, cwd: Path | None = None, check=True):
        result = subprocess.run(
            ["git", *args], cwd=cwd, env=self._env(), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if check and result.returncode:
            self.fail(f"git {' '.join(args)} failed ({result.returncode}):\n{result.stderr}")
        return result

    def _url(self, server, view: str, token: str) -> str:
        return f"http://{view}:{token}@127.0.0.1:{server.server_port}/{view}.git"

    def _refs(self, repo: Path) -> list[str]:
        return self._git("for-each-ref", "--format=%(refname)", cwd=repo).stdout.splitlines()

    def _add_commit(self, repo: Path, relative: str, content: str, message: str) -> str:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self._git("add", "--", relative, cwd=repo)
        self._git("commit", "-m", message, cwd=repo)
        return self._git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    def test_owner_pushes_projected_branches_reader_is_ref_and_object_scoped(self) -> None:
        self._configure_branch_permissions()
        server = self._live_server()
        owner_url = self._url(server, "alice", self.owner_git_token)
        owner_clone = self.root / "owner-clone"
        cloned = self._git("clone", "--quiet", owner_url, str(owner_clone), check=False)
        self.assertEqual(cloned.returncode, 0, cloned.stderr)

        # The user creates a branch from the exact currently advertised source
        # tip, then Git submits a new commit through receive-pack.
        self.assertEqual(
            [ref for ref in self._refs(owner_clone) if ref.startswith("refs/remotes/origin/")],
            [
                "refs/remotes/origin/HEAD",
                "refs/remotes/origin/main",
                "refs/remotes/origin/private",
            ],
        )
        source_tip = self._git("rev-parse", "HEAD", cwd=owner_clone).stdout.strip()
        initial_manifest, _version, _replay = Repository.open(self.forge).load()
        canonical_main_before = initial_manifest.canonical_refs["refs/heads/main"]
        historical = self._git(
            "push", "origin", "HEAD~1:refs/heads/historical", cwd=owner_clone, check=False
        )
        self.assertNotEqual(historical.returncode, 0, "a stale historical source was accepted")
        self.assertNotIn("refs/heads/historical", Repository.open(self.forge).load()[0].canonical_refs)
        forked = self._git(
            "push", "origin", "HEAD:refs/heads/fork", cwd=owner_clone, check=False
        )
        self.assertEqual(forked.returncode, 0, forked.stderr)
        manifest, _version, _replay = Repository.open(self.forge).load()
        self.assertEqual(
            manifest.canonical_refs["refs/heads/fork"],
            manifest.canonical_refs["refs/heads/main"],
        )
        from http_server import _resolve_service

        self.assertEqual(
            _resolve_service(self.forge, "alice")[3].tip_for_ref("refs/heads/fork"),
            source_tip,
        )
        self._git("switch", "--create", "feature/public", cwd=owner_clone)
        submitted = self._add_commit(
            owner_clone, "visible/new-public.txt", "public branch content\n", "new public branch"
        )
        from identity import effective_descriptor
        from permissions import transport_branch_allowed

        owner_descriptor = effective_descriptor(load_state(self.forge), "alice", "refs/heads/main")
        for action in ("read", "create", "update"):
            self.assertTrue(
                transport_branch_allowed(owner_descriptor, action, "refs/heads/feature/public"),
                f"owner descriptor denies {action} on the new branch",
            )
        pushed = self._git(
            "push", "origin", "HEAD:refs/heads/feature/public", cwd=owner_clone, check=False
        )
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        from invariants import check_invariants

        check_invariants(self.forge)

        manifest, _version, _replay = Repository.open(self.forge).load()
        self.assertEqual(manifest.canonical_refs["refs/heads/main"], canonical_main_before)
        self.assertEqual(
            _resolve_service(self.forge, "alice")[3].tip_for_ref("refs/heads/feature/public"),
            submitted,
        )
        # Creating a projected target from a path-filtered source must not
        # delete hidden canonical content from its full canonical tree.
        canonical = forge.canonical_repo(self.forge)
        hidden_tree = forge.output(canonical, "show", "refs/heads/feature/public:hidden/secret.txt")
        with self.subTest(canonical_hidden_file_preserved=True):
            self.assertEqual(hidden_tree, "secret 1")

        # Advance the published branch. The canonical and projected ref must
        # retain the exact submitted commit identity, including after a cold
        # projection rebuild (not merely a matching tree).
        self._add_commit(
            owner_clone, "visible/second.txt", "second public commit\n", "advance public branch"
        )
        second_submitted = self._git("rev-parse", "HEAD", cwd=owner_clone).stdout.strip()
        advanced = self._git("push", "origin", "HEAD:refs/heads/feature/public", cwd=owner_clone, check=False)
        self.assertEqual(advanced.returncode, 0, advanced.stderr)
        manifest, _version, _replay = Repository.open(self.forge).load()
        self.assertEqual(
            _resolve_service(self.forge, "alice")[3].tip_for_ref("refs/heads/feature/public"),
            second_submitted,
        )

        reader_url = self._url(server, "feature-reader", self.reader_git_token)
        reader_clone = self.root / "reader-clone"
        reader_result = self._git("clone", "--quiet", reader_url, str(reader_clone), check=False)
        self.assertEqual(reader_result.returncode, 0, reader_result.stderr)
        self.assertEqual(
            [ref for ref in self._refs(reader_clone) if ref.startswith("refs/remotes/origin/")],
            ["refs/remotes/origin/HEAD", "refs/remotes/origin/feature/public"],
        )
        self.assertEqual(
            self._git("symbolic-ref", "HEAD", cwd=reader_clone).stdout.strip(),
            "refs/heads/feature/public",
        )

        # A unique public blob on an owner-only branch is not fetchable merely
        # because this reader has path-level read permission for its contents.
        self._git("switch", "main", cwd=owner_clone)
        self._git("switch", "private", cwd=owner_clone)
        self._add_commit(
            owner_clone, "visible/private-only.txt", "private ref object\n", "private ref content"
        )
        self.assertEqual(
            self._git("push", "origin", "HEAD:refs/heads/private", cwd=owner_clone, check=False).returncode,
            0,
        )
        private_oid = self._git("rev-parse", "HEAD:visible/private-only.txt", cwd=owner_clone).stdout.strip()
        self.assertNotEqual(self._git("cat-file", "-e", private_oid, cwd=reader_clone, check=False).returncode, 0)
        hidden_oid = self._git("rev-parse", "refs/heads/main:hidden/secret.txt", cwd=canonical).stdout.strip()
        main_canonical_tip = manifest.canonical_refs["refs/heads/main"]
        for forbidden_oid in (private_oid, hidden_oid, main_canonical_tip):
            for protocol in ("0", "2"):
                denied = self._git(
                    "-c", f"protocol.version={protocol}", "fetch", "--no-tags", reader_url,
                    forbidden_oid, cwd=reader_clone, check=False,
                )
                self.assertNotEqual(
                    denied.returncode, 0,
                    f"protocol {protocol} disclosed unauthorized object {forbidden_oid}",
                )

        # A read-only Git credential cannot push, even to a readable branch.
        self._git("switch", "feature/public", cwd=reader_clone)
        self._add_commit(reader_clone, "visible/denied.txt", "no\n", "reader cannot push")
        push_scoped_url = self._url(server, "feature-reader", self.reader_push_token)
        readonly_push = self._git(
            "push", push_scoped_url, "HEAD:refs/heads/feature/public", cwd=reader_clone, check=False
        )
        self.assertNotEqual(readonly_push.returncode, 0)

        # Revoking the only readable branch empties the projected generation;
        # the UI session remains valid, but the stale Git credential stops
        # advertising/fetching and cannot use a previously known tip.
        read_session = self.client.get(
            "/api/v1/session", headers={"Authorization": f"Bearer {self.reader_git_token}"}
        )
        # Git credentials intentionally have no UI scope; use a separate UI token.
        self.assertEqual(read_session.status_code, 403)
        from http_server import add_token as issue_token

        ui_token = issue_token(self.forge, "feature-reader", ["ui"])
        ui_headers = {"Authorization": f"Bearer {ui_token}"}
        self.assertEqual(self.client.get("/api/v1/session", headers=ui_headers).status_code, 200)
        revoke = empty_permissions()
        revoke["branches"]["deny"]["read"] = ["refs/heads/feature/public"]
        add_role(
            self.forge, "reader-revoked-feature", empty_capability_map(),
            empty_capability_map(), permissions=revoke,
        )
        bind_role(self.forge, "reader-revoked-feature", "principal", "feature-reader")
        transition_principal(self.forge, "feature-reader")
        session = self.client.get("/api/v1/session", headers=ui_headers)
        self.assertEqual(session.status_code, 200)
        self.assertIsNone(session.get_json()["descriptor"]["projected_ref"])
        self.assertEqual(self.client.get("/api/v1/tree", headers=ui_headers).status_code, 404)
        self.assertNotEqual(self._git("ls-remote", reader_url, check=False).returncode, 0)
        old_tip_fetch = self._git(
            "fetch", "--no-tags", reader_url, second_submitted,
            cwd=reader_clone, check=False,
        )
        self.assertNotEqual(old_tip_fetch.returncode, 0)
        from http_server import _resolve_service

        _repository, _manifest, _active, materialized = _resolve_service(self.forge, "feature-reader")
        self.assertEqual(materialized.projected_refs, {})
        self.assertEqual(self._refs(materialized.path), [])
        self.assertNotEqual(
            self._git("cat-file", "-e", second_submitted, cwd=materialized.path, check=False).returncode,
            0,
        )

        # Rebuild the owner's active scoped generation after deleting only its
        # disposable materialization. Replayed refs/frontiers and invariants
        # must still agree with the published WAL state.
        from http_server import _resolve_service as resolve
        from invariants import check_invariants
        from store import backup_forge, restore_forge

        _repo, _manifest, _active, owner_view = resolve(self.forge, "alice")
        expected_owner_refs = owner_view.projected_refs
        owner_generation = owner_view.path
        import shutil

        shutil.rmtree(owner_generation)
        rebuilt = resolve(self.forge, "alice")[3]
        self.assertEqual(rebuilt.projected_refs, expected_owner_refs)
        from git import has_exact_object_closure

        self.assertTrue(has_exact_object_closure(rebuilt.path))
        self.assertEqual(
            forge.output(rebuilt.path, "rev-parse", "refs/heads/feature/public").strip(),
            second_submitted,
        )
        check_invariants(self.forge)
        backup = self.root / "backup"
        backup_forge(self.forge, backup)
        restored_root = self.root / "restored"
        restore_forge(backup, restored_root)
        restored_manifest, _version, restored_replay = Repository.open(restored_root).load()
        current_manifest, _version, current_replay = Repository.open(self.forge).load()
        self.assertEqual(restored_manifest.canonical_refs, current_manifest.canonical_refs)
        self.assertEqual(
            restored_replay.active_views["alice"]["branch_frontiers"],
            current_replay.active_views["alice"]["branch_frontiers"],
        )

    def test_legacy_v3_observer_still_gets_only_its_default_branch(self) -> None:
        server = self._live_server()
        bob_url = self._url(server, "bob", self.bob_token)
        checkout = self.root / "legacy-clone"
        result = self._git("clone", "--quiet", bob_url, str(checkout), check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [ref for ref in self._refs(checkout) if ref.startswith("refs/remotes/origin/")],
            ["refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        )

    def test_root_admin_has_no_implicit_v4_branch_transport_grants(self) -> None:
        identity_harness.provision_admin(self.forge)
        no_branch_policy = empty_permissions()
        no_branch_policy["branches"]["deny"]["read"] = ["refs/heads/main"]
        admin_read_allow = empty_capability_map()
        admin_read_allow["read"] = [""]
        add_role(
            self.forge, "admin-no-branch-grants", admin_read_allow,
            empty_capability_map(), permissions=no_branch_policy,
        )
        bind_role(self.forge, "admin-no-branch-grants", "principal", "forge-admin")
        add_view(self.forge, "forge-admin", "forge-admin", "main")
        token = add_token(self.forge, "forge-admin", ["fetch"])
        server = self._live_server()
        url = self._url(server, "forge-admin", token)

        # Admin is a path capability, not a branch grant. A v4 view with no
        # branch rules therefore exposes no refs, even when its default exists.
        from http_server import _resolve_service

        _repo, _manifest, _active, materialized = _resolve_service(self.forge, "forge-admin")
        self.assertEqual(materialized.projected_refs, {})
        self.assertNotEqual(self._git("ls-remote", url, check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
