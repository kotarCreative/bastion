from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import forge
from errors import ForgeError
from invariants import check_invariants
from service import sync_view
from state import load_state, save_state, write_receiver_marker
from worker import claim_projection_job


REPO_ROOT = Path(__file__).resolve().parents[1]
FORGE_PY = REPO_ROOT / "forge.py"


def command(*args: str, cwd: Path | None = None, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ | (env or {}))
    if check and result.returncode:
        raise AssertionError(f"{' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result


def git(repo: Path, *args: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return command("git", *args, cwd=repo, check=check, env=env)


def commit(repo: Path, message: str, timestamp: int) -> str:
    env = {
        "GIT_AUTHOR_DATE": f"{timestamp} +0000",
        "GIT_COMMITTER_DATE": f"{timestamp} +0000",
    }
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, env=env)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def authoritative_refs(root: Path) -> dict[str, str]:
    refs: dict[str, str] = {}
    repositories = {"canonical": forge.canonical_repo(root)}
    for path in sorted((root / "views").glob("*.git")):
        repositories[path.name[:-4]] = path
    for name, repo in repositories.items():
        output = command("git", "--git-dir", str(repo), "for-each-ref").stdout
        refs[name] = output.strip()
    return refs


def hook_files(root: Path) -> dict[str, bytes]:
    hooks: dict[str, bytes] = {}
    for path in sorted((root / "views").glob("*.git/hooks/*")):
        if path.name in {"pre-receive", "post-receive"}:
            hooks[f"{path.parent.parent.name}/{path.name}"] = path.read_bytes()
    return hooks


class MigrationFixture:
    """A settled forge with source pushes, an admin update, and fanout."""

    def __init__(self, root: Path) -> None:
        self.root = root
        source = root / "source"
        git(root, "init", "-q", "-b", "main", str(source))
        git(source, "config", "user.name", "Fixture")
        git(source, "config", "user.email", "fixture@example.test")
        for name in ("shared", "team-a"):
            directory = source / name
            directory.mkdir()
            (directory / "base.txt").write_text(f"{name}\n")
        commit(source, "initial", 1_700_000_000)

        self.forge = root / "forge"
        from tests.identity_harness import (
            add_view as fixture_add_view,
            provision_admin,
            update_canonical,
        )

        forge.init_forge(self.forge, source, "main")
        fixture_add_view(self.forge, "alice", ["shared", "team-a"], ["shared", "team-a"], "main")
        fixture_add_view(self.forge, "bob", ["shared"], ["shared"], "main")

        self.alice = root / "alice"
        command("git", "clone", "-q", "--no-local", str(forge.view_repo(self.forge, "alice")), str(self.alice))
        git(self.alice, "config", "user.name", "Alice")
        git(self.alice, "config", "user.email", "alice@example.test")
        (self.alice / "shared" / "change.txt").write_text("change\n")
        self.change_id = "I" + "1" * 40
        commit(self.alice, f"change\n\nChange-Id: {self.change_id}", 1_700_000_100)
        git(self.alice, "push", "-q", "origin", "main")

        admin = root / "admin"
        command("git", "clone", "-q", str(forge.canonical_repo(self.forge)), str(admin))
        git(admin, "config", "user.name", "Admin")
        git(admin, "config", "user.email", "admin@example.test")
        secret = admin / "hidden-secret"
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("s3cret\n")
        new = commit(admin, "hidden-only admin change\n\nChange-Id: I" + "2" * 40, 1_700_000_200)
        old = command("git", "rev-parse", "HEAD~1", cwd=admin).stdout.strip()
        command("git", "push", "-q", "origin", f"{new}:refs/staging/migration-fixture", cwd=admin)
        provision_admin(self.forge)
        update_canonical(self.forge, old, new)
        forge.drain_projection_jobs(self.forge)

    def state_bytes(self) -> bytes:
        return (self.forge / "state.json").read_bytes()


def downgrade_to_v1(forge_root: Path) -> None:
    """Rewrite a quiescent version-3 state into its equivalent version-1 form."""
    import identity

    state = load_state(forge_root)
    descriptors = state.get("view_descriptors", {})
    legacy: dict = {
        "version": 1,
        "canonical_branch": state["canonical_branch"],
        "views": {},
        "transactions": {},
        "changes": {},
        "outbox": {},
        "projection_jobs": {},
        "next_transaction_sequence": state["next_transaction_sequence"],
    }
    strip = ("descriptor_id", "descriptor_generation")
    for name, view in sorted(state["views"].items()):
        descriptor = descriptors[view["descriptor_id"]]
        read = list(descriptor["capabilities"]["read"])
        write_union: list[str] = []
        for capability in ("create", "modify", "delete", "mode", "symlink"):
            write_union.extend(descriptor["capabilities"][capability])
        legacy["views"][name] = {
            "principal": view["principal_id"],
            "canonical_ref": view["canonical_ref"],
            "projected_ref": view["projected_ref"],
            "read": read,
            "write": identity.minimize_prefixes(
                identity.intersect_prefixes(read, write_union)
            ),
            "canonical_frontier": view["canonical_frontier"],
            "projected_tip": view["projected_tip"],
            "anchors": {
                key: {field: value for field, value in anchor.items() if field not in strip}
                for key, anchor in view["anchors"].items()
            },
        }
    for identifier, transaction in sorted(state["transactions"].items()):
        record = {
            key: value
            for key, value in transaction.items()
            if key not in set(strip) | {"target_descriptors"}
        }
        if transaction.get("kind") != "canonical_update":
            record["principal"] = record.pop("principal_id")
        else:
            # Version-1 administrative updates carried no acting identity.
            record.pop("principal_id", None)
        legacy["transactions"][identifier] = record
    for identifier, event in sorted(state["outbox"].items()):
        legacy["outbox"][identifier] = {
            key: value for key, value in event.items() if key != "target_descriptors"
        }
    for identifier, job in sorted(state["projection_jobs"].items()):
        legacy["projection_jobs"][identifier] = {
            key: value for key, value in job.items() if key not in strip
        }
    for change_id, change in sorted(state["changes"].items()):
        record = dict(change)
        owner = record.pop("owner_principal_id", None)
        if owner is not None:
            record["owner"] = owner
        record["revisions"] = [
            {field: value for field, value in revision.items() if field not in strip}
            for revision in change["revisions"]
        ]
        legacy["changes"][change_id] = record
    save_state(forge_root, legacy)


class MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = MigrationFixture(self.root)
        self.forge = self.fixture.forge

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _downgrade(self) -> None:
        downgrade_to_v1(self.forge)
        self.assertEqual(1, load_state(self.forge)["version"])
        check_invariants(self.forge)

    def _migration_evidence_preserved(self, before_state: dict) -> None:
        after = load_state(self.forge)
        self.assertEqual(3, after["version"])
        self.assertEqual(
            sorted(before_state["transactions"]), sorted(after["transactions"])
        )
        self.assertEqual(sorted(before_state["outbox"]), sorted(after["outbox"]))
        self.assertEqual(
            sorted(before_state["projection_jobs"]),
            sorted(after["projection_jobs"]),
        )
        self.assertEqual(
            sorted(before_state["changes"]), sorted(after["changes"])
        )
        for change_id, change in before_state["changes"].items():
            migrated = after["changes"][change_id]
            self.assertEqual(change.get("owner"), migrated.get("owner_principal_id"))
            self.assertEqual(change["canonical_ref"], migrated.get("canonical_ref"))

    def test_quiescent_v1_forge_migrates_without_changing_git_data(self) -> None:
        self._downgrade()
        before_refs = authoritative_refs(self.forge)
        before_hooks = hook_files(self.forge)
        before_history = command(
            "git",
            "--git-dir",
            str(forge.canonical_repo(self.forge)),
            "rev-list",
            "refs/heads/main",
        ).stdout
        before_state = load_state(self.forge)
        anchors_before = {
            name: dict(view["anchors"]) for name, view in before_state["views"].items()
        }

        from migration import migrate_identities

        migrate_identities(self.forge)

        self.assertEqual(before_refs, authoritative_refs(self.forge))
        self.assertEqual(before_hooks, hook_files(self.forge))
        self.assertEqual(before_history, command("git", "--git-dir", str(forge.canonical_repo(self.forge)), "rev-list", "refs/heads/main").stdout)
        self._migration_evidence_preserved(before_state)
        for name, anchors in anchors_before.items():
            migrated = load_state(self.forge)["views"][name]["anchors"]
            self.assertEqual(sorted(anchors), sorted(migrated))
            for projected, anchor in anchors.items():
                self.assertEqual(anchor["canonical"], migrated[projected]["canonical"])
                self.assertEqual(anchor["kind"], migrated[projected]["kind"])
                self.assertEqual(1, migrated[projected]["descriptor_generation"])
        check_invariants(self.forge)

        # Every old view becomes its own user principal, even bot-like names.
        principals = load_state(self.forge)["principals"]
        self.assertIn("alice", principals)
        self.assertEqual("user", principals["alice"]["kind"])

    def test_second_migration_is_a_byte_preserving_noop(self) -> None:
        self._downgrade()
        from migration import migrate_identities

        migrate_identities(self.forge)
        first = self.fixture.state_bytes()
        migrate_identities(self.forge)
        self.assertEqual(first, self.fixture.state_bytes())

    def test_migration_from_cli_is_silent(self) -> None:
        self._downgrade()
        result = command("python3", str(FORGE_PY), "migrate-identities", str(self.forge), check=False)
        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual("", result.stderr)

    def test_mutations_reject_version_one_before_recovery(self) -> None:
        self._downgrade()
        before = self.fixture.state_bytes()
        with self.assertRaisesRegex(ForgeError, "forge state version 1 requires migrate-identities"):
            sync_view(self.forge, "alice")
        self.assertEqual(before, self.fixture.state_bytes())
        rejected = command("python3", str(FORGE_PY), "add-principal", str(self.forge), "carol", check=False)
        self.assertEqual(1, rejected.returncode)
        self.assertIn("requires migrate-identities", rejected.stderr)
        self.assertEqual(before, self.fixture.state_bytes())

    def _prepare_unconfirmed_source(self) -> None:
        """Leave one genuine source-unconfirmed transaction behind."""
        from state import locked

        bob = self.root / "bob-raw"
        command("git", "clone", "-q", "--no-local", str(forge.view_repo(self.forge, "bob")), str(bob))
        git(bob, "config", "user.name", "Bob")
        git(bob, "config", "user.email", "bob@example.test")
        old = command("git", "rev-parse", "HEAD", cwd=bob).stdout.strip()
        (bob / "shared" / "unconfirmed.txt").write_text("unconfirmed\n")
        new = commit(bob, "unconfirmed push", 1_700_000_250)
        forge.run("git", "--git-dir", str(forge.view_repo(self.forge, "bob")), "fetch", "-q", str(bob), new)
        forge.pre_receive(self.forge, "bob", f"{old} {new} refs/heads/main\n".encode())
        state = load_state(self.forge)
        transaction = next(tx for tx in state["transactions"].values() if tx.get("view") == "bob" and tx["projected_new"] == new)
        (self.forge / "receivers" / transaction["id"]).unlink(missing_ok=True)
        with locked(self.forge):
            pass
        state = load_state(self.forge)
        self.assertEqual(
            "source_unconfirmed",
            state["transactions"][transaction["id"]]["status"],
        )

    def _prepare_pending_jobs(self, name: str, path_name: str, contents: str) -> None:
        admin = self.root / name
        command("git", "clone", "-q", str(forge.canonical_repo(self.forge)), str(admin))
        git(admin, "config", "user.name", "Admin")
        git(admin, "config", "user.email", "admin@example.test")
        target = admin / path_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
        new = commit(admin, f"late {path_name} change", 1_700_000_300)
        old = command("git", "rev-parse", "HEAD~1", cwd=admin).stdout.strip()
        command("git", "push", "-q", "origin", f"{new}:refs/staging/migration-{name}", cwd=admin)
        from tests.identity_harness import provision_admin, update_canonical

        provision_admin(self.forge)
        update_canonical(self.forge, old, new)

    def test_unsettled_transactions_reject_migration(self) -> None:
        self._prepare_unconfirmed_source()
        self._downgrade()
        from migration import migrate_identities

        before = self.fixture.state_bytes()
        with self.assertRaisesRegex(ForgeError, "quiescence"):
            migrate_identities(self.forge)
        self.assertEqual(before, self.fixture.state_bytes())
        self.assertEqual(1, load_state(self.forge)["version"])

    def test_incomplete_jobs_reject_migration(self) -> None:
        self._prepare_pending_jobs("admin-late", "shared/late.txt", "late\n")
        from state import locked

        with locked(self.forge):
            pass
        state = load_state(self.forge)
        self.assertTrue(any(job["status"] == "pending" for job in state["projection_jobs"].values()))
        self._downgrade()

        from migration import migrate_identities

        before = self.fixture.state_bytes()
        with self.assertRaisesRegex(ForgeError, "requires quiescence"):
            migrate_identities(self.forge)
        self.assertEqual(before, self.fixture.state_bytes())
        self.assertEqual(1, load_state(self.forge)["version"])

    def test_active_lease_rejects_migration(self) -> None:
        # Any checker-valid leased or pending job implies a non-terminal
        # transaction, so quiescence is violated either way.  Deeper
        # job-level checks remain as defense in depth.
        self._prepare_pending_jobs("admin-lease", "shared/lease.txt", "lease\n")
        from state import locked

        with locked(self.forge):
            pass
        claimed = claim_projection_job(self.forge)
        self.assertIsNotNone(claimed)
        self.assertEqual("running", load_state(self.forge)["projection_jobs"][claimed["id"]]["status"])
        self._downgrade()

        from migration import migrate_identities

        before = self.fixture.state_bytes()
        with self.assertRaisesRegex(ForgeError, "requires quiescence"):
            migrate_identities(self.forge)
        self.assertEqual(before, self.fixture.state_bytes())
        self.assertEqual(1, load_state(self.forge)["version"])

    def test_receiver_markers_reject_migration(self) -> None:
        self._downgrade()
        state = load_state(self.forge)
        transaction = next(iter(state["transactions"].values()))
        write_receiver_marker(self.forge, transaction["id"], os.getpid())
        try:
            from migration import migrate_identities

            before = self.fixture.state_bytes()
            with self.assertRaisesRegex(ForgeError, "receiver markers"):
                migrate_identities(self.forge)
            self.assertEqual(before, self.fixture.state_bytes())
            self.assertEqual(1, load_state(self.forge)["version"])
        finally:
            (self.forge / "receivers" / transaction["id"]).unlink(missing_ok=True)

    def test_kill_during_save_leaves_complete_v1_or_v2_state(self) -> None:
        import time

        self._downgrade()
        for checkpoint_name, expected_versions in (
            ("state.temporary_fsynced", {1}),
            ("state.replaced", {3}),
        ):
            with self.subTest(checkpoint=checkpoint_name):
                # Rebuild a pristine quiescent v1 forge for each kill point.
                self.tearDown()
                self.temporary = tempfile.TemporaryDirectory()
                self.root = Path(self.temporary.name)
                self.fixture = MigrationFixture(self.root)
                self.forge = self.fixture.forge
                downgrade_to_v1(self.forge)

                control = self.root / "control"
                control.mkdir(exist_ok=True)
                marker = control / "marker.json"
                environment = os.environ | {
                    "BASTION_TEST_FAILPOINT": checkpoint_name,
                    "BASTION_TEST_FAILPOINT_ROOT": str(self.forge.resolve()),
                    "BASTION_TEST_FAILPOINT_MARKER": str(marker),
                }
                process = subprocess.Popen(
                    [sys.executable, str(FORGE_PY), "migrate-identities", str(self.forge)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=environment,
                )
                deadline = time.monotonic() + 30
                while not marker.exists():
                    if process.poll() is not None:
                        self.fail("migration exited before reaching the failpoint")
                    if time.monotonic() > deadline:
                        self.fail("timeout waiting for failpoint marker")
                    time.sleep(0.01)
                os.kill(process.pid, 9)
                process.wait(timeout=10)

                raw = self.fixture.state_bytes()
                state = json.loads(raw)
                self.assertIn(state["version"], expected_versions)
                check_invariants(self.forge)


if __name__ == "__main__":
    unittest.main()
