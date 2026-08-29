"""Tests for spec 012: permission integrity and verifiable authorization."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import forge
import identity
from errors import ForgeError
from service import add_principal
from invariants import check_invariants, InvariantError
from repository import Repository
from service import update_canonical_ref
from state import load_state, save_state
from store import restore_forge
from tests.identity_harness import (
    ADMIN_PRINCIPAL,
    attach_admin_push,
    capability_grants,
    provision_admin,
    update_canonical,
)
from tests.storage_helpers import repository_snapshot, repository_state, wal_records
from tests.tree_harness import TreeFixture, build_commit, command, git, oid
from tests.transport import attach_intake_push
from wal import canonical_json, validate_descriptor


def commit(repo: Path, message: str, timestamp: int) -> str:
    env = {
        "GIT_AUTHOR_DATE": f"{timestamp} +0000",
        "GIT_COMMITTER_DATE": f"{timestamp} +0000",
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.test",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.test",
    }
    git(repo, "add", "-A")
    command("git", "commit", "-q", "-m", message, cwd=repo, env=env)
    return oid(repo, "HEAD")


class PrefixAdjudicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        command("git", "init", "-q", "-b", "main", str(source), cwd=self.root)
        git(source, "config", "user.name", "Fixture")
        git(source, "config", "user.email", "fixture@example.test")
        for name in ("shared", "shared/exception", "shared/exception/private"):
            directory = source / name
            directory.mkdir(parents=True)
            (directory / "base.txt").write_text(f"{name}\n")
        commit(source, "initial", 1_700_000_000)
        self.forge = self.root / "forge"
        forge.init_forge(self.forge, source, "main")
        provision_admin(self.forge)
        add_principal(self.forge, "scoped")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _role(self, allow: dict, deny: dict) -> None:
        from service import add_role, bind_role
        add_role(self.forge, "scoped-role", allow, deny)
        bind_role(self.forge, "scoped-role", "principal", "scoped")

    def _admin_push(self, path: str, contents: str) -> tuple[str, str]:
        checkout = self.root / "admin"
        if not checkout.exists():
            command("git", "clone", "-q", str(forge.canonical_repo(self.forge)), str(checkout))
            attach_admin_push(self.forge, checkout)
            git(checkout, "config", "user.name", "Admin")
            git(checkout, "config", "user.email", "admin@example.test")
        target = checkout / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
        new = commit(checkout, f"admin {path}", 1_700_100_000)
        old = git(checkout, "rev-parse", "HEAD~1").stdout.decode().strip()
        command("git", "push", "-q", "origin", f"{new}:refs/staging/admin", cwd=checkout)
        return old, new

    def test_nested_deny_is_not_discarded(self) -> None:
        """Broad deny, nested allow, still-narrower deny rejects the narrow path."""
        allow, deny = capability_grants(["shared/exception"], ["shared/exception"])
        allow["admin"] = ["shared/exception"]
        deny["admin"] = ["shared", "shared/exception/private"]
        self._role(allow, deny)
        # shared/exception/base.txt is allowed (allow longer than deny)
        old, new = self._admin_push("shared/exception/base.txt", "ok\n")
        update_canonical(self.forge, old, new, "scoped")
        # shared/exception/private/base.txt is denied (narrower deny wins)
        old, new = self._admin_push("shared/exception/private/base.txt", "no\n")
        with self.assertRaisesRegex(ForgeError, "lacks admin coverage"):
            update_canonical(self.forge, old, new, "scoped")

    def test_broad_allow_nested_deny_narrower_allow(self) -> None:
        """Broad allow, nested deny, narrower allow overrides the deny."""
        allow, deny = capability_grants(["shared"], ["shared"])
        allow["admin"] = ["shared/exception/private"]
        deny["admin"] = ["shared/exception"]
        self._role(allow, deny)
        # shared/exception/private is allowed because narrower allow wins.
        old, new = self._admin_push("shared/exception/private/base.txt", "ok\n")
        update_canonical(self.forge, old, new, "scoped")


class DescriptorConstructionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        command("git", "init", "-q", "-b", "main", str(source), cwd=self.root)
        git(source, "config", "user.name", "Fixture")
        git(source, "config", "user.email", "fixture@example.test")
        (source / "shared").mkdir()
        (source / "shared" / "base.txt").write_text("base\n")
        commit(source, "initial", 1_700_000_000)
        self.forge = self.root / "forge"
        forge.init_forge(self.forge, source, "main")
        provision_admin(self.forge)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_equivalent_role_unions_in_different_orders_match(self) -> None:
        from service import add_role, add_team, bind_role

        add_principal(self.forge, "alice")
        add_team(self.forge, "eng", ["alice"])
        allow_a, _ = capability_grants(["shared"], ["shared"])
        allow_b, _ = capability_grants(["team-a"], ["team-a"])
        add_role(self.forge, "a", allow_a, {key: [] for key in identity.CAPABILITIES})
        add_role(self.forge, "b", allow_b, {key: [] for key in identity.CAPABILITIES})
        bind_role(self.forge, "a", "principal", "alice")
        bind_role(self.forge, "b", "team", "eng")

        add_principal(self.forge, "bob")
        add_team(self.forge, "ops", ["bob"])
        add_role(self.forge, "b2", allow_b, {key: [] for key in identity.CAPABILITIES})
        add_role(self.forge, "a2", allow_a, {key: [] for key in identity.CAPABILITIES})
        bind_role(self.forge, "b2", "team", "ops")
        bind_role(self.forge, "a2", "principal", "bob")

        from identity import effective_descriptor
        left = effective_descriptor(load_state(self.forge), "alice", "refs/heads/main")
        right = effective_descriptor(load_state(self.forge), "bob", "refs/heads/main")
        self.assertEqual(left["capabilities"], right["capabilities"])
        self.assertEqual(left["restrictions"], right["restrictions"])

    def test_exact_duplicate_prefixes_removed_ancestors_retained(self) -> None:
        from service import add_principal, add_role, bind_role
        from identity import effective_descriptor

        add_principal(self.forge, "dup")
        allow, _ = capability_grants(["shared", "shared", "shared/a", "shared"], ["shared"])
        add_role(self.forge, "dup-role", allow, {key: [] for key in identity.CAPABILITIES})
        bind_role(self.forge, "dup-role", "principal", "dup")
        descriptor = effective_descriptor(load_state(self.forge), "dup", "refs/heads/main")
        self.assertEqual(["shared", "shared/a"], descriptor["capabilities"]["read"])

    def test_unsorted_duplicate_nonnormalized_descriptors_rejected(self) -> None:
        from service import add_principal, add_role, bind_role
        from identity import effective_descriptor

        add_principal(self.forge, "bad")
        allow, deny = capability_grants(["shared"], ["shared"])
        add_role(self.forge, "bad-role", allow, deny)
        bind_role(self.forge, "bad-role", "principal", "bad")
        descriptor = effective_descriptor(load_state(self.forge), "bad", "refs/heads/main")
        # Unsorted list
        forged = copy.deepcopy(descriptor)
        forged["capabilities"]["read"] = ["shared/a", "shared"]
        forged["id"] = identity.compute_descriptor_id({k: v for k, v in forged.items() if k != "id"})
        with self.assertRaisesRegex(ForgeError, "must be sorted"):
            validate_descriptor(forged)
        # Duplicate list
        forged = copy.deepcopy(descriptor)
        forged["capabilities"]["read"] = ["shared", "shared"]
        forged["id"] = identity.compute_descriptor_id({k: v for k, v in forged.items() if k != "id"})
        with self.assertRaisesRegex(ForgeError, "must be sorted"):
            validate_descriptor(forged)
        # Non-normalized list
        forged = copy.deepcopy(descriptor)
        forged["capabilities"]["read"] = ["shared", "shared/"]
        forged["id"] = identity.compute_descriptor_id({k: v for k, v in forged.items() if k != "id"})
        with self.assertRaisesRegex(ForgeError, "must be sorted"):
            validate_descriptor(forged)

    def test_ancestor_minimized_descriptor_is_semantically_different(self) -> None:
        """A minimized descriptor loses the nested prefix and authorizes differently."""
        from service import add_principal, add_role, bind_role
        from identity import effective_descriptor
        from validation import capability_allowed

        add_principal(self.forge, "min")
        allow, deny = capability_grants(["shared/exception"], ["shared/exception"])
        allow["admin"] = ["shared/exception"]
        deny["admin"] = ["shared", "shared/exception/private"]
        add_role(self.forge, "min-role", allow, deny)
        bind_role(self.forge, "min-role", "principal", "min")
        descriptor = effective_descriptor(load_state(self.forge), "min", "refs/heads/main")
        self.assertIn("shared/exception/private", descriptor["restrictions"]["admin"])
        # Authorization with the true descriptor denies the private path.
        self.assertFalse(
            capability_allowed(
                descriptor["capabilities"]["admin"],
                descriptor["restrictions"]["admin"],
                b"shared/exception/private/base.txt",
            )
        )
        # A minimized version (missing the nested deny) would incorrectly allow it.
        minimized = copy.deepcopy(descriptor)
        minimized["restrictions"]["admin"] = ["shared"]
        self.assertTrue(
            capability_allowed(
                minimized["capabilities"]["admin"],
                minimized["restrictions"]["admin"],
                b"shared/exception/private/base.txt",
            )
        )


class AdministrativeRangeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        command("git", "init", "-q", "-b", "main", str(source), cwd=self.root)
        git(source, "config", "user.name", "Fixture")
        git(source, "config", "user.email", "fixture@example.test")
        for name in ("shared", "shared/allowed", "shared/denied"):
            directory = source / name
            directory.mkdir(parents=True)
            (directory / "base.txt").write_text(f"{name}\n")
        commit(source, "initial", 1_700_000_000)
        self.forge = self.root / "forge"
        forge.init_forge(self.forge, source, "main")
        provision_admin(self.forge)
        from service import add_principal, add_role, bind_role
        add_principal(self.forge, "scoped")
        allow, deny = capability_grants([], [])
        allow["admin"] = ["shared/allowed"]
        add_role(self.forge, "scoped-admin", allow, deny)
        bind_role(self.forge, "scoped-admin", "principal", "scoped")
        self.admin = self.root / "admin"
        command("git", "clone", "-q", str(forge.canonical_repo(self.forge)), str(self.admin))
        attach_admin_push(self.forge, self.admin)
        git(self.admin, "config", "user.name", "Admin")
        git(self.admin, "config", "user.email", "admin@example.test")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _push_admin_chain(self, commits: list[tuple[str, str | None]]) -> tuple[str, str]:
        old = oid(self.admin, "refs/heads/main")
        for path, contents in commits:
            target = self.admin / path
            if contents is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents)
            commit(self.admin, f"admin {path}", 1_700_100_000)
        new = oid(self.admin, "refs/heads/main")
        command("git", "push", "-q", "origin", f"{new}:refs/staging/admin", cwd=self.admin)
        return old, new

    def test_unauthorized_intermediate_transition_rejects_range(self) -> None:
        """A path changed and later restored still needs coverage for its transition."""
        old, new = self._push_admin_chain([
            ("shared/allowed/ok.txt", "ok\n"),
            ("shared/denied/bad.txt", "bad\n"),
            ("shared/denied/bad.txt", None),  # restored
        ])
        with self.assertRaisesRegex(ForgeError, "lacks admin coverage"):
            update_canonical(self.forge, old, new, "scoped")
        manifest, replay = repository_state(self.forge)
        self.assertEqual(old, manifest.canonical_tip)
        self.assertFalse(any(record["kind"] == "canonical_update" for record in replay.records[1:]))

    def test_authorized_multi_commit_range_accepted(self) -> None:
        old, new = self._push_admin_chain([
            ("shared/allowed/one.txt", "one\n"),
            ("shared/allowed/two.txt", "two\n"),
        ])
        update_canonical(self.forge, old, new, "scoped")
        manifest, replay = repository_state(self.forge)
        self.assertEqual(new, manifest.canonical_tip)
        record = wal_records(self.forge, "canonical_update")[-1]
        self.assertEqual("scoped", record["payload"]["principal_id"])
        self.assertEqual(record["payload"]["descriptor_id"], record["event_fingerprint"]["descriptor_id"])
        self.assertEqual(2, len(record["payload"]["commits"]))


class CapturedDescriptorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = TreeFixture()
        self.forge = self.fixture.root

    def tearDown(self) -> None:
        self.fixture.close()
        self.temporary.cleanup()

    def test_descriptor_id_survives_catalog_changes_and_index_recovery(self) -> None:
        """A canonical_update references its descriptor even after catalog changes."""
        admin = self.fixture.admin
        target = admin / "shared" / "new.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("new\n")
        new = commit(admin, "admin change", 1_700_100_000)
        old = git(admin, "rev-parse", "HEAD~1").stdout.decode().strip()
        command("git", "push", "-q", "origin", f"{new}:refs/staging/admin", cwd=admin)
        update_canonical(self.forge, old, new)

        # Wipe rebuildable catalog indexes.
        state = load_state(self.forge)
        state["transactions"] = {}
        state["changes"] = {}
        state["views"] = {}
        state["projection_jobs"] = {}
        state["last_indexed_seq"] = 0
        save_state(self.forge, state)

        record = wal_records(self.forge, "canonical_update")[-1]
        descriptor_id = record["payload"]["descriptor_id"]
        repository = Repository.open(self.forge)
        body = repository.get(f"descriptors/{descriptor_id}.json").body
        descriptor = json.loads(body)
        self.assertEqual(descriptor_id, descriptor["id"])
        check_invariants(self.forge)

    def test_fingerprint_changes_with_descriptor_id(self) -> None:
        """A canonical_update event ID includes the captured descriptor ID."""
        from wal import event_id, record_fingerprint

        repository_id = Repository.open(self.forge).repository_id
        without_descriptor = record_fingerprint(
            repository_id, "canonical_update",
            {
                "principal_id": "admin",
                "canonical_ref": "refs/heads/main",
                "canonical_old": "a" * 40,
                "canonical_new": "b" * 40,
            },
        )
        with_descriptor = record_fingerprint(
            repository_id, "canonical_update",
            {
                "principal_id": "admin",
                "descriptor_id": "vdesc-" + "c" * 64,
                "canonical_ref": "refs/heads/main",
                "canonical_old": "a" * 40,
                "canonical_new": "b" * 40,
            },
        )
        self.assertNotEqual(
            event_id(without_descriptor), event_id(with_descriptor)
        )


class DescriptorReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        command("git", "init", "-q", "-b", "main", str(source), cwd=self.root)
        git(source, "config", "user.name", "Fixture")
        git(source, "config", "user.email", "fixture@example.test")
        (source / "shared").mkdir()
        (source / "shared" / "base.txt").write_text("base\n")
        commit(source, "initial", 1_700_000_000)
        self.forge = self.root / "forge"
        forge.init_forge(self.forge, source, "main")
        provision_admin(self.forge)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _build_admin_update(self) -> tuple[str, str, str]:
        admin = self.root / "admin"
        command("git", "clone", "-q", str(forge.canonical_repo(self.forge)), str(admin))
        attach_admin_push(self.forge, admin)
        git(admin, "config", "user.name", "Admin")
        git(admin, "config", "user.email", "admin@example.test")
        target = admin / "shared" / "new.txt"
        target.write_text("new\n")
        new = commit(admin, "admin change", 1_700_100_000)
        old = git(admin, "rev-parse", "HEAD~1").stdout.decode().strip()
        command("git", "push", "-q", "origin", f"{new}:refs/staging/admin", cwd=admin)
        return old, new

    def test_missing_descriptor_fails_wal_replay(self) -> None:
        old, new = self._build_admin_update()
        update_canonical(self.forge, old, new)
        record = wal_records(self.forge, "canonical_update")[-1]
        descriptor_id = record["payload"]["descriptor_id"]
        repository = Repository.open(self.forge)
        repository.delete(f"descriptors/{descriptor_id}.json")
        with self.assertRaisesRegex(ForgeError, "referenced descriptor"):
            repository.load()

    def test_corrupted_descriptor_fails_wal_replay(self) -> None:
        old, new = self._build_admin_update()
        update_canonical(self.forge, old, new)
        record = wal_records(self.forge, "canonical_update")[-1]
        descriptor_id = record["payload"]["descriptor_id"]
        repository = Repository.open(self.forge)
        from object_store import Update
        key = f"descriptors/{descriptor_id}.json"
        current = repository.get(key)
        repository.put(key, b"corrupt", mode=Update(current.meta.version))
        with self.assertRaisesRegex(ForgeError, "descriptor"):
            repository.load()

    def test_tampered_descriptor_fails_replay(self) -> None:
        old, new = self._build_admin_update()
        update_canonical(self.forge, old, new)
        record = wal_records(self.forge, "canonical_update")[-1]
        descriptor_id = record["payload"]["descriptor_id"]
        repository = Repository.open(self.forge)
        from object_store import Update
        key = f"descriptors/{descriptor_id}.json"
        current = repository.get(key)
        forged = json.loads(current.body)
        forged["principal_id"] = "someone-else"
        # Keep the original descriptor ID but change the payload so the ID no
        # longer matches; WAL replay must fail closed.
        repository.put(key, canonical_json(forged), mode=Update(current.meta.version))
        with self.assertRaisesRegex(ForgeError, "descriptor"):
            repository.load()


class BackupRestoreDescriptorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        command("git", "init", "-q", "-b", "main", str(source), cwd=self.root)
        git(source, "config", "user.name", "Fixture")
        git(source, "config", "user.email", "fixture@example.test")
        (source / "shared").mkdir()
        (source / "shared" / "base.txt").write_text("base\n")
        commit(source, "initial", 1_700_000_000)
        self.forge = self.root / "forge"
        forge.init_forge(self.forge, source, "main")
        provision_admin(self.forge)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_backup_restore_retains_admin_descriptor(self) -> None:
        from store import backup_forge
        from tests.test_production_storage import _cleanup_directory

        admin = self.root / "admin"
        command("git", "clone", "-q", str(forge.canonical_repo(self.forge)), str(admin))
        attach_admin_push(self.forge, admin)
        git(admin, "config", "user.name", "Admin")
        git(admin, "config", "user.email", "admin@example.test")
        target = admin / "shared" / "new.txt"
        target.write_text("new\n")
        new = commit(admin, "admin change", 1_700_100_000)
        old = git(admin, "rev-parse", "HEAD~1").stdout.decode().strip()
        command("git", "push", "-q", "origin", f"{new}:refs/staging/admin", cwd=admin)
        update_canonical(self.forge, old, new)

        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup_directory, base)
        destination = base / "backup"
        restored = base / "restored"
        backup_forge(self.forge, destination)
        restore_forge(destination, restored)

        record = wal_records(restored, "canonical_update")[-1]
        descriptor_id = record["payload"]["descriptor_id"]
        self.assertTrue((restored / "object-store" / "repos" / Repository.open(restored).repository_id / "descriptors" / f"{descriptor_id}.json").exists())
        check_invariants(restored)


class ReceivedAtTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = TreeFixture()
        self.forge = self.fixture.root

    def tearDown(self) -> None:
        self.fixture.close()
        self.temporary.cleanup()

    def test_received_time_governs_timestamp_admission(self) -> None:
        """A future-dated commit beyond the captured skew window is rejected."""
        client = self.fixture.clients["alice"]
        target = client / "shared" / "future.txt"
        target.write_text("future\n")
        git(client, "add", "-A")
        future = int(time.time()) + identity.TIMESTAMP_SKEW_SECONDS + 60
        env = {
            "GIT_AUTHOR_DATE": f"{future} +0000",
            "GIT_COMMITTER_DATE": f"{future} +0000",
            "GIT_AUTHOR_NAME": "Alice",
            "GIT_AUTHOR_EMAIL": "alice@example.test",
            "GIT_COMMITTER_NAME": "Alice",
            "GIT_COMMITTER_EMAIL": "alice@example.test",
        }
        command("git", "commit", "-q", "-m", "future\n\nChange-Id: I" + "a" * 40, cwd=client, env=env)
        result = git(client, "push", "-q", "origin", "main", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("future-dated", result.stderr.decode(errors="replace"))


class CheckerFailureCategoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = TreeFixture()
        self.forge = self.fixture.root

    def tearDown(self) -> None:
        self.fixture.close()
        self.temporary.cleanup()

    def test_checker_uses_stable_categories(self) -> None:
        # Corrupt a pack to trigger a REPOSITORY failure.
        repository = Repository.open(self.forge)
        manifest, _version, _replay = repository.load()
        key = manifest.canonical_packs[0]["pack_key"]
        current = repository.get(key)
        from object_store import Update
        repository.put(key, b"corrupt", mode=Update(current.meta.version))
        with self.assertRaises(InvariantError) as context:
            check_invariants(self.forge)
        self.assertEqual("REPOSITORY", context.exception.category)

    def test_checker_redacts_hidden_content(self) -> None:
        # Corrupt a pack; the error message should not include hidden path contents.
        repository = Repository.open(self.forge)
        manifest, _version, _replay = repository.load()
        key = manifest.canonical_packs[0]["pack_key"]
        current = repository.get(key)
        from object_store import Update
        repository.put(key, b"corrupt", mode=Update(current.meta.version))
        with self.assertRaises(InvariantError) as context:
            check_invariants(self.forge)
        self.assertNotIn("secret", str(context.exception))


class CheckerReadOnlyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = TreeFixture()

    def tearDown(self) -> None:
        self.fixture.close()
        self.temporary.cleanup()

    def test_checker_does_not_mutate_filesystem_or_database(self) -> None:
        before = repository_snapshot(self.fixture.root)
        check_invariants(self.fixture.root)
        self.assertEqual(before, repository_snapshot(self.fixture.root))

    def test_checker_succeeds_with_disposable_caches_deleted(self) -> None:
        forge.canonical_repo(self.fixture.root).unlink()
        for name in self.fixture.policies:
            forge.view_repo(self.fixture.root, name).unlink()
        check_invariants(self.fixture.root)


if __name__ == "__main__":
    unittest.main()
