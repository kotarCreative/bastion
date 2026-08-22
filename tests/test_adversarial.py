

"""Adversarial tests: hostile clients attacking every supported entry point.

Each test plays an attacker who controls Git plumbing, raw protocol bytes,
and HTTP wire requests, and asserts that hard invariants survive: rejected
input never moves refs or mutates state, hidden objects never leak, and
authorization cannot be bypassed by forged history or metadata.
"""

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
from unittest.mock import patch

import forge
from errors import ForgeError
from http_server import add_token, make_server, pkt_line
from layout import canonical_repo, view_repo
from tests.tree_harness import (
    TreeFixture,
    build_commit,
    clean_environment,
    command,
    git,
    oid,
    tree_at,
)

from tests.storage_helpers import document_bytes, read_document

ZERO = "0" * 40
SECRET_BYTES = b"unshared hidden secret bytes"


def update_line(old: str, new: str, refname: str = "refs/heads/main") -> bytes:
    return f"{old} {new} {refname}\n".encode()


def raw_pre_receive(fixture: TreeFixture, view: str, updates: bytes) -> None:
    with patch.dict(os.environ, clean_environment(), clear=True):
        forge.pre_receive(fixture.root, view, updates)


def forged_commit(
    repo: Path,
    parents: list[str],
    effects: dict[bytes, tuple[bytes, bytes]],
    message: bytes,
    timestamp: int,
) -> str:
    """Build a commit with arbitrary parents and effects via plumbing."""
    entries: dict[bytes, tuple[bytes, bytes]] = {}
    if parents:
        entries = {
            path: (mode, contents)
            for path, (mode, _oid, contents) in tree_at(repo, parents[0]).items()
        }
    for path, effect in effects.items():
        if effect is None:
            entries.pop(path, None)
        else:
            entries[path] = effect
    descriptor, raw_index = tempfile.mkstemp(prefix="adv-index-", dir=repo)
    os.close(descriptor)
    index = Path(raw_index)
    index.unlink()
    environment = clean_environment() | {"GIT_INDEX_FILE": str(index)}
    try:
        git(repo, "read-tree", "--empty", env=environment)
        records: list[bytes] = []
        for path, (mode, contents) in sorted(entries.items()):
            object_id = git(repo, "hash-object", "-w", "--stdin", data=contents).stdout.strip()
            records.append(mode + b" " + object_id + b"\t" + path + b"\0")
        if records:
            git(repo, "update-index", "-z", "--index-info", data=b"".join(records), env=environment)
        tree = git(repo, "write-tree", env=environment).stdout.decode().strip()
        arguments: list[str] = ["commit-tree", tree]
        for parent in parents:
            arguments.extend(("-p", parent))
        date = f"{timestamp} +0000"
        metadata = clean_environment() | {
            "GIT_AUTHOR_NAME": "Adversary", "GIT_AUTHOR_EMAIL": "adversary@example.test",
            "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_NAME": "Adversary",
            "GIT_COMMITTER_EMAIL": "adversary@example.test", "GIT_COMMITTER_DATE": date,
        }
        return git(repo, *arguments, data=message, env=metadata).stdout.decode().strip()
    finally:
        index.unlink(missing_ok=True)


def dangling_commit(repo: Path, parent: str, message: bytes, timestamp: int) -> str:
    """Write a commit object whose parent object exists nowhere."""
    tip = oid(repo, "refs/heads/main")
    tree = git(repo, "rev-parse", f"{tip}^{{tree}}").stdout.decode().strip()
    date = f"{timestamp} +0000"
    raw = (
        f"tree {tree}\n"
        f"parent {parent}\n"
        f"author Adversary <adversary@example.test> {date}\n"
        f"committer Adversary <adversary@example.test> {date}\n"
        f"\n{message.decode(errors='replace')}\n"
    ).encode()
    return git(repo, "hash-object", "-t", "commit", "-w", "--stdin", data=raw).stdout.decode().strip()


def nested_tree(repo: Path, flat: dict[bytes, tuple[bytes, str]]) -> str:
    """Write a raw tree object from a flat path map, preserving exact modes."""

    def build(prefix: bytes) -> str:
        subtrees: dict[bytes, dict[bytes, tuple[bytes, str]]] = {}
        leaves: list[tuple[bytes, bytes, str]] = []
        for path, entry in flat.items():
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            if b"/" in rest:
                head = rest.split(b"/", 1)[0]
                subtrees.setdefault(head, {})[path] = entry
            else:
                leaves.append((rest, entry[0], entry[1]))
        records: list[tuple[bytes, bytes, str]] = [
            (name + b"/", b"040000", build(prefix + name + b"/"))
            for name, child in sorted(subtrees.items())
        ]
        records.extend(leaves)
        records.sort(key=lambda record: record[0])
        raw = b"".join(
            mode + b" " + object_id.encode("ascii") + b"\t" + name + b"\0"
            for name, mode, object_id in records
        )
        return git(repo, "hash-object", "-t", "tree", "--literally", "-w", "--stdin", data=raw).stdout.decode().strip()

    return build(b"")


def flat_entries(repo: Path, commit: str) -> dict[bytes, tuple[bytes, str]]:
    result = git(repo, "ls-tree", "-rz", "--full-tree", commit).stdout
    flat: dict[bytes, tuple[bytes, str]] = {}
    for record in result.split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        mode, _kind, object_id = metadata.split(b" ")
        flat[path] = (mode, object_id.decode("ascii"))
    return flat


class AdversarialPushTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def client(self, name: str) -> Path:
        return self.fixture.clients[name]

    def expect_rejected_push(self, client: Path, *arguments: str) -> None:
        before = self.fixture.rejection_snapshot()
        pushed = command("git", "-C", os.fsdecode(client), "push", *arguments, check=False)
        self.assertNotEqual(pushed.returncode, 0, pushed.stderr.decode(errors="replace"))
        self.fixture.assert_rejection(before)

    # -- raw ref-update protocol attacks -------------------------------

    def test_multi_ref_update_batch_rejected(self) -> None:
        view = self.fixture.state()["views"]["alice"]
        updates = update_line(view["projected_tip"], "d" * 40) + update_line(view["projected_tip"], "e" * 40)
        before = self.fixture.rejection_snapshot()
        with self.assertRaises(ForgeError):
            raw_pre_receive(self.fixture, "alice", updates)
        self.fixture.assert_rejection(before)

    def test_duplicate_and_empty_update_streams_rejected(self) -> None:
        tip = self.fixture.state()["views"]["alice"]["projected_tip"]
        for updates in (b"", b"\n", update_line(tip, "d" * 40) * 2):
            before = self.fixture.rejection_snapshot()
            with self.subTest(updates=updates), self.assertRaises(ForgeError):
                raw_pre_receive(self.fixture, "alice", updates)
            self.fixture.assert_rejection(before)

    def test_zero_oid_create_and_delete_rejected(self) -> None:
        tip = self.fixture.state()["views"]["alice"]["projected_tip"]
        for old, new in ((ZERO, "d" * 40), (tip, ZERO)):
            before = self.fixture.rejection_snapshot()
            with self.subTest(old=old, new=new), self.assertRaises(ForgeError):
                raw_pre_receive(self.fixture, "alice", update_line(old, new))
            self.fixture.assert_rejection(before)

    def test_stale_and_foreign_old_values_rejected(self) -> None:
        canonical_tip = oid(canonical_repo(self.fixture.root), "refs/heads/main")
        bob_tip = self.fixture.state()["views"]["bob"]["projected_tip"]
        for old in (canonical_tip, bob_tip):
            before = self.fixture.rejection_snapshot()
            with self.subTest(old=old), self.assertRaises(ForgeError):
                raw_pre_receive(self.fixture, "alice", update_line(old, "d" * 40))
            self.fixture.assert_rejection(before)

    def test_ref_name_confusion_rejected(self) -> None:
        tip = self.fixture.state()["views"]["alice"]["projected_tip"]
        for refname in (
            "refs/heads/Main",
            "REFS/heads/main",
            "refs/heads/main/nested",
            "refs/heads/alice",
            "refs/tags/v1",
            "refs/heads/main ",
        ):
            before = self.fixture.rejection_snapshot()
            with self.subTest(refname=refname), self.assertRaises(ForgeError):
                raw_pre_receive(self.fixture, "alice", update_line(tip, "d" * 40, refname))
            self.fixture.assert_rejection(before)

    def test_malformed_object_ids_rejected(self) -> None:
        tip = self.fixture.state()["views"]["alice"]["projected_tip"]
        for old, new in (
            ("A" * 40, "d" * 40),
            (tip[:30], "d" * 40),
            ("HEAD", "d" * 40),
            (tip, "z" * 40),
            (tip, "d" * 39 + "g"),
        ):
            before = self.fixture.rejection_snapshot()
            with self.subTest(old=old, new=new), self.assertRaises(ForgeError):
                raw_pre_receive(self.fixture, "alice", update_line(old, new))
            self.fixture.assert_rejection(before)

    # -- history forgery ------------------------------------------------

    def test_merge_commit_push_rejected(self) -> None:
        self.fixture.push_source("alice", {b"shared/seed.txt": (b"100644", b"seed\n")})
        client = self.client("alice")
        tip = oid(client, "refs/heads/main")
        previous = git(client, "rev-parse", f"{tip}^").stdout.decode().strip()
        merge = forged_commit(client, [tip, previous], {}, b"merge attempt\n", self.fixture._next_timestamp())
        self.expect_rejected_push(client, "-q", "origin", f"{merge}:refs/heads/main")

    def test_rewind_graft_force_push_rejected(self) -> None:
        self.fixture.push_source("alice", {b"shared/seed.txt": (b"100644", b"seed\n")})
        client = self.client("alice")
        tip = oid(client, "refs/heads/main")
        previous = git(client, "rev-parse", f"{tip}^").stdout.decode().strip()
        rewind = forged_commit(client, [previous], {b"shared/base.txt": (b"100644", b"rewritten\n")},
                               b"history rewrite\n", self.fixture._next_timestamp())
        self.expect_rejected_push(client, "-q", "--force", "origin", f"{rewind}:refs/heads/main")

    def test_dangling_graft_onto_hidden_canonical_commit_rejected(self) -> None:
        hidden_commit = self.fixture.admin_update([{b"hidden/grown.txt": (b"100644", b"admin only\n")}])[0]
        client = self.client("alice")
        forged = dangling_commit(client, hidden_commit, b"graft onto hidden history\n", self.fixture._next_timestamp())
        tip = oid(client, "refs/heads/main")
        before = self.fixture.rejection_snapshot()
        with self.assertRaises(ForgeError):
            raw_pre_receive(self.fixture, "alice", update_line(tip, forged))
        self.fixture.assert_rejection(before)

    def test_dangling_graft_onto_foreign_view_tip_rejected(self) -> None:
        alice_tip = self.fixture.state()["views"]["alice"]["projected_tip"]
        client = self.client("bob")
        forged = dangling_commit(client, alice_tip, b"graft onto alice projection\n", self.fixture._next_timestamp())
        tip = oid(client, "refs/heads/main")
        before = self.fixture.rejection_snapshot()
        with self.assertRaises(ForgeError):
            raw_pre_receive(self.fixture, "bob", update_line(tip, forged))
        self.fixture.assert_rejection(before)

    # -- tree smuggling --------------------------------------------------

    def test_hidden_path_smuggled_into_authorized_commit_rejected(self) -> None:
        client = self.client("alice")
        build_commit(client, oid(client, "refs/heads/main"),
                     {b"shared/ok.txt": (b"100644", b"fine\n"), b"hidden/evil.txt": (b"100644", b"smuggled\n")},
                     b"smuggle\n", self.fixture._next_timestamp())
        self.expect_rejected_push(client, "-q", "origin", "main")

    def test_foreign_scope_smuggled_into_authorized_commit_rejected(self) -> None:
        client = self.client("bob")
        build_commit(client, oid(client, "refs/heads/main"),
                     {b"shared/ok.txt": (b"100644", b"fine\n"), b"team-a/invasion.txt": (b"100644", b"invaded\n")},
                     b"invade\n", self.fixture._next_timestamp())
        self.expect_rejected_push(client, "-q", "origin", "main")

    def test_illegal_tree_mode_rejected(self) -> None:
        client = self.client("alice")
        tip = oid(client, "refs/heads/main")
        flat = flat_entries(client, tip)
        blob = git(client, "hash-object", "-w", "--stdin", data=b"evil\n").stdout.decode().strip()
        flat[b"shared/evil.txt"] = (b"100664", blob)
        tree = nested_tree(client, flat)
        date = f"{self.fixture._next_timestamp()} +0000"
        metadata = clean_environment() | {
            "GIT_AUTHOR_NAME": "Adversary", "GIT_AUTHOR_EMAIL": "adversary@example.test",
            "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_NAME": "Adversary",
            "GIT_COMMITTER_EMAIL": "adversary@example.test", "GIT_COMMITTER_DATE": date,
        }
        forged = git(client, "commit-tree", tree, "-p", tip, data=b"illegal mode\n",
                     env=metadata).stdout.decode().strip()
        before = self.fixture.rejection_snapshot()
        pushed = command("git", "-C", os.fsdecode(client), "push", "-q", "origin",
                         f"{forged}:refs/heads/main", check=False)
        self.assertNotEqual(pushed.returncode, 0)
        self.fixture.assert_rejection(before)

    def test_exec_bit_flip_outside_write_scope_rejected(self) -> None:
        client = self.client("limited")
        build_commit(client, oid(client, "refs/heads/main"),
                     {b"team-a/private-exec": (b"100644", b"private executable\n")},
                     b"flip\n", self.fixture._next_timestamp())
        self.expect_rejected_push(client, "-q", "origin", "main")

    def test_symlink_escape_grants_nothing(self) -> None:
        self.fixture.push_source("alice", {b"shared/link": (b"120000", b"../hidden/secret.txt")})
        bob_repo = view_repo(self.fixture.root, "bob")
        link_blob = git(bob_repo, "rev-parse", "refs/heads/main:shared/link").stdout.decode().strip()
        self.assertEqual(git(bob_repo, "cat-file", "blob", link_blob).stdout, b"../hidden/secret.txt")
        checkout = self.fixture.directory / "bob-symlink-check"
        command("git", "clone", "-q", "--no-local", os.fsdecode(bob_repo), os.fsdecode(checkout))
        link = checkout / "shared" / "link"
        self.assertTrue(os.path.islink(link))
        self.assertFalse(os.path.exists(link))
        self.assertFalse(os.path.exists(checkout / "hidden" / "secret.txt"))

    # -- Change-Id abuse -------------------------------------------------

    def test_duplicate_change_id_trailers_rejected(self) -> None:
        client = self.client("alice")
        build_commit(client, oid(client, "refs/heads/main"), {b"shared/dup.txt": (b"100644", b"dup\n")},
                     b"subject\n\nChange-Id: I1111111111111\nChange-Id: I2222222222222\n",
                     self.fixture._next_timestamp())
        self.expect_rejected_push(client, "-q", "origin", "main")

    def test_change_id_smuggling_positions_never_claimed(self) -> None:
        for message in (
            b"Change-Id: IMID123456789\n\nreal body\n",
            b"subject\n\nChange-Id: I$(touch /tmp/pwned)\n",
            b"subject\n\nChange-Id: I" + b"a" * 200 + b"\n",
        ):
            with self.subTest(message=message):
                self.fixture.push_source("alice", {b"shared/smuggle.txt": (b"100644", b"x\n")}, message=message)
                claimed = json.dumps(self.fixture.state()["changes"])
                for line in message.split(b"\n"):
                    if line.startswith(b"Change-Id: "):
                        self.assertNotIn(line[len(b"Change-Id: "):].decode(), claimed)

    # -- replay and desync ------------------------------------------------

    def test_post_receive_unknown_transaction_is_inert(self) -> None:
        before = self.fixture.rejection_snapshot()
        with patch.dict(os.environ, clean_environment(), clear=True):
            forge.post_receive(self.fixture.root, "alice", update_line("1" * 40, "2" * 40))
        self.fixture.assert_rejection(before)


class AdversarialHttpWireTest(unittest.TestCase):
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
        (self.source / "hidden" / "secret.txt").write_text(SECRET_BYTES.decode())
        environment = dict(os.environ, GIT_AUTHOR_DATE="1700000000 +0000", GIT_COMMITTER_DATE="1700000000 +0000")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-q", "-m", "initial mixed state", env=environment)
        self.hidden_blob = git(self.source, "rev-parse", "HEAD:hidden/secret.txt").stdout.strip()
        self.forge = self.root / "forge"
        forge.init_forge(self.forge, self.source, "main")
        from tests import identity_harness

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

    def request(self, path: str, method: str = "GET", headers: dict[str, str] | None = None,
                data: bytes | None = None) -> tuple[int, bytes]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            try:
                return error.code, error.read()
            finally:
                error.close()

    def alice(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.alice_token}"}

    def snapshot(self) -> tuple[bytes, bytes, str]:
        return (
            document_bytes(self.forge),
            (self.forge / "tokens.json").read_bytes(),
            subprocess.run(
                ["git", "--git-dir", str(canonical_repo(self.forge)), "for-each-ref"],
                stdout=subprocess.PIPE, check=True,
            ).stdout.decode(),
        )

    def test_raw_upload_pack_want_of_hidden_objects_leaks_nothing(self) -> None:
        canonical_tip = subprocess.run(
            ["git", "--git-dir", str(canonical_repo(self.forge)), "rev-parse", "refs/heads/main"],
            stdout=subprocess.PIPE, check=True,
        ).stdout.strip()
        headers = self.alice() | {"Content-Type": "application/x-git-upload-pack-request"}
        for wanted in (self.hidden_blob, canonical_tip):
            body = pkt_line(b"want " + wanted + b" multi_ack_detailed side-band-64k agent=adversary\n") + b"0000"
            status, data = self.request("/alice.git/git-upload-pack", method="POST", headers=headers, data=body)
            self.assertIn(status, (200, 500), wanted)
            self.assertNotIn(SECRET_BYTES, data)
            self.assertNotIn(b"secret", data.lower())

    def test_route_confusion_variants_are_uniform_404(self) -> None:
        probes = (
            "/ALICE.git/info/refs?service=git-upload-pack",
            "/%D0%B0lice.git/info/refs?service=git-upload-pack",
            "/alice.git%5C/info/refs?service=git-upload-pack",
            "/alice.git./info/refs?service=git-upload-pack",
            "/canonical.git/info/refs?service=git-upload-pack",
            "/alice.git/info/refs?service=git-upload-pack%00",
        )
        observed = [self.request(path, headers=self.alice()) for path in probes]
        for path, (status, body) in zip(probes, observed):
            self.assertEqual(status, 404, path)
            self.assertEqual(body, b"not found", path)
        self.assertEqual(len({body for _, body in observed}), 1)

    def test_leading_double_slash_normalization_grants_no_access(self) -> None:
        # Python's http.server collapses leading "//" before routing; the
        # invariant is that normalization never bypasses authentication or
        # cross-view authorization.
        status, body = self.request(
            "//alice.git/info/refs?service=git-upload-pack"
        )
        self.assertEqual(status, 401)
        self.assertEqual(body, b"authentication required")
        status, _ = self.request(
            "//alice.git/info/refs?service=git-upload-pack",
            headers={"Authorization": f"Bearer {self.bob_token}"},
        )
        self.assertEqual(status, 404)

    def test_garbage_gzip_body_rejected(self) -> None:
        status, body = self.request(
            "/alice.git/git-upload-pack",
            method="POST",
            headers=self.alice() | {"Content-Encoding": "gzip"},
            data=b"\xff\x00not-gzip\xff",
        )
        self.assertEqual(status, 400)
        self.assertEqual(body, b"bad request")

    def test_wire_attacks_leave_forge_state_identical(self) -> None:
        before = self.snapshot()
        attempts = [
            ("/alice.git/git-upload-pack", "POST", self.alice() | {"Content-Encoding": "gzip"}, b"garbage"),
            ("/alice.git/git-receive-pack", "POST", self.alice(), b"\xff\xff\xff\xff"),
            ("/ALICE.git/info/refs?service=git-receive-pack", "GET", self.alice(), None),
            ("/canonical.git/git-receive-pack", "POST", self.alice(), b"0000"),
        ]
        for path, method, headers, data in attempts:
            self.request(path, method=method, headers=headers, data=data)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
