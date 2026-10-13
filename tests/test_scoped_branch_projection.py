from branch_projection import authorized_frontiers, selected_default
from layout import ZERO_OID
from materialization import ensure_intake_materialized, ensure_view_materialized
from repository import Repository
from permissions import empty_permissions
from service import add_role, bind_role, transition_principal
from identity import empty_capability_map
from tests.test_scoped_branch_receive import _real_v4_receive
import subprocess
import pytest
from errors import ForgeError


def test_authorized_frontiers_and_default_do_not_expose_denied_default():
    descriptor = {
        "algorithm": "capabilities-v4",
        "canonical_ref": "refs/heads/private",
        "capabilities": {"read": [], "update": []},
        "restrictions": {"read": [], "update": []},
        "permissions": {
            "repository": {"allow": [], "deny": []},
            "branches": {
                "allow": {"read": ["refs/heads/public"], "update": [], "create": [], "delete": []},
                "deny": {"read": [], "update": [], "create": [], "delete": []},
            }
        },
    }
    refs = {"refs/heads/private": "a" * 40, "refs/heads/public": "b" * 40}
    visible = authorized_frontiers(descriptor, refs)
    assert visible == {"refs/heads/public": "b" * 40}
    assert selected_default(descriptor, visible) == "refs/heads/public"
    assert selected_default(descriptor, {}) is None


def test_v4_materializes_all_readable_refs_and_selects_fallback_head(tmp_path):
    fixture, checkout = _real_v4_receive(tmp_path)
    try:
        initial_manifest, _version, initial_replay = Repository.open(fixture.root).load()
        initial_view = ensure_view_materialized(fixture.root, "alice", initial_manifest, initial_replay)
        assert set(initial_view.projected_refs) == {"refs/heads/main", "refs/heads/feature"}
        subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "main"], check=True)
        (checkout / "team-a" / "main-only.txt").write_text("private to main tip\n")
        subprocess.run(["git", "-C", str(checkout), "add", "team-a/main-only.txt"], check=True)
        subprocess.run(["git", "-C", str(checkout), "commit", "-q", "-m", "main only"], check=True)
        private_blob = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD:team-a/main-only.txt"], check=True, text=True, capture_output=True).stdout.strip()
        subprocess.run(["git", "-C", str(checkout), "push", "origin", "HEAD:refs/heads/main"], check=True, capture_output=True)
        # Deny only the default branch. The feature branch remains readable.
        deny = empty_permissions()
        deny["branches"]["deny"]["read"] = ["refs/heads/main"]
        add_role(fixture.root, "deny-main-read", empty_capability_map(), empty_capability_map(), permissions=deny)
        bind_role(fixture.root, "deny-main-read", "principal", "alice")
        transition_principal(fixture.root, "alice")
        manifest, _version, replay = Repository.open(fixture.root).load()
        view = ensure_view_materialized(fixture.root, "alice", manifest, replay)
        assert view.projected_ref == "refs/heads/feature"
        assert set(view.projected_refs) == {"refs/heads/feature"}
        assert subprocess.run(["git", f"--git-dir={view.path}", "cat-file", "-e", private_blob], check=False).returncode != 0
        intake = ensure_intake_materialized(fixture.root, view, manifest, replay)
        refs = subprocess.run(["git", f"--git-dir={intake}", "for-each-ref", "--format=%(refname)"], check=True, text=True, capture_output=True).stdout.splitlines()
        assert refs == ["refs/heads/feature"]
        subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "feature"], check=True)
        (checkout / "team-a" / "feature-next.txt").write_text("new authorized frontier\n")
        subprocess.run(["git", "-C", str(checkout), "add", "team-a/feature-next.txt"], check=True)
        subprocess.run(["git", "-C", str(checkout), "commit", "-q", "-m", "feature next"], check=True)
        subprocess.run(["git", "-C", str(checkout), "push", "origin", "HEAD:refs/heads/feature"], check=True, capture_output=True)
        newer_manifest, _version, newer_replay = Repository.open(fixture.root).load()
        with pytest.raises(ForgeError, match="superseded"):
            ensure_intake_materialized(fixture.root, view, newer_manifest, newer_replay)
    finally:
        fixture.close()


def test_v4_empty_read_scope_materializes_and_intake_has_no_refs(tmp_path):
    fixture, _checkout = _real_v4_receive(tmp_path)
    try:
        deny = empty_permissions()
        deny["branches"]["deny"]["read"] = ["refs/heads/main", "refs/heads/feature", "refs/heads/topic"]
        add_role(fixture.root, "deny-all-branch-read", empty_capability_map(), empty_capability_map(), permissions=deny)
        bind_role(fixture.root, "deny-all-branch-read", "principal", "alice")
        transition_principal(fixture.root, "alice")
        manifest, _version, replay = Repository.open(fixture.root).load()
        view = ensure_view_materialized(fixture.root, "alice", manifest, replay)
        assert view.projected_refs == {}
        assert view.projected_ref is None
        assert view.projected_tip == ZERO_OID
        inventory = subprocess.run(["git", f"--git-dir={view.path}", "cat-file", "--batch-all-objects", "--batch-check=%(objectname)"], check=True, text=True, capture_output=True).stdout
        assert inventory == ""
        intake = ensure_intake_materialized(fixture.root, view, manifest, replay)
        refs = subprocess.run(["git", f"--git-dir={intake}", "for-each-ref", "--format=%(refname)"], check=True, text=True, capture_output=True).stdout
        assert refs == ""
    finally:
        fixture.close()


def test_v3_default_observer_replays_v4_branch_submission(tmp_path):
    from materialization import clear_materialization_registrations
    from tests import identity_harness

    fixture, checkout = _real_v4_receive(tmp_path)
    try:
        identity_harness.add_view(fixture.root, "legacy", ["team-a"], ["team-a"], "main")
        manifest, _version, replay = Repository.open(fixture.root).load()
        before = ensure_view_materialized(fixture.root, "legacy", manifest, replay)
        (tmp_path / "v4-update.txt").write_text("visible update\n")
        subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "main"], check=True)
        (checkout / "team-a" / "v4-update.txt").write_text("visible update\n")
        subprocess.run(["git", "-C", str(checkout), "add", "team-a/v4-update.txt"], check=True)
        subprocess.run(["git", "-C", str(checkout), "commit", "-q", "-m", "v4 default update"], check=True)
        submitted_oid = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], check=True, text=True, capture_output=True).stdout.strip()
        subprocess.run(["git", "-C", str(checkout), "push", "origin", "HEAD:refs/heads/main"], check=True, capture_output=True)
        manifest, _version, replay = Repository.open(fixture.root).load()
        clear_materialization_registrations()
        after = ensure_view_materialized(fixture.root, "legacy", manifest, replay)
        assert after.projected_tip != before.projected_tip
        branch_event = next(record for record in reversed(replay.records) if record["kind"] == "branch_submission" and record["payload"]["canonical_ref"] == "refs/heads/main")
        assert branch_event["payload"]["canonical_new"] == manifest.canonical_refs["refs/heads/main"]
        assert submitted_oid != ZERO_OID
        assert subprocess.run(["git", f"--git-dir={after.path}", "show", f"{after.projected_tip}:team-a/v4-update.txt"], check=True, text=True, capture_output=True).stdout == "visible update\n"
    finally:
        fixture.close()
