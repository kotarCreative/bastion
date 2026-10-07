"""Cold-start, HTTP, Git, and recovery coverage for the hosted runtime."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import cloudflare_runtime
import forge
import service
from accounts import link_clerk_account
from identity import CAPABILITIES
from http_server import add_token
from materialization import ensure_canonical_materialized
from object_store_artifacts import ArtifactsObjectStore
from repository import Repository, clear_repository_cache
from service import add_principal, add_role, add_view, bind_role, managed_private_paths
from state import load_state
from tests.artifacts_https_harness import ArtifactsHttpsHarness


def _run(*args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        list(args),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if check and result.returncode:
        raise AssertionError(
            f"command failed ({result.returncode}): {args!r}\n"
            f"stdout: {result.stdout.decode(errors='replace')}\n"
            f"stderr: {result.stderr.decode(errors='replace')}"
        )
    return result


class HttpServer:
    def __init__(self, app):
        self.server = make_server("127.0.0.1", 0, app, threaded=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def _http(server: HttpServer, path: str, *, method="GET", body=None, token=None, basic=None):
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(basic.encode()).decode()
    request = Request(server.base + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, response.read(), response.headers
    except HTTPError as error:
        return error.code, error.read(), error.headers


class CloudflareRuntimeIntegrationTest(unittest.TestCase):
    def test_bootstrap_resumes_after_catalog_commit_and_ephemeral_loss(self):
        with tempfile.TemporaryDirectory(prefix="bastion-cf-bootstrap-") as temporary:
            base = Path(temporary)
            root = base / "ephemeral-root"
            cache = base / "runtime-cache"
            remote_fixture = ArtifactsHttpsHarness()
            self.addCleanup(remote_fixture.close)
            original_from_config = ArtifactsObjectStore.from_config.__func__
            original_add_principal = service.add_principal

            def https_from_config(cls, config, cache_dir):
                return remote_fixture.configure_cache(
                    original_from_config(cls, config, cache_dir)
                )

            def fail_after_admin_commit(root_arg, principal, emails=None):
                original_add_principal(root_arg, principal, emails)
                raise RuntimeError("injected interruption after bootstrap catalog commit")

            environment = {
                "BASTION_ROOT": str(root),
                "BASTION_ARTIFACTS_REMOTE": remote_fixture.remote,
                "BASTION_ARTIFACTS_TOKEN": "test-backing-token",
                "BASTION_ARTIFACTS_CACHE": str(cache),
                "BASTION_BOOTSTRAP_ADMIN_EMAIL": "admin@example.test",
            }
            with patch.dict(os.environ, environment, clear=False), patch.object(
                ArtifactsObjectStore, "from_config", new=classmethod(https_from_config)
            ):
                with patch.object(service, "add_principal", fail_after_admin_commit):
                    with self.assertRaisesRegex(RuntimeError, "injected interruption"):
                        cloudflare_runtime.build_app()
                payload, _version = cloudflare_runtime._read_snapshot(
                    cloudflare_runtime.build_store()
                )
                self.assertIsNotNone(payload)
                self.assertEqual("bootstrapping", payload["phase"])
                original_config = json.loads((root / "repository.json").read_text())
                original_repository_id = original_config["repository_id"]

                # Both the forge files and the Artifacts Git cache are
                # disposable. The private snapshot resumes the same init.
                shutil.rmtree(root)
                shutil.rmtree(cache, ignore_errors=True)
                clear_repository_cache()
                runtime = cloudflare_runtime.build_app()
                restored_config = json.loads((root / "repository.json").read_text())
                self.assertEqual(original_repository_id, restored_config["repository_id"])
                self.assertEqual("artifacts", restored_config["object_store"]["kind"])
                self.assertEqual(
                    ["admin@example.test"],
                    load_state(root)["principals"]["admin"]["emails"],
                )
                manifest, _version, replay = Repository.open(root).load(use_cache=False)
                self.assertEqual("ready", load_state(root)["repository_state"])
                self.assertEqual("admin", replay.active_views["admin"]["principal_id"])
                canonical = ensure_canonical_materialized(root, manifest)
                self.assertEqual(
                    0,
                    forge.git(
                        canonical,
                        "cat-file",
                        "-e",
                        f"{manifest.canonical_tip}:README.md",
                        check=False,
                    ).returncode,
                )
                self.assertFalse(runtime.poisoned)

    def test_artifacts_cold_boot_http_git_policy_crash_and_recovery(self):
        with tempfile.TemporaryDirectory(prefix="bastion-cf-e2e-") as temporary:
            base = Path(temporary)
            root = base / "ephemeral-root"
            runtime_cache = base / "runtime-cache"
            clone = base / "clone"
            remote_fixture = ArtifactsHttpsHarness()
            self.addCleanup(remote_fixture.close)
            original_from_config = ArtifactsObjectStore.from_config.__func__

            def local_https_from_config(cls, config, cache_dir):
                store = original_from_config(cls, config, cache_dir)
                return remote_fixture.configure_cache(store)

            environment = {
                "BASTION_ROOT": str(root),
                "BASTION_ARTIFACTS_REMOTE": remote_fixture.remote,
                "BASTION_ARTIFACTS_TOKEN": "test-backing-token",
                "BASTION_ARTIFACTS_CACHE": str(runtime_cache),
                "BASTION_BOOTSTRAP_ADMIN_EMAIL": "admin@example.test",
            }
            with patch.dict(os.environ, environment, clear=False), patch.object(
                ArtifactsObjectStore,
                "from_config",
                new=classmethod(local_https_from_config),
            ):
                # This is the actual cold factory path: bootstrap an empty
                # ephemeral root and publish the first control snapshot.
                runtime = cloudflare_runtime.build_app()
                self.assertEqual("artifacts", json.loads((root / "repository.json").read_text())["object_store"]["kind"])

                repository = Repository.open(root)
                old_config = json.loads((root / "repository.json").read_text())
                self.assertEqual("artifacts", old_config["object_store"]["kind"])
                clear_repository_cache()

                # Give Bob a read-only view. The owner invitation comes from
                # the cold-start email bootstrap above.
                link_clerk_account(root, "user_runtimeAdmin", ["admin@example.test"])
                add_principal(root, "bob", ["bob@example.test"])
                bob_allow = {capability: [] for capability in CAPABILITIES}
                bob_deny = {capability: [] for capability in CAPABILITIES}
                bob_allow["read"] = [""]
                add_role(root, "bob-reader", bob_allow, bob_deny)
                bind_role(root, "bob-reader", "principal", "bob")
                add_view(root, "bob", "bob", "main")
                bob_token = add_token(root, "bob", scopes=["fetch", "ui"])
                push_token = add_token(root, "admin", scopes=["fetch", "push", "ui"])
                runtime.version = cloudflare_runtime.checkpoint_root(
                    root, runtime.store, runtime.version
                )

                server = HttpServer(runtime)
                self.assertTrue(
                    all(entry["auth_passed"] for entry in remote_fixture.request_log)
                )

                # Clone and push through Git smart HTTP served by Flask and
                # guarded by the durable WSGI wrapper.
                clone_url = f"{server.base}/admin.git"
                authenticated_url = clone_url.replace(
                    "http://", f"http://admin:{push_token}@", 1
                )
                _run("git", "clone", authenticated_url, str(clone))
                (clone / "README.md").write_text("# Runtime recovery\nfirst private version\n")
                (clone / "recovery-notes.txt").write_text("second policy target\n")
                _run("git", "-C", str(clone), "add", "README.md", "recovery-notes.txt")
                commit_env = dict(os.environ)
                commit_env.update(
                    GIT_AUTHOR_NAME="Runtime Test",
                    GIT_AUTHOR_EMAIL="admin@example.test",
                    GIT_COMMITTER_NAME="Runtime Test",
                    GIT_COMMITTER_EMAIL="admin@example.test",
                )
                _run("git", "-C", str(clone), "commit", "-m", "Add recovery content", env=commit_env)
                _run("git", "-C", str(clone), "push", authenticated_url, "HEAD:main")
                self.assertTrue(
                    any(
                        entry["method"] == "POST"
                        and "git-receive-pack" in entry["path"]
                        and entry["auth_passed"]
                        for entry in remote_fixture.request_log
                    ),
                    "receive hook did not publish through authenticated Artifacts Git HTTPS",
                )

                clear_repository_cache()
                manifest = Repository.open(root).load(use_cache=False)[0]
                canonical = ensure_canonical_materialized(root, manifest)
                hidden_readme_oid = forge.output(
                    canonical, "rev-parse", f"{manifest.canonical_tip}:README.md"
                )
                hidden_notes_oid = forge.output(
                    canonical, "rev-parse", f"{manifest.canonical_tip}:recovery-notes.txt"
                )
                _http(server, "/api/v1/session", token=bob_token)

                def hide(path: str):
                    return _http(
                        server,
                        "/api/v1/permissions",
                        method="POST",
                        token=push_token,
                        body={
                            "view_id": "bob",
                            "path_b64": base64.b64encode(path.encode()).decode(),
                            "private": True,
                        },
                    )

                first_private = hide("README.md")
                self.assertEqual(200, first_private[0], first_private[1])
                bob_view = forge.view_repo(root, "bob")
                self.assertNotEqual(
                    0, forge.git(bob_view, "cat-file", "-e", hidden_readme_oid, check=False).returncode
                )
                self.assertEqual(["README.md"], managed_private_paths(load_state(root), "bob"))
                self.assertEqual(404, _http(server, "/control/cloudflare-root-v1.json")[0])

                # Inject a process crash after the policy route has committed
                # the WAL, but before DurableRuntime can publish its final
                # successful-response checkpoint. The observer checkpoints
                # the catalog commit before WAL publication; startup recovery
                # must replay any policy record beyond that snapshot.
                real_checkpoint = cloudflare_runtime.checkpoint_root
                fail_final_checkpoint = [False]
                pre_crash_manifest, _pre_crash_version, pre_crash_replay = Repository.open(
                    root
                ).load(use_cache=False)
                pre_crash_head = pre_crash_manifest.head_seq

                def checkpoint_with_crash(root_arg, store_arg, version_arg=None, **kwargs):
                    if fail_final_checkpoint[0] and Repository.open(root_arg).load(
                        use_cache=False
                    )[0].head_seq > pre_crash_head:
                        raise OSError("injected crash before response checkpoint")
                    return real_checkpoint(root_arg, store_arg, version_arg, **kwargs)

                fail_final_checkpoint[0] = True
                with patch.object(cloudflare_runtime, "checkpoint_root", checkpoint_with_crash):
                    crashed = hide("recovery-notes.txt")
                self.assertEqual(503, crashed[0])
                self.assertTrue(runtime.poisoned)
                crash_manifest = Repository.open(root).load(use_cache=False)[0]
                self.assertEqual(pre_crash_head + 1, crash_manifest.head_seq)
                crash_replay = Repository.open(root).load(use_cache=False)[2]
                snapshot_payload, _snapshot_version = cloudflare_runtime._read_snapshot(
                    runtime.store
                )
                durable_snapshot_root = base / "latest-durable-snapshot"
                cloudflare_runtime.restore_root(durable_snapshot_root, snapshot_payload)
                self.assertLess(
                    load_state(durable_snapshot_root)["last_indexed_seq"],
                    crash_manifest.head_seq,
                )
                self.assertEqual(
                    pre_crash_replay.active_views["bob"]["projection_epoch"] + 1,
                    crash_replay.active_views["bob"]["projection_epoch"],
                )
                # Whether the snapshot was taken immediately before WAL
                # publication and the next checkpoint failed, the WAL is
                # authoritative and must retain both requested denials.
                self.assertEqual(
                    ["README.md", "recovery-notes.txt"],
                    managed_private_paths(load_state(root), "bob"),
                )

                server.close()
                shutil.rmtree(root)
                shutil.rmtree(runtime_cache, ignore_errors=True)
                clear_repository_cache()
                runtime = cloudflare_runtime.build_app()
                self.assertFalse(runtime.poisoned)
                self.assertEqual(
                    "artifacts",
                    json.loads((root / "repository.json").read_text())["object_store"]["kind"],
                )
                self.assertEqual(
                    ["README.md", "recovery-notes.txt"],
                    managed_private_paths(load_state(root), "bob"),
                )
                self.assertEqual(
                    crash_replay.active_views["bob"]["projection_epoch"],
                    Repository.open(root).load(use_cache=False)[2]
                    .active_views["bob"]["projection_epoch"],
                )
                # Exercise another policy publication after recovery. Existing
                # denials remain in force, and Bob's new view contains neither
                # hidden blob.
                restarted_server = HttpServer(runtime)
                self.addCleanup(restarted_server.close)
                self.assertEqual(
                    "user_runtimeAdmin",
                    load_state(root)["principals"]["admin"]["clerk_user_id"],
                )
                status, _body, _headers = _http(
                    restarted_server, "/api/v1/session", token=bob_token
                )
                self.assertEqual(200, status)
                bob_git_url = f"{restarted_server.base}/bob.git"
                bob_authenticated_url = bob_git_url.replace(
                    "http://", f"http://bob:{bob_token}@", 1
                )
                self.assertIn(
                    b"refs/heads/main",
                    _run("git", "ls-remote", bob_authenticated_url).stdout,
                )
                status, body, _headers = _http(
                    restarted_server,
                    "/api/v1/permissions",
                    method="POST",
                    token=push_token,
                    body={
                        "view_id": "bob",
                        "path_b64": base64.b64encode(b"README.md").decode(),
                        "private": False,
                    },
                )
                self.assertEqual(200, status, body)
                # Removing one deny cannot restore access to the distinct
                # second path that was committed immediately before restart.
                self.assertEqual(
                    ["recovery-notes.txt"], managed_private_paths(load_state(root), "bob")
                )
                bob_view = forge.view_repo(root, "bob")
                self.assertEqual(
                    0, forge.git(bob_view, "cat-file", "-e", hidden_readme_oid, check=False).returncode
                )
                self.assertNotEqual(
                    0, forge.git(bob_view, "cat-file", "-e", hidden_notes_oid, check=False).returncode
                )
                self.assertEqual(404, _http(restarted_server, "/control/cloudflare-root-v1.json")[0])


if __name__ == "__main__":
    unittest.main()
