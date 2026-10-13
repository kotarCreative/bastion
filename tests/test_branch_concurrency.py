"""Adversarial races around canonical branch creation."""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock

import pytest

from errors import ForgeError
from service import create_branch, init_forge, list_branches, update_canonical_ref
from tests.identity_harness import provision_admin


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


@pytest.fixture
def forge_root(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.test")
    (source / "file").write_text("base\n")
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "base\n\nChange-Id: Ibranchbase123456")
    root = tmp_path / "forge"
    init_forge(root, source, "main")
    return root


def test_distinct_branch_creations_racing_preserve_both(forge_root, monkeypatch):
    import repository as repository_module

    admin = provision_admin(forge_root)
    original = repository_module.Repository.publish
    rendezvous = Barrier(2)
    builds = 0
    builds_lock = Lock()

    def synchronized_publish(repository, build_event):
        first_build = True

        def synchronized_build(manifest, replay):
            nonlocal first_build, builds
            candidate = build_event(manifest, replay)
            with builds_lock:
                builds += 1
            if first_build:
                first_build = False
                rendezvous.wait(timeout=20)
            return candidate

        return original(repository, synchronized_build)

    monkeypatch.setattr(repository_module.Repository, "publish", synchronized_publish)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda branch: create_branch(forge_root, branch, admin),
            ("race/one", "race/two"),
        ))
    assert all(result["created"] for result in results)
    assert builds >= 3, f"expected one CAS loser to rebuild; observed {builds} candidate builds"
    rows = {row["ref"]: row for row in list_branches(forge_root)}
    assert {"refs/heads/race/one", "refs/heads/race/two"} <= rows.keys()
    assert rows["refs/heads/race/one"]["tip"] == rows["refs/heads/race/two"]["tip"]


def test_same_branch_race_has_one_creation_and_other_identity_conflicts(forge_root, monkeypatch):
    import repository as repository_module

    admin = provision_admin(forge_root)
    other_admin = provision_admin(forge_root, "other-admin")
    original = repository_module.Repository.publish
    rendezvous = Barrier(2)

    def synchronized_publish(repository, build_event):
        first_build = True

        def synchronized_build(manifest, replay):
            nonlocal first_build
            candidate = build_event(manifest, replay)
            if first_build:
                first_build = False
                assert candidate.event_fingerprint["kind"] == "branch_create"
                rendezvous.wait(timeout=20)
            return candidate

        return original(repository, synchronized_build)

    monkeypatch.setattr(repository_module.Repository, "publish", synchronized_publish)
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(create_branch, forge_root, "race/shared", principal)
                 for principal in (admin, other_admin)]
        outcomes = []
        for call in calls:
            try:
                outcomes.append(call.result())
            except ForgeError as error:
                outcomes.append(error)
    assert sum(isinstance(result, dict) for result in outcomes) == 1
    assert sum(isinstance(result, ForgeError) and result.category == "conflict"
               for result in outcomes) == 1
    assert [row["ref"] for row in list_branches(forge_root)].count("refs/heads/race/shared") == 1


def test_unauthorized_attempt_does_not_change_manifest_or_objects(forge_root):
    from repository import Repository
    from service import add_principal

    add_principal(forge_root, "regular-user")
    repository = Repository.open(forge_root)
    before_manifest = repository.load()[0].to_dict()
    before_objects = tuple((meta.key, meta.size, meta.version.value)
                           for meta in repository.store.list_prefix(f"{repository.prefix}/"))
    with pytest.raises(ForgeError):
        create_branch(forge_root, "forbidden", "regular-user")
    assert repository.load(use_cache=False)[0].to_dict() == before_manifest
    after_objects = tuple((meta.key, meta.size, meta.version.value)
                          for meta in repository.store.list_prefix(f"{repository.prefix}/"))
    assert after_objects == before_objects


def test_source_advance_between_capture_and_publish_refuses_branch(forge_root, tmp_path, monkeypatch):
    import repository as repository_module

    admin = provision_admin(forge_root)
    original = repository_module.Repository.publish
    advanced = False

    def advance_default():
        nonlocal advanced
        assert not advanced
        advanced = True
        # Build a new valid commit and move the default through the normal service path.
        canonical_path = forge_root / "canonical.git"
        checkout = tmp_path / "advance"
        subprocess.run(["git", "clone", "-q", str(canonical_path), str(checkout)], check=True)
        git(checkout, "config", "user.name", "Fixture")
        git(checkout, "config", "user.email", "fixture@example.test")
        (checkout / "file").write_text("advanced\n")
        git(checkout, "commit", "-qam", "advance")
        new_tip = git(checkout, "rev-parse", "HEAD")
        old_tip = next(row["tip"] for row in list_branches(forge_root)
                       if row["ref"] == "refs/heads/main")
        subprocess.run(["git", "-C", str(forge_root / "admin-staging.git"), "fetch",
                        str(checkout), f"{new_tip}:refs/staging/next"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        update_canonical_ref(forge_root, old_tip, new_tip, admin)

    def advance_before_branch_cas(repository, build_event):
        def build_with_advancing_prerequisite(manifest, replay):
            candidate = build_event(manifest, replay)
            if candidate.event_fingerprint["kind"] == "branch_create":
                candidate.prerequisites = (*candidate.prerequisites, lambda _target: advance_default())
            return candidate

        return original(repository, build_with_advancing_prerequisite)

    monkeypatch.setattr(repository_module.Repository, "publish", advance_before_branch_cas)
    with pytest.raises(ForgeError, match="source branch changed"):
        create_branch(forge_root, "stale-source", admin)
    assert "refs/heads/stale-source" not in {row["ref"] for row in list_branches(forge_root)}
