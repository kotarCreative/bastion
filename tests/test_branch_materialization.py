"""Multi-branch canonical cache recovery and projection isolation."""

import shutil
import subprocess

import pytest

from errors import ForgeError
import forge
from invariants import check_invariants
from layout import canonical_repo, view_repo
from materialization import clear_materialization_registrations, ensure_canonical_materialized
from repository import Repository
from service import create_branch
from tests.identity_harness import provision_admin
from tests.tree_harness import TreeFixture


@pytest.fixture
def fixture():
    value = TreeFixture()
    yield value
    value.close()


def _refs(repo):
    return subprocess.run(
        ["git", "--git-dir", str(repo), "for-each-ref", "--format=%(refname) %(objectname)"],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()


def test_secondary_canonical_ref_survives_cache_rebuild_without_entering_view(fixture):
    admin = provision_admin(fixture.root)
    created = create_branch(fixture.root, "feature/private", admin)
    manifest, _version, replay = Repository.open(fixture.root).load()
    secondary = "refs/heads/feature/private"
    assert manifest.canonical_refs[secondary] == created["tip"]
    check_invariants(fixture.root)
    canonical_listing = subprocess.run(
        ["git", "--git-dir", str(canonical_repo(fixture.root)), "ls-tree", "-r", "--name-only", created["tip"]],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    assert "hidden/secret.txt" in canonical_listing
    assert subprocess.run(
        ["git", "--git-dir", str(canonical_repo(fixture.root)), "cat-file", "-e", fixture.hidden_oid],
        capture_output=True,
    ).returncode == 0

    canonical_link = canonical_repo(fixture.root)
    old_generation = canonical_link.resolve()
    shutil.rmtree(old_generation)
    clear_materialization_registrations()
    rebuilt = ensure_canonical_materialized(fixture.root, manifest)
    inventory = dict(line.split() for line in _refs(rebuilt))
    assert inventory == dict(manifest.canonical_refs)
    assert all(
        subprocess.run(
            ["git", "--git-dir", str(rebuilt), "cat-file", "-t", tip],
            check=True, capture_output=True, text=True,
        ).stdout.strip() == "commit"
        for tip in inventory.values()
    )
    assert subprocess.run(
        ["git", "--git-dir", str(rebuilt), "symbolic-ref", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip() == manifest.canonical_ref

    # The projected view remains strictly single-ref and excludes hidden data.
    forge.sync_view(fixture.root, "alice")
    forge_view = view_repo(fixture.root, "alice")
    view = forge_view.resolve()
    assert len(_refs(view)) == 1
    paths = subprocess.run(
        ["git", "--git-dir", str(view), "ls-tree", "-r", "--name-only", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    assert "hidden/secret.txt" not in paths
    assert subprocess.run(
        ["git", "--git-dir", str(view), "cat-file", "-e", fixture.hidden_oid],
        capture_output=True,
    ).returncode != 0


def test_canonical_secondary_ref_tampering_is_detected_and_repaired(fixture):
    admin = provision_admin(fixture.root)
    created = create_branch(fixture.root, "side/branch", admin)
    manifest, _version, _replay = Repository.open(fixture.root).load()
    generation = canonical_repo(fixture.root).resolve()
    subprocess.run(
        ["git", "--git-dir", str(generation), "update-ref", "refs/heads/side/branch", "0" * 40],
        check=True, capture_output=True,
    )
    clear_materialization_registrations()
    repaired = ensure_canonical_materialized(fixture.root, manifest)
    assert repaired != generation
    assert dict(line.split() for line in _refs(repaired))["refs/heads/side/branch"] == created["tip"]


def test_wrong_canonical_head_is_repaired(fixture):
    admin = provision_admin(fixture.root)
    create_branch(fixture.root, "secondary", admin)
    manifest, _version, _replay = Repository.open(fixture.root).load()
    generation = canonical_repo(fixture.root).resolve()
    subprocess.run(
        ["git", "--git-dir", str(generation), "symbolic-ref", "HEAD", "refs/heads/secondary"],
        check=True, capture_output=True,
    )
    clear_materialization_registrations()
    repaired = ensure_canonical_materialized(fixture.root, manifest)
    assert repaired != generation
    assert subprocess.run(
        ["git", "--git-dir", str(repaired), "symbolic-ref", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip() == manifest.canonical_ref


def test_fork_replay_uses_historical_branch_tip(fixture):
    admin = provision_admin(fixture.root)
    first = create_branch(fixture.root, "fork", admin)
    fixture.push_source("alice", {b"shared/main.txt": (b"100644", b"advanced default\n")})
    second = create_branch(fixture.root, "fork-again", admin, source_branch="fork")
    assert second["tip"] == first["tip"]
    manifest, _version, _replay = Repository.open(fixture.root).load()
    assert manifest.canonical_refs["refs/heads/fork"] == first["tip"]
    assert manifest.canonical_refs["refs/heads/fork-again"] == first["tip"]
    check_invariants(fixture.root)


def test_full_ref_branch_with_leading_dash_passes_independent_check(fixture):
    admin = provision_admin(fixture.root)
    result = create_branch(fixture.root, "refs/heads/-topic", admin)
    assert result["created"]
    check_invariants(fixture.root)


def test_branch_materialization_fails_closed_for_invalid_manifest_ref_map(fixture):
    manifest, _version, _replay = Repository.open(fixture.root).load()
    object.__setattr__(manifest, "canonical_refs", {manifest.canonical_ref: "0" * 40})
    with pytest.raises(ForgeError):
        ensure_canonical_materialized(fixture.root, manifest)
