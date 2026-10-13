"""Owner-only, one-branch smart-HTTP bootstrap for an uninitialized repository.

This is deliberately separate from ordinary receive-pack. Imported historical
authors are source evidence, never grants. Once ready, only an idempotent initial
push acknowledgement is available here; repository credentials and normal view
authorization handle all subsequent reads and writes.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import time
from contextlib import nullcontext

import limits
import repository_import as imports
import tokens
from errors import ForgeError
from http_server import pkt_line
from layout import ZERO_OID

TOKEN_RE = re.compile(r"^bsi1\.([A-Za-z0-9_-]{22})\.([A-Za-z0-9_-]{43})$")
CAPABILITIES = b"report-status ofs-delta atomic object-format=sha1 agent=bastion/1"


def reserve(host, account_id, payload):
    if not isinstance(payload, dict) or set(payload) != {"name", "branch", "request_id"}:
        raise ForgeError("invalid first-push import request")
    # Reserve repository capacity now; reserve disk work immediately before the
    # receive body, so empty pending repos do not monopolize all staging space.
    internal = dict(payload, bundle_sha256="0" * 64, size_bytes=limits.IMPORT_BUNDLE_BYTES)
    return imports.reserve(host, account_id, internal, limits.IMPORT_BUNDLE_BYTES, mode="push")


def public(host, row):
    result = imports.public(host, row)
    if result["state"] == "awaiting-upload":
        result["state"] = "awaiting-push"
    result["git_url"] = f"/{host._account_owner(row['account_id'])}/{row['name']}.git"
    result["username"] = "owner"
    return result


def _digest(token_id, secret):
    return hashlib.sha256(b"bastion-first-push-v1\0" + token_id.encode() + b"\0" + secret.encode()).hexdigest()


def issue_credential(host, account_id, import_id, authorize=None):
    prior = imports.get(host, account_id, import_id)
    if prior is None or prior["mode"] != "push":
        return None
    with imports._lock(host, import_id):
        row = imports.get(host, account_id, import_id)
        if row is None or row["mode"] != "push":
            return None
        if row["state"] == "ready":
            raise ForgeError("import is already initialized", "conflict")
        now = int(time.time())
        token_id, secret = tokens.generate_token_id(), tokens.generate_secret()
        with host._connect() as con:
            if not con.execute("SELECT 1 FROM accounts WHERE account_id=? AND state='approved'", (account_id,)).fetchone():
                raise ForgeError("account is unavailable", "authentication")
            if authorize:
                authorize()
            # Rotation is bounded to one digest per operation and invalidates
            # a lost credential without attempting to recover its secret.
            con.execute("INSERT INTO import_tokens VALUES(?,?,?,?,?) ON CONFLICT(import_id) DO UPDATE SET token_id=excluded.token_id,digest=excluded.digest,created_at=excluded.created_at,expires_at=excluded.expires_at",
                        (import_id, token_id, _digest(token_id, secret), now, now + 3600))
        return {"credential": f"bsi1.{token_id}.{secret}", "expires_at": now + 3600, "username": "owner"}


def authenticate(host, credential, owner, name):
    match = TOKEN_RE.fullmatch(credential or "")
    if not match:
        return None
    token_id, secret = match.groups()
    with host._connect(read_only=True) as con:
        row = con.execute("SELECT i.*, t.digest AS token_digest,t.expires_at FROM import_tokens t "
                          "JOIN imports i ON i.import_id=t.import_id JOIN accounts a ON a.account_id=i.account_id "
                          "WHERE t.token_id=? AND i.mode='push' AND a.state='approved' AND a.owner_name=? AND i.name=?",
                          (token_id, owner, name)).fetchone()
    if not tokens.compare_digest(_digest(token_id, secret), row["token_digest"] if row else tokens.DUMMY_DIGEST):
        return None
    if not row or row["expires_at"] <= int(time.time()):
        return None
    result = dict(row)
    if result["state"] == "ready" and (not result["repo_id"] or not re.fullmatch(r"[0-9a-f]{40}", result["push_tip"] or "")):
        return None
    if result["repo_id"]:
        with host._connect(read_only=True) as con:
            repo = con.execute("SELECT state FROM repos WHERE repo_id=?", (result["repo_id"],)).fetchone()
        if repo is None or result["state"] == "ready" and repo["state"] != "ready":
            return None
        if repo and repo["state"] == "ready":
            # A bootstrap credential must not survive policy/membership removal
            # as a route to historical refs or an invitation/permission repair.
            visible = host.public_repos(result["account_id"])
            if not any(r["repo_id"] == result["repo_id"] and "owner" in r["views"] for r in visible):
                return None
            from repository import Repository
            repository = Repository.open(host.root / "repos" / result["repo_id"])
            view = repository.load()[2].active_views.get("owner")
            if not view or view["principal_id"] != "owner":
                return None
            descriptor = repository._descriptor(view["descriptor_id"])
            if (descriptor["capabilities"]["read"] != [""] or descriptor["restrictions"]["read"] or
                    descriptor["capabilities"]["admin"] != [""] or descriptor["restrictions"]["admin"]):
                return None
    return result


def advertisement(row):
    if row["state"] == "ready":
        # Advertise only the original imported ref for acknowledgement retries,
        # not the current canonical or permission-projected repository tip.
        ref = row["push_tip"] + " refs/heads/" + row["branch"]
    else:
        ref = ZERO_OID + " capabilities^{}"
    return pkt_line(b"# service=git-receive-pack\n") + b"0000" + pkt_line(ref.encode() + b"\0" + CAPABILITIES + b"\n") + b"0000"


def parse_receive(body, branch):
    """Accept precisely one create command followed by a self-contained pack."""
    pos, commands, capabilities = 0, [], []
    while True:
        if pos + 4 > len(body) or pos > 8192:
            raise ForgeError("invalid receive framing", "policy")
        raw_length = body[pos:pos + 4]
        if not re.fullmatch(rb"[0-9a-fA-F]{4}", raw_length):
            raise ForgeError("invalid receive framing", "policy")
        length = int(raw_length, 16)
        pos += 4
        if length == 0:
            break
        if length < 4 or length > 8192 or pos + length - 4 > len(body):
            raise ForgeError("invalid receive command", "policy")
        command = body[pos:pos + length - 4].rstrip(b"\n")
        pos += length - 4
        if not commands:
            command, separator, caps = command.partition(b"\0")
            if separator:
                capabilities = caps.split()
        if b"\0" in command:
            raise ForgeError("invalid receive command", "policy")
        commands.append(command)
        if len(commands) != 1:
            raise ForgeError("only the selected branch may be imported", "policy")
    if len(commands) != 1:
        raise ForgeError("one initial branch is required", "policy")
    fields = commands[0].split(b" ")
    if (len(fields) != 3 or fields[0] != ZERO_OID.encode() or not re.fullmatch(rb"[0-9a-f]{40}", fields[1]) or
            fields[1] == ZERO_OID.encode() or fields[2] != ("refs/heads/" + branch).encode()):
        raise ForgeError("only the initial selected branch is authorized", "policy")
    allowed = {b"report-status", b"ofs-delta", b"atomic", b"object-format=sha1"}
    if any(c not in allowed and not c.startswith(b"agent=") for c in capabilities):
        raise ForgeError("unsupported receive capability", "policy")
    pack = body[pos:]
    if len(pack) > limits.IMPORT_PACK_BYTES:
        raise ForgeError("import pack exceeds limit", "limits")
    if not pack.startswith(b"PACK"):
        raise ForgeError("a complete pack is required", "policy")
    return fields[1].decode(), pack


def _reset_failed_attempt(host, row):
    """Retire only proven, uncommitted work when a rejected source is corrected."""
    if row["state"] != "failed" or row["source_identity"] is not None or row["repo_id"] is not None:
        raise ForgeError("initial source is already pinned", "conflict")
    stage = imports._stage(host, row)
    if (stage / "source.git").exists() or (stage / "source.git").is_symlink():
        raise ForgeError("unregistered source must not be adopted", "integrity")
    if row["work_name"]:
        if row["work_name"] != "validation-work":
            raise ForgeError("validation work identity changed", "integrity")
        work = stage / row["work_name"]
        if work.exists():
            info = work.stat()
            if work.is_symlink() or (info.st_dev, info.st_ino) != (row["work_dev"], row["work_ino"]):
                raise ForgeError("validation work identity changed", "integrity")
            shutil.rmtree(work)
    row = imports._retire_upload_temp(host, row["account_id"], row, stage)
    bundle = stage / "bundle"
    if bundle.exists():
        if not imports._bundle_matches(bundle, row):
            raise ForgeError("intake identity changed", "integrity")
        bundle.unlink()
    fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    with host._connect() as con:
        con.execute("UPDATE imports SET work_name=NULL,work_dev=NULL,work_ino=NULL,work_repo_dev=NULL,work_repo_ino=NULL,"
                    "push_tip=NULL,bundle_sha256=?,size_bytes=?,state='awaiting-upload',error=NULL WHERE import_id=?",
                    ("0" * 64, limits.IMPORT_BUNDLE_BYTES, row["import_id"]))


def reserve_intake(host, row):
    required = limits.IMPORT_BUNDLE_BYTES + 3 * limits.IMPORT_EXPANDED_BYTES
    with host._connect() as con:
        con.execute("BEGIN IMMEDIATE")
        current = con.execute("SELECT state,reserved_stage_bytes FROM imports WHERE import_id=? AND mode='push'", (row["import_id"],)).fetchone()
        if current is None:
            raise ForgeError("import is unavailable", "integrity")
        if current["state"] == "ready":
            return
        reserved = con.execute("SELECT coalesce(sum(reserved_stage_bytes),0) FROM imports").fetchone()[0]
        if reserved - current["reserved_stage_bytes"] + required > limits.IMPORT_STAGING_BYTES:
            raise ForgeError("import staging is saturated", "limits")
        con.execute("UPDATE imports SET reserved_stage_bytes=? WHERE import_id=?", (required, row["import_id"]))


def release_unused_intake(host, row):
    # Called under the operation lock. No pinned/retained work is discounted.
    with host._connect() as con:
        con.execute("UPDATE imports SET reserved_stage_bytes=0 WHERE import_id=? AND push_tip IS NULL AND stage_dev IS NULL AND source_dev IS NULL AND repo_id IS NULL", (row["import_id"],))


def receive(host, row, body, authorize, failpoint=None, *, _locked=False):
    tip, pack = parse_receive(body, row["branch"])
    # An internal ref envelope reuses the already reviewed pack/history validator;
    # no bundle file, browser upload, or client-side archive is involved.
    envelope = b"# v2 git bundle\n" + tip.encode() + b" refs/heads/" + row["branch"].encode() + b"\n\n" + pack
    digest = hashlib.sha256(envelope).hexdigest()
    incoming = dict(row, bundle_sha256=digest, size_bytes=len(envelope))
    imports._preflight_data(envelope, incoming, time.monotonic() + limits.IMPORT_VALIDATION_SECONDS)
    with nullcontext() if _locked else imports._lock(host, row["import_id"]):
        authorize()
        reserve_intake(host, row)
        current = host.prepare_import(imports.get(host, row["account_id"], row["import_id"]))
        corrected_failed_pack = (current["state"] == "failed" and current["source_identity"] is None and
                                 current["repo_id"] is None and current["bundle_sha256"] != digest)
        if current["push_tip"] and (current["push_tip"] != tip or corrected_failed_pack):
            _reset_failed_attempt(host, current)
            current = imports.get(host, row["account_id"], row["import_id"])
        if current["state"] == "ready":
            return current
        if not current["push_tip"]:
            with host._connect() as con:
                con.execute("UPDATE imports SET push_tip=?,bundle_sha256=?,size_bytes=? WHERE import_id=? AND push_tip IS NULL",
                            (tip, digest, len(envelope), row["import_id"]))
            current = imports.get(host, row["account_id"], row["import_id"])
        # A retransmitted pack may have different compression/order. The pinned
        # original intake remains authoritative for the same requested tip.
        stage = imports._stage(host, current)
        if not (stage / "bundle").exists():
            if current["source_dev"] is not None or current["repo_id"] is not None:
                raise ForgeError("pinned intake is missing", "integrity")
            # A crash before the atomic intake publication must not require the
            # client's recompressed pack to have exactly the previous byte order.
            current = imports._retire_upload_temp(host, current["account_id"], current, stage)
            with host._connect() as con:
                con.execute("UPDATE imports SET bundle_sha256=?,size_bytes=? WHERE import_id=? AND push_tip=? AND source_dev IS NULL AND repo_id IS NULL",
                            (digest, len(envelope), current["import_id"], tip))
            current = imports.get(host, current["account_id"], current["import_id"])
            imports.upload(host, current["account_id"], current["import_id"], io.BytesIO(envelope), len(envelope),
                           limits.IMPORT_BUNDLE_BYTES, authorize=authorize, _locked=True)
            current = imports.get(host, current["account_id"], current["import_id"])
        # Publish the authoritative intake before durable validation/provisioning
        # receipts. A replacement container reconstructs these exact bytes.
        host.persist_import(current, envelope)
        return imports.complete(host, current["account_id"], current["import_id"], failpoint=failpoint,
                                authorize=authorize, _locked=True)


def report(branch, error=None):
    ref = ("refs/heads/" + branch).encode()
    status = b"ok " + ref + b"\n" if error is None else b"ng " + ref + b" import rejected\n"
    unpack = b"unpack ok\n" if error is None else b"unpack import rejected\n"
    return pkt_line(unpack) + pkt_line(status) + b"0000"
