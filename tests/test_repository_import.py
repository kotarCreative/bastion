import hashlib
import io
import os
import subprocess
import struct
import zlib

import pytest

from host import Host
from errors import ForgeError
import repository_import


def test_import_reservation_is_idempotent_and_capacity_is_shared(tmp_path):
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"], repo_limit=1)
    args = {"name": "project", "branch": "main", "request_id": "req-1",
            "bundle_sha256": hashlib.sha256(b"bundle").hexdigest(), "size_bytes": 6}
    first = repository_import.reserve(host, account, args, 1024)
    retry = repository_import.reserve(host, account, args, 1024)
    assert first["import_id"] == retry["import_id"]
    assert repository_import.public(host, first) == {
        "import_id": first["import_id"], "name": "project", "branch": "main",
        "state": "awaiting-upload", "repo": None,
    }
    with pytest.raises(ForgeError):
        repository_import.reserve(host, account, {**args, "size_bytes": 5}, 1024)
    with pytest.raises(ForgeError):
        repository_import.reserve(host, account, {**args, "name": "other", "request_id": "req-2"}, 1024)


def test_upload_verifies_size_digest_and_account_scope(tmp_path):
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    body = b"not-a-git-bundle"
    args = {"name": "project", "branch": "main", "request_id": "req-1",
            "bundle_sha256": hashlib.sha256(body).hexdigest(), "size_bytes": len(body)}
    row = repository_import.reserve(host, account, args, 1024)
    assert repository_import.get(host, "f" * 32, row["import_id"]) is None
    uploaded = repository_import.upload(host, account, row["import_id"], io.BytesIO(body), len(body), 1024)
    assert uploaded["state"] == "uploaded"
    assert (host.root / "imports" / row["import_id"] / "bundle").read_bytes() == body
    with pytest.raises(ForgeError):
        repository_import.upload(host, account, row["import_id"], io.BytesIO(b"wrong"), 5, 1024)


def test_upload_refuses_unclaimed_populated_stage_directory(tmp_path):
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["alice@example.test"])
    body = b"bundle"
    row = repository_import.reserve(host, account, {
        "name":"project", "branch":"main", "request_id":"r1",
        "bundle_sha256":hashlib.sha256(body).hexdigest(), "size_bytes":len(body),
    }, 1024)
    stage = host.root / "imports" / row["import_id"]
    stage.mkdir(mode=0o700); (stage / "foreign").write_text("do not adopt")
    with pytest.raises(ForgeError):
        repository_import.upload(host, account, row["import_id"], io.BytesIO(body), len(body), 1024)
    assert (stage / "foreign").read_text() == "do not adopt"


def test_real_bundle_import_preserves_tip_and_ready_retry(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    def git(*args, env=None):
        return subprocess.run(["git", "-C", str(source), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env).stdout
    git("init", "-b", "main")
    env = os.environ.copy(); env.update({"GIT_AUTHOR_NAME":"Old One", "GIT_AUTHOR_EMAIL":"old1@example.test",
        "GIT_COMMITTER_NAME":"Old One", "GIT_COMMITTER_EMAIL":"old1@example.test"})
    (source / "readme.txt").write_text("history\n")
    git("add", "."); git("commit", "-m", "first", env=env)
    env.update({"GIT_AUTHOR_NAME":"Old Two", "GIT_AUTHOR_EMAIL":"old2@example.test",
                "GIT_COMMITTER_NAME":"Old Two", "GIT_COMMITTER_EMAIL":"old2@example.test"})
    (source / "readme.txt").write_text("history two\n")
    git("commit", "-am", "second", env=env)
    tip = git("rev-parse", "refs/heads/main").decode().strip()
    bundle = tmp_path / "repo.bundle"
    subprocess.run(["git", "-C", str(source), "bundle", "create", "--version=2", str(bundle), "refs/heads/main"], check=True)
    content = bundle.read_bytes()
    host = Host(tmp_path / "host")
    account = host.approve_account("alice", ["owner@example.test"])
    payload = {"name":"project", "branch":"main", "request_id":"import-1",
               "bundle_sha256":hashlib.sha256(content).hexdigest(), "size_bytes":len(content)}
    from host_server import create_app
    client = create_app(host.root).test_client()
    auth = {"Authorization": "Bearer " + host.issue_session(account)}
    # Exercise the private intake validator separately from the first-push HTTP
    # workflow. Bundle upload/completion are no longer public API routes.
    row = repository_import.reserve(host, account, payload, 32 << 20)
    job = repository_import.public(host, row)
    other = host.approve_account("bob", ["bob@example.test"])
    assert client.get(f"/api/v1/imports/{job['import_id']}",
                      headers={"Authorization":"Bearer " + host.issue_session(other)}).status_code == 404
    repository_import.upload(host, account, job["import_id"], io.BytesIO(content), len(content), 32 << 20)
    upload_retry = repository_import.upload(host, account, job["import_id"], io.BytesIO(content), len(content), 32 << 20)
    assert upload_retry["state"] == "uploaded"
    # Simulate a process dying during validation: the restart may retire only
    # the work directory whose persisted device/inode proves ownership.
    stage = host.root / "imports" / job["import_id"]
    stale_work = stage / "validation-work"
    stale_work.mkdir(mode=0o700); (stale_work / "partial").write_bytes(b"partial")
    stale_info = stale_work.stat()
    with host._connect() as con:
        con.execute("UPDATE imports SET work_name=?,work_dev=?,work_ino=? WHERE import_id=?",
                    (stale_work.name, stale_info.st_dev, stale_info.st_ino, job["import_id"]))
    host = Host(host.root, initialize=False, migrate=True)
    client = create_app(host.root).test_client()
    completed = repository_import.complete(host, account, job["import_id"])
    assert completed["state"] == "ready"
    ready = repository_import.get(host, account, job["import_id"])
    assert ready["state"] == "ready"
    with host._connect(read_only=True) as con:
        repo = con.execute("SELECT * FROM repos WHERE repo_id=?", (ready["repo_id"],)).fetchone()
    assert repo and repo["state"] == "ready"
    root = host.root / "repos" / ready["repo_id"]
    from repository import Repository
    repository = Repository.open(root)
    assert repository.load(use_cache=False)[2].records[0]["payload"]["canonical_tip"] == tip
    retry = repository_import.complete(host, account, job["import_id"])
    assert retry["repo_id"] == ready["repo_id"]
    assert repository_import.public(host, retry)["repo"]
    # Imported historical authors are preserved; subsequent writes still use
    # the owner's ordinary approved identity and the normal Git receive path.
    import threading
    from werkzeug.serving import make_server
    from http_server import add_token
    from host_server import create_app
    credential = add_token(root, "owner", scopes=["fetch", "push"], expires_in="1h")
    server = make_server("127.0.0.1", 0, create_app(host.root), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        clone = tmp_path / "clone"
        git_env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        url = f"http://owner:{credential}@127.0.0.1:{server.server_port}/alice/project.git"
        subprocess.run(["git", "clone", url, str(clone)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=git_env, timeout=60)
        subprocess.run(["git", "-C", str(clone), "config", "user.name", "Owner"], check=True)
        subprocess.run(["git", "-C", str(clone), "config", "user.email", "owner@example.test"], check=True)
        (clone / "after-import.txt").write_text("pushed\n")
        subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
        subprocess.run(["git", "-C", str(clone), "commit", "-qm", "after import"], check=True)
        subprocess.run(["git", "-C", str(clone), "push", "origin", "HEAD:main"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=git_env, timeout=60)
        advanced = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], check=True,
                                  stdout=subprocess.PIPE).stdout.decode().strip()
        assert advanced != tip
        advanced_canonical = repository.load(use_cache=False)[2].canonical_tip
        assert advanced_canonical != tip
        # Simulate the host publishing before the import-status transaction.
        # Retry must reconcile the ready repository, not rewind it.
        with host._connect() as con:
            con.execute("UPDATE imports SET state='failed' WHERE import_id=?", (job["import_id"],))
        retry = repository_import.complete(host, account, job["import_id"])
        assert retry["repo_id"] == ready["repo_id"]
        assert repository.load(use_cache=False)[2].canonical_tip == advanced_canonical
        subprocess.run(["git", "-C", str(clone), "config", "user.email", "not-approved@example.test"], check=True)
        (clone / "unapproved.txt").write_text("must not be accepted\n")
        subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
        subprocess.run(["git", "-C", str(clone), "commit", "-qm", "unapproved author"], check=True)
        denied = subprocess.run(["git", "-C", str(clone), "push", "origin", "HEAD:main"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=git_env, timeout=60)
        assert denied.returncode != 0
        assert repository.load(use_cache=False)[2].canonical_tip == advanced_canonical
        reopened = Host(host.root, initialize=False, migrate=True)
        recovered = repository_import.complete(reopened, account, job["import_id"])
        assert recovered["state"] == "ready" and recovered["repo_id"] == ready["repo_id"]
    finally:
        server.shutdown(); thread.join(timeout=5)


def _git_varint(value):
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128); value >>= 7
    out.append(value)
    return bytes(out)


def test_pack_preflight_rejects_declared_bomb_and_delta_target_before_git(tmp_path):
    branch = "main"
    oid = "0" * 40
    header = b"# v2 git bundle\n" + oid.encode() + b" refs/heads/main\n\n"
    # A tiny compressed stream claiming a per-object size beyond the configured cap.
    size = 4 << 20 | 1
    size_header = bytearray([(3 << 4) | (size & 15)]); size >>= 4
    while size:
        size_header[-1] |= 128
        size_header.append((size & 127) | (128 if size >> 7 else 0)); size >>= 7
    pack = b"PACK" + struct.pack(">II", 2, 1) + bytes(size_header) + zlib.compress(b"x")
    bundle = header + pack + hashlib.sha1(pack).digest()
    path = tmp_path / "bomb.bundle"; path.write_bytes(bundle)
    row = {"size_bytes":len(bundle), "bundle_sha256":hashlib.sha256(bundle).hexdigest(), "branch":branch}
    with pytest.raises(ForgeError): repository_import.preflight(path, row)

    # A small declared object must not let compressed input inflate past its cap.
    pack = b"PACK" + struct.pack(">II", 2, 1) + bytes([0x31]) + zlib.compress(b"x" * ((4 << 20) + 1))
    pack += hashlib.sha1(pack).digest()
    bundle = header + pack; path.write_bytes(bundle)
    row = {"size_bytes":len(bundle), "bundle_sha256":hashlib.sha256(bundle).hexdigest(), "branch":branch}
    with pytest.raises(ForgeError): repository_import.preflight(path, row)


    # Delta has source-size then target-size varints; reject the target before resolving a base.
    delta = _git_varint(1) + _git_varint((4 << 20) + 1) + b"\x00"
    delta_header = bytearray([(6 << 4) | (len(delta) & 15)]); remaining = len(delta) >> 4
    while remaining:
        delta_header[-1] |= 128; delta_header.append((remaining & 127) | (128 if remaining >> 7 else 0)); remaining >>= 7
    pack = b"PACK" + struct.pack(">II", 2, 1) + bytes(delta_header) + b"\x01" + zlib.compress(delta)
    pack += hashlib.sha1(pack).digest()
    bundle = header + pack; path.write_bytes(bundle)
    row = {"size_bytes":len(bundle), "bundle_sha256":hashlib.sha256(bundle).hexdigest(), "branch":branch}
    with pytest.raises(ForgeError): repository_import.preflight(path, row)


def test_bundle_header_preflight_rejects_wrong_branch_extra_ref_and_prerequisites(tmp_path):
    bad_headers = [
        b"# v2 git bundle\n" + b"0" * 40 + b" refs/heads/other\n\n",
        b"# v2 git bundle\n" + b"0" * 40 + b" refs/heads/main\n" + b"1" * 40 + b" refs/tags/extra\n\n",
        b"# v2 git bundle\n-" + b"0" * 40 + b" missing base\n" + b"1" * 40 + b" refs/heads/main\n\n",
    ]
    for index, header in enumerate(bad_headers):
        path = tmp_path / f"bad-{index}.bundle"
        path.write_bytes(header + b"not a pack")
        row = {"size_bytes":path.stat().st_size, "bundle_sha256":hashlib.sha256(path.read_bytes()).hexdigest(), "branch":"main"}
        with pytest.raises(ForgeError): repository_import.preflight(path, row)
