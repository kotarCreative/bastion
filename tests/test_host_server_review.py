"""Independent integration regressions from the served-host review."""

import base64
import subprocess
import threading

import pytest

from errors import ForgeError
from host import Host
from host_server import create_app
from identity import empty_capability_map


@pytest.fixture
def hosted(tmp_path):
    host = Host(tmp_path / "host")
    alice = host.approve_account("alice", ["alice@example.test"])
    bob = host.approve_account("bob", ["bob@example.test"])
    host.link_clerk_account("user_alice", ["alice@example.test"])
    host.link_clerk_account("user_bob", ["bob@example.test"])
    repo = host.create_repo("alice", "private", request_id="private", seed_empty=True)
    app = create_app(host.root)
    client = app.test_client()
    at = host.issue_session(alice)
    bt = host.issue_session(bob)
    exchange = client.post(f"/api/v1/repos/{repo['repo_id']}/auth/session", json={},
                           headers={"Authorization": "Bearer " + at})
    assert exchange.status_code == 200
    return host, alice, bob, repo, app, client, at, bt, exchange.json["access_token"]


def test_corrupt_membership_cannot_exchange_another_accounts_owner_token(hosted):
    host, alice, bob, repo, app, client, at, bt, ui = hosted
    with host._connect() as con:
        con.execute("INSERT INTO memberships VALUES(?,?,?,'active')", (repo["repo_id"], bob, "owner"))
    assert client.get("/api/v1/repos", headers={"Authorization": "Bearer " + bt}).json == {"repos": []}
    assert client.post(f"/api/v1/repos/{repo['repo_id']}/auth/session", json={},
                       headers={"Authorization": "Bearer " + bt}).status_code == 404
    assert client.get(f"/api/v1/repos/{repo['repo_id']}/tree",
                      headers={"Authorization": "Bearer " + ui}).status_code == 200


@pytest.mark.parametrize("column,value", [("digest", b"not-a-string"), ("digest", "not-a-digest"),
                                         ("expires_at", "not-a-time"), ("created_at", "not-a-time")])
def test_corrupt_host_session_records_fail_with_uniform_401(tmp_path, column, value):
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    token = host.issue_session(account)
    with host._connect() as con:
        con.execute(f"UPDATE host_sessions SET {column}=?", (value,))
    assert host.authenticate_session(token) is None
    response = create_app(host.root).test_client().get("/api/v1/session", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401
    assert response.json == {"error": "auth-required"}


def test_serve_migrates_existing_v1_without_initializing_unknown_roots(tmp_path):
    host = Host(tmp_path / "host")
    aid = host.approve_account("alice", ["alice@example.test"])
    with host._connect() as con:
        for table in ("host_sessions", "host_settings", "membership_intents", "import_tokens"):
            con.execute(f"DROP TABLE {table}")
        con.execute("UPDATE schema_info SET version=1")
    assert create_app(host.root).test_client().get("/api/v1/auth/config").status_code == 200
    recovered = Host(host.root, initialize=False)
    assert recovered.authenticate_session(recovered.issue_session(aid))["account_id"] == aid
    with pytest.raises(ForgeError):
        create_app(tmp_path / "unknown")
    assert not (tmp_path / "unknown").exists()


def test_provisioning_slots_bound_work_before_reservation_and_release_on_error(hosted):
    host, alice, bob, repo, app, client, at, bt, ui = hosted
    slots = app.config["HOST_PROVISION_SLOTS"]
    held = 0
    while slots.acquire(blocking=False):
        held += 1
    try:
        response = client.post("/api/v1/repos", json={"name": "new", "request_id": "new"},
                               headers={"Authorization": "Bearer " + at})
        assert response.status_code == 503
        assert len(host.list_repos()) == 1
    finally:
        for _ in range(held):
            slots.release()
    assert client.post("/api/v1/repos", json={"name": "new", "request_id": "new", "branch": []},
                       headers={"Authorization": "Bearer " + at}).status_code == 400
    assert client.post("/api/v1/repos", json={"name": "new", "request_id": "new"},
                       headers={"Authorization": "Bearer " + at}).status_code == 201


def test_scoped_credential_revocation_preserves_token_id_grammar(hosted):
    host, alice, bob, repo, app, client, at, bt, ui = hosted
    prefix = f"/api/v1/repos/{repo['repo_id']}"
    headers = {"Authorization": "Bearer " + ui}
    created = client.post(prefix + "/credentials", headers=headers,
                          json={"view_id": "owner", "scopes": ["fetch"], "expires_in": "1h"})
    assert created.status_code == 201
    token_id = created.json["credential"].split(".")[1]
    assert client.delete(prefix + "/credentials/" + token_id, headers=headers).status_code == 200
    listing = client.get(prefix + "/credentials", headers=headers).json
    assert listing["tokens"][0]["revoked_at"] is not None
    for method, suffix in (("POST", "tree"), ("GET", "logout"), ("GET", "credentials/anything"), ("POST", "auth/clerk")):
        assert client.open(prefix + "/" + suffix, method=method, headers=headers).status_code == 404


def test_shared_git_and_source_admission_cannot_be_reset_by_repository_url(hosted, monkeypatch):
    import repository_server
    import limits
    from http_server import add_token
    host, alice, bob, repo, app, client, at, bt, ui = hosted
    other = host.create_repo("alice", "other", request_id="other", seed_empty=True)
    tokens = [add_token(host.root / "repos" / item["repo_id"], "owner", scopes=["fetch"]) for item in (repo, other)]
    headers = [{"Authorization": "Basic " + base64.b64encode(f"owner:{token}".encode()).decode()} for token in tokens]
    paths = ["/alice/private.git/info/refs?service=git-upload-pack", "/alice/other.git/info/refs?service=git-upload-pack"]
    slots = app.config["HOST_GIT_SLOTS"]
    held = 0
    while slots.acquire(blocking=False):
        held += 1
    try:
        assert all(client.get(path, headers=header).status_code == 503 for path, header in zip(paths, headers))
    finally:
        for _ in range(held):
            slots.release()
    assert client.get(paths[0], headers=headers[0]).status_code == 200
    entered, release = threading.Event(), threading.Event()
    result = []
    def blocked_service(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return subprocess.CompletedProcess([], 0, b"", b"")
    monkeypatch.setattr(repository_server, "run_service", blocked_service)
    monkeypatch.setattr(limits, "MAX_ACTIVE_PER_IP", 1)
    def first_request():
        with app.test_client() as threaded_client:
            result.append(threaded_client.get(paths[0], headers=headers[0]).status_code)
    thread = threading.Thread(target=first_request)
    thread.start()
    try:
        assert entered.wait(10)
        assert client.get(paths[1], headers=headers[1]).status_code == 429
    finally:
        release.set()
        thread.join(15)
    assert result == [200]
    assert client.get(paths[1], headers=headers[1]).status_code == 200


def test_email_invitation_and_reconciliation_never_restore_disabled_membership(hosted, monkeypatch):
    import clerk_auth
    from clerk_auth import ClerkConfig
    from http_server import add_token
    from service import add_role, bind_role, transition_principal
    class FakeClerk:
        def __init__(self, config): pass
        def get_account(self, uid):
            return {"clerk_user_id": uid, "email": "bob@example.test"} if uid == "user_bob" else None
    monkeypatch.setattr(clerk_auth, "load_clerk_config", lambda: ClerkConfig("pk_test", "sk_test", ("https://bastion.test",), "https://clerk.test"))
    monkeypatch.setattr(clerk_auth, "ClerkAuthenticator", FakeClerk)
    host, alice, bob, repo, _app, _client, at, bt, ui = hosted
    client = create_app(host.root).test_client()
    prefix = f"/api/v1/repos/{repo['repo_id']}"
    headers = {"Authorization": "Bearer " + ui}
    body = {"principal_id": "bob-view", "email": "bob@example.test", "access": "read-only", "readable_paths": [""]}
    assert client.post(prefix + "/invitations", json=body, headers=headers).status_code == 201
    assert host.public_repos(bob)[0]["views"] == ["bob-view"]
    # Lost host publication is recovered from the existing WAL; it grants no new policy.
    with host._connect() as con:
        con.execute("DELETE FROM memberships WHERE account_id=?", (bob,))
        con.execute("UPDATE membership_intents SET state='pending' WHERE account_id=?", (bob,))
    assert host.public_repos(bob)[0]["views"] == ["bob-view"]
    with host._connect() as con:
        con.execute("UPDATE memberships SET state='disabled' WHERE account_id=?", (bob,))
        con.execute("UPDATE membership_intents SET state='pending' WHERE account_id=?", (bob,))
    assert host.public_repos(bob) == []
    assert client.post(prefix + "/invitations", json=body, headers=headers).status_code != 201
    with host._connect(read_only=True) as con:
        assert con.execute("SELECT state FROM memberships WHERE account_id=?", (bob,)).fetchone()[0] == "disabled"
    # A root administrator with a hidden read path cannot delegate unrestricted read.
    host.approve_account("charlie", ["charlie@example.test"])
    root = host.root / "repos" / repo["repo_id"]
    deny = empty_capability_map()
    deny["read"] = ["secret"]
    allow = empty_capability_map()
    allow["read"] = [""]
    add_role(root, "owner-hidden", allow, deny)
    bind_role(root, "owner-hidden", "principal", "owner")
    transition_principal(root, "owner")
    new_ui = add_token(root, "owner", scopes=["ui"])
    denied = dict(body, email="charlie@example.test", principal_id="charlie")
    assert client.post(prefix + "/invitations", json=denied, headers={"Authorization": "Bearer " + new_ui}).status_code == 400
    with host._connect(read_only=True) as con:
        assert con.execute("SELECT count(*) FROM membership_intents").fetchone()[0] == 1


def test_partial_clerk_configuration_fails_at_startup(tmp_path, monkeypatch):
    host = Host(tmp_path / "host")
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test")
    monkeypatch.delenv("CLERK_PUBLISHABLE_KEY", raising=False)
    monkeypatch.delenv("CLERK_AUTHORIZED_PARTIES", raising=False)
    with pytest.raises(ForgeError, match="Clerk requires"):
        create_app(host.root)


def test_invitation_parent_prefix_cannot_bypass_nested_private_paths():
    from service import require_delegable_coverage
    descriptor = {"capabilities": {"read": ["", "secret/open"]}, "restrictions": {"read": ["secret", "docs/private"]}}
    for path in ("", "docs", "secret"):
        with pytest.raises(ForgeError, match="visibility"):
            require_delegable_coverage(descriptor, [path], "read-only")
    for path in ("public", "docs/public", "secret/open"):
        require_delegable_coverage(descriptor, [path], "contributor")
