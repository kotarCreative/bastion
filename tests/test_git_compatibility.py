"""Git maintenance, wire-protocol, and shallow-history compatibility.

The cases are derived from upstream Git tests including t5516, t5537,
t5539, t5702, t6500, t7701, t5310, t5318, and t5319.  In particular,
protocol v2 can request an unadvertised object by ID, so a projected object
database must contain exactly its reachable closure rather than relying on
upload-pack configuration for confidentiality.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import forge
from errors import ForgeError
from tests.storage_helpers import repository_snapshot
from tests.transport import LoopbackTransport
from tests.tree_harness import TreeFixture, command, git, oid


def object_inventory(repository: Path) -> set[str]:
    result = git(
        repository,
        "cat-file",
        "--batch-all-objects",
        "--batch-check=%(objectname)",
    )
    return set(result.stdout.decode("ascii").splitlines())


def reachable_inventory(repository: Path) -> set[str]:
    result = git(repository, "rev-list", "--objects", "--all")
    return {
        line.split(b" ", 1)[0].decode("ascii")
        for line in result.stdout.splitlines()
        if line
    }


class GitCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()
        self.addCleanup(self.fixture.close)

        similar = b"common delta candidate\n" + b"x" * 8192
        hidden_commit = self.fixture.admin_update(
            [
                {
                    b"hidden/maintenance-secret.txt": (
                        b"100644",
                        similar + b"\nhidden tail\n",
                    )
                }
            ]
        )[0]
        canonical = forge.canonical_repo(self.fixture.root)
        self.hidden_objects = {
            "commit": hidden_commit,
            "tree": oid(canonical, f"{hidden_commit}^{{tree}}"),
            "blob": oid(canonical, f"{hidden_commit}:hidden/maintenance-secret.txt"),
        }

        for index in range(4):
            effects = {
                b"shared/delta-candidate.txt": (
                    b"100644",
                    similar + f"\nvisible tail {index}\n".encode("ascii"),
                )
            }
            if index == 0:
                effects[b"team-a/maintenance-private.txt"] = (
                    b"100644",
                    b"alice-only maintenance object\n",
                )
            self.fixture.push_source("alice", effects)

    def assert_exact_closure(self, repository: Path) -> None:
        self.assertEqual(reachable_inventory(repository), object_inventory(repository))
        self.assertFalse((repository / "objects" / "info" / "alternates").exists())
        git(repository, "fsck", "--full", "--strict")
        for kind, object_id in self.hidden_objects.items():
            with self.subTest(hidden_kind=kind):
                result = git(repository, "cat-file", "-e", object_id, check=False)
                self.assertNotEqual(0, result.returncode)

    def assert_client_isolated(self, client: Path) -> None:
        self.assertEqual(reachable_inventory(client), object_inventory(client))
        git(client, "fsck", "--full", "--strict")
        for kind, object_id in self.hidden_objects.items():
            with self.subTest(hidden_kind=kind):
                result = git(client, "cat-file", "-e", object_id, check=False)
                self.assertNotEqual(0, result.returncode)

    def test_maintenance_and_protocol_matrix_preserves_isolation(self) -> None:
        link = forge.view_repo(self.fixture.root, "alice")
        generation = link.resolve()
        expected_tip = oid(generation, "refs/heads/main")
        expected_history = git(generation, "rev-list", "--parents", "--all").stdout
        truth_before = repository_snapshot(self.fixture.root)
        packs = list((generation / "objects" / "pack").glob("*.pack"))
        self.assertGreaterEqual(len(packs), 2, "MIDX coverage requires multiple packs")
        self.assert_exact_closure(generation)

        operations = (
            ("commit-graph", ("commit-graph", "write", "--reachable")),
            ("multi-pack-index", ("multi-pack-index", "write")),
            ("pack-bitmap", ("repack", "-a", "-d", "-b")),
            ("gc", ("gc", "--prune=now")),
        )
        for name, arguments in operations:
            with self.subTest(operation=name):
                git(generation, *arguments)
                if name == "commit-graph":
                    self.assertTrue(
                        (generation / "objects" / "info" / "commit-graph").is_file()
                    )
                elif name == "multi-pack-index":
                    index = generation / "objects" / "pack" / "multi-pack-index"
                    self.assertTrue(index.is_file())
                    git(generation, "multi-pack-index", "verify")
                elif name == "pack-bitmap":
                    self.assertTrue(
                        list((generation / "objects" / "pack").glob("*.bitmap"))
                    )

                self.assertEqual(expected_tip, oid(generation, "refs/heads/main"))
                self.assertEqual(
                    expected_history,
                    git(generation, "rev-list", "--parents", "--all").stdout,
                )
                self.assert_exact_closure(generation)
                self.assertEqual(truth_before, repository_snapshot(self.fixture.root))

                forge.sync_view(self.fixture.root, "alice")
                self.assertEqual(generation, link.resolve())

        transport = LoopbackTransport(self.fixture.root)
        self.addCleanup(transport.stop)
        url = transport.url("alice")
        unknown = "a" * 40
        for version in ("0", "1", "2"):
            with self.subTest(protocol=version):
                client = self.fixture.directory / f"protocol-{version}"
                command(
                    "git",
                    "-c",
                    f"protocol.version={version}",
                    "clone",
                    "-q",
                    "--no-local",
                    url,
                    str(client),
                )
                self.assert_client_isolated(client)
                for kind, object_id in (self.hidden_objects | {"unknown": unknown}).items():
                    with self.subTest(protocol=version, wanted_kind=kind):
                        fetched = git(
                            client,
                            "-c",
                            f"protocol.version={version}",
                            "fetch",
                            "--no-tags",
                            url,
                            object_id,
                            check=False,
                        )
                        self.assertNotEqual(0, fetched.returncode)
                        present = git(client, "cat-file", "-e", object_id, check=False)
                        self.assertNotEqual(0, present.returncode)

        self.assertEqual(truth_before, repository_snapshot(self.fixture.root))
        transport.attach(self.fixture.clients["alice"], "alice")
        self.fixture.push_source(
            "alice",
            {b"shared/post-maintenance.txt": (b"100644", b"push still works\n")},
        )
        self.assert_exact_closure(forge.view_repo(self.fixture.root, "alice").resolve())

    def test_sync_replaces_a_generation_containing_hidden_cruft(self) -> None:
        link = forge.view_repo(self.fixture.root, "alice")
        contaminated = link.resolve()
        canonical = forge.canonical_repo(self.fixture.root)
        hidden_blob = self.hidden_objects["blob"]
        contents = git(canonical, "cat-file", "blob", hidden_blob).stdout
        copied = git(
            contaminated,
            "hash-object",
            "-w",
            "-t",
            "blob",
            "--stdin",
            data=contents,
        ).stdout.decode("ascii").strip()
        self.assertEqual(hidden_blob, copied)

        git(contaminated, "repack", "-d", "--cruft")
        self.assertTrue(list((contaminated / "objects" / "pack").glob("*.mtimes")))
        git(contaminated, "fsck", "--full", "--strict")
        self.assertIn(hidden_blob, object_inventory(contaminated))
        self.assertNotIn(hidden_blob, reachable_inventory(contaminated))
        truth_before = repository_snapshot(self.fixture.root)
        with self.assertRaisesRegex(ForgeError, "unreachable objects"):
            forge.check_invariants(self.fixture.root)

        forge.sync_view(self.fixture.root, "alice")

        repaired = link.resolve()
        self.assertNotEqual(contaminated, repaired)
        self.assert_exact_closure(repaired)
        self.assertEqual(truth_before, repository_snapshot(self.fixture.root))

    def test_shallow_deepen_and_unshallow_protocol_matrix(self) -> None:
        view = forge.view_repo(self.fixture.root, "alice").resolve()
        full_count = int(git(view, "rev-list", "--count", "HEAD").stdout)
        self.assertGreater(full_count, 3)
        truth_before = repository_snapshot(self.fixture.root)

        transport = LoopbackTransport(self.fixture.root)
        self.addCleanup(transport.stop)
        url = transport.url("alice")
        for version in ("0", "1", "2"):
            with self.subTest(protocol=version):
                client = self.fixture.directory / f"shallow-{version}"
                command(
                    "git",
                    "-c",
                    f"protocol.version={version}",
                    "clone",
                    "-q",
                    "--no-local",
                    "--depth=1",
                    url,
                    str(client),
                )
                self.assertTrue((client / ".git" / "shallow").is_file())
                self.assertEqual(1, int(git(client, "rev-list", "--count", "HEAD").stdout))
                self.assert_client_isolated(client)

                git(
                    client,
                    "-c",
                    f"protocol.version={version}",
                    "fetch",
                    "-q",
                    "--deepen=2",
                    "origin",
                    "main",
                )
                self.assertEqual(3, int(git(client, "rev-list", "--count", "HEAD").stdout))
                self.assert_client_isolated(client)

                git(
                    client,
                    "-c",
                    f"protocol.version={version}",
                    "fetch",
                    "-q",
                    "--unshallow",
                    "origin",
                    "main",
                )
                self.assertFalse((client / ".git" / "shallow").exists())
                self.assertEqual(
                    full_count,
                    int(git(client, "rev-list", "--count", "HEAD").stdout),
                )
                self.assert_client_isolated(client)

        self.assertEqual(truth_before, repository_snapshot(self.fixture.root))


if __name__ == "__main__":
    unittest.main()
