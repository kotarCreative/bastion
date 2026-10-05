"""Cloudflare Artifacts object-store tests using real bare Git remotes."""

from __future__ import annotations

import os
import hashlib
import multiprocessing
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from object_store import (
    AmbiguousWrite,
    Create,
    NotModified,
    ObjectNotFound,
    PreconditionFailed,
    StorageFailure,
    Update,
)
from object_store_artifacts import (
    ArtifactsObjectStore,
    ArtifactsObjectStoreConfig,
    parse_artifacts_uri,
)


def _put_in_process(remote: str, cache: str, key: str, output) -> None:
    try:
        store = ArtifactsObjectStore.for_test_remote(remote, Path(cache))
        output.put((key, store.put(key, key.encode(), mode=Create()).version.value))
    except BaseException as error:
        output.put((key, f"ERROR:{type(error).__name__}:{error}"))


def git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
    ).stdout


class ArtifactsObjectStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, stdout=subprocess.DEVNULL)
        self.branch = "bastion-storage"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def store(self, name: str = "cache") -> ArtifactsObjectStore:
        return ArtifactsObjectStore.for_test_remote(self.remote, self.root / name, branch=self.branch)

    def test_empty_remote_create_get_head_and_not_modified(self) -> None:
        store = self.store()
        self.assertIsNone(store.head("repos/abc/manifest.json"))
        created = store.put("repos/abc/manifest.json", b"manifest", mode=Create())
        self.assertEqual("manifest", store.get("repos/abc/manifest.json").body.decode())
        self.assertEqual(created, store.head("repos/abc/manifest.json"))
        with self.assertRaises(NotModified):
            store.get("repos/abc/manifest.json", if_none_match=created.version)

    def test_range_list_and_conditional_delete(self) -> None:
        store = self.store()
        meta = store.put("repos/id/a", b"abcdef", mode=Create())
        store.put("repos/id/b", b"xyz", mode=Create())
        self.assertEqual(b"bcd", store.get("repos/id/a", byte_range=(1, 3)).body)
        self.assertEqual(["repos/id/a", "repos/id/b"], [m.key for m in store.list_prefix("repos/id/")])
        with self.assertRaises(ValueError):
            store.get("repos/id/a", byte_range=(0, 6))
        with self.assertRaises(PreconditionFailed):
            store.delete("repos/id/a", if_version=type(meta.version)("wrong"))
        store.delete("repos/id/a", if_version=meta.version)
        self.assertIsNone(store.head("repos/id/a"))
        with self.assertRaises(ObjectNotFound):
            store.get("repos/id/a")

    def test_large_objects_are_chunked_below_artifacts_blob_limit(self) -> None:
        store = self.store()
        body = (bytes(range(251)) * ((33 * 1024 * 1024 // 251) + 1))[: 33 * 1024 * 1024 + 17]
        created = store.put("large/key", body, mode=Create())
        self.assertEqual(len(body), store.head("large/key").size)
        start = 8 * 1024 * 1024 - 3
        end = 8 * 1024 * 1024 + 11
        self.assertEqual(body[start : end + 1], store.get("large/key", byte_range=(start, end)).body)
        fetched = store.get("large/key")
        self.assertEqual(body, fetched.body)
        self.assertEqual(hashlib.sha256(body).hexdigest(), hashlib.sha256(fetched.body).hexdigest())

        commit = git("--git-dir", str(self.remote), "rev-parse", f"refs/heads/{self.branch}").decode().strip()
        paths = git("--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", commit).decode().splitlines()
        chunk_paths = [path for path in paths if path.endswith(".chunk")]
        self.assertEqual(5, len(chunk_paths))
        sizes = [
            int(git("--git-dir", str(self.remote), "cat-file", "-s", git("--git-dir", str(self.remote), "rev-parse", f"{commit}:{path}").decode().strip()))
            for path in chunk_paths
        ]
        self.assertLessEqual(max(sizes), 32_000_000)
        updated = store.put("large/key", b"small", mode=Update(created.version))
        self.assertNotEqual(created.version, updated.version)
        tree = git("--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "refs/heads/" + self.branch).decode().splitlines()
        self.assertEqual(1, sum(path.endswith(".chunk") for path in tree))
        self.assertEqual(b"small", store.get("large/key").body)

    def test_chunk_checksum_detects_remote_corruption(self) -> None:
        store = self.store()
        store.put("k", b"original", mode=Create())
        remote_head = git("--git-dir", str(self.remote), "rev-parse", f"refs/heads/{self.branch}").decode().strip()
        path = store._chunk_path(store._paths("k")[0], 0)
        with tempfile.NamedTemporaryFile() as index:
            env = os.environ.copy()
            env["GIT_DIR"] = str(self.remote)
            env["GIT_INDEX_FILE"] = index.name
            env["GIT_WORK_TREE"] = str(self.root)
            subprocess.run(["git", "read-tree", remote_head], env=env, check=True)
            corrupt_oid = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"], input=b"altered!", env=env,
                stdout=subprocess.PIPE, check=True,
            ).stdout.decode().strip()
            subprocess.run(
                ["git", "update-index", "--add", "--cacheinfo", "100644", corrupt_oid, path],
                env=env, check=True,
            )
            tree = subprocess.run(["git", "write-tree"], env=env, stdout=subprocess.PIPE, check=True).stdout.decode().strip()
            corrupt_commit = subprocess.run(
                ["git", "commit-tree", tree, "-p", remote_head], env=env,
                input=b"corrupt test\n", stdout=subprocess.PIPE, check=True,
            ).stdout.decode().strip()
            subprocess.run(
                ["git", "update-ref", f"refs/heads/{self.branch}", corrupt_commit, remote_head],
                env=env, check=True,
            )
        with self.assertRaisesRegex(StorageFailure, "checksum"):
            store.get("k")

    def test_separate_instances_have_one_create_winner(self) -> None:
        stores = [self.store("cache-one"), self.store("cache-two")]
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def create(store: ArtifactsObjectStore, body: bytes) -> None:
            barrier.wait()
            try:
                store.put("manifest.json", body, mode=Create())
                outcomes.append("won")
            except PreconditionFailed:
                outcomes.append("lost")

        left = threading.Thread(target=create, args=(stores[0], b"one"))
        right = threading.Thread(target=create, args=(stores[1], b"two"))
        left.start(); right.start(); left.join(); right.join()
        self.assertCountEqual(["won", "lost"], outcomes)
        self.assertIn(stores[0].get("manifest.json").body, {b"one", b"two"})

    def test_unrelated_concurrent_writes_retry(self) -> None:
        stores = [self.store("cache-left"), self.store("cache-right")]
        barrier = threading.Barrier(2)
        original_refresh = [store._refresh for store in stores]
        for index, store in enumerate(stores):
            first = True

            def synchronized_refresh(index=index, first_ref=[True]):
                result = original_refresh[index]()
                if first_ref[0]:
                    first_ref[0] = False
                    barrier.wait()
                return result

            store._refresh = synchronized_refresh  # type: ignore[method-assign]
        failures: list[BaseException] = []

        def put(store: ArtifactsObjectStore, key: str) -> None:
            try:
                store.put(key, key.encode(), mode=Create())
            except BaseException as error:
                failures.append(error)

        threads = [
            threading.Thread(target=put, args=(stores[0], "a")),
            threading.Thread(target=put, args=(stores[1], "b")),
        ]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual([], failures)
        self.assertEqual(b"a", stores[0].get("a").body)
        self.assertEqual(b"b", stores[1].get("b").body)

    def test_shared_cache_serializes_real_process_constructors_and_writes(self) -> None:
        context = multiprocessing.get_context("fork")
        output = context.Queue()
        cache = self.root / "shared-process-cache"
        processes = [
            context.Process(target=_put_in_process, args=(str(self.remote), str(cache), key, output))
            for key in ("process/a", "process/b")
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=30)
            self.assertFalse(process.is_alive(), "Git object-store process did not finish")
            self.assertEqual(0, process.exitcode)
        results = [output.get(timeout=2) for _ in processes]
        self.assertTrue(all(not version.startswith("ERROR:") for _, version in results), results)
        store = self.store("shared-process-cache")
        self.assertEqual(b"process/a", store.get("process/a").body)
        self.assertEqual(b"process/b", store.get("process/b").body)

    def test_stale_update_and_delete_after_aba_are_rejected(self) -> None:
        first = self.store("cache-first")
        second = self.store("cache-second")
        v1 = first.put("k", b"same", mode=Create())
        v2 = second.put("k", b"changed", mode=Update(v1.version))
        v3 = first.put("k", b"same", mode=Update(v2.version))
        self.assertEqual(b"same", second.get("k").body)
        self.assertNotEqual(v1.version, v3.version)
        with self.assertRaises(PreconditionFailed):
            second.put("k", b"stale", mode=Update(v1.version))
        with self.assertRaises(PreconditionFailed):
            second.delete("k", if_version=v1.version)

    def test_cache_is_disposable_and_remote_deletion_is_authoritative(self) -> None:
        store = self.store()
        store.put("k", b"truth", mode=Create())
        rebuilt = self.store()
        self.assertEqual(b"truth", rebuilt.get("k").body)
        head = git("--git-dir", str(self.remote), "rev-parse", f"refs/heads/{self.branch}").decode().strip()
        subprocess.run(["git", "--git-dir", str(self.remote), "update-ref", "-d", f"refs/heads/{self.branch}", head], check=True)
        self.assertIsNone(rebuilt.head("k"))
        with self.assertRaises(ObjectNotFound):
            rebuilt.get("k")

    def test_remote_outage_never_uses_cached_branch_as_authority(self) -> None:
        store = self.store()
        store.put("k", b"truth", mode=Create())
        unavailable = self.root / "remote-offline.git"
        self.remote.rename(unavailable)
        with self.assertRaises(StorageFailure):
            store.get("k")

    def test_lost_response_followed_by_later_commit_proves_earlier_success(self) -> None:
        first = self.store("cache-first-writer")
        second = self.store("cache-second-writer")
        original_git = first._git
        made_later_write = False

        def after_push(args, **kwargs):
            nonlocal made_later_write
            result = original_git(args, **kwargs)
            if args and args[0] == "push" and result.returncode == 0 and not made_later_write:
                made_later_write = True
                second.put("later", b"second", mode=Create())
                raise StorageFailure("simulated lost response after a later writer")
            return result

        first._git = after_push  # type: ignore[method-assign]
        first_result = first.put("first", b"one", mode=Create())
        self.assertEqual(first_result.version, first.head("first").version)
        self.assertEqual(b"second", second.get("later").body)

    def test_uncertain_push_is_ambiguous_unless_commit_reached_remote(self) -> None:
        store = self.store()
        original_git = store._git
        did_push = False

        def after_remote_push(args, **kwargs):
            nonlocal did_push
            result = original_git(args, **kwargs)
            if args and args[0] == "push" and result.returncode == 0 and not did_push:
                did_push = True
                raise StorageFailure("simulated lost response")
            return result

        store._git = after_remote_push  # type: ignore[method-assign]
        result = store.put("k", b"committed", mode=Create())
        self.assertEqual(b"committed", store.get("k").body)
        self.assertTrue(result.version.value)

    def test_failed_uncertain_push_without_remote_commit_raises_ambiguous(self) -> None:
        store = self.store()
        original_git = store._git

        def before_remote_push(args, **kwargs):
            if args and args[0] == "push":
                raise StorageFailure("simulated connection loss")
            return original_git(args, **kwargs)

        store._git = before_remote_push  # type: ignore[method-assign]
        with self.assertRaises(AmbiguousWrite):
            store.put("k", b"possibly committed", mode=Create())

    def test_cache_can_be_deleted_and_recreated(self) -> None:
        store = self.store()
        store.put("k", b"value", mode=Create())
        cache = store.cache_dir
        import shutil
        shutil.rmtree(cache)
        recovered = self.store()
        self.assertEqual(b"value", recovered.get("k").body)

    def test_uri_is_strict_and_config_has_only_secret_reference(self) -> None:
        remote = "a" * 32 + ".artifacts.cloudflare.net"
        config = parse_artifacts_uri(
            f"artifacts+https://{remote}/git/team/repo.git?branch=main&token_env=CI_TOKEN"
        )
        self.assertEqual(f"https://{remote}/git/team/repo.git", config.remote)
        self.assertEqual("main", config.branch)
        self.assertEqual("CI_TOKEN", config.token_env)
        for suffix in (
            "?branch=main&branch=other", "?token=secret", "#fragment",
            "?branch=", "?branch=main&unexpected=yes",
        ):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                parse_artifacts_uri(f"artifacts+https://{remote}/git/team/repo.git{suffix}")
        self.assertEqual({"remote", "branch", "token_env"}, set(config.__dict__))

    def test_token_required_only_for_https_and_never_in_remote_or_config(self) -> None:
        remote = "https://" + "a" * 32 + ".artifacts.cloudflare.net/git/team/repo.git"
        config = ArtifactsObjectStoreConfig(remote, token_env="ARTIFACTS_TOKEN_TEST")
        store = ArtifactsObjectStore.from_config(config, self.root / "https-cache")
        old = os.environ.pop("ARTIFACTS_TOKEN_TEST", None)
        try:
            with self.assertRaises(StorageFailure):
                store.head("k")
        finally:
            if old is not None:
                os.environ["ARTIFACTS_TOKEN_TEST"] = old
        self.assertNotIn("secret", repr(config))
        self.assertNotIn("secret", config.remote)

    def test_token_uses_ephemeral_git_config_environment_only(self) -> None:
        remote = "https://" + "b" * 32 + ".artifacts.cloudflare.net/git/team/repo.git"
        token = "art_v1_0123456789?expires=1234567890"
        config = ArtifactsObjectStoreConfig(remote, token_env="ARTIFACTS_TOKEN_PRIVACY_TEST")
        store = ArtifactsObjectStore.from_config(config, self.root / "privacy-cache")
        old = os.environ.get(config.token_env)
        os.environ[config.token_env] = token
        original_run = subprocess.run
        captured: list[tuple[list[str], dict[str, str]]] = []

        def intercept(command, *args, **kwargs):
            if command[:2] == ["git", "ls-remote"]:
                captured.append((command, kwargs["env"]))
                return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
            return original_run(command, *args, **kwargs)

        try:
            with patch("object_store_artifacts.subprocess.run", side_effect=intercept):
                self.assertIsNone(store.head("missing"))
        finally:
            if old is None:
                os.environ.pop(config.token_env, None)
            else:
                os.environ[config.token_env] = old
        self.assertEqual(1, len(captured))
        command, env = captured[0]
        self.assertNotIn(token, " ".join(command))
        self.assertEqual(f"Authorization: Bearer {token}", env["GIT_CONFIG_VALUE_0"])
        self.assertNotIn(token, (store.cache_dir / "artifacts-cache.git" / "config").read_text())

    def test_invalid_keys_and_non_bytes_are_rejected(self) -> None:
        store = self.store()
        for key in ("../escape", "/absolute", "a\\b", "", "é"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                store.head(key)
        with self.assertRaises(TypeError):
            store.put("k", "not bytes", mode=Create())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
