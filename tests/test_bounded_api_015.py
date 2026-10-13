"""Spec 015 E1: bounded pagination and batched/targeted metadata access.

History pages visit only their bounded commit range with an
integrity-protected anchor; tree pages never enumerate unrelated
subtrees; Git metadata reads are batched per page, not one process per
entry; and the session response reads only the authenticated principal's
catalog provenance rows.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import api_v1
import forge
import store as store_module
from repository_server import create_app
from http_server import add_token
from state import load_state, recover
from tests import identity_harness
from tests.tree_harness import command, git


class BoundedApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        source = root / "source"
        command("git", "init", "-q", "-b", "main", str(source))
        (source / "visible").mkdir()
        (source / "hidden" / "deep").mkdir(parents=True)
        (source / "visible" / "a.txt").write_text("a\n")
        (source / "visible" / "b.txt").write_text("b\n")
        (source / "hidden" / "secret.txt").write_text("s\n")
        for index in range(40):
            (source / "hidden" / "deep" / f"n{index:02d}.txt").write_text("x\n")
        self.environment = dict(
            os.environ,
            GIT_AUTHOR_DATE="1700000000 +0000",
            GIT_COMMITTER_DATE="1700000000 +0000",
            GIT_AUTHOR_NAME="Fixture",
            GIT_AUTHOR_EMAIL="fixture@example.test",
            GIT_COMMITTER_NAME="Fixture",
            GIT_COMMITTER_EMAIL="fixture@example.test",
        )
        git(source, "add", "-A", env=self.environment)
        git(source, "commit", "-q", "-m", "initial", env=self.environment)
        for index in range(11):
            (source / "visible" / "a.txt").write_text(f"revision {index}\n")
            git(source, "add", "-A", env=self.environment)
            git(source, "commit", "-q", "-m", f"change {index}", env=self.environment)
        self.forge = root / "forge"
        forge.init_forge(self.forge, source, "main")
        identity_harness.add_view(self.forge, "alice", ["visible"], ["visible"], "main")
        identity_harness.add_view(
            self.forge, "nadia", ["visible/a.txt"], ["visible/a.txt"], "main"
        )
        recover(self.forge, load_state(self.forge))
        self.alice_token = add_token(self.forge, "alice")
        self.nadia_token = add_token(self.forge, "nadia")
        self.app = create_app(self.forge)
        self.client = self.app.test_client()

    def bearer(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def test_history_pages_visit_bounded_commit_ranges(self) -> None:
        seen: list[list[str]] = []
        real = api_v1.git_command

        def counting(repo, *args, **kwargs):
            if args and args[0] == "rev-list":
                seen.append(list(args))
            return real(repo, *args, **kwargs)

        with patch.object(api_v1, "git_command", counting):
            pages = []
            cursor = ""
            for _ in range(10):
                path = "/api/v1/history?limit=3" + (f"&cursor={cursor}" if cursor else "")
                response = self.client.get(path, headers=self.bearer(self.alice_token))
                self.assertEqual(response.status_code, 200)
                data = response.get_json()
                pages.append(data)
                cursor = data["next_cursor"] or ""
                if not data["next_cursor"]:
                    break
        # Every walk is bounded: -n with at most limit+1 commits and a
        # concrete start (HEAD for the first page, the anchor for later
        # pages); no unbounded full-history enumeration ever happens.
        self.assertLessEqual(len(seen), 4)
        for arguments in seen:
            self.assertIn("-n", arguments)
            self.assertIn("4", arguments)
        commits = [commit for page in pages for commit in page["commits"]]
        oids = [commit["oid"] for commit in commits]
        self.assertEqual(len(oids), len(set(oids)), "pages overlap")
        self.assertEqual(12, len(oids))

    def test_history_page_metadata_is_batched(self) -> None:
        calls: list[list[str]] = []
        real = api_v1.git_command

        def counting(repo, *args, **kwargs):
            calls.append(list(args))
            return real(repo, *args, **kwargs)

        with patch.object(api_v1, "git_command", counting):
            response = self.client.get(
                "/api/v1/history?limit=10", headers=self.bearer(self.alice_token)
            )
        self.assertEqual(response.status_code, 200)
        commits = response.get_json()["commits"]
        self.assertEqual(len(commits), 10)
        # One bounded rev-list walk plus one batched cat-file process; the
        # count is independent of the page size.
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "rev-list")
        self.assertIn("--batch", calls[1])

    def test_tree_page_sizes_are_batched(self) -> None:
        # Warm the view generation first so the measured request is a page
        # read, not a build.
        self.client.get("/api/v1/tree", headers=self.bearer(self.alice_token))
        prefix = base64.urlsafe_b64encode(b"visible").decode().rstrip("=")
        calls: list[list[str]] = []
        real = api_v1.git_command

        def counting(repo, *args, **kwargs):
            calls.append(list(args))
            return real(repo, *args, **kwargs)

        with patch.object(api_v1, "git_command", counting):
            response = self.client.get(
                f"/api/v1/tree?path_b64={prefix}",
                headers=self.bearer(self.alice_token),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()["entries"]), 2)
        batch_checks = [
            call
            for call in calls
            if any("--batch-check" in str(argument) for argument in call)
        ]
        self.assertEqual(len(batch_checks), 1)

    def test_tree_page_does_not_walk_unrelated_subtrees(self) -> None:
        # Warm the generation first: the measured request is a page read.
        self.client.get("/api/v1/tree", headers=self.bearer(self.alice_token))
        # The root page lists only "visible"; the hidden subtree with its
        # 40 descendants is never recursively enumerated as a whole, and
        # only page children get their own streamed visibility walk.
        import subprocess as sp

        # subprocess.run constructs a Popen internally, so tracking Popen
        # counts every Git process exactly once.
        walked: list[list[str]] = []
        real_popen = sp.Popen

        class TrackedPopen(real_popen):
            def __init__(self, args, *rest, **kwargs):
                if args and args[0] == "git" and "ls-tree" in args:
                    walked.append(
                        [
                            argument.decode("utf-8", "replace")
                            if isinstance(argument, bytes)
                            else argument
                            for argument in args
                        ]
                    )
                super().__init__(args, *rest, **kwargs)

        with patch.object(sp, "Popen", TrackedPopen):
            response = self.client.get(
                "/api/v1/tree", headers=self.bearer(self.alice_token)
            )
        self.assertEqual(response.status_code, 200)
        recursive = [
            call for call in walked if any(flag.startswith("-r") for flag in call)
        ]
        plain = [
            call
            for call in walked
            if not any(flag.startswith("-r") for flag in call)
        ]
        # One non-recursive page listing; per-child visibility walks touch
        # only page children (the early-exit streamed check).
        self.assertEqual(len(plain), 1)
        for call in recursive:
            self.assertIn("HEAD:visible", call)

    def test_stale_and_cross_view_cursors_rejected(self) -> None:
        first = self.client.get(
            "/api/v1/history?limit=2", headers=self.bearer(self.alice_token)
        )
        cursor = first.get_json()["next_cursor"]
        foreign = self.client.get(
            f"/api/v1/history?limit=2&cursor={cursor}",
            headers=self.bearer(self.nadia_token),
        )
        self.assertEqual(foreign.status_code, 400)
        tampered = cursor[:-2] + "zz"
        bad = self.client.get(
            f"/api/v1/history?limit=2&cursor={tampered}",
            headers=self.bearer(self.alice_token),
        )
        self.assertEqual(bad.status_code, 400)

    def test_session_provenance_reads_only_required_rows(self) -> None:
        # Unrelated catalog rows exist but must never be read for this
        # principal's response.
        from service import add_principal

        for index in range(8):
            add_principal(self.forge, f"unrelated-{index}")
        full_loads: list[list[str]] = []
        real_load_sections = store_module.Store.load_sections
        real_full = store_module.Store.load_document

        def counting_load_sections(self_store, sections):
            if sections and "principals" in sections:
                full_loads.append(list(sections))
            return real_load_sections(self_store, sections)

        def counting_full(self_store):
            full_loads.append(["FULL"])
            return real_full(self_store)

        with patch.object(store_module.Store, "load_sections", counting_load_sections), patch.object(
            store_module.Store, "load_document", counting_full
        ):
            response = self.client.get(
                "/api/v1/session", headers=self.bearer(self.alice_token)
            )
        self.assertEqual(response.status_code, 200)
        envelope = response.get_json()
        self.assertEqual(envelope["principal"]["id"], "alice")
        self.assertEqual(full_loads, [])
        filtered = store_module.open_store(self.forge)
        try:
            self.assertEqual(
                list(filtered.iter_section_where_field("role_bindings", "subject_id", "unrelated-0")),
                [],
            )
            self.assertEqual(
                list(filtered.iter_section_where_member("teams", "unrelated-0")),
                [],
            )
        finally:
            filtered.close()


if __name__ == "__main__":
    unittest.main()
