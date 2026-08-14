from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import forge
from errors import ForgeError
from invariants import check_invariants


def command(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise AssertionError(f"{' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result


class InvariantTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        command("git", "init", "-q", "-b", "main", str(source))
        command("git", "config", "user.name", "Fixture", cwd=source)
        command("git", "config", "user.email", "fixture@example.test", cwd=source)
        for name, value in (("shared/base.txt", "base\n"), ("private/a.txt", "a\n"), ("other/b.txt", "b\n")):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        executable = source / "shared" / "run"
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o755)
        os.symlink("base.txt", source / "shared" / "link")
        command("git", "add", "-A", cwd=source)
        command("git", "commit", "-q", "-m", "initial", cwd=source)
        self.forge = self.root / "forge"
        forge.init_forge(self.forge, source, "main")
        forge.add_view(self.forge, "alice", ["shared", "private"], ["shared", "private"], "main")
        forge.add_view(self.forge, "bob", ["shared", "other"], ["shared", "other"], "main")
        self._admin_counter = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _canonical_tip(self) -> str:
        return command("git", "--git-dir", str(forge.canonical_repo(self.forge)), "rev-parse", "refs/heads/main").stdout.strip()

    def _admin_commit(self, path: str, contents: str, message: str) -> str:
        admin = self.root / f"admin-{self._admin_counter}"
        self._admin_counter += 1
        command("git", "clone", "-q", str(forge.canonical_repo(self.forge)), str(admin))
        command("git", "config", "user.name", "Admin", cwd=admin)
        command("git", "config", "user.email", "admin@example.test", cwd=admin)
        target = admin / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
        command("git", "add", "-A", cwd=admin)
        command("git", "commit", "-q", "-m", message, cwd=admin)
        new = command("git", "rev-parse", "HEAD", cwd=admin).stdout.strip()
        # Push the object into canonical via a staging ref so the commit is
        # present, but leave the configured branch at its old tip for the
        # administrative update command to move.
        command("git", "push", "-q", "origin", f"{new}:refs/staging/admin-{self._admin_counter}", cwd=admin)
        return new

    def _checkout(self, name: str) -> Path:
        checkout = self.root / name
        command("git", "clone", "-q", "--no-local", str(forge.view_repo(self.forge, name)), str(checkout))
        command("git", "config", "user.name", name.capitalize(), cwd=checkout)
        command("git", "config", "user.email", f"{name}@example.test", cwd=checkout)
        return checkout

    def _refs(self) -> dict[str, str]:
        repos = [forge.canonical_repo(self.forge), *(forge.view_repo(self.forge, name) for name in ("alice", "bob"))]
        return {str(repo): command("git", "--git-dir", str(repo), "for-each-ref", "--format=%(refname):%(objectname)").stdout for repo in repos}

    def test_healthy_forge_is_read_only(self) -> None:
        before_state = (self.forge / "state.json").read_bytes()
        before_refs = self._refs()
        before_objects = {repo: command("git", "--git-dir", repo, "count-objects", "-v").stdout for repo in before_refs}
        check_invariants(self.forge)
        self.assertEqual(before_state, (self.forge / "state.json").read_bytes())
        self.assertEqual(before_refs, self._refs())
        self.assertEqual(before_objects, {repo: command("git", "--git-dir", repo, "count-objects", "-v").stdout for repo in before_refs})

    def test_failing_check_is_read_only(self) -> None:
        state = json.loads((self.forge / "state.json").read_text())
        state["next_transaction_sequence"] = 0
        forge.save_state(self.forge, state)
        before_state = (self.forge / "state.json").read_bytes()
        before_refs = self._refs()
        repos = [forge.canonical_repo(self.forge), forge.view_repo(self.forge, "alice"), forge.view_repo(self.forge, "bob")]
        before_objects = {str(repo): command("git", "--git-dir", str(repo), "count-objects", "-v").stdout for repo in repos}
        with self.assertRaisesRegex(ForgeError, r"invariant STATE_SCHEMA failed for state"):
            check_invariants(self.forge)
        self.assertEqual(before_state, (self.forge / "state.json").read_bytes())
        self.assertEqual(before_refs, self._refs())
        self.assertEqual(before_objects, {str(repo): command("git", "--git-dir", str(repo), "count-objects", "-v").stdout for repo in repos})

    def test_source_change_and_synthetic_fanout_pass(self) -> None:
        checkout = self.root / "alice"
        command("git", "clone", "-q", "--no-local", str(forge.view_repo(self.forge, "alice")), str(checkout))
        command("git", "config", "user.name", "Alice", cwd=checkout)
        command("git", "config", "user.email", "alice@example.test", cwd=checkout)
        (checkout / "shared" / "change.txt").write_text("change\n")
        command("git", "add", "-A", cwd=checkout)
        command("git", "commit", "-q", "-m", "change\n\nChange-Id: Iaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", cwd=checkout)
        command("git", "push", "-q", "origin", "main", cwd=checkout)
        check_invariants(self.forge)

    def test_empty_forge_passes(self) -> None:
        source = self.root / "empty-source"
        command("git", "init", "-q", "-b", "main", str(source))
        command("git", "config", "user.name", "Fixture", cwd=source)
        command("git", "config", "user.email", "fixture@example.test", cwd=source)
        (source / "file.txt").write_text("x\n")
        command("git", "add", "-A", cwd=source)
        command("git", "commit", "-q", "-m", "initial", cwd=source)
        empty = self.root / "empty-forge"
        forge.init_forge(empty, source, "main")
        check_invariants(empty)

    def test_hidden_canonical_change_does_not_move_tip(self) -> None:
        before = json.loads((self.forge / "state.json").read_text())
        alice_old_tip = before["views"]["alice"]["projected_tip"]
        alice_old_frontier = before["views"]["alice"]["canonical_frontier"]
        old_tip = self._canonical_tip()
        new_tip = self._admin_commit("other/hidden.txt", "hidden\n", "hidden-only canonical change")
        forge.update_canonical_ref(self.forge, old_tip, new_tip)
        forge.drain_projection_jobs(self.forge)
        check_invariants(self.forge)
        after = json.loads((self.forge / "state.json").read_text())
        self.assertNotEqual(alice_old_frontier, after["views"]["alice"]["canonical_frontier"])
        self.assertEqual(alice_old_tip, after["views"]["alice"]["projected_tip"])

    def test_multi_commit_source_chain_passes(self) -> None:
        checkout = self._checkout("alice")
        (checkout / "shared" / "first.txt").write_text("first\n")
        command("git", "add", "-A", cwd=checkout)
        command("git", "commit", "-q", "-m", "first\n\nChange-Id: Icccccccccccccccccccccccccccccccccccccccc", cwd=checkout)
        (checkout / "shared" / "second.txt").write_text("second\n")
        command("git", "add", "-A", cwd=checkout)
        command("git", "commit", "-q", "-m", "second\n\nChange-Id: Idddddddddddddddddddddddddddddddddddddddd", cwd=checkout)
        command("git", "push", "-q", "origin", "main", cwd=checkout)
        check_invariants(self.forge)

    def test_ownerless_synthetic_change_id_passes(self) -> None:
        change_id = "Ieeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        old_tip = self._canonical_tip()
        new_tip = self._admin_commit("shared/admin.txt", "admin\n", f"admin change\n\nChange-Id: {change_id}")
        forge.update_canonical_ref(self.forge, old_tip, new_tip)
        forge.drain_projection_jobs(self.forge)
        check_invariants(self.forge)
        state = json.loads((self.forge / "state.json").read_text())
        self.assertIn(change_id, state["changes"])
        self.assertNotIn("owner", state["changes"][change_id])
        self.assertNotIn("canonical_ref", state["changes"][change_id])

    def test_canonical_staging_ref_is_allowed(self) -> None:
        canonical = forge.canonical_repo(self.forge)
        tip = command("git", "--git-dir", str(canonical), "rev-parse", "HEAD").stdout.strip()
        command("git", "--git-dir", str(canonical), "update-ref", "refs/staging/extra", tip)
        check_invariants(self.forge)

    def test_source_unconfirmed_barrier_passes(self) -> None:
        checkout = self._checkout("alice")
        old = command("git", "rev-parse", "HEAD", cwd=checkout).stdout.strip()
        (checkout / "shared" / "unconfirmed.txt").write_text("unconfirmed\n")
        command("git", "add", "-A", cwd=checkout)
        command("git", "commit", "-q", "-m", "unconfirmed\n\nChange-Id: Iffffffffffffffffffffffffffffffffffffffff", cwd=checkout)
        new = command("git", "rev-parse", "HEAD", cwd=checkout).stdout.strip()
        forge.run("git", "--git-dir", str(forge.view_repo(self.forge, "alice")), "fetch", "-q", str(checkout), new)
        forge.pre_receive(self.forge, "alice", f"{old} {new} refs/heads/main\n".encode())
        transaction = next(iter(json.loads((self.forge / "state.json").read_text())["transactions"].values()))
        (self.forge / "receivers" / transaction["id"]).unlink(missing_ok=True)
        forge.drain_projection_jobs(self.forge)
        state = json.loads((self.forge / "state.json").read_text())
        tx = state["transactions"][transaction["id"]]
        self.assertEqual("source_unconfirmed", tx["status"])
        self.assertEqual("blocked", state["outbox"][tx["event_id"]]["status"])
        check_invariants(self.forge)

    def test_dangling_objects_do_not_fail(self) -> None:
        for repo in (forge.canonical_repo(self.forge), forge.view_repo(self.forge, "alice"), forge.view_repo(self.forge, "bob")):
            forge.git(repo, "hash-object", "-w", "--stdin", data=b"dangling content")
        check_invariants(self.forge)

    def test_extra_view_ref_fails(self) -> None:
        repo = forge.view_repo(self.forge, "alice")
        tip = command("git", "--git-dir", str(repo), "rev-parse", "HEAD").stdout.strip()
        command("git", "--git-dir", str(repo), "update-ref", "refs/tags/extra", tip)
        with self.assertRaisesRegex(ForgeError, r"invariant VIEW_REF failed for alice"):
            check_invariants(self.forge)

    def test_frontier_off_canonical_history_fails(self) -> None:
        state = json.loads((self.forge / "state.json").read_text())
        state["views"]["alice"]["canonical_frontier"] = "f" * 40
        forge.save_state(self.forge, state)
        with self.assertRaisesRegex(ForgeError, r"invariant FRONTIER failed for alice"):
            check_invariants(self.forge)

    def test_missing_canonical_ref_fails(self) -> None:
        canonical = forge.canonical_repo(self.forge)
        command("git", "--git-dir", str(canonical), "update-ref", "-d", "refs/heads/main")
        with self.assertRaisesRegex(ForgeError, r"invariant CANONICAL_REF failed for refs/heads/main"):
            check_invariants(self.forge)

    def test_duplicate_transaction_sequence_fails(self) -> None:
        state = json.loads((self.forge / "state.json").read_text())
        for name, oid in (("fake1", "a"), ("fake2", "b")):
            state["transactions"][name] = {
                "id": name,
                "kind": "canonical_update",
                "sequence": 1,
                "canonical_ref": "refs/heads/main",
                "canonical_old": oid * 40,
                "canonical_new": (oid * 40)[:39] + ("c" if oid == "a" else "d"),
                "pending_views": [],
                "status": "published",
            }
        state["next_transaction_sequence"] = 3
        forge.save_state(self.forge, state)
        with self.assertRaisesRegex(ForgeError, r"invariant STATE_SCHEMA failed for fake"):
            check_invariants(self.forge)

    def test_wrong_event_status_blocked_with_job_fails(self) -> None:
        old_tip = self._canonical_tip()
        new_tip = self._admin_commit("shared/x.txt", "x\n", "admin")
        forge.update_canonical_ref(self.forge, old_tip, new_tip)
        from state import load_state, recover, save_state
        state = load_state(self.forge)
        recover(self.forge, state)
        save_state(self.forge, state)
        state = json.loads((self.forge / "state.json").read_text())
        event_id = next(iter(state["outbox"]))
        state["outbox"][event_id]["status"] = "blocked"
        forge.save_state(self.forge, state)
        with self.assertRaisesRegex(ForgeError, r"invariant OUTBOX failed for canonical:"):
            check_invariants(self.forge)

    def test_job_linked_to_another_events_transaction_fails(self) -> None:
        old_tip = self._canonical_tip()
        first_tip = self._admin_commit("shared/first.txt", "first\n", "first")
        forge.update_canonical_ref(self.forge, old_tip, first_tip)
        second_tip = self._admin_commit("shared/second.txt", "second\n", "second")
        forge.update_canonical_ref(self.forge, first_tip, second_tip)
        from state import load_state, recover, save_state
        state = load_state(self.forge)
        recover(self.forge, state)
        save_state(self.forge, state)
        state = json.loads((self.forge / "state.json").read_text())
        jobs = sorted(
            (job for job in state["projection_jobs"].values() if job["view"] == "alice"),
            key=lambda job: job["sequence"],
        )
        first, second = jobs
        del state["projection_jobs"][first["id"]]
        del state["projection_jobs"][second["id"]]
        first["transaction_id"], second["transaction_id"] = second["transaction_id"], first["transaction_id"]
        first["id"] = f"project:{first['transaction_id']}:alice"
        second["id"] = f"project:{second['transaction_id']}:alice"
        state["projection_jobs"][first["id"]] = first
        state["projection_jobs"][second["id"]] = second
        forge.save_state(self.forge, state)
        with self.assertRaisesRegex(ForgeError, r"invariant JOB failed for project:"):
            check_invariants(self.forge)

    def test_missing_change_revision_fails(self) -> None:
        checkout = self._checkout("alice")
        (checkout / "shared" / "rev.txt").write_text("rev\n")
        command("git", "add", "-A", cwd=checkout)
        command("git", "commit", "-q", "-m", "rev\n\nChange-Id: I1111111111111111111111111111111111111112", cwd=checkout)
        command("git", "push", "-q", "origin", "main", cwd=checkout)
        state = json.loads((self.forge / "state.json").read_text())
        change_id = "I1111111111111111111111111111111111111112"
        self.assertIn(change_id, state["changes"])
        state["changes"][change_id]["revisions"] = []
        forge.save_state(self.forge, state)
        with self.assertRaisesRegex(ForgeError, r"invariant CHANGE failed for " + change_id):
            check_invariants(self.forge)

    def test_missing_view_repository_fails(self) -> None:
        import shutil
        shutil.rmtree(forge.view_repo(self.forge, "alice"))
        with self.assertRaisesRegex(ForgeError, r"invariant FORGE_LAYOUT failed for views"):
            check_invariants(self.forge)

    def test_orphan_view_repository_fails(self) -> None:
        command("git", "init", "--bare", "-q", str(self.forge / "views" / "orphan.git"))
        with self.assertRaisesRegex(ForgeError, r"invariant FORGE_LAYOUT failed for views"):
            check_invariants(self.forge)

    def test_view_replace_ref_fails(self) -> None:
        repo = forge.view_repo(self.forge, "alice")
        tip = command("git", "--git-dir", str(repo), "rev-parse", "HEAD").stdout.strip()
        command("git", "--git-dir", str(repo), "update-ref", "refs/replace/abc", tip)
        with self.assertRaisesRegex(ForgeError, r"invariant FORGE_LAYOUT failed for alice"):
            check_invariants(self.forge)

    def test_view_alternates_fails(self) -> None:
        repo = forge.view_repo(self.forge, "alice")
        (repo / "objects" / "info" / "alternates").write_text(str(forge.canonical_repo(self.forge) / "objects") + "\n")
        with self.assertRaisesRegex(ForgeError, r"invariant FORGE_LAYOUT failed for alice"):
            check_invariants(self.forge)

    def test_noop_source_commit_preserved_and_elided(self) -> None:
        checkout = self._checkout("alice")
        command("git", "commit", "-q", "--allow-empty", "-m", "no-op\n\nChange-Id: Inoopnopnopnopnopnopnopnopnopnopnopnop", cwd=checkout)
        new = command("git", "rev-parse", "HEAD", cwd=checkout).stdout.strip()
        command("git", "push", "-q", "origin", "main", cwd=checkout)
        check_invariants(self.forge)
        state = json.loads((self.forge / "state.json").read_text())
        self.assertIn(new, state["views"]["alice"]["anchors"])
        self.assertNotIn(new, state["views"]["bob"]["anchors"])

    def test_pending_job_passes(self) -> None:
        old_tip = self._canonical_tip()
        new_tip = self._admin_commit("shared/x.txt", "x\n", "admin")
        forge.update_canonical_ref(self.forge, old_tip, new_tip)
        from state import load_state, recover, save_state
        state = load_state(self.forge)
        recover(self.forge, state)
        save_state(self.forge, state)
        state = json.loads((self.forge / "state.json").read_text())
        self.assertTrue(any(job["status"] == "pending" for job in state["projection_jobs"].values()))
        check_invariants(self.forge)

    def test_retry_job_without_plan_passes(self) -> None:
        old_tip = self._canonical_tip()
        new_tip = self._admin_commit("shared/x.txt", "x\n", "admin")
        forge.update_canonical_ref(self.forge, old_tip, new_tip)
        from state import load_state, recover, save_state
        state = load_state(self.forge)
        recover(self.forge, state)
        save_state(self.forge, state)
        from worker import claim_projection_job, fail_projection_job
        claimed = claim_projection_job(self.forge)
        self.assertIsNotNone(claimed)
        fail_projection_job(self.forge, claimed, RuntimeError("temporary failure"))
        state = json.loads((self.forge / "state.json").read_text())
        job = state["projection_jobs"][claimed["id"]]
        self.assertEqual("retry", job["status"])
        self.assertNotIn("plan", job)
        check_invariants(self.forge)

    def test_retry_job_with_plan_passes(self) -> None:
        old_tip = self._canonical_tip()
        new_tip = self._admin_commit("shared/x.txt", "x\n", "admin")
        forge.update_canonical_ref(self.forge, old_tip, new_tip)
        from state import load_state, recover, save_state
        state = load_state(self.forge)
        recover(self.forge, state)
        save_state(self.forge, state)
        from service import build_projection_plan
        from worker import claim_projection_job, fail_projection_job
        claimed = claim_projection_job(self.forge)
        self.assertIsNotNone(claimed)
        plan = build_projection_plan(self.forge, claimed["view"], claimed["view_state"], claimed["canonical_to"])
        state = load_state(self.forge)
        state["projection_jobs"][claimed["id"]]["plan"] = plan
        save_state(self.forge, state)
        fail_projection_job(self.forge, claimed, RuntimeError("temporary failure"))
        state = json.loads((self.forge / "state.json").read_text())
        job = state["projection_jobs"][claimed["id"]]
        self.assertEqual("retry", job["status"])
        self.assertIn("plan", job)
        check_invariants(self.forge)

    def test_running_job_passes(self) -> None:
        old_tip = self._canonical_tip()
        new_tip = self._admin_commit("shared/x.txt", "x\n", "admin")
        forge.update_canonical_ref(self.forge, old_tip, new_tip)
        from state import load_state, recover, save_state
        state = load_state(self.forge)
        recover(self.forge, state)
        save_state(self.forge, state)
        from worker import claim_projection_job
        claimed = claim_projection_job(self.forge)
        self.assertIsNotNone(claimed)
        check_invariants(self.forge)

    def test_ready_job_without_plan_fails(self) -> None:
        old_tip = self._canonical_tip()
        new_tip = self._admin_commit("shared/x.txt", "x\n", "admin")
        forge.update_canonical_ref(self.forge, old_tip, new_tip)
        from state import load_state, recover, save_state
        state = load_state(self.forge)
        recover(self.forge, state)
        save_state(self.forge, state)
        from worker import claim_projection_job
        claimed = claim_projection_job(self.forge)
        self.assertIsNotNone(claimed)
        state = json.loads((self.forge / "state.json").read_text())
        state["projection_jobs"][claimed["id"]]["status"] = "ready"
        forge.save_state(self.forge, state)
        with self.assertRaisesRegex(ForgeError, r"invariant JOB failed for project:"):
            check_invariants(self.forge)

    def test_ready_job_with_retained_plan_and_unchanged_ref_passes(self) -> None:
        old_tip = self._canonical_tip()
        new_tip = self._admin_commit("shared/x.txt", "x\n", "admin")
        forge.update_canonical_ref(self.forge, old_tip, new_tip)
        from state import recover, save_state
        state = json.loads((self.forge / "state.json").read_text())
        # Dispatch without changing the claimed job's durable ref boundary.
        recover(self.forge, state)
        save_state(self.forge, state)
        from worker import claim_projection_job
        from service import build_projection_plan
        claimed = claim_projection_job(self.forge, now=1)
        plan = build_projection_plan(self.forge, claimed["view"], claimed["view_state"], claimed["canonical_to"])
        state = json.loads((self.forge / "state.json").read_text())
        state["projection_jobs"][claimed["id"]]["status"] = "ready"
        state["projection_jobs"][claimed["id"]]["plan"] = plan
        forge.save_state(self.forge, state)
        check_invariants(self.forge)

    def test_ready_plan_change_id_must_match_canonical_commit(self) -> None:
        old_tip = self._canonical_tip()
        change_id = "Iabababababababababababababababababababab"
        new_tip = self._admin_commit("shared/x.txt", "x\n", f"admin\n\nChange-Id: {change_id}")
        forge.update_canonical_ref(self.forge, old_tip, new_tip)
        from state import recover, save_state
        state = json.loads((self.forge / "state.json").read_text())
        recover(self.forge, state)
        save_state(self.forge, state)
        from worker import claim_projection_job
        from service import build_projection_plan
        claimed = claim_projection_job(self.forge, now=1)
        plan = build_projection_plan(self.forge, claimed["view"], claimed["view_state"], claimed["canonical_to"])
        old_projected, anchor = next(iter(plan["anchors"].items()))
        wrong = "Icdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"
        raw = forge.git(forge.view_repo(self.forge, claimed["view"]), "cat-file", "commit", old_projected).stdout
        replacement = raw.replace(change_id.encode(), wrong.encode())
        self.assertNotEqual(raw, replacement)
        new_projected = forge.git(forge.view_repo(self.forge, claimed["view"]), "hash-object", "-w", "-t", "commit", "--stdin", data=replacement).stdout.decode().strip()
        plan["anchors"] = {new_projected: {**anchor, "change_id": wrong}}
        plan["new_tip"] = new_projected
        state = json.loads((self.forge / "state.json").read_text())
        state["projection_jobs"][claimed["id"]]["status"] = "ready"
        state["projection_jobs"][claimed["id"]]["plan"] = plan
        forge.save_state(self.forge, state)
        with self.assertRaisesRegex(ForgeError, r"invariant JOB failed for project:.*plan Change-Id differs from canonical commit"):
            check_invariants(self.forge)

    def test_completed_job_ahead_of_frontier_fails(self) -> None:
        first_old = self._canonical_tip()
        first_new = self._admin_commit("shared/first.txt", "first\n", "first")
        forge.update_canonical_ref(self.forge, first_old, first_new)
        second_new = self._admin_commit("shared/second.txt", "second\n", "second")
        forge.update_canonical_ref(self.forge, first_new, second_new)
        from state import recover, save_state
        state = json.loads((self.forge / "state.json").read_text())
        recover(self.forge, state)
        save_state(self.forge, state)
        state = json.loads((self.forge / "state.json").read_text())
        first_tx = next(tx for tx in state["transactions"].values() if tx["canonical_new"] == first_new)
        first_tx["pending_views"] = []
        first_tx["status"] = "published"
        for job in state["projection_jobs"].values():
            if job["transaction_id"] == first_tx["id"]:
                job["status"] = "completed"
                job["lease_token"] = None
                job.pop("lease_expires_at", None)
                job.pop("plan", None)
        forge.save_state(self.forge, state)
        with self.assertRaisesRegex(ForgeError, r"invariant JOB failed for project:.*completed target is ahead of the view frontier"):
            check_invariants(self.forge)

    def test_invalid_prepared_administrative_history_fails(self) -> None:
        canonical = forge.canonical_repo(self.forge)
        parent = self._canonical_tip()
        tree_input = f"160000 commit {parent}\tgitlink\n".encode()
        result = subprocess.run(["git", "--git-dir", str(canonical), "mktree"], input=tree_input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        tree = result.stdout.decode().strip()
        candidate = command("git", "--git-dir", str(canonical), "commit-tree", tree, "-p", parent, "-m", "invalid candidate").stdout.strip()
        command("git", "--git-dir", str(canonical), "update-ref", "refs/staging/invalid-candidate", candidate)
        state = json.loads((self.forge / "state.json").read_text())
        sequence = state["next_transaction_sequence"]
        identifier = f"canonical:{sequence}:{candidate}"
        state["next_transaction_sequence"] += 1
        state["transactions"][identifier] = {
            "id": identifier,
            "kind": "canonical_update",
            "sequence": sequence,
            "canonical_ref": "refs/heads/main",
            "canonical_old": parent,
            "canonical_new": candidate,
            "pending_views": sorted(state["views"]),
            "status": "prepared",
        }
        forge.save_state(self.forge, state)
        with self.assertRaisesRegex(ForgeError, r"invariant CANONICAL_REF failed for"):
            check_invariants(self.forge)

    def test_explicit_empty_subtree_fails(self) -> None:
        canonical = forge.canonical_repo(self.forge)
        empty_tree = command("git", "--git-dir", str(canonical), "hash-object", "-t", "tree", "/dev/null").stdout.strip()
        tree_input = f"040000 tree {empty_tree}\temptydir\n".encode()
        result = subprocess.run(["git", "--git-dir", str(canonical), "mktree"], input=tree_input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        tree = result.stdout.decode().strip()
        parent = command("git", "--git-dir", str(canonical), "rev-parse", "HEAD").stdout.strip()
        commit = command("git", "--git-dir", str(canonical), "commit-tree", tree, "-p", parent, "-m", "empty subtree").stdout.strip()
        command("git", "--git-dir", str(canonical), "update-ref", "refs/heads/main", commit, parent)
        with self.assertRaisesRegex(ForgeError, r"invariant CANONICAL_GRAPH failed for"):
            check_invariants(self.forge)

    def test_unsupported_mode_fails(self) -> None:
        source = self.root / "bad-source"
        command("git", "init", "-q", "-b", "main", str(source))
        command("git", "config", "user.name", "Fixture", cwd=source)
        command("git", "config", "user.email", "fixture@example.test", cwd=source)
        gitdir = str(source / ".git")
        empty_tree = command("git", "--git-dir", gitdir, "hash-object", "-t", "tree", "/dev/null").stdout.strip()
        dummy_commit = command("git", "--git-dir", gitdir, "commit-tree", empty_tree, "-m", "dummy").stdout.strip()
        tree_input = f"160000 commit {dummy_commit}\tgitlink\n".encode()
        result = subprocess.run(["git", "--git-dir", gitdir, "mktree"], input=tree_input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        tree = result.stdout.decode().strip()
        bad_commit = command("git", "--git-dir", gitdir, "commit-tree", tree, "-m", "gitlink").stdout.strip()
        command("git", "--git-dir", gitdir, "update-ref", "refs/heads/main", bad_commit)
        bad_forge = self.root / "bad-forge"
        forge.init_forge(bad_forge, source, "main")
        forge.add_view(bad_forge, "alice", ["shared"], ["shared"], "main")
        with self.assertRaisesRegex(ForgeError, r"invariant CANONICAL_GRAPH failed for .*gitlink"):
            check_invariants(bad_forge)

    def test_wrong_anchor_kind_fails(self) -> None:
        checkout = self._checkout("alice")
        (checkout / "shared" / "x.txt").write_text("x\n")
        command("git", "add", "-A", cwd=checkout)
        command("git", "commit", "-q", "-m", "x\n\nChange-Id: I2222222222222222222222222222222222222222", cwd=checkout)
        command("git", "push", "-q", "origin", "main", cwd=checkout)
        state = json.loads((self.forge / "state.json").read_text())
        submitted_oid = next(oid for oid, anchor in state["views"]["alice"]["anchors"].items() if anchor["kind"] == "submitted")
        state["views"]["alice"]["anchors"][submitted_oid]["kind"] = "synthetic"
        state["views"]["alice"]["anchors"][submitted_oid]["change_id"] = None
        forge.save_state(self.forge, state)
        with self.assertRaisesRegex(ForgeError, r"invariant ANCHOR failed for " + submitted_oid):
            check_invariants(self.forge)

    def test_missing_anchor_fails(self) -> None:
        checkout = self._checkout("alice")
        (checkout / "shared" / "x.txt").write_text("x\n")
        command("git", "add", "-A", cwd=checkout)
        command("git", "commit", "-q", "-m", "x\n\nChange-Id: I3333333333333333333333333333333333333333", cwd=checkout)
        command("git", "push", "-q", "origin", "main", cwd=checkout)
        state = json.loads((self.forge / "state.json").read_text())
        submitted_oid = next(oid for oid, anchor in state["views"]["alice"]["anchors"].items() if anchor["kind"] == "submitted")
        del state["views"]["alice"]["anchors"][submitted_oid]
        forge.save_state(self.forge, state)
        # The MAPPING phase detects the missing anchor before the ANCHOR phase runs.
        with self.assertRaisesRegex(ForgeError, r"invariant MAPPING failed for .*confirmed source publication is missing"):
            check_invariants(self.forge)

    def test_extra_anchor_fails(self) -> None:
        state = json.loads((self.forge / "state.json").read_text())
        fake_oid = "c" * 40
        state["views"]["alice"]["anchors"][fake_oid] = {"canonical": fake_oid, "kind": "synthetic", "change_id": None}
        forge.save_state(self.forge, state)
        with self.assertRaisesRegex(ForgeError, r"invariant ANCHOR failed for alice"):
            check_invariants(self.forge)

    def test_hostile_global_commit_encoding_does_not_change_projection(self) -> None:
        hostile = self.root / "hostile.gitconfig"
        hostile.write_bytes(b"[i18n]\n\tcommitEncoding = ISO-8859-1\n")
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(hostile)}):
            # Prove the hostile global config would alter a commit written
            # without the forge's environment isolation.
            probe = self.root / "probe"
            command("git", "init", "-q", "-b", "main", str(probe))
            command("git", "config", "user.name", "Probe", cwd=probe)
            command("git", "config", "user.email", "probe@example.test", cwd=probe)
            (probe / "file.txt").write_text("x\n")
            command("git", "add", "-A", cwd=probe)
            command("git", "commit", "-q", "-m", "probe", cwd=probe)
            probe_raw = command("git", "--git-dir", str(probe / ".git"), "cat-file", "commit", "HEAD").stdout.encode()
            self.assertIn(b"encoding ", probe_raw)

            # Forge-generated commits must ignore the hostile config.
            forge.add_view(self.forge, "encoded", ["shared"], ["shared"], "main")
        state = json.loads((self.forge / "state.json").read_text())
        tip = state["views"]["encoded"]["projected_tip"]
        raw = forge.git(forge.view_repo(self.forge, "encoded"), "cat-file", "commit", tip).stdout
        self.assertNotIn(b"encoding ", raw)
        self.assertIn(b"author Bastion Projection <projection@localhost> 946684800 +0000", raw)
        check_invariants(self.forge)

    def test_bad_persisted_tip_reports_view_ref(self) -> None:
        state = json.loads((self.forge / "state.json").read_text())
        state["views"]["alice"]["projected_tip"] = "f" * 40
        forge.save_state(self.forge, state)
        with self.assertRaisesRegex(ForgeError, r"invariant VIEW_REF failed for alice"):
            check_invariants(self.forge)

    def test_cli_is_silent_on_success_and_reports_failure(self) -> None:
        script = Path(__file__).resolve().parents[1] / "forge.py"
        healthy = command("python3", str(script), "check", str(self.forge), check=False)
        self.assertEqual(0, healthy.returncode)
        self.assertEqual("", healthy.stdout)
        self.assertEqual("", healthy.stderr)
        state = json.loads((self.forge / "state.json").read_text())
        state["next_transaction_sequence"] = 0
        forge.save_state(self.forge, state)
        failed = command("python3", str(script), "check", str(self.forge), check=False)
        self.assertEqual(1, failed.returncode)
        self.assertEqual("", failed.stdout)
        self.assertIn("bastion: invariant STATE_SCHEMA failed for state", failed.stderr)


if __name__ == "__main__":
    unittest.main()
