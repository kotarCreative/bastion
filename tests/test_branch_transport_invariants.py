"""Independent checks distinguish projected wire tips from canonical tips."""

import subprocess

import pytest

from invariants import InvariantError, check_invariants
from materialization import ensure_view_materialized
from repository import Repository
from tests import identity_harness
from tests.test_scoped_branch_receive import _git, _real_v4_receive


def test_checker_accepts_projected_secondary_branch_update(tmp_path):
    fixture, checkout = _real_v4_receive(tmp_path)
    try:
        _git(checkout, "checkout", "-q", "feature")
        (checkout / "team-a" / "feature.txt").write_text("feature update\n")
        _git(checkout, "add", "team-a/feature.txt")
        _git(checkout, "commit", "-qm", "feature update")
        _git(checkout, "push", "origin", "HEAD:refs/heads/feature")
        _manifest, _version, replay = Repository.open(fixture.root).load()
        event = next(r for r in reversed(replay.records) if r["kind"] == "branch_submission")
        assert event["payload"]["receive_old"] == event["payload"]["projected_old"]
        assert event["payload"]["receive_old"] != event["payload"]["canonical_old"]
        check_invariants(fixture.root)
    finally:
        fixture.close()


def test_checker_advances_legacy_observer_after_scoped_default_push(tmp_path):
    fixture, checkout = _real_v4_receive(tmp_path)
    try:
        identity_harness.add_view(fixture.root, "legacy", ["team-a"], ["team-a"], "main")
        (checkout / "team-a" / "main.txt").write_text("default update\n")
        _git(checkout, "add", "team-a/main.txt")
        _git(checkout, "commit", "-qm", "default update")
        _git(checkout, "push", "origin", "HEAD:refs/heads/main")
        check_invariants(fixture.root)
    finally:
        fixture.close()


@pytest.mark.parametrize("contamination", ["ref", "object", "graft"])
def test_checker_rejects_scoped_cache_contamination(tmp_path, contamination):
    fixture, _checkout = _real_v4_receive(tmp_path)
    try:
        repository = Repository.open(fixture.root)
        manifest, _version, replay = repository.load()
        view = ensure_view_materialized(fixture.root, "alice", manifest, replay, repository)
        if contamination == "ref":
            subprocess.run(
                ["git", f"--git-dir={view.path}", "update-ref", "refs/heads/private", view.projected_tip],
                check=True,
            )
        elif contamination == "object":
            subprocess.run(
                ["git", f"--git-dir={view.path}", "hash-object", "-w", "--stdin"],
                input=b"unreachable private object\n", check=True, capture_output=True,
            )
        else:
            (view.path / "info" / "grafts").write_text("")
        with pytest.raises(InvariantError, match="ISOLATION"):
            check_invariants(fixture.root)
    finally:
        fixture.close()
