"""Real smart-HTTP first-push import and bootstrap-credential boundaries."""
import base64
import hashlib
import io
import os
import struct
import subprocess
import threading
import zlib

import pytest
from werkzeug.serving import make_server

import first_push
import repository_import
from errors import ForgeError
from host import Host
from host_server import create_app
from http_server import pkt_line
from layout import ZERO_OID


def git(path, *args, email="historical@example.test", check=True):
    env = dict(os.environ, GIT_AUTHOR_NAME="Author", GIT_COMMITTER_NAME="Author",
               GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_EMAIL=email, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git", "-C", str(path), *args], env=env, check=check,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)


@pytest.fixture
def setup(tmp_path):
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"], repo_limit=4)
    app = create_app(host.root)
    client = app.test_client()
    auth = {"Authorization": "Bearer " + host.issue_session(account)}
    payload = {"name": "project", "branch": "main", "request_id": "first-push"}
    response = client.post("/api/v1/imports", json=payload, headers=auth)
    assert response.status_code == 201, response.json
    job = response.json["import"]
    assert job["state"] == "awaiting-push" and job["repo"] is None
    response = client.post(f"/api/v1/imports/{job['import_id']}/credentials", json={}, headers=auth)
    assert response.status_code == 201, response.json
    credential = response.json["credential"]
    git_auth = {"Authorization": "Basic " + base64.b64encode(("owner:" + credential).encode()).decode()}
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    (source / "README.md").write_text("existing history\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "old author one", email="old-one@example.test")
    (source / "README.md").write_text("more existing history\n")
    git(source, "commit", "-am", "old author two", email="old-two@example.test")
    return host, account, app, client, auth, job, credential, git_auth, source


def receive_body(source, branch="main", old=ZERO_OID, tip=None):
    tip = tip or git(source, "rev-parse", "HEAD").stdout.decode().strip()
    objects = git(source, "rev-list", "--objects", "--no-object-names", "HEAD").stdout
    pack = subprocess.run(["git", "-C", str(source), "pack-objects", "--stdout"], input=objects,
                          check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30).stdout
    command = f"{old} {tip} refs/heads/{branch}".encode() + b"\0report-status ofs-delta object-format=sha1\n"
    return pkt_line(command) + b"0000" + pack


def test_real_git_push_initializes_and_normal_clone_push_remains_fenced(setup, tmp_path):
    host, account, app, client, auth, job, credential, git_auth, source = setup
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/alice/project.git"
        header = git_auth["Authorization"]
        git(source, "remote", "add", "bastion", url)
        pushed = git(source, "-c", "http.extraHeader=Authorization: " + header, "push", "bastion", "main")
        assert pushed.returncode == 0
        status = client.get(f"/api/v1/imports/{job['import_id']}", headers=auth)
        assert status.json["import"]["state"] == "ready"
        repo = status.json["import"]["repo"]
        assert len(host.public_repos(account)) == 1
        from repository import Repository
        repository = Repository.open(host.root / "repos" / repo["repo_id"])
        tip = git(source, "rev-parse", "HEAD").stdout.decode().strip()
        assert repository.load()[2].records[0]["payload"]["canonical_tip"] == tip
        # Git repeats a lost first-push response without creating another repo.
        git(source, "-c", "http.extraHeader=Authorization: " + header, "push", "bastion", "main")
        assert len(host.list_repos()) == 1
        from http_server import add_token
        normal = add_token(repository.root, "owner", scopes=["fetch", "push"], expires_in="1h")
        normal_header = "Basic " + base64.b64encode(("owner:" + normal).encode()).decode()
        clone = tmp_path / "clone"
        git(tmp_path, "-c", "http.extraHeader=Authorization: " + normal_header, "clone", url, str(clone))
        (clone / "new.txt").write_text("new owner commit\n")
        git(clone, "add", ".")
        git(clone, "commit", "-m", "normal push", email="alice@example.test")
        git(clone, "-c", "http.extraHeader=Authorization: " + normal_header, "push", "origin", "main", email="alice@example.test")
        advanced = repository.load(use_cache=False)[2].canonical_tip
        assert advanced != tip
        # Bootstrap credentials cannot append history, fetch, or use UI APIs.
        (source / "unauthorized.txt").write_text("not an import retry\n")
        git(source, "add", ".")
        git(source, "commit", "-m", "bootstrap append", email="alice@example.test")
        denied = git(source, "-c", "http.extraHeader=Authorization: " + header, "push", "bastion", "main", check=False)
        assert denied.returncode != 0
        assert repository.load(use_cache=False)[2].canonical_tip == advanced
        assert client.get("/alice/project.git/info/refs?service=git-upload-pack", headers=git_auth).status_code == 404
        assert client.get("/api/v1/session", headers={"Authorization": "Bearer " + credential}).status_code == 401
        (clone / "wrong.txt").write_text("unapproved author\n")
        git(clone, "add", ".")
        git(clone, "commit", "-m", "wrong author", email="wrong@example.test")
        assert git(clone, "-c", "http.extraHeader=Authorization: " + normal_header, "push", "origin", "main", check=False).returncode != 0
        assert repository.load(use_cache=False)[2].canonical_tip == advanced
    finally:
        server.shutdown()
        thread.join(5)


def test_reservation_credentials_rotation_capacity_and_account_secrecy(setup):
    host, account, app, client, auth, job, credential, git_auth, source = setup
    payload = {"name": "project", "branch": "main", "request_id": "first-push"}
    assert client.post("/api/v1/imports", json=payload, headers=auth).json["import"]["import_id"] == job["import_id"]
    assert client.post("/api/v1/imports", json={**payload, "branch": "other"}, headers=auth).status_code == 409
    assert client.post("/api/v1/repos", json={"name": "project", "request_id": "seed"}, headers=auth).status_code == 409
    assert client.get("/api/v1/imports", headers=auth).json["imports"][0]["import_id"] == job["import_id"]
    bob = host.approve_account("bob", ["bob@example.test"])
    other = {"Authorization": "Bearer " + host.issue_session(bob)}
    assert client.get(f"/api/v1/imports/{job['import_id']}", headers=other).status_code == 404
    assert client.post(f"/api/v1/imports/{job['import_id']}/credentials", json={}, headers=other).status_code == 404
    assert client.get("/api/v1/imports", headers=other).json["imports"] == []
    assert client.get("/bob/project.git/info/refs?service=git-receive-pack", headers=git_auth).status_code == 401
    assert client.get("/alice/other.git/info/refs?service=git-receive-pack", headers=git_auth).status_code == 401
    replacement = client.post(f"/api/v1/imports/{job['import_id']}/credentials", json={}, headers=auth).json["credential"]
    assert replacement != credential
    assert first_push.authenticate(host, credential, "alice", "project") is None
    assert first_push.authenticate(host, replacement, "alice", "project")
    with host._connect(read_only=True) as con:
        rows = con.execute("SELECT * FROM import_tokens").fetchall()
        assert len(rows) == 1
        assert credential not in repr([tuple(row) for row in rows])
        assert replacement not in repr([tuple(row) for row in rows])


@pytest.mark.parametrize("kind", ["wrong-branch", "delete", "stale", "multi", "missing-pack", "bad-checksum"])
def test_invalid_initial_receive_never_publishes_repository(setup, kind):
    host, account, app, client, auth, job, credential, git_auth, source = setup
    body = receive_body(source, branch="other" if kind == "wrong-branch" else "main",
                        old="1" * 40 if kind == "stale" else ZERO_OID,
                        tip=ZERO_OID if kind == "delete" else None)
    if kind == "multi":
        pos = int(body[:4], 16)
        body = body[:pos] + pkt_line((ZERO_OID + " " + "1" * 40 + " refs/heads/other\n").encode()) + body[pos:]
    elif kind == "missing-pack":
        body = body[:int(body[:4], 16) + 4]
    elif kind == "bad-checksum":
        body = body[:-1] + bytes([body[-1] ^ 1])
    response = client.post("/alice/project.git/git-receive-pack", data=body,
                           content_type="application/x-git-receive-pack-request", headers=git_auth)
    assert response.status_code == 200
    assert b"ng refs/heads/main" in response.data
    assert host.public_repos(account) == [] and host.list_repos() == []
    assert b"historical@" not in response.data and str(host.root).encode() not in response.data


def test_initial_pack_expansion_is_bounded_before_git(setup, monkeypatch):
    host, account, app, client, auth, job, credential, git_auth, source = setup
    import limits
    monkeypatch.setattr(limits, "IMPORT_OBJECT_BYTES", 1024)
    pack = b"PACK" + struct.pack(">II", 2, 1) + b"\x31" + zlib.compress(b"x" * 1025)
    pack += hashlib.sha1(pack).digest()
    body = pkt_line((ZERO_OID + " " + "1" * 40 + " refs/heads/main\0report-status\n").encode()) + b"0000" + pack
    monkeypatch.setattr(repository_import, "_run", lambda *a, **k: pytest.fail("unbounded pack reached Git"))
    response = client.post("/alice/project.git/git-receive-pack", data=body,
                           content_type="application/x-git-receive-pack-request", headers=git_auth)
    assert b"ng refs/heads/main" in response.data
    assert repository_import.get(host, account, job["import_id"])["push_tip"] is None


def test_failed_historical_mode_can_be_corrected_before_initialization(setup):
    host, account, app, client, auth, job, credential, git_auth, source = setup
    good = git(source, "rev-parse", "HEAD").stdout.decode().strip()
    git(source, "update-index", "--add", "--cacheinfo", f"160000,{good},submodule")
    git(source, "commit", "-m", "submodule")
    body = receive_body(source)
    row = repository_import.get(host, account, job["import_id"])
    with pytest.raises(ForgeError):
        first_push.receive(host, row, body, lambda: None)
    assert host.list_repos() == []
    # Correct source history, not merely a tip tree which hides older gitlinks.
    git(source, "reset", "--soft", good)
    completed = first_push.receive(host, repository_import.get(host, account, job["import_id"]),
                                   receive_body(source), lambda: None)
    assert completed["state"] == "ready"


def test_interrupted_first_push_restarts_without_resetting_ready_repository(setup):
    host, account, app, client, auth, job, credential, git_auth, source = setup
    body = receive_body(source)
    def stop(stage):
        if stage == "repo_ready_before_import_ready":
            raise SystemExit("process lost response")
    with pytest.raises(SystemExit):
        first_push.receive(host, repository_import.get(host, account, job["import_id"]), body, lambda: None, failpoint=stop)
    restarted = Host(host.root, initialize=False, migrate=True)
    completed = first_push.receive(restarted, repository_import.get(restarted, account, job["import_id"]), body, lambda: None)
    assert completed["state"] == "ready"
    assert len(restarted.list_repos()) == 1


def test_initial_credentials_never_accept_host_tokens_or_wrong_basic_username(setup):
    host, account, app, client, auth, job, credential, git_auth, source = setup
    url = "/alice/project.git/info/refs?service=git-receive-pack"
    assert client.get(url, headers=auth).status_code == 401
    assert client.get(url).status_code == 401
    wrong_user = {"Authorization": "Basic " + base64.b64encode(("alice:" + credential).encode()).decode()}
    assert client.get(url, headers=wrong_user).status_code == 401
    assert client.get(url, headers=git_auth).status_code == 200
    assert client.put(f"/api/v1/imports/{job['import_id']}/bundle", data=b"anything", headers=auth).status_code == 404
    assert client.post(f"/api/v1/imports/{job['import_id']}/complete", json={}, headers=auth).status_code == 404
    assert client.post("/api/v1/imports", json={"name": "bundle", "branch": "main", "request_id": "bundle", "bundle_sha256": "0" * 64, "size_bytes": 100}, headers=auth).status_code == 400


@pytest.mark.parametrize("suspend", [False, True])
def test_expiry_or_suspension_during_validation_prevents_publication(setup, monkeypatch, suspend):
    host, account, app, client, auth, job, credential, git_auth, source = setup
    validate = repository_import._validate
    def invalidate(*args):
        result = validate(*args)
        with host._connect() as con:
            if suspend:
                con.execute("UPDATE accounts SET state='suspended' WHERE account_id=?", (account,))
            else:
                con.execute("UPDATE import_tokens SET expires_at=0 WHERE import_id=?", (job["import_id"],))
        return result
    monkeypatch.setattr(repository_import, "_validate", invalidate)
    path = "/alice/project.git/git-receive-pack"
    body = receive_body(source)
    response = client.post(path, data=body, content_type="application/x-git-receive-pack-request", headers=git_auth)
    assert response.status_code == 401
    assert host.list_repos() == []
    monkeypatch.setattr(repository_import, "_validate", validate)
    with host._connect() as con:
        con.execute("UPDATE accounts SET state='approved' WHERE account_id=?", (account,))
    replacement = client.post(f"/api/v1/imports/{job['import_id']}/credentials", json={}, headers=auth)
    assert replacement.status_code == 201
    new_auth = {"Authorization": "Bearer " + replacement.json["credential"]}
    response = client.post(path, data=body, content_type="application/x-git-receive-pack-request", headers=new_auth)
    assert b"ok refs/heads/main" in response.data


def test_pending_repositories_do_not_monopolize_disk_budget_and_count_once(setup):
    host, account, app, client, auth, job, credential, git_auth, source = setup
    for i in range(2):
        response = client.post("/api/v1/imports", json={"name": f"pending-{i}", "branch": "main", "request_id": f"pending-{i}"}, headers=auth)
        assert response.status_code == 201
    with host._connect(read_only=True) as con:
        assert con.execute("SELECT sum(reserved_stage_bytes) FROM imports").fetchone()[0] == 0
    response = client.post("/alice/project.git/git-receive-pack", data=receive_body(source), content_type="application/x-git-receive-pack-request", headers=git_auth)
    assert b"ok refs/heads/main" in response.data
    assert client.post("/api/v1/repos", json={"name": "fourth", "request_id": "fourth"}, headers=auth).status_code == 201
    assert client.post("/api/v1/imports", json={"name": "fifth", "branch": "main", "request_id": "fifth"}, headers=auth).status_code == 413
    assert len(host.list_repos()) == 2


def test_failed_missing_objects_can_retry_a_complete_pack_for_same_tip(setup):
    host, account, app, client, auth, job, credential, git_auth, source = setup
    body = receive_body(source)
    tip = git(source, "rev-parse", "HEAD").stdout.decode().strip()
    # A checksum-valid pack with only the target commit lacks its trees/parents.
    partial = subprocess.run(["git", "-C", str(source), "pack-objects", "--stdout"], input=tip.encode()+b"\n",
                             check=True, stdout=subprocess.PIPE).stdout
    header = body[:int(body[:4],16)+4]
    path = "/alice/project.git/git-receive-pack"
    failed = client.post(path, data=header+partial, content_type="application/x-git-receive-pack-request", headers=git_auth)
    assert b"ng refs/heads/main" in failed.data
    assert repository_import.get(host, account, job["import_id"])["state"] == "failed"
    completed = client.post(path, data=body, content_type="application/x-git-receive-pack-request", headers=git_auth)
    assert b"ok refs/heads/main" in completed.data


def test_receive_intake_pinning_interruption_accepts_recompressed_same_tip(setup):
    host, account, app, client, auth, job, credential, git_auth, source = setup
    body = receive_body(source)
    tip, _pack = first_push.parse_receive(body, "main")
    # The process recorded an earlier pack identity but died before publishing
    # any bytes. A retry's pack order/compression need not be identical.
    with host._connect() as con:
        con.execute("UPDATE imports SET push_tip=?,bundle_sha256=?,size_bytes=? WHERE import_id=?", (tip,"f"*64,123,job["import_id"]))
    reopened = Host(host.root, initialize=False, migrate=True)
    completed = first_push.receive(reopened, repository_import.get(reopened, account, job["import_id"]), body, lambda: None)
    assert completed["state"] == "ready"


def test_bootstrap_recovery_never_restores_disabled_membership(setup):
    host, account, app, client, auth, job, credential, git_auth, source = setup
    body = receive_body(source)
    first_push.receive(host, repository_import.get(host, account, job["import_id"]), body, lambda: None)
    current = repository_import.get(host, account, job["import_id"])
    with host._connect() as con:
        con.execute("UPDATE imports SET state='failed' WHERE import_id=?", (job["import_id"],))
        con.execute("UPDATE memberships SET state='disabled' WHERE repo_id=? AND account_id=?", (current["repo_id"],account))
    response = client.post("/alice/project.git/git-receive-pack", data=body, content_type="application/x-git-receive-pack-request", headers=git_auth)
    assert response.status_code == 401
    with host._connect(read_only=True) as con:
        assert con.execute("SELECT state FROM memberships WHERE repo_id=? AND account_id=?", (current["repo_id"],account)).fetchone()[0] == "disabled"


def test_v4_staged_data_migrates_without_adoption_or_conversion(setup):
    host, account, app, client, auth, job, credential, git_auth, source = setup
    body = receive_body(source)
    tip, pack = first_push.parse_receive(body,"main")
    envelope = b"# v2 git bundle\n"+tip.encode()+b" refs/heads/main\n\n"+pack
    legacy = repository_import.reserve(host, account, {"name":"legacy", "branch":"main", "request_id":"legacy", "bundle_sha256":hashlib.sha256(envelope).hexdigest(), "size_bytes":len(envelope)}, len(envelope))
    legacy = repository_import.upload(host, account, legacy["import_id"], io.BytesIO(envelope), len(envelope), len(envelope))
    # Reconstruct the prior v4 table layout instead of only lowering a version
    # number on a v5 table. The staged source and its receipts must survive.
    with host._connect() as con:
        con.execute("DROP TABLE import_tokens")
        extension = ", mode TEXT NOT NULL DEFAULT 'bundle' CHECK(mode IN ('bundle','push')), push_tip TEXT"
        ddl = host._imports_ddl().replace("IF NOT EXISTS imports(", "imports_v4(").replace(extension, "")
        con.execute(ddl)
        columns = [r[1] for r in con.execute("PRAGMA table_info(imports_v4)")]
        names = ",".join(columns)
        con.execute(f"INSERT INTO imports_v4({names}) SELECT {names} FROM imports")
        con.execute("DROP TABLE imports")
        con.execute("ALTER TABLE imports_v4 RENAME TO imports")
        con.execute("UPDATE schema_info SET version=4")
    restarted = Host(host.root, initialize=False, migrate=True)
    current = repository_import.get(restarted, account, legacy["import_id"])
    assert current["mode"] == "bundle"
    assert current["bundle_sha256"] == legacy["bundle_sha256"]
    assert current["stage_ino"] == legacy["stage_ino"]
    assert (host.root/"imports"/legacy["import_id"]/"bundle").read_bytes() == envelope
    assert repository_import.complete(restarted, account, legacy["import_id"])["state"] == "ready"


def test_host_logout_during_credential_issue_does_not_mint_a_new_secret(setup, monkeypatch):
    host, account, app, client, auth, job, credential, git_auth, source = setup
    issue = first_push.issue_credential
    def logout_before_issue(*args, **kwargs):
        host.revoke_session(auth["Authorization"].split(" ",1)[1])
        return issue(*args, **kwargs)
    monkeypatch.setattr(first_push,"issue_credential",logout_before_issue)
    response = client.post(f"/api/v1/imports/{job['import_id']}/credentials", json={}, headers=auth)
    assert response.status_code == 401
    # Existing Git credentials have their own lifetime; logout cannot replace
    # them with a newly issued secret after the host session was revoked.
    assert first_push.authenticate(host,credential,"alice","project")
