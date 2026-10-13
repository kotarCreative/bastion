import json
import subprocess
import sys
from pathlib import Path

import pytest

from errors import ForgeError
from service import create_branch, init_forge, list_branches, update_canonical_ref
from tests.identity_harness import provision_admin


PROJECT = Path(__file__).resolve().parents[1]


def git(path, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


@pytest.fixture
def forge_root(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.test")
    (source / "visible").write_text("visible\n")
    (source / ".hidden").write_text("private\n")
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "base\n\nChange-Id: Ibranchbase123456")
    root = tmp_path / "forge"
    init_forge(root, source, "main")
    return root


def advance_default(root, tmp_path, principal, text="advanced\n"):
    from layout import canonical_repo
    from git import ref

    canonical = canonical_repo(root)
    old_tip = ref(canonical, "refs/heads/main")
    checkout = tmp_path / f"advance-{len(list_branches(root))}"
    subprocess.run(["git", "clone", "-q", str(canonical), str(checkout)], check=True)
    git(checkout, "config", "user.name", "Fixture")
    git(checkout, "config", "user.email", "fixture@example.test")
    (checkout / "visible").write_text(text)
    git(checkout, "commit", "-qam", "advance")
    new_tip = git(checkout, "rev-parse", "HEAD")
    subprocess.run(
        ["git", "-C", str(root / "admin-staging.git"), "fetch", str(checkout),
         f"{new_tip}:refs/staging/next"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    update_canonical_ref(root, old_tip, new_tip, principal)
    return new_tip


def test_create_nested_and_from_secondary_and_default_stays_pinned(forge_root):
    admin = provision_admin(forge_root)
    default_tip = list_branches(forge_root)[0]["tip"]
    assert create_branch(forge_root, "feature/login", admin)["tip"] == default_tip
    advanced_tip = advance_default(forge_root, Path(forge_root).parent, admin)
    # A same-request retry succeeds despite the source having since moved.
    retry = create_branch(forge_root, "feature/login", admin)
    assert retry["tip"] == default_tip and not retry["created"]
    secondary = create_branch(forge_root, "release/candidate", admin, "feature/login")
    assert secondary["tip"] == default_tip
    rows = {item["ref"]: item for item in list_branches(forge_root)}
    assert rows["refs/heads/main"]["tip"] == advanced_tip
    assert rows["refs/heads/feature/login"]["tip"] == default_tip
    assert rows["refs/heads/release/candidate"]["tip"] == default_tip
    assert rows["refs/heads/main"]["default"]


def test_root_admin_only_namespace_validation_and_conflict_identity(forge_root):
    from service import add_principal

    add_principal(forge_root, "regular-user")
    with pytest.raises(ForgeError):
        create_branch(forge_root, "unauthorized", "regular-user")
    admin = provision_admin(forge_root)
    other_admin = provision_admin(forge_root, "second-admin")
    for malformed in ("bad..name", "bad/name.lock", "refs/tags/nope", None, 7):
        with pytest.raises(ForgeError):
            create_branch(forge_root, malformed, admin)
    with pytest.raises(ForgeError):
        create_branch(forge_root, "ok", admin, 42)
    create_branch(forge_root, "feature", admin)
    for name in ("main", "feature/sub"):
        with pytest.raises(ForgeError):
            create_branch(forge_root, name, admin)
    with pytest.raises(ForgeError):
        create_branch(forge_root, "feature", other_admin)


def test_branch_event_does_not_change_change_id_claims(forge_root):
    from repository import Repository

    admin = provision_admin(forge_root)
    repository = Repository.open(forge_root)
    before = dict(repository.load()[2].change_claims)
    create_branch(forge_root, "fork", admin)
    replay = repository.load(use_cache=False)[2]
    assert replay.change_claims == before
    event = replay.records[-1]
    assert event["kind"] == "branch_create"
    assert set(event["payload"]) == {
        "principal_id", "descriptor_id", "source_ref", "branch_ref",
        "canonical_tip", "received_at",
    }


def test_cli_create_and_operator_inventory(forge_root):
    admin = provision_admin(forge_root)
    created = subprocess.run(
        [sys.executable, str(PROJECT / "forge.py"), "create-branch", str(forge_root),
         "feature/cli", "--principal", admin],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(created.stdout)["ref"] == "refs/heads/feature/cli"
    listed = subprocess.run(
        [sys.executable, str(PROJECT / "forge.py"), "list-branches", str(forge_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    inventory = [json.loads(line) for line in listed.stdout.splitlines()]
    assert {row["ref"] for row in inventory} == {
        "refs/heads/main", "refs/heads/feature/cli"
    }


def test_real_projected_clone_push_does_not_publish_hidden_files_or_secondary_ref(
    forge_root, tmp_path
):
    import threading

    from http_server import add_token, make_server
    from tests.identity_harness import add_view

    admin = provision_admin(forge_root)
    fork = create_branch(forge_root, "fork", admin)
    add_view(forge_root, "alice", ["visible"], ["visible"])
    token = add_token(forge_root, "alice", scopes=["fetch", "push"])
    server = make_server(forge_root, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://alice:{token}@127.0.0.1:{server.server_port}/alice.git"
        checkout = tmp_path / "projected"
        subprocess.run(["git", "clone", "-q", "--no-local", url, str(checkout)], check=True)
        assert not (checkout / ".hidden").exists()
        git(checkout, "config", "user.name", "Alice")
        git(checkout, "config", "user.email", "alice@example.test")
        (checkout / "visible").write_text("updated visible file\n")
        git(checkout, "add", "-A")
        git(checkout, "commit", "-q", "-m", "projected change")
        git(checkout, "push", "-q", "origin", "main")
        refs = git(checkout, "ls-remote", "origin")
        assert "refs/heads/main" in refs
        assert "refs/heads/fork" not in refs
        assert ".hidden" not in git(checkout, "show", "HEAD")
        assert {row["ref"]: row["tip"] for row in list_branches(forge_root)}[
            "refs/heads/fork"
        ] == fork["tip"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_source_advance_during_cas_fails_without_repinning(forge_root, tmp_path, monkeypatch):
    import repository as repository_module

    admin = provision_admin(forge_root)
    original_publish = repository_module.Repository.publish
    advanced = False

    def publish(repository, build_event):
        nonlocal advanced
        if not advanced:
            advanced = True
            advance_default(forge_root, tmp_path, admin)
        return original_publish(repository, build_event)

    monkeypatch.setattr(repository_module.Repository, "publish", publish)
    with pytest.raises(ForgeError, match="source branch changed"):
        create_branch(forge_root, "must-not-appear", admin)
    assert "refs/heads/must-not-appear" not in {
        row["ref"] for row in list_branches(forge_root)
    }
