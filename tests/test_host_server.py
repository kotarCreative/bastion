from host import Host
from host_server import create_app
import subprocess


def test_host_sessions_are_distinct_digest_only_and_revocable(tmp_path):
    host = Host(tmp_path / "host")
    account_id = host.approve_account("alice", ["alice@example.test"])
    token = host.issue_session(account_id)
    assert token.startswith("bsh1.")
    assert host.authenticate_session(token)["account_id"] == account_id
    with host._connect(read_only=True) as con:
        assert token.rsplit(".", 1)[1] not in con.execute("SELECT digest FROM host_sessions").fetchone()[0]
    app = create_app(tmp_path / "host")
    client = app.test_client()
    assert client.get("/api/v1/auth/config").json["mode"] == "host"
    assert client.get("/api/v1/session", headers={"Authorization": "Bearer " + token}).json["mode"] == "host"
    assert client.post("/api/v1/logout", headers={"Authorization": "Bearer " + token}).status_code == 200
    assert host.authenticate_session(token) is None


def test_host_routes_require_host_credential(tmp_path):
    host = Host(tmp_path / "host")
    account_id = host.approve_account("alice", ["alice@example.test"])
    token = host.issue_session(account_id)
    client = create_app(tmp_path / "host").test_client()
    assert client.get("/api/v1/repos").status_code == 401
    assert client.get("/api/v1/repos", headers={"Authorization": "Bearer " + token}).json == {"repos": []}
    assert client.post("/api/v1/repos", headers={"Authorization": "Bearer " + token}, json={"name": "first", "request_id": "first"}).status_code == 201
    assert len(client.get("/api/v1/repos", headers={"Authorization": "Bearer " + token}).json["repos"]) == 1


def test_approved_zero_repository_clerk_login_uses_fake_authenticator(tmp_path, monkeypatch):
    import clerk_auth
    from clerk_auth import ClerkConfig
    config = ClerkConfig("pk_test", "sk_test", ("https://app.example.test",), "https://clerk.example.test")
    class FakeClerk:
        def __init__(self, _config): pass
        def authenticate(self, _headers): return "user_new", ["new@example.test"]
    monkeypatch.setattr(clerk_auth, "load_clerk_config", lambda: config)
    monkeypatch.setattr(clerk_auth, "ClerkAuthenticator", FakeClerk)
    host = Host(tmp_path / "host")
    host.approve_account("new", ["new@example.test"])
    client = create_app(host.root).test_client()
    response = client.post("/api/v1/auth/clerk", json={})
    assert response.status_code == 200
    assert response.json["mode"] == "host"
    assert response.json["access_token"].startswith("bsh1.")
    assert client.get("/api/v1/repos", headers={"Authorization": "Bearer " + response.json["access_token"]}).json == {"repos": []}
    assert client.post("/api/v1/auth/clerk", data="{", content_type="application/json").status_code == 400


def test_exchange_revokes_session_when_downstream_envelope_fails(tmp_path, monkeypatch):
    from flask import Flask, jsonify
    import repository_server
    from http_server import list_tokens
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    repo = host.create_repo("alice", "repo", request_id="repo", seed_empty=True)
    failed = Flask("failed-child")
    @failed.get("/api/v1/session")
    def broken_session(): return jsonify(error="service-unavailable"), 503
    monkeypatch.setattr(repository_server, "create_app", lambda _root: failed)
    client = create_app(host.root).test_client()
    response = client.post(f"/api/v1/repos/{repo['repo_id']}/auth/session", json={}, headers={"Authorization": "Bearer " + host.issue_session(account)})
    assert response.status_code == 503
    tokens = list_tokens(host.root / "repos" / repo["repo_id"])
    assert len(tokens) == 1 and tokens[0]["revoked_at"] is not None


def test_cached_repository_root_replacement_fails_closed(tmp_path):
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    repo = host.create_repo("alice", "repo", request_id="repo", seed_empty=True)
    client = create_app(host.root).test_client()
    host_token = host.issue_session(account)
    ui = client.post(f"/api/v1/repos/{repo['repo_id']}/auth/session", json={}, headers={"Authorization": "Bearer " + host_token}).json["access_token"]
    original = host.root / "repos" / repo["repo_id"]
    moved = tmp_path / "moved-repo"
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=True)
    response = client.get(f"/api/v1/repos/{repo['repo_id']}/tree", headers={"Authorization": "Bearer " + ui})
    assert response.status_code == 503


def test_accounts_are_isolated_and_repository_tokens_are_not_host_tokens(tmp_path):
    host = Host(tmp_path / "host")
    alice = host.approve_account("alice", ["alice@example.test"])
    bob = host.approve_account("bob", ["bob@example.test"])
    host.link_clerk_account("user_alice", ["alice@example.test"])
    host.link_clerk_account("user_bob", ["bob@example.test"])
    first = host.create_repo("alice", "one", request_id="one", seed_empty=True)
    second = host.create_repo("alice", "two", request_id="two", seed_empty=True)
    bob_first = host.create_repo("bob", "one", request_id="bob-one", seed_empty=True)
    bob_second = host.create_repo("bob", "two", request_id="bob-two", seed_empty=True)
    app = create_app(host.root)
    client = app.test_client()
    at = host.issue_session(alice)
    bt = host.issue_session(bob)
    alice_repos = client.get("/api/v1/repos", headers={"Authorization": "Bearer " + at}).json["repos"]
    assert {r["name"] for r in alice_repos} == {"one", "two"}
    assert set(alice_repos[0]) == {"repo_id", "owner_name", "name", "branch", "git_url", "views"}
    assert {r["name"] for r in client.get("/api/v1/repos", headers={"Authorization": "Bearer " + bt}).json["repos"]} == {"one", "two"}
    ui = client.post(f"/api/v1/repos/{first['repo_id']}/auth/session", json={}, headers={"Authorization": "Bearer " + at}).json["access_token"]
    assert client.get(f"/api/v1/repos/{first['repo_id']}/tree", headers={"Authorization": "Bearer " + ui}).status_code == 200
    assert client.get(f"/api/v1/repos/{first['repo_id']}/tree", headers={"Authorization": "Bearer " + at}).status_code == 401
    assert client.get("/api/v1/session", headers={"Authorization": "Bearer " + ui}).status_code == 401
    assert client.get(f"/api/v1/repos/{second['repo_id']}/tree", headers={"Authorization": "Bearer " + ui}).status_code == 401
    assert client.post(f"/api/v1/repos/{first['repo_id']}/auth/session", json={}, headers={"Authorization": "Bearer " + bt}).status_code == 404
    bob_ui = client.post(f"/api/v1/repos/{bob_first['repo_id']}/auth/session", json={}, headers={"Authorization": "Bearer " + bt}).json["access_token"]
    assert client.get(f"/api/v1/repos/{bob_second['repo_id']}/tree", headers={"Authorization": "Bearer " + bob_ui}).status_code == 401


def test_create_does_not_accept_owner_or_source_overrides_and_bad_json_is_400(tmp_path):
    host = Host(tmp_path / "host")
    aid = host.approve_account("alice", ["alice@example.test"])
    client = create_app(host.root).test_client()
    auth = {"Authorization": "Bearer " + host.issue_session(aid)}
    assert client.post("/api/v1/repos", headers=auth, json={"name": "repo", "request_id": "one", "owner": "bob"}).status_code == 400
    assert client.post("/api/v1/auth/clerk", data="null", content_type="application/json").status_code == 400
    assert client.get("/api/v1/auth/config").headers["Content-Security-Policy"].find("script-src 'self'") >= 0


def test_unknown_host_root_is_not_bootstrapped(tmp_path):
    import pytest
    from errors import ForgeError
    root = tmp_path / "absent"
    with pytest.raises(ForgeError):
        create_app(root)
    assert not root.exists()


def test_host_v1_migration_and_corrupt_catalog_fail_closed(tmp_path):
    import sqlite3
    import pytest
    from errors import ForgeError
    root = tmp_path / "host"
    Host(root)
    con = sqlite3.connect(root / "host.sqlite3")
    con.execute("DROP TABLE membership_intents")
    con.execute("DROP TABLE host_sessions")
    con.execute("DROP TABLE host_settings")
    con.execute("DROP TABLE import_tokens")
    con.execute("UPDATE schema_info SET version=1")
    con.commit(); con.close()
    migrated = Host(root)
    with migrated._connect(read_only=True) as con:
        assert con.execute("SELECT version FROM schema_info").fetchone()[0] == 5
        assert con.execute("SELECT count(*) FROM membership_intents").fetchone()[0] == 0
    bad = tmp_path / "bad"
    bad.mkdir()
    con = sqlite3.connect(bad / "host.sqlite3")
    con.execute("CREATE TABLE schema_info(version INTEGER)")
    con.execute("INSERT INTO schema_info VALUES(1)")
    con.commit(); con.close()
    with pytest.raises(ForgeError):
        Host(bad)


def test_host_session_expiry_and_suspension_fail_closed(tmp_path):
    import time
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    token = host.issue_session(account, lifetime=60)
    with host._connect() as con:
        con.execute("UPDATE host_sessions SET expires_at=?", (int(time.time()) - 1,))
    assert host.authenticate_session(token) is None
    fresh = host.issue_session(account)
    with host._connect() as con:
        con.execute("UPDATE accounts SET state='suspended' WHERE account_id=?", (account,))
    assert host.authenticate_session(fresh) is None


def test_global_request_admission_is_released_on_routes(tmp_path):
    host = Host(tmp_path / "host")
    app = create_app(host.root)
    slots = app.config["HOST_REQUEST_SLOTS"]
    held = [slots.acquire(blocking=False) for _ in range(31)]
    assert all(held)
    try:
        assert app.test_client().post("/api/v1/repos", data="{", content_type="application/json").status_code == 401
        assert slots.acquire(blocking=False)
        slots.release()
        assert app.test_client().get("/api/v1/auth/config").status_code == 200
    finally:
        for _ in held:
            slots.release()


def test_global_repository_budget_counts_reservations(tmp_path, monkeypatch):
    monkeypatch.setenv("BASTION_HOST_REPO_LIMIT", "1")
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["alice@example.test"])
    host.approve_account("bob", ["bob@example.test"])
    host.create_repo("alice", "one", request_id="one", seed_empty=True)
    import pytest
    from errors import ForgeError
    with pytest.raises(ForgeError, match="host repository capacity"):
        host.create_repo("bob", "two", request_id="two", seed_empty=True)


def test_clerk_csp_uses_configured_origin(monkeypatch, tmp_path):
    import clerk_auth
    from clerk_auth import ClerkConfig
    config = ClerkConfig("pk_test", "sk_test", ("https://app.example.test",), "https://clerk.example.test")
    monkeypatch.setattr(clerk_auth, "load_clerk_config", lambda: config)
    root = tmp_path / "host"
    Host(root)
    response = create_app(root).test_client().get("/api/v1/auth/config")
    csp = response.headers["Content-Security-Policy"]
    assert "script-src 'self' https://clerk.example.test" in csp
    assert "https://*.clerk.com" not in csp


def test_shared_invitation_activates_only_approved_account_and_is_retryable(tmp_path, monkeypatch):
    from errors import ForgeError
    import clerk_auth
    class FakeClerk:
        def __init__(self, _config): pass
        def get_account(self, user_id):
            email = {"user_bob": "bob@example.test", "user_charlie": "charlie@example.test"}.get(user_id)
            return {"clerk_user_id": user_id, "email": email} if email else None
    from clerk_auth import ClerkConfig
    config = ClerkConfig("pk_test", "sk_test", ("https://app.example.test",), "https://clerk.example.test")
    monkeypatch.setattr(clerk_auth, "load_clerk_config", lambda: config)
    monkeypatch.setattr(clerk_auth, "ClerkAuthenticator", FakeClerk)
    host = Host(tmp_path / "host")
    alice = host.approve_account("alice", ["alice@example.test"])
    bob = host.approve_account("bob", ["bob@example.test"])
    charlie = host.approve_account("charlie", ["charlie@example.test"])
    host.link_clerk_account("user_alice", ["alice@example.test"])
    host.link_clerk_account("user_bob", ["bob@example.test"])
    host.link_clerk_account("user_charlie", ["charlie@example.test"])
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    (source / "src").mkdir()
    (source / "src" / "lib.txt").write_text("shared\n")
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Alice"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "alice@example.test"], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "seed"], check=True)
    repo = host.create_repo("alice", "shared", request_id="shared", source=source)
    client = create_app(host.root).test_client()
    alice_host = host.issue_session(alice)
    bob_host = host.issue_session(bob)
    alice_ui = client.post(f"/api/v1/repos/{repo['repo_id']}/auth/session", json={}, headers={"Authorization": "Bearer " + alice_host}).json["access_token"]
    payload = {"principal_id": "bob_view", "clerk_user_id": "user_bob", "readable_paths": ["src"], "access": "contributor"}
    url = f"/api/v1/repos/{repo['repo_id']}/invitations"
    activate = Host.activate_membership_intent
    attempts = {"count": 0}
    def fail_after_local_publish(self, *args):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ForgeError("injected host publication interruption", "storage")
        return activate(self, *args)
    monkeypatch.setattr(Host, "activate_membership_intent", fail_after_local_publish)
    assert client.post(url, json=payload, headers={"Authorization": "Bearer " + alice_ui}).status_code == 503
    assert client.post(url, json=payload, headers={"Authorization": "Bearer " + alice_ui}).status_code == 201
    assert client.post(url, json=payload, headers={"Authorization": "Bearer " + alice_ui}).status_code == 201
    discovered = client.get("/api/v1/repos", headers={"Authorization": "Bearer " + bob_host}).json["repos"]
    assert [item["repo_id"] for item in discovered] == [repo["repo_id"]]
    bob_ui = client.post(f"/api/v1/repos/{repo['repo_id']}/auth/session", json={}, headers={"Authorization": "Bearer " + bob_host}).json["access_token"]
    assert client.get(f"/api/v1/repos/{repo['repo_id']}/tree", headers={"Authorization": "Bearer " + bob_ui}).status_code == 200
    from http_server import add_token
    credential = add_token(host.root / "repos" / repo["repo_id"], "bob_view", scopes=["fetch", "push"], expires_in="1h")
    import os
    import threading
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        clone = tmp_path / "bob-clone"
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        url_git = f"http://bob_view:{credential}@127.0.0.1:{server.server_port}/alice/shared.git"
        subprocess.run(["git", "clone", url_git, str(clone)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
        subprocess.run(["git", "-C", str(clone), "config", "user.name", "Bob"], check=True)
        subprocess.run(["git", "-C", str(clone), "config", "user.email", "bob@example.test"], check=True)
        (clone / "outside.txt").write_text("denied\n")
        subprocess.run(["git", "-C", str(clone), "add", "outside.txt"], check=True)
        subprocess.run(["git", "-C", str(clone), "commit", "-qm", "outside path"], check=True)
        denied = subprocess.run(["git", "-C", str(clone), "push", "origin", "HEAD:main"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)
        assert denied.returncode != 0
        subprocess.run(["git", "-C", str(clone), "reset", "--hard", "origin/main"], check=True, stdout=subprocess.PIPE)
        (clone / "src" / "bob.txt").write_text("allowed\n")
        subprocess.run(["git", "-C", str(clone), "add", "src/bob.txt"], check=True)
        subprocess.run(["git", "-C", str(clone), "commit", "-qm", "inside path"], check=True)
        subprocess.run(["git", "-C", str(clone), "push", "origin", "HEAD:main"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)
    finally:
        server.shutdown()
        thread.join(timeout=5)
    delegated = {"principal_id": "charlie_view", "clerk_user_id": "user_charlie", "readable_paths": ["src"], "access": "read-only"}
    assert client.post(url, json=delegated, headers={"Authorization": "Bearer " + bob_ui}).status_code == 403
    with host._connect(read_only=True) as con:
        assert con.execute("SELECT count(*) FROM membership_intents WHERE account_id=?", (charlie,)).fetchone()[0] == 0


def test_namespaced_smart_http_clone_and_large_push(tmp_path):
    import os
    import threading
    from werkzeug.serving import make_server
    from http_server import add_token

    host = Host(tmp_path / "host")
    aid = host.approve_account("alice", ["alice@example.test"])
    host.link_clerk_account("user_alice", ["alice@example.test"])
    repo = host.create_repo("alice", "project", request_id="project", seed_empty=True)
    credential = add_token(host.root / "repos" / repo["repo_id"], "owner", scopes=["fetch", "push"], expires_in="1h")
    app = create_app(host.root)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://owner:{credential}@127.0.0.1:{server.server_port}/alice/project.git"
        clone = tmp_path / "clone"
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        subprocess.run(["git", "clone", url, str(clone)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
        subprocess.run(["git", "-C", str(clone), "config", "user.name", "Alice"], check=True)
        subprocess.run(["git", "-C", str(clone), "config", "user.email", "alice@example.test"], check=True)
        # Incompressible data ensures the transport pack itself exceeds the
        # host JSON limit, not just the eventual uncompressed Git blob.
        (clone / "large.bin").write_bytes(os.urandom(64 << 10))
        subprocess.run(["git", "-C", str(clone), "add", "large.bin"], check=True)
        subprocess.run(["git", "-C", str(clone), "commit", "-qm", "large change"], check=True)
        subprocess.run(["git", "-C", str(clone), "push", "origin", "HEAD:main"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)
        subprocess.run(["git", "-C", str(clone), "config", "user.email", "mallory@example.test"], check=True)
        (clone / "bad-author.txt").write_text("not allowed\n")
        subprocess.run(["git", "-C", str(clone), "add", "bad-author.txt"], check=True)
        subprocess.run(["git", "-C", str(clone), "commit", "-qm", "bad author"], check=True)
        rejected = subprocess.run(["git", "-C", str(clone), "push", "origin", "HEAD:main"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)
        assert rejected.returncode != 0
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_git_token_scope_username_and_repository_are_bound(tmp_path):
    import base64
    from http_server import add_token
    host = Host(tmp_path / "host")
    host.approve_account("alice", ["alice@example.test"])
    one = host.create_repo("alice", "one", request_id="one", seed_empty=True)
    two = host.create_repo("alice", "two", request_id="two", seed_empty=True)
    root_one = host.root / "repos" / one["repo_id"]
    fetch = add_token(root_one, "owner", scopes=["fetch"])
    basic = "Basic " + base64.b64encode(f"owner:{fetch}".encode()).decode()
    wrong_user = "Basic " + base64.b64encode(f"other:{fetch}".encode()).decode()
    client = create_app(host.root).test_client()
    host_account = host._repo_owner_id(one["repo_id"])
    host_token = host.issue_session(host_account)
    host_basic = "Basic " + base64.b64encode(f"owner:{host_token}".encode()).decode()
    assert client.get("/alice/one.git/info/refs?service=git-upload-pack", headers={"Authorization": host_basic}).status_code == 401
    path = "/alice/one.git/info/refs?service=git-upload-pack"
    assert client.get(path, headers={"Authorization": basic}).status_code == 200
    assert client.get(path, headers={"Authorization": wrong_user}).status_code == 404
    receive = client.get("/alice/one.git/info/refs?service=git-receive-pack", headers={"Authorization": basic})
    assert receive.status_code == 403
    cross = client.get("/alice/two.git/info/refs?service=git-upload-pack", headers={"Authorization": basic})
    assert cross.status_code == 401
