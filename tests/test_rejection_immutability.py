"""Systematic verification that rejected operations leave authoritative state unchanged.

This module covers the roadmap item:

> Verify every rejected operation leaves authoritative refs and mappings unchanged.

For each rejection category the harness captures the authoritative snapshot
before the operation (``state.json`` bytes, all canonical and view refs, receiver
markers, logical changes, and mappings) and requires it to be identical after
the rejection.  Every rejected stable checkpoint must also pass the global
invariant checker.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import forge
from tests import identity_harness
from errors import ForgeError
from state import locked
from tests.tree_harness import TreeFixture, build_commit, clean_environment, command, empty_subtree_commit, git, gitlink_commit, oid, tree_at


def _make_commit(repo: Path, parent: str, effects: dict[bytes, tuple[bytes, bytes] | None], message: bytes, timestamp: int) -> str:
    """Build a commit on ``parent`` without moving ``refs/heads/main``."""
    base = {path: (mode, contents) for path, (mode, _oid, contents) in tree_at(repo, parent).items()}
    for path, effect in effects.items():
        if effect is None:
            base.pop(path, None)
        else:
            base[path] = effect
    descriptor, raw = tempfile.mkstemp(prefix="commit-index-", dir=repo)
    os.close(descriptor)
    index = Path(raw)
    index.unlink()
    env = clean_environment() | {"GIT_INDEX_FILE": str(index)}
    metadata = clean_environment() | {
        "GIT_AUTHOR_NAME": "Rejection Test", "GIT_AUTHOR_EMAIL": "rejection@example.test",
        "GIT_AUTHOR_DATE": f"{timestamp} +0000",
        "GIT_COMMITTER_NAME": "Rejection Test", "GIT_COMMITTER_EMAIL": "rejection@example.test",
        "GIT_COMMITTER_DATE": f"{timestamp} +0000",
    }
    try:
        git(repo, "read-tree", "--empty", env=env)
        records: list[bytes] = []
        for path, (mode, contents) in sorted(base.items()):
            object_id = git(repo, "hash-object", "-w", "--stdin", data=contents).stdout.strip()
            records.append(mode + b" " + object_id + b"\t" + path + b"\0")
        if records:
            git(repo, "update-index", "-z", "--index-info", data=b"".join(records), env=env)
            tree = git(repo, "write-tree", env=env).stdout.decode("ascii").strip()
        else:
            tree = git(repo, "hash-object", "-w", "-t", "tree", "--stdin", data=b"", env=env).stdout.decode("ascii").strip()
        args = ["commit-tree", tree, "-p", parent]
        return git(repo, *args, data=message, env=metadata).stdout.decode("ascii").strip()
    finally:
        index.unlink(missing_ok=True)


class RejectionImmutabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def _reject_push(self, view: str, build: callable, category: bytes) -> None:
        """Build a rejected commit, push it, and assert the snapshot is unchanged."""
        client = self.fixture.clients[view]
        command("git", "-C", os.fsdecode(client), "fetch", "-q", "origin")
        git(client, "update-ref", "refs/heads/main", oid(client, "refs/remotes/origin/main"))
        build(client)
        before = self.fixture.rejection_snapshot()
        result = command("git", "-C", os.fsdecode(client), "push", "origin", "main", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(category, result.stderr)
        self.fixture.assert_rejection(before)

    def test_force_push_rejected(self) -> None:
        """A non-ancestor update is rejected and leaves no authoritative trace."""
        old, _current = self.fixture.push_source("alice", {b"shared/base.txt": (b"100644", b"current\n")})
        client = self.fixture.clients["alice"]
        force_commit = _make_commit(client, old, {b"shared/base.txt": (b"100644", b"forced\n")}, b"force\n", 1_700_000_100)
        git(client, "update-ref", "refs/heads/main", force_commit)
        before = self.fixture.rejection_snapshot()
        result = command("git", "-C", os.fsdecode(client), "push", "origin", "main", "--force", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertTrue(
            b"force updates are not supported" in result.stderr
            or b"non-fast-forward" in result.stderr
        )
        self.fixture.assert_rejection(before)

    def test_multiple_ref_updates_rejected(self) -> None:
        """A push updating more than one ref is rejected."""
        client = self.fixture.clients["alice"]
        current = oid(client, "refs/heads/main")
        side = build_commit(client, current, {b"shared/side.txt": (b"100644", b"side\n")}, b"side\n", 1_700_000_100)
        before = self.fixture.rejection_snapshot()
        result = command(
            "git", "-C", os.fsdecode(client), "push", "origin",
            f"{side}:refs/heads/main", f"{side}:refs/heads/other",
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn(b"exactly one authorized branch", result.stderr)
        self.fixture.assert_rejection(before)

    def test_branch_deletion_rejected(self) -> None:
        """Deleting the configured branch is rejected."""
        client = self.fixture.clients["alice"]
        before = self.fixture.rejection_snapshot()
        result = command(
            "git", "-C", os.fsdecode(client), "push", "origin", ":main", check=False
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn(b"branch creation, deletion, and stale or force updates are not supported", result.stderr)
        self.fixture.assert_rejection(before)

    def test_stale_update_rejected(self) -> None:
        """Pushing a commit based on an old tip is rejected."""
        self.fixture.push_source("alice", {b"shared/base.txt": (b"100644", b"first\n")})
        client = self.fixture.clients["bob"]
        old_local = oid(client, "refs/heads/main")
        stale = _make_commit(client, old_local, {b"shared/base.txt": (b"100644", b"stale\n")}, b"stale\n", 1_700_000_100)
        git(client, "update-ref", "refs/heads/main", stale)
        command("git", "-C", os.fsdecode(client), "fetch", "-q", "origin")
        before = self.fixture.rejection_snapshot()
        result = command("git", "-C", os.fsdecode(client), "push", "origin", "main", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertTrue(
            b"fetch first" in result.stderr
            or b"non-fast-forward" in result.stderr
            or b"stale or force updates" in result.stderr
        )
        self.fixture.assert_rejection(before)

    def test_merge_commit_rejected(self) -> None:
        """A client-created merge commit is rejected."""
        client = self.fixture.clients["alice"]
        parent = oid(client, "refs/heads/main")
        side = _make_commit(client, parent, {b"shared/side.txt": (b"100644", b"side\n")}, b"side\n", 1_700_000_100)
        main = _make_commit(client, parent, {b"shared/main.txt": (b"100644", b"main\n")}, b"main\n", 1_700_000_101)
        tree = git(client, "rev-parse", f"{main}^{{tree}}").stdout.decode("ascii").strip()
        env = clean_environment() | {
            "GIT_AUTHOR_NAME": "Alice", "GIT_AUTHOR_EMAIL": "alice@example.test",
            "GIT_AUTHOR_DATE": "1700000100 +0000",
            "GIT_COMMITTER_NAME": "Alice", "GIT_COMMITTER_EMAIL": "alice@example.test",
            "GIT_COMMITTER_DATE": "1700000100 +0000",
        }
        merge = git(client, "commit-tree", tree, "-p", main, "-p", side, data=b"merge\n", env=env).stdout.decode("ascii").strip()
        git(client, "update-ref", "refs/heads/main", merge)
        parents = git(client, "show", "-s", "--format=%P", merge).stdout.decode("ascii").strip().split()
        self.assertEqual(2, len(parents))
        before = self.fixture.rejection_snapshot()
        result = command("git", "-C", os.fsdecode(client), "push", "origin", "main", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertTrue(
            b"merge commits" in result.stderr
            or b"submitted history must be a linear chain" in result.stderr
        )
        self.fixture.assert_rejection(before)

    def test_non_linear_submitted_history_rejected(self) -> None:
        """Submitted commits that do not form a linear chain are rejected."""
        _old, _current = self.fixture.push_source("alice", {b"shared/base.txt": (b"100644", b"current\n")})
        client = self.fixture.clients["alice"]
        initial = git(client, "rev-list", "--max-parents=0", "HEAD").stdout.decode("ascii").strip()
        # Build a commit on the initial commit while the remote tip has moved forward.
        offshoot = _make_commit(client, initial, {b"shared/offshoot.txt": (b"100644", b"offshoot\n")}, b"offshoot\n", 1_700_000_100)
        git(client, "update-ref", "refs/heads/main", offshoot)
        before = self.fixture.rejection_snapshot()
        result = command("git", "-C", os.fsdecode(client), "push", "origin", "main", "--force", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(b"force updates are not supported", result.stderr)
        self.fixture.assert_rejection(before)

    def test_unauthorized_path_rejected(self) -> None:
        """A path outside read/write authorization is rejected."""
        self._reject_push(
            "limited",
            lambda client: build_commit(client, oid(client, "refs/heads/main"), {b"team-a/private.txt": (b"100644", b"injected\n")}, b"unauthorized\n", 1_700_000_100),
            b"outside current authorization",
        )

    def test_unsupported_entry_rejected(self) -> None:
        """A gitlink in a source push is rejected."""
        client = self.fixture.clients["alice"]
        parent = oid(client, "refs/heads/main")
        gitlink_commit(client, parent, 1_700_000_100)
        before = self.fixture.rejection_snapshot()
        result = command("git", "-C", os.fsdecode(client), "push", "origin", "main", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(b"unsupported entry type or mode", result.stderr)
        self.fixture.assert_rejection(before)

    def test_empty_subtree_rejected(self) -> None:
        """An explicit empty subtree in a source push is rejected."""
        client = self.fixture.clients["alice"]
        parent = oid(client, "refs/heads/main")
        empty_subtree_commit(client, parent, 1_700_000_100)
        before = self.fixture.rejection_snapshot()
        result = command("git", "-C", os.fsdecode(client), "push", "origin", "main", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(b"canonical round-trip does not reproduce the submitted tree", result.stderr)
        self.fixture.assert_rejection(before)

    def test_hidden_ancestor_file_directory_conflict_rejected(self) -> None:
        """A hidden canonical file conflicting with a submitted descendant is rejected."""
        # Add a hidden file that will collide with a visible descendant path.
        self.fixture.admin_update([{b"container": (b"100644", b"hidden boundary\n")}])
        identity_harness.add_view(self.fixture.root, "narrow", ["container/visible"], ["container/visible"], "main")
        checkout = self.fixture.directory / "client-narrow"
        command("git", "clone", "-q", "--no-local", os.fsdecode(forge.view_repo(self.fixture.root, "narrow")), os.fsdecode(checkout))
        self.fixture.policies["narrow"] = (["container/visible"], ["container/visible"])
        self.fixture.clients["narrow"] = checkout
        self._reject_push(
            "narrow",
            lambda client: build_commit(
                client,
                oid(client, "refs/heads/main"),
                {b"container/visible/child": (b"100644", b"child\n")},
                b"conflict\n",
                1_700_000_100,
            ),
            b"file/directory authorization boundary",
        )

    def test_duplicate_change_id_by_other_principal_rejected(self) -> None:
        """Reusing another view's Change-Id is rejected and leaves ownership unchanged."""
        change_id = "I1111111111111111111111111111111111111111"
        self.fixture.push_source("alice", {b"shared/owned.txt": (b"100644", b"owned\n")}, f"alice owns\n\nChange-Id: {change_id}\n".encode())
        canonical_before = oid(forge.canonical_repo(self.fixture.root), "refs/heads/main")

        client = self.fixture.clients["bob"]
        command("git", "-C", os.fsdecode(client), "fetch", "-q", "origin")
        git(client, "update-ref", "refs/heads/main", oid(client, "refs/remotes/origin/main"))
        build_commit(client, oid(client, "refs/heads/main"), {b"shared/claim.txt": (b"100644", b"claim\n")}, f"bob claims\n\nChange-Id: {change_id}\n".encode(), 1_700_000_100)
        before = self.fixture.rejection_snapshot()
        result = command("git", "-C", os.fsdecode(client), "push", "origin", "main", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(b"belongs to another principal", result.stderr)
        self.fixture.assert_rejection(before)
        self.assertEqual(canonical_before, oid(forge.canonical_repo(self.fixture.root), "refs/heads/main"))
        self.assertEqual("alice", self.fixture.state()["changes"][change_id]["owner_principal_id"])

    def test_administrative_stale_expected_oid_rejected(self) -> None:
        """An administrative update with a stale expected OID is rejected."""
        canonical = forge.canonical_repo(self.fixture.root)
        expected = oid(canonical, "refs/heads/main")
        git(self.fixture.admin, "update-ref", "refs/heads/main", expected)
        first = build_commit(self.fixture.admin, expected, {b"shared/admin.txt": (b"100644", b"first\n")}, b"first\n", 1_700_000_100)
        command("git", "-C", os.fsdecode(self.fixture.admin), "push", "origin", f"{first}:refs/staging/admin-first")
        forge.update_canonical_ref(self.fixture.root, expected, first)
        forge.drain_projection_jobs(self.fixture.root)
        second = build_commit(self.fixture.admin, first, {b"shared/admin.txt": (b"100644", b"second\n")}, b"second\n", 1_700_000_101)
        command("git", "-C", os.fsdecode(self.fixture.admin), "push", "origin", f"{second}:refs/staging/admin-second")
        before = self.fixture.rejection_snapshot()
        with self.assertRaisesRegex(ForgeError, "canonical ref changed before administrative update"):
            forge.update_canonical_ref(self.fixture.root, expected, second)
        self.fixture.assert_rejection(before)

    def test_administrative_non_fast_forward_rejected(self) -> None:
        """An administrative update that is not a fast-forward is rejected."""
        canonical = forge.canonical_repo(self.fixture.root)
        expected = oid(canonical, "refs/heads/main")
        git(self.fixture.admin, "update-ref", "refs/heads/main", expected)
        first = _make_commit(self.fixture.admin, expected, {b"shared/admin.txt": (b"100644", b"first\n")}, b"first\n", 1_700_000_100)
        second = _make_commit(self.fixture.admin, expected, {b"shared/admin.txt": (b"100644", b"second\n")}, b"second\n", 1_700_000_101)
        command("git", "-C", os.fsdecode(self.fixture.admin), "push", "origin", f"{first}:refs/staging/admin-first")
        command("git", "-C", os.fsdecode(self.fixture.admin), "push", "origin", f"{second}:refs/staging/admin-second")
        forge.update_canonical_ref(self.fixture.root, expected, first)
        forge.drain_projection_jobs(self.fixture.root)
        before = self.fixture.rejection_snapshot()
        with self.assertRaisesRegex(ForgeError, "canonical non-fast-forward updates are not supported"):
            forge.update_canonical_ref(self.fixture.root, first, second)
        self.fixture.assert_rejection(before)

    def test_administrative_merge_commit_rejected(self) -> None:
        """An administrative update containing a merge commit is rejected."""
        canonical = forge.canonical_repo(self.fixture.root)
        expected = oid(canonical, "refs/heads/main")
        git(self.fixture.admin, "update-ref", "refs/heads/main", expected)
        side = _make_commit(self.fixture.admin, expected, {b"shared/side.txt": (b"100644", b"side\n")}, b"side\n", 1_700_000_100)
        main = _make_commit(self.fixture.admin, expected, {b"shared/main.txt": (b"100644", b"main\n")}, b"main\n", 1_700_000_101)
        tree = git(self.fixture.admin, "rev-parse", f"{main}^{{tree}}").stdout.decode("ascii").strip()
        env = clean_environment() | {
            "GIT_AUTHOR_NAME": "Admin", "GIT_AUTHOR_EMAIL": "admin@example.test",
            "GIT_AUTHOR_DATE": "1700000100 +0000",
            "GIT_COMMITTER_NAME": "Admin", "GIT_COMMITTER_EMAIL": "admin@example.test",
            "GIT_COMMITTER_DATE": "1700000100 +0000",
        }
        merge = git(self.fixture.admin, "commit-tree", tree, "-p", main, "-p", side, data=b"merge\n", env=env).stdout.decode("ascii").strip()
        git(self.fixture.admin, "update-ref", "refs/heads/main", merge)
        command("git", "-C", os.fsdecode(self.fixture.admin), "push", "origin", f"{merge}:refs/staging/admin-merge")
        before = self.fixture.rejection_snapshot()
        with self.assertRaisesRegex(ForgeError, "merge commits are not supported"):
            forge.update_canonical_ref(self.fixture.root, expected, merge)
        self.fixture.assert_rejection(before)

    def test_administrative_unsupported_tree_entry_rejected(self) -> None:
        """An administrative update containing a gitlink is rejected."""
        canonical = forge.canonical_repo(self.fixture.root)
        expected = oid(canonical, "refs/heads/main")
        git(self.fixture.admin, "update-ref", "refs/heads/main", expected)
        bad = gitlink_commit(self.fixture.admin, expected, 1_700_000_100)
        command("git", "-C", os.fsdecode(self.fixture.admin), "push", "origin", f"{bad}:refs/staging/admin-bad")
        before = self.fixture.rejection_snapshot()
        with self.assertRaisesRegex(ForgeError, "unsupported tree entry"):
            forge.update_canonical_ref(self.fixture.root, expected, bad)
        self.fixture.assert_rejection(before)

    def test_administrative_empty_subtree_rejected(self) -> None:
        """An administrative update containing an explicit empty subtree is rejected."""
        canonical = forge.canonical_repo(self.fixture.root)
        expected = oid(canonical, "refs/heads/main")
        git(self.fixture.admin, "update-ref", "refs/heads/main", expected)
        bad = empty_subtree_commit(self.fixture.admin, expected, 1_700_000_100)
        command("git", "-C", os.fsdecode(self.fixture.admin), "push", "origin", f"{bad}:refs/staging/admin-empty")
        before = self.fixture.rejection_snapshot()
        with self.assertRaisesRegex(ForgeError, "explicit empty subtrees are not supported"):
            forge.update_canonical_ref(self.fixture.root, expected, bad)
        self.fixture.assert_rejection(before)


class SyncBarrierImmutabilityTest(unittest.TestCase):
    """Server sync rejections leave authoritative state unchanged."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        command("git", "init", "-q", "--object-format=sha1", "-b", "main", os.fsdecode(self.source))
        command("git", "-C", os.fsdecode(self.source), "config", "user.name", "Fixture")
        command("git", "-C", os.fsdecode(self.source), "config", "user.email", "fixture@example.test")
        (self.source / "shared").mkdir()
        (self.source / "shared" / "base.txt").write_text("shared\n")
        (self.source / "team-a").mkdir()
        (self.source / "team-a" / "private.txt").write_text("team a\n")
        (self.source / "team-b").mkdir()
        (self.source / "team-b" / "private.txt").write_text("team b\n")
        (self.source / "hidden").mkdir()
        (self.source / "hidden" / "secret.txt").write_text("secret\n")
        command("git", "-C", os.fsdecode(self.source), "add", ".")
        command("git", "-C", os.fsdecode(self.source), "commit", "-q", "-m", "initial", env={"GIT_AUTHOR_DATE": "1700000000 +0000", "GIT_COMMITTER_DATE": "1700000000 +0000"})
        forge.init_forge(self.root / "forge", self.source, "main")
        self.forge = self.root / "forge"
        identity_harness.add_view(self.forge, "alice", ["shared", "team-a"], ["shared", "team-a"], "main")
        identity_harness.add_view(self.forge, "bob", ["shared", "team-b"], ["shared", "team-b"], "main")
        self.alice_remote = forge.view_repo(self.forge, "alice")
        self.bob_remote = forge.view_repo(self.forge, "bob")
        self.alice = self._clone("alice", self.alice_remote)
        self.bob = self._clone("bob", self.bob_remote)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _clone(self, name: str, remote: Path) -> Path:
        checkout = self.root / name
        command("git", "clone", "-q", "--no-local", os.fsdecode(remote), os.fsdecode(checkout))
        command("git", "-C", os.fsdecode(checkout), "config", "user.name", name.title())
        command("git", "-C", os.fsdecode(checkout), "config", "user.email", f"{name}@example.test")
        return checkout

    def _state(self) -> dict:
        return json.loads((self.forge / "state.json").read_text())

    def _refs(self) -> dict[str, tuple[tuple[str, str, str], ...]]:
        result: dict[str, tuple[tuple[str, str, str], ...]] = {}
        for name, repo in {"canonical": forge.canonical_repo(self.forge), "alice": self.alice_remote, "bob": self.bob_remote}.items():
            lines = git(repo, "for-each-ref", "--format=%(refname) %(objectname) %(objecttype)").stdout.decode("ascii").splitlines()
            result[name] = tuple(tuple(line.split(" ", 2)) for line in lines)
        return result

    def _rejection_snapshot(self) -> tuple[bytes, dict[str, tuple[tuple[str, str, str], ...]], tuple[str, ...]]:
        markers = self.forge / "receivers"
        return ((self.forge / "state.json").read_bytes(), self._refs(), tuple(sorted(path.name for path in markers.iterdir())) if markers.exists() else ())

    def _assert_rejection(self, before: tuple[bytes, dict[str, tuple[tuple[str, str, str], ...]], tuple[str, ...]]) -> None:
        self.assertEqual(before, self._rejection_snapshot())
        forge.check_invariants(self.forge)

    def _commit(self, repo: Path, message: str, timestamp: int) -> str:
        command("git", "-C", os.fsdecode(repo), "add", "-A")
        command("git", "-C", os.fsdecode(repo), "commit", "-q", "-m", message, env={"GIT_AUTHOR_DATE": f"{timestamp} +0000", "GIT_COMMITTER_DATE": f"{timestamp} +0000"})
        return git(repo, "rev-parse", "HEAD").stdout.decode("ascii").strip()

    def _recover(self) -> None:
        """Run recovery to a stable checkpoint without completing projection jobs."""
        with locked(self.forge) as state:
            pass

    def test_sync_rejects_active_receive(self) -> None:
        """Sync rejects an active receive transaction and leaves state unchanged."""
        old = git(self.alice_remote, "rev-parse", "refs/heads/main").stdout.decode("ascii").strip()
        (self.alice / "shared" / "active.txt").write_text("active\n")
        new = self._commit(self.alice, "active receive", 1_700_000_100)
        forge.run("git", "--git-dir", os.fsdecode(self.alice_remote), "fetch", "-q", os.fsdecode(self.alice), new)
        forge.pre_receive(self.forge, "alice", f"{old} {new} refs/heads/main\n".encode())
        self._recover()
        before = self._rejection_snapshot()
        with self.assertRaisesRegex(ForgeError, "active receive transaction"):
            forge.sync_view(self.forge, "alice")
        self._assert_rejection(before)

    def test_sync_rejects_unconfirmed_source(self) -> None:
        """Sync rejects while another view has an unconfirmed source publication."""
        old = git(self.alice_remote, "rev-parse", "refs/heads/main").stdout.decode("ascii").strip()
        (self.alice / "team-a" / "unconfirmed.txt").write_text("unconfirmed\n")
        new = self._commit(self.alice, "unconfirmed source", 1_700_000_100)
        forge.run("git", "--git-dir", os.fsdecode(self.alice_remote), "fetch", "-q", os.fsdecode(self.alice), new)
        forge.pre_receive(self.forge, "alice", f"{old} {new} refs/heads/main\n".encode())
        transaction = next(iter(self._state()["transactions"].values()))
        (self.forge / "receivers" / transaction["id"]).unlink(missing_ok=True)
        self._recover()
        before = self._rejection_snapshot()
        with self.assertRaisesRegex(ForgeError, "unconfirmed source publication"):
            forge.sync_view(self.forge, "bob")
        self._assert_rejection(before)

    def test_sync_rejects_unfinished_projection_work(self) -> None:
        """Sync rejects when the selected view has unfinished projection work."""
        old = git(self.alice_remote, "rev-parse", "refs/heads/main").stdout.decode("ascii").strip()
        (self.alice / "shared" / "pending.txt").write_text("pending\n")
        new = self._commit(self.alice, "pending projection", 1_700_000_100)
        forge.run("git", "--git-dir", os.fsdecode(self.alice_remote), "fetch", "-q", os.fsdecode(self.alice), new)
        forge.pre_receive(self.forge, "alice", f"{old} {new} refs/heads/main\n".encode())
        forge.git(self.alice_remote, "update-ref", "refs/heads/main", new, old)
        self._recover()
        before = self._rejection_snapshot()
        with self.assertRaisesRegex(ForgeError, "pending projection work"):
            forge.sync_view(self.forge, "bob")
        self._assert_rejection(before)


if __name__ == "__main__":
    unittest.main()
