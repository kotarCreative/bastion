"""Independent regressions for import isolation, recovery, and history checks."""

import hashlib
import io
import os
import struct
import subprocess
import sys
import threading
import zlib

import pytest

from errors import ForgeError
from host import Host
import repository_import


def git(path, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="Historical Author", GIT_COMMITTER_NAME="Historical Author",
               GIT_AUTHOR_EMAIL="historical@example.test", GIT_COMMITTER_EMAIL="historical@example.test")
    return subprocess.run(["git", "-C", str(path), *args], env=env, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30).stdout


@pytest.fixture
def source_bundle(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    git(source, "init", "-b", "main")
    (source / "README.md").write_text("existing project\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "initial")
    return source


def bundle_bytes(source, target):
    git(source, "bundle", "create", "--version=2", str(target), "refs/heads/main")
    return target.read_bytes()


def operation(host, account, data, name="project", request_id="import-project"):
    payload = {"name": name, "branch": "main", "request_id": request_id,
               "bundle_sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
    row = repository_import.reserve(host, account, payload, 32 << 20)
    return row, payload


def staged(host, account, data, **kwargs):
    row, _payload = operation(host, account, data, **kwargs)
    row = repository_import.upload(host, account, row["import_id"], io.BytesIO(data), len(data), 32 << 20)
    assert row["state"] == "uploaded"
    return row


def test_upload_retry_recovers_bytes_published_before_status_update(tmp_path, source_bundle):
    data = bundle_bytes(source_bundle, tmp_path / "repo.bundle")
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    row = staged(host, account, data)
    with host._connect() as con:
        con.execute("UPDATE imports SET state='awaiting-upload' WHERE import_id=?", (row["import_id"],))
    restarted = Host(host.root, initialize=False, migrate=True)
    retry = repository_import.upload(restarted, account, row["import_id"], io.BytesIO(data), len(data), 32 << 20)
    assert retry["state"] == "uploaded"
    completed = repository_import.complete(restarted, account, row["import_id"])
    assert completed["state"] == "ready"
    assert len(restarted.public_repos(account)) == 1


def test_empty_stage_creation_interruption_is_recoverable(tmp_path, source_bundle):
    data = bundle_bytes(source_bundle, tmp_path / "repo.bundle")
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    row, _ = operation(host, account, data)
    stage = host.root / "imports" / row["import_id"]
    stage.mkdir(mode=0o700)
    assert row["stage_dev"] is None
    recovered = repository_import.upload(host, account, row["import_id"], io.BytesIO(data), len(data), 32 << 20)
    assert recovered["state"] == "uploaded"
    assert recovered["stage_dev"] == stage.stat().st_dev
    assert recovered["stage_ino"] == stage.stat().st_ino


def test_unknown_matching_source_is_not_adopted_or_executed(tmp_path, source_bundle, monkeypatch):
    data = bundle_bytes(source_bundle, tmp_path / "repo.bundle")
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    row = staged(host, account, data)
    source = host.root / "imports" / row["import_id"] / "source.git"
    git(source_bundle, "clone", "--bare", str(source_bundle), str(source))
    (source / "foreign-marker").write_text("not created by the importer")
    before = source.stat()
    git_calls = []
    original_run = repository_import._run
    def observed_run(args, **kwargs):
        git_calls.append(args)
        return original_run(args, **kwargs)
    monkeypatch.setattr(repository_import, "_run", observed_run)
    with pytest.raises(ForgeError):
        repository_import.complete(host, account, row["import_id"])
    assert not git_calls, "reject an unregistered source before invoking Git"
    assert (source / "foreign-marker").read_text() == "not created by the importer"
    assert (source.stat().st_dev, source.stat().st_ino) == (before.st_dev, before.st_ino)
    assert host.list_repos() == []


def test_committed_empty_root_is_importable(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "commit", "--allow-empty", "-m", "initial empty commit")
    data = bundle_bytes(source, tmp_path / "repo.bundle")
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    row = staged(host, account, data)
    result = repository_import.complete(host, account, row["import_id"])
    assert result["state"] == "ready"
    assert host.public_repos(account)[0]["name"] == "project"


def test_historical_gitlink_is_rejected_even_after_removal(tmp_path, source_bundle):
    oid = git(source_bundle, "rev-parse", "HEAD").decode().strip()
    git(source_bundle, "update-index", "--add", "--cacheinfo", f"160000,{oid},submodule")
    git(source_bundle, "commit", "-m", "historical submodule")
    git(source_bundle, "update-index", "--force-remove", "submodule")
    git(source_bundle, "commit", "-m", "remove submodule")
    data = bundle_bytes(source_bundle, tmp_path / "repo.bundle")
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    row = staged(host, account, data)
    with pytest.raises(ForgeError):
        repository_import.complete(host, account, row["import_id"])
    assert repository_import.get(host, account, row["import_id"])["state"] == "failed"
    assert host.list_repos() == []


def test_default_staging_budget_allows_multiple_small_completed_imports(tmp_path, source_bundle):
    data = bundle_bytes(source_bundle, tmp_path / "repo.bundle")
    host = Host(tmp_path / "host")
    alice = host.approve_account("alice", ["alice@example.test"])
    bob = host.approve_account("bob", ["bob@example.test"])
    for index, account in enumerate((alice, alice, bob)):
        row = staged(host, account, data, name=f"project-{index}", request_id=f"import-{index}")
        assert repository_import.complete(host, account, row["import_id"])["state"] == "ready"
    assert len(host.public_repos(alice)) == 2
    assert len(host.public_repos(bob)) == 1
    assert len(host.list_repos()) == 3


@pytest.mark.parametrize("content", [b"# v2 git bundle\n\n", b"# v3 git bundle\n@object-format=sha256\n\n",
    b"# v2 git bundle\n" + b"0" * 40 + b" refs/heads/main\n\nPACK" + struct.pack(">II", 2, 1) + b"\x31NOT-ZLIB" + b"0" * 20])
def test_malformed_bundle_preflight_has_stable_policy_errors(tmp_path, content):
    path = tmp_path / "malformed.bundle"
    path.write_bytes(content)
    row = {"size_bytes": len(content), "bundle_sha256": hashlib.sha256(content).hexdigest(), "branch": "main"}
    with pytest.raises(ForgeError) as error:
        repository_import.preflight(path, row)
    assert error.value.category == "policy"


def test_import_conversion_counts_capacity_once(tmp_path, source_bundle, monkeypatch):
    monkeypatch.setenv("BASTION_HOST_REPO_LIMIT", "1")
    data = bundle_bytes(source_bundle, tmp_path / "repo.bundle")
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"], repo_limit=1)
    row = staged(host, account, data)
    with pytest.raises(ForgeError):
        host.create_repo("alice", "other", request_id="other", seed_empty=True)
    completed = repository_import.complete(host, account, row["import_id"])
    assert completed["state"] == "ready"
    assert len(host.list_repos()) == 1


def test_uploaded_import_remains_recoverable_after_session_revocation(tmp_path, source_bundle):
    data = bundle_bytes(source_bundle, tmp_path / "repo.bundle")
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    row = staged(host, account, data)
    token = host.issue_session(account)
    host.revoke_session(token)
    def require_session():
        if host.authenticate_session(token) is None:
            raise ForgeError("session revoked", "authentication")
    with pytest.raises(ForgeError):
        repository_import.complete(host, account, row["import_id"], authorize=require_session)
    assert host.list_repos() == []
    token = host.issue_session(account)
    assert repository_import.complete(host, account, row["import_id"], authorize=require_session)["state"] == "ready"


def test_revocation_during_validation_refuses_publication_and_allows_renewal(tmp_path, source_bundle, monkeypatch):
    data = bundle_bytes(source_bundle, tmp_path / "repo.bundle")
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    row = staged(host, account, data)
    token = host.issue_session(account)
    validate = repository_import._validate
    def revoke_after_validation(*args):
        result = validate(*args)
        host.revoke_session(token)
        return result
    monkeypatch.setattr(repository_import, "_validate", revoke_after_validation)
    def require_session():
        if host.authenticate_session(token) is None:
            raise ForgeError("session revoked", "authentication")
    with pytest.raises(ForgeError) as error:
        repository_import.complete(host, account, row["import_id"], authorize=require_session)
    assert error.value.category == "authentication"
    assert host.public_repos(account) == []
    monkeypatch.setattr(repository_import, "_validate", validate)
    token = host.issue_session(account)
    assert repository_import.complete(host, account, row["import_id"], authorize=require_session)["state"] == "ready"


@pytest.mark.parametrize("name", ["IMPORT_MAX_COMMITS", "IMPORT_MAX_OBJECTS", "IMPORT_MAX_TREE_ENTRIES"])
def test_expanded_history_limits_prevent_repository_publication(tmp_path, source_bundle, monkeypatch, name):
    import limits
    (source_bundle / "second.txt").write_text("another object\n")
    git(source_bundle, "add", ".")
    git(source_bundle, "commit", "-m", "second commit")
    data = bundle_bytes(source_bundle, tmp_path / "repo.bundle")
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    row = staged(host, account, data)
    monkeypatch.setattr(limits, name, 1)
    with pytest.raises(ForgeError) as error:
        repository_import.complete(host, account, row["import_id"])
    assert error.value.category == "limits"
    assert host.list_repos() == []


def test_import_admission_is_shared_across_accounts_tokens_and_addresses(tmp_path, source_bundle, monkeypatch):
    import limits
    import first_push
    from http_server import pkt_line
    from host_server import create_app
    monkeypatch.setattr(limits, "IMPORT_STAGING_BYTES", 2 << 30)
    monkeypatch.setattr(limits, "MAX_IMPORTS_PER_IP", 1)
    tip = git(source_bundle, "rev-parse", "HEAD").decode().strip()
    objects = git(source_bundle, "rev-list", "--objects", "--no-object-names", "HEAD")
    pack = subprocess.run(["git", "-C", str(source_bundle), "pack-objects", "--stdout"], input=objects, check=True, stdout=subprocess.PIPE).stdout
    data = pkt_line(("0" * 40 + " " + tip + " refs/heads/main\0report-status\n").encode()) + b"0000" + pack
    host = Host(tmp_path / "host")
    alice = host.approve_account("alice", ["alice@example.test"])
    bob = host.approve_account("bob", ["bob@example.test"])
    arow = first_push.reserve(host, alice, {"name": "project", "branch": "main", "request_id": "alice-import"})
    brow = first_push.reserve(host, bob, {"name": "project", "branch": "main", "request_id": "bob-import"})
    app = create_app(host.root)
    at = first_push.issue_credential(host, alice, arow["import_id"])["credential"]
    bt = first_push.issue_credential(host, bob, brow["import_id"])["credential"]
    entered, release = threading.Event(), threading.Event()
    statuses = []
    receive = first_push.receive
    def blocking_receive(*args, **kwargs):
        if args[1]["account_id"] == alice:
            entered.set()
            assert release.wait(10)
        return receive(*args, **kwargs)
    monkeypatch.setattr(first_push, "receive", blocking_receive)
    def request(account_token, job, address="127.0.0.1"):
        with app.test_client() as client:
            owner = host._account_owner(job["account_id"])
            return client.post(f"/{owner}/project.git/git-receive-pack", data=data,
                              content_type="application/x-git-receive-pack-request", headers={"Authorization": "Bearer " + account_token},
                              environ_overrides={"REMOTE_ADDR": address}).status_code
    thread = threading.Thread(target=lambda: statuses.append(request(at, arow)))
    thread.start()
    try:
        assert entered.wait(10)
        # Another source address does not bypass the same account's budget.
        assert request(at, arow, "127.0.0.2") == 429
        # Another account/token does not bypass the source-address budget.
        assert request(bt, brow) == 429
    finally:
        release.set()
        thread.join(15)
    assert statuses == [200]
    assert request(bt, brow) == 200


@pytest.mark.parametrize("published", [False, True])
def test_source_publication_interruption_recovers_only_the_pinned_source(tmp_path, source_bundle, monkeypatch, published):
    data = bundle_bytes(source_bundle, tmp_path / "repo.bundle")
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    row = staged(host, account, data)
    rename = os.rename
    def interrupt(source, target, *args, **kwargs):
        if str(target).endswith("/source.git"):
            if published:
                rename(source, target, *args, **kwargs)
            raise SystemExit("simulated process exit at source publication")
        return rename(source, target, *args, **kwargs)
    monkeypatch.setattr(os, "rename", interrupt)
    with pytest.raises(SystemExit):
        repository_import.complete(host, account, row["import_id"])
    current = repository_import.get(host, account, row["import_id"])
    assert current["source_identity"]
    assert current["source_ino"]
    monkeypatch.setattr(os, "rename", rename)
    restarted = Host(host.root, initialize=False, migrate=True)
    completed = repository_import.complete(restarted, account, row["import_id"])
    assert completed["state"] == "ready"
    assert completed["source_identity"] == current["source_identity"]
    assert len(restarted.list_repos()) == 1


def test_pending_import_recovery_does_not_restore_disabled_membership(tmp_path, source_bundle):
    data = bundle_bytes(source_bundle, tmp_path / "repo.bundle")
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    row = staged(host, account, data)
    def interrupt(stage):
        if stage == "repo_ready_before_import_ready":
            raise SystemExit("lost import-ready publication")
    with pytest.raises(SystemExit):
        repository_import.complete(host, account, row["import_id"], failpoint=interrupt)
    current = repository_import.get(host, account, row["import_id"])
    assert current["state"] == "provisioning"
    with host._connect() as con:
        con.execute("UPDATE memberships SET state='disabled' WHERE repo_id=? AND account_id=?", (current["repo_id"], account))
    restarted = Host(host.root, initialize=False, migrate=True)
    repository_import.complete(restarted, account, row["import_id"])
    with restarted._connect(read_only=True) as con:
        assert con.execute("SELECT state FROM memberships WHERE repo_id=? AND account_id=?", (current["repo_id"], account)).fetchone()[0] == "disabled"
    assert restarted.public_repos(account) == []


def test_import_apis_never_accept_repository_ui_or_git_tokens(tmp_path, source_bundle):
    from host_server import create_app
    from http_server import add_token
    data = bundle_bytes(source_bundle, tmp_path / "repo.bundle")
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    row = staged(host, account, data)
    ready = repository_import.complete(host, account, row["import_id"])
    root = host.root / "repos" / ready["repo_id"]
    credentials = [add_token(root, "owner", scopes=scopes, expires_in="1h") for scopes in (["ui"], ["fetch", "push"])]
    client = create_app(host.root).test_client()
    for token in credentials:
        auth = {"Authorization": "Bearer " + token}
        assert client.post("/api/v1/imports", json={}, headers=auth).status_code == 401
        assert client.get(f"/api/v1/imports/{row['import_id']}", headers=auth).status_code == 401
        assert client.post(f"/api/v1/imports/{row['import_id']}/credentials", json={}, headers=auth).status_code == 401
        assert client.get("/api/v1/imports", headers=auth).status_code == 401
        assert client.put(f"/api/v1/imports/{row['import_id']}/bundle", data=data,
                          content_type="application/octet-stream", headers=auth).status_code == 404
        assert client.post(f"/api/v1/imports/{row['import_id']}/complete", json={}, headers=auth).status_code == 404


def test_import_parent_substitution_is_rejected_after_restart(tmp_path, source_bundle):
    data = bundle_bytes(source_bundle, tmp_path / "repo.bundle")
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    row = staged(host, account, data)
    parent = host.root / "imports"
    previous = tmp_path / "original-imports"
    parent.rename(previous)
    parent.mkdir(mode=0o700)
    # Preserve the job directory inode while replacing its parent.
    (previous / row["import_id"]).rename(parent / row["import_id"])
    with pytest.raises(ForgeError):
        Host(host.root, initialize=False, migrate=True)


def test_expansion_bomb_is_rejected_before_any_git_process(tmp_path, monkeypatch):
    import limits
    monkeypatch.setattr(limits, "IMPORT_OBJECT_BYTES", 1024)
    pack = b"PACK" + struct.pack(">II", 2, 1) + b"\x31" + zlib.compress(b"x" * 1025)
    data = b"# v2 git bundle\n" + b"0" * 40 + b" refs/heads/main\n\n" + pack + hashlib.sha1(pack).digest()
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    row = staged(host, account, data)
    def forbidden_run(*args, **kwargs):
        pytest.fail("Git must not see a pack which exceeds preflight expansion limits")
    monkeypatch.setattr(repository_import, "_run", forbidden_run)
    with pytest.raises(ForgeError) as error:
        repository_import.complete(host, account, row["import_id"])
    assert error.value.category == "limits"
    assert host.list_repos() == []


def test_published_upload_does_not_accept_different_same_length_retry(tmp_path, source_bundle):
    data = bundle_bytes(source_bundle, tmp_path / "repo.bundle")
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    row = staged(host, account, data)
    wrong = b"X" * len(data)
    with pytest.raises(ForgeError) as error:
        repository_import.upload(host, account, row["import_id"], io.BytesIO(wrong), len(wrong), 32 << 20)
    assert error.value.category == "conflict"
    assert (host.root / "imports" / row["import_id"] / "bundle").read_bytes() == data
    assert repository_import.get(host, account, row["import_id"])["state"] == "uploaded"


@pytest.mark.parametrize("flood_output", [False, True])
def test_validation_subprocess_budget_kills_and_reaps_child(tmp_path, flood_output):
    pid_file = tmp_path / "validator.pid"
    code = ("import os,time; from pathlib import Path; "
            "Path(os.environ['PID_FILE']).write_text(str(os.getpid())); "
            + ("os.write(1, b'x' * 65536); time.sleep(30)" if flood_output else "time.sleep(30)"))
    env = dict(os.environ, PID_FILE=str(pid_file))
    with pytest.raises(ForgeError) as error:
        repository_import._run([sys.executable, "-c", code], env=env, timeout=1, output_limit=1024)
    assert error.value.category == "limits"
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_unreachable_pack_objects_do_not_enter_an_imported_repository(tmp_path, source_bundle):
    tip = git(source_bundle, "rev-parse", "HEAD").strip()
    loose = subprocess.run(["git", "-C", str(source_bundle), "hash-object", "-w", "--stdin"],
                           input=b"unreachable secret", check=True, stdout=subprocess.PIPE).stdout.strip()
    reachable = git(source_bundle, "rev-list", "--objects", "--no-object-names", "HEAD")
    pack = subprocess.run(["git", "-C", str(source_bundle), "pack-objects", "--stdout"],
                          input=reachable + loose + b"\n", check=True, stdout=subprocess.PIPE).stdout
    data = b"# v2 git bundle\n" + tip + b" refs/heads/main\n\n" + pack
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    row = staged(host, account, data)
    with pytest.raises(ForgeError):
        repository_import.complete(host, account, row["import_id"])
    assert host.list_repos() == []
