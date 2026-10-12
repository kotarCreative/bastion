import os
import multiprocessing
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from errors import ForgeError
from host import Host


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()


def source_repo(path):
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "README").write_text("source\n")
    git(path, "add", "README")
    env = dict(os.environ, GIT_AUTHOR_NAME="A", GIT_AUTHOR_EMAIL="a@example.com",
               GIT_COMMITTER_NAME="A", GIT_COMMITTER_EMAIL="a@example.com")
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], env=env, check=True)
    return path


def test_zero_repo_account_multi_repo_independent_and_owner_capabilities(tmp_path):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["Alice@example.com"])
    host.link_clerk_account("user_alice", ["alice@example.com"])
    assert host.list_repos() == []
    a = host.create_repo("alice", "one", request_id="one", seed_empty=True)
    b = host.create_repo("alice", "two", request_id="two", seed_empty=True)
    assert a["state"] == b["state"] == "ready"
    assert a["repo_id"] != b["repo_id"] and a["repository_id"] != b["repository_id"]
    assert host.create_repo("alice", "one", request_id="one", seed_empty=True)["repo_id"] == a["repo_id"]
    from repository import Repository
    for row in (a, b):
        repo = Repository.open(host.root / "repos" / row["repo_id"])
        _, _, replay = repo.load(use_cache=False)
        owner = replay.active_views["owner"]
        descriptor = repo._descriptor(owner["descriptor_id"])
        assert all(grant == [""] for grant in descriptor["capabilities"].values())
        assert owner["principal_id"] == "owner"
        assert host.list_repos()[0]["state"] == "ready"
    assert len(host.list_repos("alice")) == 2


def test_accounts_can_reuse_repo_name_but_request_inputs_are_pinned(tmp_path):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["a@example.com"])
    host.approve_account("bob", ["b@example.com"])
    assert host.create_repo("alice", "same", request_id="a", seed_empty=True)["state"] == "ready"
    assert host.create_repo("bob", "same", request_id="b", seed_empty=True)["state"] == "ready"
    with pytest.raises(ForgeError, match="different inputs"):
        host.create_repo("alice", "other", request_id="a", seed_empty=True)
    with pytest.raises(ForgeError, match="unknown or unapproved"):
        host.create_repo("nobody", "repo", request_id="c", seed_empty=True)


@pytest.mark.parametrize("name", ["../escape", "Upper", ".", "api", "x.git"])
def test_rejects_noncanonical_names(tmp_path, name):
    host = Host(tmp_path / "host")
    with pytest.raises(ForgeError):
        host.create_repo("alice", name, request_id="x", seed_empty=True)


def test_symlink_and_remote_backend_rejected(tmp_path, monkeypatch):
    host_root = tmp_path / "host"
    target = tmp_path / "target"
    target.mkdir()
    host_root.symlink_to(target)
    with pytest.raises(ForgeError):
        Host(host_root)
    monkeypatch.setenv("BASTION_DATABASE", "durable_objects")
    with pytest.raises(ForgeError, match="ambient"):
        Host(tmp_path / "other")


def test_read_list_does_not_create_host_catalog(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(ForgeError, match="does not exist"):
        Host(missing, initialize=False)
    assert not missing.exists()


def test_link_cannot_be_reassigned_and_suspended_link_is_denied(tmp_path):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["alice@example.com"])
    host.link_clerk_account("user_first", ["alice@example.com"])
    with pytest.raises(ForgeError):
        host.link_clerk_account("user_second", ["alice@example.com"])
    with host._connect() as con:
        con.execute("UPDATE accounts SET state='suspended'")
    with pytest.raises(ForgeError):
        host.link_clerk_account("user_first", ["alice@example.com"])


def test_linked_clerk_identity_survives_verified_email_change(tmp_path):
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.com"])
    assert host.link_clerk_account("user_first", ["alice@example.com"]) == account
    assert host.link_clerk_account("user_first", ["new-address@example.com"]) == account


def test_clerk_link_requires_unambiguous_approved_account(tmp_path):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["shared@example.com"])
    host.approve_account("bob", ["shared@example.com"])
    with pytest.raises(ForgeError):
        host.link_clerk_account("user_ambiguous", ["shared@example.com"])
    with host._connect() as con:
        con.execute("UPDATE accounts SET state='suspended'")
    with pytest.raises(ForgeError):
        host.link_clerk_account("user_suspended", ["shared@example.com"])


def test_owner_clerk_link_persists_to_repository(tmp_path):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["alice@example.com"])
    host.link_clerk_account("user_first", ["alice@example.com"])
    row = host.create_repo("alice", "repo", request_id="r", seed_empty=True)
    from state import load_state
    state = load_state(host.root / "repos" / row["repo_id"])
    assert state["principals"]["owner"].get("clerk_user_id") == "user_first"


def test_account_linked_after_provisioning_can_retry_without_recreating_grants(tmp_path):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["alice@example.com"])
    row = host.create_repo("alice", "repo", request_id="r", seed_empty=True)
    root = host.root / "repos" / row["repo_id"]
    from state import load_state
    roles = load_state(root)["roles"]
    host.link_clerk_account("user_first", ["alice@example.com"])
    retry = host.create_repo("alice", "repo", request_id="r", seed_empty=True)
    assert retry["repo_id"] == row["repo_id"]
    state = load_state(root)
    assert state["principals"]["owner"]["clerk_user_id"] == "user_first"
    assert state["roles"] == roles


def test_retry_initialization_after_repository_config_written(tmp_path, monkeypatch):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["alice@example.com"])
    import service
    original = service.ensure_canonical_materialized
    attempts = []
    def interrupt(*args, **kwargs):
        if not attempts:
            attempts.append(True)
            raise RuntimeError("interrupted inside forge initialization")
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "ensure_canonical_materialized", interrupt)
    with pytest.raises(RuntimeError):
        host.create_repo("alice", "repo", request_id="r", seed_empty=True)
    assert host.create_repo("alice", "repo", request_id="r", seed_empty=True)["state"] == "ready"


def test_retry_interrupted_seed_creation(tmp_path, monkeypatch):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["alice@example.com"])
    original = subprocess.run
    attempts = []
    def interrupt(args, *rest, **kwargs):
        if "commit-tree" in args and not attempts:
            attempts.append(True)
            raise RuntimeError("interrupted seed creation")
        return original(args, *rest, **kwargs)
    monkeypatch.setattr(subprocess, "run", interrupt)
    with pytest.raises(RuntimeError):
        host.create_repo("alice", "repo", request_id="r", seed_empty=True)
    assert host.create_repo("alice", "repo", request_id="r", seed_empty=True)["state"] == "ready"


@pytest.mark.parametrize("stage", ["reserved", "before_forge_init", "forge_initialized", "owner_provisioned"])
def test_provisioning_stage_failure_recovers_without_replacing_root(tmp_path, stage):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["alice@example.com"])
    hit = []
    def fail(name):
        if name == stage and not hit:
            hit.append(name)
            raise RuntimeError("stage interrupted")
    with pytest.raises(RuntimeError):
        host.create_repo("alice", "repo", request_id="r", seed_empty=True, failpoint=fail)
    root_id = host.list_repos()[0]["repo_id"]
    result = host.create_repo("alice", "repo", request_id="r", seed_empty=True)
    assert result["repo_id"] == root_id and result["state"] == "ready"


def test_seeded_owner_clone_commit_and_push(tmp_path):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["alice@example.com"])
    row = host.create_repo("alice", "repo", request_id="r", seed_empty=True)
    root = host.root / "repos" / row["repo_id"]
    client = tmp_path / "client"
    git(client.parent, "clone", "-q", "--no-local", str(root / "views" / "owner.git"), str(client))
    from transport import attach_intake_push
    attach_intake_push(root, client, "owner")
    git(client, "config", "user.name", "Alice")
    git(client, "config", "user.email", "alice@example.com")
    (client / "README").write_text("first commit\n")
    git(client, "add", "README")
    git(client, "commit", "-qm", "first commit on seed")
    git(client, "push", "-q", "origin", "main")
    from service import sync_view
    sync_view(root, "owner")
    assert git(client, "rev-parse", "HEAD") == git(root / "views" / "owner.git", "rev-parse", "refs/heads/main")


def test_ready_import_retry_after_push_uses_initial_import_evidence(tmp_path):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["a@example.com"])
    source = source_repo(tmp_path / "source")
    row = host.create_repo("alice", "imported", request_id="r", source=source)
    root = host.root / "repos" / row["repo_id"]
    client = tmp_path / "client"
    git(client.parent, "clone", "-q", "--no-local", str(root / "views" / "owner.git"), str(client))
    from transport import attach_intake_push
    attach_intake_push(root, client, "owner")
    git(client, "config", "user.name", "Alice")
    git(client, "config", "user.email", "a@example.com")
    (client / "README").write_text("updated by push\n")
    git(client, "commit", "-qam", "normal push after import")
    git(client, "push", "-q", "origin", "main")
    retry = host.create_repo("alice", "imported", request_id="r", source=source)
    assert retry["repo_id"] == row["repo_id"] and retry["state"] == "ready"


def test_cross_repository_token_is_rejected(tmp_path):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["alice@example.com"])
    one = host.create_repo("alice", "one", request_id="one", seed_empty=True)
    two = host.create_repo("alice", "two", request_id="two", seed_empty=True)
    from http_server import add_token, authenticate_token
    root_one, root_two = (host.root / "repos" / row["repo_id"] for row in (one, two))
    credential = add_token(root_one, "owner", scopes=["fetch"])
    assert authenticate_token(root_one, credential) is not None
    assert authenticate_token(root_two, credential) is None


def _create_same_request(host_path, queue):
    try:
        queue.put(Host(host_path).create_repo("alice", "same", request_id="same", seed_empty=True)["repo_id"])
    except Exception as error:  # surfaced in the parent assertion
        queue.put(type(error).__name__ + ":" + str(error))


def _initialize_host_concurrently(host_path, barrier, queue):
    try:
        barrier.wait(timeout=10)
        Host(host_path)
        queue.put("ready")
    except Exception as error:
        queue.put(type(error).__name__ + ":" + str(error))


def test_initial_catalog_creation_is_atomic_across_processes(tmp_path):
    host_path = tmp_path / "host"
    barrier = multiprocessing.Barrier(2)
    queue = multiprocessing.Queue()
    workers = [multiprocessing.Process(target=_initialize_host_concurrently,
               args=(host_path, barrier, queue)) for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        for worker in workers:
            worker.join(20)
        assert all(worker.exitcode == 0 for worker in workers)
        assert [queue.get(timeout=2) for _ in workers] == ["ready", "ready"]
        assert Host(host_path, initialize=False).list_repos() == []
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)


def _create_name_collision(host_path, request_id, queue):
    try:
        queue.put(Host(host_path).create_repo("alice", "collision", request_id=request_id,
                                               seed_empty=True)["repo_id"])
    except Exception as error:
        queue.put(type(error).__name__ + ":" + str(error))


def test_same_request_is_serialized_across_processes(tmp_path):
    host_path = tmp_path / "host"
    host = Host(host_path)
    host.approve_account("alice", ["alice@example.com"])
    queue = multiprocessing.Queue()
    workers = [multiprocessing.Process(target=_create_same_request, args=(host_path, queue)) for _ in range(2)]
    for worker in workers: worker.start()
    for worker in workers: worker.join(30)
    assert all(worker.exitcode == 0 for worker in workers)
    ids = [queue.get(timeout=2) for _ in workers]
    assert ids[0] == ids[1] and len(host.list_repos()) == 1
    assert host.list_repos()[0]["state"] == "ready"


def test_owner_name_collision_is_reserved_once_across_processes(tmp_path):
    host_path = tmp_path / "host"
    host = Host(host_path)
    host.approve_account("alice", ["alice@example.com"])
    queue = multiprocessing.Queue()
    workers = [multiprocessing.Process(target=_create_name_collision, args=(host_path, f"name-{i}", queue))
               for i in range(2)]
    for worker in workers: worker.start()
    for worker in workers: worker.join(30)
    assert all(worker.exitcode == 0 for worker in workers)
    results = [queue.get(timeout=2) for _ in workers]
    assert sum(isinstance(result, str) and len(result) == 32 for result in results) == 1
    assert len(host.list_repos()) == 1


def test_lost_ready_response_retry_returns_same_repo(tmp_path):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["alice@example.com"])
    with pytest.raises(RuntimeError, match="lost response"):
        host.create_repo("alice", "repo", request_id="r", seed_empty=True,
                         failpoint=lambda stage: (_ for _ in ()).throw(RuntimeError("lost response")) if stage == "ready" else None)
    assert host.list_repos()[0]["state"] == "ready"
    assert host.create_repo("alice", "repo", request_id="r", seed_empty=True)["state"] == "ready"


def test_configured_repo_budget_counts_failed_reservations(tmp_path):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["alice@example.com"], repo_limit=1)
    with pytest.raises(RuntimeError):
        host.create_repo("alice", "reserved", request_id="one", seed_empty=True,
                         failpoint=lambda stage: (_ for _ in ()).throw(RuntimeError("stop")) if stage == "reserved" else None)
    with pytest.raises(ForgeError, match="capacity"):
        host.create_repo("alice", "second", request_id="two", seed_empty=True)


def test_unknown_roots_and_late_symlinks_fail_closed(tmp_path):
    unknown = tmp_path / "unknown"
    unknown.mkdir()
    (unknown / "notes").write_text("not a host")
    with pytest.raises(ForgeError):
        Host(unknown)
    direct_forge = tmp_path / "direct-forge"
    direct_forge.mkdir()
    (direct_forge / "repository.json").write_text("{}")
    with pytest.raises(ForgeError, match="forge root"):
        Host(direct_forge)
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["alice@example.com"])
    repos = host.root / "repos"
    displaced = tmp_path / "repos-original"
    repos.rename(displaced)
    repos.symlink_to(displaced, target_is_directory=True)
    with pytest.raises(ForgeError):
        host.list_repos()


def test_list_repos_does_not_recreate_deleted_catalog(tmp_path):
    host = Host(tmp_path / "host")
    host.db.unlink()
    with pytest.raises(ForgeError):
        host.list_repos()
    assert not host.db.exists()


def test_unrelated_sqlite_database_is_not_adopted(tmp_path):
    path = tmp_path / "host"
    path.mkdir()
    import sqlite3
    con = sqlite3.connect(path / "host.sqlite3")
    con.execute("CREATE TABLE user_data(value TEXT)")
    con.close()
    with pytest.raises(ForgeError, match="not a host catalog"):
        Host(path)


def test_source_mutation_after_reservation_is_refused(tmp_path):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["alice@example.com"])
    source = source_repo(tmp_path / "source")
    def mutate(stage):
        if stage == "reserved":
            git(source, "config", "user.name", "A")
            git(source, "config", "user.email", "a@example.com")
            (source / "README").write_text("mutated\n")
            git(source, "commit", "-qam", "mutation")
    with pytest.raises(ForgeError, match="changed"):
        host.create_repo("alice", "import", request_id="pinned", source=source, failpoint=mutate)


def test_ready_retry_detects_owner_policy_corruption(tmp_path):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["alice@example.com"])
    row = host.create_repo("alice", "repo", request_id="r", seed_empty=True)
    root = host.root / "repos" / row["repo_id"]
    from state import load_state, save_state
    state = load_state(root)
    state["principals"]["owner"]["emails"] = ["intruder@example.com"]
    save_state(root, state)
    with pytest.raises(ForgeError, match="owner email identity"):
        host.create_repo("alice", "repo", request_id="r", seed_empty=True)


def test_ready_retry_does_not_restore_missing_owner_grants(tmp_path):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["alice@example.com"])
    row = host.create_repo("alice", "repo", request_id="r", seed_empty=True)
    root = host.root / "repos" / row["repo_id"]
    from state import load_state, save_state
    state = load_state(root)
    state["role_bindings"] = {}
    save_state(root, state)
    with pytest.raises(ForgeError, match="grants changed"):
        host.create_repo("alice", "repo", request_id="r", seed_empty=True)
    assert load_state(root)["role_bindings"] == {}


def test_reserved_root_replacement_is_not_adopted(tmp_path):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["alice@example.com"])
    with pytest.raises(RuntimeError):
        host.create_repo("alice", "repo", request_id="r", seed_empty=True,
                         failpoint=lambda stage: (_ for _ in ()).throw(RuntimeError("stop"))
                         if stage == "forge_initialized" else None)
    row = host.list_repos()[0]
    root = host.root / "repos" / row["repo_id"]
    root.rename(tmp_path / "original-root")
    root.mkdir()
    (root / "foreign-data").write_text("do not adopt or discard\n")
    with pytest.raises(ForgeError, match="identity changed"):
        host.create_repo("alice", "repo", request_id="r", seed_empty=True)
    assert (root / "foreign-data").read_text() == "do not adopt or discard\n"


def test_ready_retry_rejects_remote_storage_before_opening_catalog(tmp_path, monkeypatch):
    import json
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["alice@example.com"])
    row = host.create_repo("alice", "repo", request_id="r", seed_empty=True)
    root = host.root / "repos" / row["repo_id"]
    config = json.loads((root / "backend.json").read_text())
    config["backend"] = "durable_objects"
    (root / "backend.json").write_text(json.dumps(config))
    import store

    def unexpected_open(*args, **kwargs):
        raise AssertionError("must reject remote configuration before opening a store")

    monkeypatch.setattr(store, "open_store", unexpected_open)
    with pytest.raises(ForgeError, match="not local SQLite"):
        host.create_repo("alice", "repo", request_id="r", seed_empty=True)


def test_import_retry_source_mutation_and_failure_recovery(tmp_path):
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["a@example.com"])
    source = source_repo(tmp_path / "source")
    count = [0]
    def fail(stage):
        if stage == "forge_initialized" and count[0] == 0:
            count[0] += 1
            raise RuntimeError("injected")
    with pytest.raises(RuntimeError):
        host.create_repo("alice", "imported", request_id="imp", source=source, failpoint=fail)
    assert host.list_repos()[0]["state"] == "failed"
    row = host.create_repo("alice", "imported", request_id="imp", source=source)
    assert row["state"] == "ready"
    git(source, "config", "user.name", "A")
    git(source, "config", "user.email", "a@example.com")
    (source / "README").write_text("changed\n")
    git(source, "commit", "-am", "change")
    with pytest.raises(ForgeError, match="different inputs"):
        host.create_repo("alice", "imported", request_id="imp", source=source)


def test_cli_host_smoke(tmp_path):
    host = tmp_path / "host"
    def cli(*args):
        return subprocess.run([sys.executable, str(ROOT / "forge.py"), *map(str, args)],
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    approved = cli("approve-host-account", host, "alice", "--email", "a@example.com")
    assert approved.returncode == 0, approved.stderr
    linked = cli("link-host-account", host, "user_cli", "--email", "a@example.com")
    assert linked.returncode == 0, linked.stderr
    created = cli("create-repo", host, "alice", "demo", "--request-id", "cli1", "--seed-empty")
    assert created.returncode == 0, created.stderr
    assert '"state": "ready"' in created.stdout
    listed = cli("list-repos", host)
    assert listed.returncode == 0 and '"name": "demo"' in listed.stdout
