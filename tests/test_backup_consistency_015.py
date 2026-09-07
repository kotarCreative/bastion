"""Spec 015 C1: enforced backup consistency and usable restore drill.

Barrier-controlled publication and index changes during a backup are
rejected; the fence comes from the database snapshot actually exported;
restore validates the repository identity and the database/WAL fence; an
ahead-of-truth snapshot fails cleanly unless explicitly repaired by
rebuilding derived rows only; and a restored target passes the full
restore drill.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import forge
import identity
from errors import ForgeError
from state import load_state, locked, save_state
from store import (
    _checksum,
    _SqliteBackend,
    backup_forge,
    open_store,
    repair_derived_state,
    restore_forge,
)
from tests.storage_helpers import repository_state
from tests.transport import attach_intake_push
from tests.tree_harness import TreeFixture, build_commit, command, git, oid


def stage_receive(fixture: TreeFixture, name: str = "baseline") -> None:
    """Build and stage one commit so a later direct receive publishes it."""
    client = fixture.clients["alice"]
    parent = oid(client, "refs/heads/main")
    new = build_commit(
        client,
        parent,
        {f"shared/{name}.txt".encode(): (b"100644", f"{name}\n".encode())},
        f"{name}\n".encode(),
        1_700_000_700,
    )
    from layout import intake_repo

    command(
        "git", "--git-dir", os.fsdecode(intake_repo(fixture.root, "alice")),
        "fetch", "-q", os.fsdecode(client), "refs/heads/main",
    )
    return parent, new


def direct_receive(fixture: TreeFixture, parent: str, new: str) -> None:
    from unittest.mock import patch

    with patch.dict(
        os.environ,
        {
            "BASTION_VIEW": "alice",
            "BASTION_PRINCIPAL_ID": "alice",
            "BASTION_TRANSPORT": "test",
        },
        clear=False,
    ):
        forge.pre_receive(
            fixture.root, "alice", f"{parent} {new} refs/heads/main\n".encode()
        )


def bump_index_ahead(root: Path) -> None:
    """A lease-ignoring writer advances the derived index unconditionally."""
    store = open_store(root)
    try:
        document = store.load_document()
        manifest, _replay = repository_state(root)
        document["last_indexed_seq"] = manifest.head_seq + 1
        store.save_document(document, [], root)
    finally:
        store.close()


def tamper_backup_index_ahead(backup: Path) -> None:
    """Make the exported database index ahead of the pinned WAL."""
    database = backup / "database.sqlite3"
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT doc FROM forge_meta WHERE id = 1"
        ).fetchone()
        meta = json.loads(row[0])
        meta["last_indexed_seq"] = int(meta["last_indexed_seq"]) + 3
        connection.execute(
            "UPDATE forge_meta SET doc = ?, checksum = ? WHERE id = 1",
            (json.dumps(meta, sort_keys=True), _checksum(meta)),
        )
        connection.commit()
    finally:
        connection.close()
    manifest = json.loads((backup / "manifest.json").read_text())
    data = database.read_bytes()
    for artifact in manifest["artifacts"]:
        if artifact["path"] == "database.sqlite3":
            artifact["sha256"] = hashlib.sha256(data).hexdigest()
            artifact["size"] = len(data)
    (backup / "manifest.json").write_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    )


class BackupConsistencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()
        self.addCleanup(self.fixture.close)
        self.root = self.fixture.root

    def test_quiet_backup_records_fence_from_exported_database(self) -> None:
        destination = self.fixture.directory / "backup-quiet"
        backup_forge(self.root, destination)
        metadata = json.loads((destination / "manifest.json").read_text())
        store = open_store(self.root)
        try:
            document = store.load_document()
        finally:
            store.close()
        self.assertEqual(metadata["last_indexed_seq"], document["last_indexed_seq"])
        self.assertTrue(metadata["quiesced"])
        self.assertLessEqual(metadata["last_indexed_seq"], metadata["head_seq"])

    def test_backup_racing_publication_and_index_is_refused(self) -> None:
        manifest, _replay = repository_state(self.root)
        parent, new = stage_receive(self.fixture, "race")
        destination = self.fixture.directory / "backup-race"
        original_dump = _SqliteBackend.dump

        def racing_dump(backend, target):
            # Interleave between the WAL pin and the database export: a
            # real publication advances the WAL while a lease-ignoring
            # writer advances the derived index.
            direct_receive(self.fixture, parent, new)
            bump_index_ahead(self.root)
            return original_dump(backend, target)

        with patch.object(_SqliteBackend, "dump", racing_dump):
            with self.assertRaises(ForgeError) as caught:
                backup_forge(self.root, destination)
        self.assertIn("quiet", str(caught.exception))
        self.assertFalse(destination.exists(), "partial backup survived the race")
        # The explicit validated derived-state repair rewinds the index the
        # rogue writer pushed ahead; a quiet retry then succeeds.
        repair_derived_state(self.root)
        backup_forge(self.root, destination)
        self.assertTrue(destination.exists())

    def test_backup_with_ahead_index_refused(self) -> None:
        manifest, _replay = repository_state(self.root)
        destination = self.fixture.directory / "backup-ahead"
        original_dump = _SqliteBackend.dump

        def ahead_dump(backend, target):
            bump_index_ahead(self.root)
            return original_dump(backend, target)

        with patch.object(_SqliteBackend, "dump", ahead_dump):
            with self.assertRaises(ForgeError) as caught:
                backup_forge(self.root, destination)
        self.assertIn("ahead", str(caught.exception))
        self.assertFalse(destination.exists())

    def test_restore_refuses_ahead_of_truth_snapshot(self) -> None:
        destination = self.fixture.directory / "backup-drill-src"
        backup_forge(self.root, destination)
        tamper_backup_index_ahead(destination)
        target = self.fixture.directory / "restore-refused"
        with self.assertRaises(ForgeError) as caught:
            restore_forge(destination, target)
        self.assertIn("ahead", str(caught.exception))
        self.assertFalse(target.exists(), "partial restore target survived")

    def test_restore_refuses_wrong_repository_identity(self) -> None:
        destination = self.fixture.directory / "backup-identity"
        backup_forge(self.root, destination)
        manifest = json.loads((destination / "manifest.json").read_text())
        manifest["repository_id"] = "f" * 32
        (destination / "manifest.json").write_bytes(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        )
        target = self.fixture.directory / "restore-identity"
        with self.assertRaises(ForgeError):
            restore_forge(destination, target)
        self.assertFalse(target.exists())

    def test_repair_rebuilds_only_derived_state(self) -> None:
        destination = self.fixture.directory / "backup-repair"
        backup_forge(self.root, destination)
        tamper_backup_index_ahead(destination)
        target = self.fixture.directory / "restore-repaired"
        restore_forge(destination, target, repair_derived=True)
        restored = load_state(target)
        # Derived rows are rebuilt from the pinned WAL.
        self.assertIn("alice", restored["views"])
        self.assertTrue(restored["transactions"], "derived transactions were not rebuilt")
        # Authoritative catalog and token state is untouched.
        self.assertIn("alice", restored["principals"])
        manifest, _replay = repository_state(target)
        self.assertLessEqual(restored["last_indexed_seq"], manifest.head_seq)
        forge.check_invariants(target)


class RestoreDrillTest(unittest.TestCase):
    def test_full_drill_on_throwaway_target(self) -> None:
        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        parent, new = fixture.push_source(
            "alice", {b"shared/drill.txt": (b"100644", b"drill\n")}
        ) if hasattr(fixture, "push_source") else (None, None)
        destination = fixture.directory / "drill-backup"
        backup_forge(fixture.root, destination)
        target = fixture.directory / "drill-restore"
        restore_forge(destination, target)

        # Invariants on the restored target.
        forge.check_invariants(target)

        # Authenticated clone and fetch.
        clone = fixture.directory / "drill-clone"
        command(
            "git", "clone", "-q", "--no-local",
            os.fsdecode(forge.view_repo(target, "alice")),
            os.fsdecode(clone),
        )
        command("git", "-C", os.fsdecode(clone), "fetch", "-q", "origin")

        # A catalog write on the restored target.
        from service import add_principal

        add_principal(target, "carol")
        self.assertIn("carol", load_state(target)["principals"])

        # A permitted push on the restored target.
        attach_intake_push(target, clone, "alice")
        build_commit(
            clone,
            oid(clone, "refs/heads/main"),
            {b"shared/drill-2.txt": (b"100644", b"drill 2\n")},
            b"drill 2\n",
            1_700_000_800,
        )
        command("git", "-C", os.fsdecode(clone), "push", "origin", "main")
        forge.drain_projection_jobs(target)
        forge.check_invariants(target)


if __name__ == "__main__":
    unittest.main()
