"""End-to-end forge integration over the Artifacts object-store adapter."""

from __future__ import annotations

import json
import os
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import forge
from errors import ForgeError
from identity import CAPABILITIES
from accounts import link_clerk_account
from repository_server import create_app
from http_server import add_token
from service import add_role, bind_role, managed_private_paths, set_path_privacy, transition_principal
from state import load_state
from object_store import Create
from object_store_artifacts import ArtifactsObjectStore, ArtifactsObjectStoreConfig
from object_store_fs import FilesystemObjectStore
from repository import Repository, clear_repository_cache, write_repository_config
from store import backup_forge, restore_forge
from tests.storage_helpers import repository_state
from tests.tree_harness import TreeFixture, build_commit, command, oid, tree_at
from layout import intake_repo


def _uri(name: str) -> str:
    return f"artifacts+https://0123456789abcdef0123456789abcdef.artifacts.cloudflare.net/git/test/{name}.git"


class ArtifactsIntegrationTest(unittest.TestCase):
    def test_init_selects_artifacts_from_uri(self) -> None:
        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        base = Path(tempfile.mkdtemp(prefix="bastion-artifacts-init-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(base, ignore_errors=True))
        remote = base / "init-artifacts.git"
        command("git", "init", "--bare", "-q", str(remote))
        uri = _uri("init")
        remote_url = uri.removeprefix("artifacts+")

        with patch.object(
            ArtifactsObjectStore,
            "from_config",
            new=classmethod(
                lambda cls, config, cache_dir: ArtifactsObjectStore.for_test_remote(
                    remote, cache_dir, branch=config.branch
                )
            ),
        ):
            root = base / "initialized"
            forge.init_forge(root, fixture.source, "main", object_store=uri)
            config = json.loads((root / "repository.json").read_text())
            self.assertEqual("artifacts", config["object_store"]["kind"])
            self.assertEqual(remote_url, config["object_store"]["remote"])
            manifest, _version, _replay = Repository.open(root).load(use_cache=False)
            self.assertEqual("refs/heads/main", manifest.canonical_ref)

    def test_push_backup_restore_and_restart_keep_repository_state_and_views(self) -> None:
        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        base = Path(tempfile.mkdtemp(prefix="bastion-artifacts-integration-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(base, ignore_errors=True))

        source_remote = base / "source-artifacts.git"
        target_remote = base / "target-artifacts.git"
        command("git", "init", "--bare", "-q", str(source_remote))
        command("git", "init", "--bare", "-q", str(target_remote))
        source_uri = _uri("source")
        target_uri = _uri("target")
        remotes = {source_uri.removeprefix("artifacts+"): source_remote,
                   target_uri.removeprefix("artifacts+"): target_remote}

        def local_adapter(config: ArtifactsObjectStoreConfig, cache_dir: Path):
            return ArtifactsObjectStore.for_test_remote(
                remotes[config.remote], cache_dir, branch=config.branch
            )

        def enabled():
            return patch.object(
                ArtifactsObjectStore,
                "from_config",
                new=classmethod(lambda cls, config, cache_dir: local_adapter(config, cache_dir)),
            )

        # Re-home the harness's initialized repository to an Artifacts test
        # remote while keeping its repository ID, catalogs, and projections.
        original = Repository.open(fixture.root)
        old_store = FilesystemObjectStore(fixture.root / "object-store")
        artifacts = ArtifactsObjectStore.for_test_remote(
            source_remote, base / "source-cache", branch="bastion-storage"
        )
        for item in old_store.list_prefix(f"repos/{original.repository_id}/"):
            artifacts.put(item.key, old_store.get(item.key).body, mode=Create())
        config = ArtifactsObjectStoreConfig(
            remote=source_uri.removeprefix("artifacts+"),
        )
        (fixture.root / "repository.json").unlink()
        write_repository_config(
            fixture.root,
            original.repository_id,
            "main",
            artifacts_config=config,
        )

        with enabled():
            def submit(view: str, parent: str, new: str) -> None:
                with patch.dict(
                    os.environ,
                    {
                        "BASTION_VIEW": view,
                        "BASTION_PRINCIPAL_ID": view,
                        "BASTION_TRANSPORT": "test",
                    },
                    clear=False,
                ):
                    forge.pre_receive(
                        fixture.root,
                        view,
                        f"{parent} {new} refs/heads/main\n".encode(),
                    )

            client = fixture.clients["alice"]
            parent = oid(client, "refs/heads/main")
            new = build_commit(
                client,
                parent,
                {b"shared/artifacts-push.txt": (b"100644", b"Artifacts payload\n")},
                b"Artifacts integration push\n",
                1_700_000_700,
            )
            command(
                "git", "--git-dir", str(intake_repo(fixture.root, "alice")),
                "fetch", "-q", str(client), "refs/heads/main",
            )
            submit("alice", parent, new)
            forge.drain_projection_jobs(fixture.root)

            # A canonical-only change must keep the other user's projected
            # ref stable and leave the hidden blob out of its object database.
            bob_remote = forge.view_repo(fixture.root, "bob")
            bob_ref_before_private = forge.ref(bob_remote, "refs/heads/main")
            private_oid = forge.output(
                forge.canonical_repo(fixture.root),
                "rev-parse",
                "main:team-a/private.txt",
            )
            hidden_object_oid = forge.output(
                forge.canonical_repo(fixture.root), "rev-parse", "main:hidden/secret.txt"
            )
            parent = oid(client, "refs/heads/main")
            private_commit = build_commit(
                client,
                parent,
                {b"team-a/private.txt": (b"100644", b"Alice only\n")},
                b"Alice private update\n",
                1_700_000_701,
            )
            command(
                "git", "--git-dir", str(intake_repo(fixture.root, "alice")),
                "fetch", "-q", str(client), "refs/heads/main",
            )
            submit("alice", parent, private_commit)
            forge.drain_projection_jobs(fixture.root)
            self.assertEqual(bob_ref_before_private, forge.ref(bob_remote, "refs/heads/main"))
            for hidden_oid in (private_oid, hidden_object_oid):
                self.assertNotEqual(
                    0,
                    forge.git(bob_remote, "cat-file", "-e", hidden_oid, check=False).returncode,
                )

            # Exercise a policy epoch transition against the same remote.
            # The target's projection keeps its branch ref while rebuilding
            # without newly hidden objects.
            allow = {capability: [] for capability in CAPABILITIES}
            deny = {capability: [] for capability in CAPABILITIES}
            allow["admin"] = [""]
            add_role(fixture.root, "alice-owner", allow, deny)
            bind_role(fixture.root, "alice-owner", "principal", "alice")
            transition_principal(fixture.root, "alice")
            bob_before = Repository.open(fixture.root).load()[2].active_views["bob"]
            bob_ref_before = forge.ref(bob_remote, "refs/heads/main")
            self.assertNotEqual(
                0,
                forge.git(bob_remote, "cat-file", "-e", hidden_object_oid, check=False).returncode,
            )
            set_path_privacy(fixture.root, "alice", "bob", "shared", True)
            forge.drain_projection_jobs(fixture.root)
            source_manifest, source_replay = repository_state(fixture.root)
            bob_after = source_replay.active_views["bob"]
            self.assertEqual(bob_before["projection_epoch"] + 1, bob_after["projection_epoch"])
            self.assertEqual("refs/heads/main", bob_after["projected_ref"])
            bob_ref_after = forge.ref(bob_remote, "refs/heads/main")
            self.assertNotEqual("0" * 40, bob_ref_after)
            self.assertNotEqual(bob_ref_before, bob_ref_after)
            bob_tree = tree_at(bob_remote, "refs/heads/main")
            self.assertFalse(any(path.startswith(b"shared/") for path in bob_tree))
            self.assertNotIn(b"hidden/secret.txt", bob_tree)
            self.assertNotEqual(
                0,
                forge.git(bob_remote, "cat-file", "-e", hidden_object_oid, check=False).returncode,
            )
            self.assertEqual(["shared"], managed_private_paths(load_state(fixture.root), "bob"))

            from state import locked, save_state

            with locked(fixture.root) as state:
                state["principals"]["alice"]["emails"] = ["alice@example.test"]
                save_state(fixture.root, state)
            link_clerk_account(
                fixture.root, "user_artifactsAlice", ["alice@example.test"]
            )
            scoped_token = add_token(fixture.root, "alice", scopes=["fetch", "ui"])

            source_manifest, source_replay = repository_state(fixture.root)
            expected_canonical = tree_at(
                forge.canonical_repo(fixture.root), "refs/heads/main"
            )
            expected_view = tree_at(
                forge.view_repo(fixture.root, "alice"), "refs/heads/main"
            )
            self.assertIn(b"shared/artifacts-push.txt", expected_canonical)
            self.assertIn(b"shared/artifacts-push.txt", expected_view)

            backup = base / "backup"
            restored_root = base / "restored"
            backup_forge(fixture.root, backup)
            backup_metadata = json.loads((backup / "manifest.json").read_text())
            self.assertEqual("artifacts", backup_metadata["source_object_store_kind"])
            restore_forge(backup, restored_root, object_store=target_uri)

            restored_manifest, restored_replay = repository_state(restored_root)
            self.assertEqual(source_manifest.to_dict(), restored_manifest.to_dict())
            self.assertEqual(
                [row["event_id"] for row in source_replay.records],
                [row["event_id"] for row in restored_replay.records],
            )
            self.assertEqual(
                expected_canonical,
                tree_at(forge.canonical_repo(restored_root), "refs/heads/main"),
            )
            self.assertEqual(
                expected_view,
                tree_at(forge.view_repo(restored_root, "alice"), "refs/heads/main"),
            )
            self.assertFalse(
                any(
                    path.startswith(b"shared/")
                    for path in tree_at(forge.view_repo(restored_root, "bob"), "refs/heads/main")
                )
            )
            restored_bob = forge.view_repo(restored_root, "bob")
            self.assertNotEqual(
                0,
                forge.git(
                    restored_bob, "cat-file", "-e", hidden_object_oid, check=False
                ).returncode,
            )
            restored_replay = repository_state(restored_root)[1]
            self.assertEqual(
                source_replay.active_views["bob"]["projection_epoch"],
                restored_replay.active_views["bob"]["projection_epoch"],
            )
            restored_catalog = load_state(restored_root)
            self.assertEqual(["shared"], managed_private_paths(restored_catalog, "bob"))
            self.assertIn("alice-owner", restored_catalog["roles"])
            self.assertIn("principal:alice:alice-owner", restored_catalog["role_bindings"])
            self.assertEqual(
                "user_artifactsAlice",
                restored_catalog["principals"]["alice"]["clerk_user_id"],
            )
            client_app = create_app(restored_root).test_client()
            self.assertEqual(
                200,
                client_app.get(
                    "/api/v1/session",
                    headers={"Authorization": f"Bearer {scoped_token}"},
                ).status_code,
            )
            basic = base64.b64encode(f"alice:{scoped_token}".encode()).decode()
            advertisement = client_app.get(
                "/alice.git/info/refs?service=git-upload-pack",
                headers={"Authorization": f"Basic {basic}"},
            )
            self.assertEqual(200, advertisement.status_code)
            self.assertIn(b"refs/heads/main", advertisement.data)
            restored_config = json.loads((restored_root / "repository.json").read_text())
            self.assertEqual("artifacts", restored_config["object_store"]["kind"])
            self.assertEqual(target_uri.removeprefix("artifacts+"), restored_config["object_store"]["remote"])
            self.assertEqual(
                "BASTION_ARTIFACTS_TOKEN", restored_config["object_store"]["token_env"]
            )
            self.assertNotIn("Authorization", (restored_root / "repository.json").read_text())

            conflict_root = base / "restore-conflict"
            with self.assertRaisesRegex(ForgeError, "Artifacts repository namespace"):
                restore_forge(backup, conflict_root, object_store=target_uri)
            self.assertFalse(conflict_root.exists())

            # A fresh repository handle after cache eviction behaves like a
            # process restart and still reads the authoritative remote branch.
            clear_repository_cache()
            reopened = Repository.open(restored_root)
            after_restart, _version, _replay = reopened.load(use_cache=False)
            self.assertEqual(source_manifest.to_dict(), after_restart.to_dict())


if __name__ == "__main__":
    unittest.main()
