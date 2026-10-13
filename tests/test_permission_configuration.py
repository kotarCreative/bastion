from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from permissions import descriptor_permissions, repository_allowed, branch_allowed
from service import add_principal, add_role, add_team, bind_role, add_view, transition_principal
from state import load_state
from tests.identity_harness import capability_grants

ROOT = Path(__file__).resolve().parents[1]


class PermissionConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = self.base / "source"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.source)], check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=self.source, check=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.test"], cwd=self.source, check=True)
        (self.source / "base").write_text("base\n")
        subprocess.run(["git", "add", "."], cwd=self.source, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=self.source, check=True)
        self.forge = self.base / "forge"
        result = self.cli("init", str(self.forge), str(self.source))
        self.assertEqual(result.returncode, 0, result.stderr)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "forge.py"), *args], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def role(self, role_id, repo_allow=(), repo_deny=(), branch_allow=(), branch_deny=()):
        args = ["add-role", str(self.forge), role_id]
        for value in repo_allow: args += ["--repo-allow", value]
        for value in repo_deny: args += ["--repo-deny", value]
        for value in branch_allow: args += ["--branch-allow", value]
        for value in branch_deny: args += ["--branch-deny", value]
        result = self.cli(*args)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_permission_only_role_and_explicit_transition(self):
        add_principal(self.forge, "reader")
        allow, deny = capability_grants([""], [])
        add_role(self.forge, "path", allow, deny)
        bind_role(self.forge, "path", "principal", "reader")
        add_view(self.forge, "reader", "reader", "main")
        self.role("review", ["pr_view"])
        bind_role(self.forge, "review", "principal", "reader")
        repository = __import__("repository").Repository.open(self.forge)
        before = repository._descriptor(repository.load()[2].active_views["reader"]["descriptor_id"])
        # The selected policy is dormant until a committed transition; the active
        # descriptor remains the old legacy descriptor.
        self.assertEqual(before["algorithm"], "capabilities-v3")
        transition_principal(self.forge, "reader")
        descriptor = repository._descriptor(repository.load()[2].active_views["reader"]["descriptor_id"])
        self.assertTrue(repository_allowed(descriptor_permissions(descriptor), "pr_view"))
        self.role("revoke-review", repo_deny=["pr_view"])
        bind_role(self.forge, "revoke-review", "principal", "reader")
        # A catalog change does not mutate the active descriptor.
        active = repository._descriptor(repository.load()[2].active_views["reader"]["descriptor_id"])
        self.assertTrue(repository_allowed(descriptor_permissions(active), "pr_view"))
        transition_principal(self.forge, "reader")
        revoked = repository._descriptor(repository.load()[2].active_views["reader"]["descriptor_id"])
        self.assertFalse(repository_allowed(descriptor_permissions(revoked), "pr_view"))

    def test_cli_bad_permission_does_not_change_catalog(self):
        before = load_state(self.forge)
        for flags in (("--repo-allow", "admin"), ("--branch-allow", "read=refs/heads/*/x"),
                      ("--branch-deny", "update=")):
            result = self.cli("add-role", str(self.forge), "bad", *flags)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(load_state(self.forge), before)

    def test_team_union_and_root_admin_has_no_implicit_pr_view(self):
        add_principal(self.forge, "one")
        add_principal(self.forge, "two")
        self.role("one-policy", ["pr_view"])
        bind_role(self.forge, "one-policy", "principal", "one")
        add_team(self.forge, "both", ["one", "two"])
        self.role("team-policy", ["pr_create"], ["pr_view"])
        bind_role(self.forge, "team-policy", "team", "both")
        state = load_state(self.forge)
        from identity import effective_descriptor
        combined = descriptor_permissions(effective_descriptor(state, "one", "refs/heads/main"))
        self.assertFalse(repository_allowed(combined, "pr_view"))
        self.assertTrue(repository_allowed(combined, "pr_create"))
        self.assertFalse(repository_allowed(combined, "pr_merge"))
        allow, deny = capability_grants([""], [])
        allow["admin"] = [""]
        add_principal(self.forge, "root")
        add_role(self.forge, "root-path", allow, deny)
        bind_role(self.forge, "root-path", "principal", "root")
        add_view(self.forge, "root", "root", "main")
        from identity import effective_descriptor
        root_descriptor = effective_descriptor(load_state(self.forge), "root", "refs/heads/main")
        self.assertEqual(root_descriptor["algorithm"], "capabilities-v3")
        policy = descriptor_permissions(root_descriptor)
        self.assertFalse(repository_allowed(policy, "pr_view"))
        for action in ("read", "create", "update", "delete"):
            self.assertFalse(branch_allowed(policy, action, "refs/heads/main"))

    def test_branch_rules_are_explicit(self):
        add_principal(self.forge, "branchuser")
        self.role("branch-policy", branch_allow=["read=refs/heads/feature/*"])
        bind_role(self.forge, "branch-policy", "principal", "branchuser")
        from identity import effective_descriptor
        policy = descriptor_permissions(effective_descriptor(load_state(self.forge), "branchuser", "refs/heads/main"))
        self.assertTrue(branch_allowed(policy, "read", "refs/heads/feature/topic"))
        self.assertFalse(branch_allowed(policy, "read", "refs/heads/main"))

    def test_cli_global_branch_wildcard_and_legacy_role_compatibility(self):
        self.role("all-branches", branch_allow=["read=refs/heads/*"])
        add_principal(self.forge, "branch-reader")
        bind_role(self.forge, "all-branches", "principal", "branch-reader")
        from identity import effective_descriptor
        policy = descriptor_permissions(
            effective_descriptor(load_state(self.forge), "branch-reader", "refs/heads/main")
        )
        self.assertTrue(branch_allowed(policy, "read", "refs/heads/main"))
        self.assertTrue(branch_allowed(policy, "read", "refs/heads/topic/nested"))

        grants, restrictions = capability_grants(["docs"], [])
        result = self.cli("add-role", str(self.forge), "legacy-role", "--allow", "read=docs")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = load_state(self.forge)
        self.assertEqual(
            state["roles"]["legacy-role"],
            {"id": "legacy-role", "allow": grants, "deny": restrictions},
        )
        add_principal(self.forge, "legacy")
        bind_role(self.forge, "legacy-role", "principal", "legacy")
        descriptor = effective_descriptor(load_state(self.forge), "legacy", "refs/heads/main")
        self.assertEqual(descriptor["algorithm"], "capabilities-v3")
        self.assertNotIn("permissions", descriptor)
        from identity import compute_descriptor_id
        payload = {key: value for key, value in descriptor.items() if key != "id"}
        self.assertEqual(descriptor["id"], compute_descriptor_id(payload))


if __name__ == "__main__":
    unittest.main()
