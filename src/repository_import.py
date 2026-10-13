"""Bounded history intake and provisioning shared by first-push bootstrap.

The ref envelope is private server staging, not a browser upload API. Legacy
v4 staged imports remain readable here without adopting or deleting their data.
"""
from __future__ import annotations

import fcntl
import configparser
import hashlib
import mmap
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
import uuid
import zlib
from contextlib import contextmanager, nullcontext
from pathlib import Path

from errors import ForgeError
from host import OPAQUE, _slug

_BUNDLE = b"# v2 git bundle\n"


def _public(row, repo=None):
    value = {"import_id": row["import_id"], "name": row["name"], "branch": row["branch"],
             "state": row["state"], "repo": repo}
    if row["state"] == "failed": value["error"] = "import-failed"
    return value


@contextmanager
def _lock(host, import_id):
    if not OPAQUE.fullmatch(import_id): raise ForgeError("import unavailable", "policy")
    path = host.root / "locks" / f"import-{import_id}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        host._check_layout()
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


def _stage(host, row):
    root = host.root / "imports"
    identity = host._directory_ids.get("imports")
    if (not identity or root.is_symlink() or not root.is_dir() or
            (root.stat().st_dev, root.stat().st_ino) != identity or root.resolve(strict=True) != root or
            root.stat().st_mode & 0o077):
        raise ForgeError("import staging is unsafe", "integrity")
    path = root / row["import_id"]
    if path.is_symlink(): raise ForgeError("import staging is unsafe", "integrity")
    if path.exists():
        if not path.is_dir(): raise ForgeError("import staging is unsafe", "integrity")
        info = path.stat()
        if row["stage_dev"] is None or row["stage_ino"] is None:
            eligible_empty = (not any(path.iterdir()) and info.st_mode & 0o077 == 0 and
                              row["state"] in {"awaiting-upload", "failed"} and
                              row["repo_id"] is None and row["source_dev"] is None and row["work_name"] is None and
                              row["upload_temp_name"] is None)
            if not eligible_empty: raise ForgeError("refusing to adopt import staging", "integrity")
        elif (info.st_dev, info.st_ino) != (row["stage_dev"], row["stage_ino"]):
            raise ForgeError("import staging identity changed", "integrity")
        if info.st_mode & 0o077: raise ForgeError("import staging permissions are unsafe", "integrity")
        for child in path.iterdir():
            if (child.is_symlink() or child.name not in {"bundle", "source.git", "validation-work"} and
                    not re.fullmatch(r"upload-[0-9a-f]{32}", child.name)):
                raise ForgeError("import staging contains an unknown entry", "integrity")
            if child.name.startswith("upload-") and child.name != row["upload_temp_name"]:
                raise ForgeError("unowned upload staging exists", "integrity")
            if child.name.startswith(".validate-") and child.name != row["work_name"]:
                raise ForgeError("unowned validation staging exists", "integrity")
    elif row["stage_dev"] is not None:
        raise ForgeError("owned import staging disappeared", "integrity")
    return path


def _ensure_stage(host, row):
    path = _stage(host, row)
    if not path.exists():
        if row["stage_dev"] is not None: raise ForgeError("owned import staging disappeared", "integrity")
        path.mkdir(mode=0o700)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)
    info = path.stat()
    if row["stage_dev"] is None:
        if any(path.iterdir()) or info.st_mode & 0o077:
            raise ForgeError("refusing to adopt populated import staging", "integrity")
        with host._connect() as con:
            con.execute("UPDATE imports SET stage_dev=?,stage_ino=? WHERE import_id=? AND stage_dev IS NULL AND state IN ('awaiting-upload','failed') AND repo_id IS NULL AND source_dev IS NULL AND work_name IS NULL AND upload_temp_name IS NULL",
                        (info.st_dev, info.st_ino, row["import_id"]))
            if con.execute("SELECT changes()").fetchone()[0] != 1:
                raise ForgeError("import staging reservation changed", "integrity")
        row = get(host, row["account_id"], row["import_id"])
        path = _stage(host, row)
    return path, row


def _retire_upload_temp(host, account_id, row, path):
    name = row["upload_temp_name"]
    if not name: return get(host, account_id, row["import_id"])
    if not re.fullmatch(r"upload-[0-9a-f]{32}", name): raise ForgeError("upload temporary identity is invalid", "integrity")
    temp = path / name
    if temp.is_symlink(): raise ForgeError("upload temporary identity changed", "integrity")
    if temp.exists():
        info = temp.stat()
        if row["upload_temp_dev"] is None and row["upload_temp_ino"] is None and info.st_size == 0:
            pass
        elif (info.st_dev, info.st_ino) != (row["upload_temp_dev"], row["upload_temp_ino"]):
            raise ForgeError("upload temporary identity changed", "integrity")
        temp.unlink()
    with host._connect() as con:
        con.execute("UPDATE imports SET upload_temp_name=NULL,upload_temp_dev=NULL,upload_temp_ino=NULL WHERE import_id=? AND upload_temp_name=?",
                    (row["import_id"], name))
    return get(host, account_id, row["import_id"])


def _bundle_matches(path, row):
    if path.is_symlink() or not path.is_file(): return False
    if path.stat().st_size != row["size_bytes"]: return False
    digest = hashlib.sha256()
    with path.open("rb") as bundle:
        for block in iter(lambda: bundle.read(65536), b""): digest.update(block)
    return digest.hexdigest() == row["bundle_sha256"]


def _assert_private_bare(path, row):
    if path.is_symlink() or not path.is_dir(): raise ForgeError("validated source identity changed", "integrity")
    allowed = {"HEAD", "config", "description", "objects", "refs", "packed-refs"}
    if any(child.name not in allowed or child.is_symlink() for child in path.iterdir()):
        raise ForgeError("validated source configuration changed", "integrity")
    config = path / "config"
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        if config.is_symlink() or not config.is_file(): raise ValueError
        parser.read(config, encoding="utf-8")
        if set(parser.sections()) != {"core", "uploadpack"}: raise ValueError
        expected = {"core": {"repositoryformatversion", "filemode", "bare", "hookspath"},
                    "uploadpack": {"allowanysha1inwant"}}
        if any(set(parser[section]) != keys for section, keys in expected.items()): raise ValueError
        if (parser["core"]["repositoryformatversion"] != "0" or parser["core"]["bare"].lower() != "true" or
                parser["core"]["hookspath"] != "/dev/null" or parser["uploadpack"]["allowanysha1inwant"].lower() != "false"):
            raise ValueError
    except (OSError, UnicodeError, configparser.Error, ValueError) as error:
        raise ForgeError("validated source configuration changed", "integrity") from error
    objects = path / "objects"
    if objects.is_symlink() or not objects.is_dir(): raise ForgeError("validated source object store changed", "integrity")
    alternates = objects / "info" / "alternates"
    if alternates.exists() or alternates.is_symlink() or (path / "shallow").exists():
        raise ForgeError("validated source contains external object state", "integrity")
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            if (Path(base) / name).is_symlink(): raise ForgeError("validated source contains a symlink", "integrity")
    if (path / "hooks").exists() and any((path / "hooks").iterdir()):
        raise ForgeError("validated source contains hooks", "integrity")


@contextmanager
def _owned_work(host, row, stage):
    name = "validation-work"
    with host._connect() as con:
        current = con.execute("SELECT work_name FROM imports WHERE import_id=?", (row["import_id"],)).fetchone()
        if not current or current["work_name"] is not None:
            raise ForgeError("validation work reservation is busy", "conflict")
        con.execute("UPDATE imports SET work_name=?,work_dev=NULL,work_ino=NULL,work_repo_dev=NULL,work_repo_ino=NULL WHERE import_id=? AND work_name IS NULL",
                    (name, row["import_id"]))
        if con.execute("SELECT changes()").fetchone()[0] != 1: raise ForgeError("validation work reservation changed", "conflict")
    path = stage / name
    if path.is_symlink(): raise ForgeError("validation work path is unsafe", "integrity")
    if path.exists():
        if not path.is_dir() or any(path.iterdir()) or path.stat().st_mode & 0o077:
            raise ForgeError("refusing to adopt validation work", "integrity")
    else:
        path.mkdir(mode=0o700)
    parent_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(parent_fd)
    finally: os.close(parent_fd)
    info = path.stat()
    with host._connect() as con:
        con.execute("UPDATE imports SET work_dev=?,work_ino=? WHERE import_id=? AND work_name=? AND work_dev IS NULL",
                    (info.st_dev, info.st_ino, row["import_id"], name))
        if con.execute("SELECT changes()").fetchone()[0] != 1: raise ForgeError("validation work identity changed", "integrity")
    try:
        yield path
    except BaseException:
        # A retry can retire only this persisted work root after checking its inode.
        raise
    current = get(host, row["account_id"], row["import_id"])
    _stage(host, current)
    if path.is_symlink() or not path.is_dir(): raise ForgeError("validation work path changed", "integrity")
    current_info = path.stat()
    if (current_info.st_dev, current_info.st_ino) != (info.st_dev, info.st_ino):
        raise ForgeError("validation work identity changed", "integrity")
    shutil.rmtree(path)
    parent_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(parent_fd)
    finally: os.close(parent_fd)
    with host._connect() as con:
        con.execute("UPDATE imports SET work_name=NULL,work_dev=NULL,work_ino=NULL,work_repo_dev=NULL,work_repo_ino=NULL WHERE import_id=? AND work_name=? AND work_dev=? AND work_ino=?",
                    (row["import_id"], name, info.st_dev, info.st_ino))
        if con.execute("SELECT changes()").fetchone()[0] != 1: raise ForgeError("validation work reservation changed", "integrity")


def _check_branch(branch):
    from git import clean_env
    import limits
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        raise ForgeError("invalid import branch")
    try: encoded = branch.encode("utf-8")
    except UnicodeEncodeError as error: raise ForgeError("invalid import branch") from error
    if len(encoded) > 255: raise ForgeError("invalid import branch")
    try:
        result = subprocess.run(["git", "check-ref-format", "refs/heads/" + branch], env=clean_env(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=limits.GIT_COMMAND_TIMEOUT)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error: raise ForgeError("invalid import branch") from error
    if result.returncode: raise ForgeError("invalid import branch")


def reserve(host, account_id, payload, maximum, *, mode="bundle"):
    import limits
    required = {"name", "branch", "request_id", "bundle_sha256", "size_bytes"}
    if not isinstance(payload, dict) or set(payload) != required: raise ForgeError("invalid import request")
    name = _slug(payload["name"], "repository name"); branch = payload["branch"]
    _check_branch(branch)
    request_id, digest, size = payload["request_id"], payload["bundle_sha256"], payload["size_bytes"]
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id): raise ForgeError("invalid request id")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest): raise ForgeError("invalid bundle digest")
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= maximum: raise ForgeError("invalid bundle size", "limits")
    immutable = (account_id, name, branch, request_id, digest, size)
    with host._connect() as con:
        con.execute("BEGIN IMMEDIATE")
        actor = con.execute("SELECT * FROM accounts WHERE account_id=? AND state='approved'", (account_id,)).fetchone()
        if not actor: raise ForgeError("account unavailable", "policy")
        prior = con.execute("SELECT * FROM imports WHERE request_id=?", (request_id,)).fetchone()
        if prior:
            keys = ("account_id", "name", "branch", "request_id") if mode == "push" else ("account_id", "name", "branch", "request_id", "bundle_sha256", "size_bytes")
            if prior["mode"] != mode or tuple(prior[k] for k in keys) != immutable[:len(keys)]: raise ForgeError("request conflicts", "conflict")
            return dict(prior)
        if con.execute("SELECT 1 FROM repos WHERE request_id=? OR (owner_account_id=? AND name=?)", (request_id, account_id, name)).fetchone() or con.execute("SELECT 1 FROM imports WHERE account_id=? AND name=?", (account_id, name)).fetchone(): raise ForgeError("repository is already reserved", "conflict")
        raw = con.execute("SELECT value FROM host_settings WHERE key='total_repo_limit'").fetchone()
        if not raw or not re.fullmatch(r"[1-9][0-9]{0,6}", raw[0]): raise ForgeError("host capacity setting corrupt", "integrity")
        total = con.execute("SELECT (SELECT count(*) FROM repos)+(SELECT count(*) FROM imports WHERE repo_id IS NULL)").fetchone()[0]
        account_total = con.execute("SELECT (SELECT count(*) FROM repos WHERE owner_account_id=?)+(SELECT count(*) FROM imports WHERE account_id=? AND repo_id IS NULL)", (account_id, account_id)).fetchone()[0]
        if total >= int(raw[0]) or (actor["repo_limit"] is not None and account_total >= actor["repo_limit"]): raise ForgeError("repository capacity exhausted", "limits")
        staged = con.execute("SELECT coalesce(sum(reserved_stage_bytes),0) FROM imports").fetchone()[0]
        reservation = 0 if mode == "push" else size + 3 * limits.IMPORT_EXPANDED_BYTES
        if staged + reservation > limits.IMPORT_STAGING_BYTES:
            raise ForgeError("host import staging budget exhausted", "limits")
        con.execute("INSERT INTO imports(import_id,account_id,name,branch,request_id,bundle_sha256,size_bytes,state,created_at,reserved_stage_bytes,mode) VALUES(?,?,?,?,?,?,?,'awaiting-upload',?,?,?)", (uuid.uuid4().hex, account_id, name, branch, request_id, digest, size, int(time.time()), reservation, mode))
        return dict(con.execute("SELECT * FROM imports WHERE request_id=?", (request_id,)).fetchone())


def get(host, account_id, import_id):
    with host._connect(read_only=True) as con:
        row = con.execute("SELECT * FROM imports WHERE import_id=? AND account_id=?", (import_id, account_id)).fetchone()
    return dict(row) if row else None


def _disk_ok(path, expected):
    import limits
    usage = shutil.disk_usage(path)
    return usage.free >= limits.FREE_FLOOR_BYTES + limits.RESERVE_BYTES + expected


def _consume_upload(host, row, stream, maximum, out=None):
    """Check retries too: published bytes never authorize a different payload."""
    import limits
    digest = hashlib.sha256()
    count = 0
    deadline = time.monotonic() + limits.IMPORT_UPLOAD_SECONDS
    while True:
        if time.monotonic() > deadline:
            raise ForgeError("upload budget exceeded", "limits")
        if out is not None and not _disk_ok(host.root, max(0, row["size_bytes"] - count)):
            raise ForgeError("upload disk budget exceeded", "limits")
        block = stream.read(min(65536, row["size_bytes"] - count + 1))
        if not block:
            break
        count += len(block)
        if count > row["size_bytes"] or count > maximum:
            raise ForgeError("upload exceeds reservation", "limits")
        digest.update(block)
        if out is not None:
            out.write(block)
    if time.monotonic() > deadline:
        raise ForgeError("upload budget exceeded", "limits")
    if count != row["size_bytes"] or digest.hexdigest() != row["bundle_sha256"]:
        raise ForgeError("upload does not match reservation", "conflict")


def upload(host, account_id, import_id, stream, content_length, maximum, authorize=None, *, _locked=False):
    if get(host, account_id, import_id) is None: return None
    with nullcontext() if _locked else _lock(host, import_id):
        row = get(host, account_id, import_id)
        if not row: return None
        if content_length != row["size_bytes"]: raise ForgeError("invalid upload size", "limits")
        path, row = _ensure_stage(host, row)
        row = _retire_upload_temp(host, account_id, row, path)
        target = path / "bundle"
        if target.is_symlink(): raise ForgeError("staged bundle is unsafe", "integrity")
        if target.exists():
            if _bundle_matches(target, row):
                _consume_upload(host, row, stream, maximum)
                if authorize: authorize()
                if row["state"] in {"awaiting-upload", "failed"}:
                    with host._connect() as con:
                        con.execute("UPDATE imports SET state='uploaded',error=NULL WHERE import_id=? AND state IN ('awaiting-upload','failed')", (import_id,))
                return get(host, account_id, import_id)
            raise ForgeError("staged bundle identity changed", "integrity")
        if row["state"] not in {"awaiting-upload", "uploaded", "failed"}: raise ForgeError("import is not uploadable", "conflict")
        if not _disk_ok(host.root, row["size_bytes"]): raise ForgeError("insufficient staging space", "limits")
        temp_name = "upload-" + secrets.token_hex(16); temporary = path / temp_name
        with host._connect() as con:
            con.execute("UPDATE imports SET upload_temp_name=?,upload_temp_dev=NULL,upload_temp_ino=NULL WHERE import_id=? AND upload_temp_name IS NULL", (temp_name, import_id))
            if con.execute("SELECT changes()").fetchone()[0] != 1: raise ForgeError("upload already in progress", "conflict")
        temp_identity = None
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            fstat = os.fstat(fd); temp_identity = (fstat.st_dev, fstat.st_ino)
            with os.fdopen(fd, "wb") as out:
                with host._connect() as con:
                    con.execute("UPDATE imports SET upload_temp_dev=?,upload_temp_ino=? WHERE import_id=? AND upload_temp_name=?",
                                (fstat.st_dev, fstat.st_ino, import_id, temp_name))
                _consume_upload(host, row, stream, maximum, out)
                out.flush(); os.fsync(out.fileno())
            if authorize: authorize()
            current = get(host, account_id, import_id); _stage(host, current)
            dfd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                current_temp = os.stat(temp_name, dir_fd=dfd, follow_symlinks=False)
                if (current_temp.st_dev, current_temp.st_ino) != temp_identity or target.exists():
                    raise ForgeError("upload staging changed", "integrity")
                os.link(temp_name, "bundle", src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
                os.unlink(temp_name, dir_fd=dfd)
                os.fsync(dfd)
            finally: os.close(dfd)
            with host._connect() as con:
                con.execute("UPDATE imports SET state='uploaded',error=NULL,upload_temp_name=NULL,upload_temp_dev=NULL,upload_temp_ino=NULL WHERE import_id=? AND account_id=? AND state IN ('awaiting-upload','uploaded','failed') AND upload_temp_name=?",
                            (import_id, account_id, temp_name))
                if con.execute("SELECT changes()").fetchone()[0] != 1: raise ForgeError("upload reservation changed", "conflict")
        except Exception:
            current = get(host, account_id, import_id)
            if current and current["upload_temp_name"] == temp_name:
                _stage(host, current)
                _retire_upload_temp(host, account_id, current, path)
            raise
        return get(host, account_id, import_id)


def _line(data, pos, deadline):
    if time.monotonic() > deadline: raise ForgeError("bundle validation timed out", "limits")
    if pos > 65536: raise ForgeError("bundle header exceeds limit", "limits")
    end = data.find(b"\n", pos, min(len(data), pos + 1025, 65537))
    if end < 0: raise ForgeError("invalid bundle header", "policy")
    return data[pos:end], end + 1


def preflight(path, row, deadline=None):
    """Bounded v2 bundle/pack parser; no Git process sees bytes before this passes."""
    import limits
    deadline = deadline if deadline is not None else time.monotonic() + limits.IMPORT_VALIDATION_SECONDS
    bundle_file = path.open("rb")
    try:
        actual_size = os.fstat(bundle_file.fileno()).st_size
        if actual_size == 0: raise ForgeError("empty bundle", "policy")
        if actual_size > limits.IMPORT_BUNDLE_BYTES: raise ForgeError("bundle exceeds configured limit", "limits")
        with mmap.mmap(bundle_file.fileno(), 0, access=mmap.ACCESS_READ) as data:
            return _preflight_data(data, row, deadline)
    finally:
        bundle_file.close()


def _preflight_data(data, row, deadline):
    import limits
    if len(data) != row["size_bytes"] or hashlib.sha256(data).hexdigest() != row["bundle_sha256"]: raise ForgeError("bundle identity mismatch", "policy")
    if time.monotonic() > deadline: raise ForgeError("bundle validation timed out", "limits")
    if data[:len(_BUNDLE)] != _BUNDLE: raise ForgeError("unsupported bundle", "policy")
    pos = len(_BUNDLE); refs = []
    while True:
        line, pos = _line(data, pos, deadline)
        if not line: break
        if line.startswith(b"-"): raise ForgeError("prerequisite bundles are unsupported", "policy")
        parts = line.split(b" ", 1)
        if len(parts) != 2 or not re.fullmatch(rb"[0-9a-f]{40}", parts[0]): raise ForgeError("invalid bundle ref", "policy")
        try: ref = parts[1].decode("utf-8")
        except UnicodeDecodeError as error: raise ForgeError("invalid bundle ref", "policy") from error
        refs.append((parts[0].decode(), ref))
        if len(refs) > 1: raise ForgeError("bundle must contain only selected branch", "policy")
    if len(refs) != 1 or refs[0][1] != "refs/heads/" + row["branch"]: raise ForgeError("bundle must contain only selected branch", "policy")
    tip = refs[0][0]
    if data[pos:pos+4] != b"PACK" or pos + 12 > len(data): raise ForgeError("invalid pack", "policy")
    version = int.from_bytes(data[pos+4:pos+8], "big"); objects = int.from_bytes(data[pos+8:pos+12], "big")
    if version not in (2, 3) or not 1 <= objects <= limits.IMPORT_MAX_OBJECTS: raise ForgeError("pack limits exceeded", "limits")
    pack_start = pos; pos += 12; expanded = 0
    for _ in range(objects):
        if time.monotonic() > deadline: raise ForgeError("bundle validation timed out", "limits")
        if pos >= len(data): raise ForgeError("truncated pack", "policy")
        first = data[pos]; pos += 1; kind = (first >> 4) & 7; size = first & 15; shift = 4
        while first & 0x80:
            if pos >= len(data) or shift > 63: raise ForgeError("invalid object header", "policy")
            first = data[pos]; pos += 1; size |= (first & 127) << shift; shift += 7
        if kind not in (1, 2, 3, 4, 6, 7) or size > limits.IMPORT_OBJECT_BYTES:
            raise ForgeError("unsupported or oversized object", "limits")
        if kind == 6:
            if pos >= len(data): raise ForgeError("invalid delta offset", "policy")
            byte = data[pos]; pos += 1
            offset_bytes = 1
            while byte & 0x80:
                if pos >= len(data) or offset_bytes >= 10 or time.monotonic() > deadline: raise ForgeError("invalid delta offset", "policy")
                byte = data[pos]; pos += 1
                offset_bytes += 1
        elif kind == 7:
            if pos + 20 > len(data): raise ForgeError("invalid delta base", "policy")
            pos += 20
        dec = zlib.decompressobj(); produced = 0; delta_head = bytearray(); consumed_end = None
        while not dec.eof:
            if pos >= len(data) - 20 or time.monotonic() > deadline: raise ForgeError("truncated or slow object", "limits")
            chunk = data[pos:min(pos+16384, len(data)-20)]; pos += len(chunk)
            pending = chunk
            while pending:
                previous = pending
                try:
                    out = dec.decompress(pending, min(65536, limits.IMPORT_OBJECT_BYTES - produced + 1))
                except zlib.error as error:
                    raise ForgeError("invalid compressed object", "policy") from error
                pending = dec.unconsumed_tail
                if pending == previous and not out: raise ForgeError("invalid compressed object", "policy")
                if len(out) > limits.IMPORT_OBJECT_BYTES - produced: raise ForgeError("expanded object exceeds limit", "limits")
                produced += len(out)
                if kind in (6,7) and len(delta_head) < 20: delta_head.extend(out[:20-len(delta_head)])
                if dec.unused_data:
                    pos -= len(dec.unused_data); consumed_end = pos; pending = b""; break
                if not pending and len(out) == 0 and not dec.eof: break
            if consumed_end is not None: break
        if not dec.eof: raise ForgeError("invalid compressed object", "policy")
        if produced != size: raise ForgeError("object size mismatch", "policy")
        if kind in (6,7):
            def delta_varint(idx):
                value = 0; shift = 0
                while True:
                    if idx >= len(delta_head) or shift > 63: raise ForgeError("invalid delta header", "policy")
                    b = delta_head[idx]; idx += 1; value |= (b & 127) << shift; shift += 7
                    if not b & 128: return value, idx
            base_size, idx = delta_varint(0)
            target, _idx = delta_varint(idx)
            if base_size > limits.IMPORT_OBJECT_BYTES or target > limits.IMPORT_OBJECT_BYTES: raise ForgeError("delta target exceeds limit", "limits")
            expanded += target
        expanded += produced
        if expanded > limits.IMPORT_EXPANDED_BYTES: raise ForgeError("expanded pack exceeds limit", "limits")
    if pos + 20 != len(data): raise ForgeError("extra or truncated pack data", "policy")
    if time.monotonic() > deadline: raise ForgeError("bundle validation timed out", "limits")
    if hashlib.sha1(data[pack_start:pos]).digest() != data[pos:]: raise ForgeError("pack checksum mismatch", "policy")
    return tip


def _run(args, *, env, timeout, output_limit=1 << 20, input_data=None):
    import selectors
    proc = subprocess.Popen(args, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "out")
    selector.register(proc.stderr, selectors.EVENT_READ, "err")
    if input_data is not None:
        os.set_blocking(proc.stdin.fileno(), False)
        selector.register(proc.stdin, selectors.EVENT_WRITE, "in")
    buffers = {"out": bytearray(), "err": bytearray()}; total = 0
    input_offset = 0
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise TimeoutError
            for key, _ in selector.select(min(remaining, .2)):
                if key.data == "in":
                    if input_offset >= len(input_data):
                        selector.unregister(key.fileobj); key.fileobj.close(); continue
                    try:
                        written = os.write(key.fileobj.fileno(), input_data[input_offset:input_offset + 65536])
                        input_offset += written
                    except BrokenPipeError:
                        selector.unregister(key.fileobj); key.fileobj.close()
                    continue
                block = os.read(key.fileobj.fileno(), 65536)
                if not block:
                    selector.unregister(key.fileobj); continue
                total += len(block)
                if total > output_limit: raise ForgeError("Git validation output exceeded limit", "limits")
                buffers[key.data].extend(block)
        proc.wait(timeout=max(.01, deadline-time.monotonic()))
    except (TimeoutError, subprocess.TimeoutExpired) as error:
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        proc.wait()
        raise ForgeError("Git validation timed out", "limits") from error
    except BaseException:
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        proc.wait()
        raise
    finally:
        selector.close()
        proc.stdout.close(); proc.stderr.close()
        if proc.stdin is not None: proc.stdin.close()
    out = bytes(buffers["out"])
    if proc.returncode: raise ForgeError("Git bundle validation failed", "policy")
    return out


def _validate(host, row, stage):
    import limits
    from git import clean_env
    source = stage / "source.git"
    if source.exists() or source.is_symlink():
        if row["source_dev"] is None or row["source_ino"] is None:
            raise ForgeError("refusing to adopt unpinned validated source", "integrity")
        source_info = source.stat()
        receipt = f"bundle:{row['bundle_sha256']}:"
        if (source_info.st_dev, source_info.st_ino) != (row["source_dev"], row["source_ino"]):
            raise ForgeError("validated source identity changed", "integrity")
        if not isinstance(row["source_identity"], str) or not row["source_identity"].startswith(receipt):
            raise ForgeError("validated source receipt changed", "integrity")
        _assert_private_bare(source, row)
    deadline = time.monotonic() + limits.IMPORT_VALIDATION_SECONDS
    bundle = stage / "bundle"; tip = preflight(bundle, row, deadline)
    def run(args, *, output_limit=1 << 20, input_data=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise ForgeError("Git validation timed out", "limits")
        if not _disk_ok(host.root, limits.IMPORT_EXPANDED_BYTES * 3): raise ForgeError("validation disk budget exceeded", "limits")
        output = _run(args, env=env, timeout=remaining, output_limit=output_limit, input_data=input_data)
        if not _disk_ok(host.root, limits.IMPORT_EXPANDED_BYTES * 3): raise ForgeError("validation disk budget exceeded", "limits")
        return output
    if not _disk_ok(host.root, limits.IMPORT_EXPANDED_BYTES * 3):
        raise ForgeError("insufficient validation space", "limits")
    if row["source_dev"] is not None and not source.exists():
        if not row["work_name"]:
            raise ForgeError("pinned validated source is missing", "integrity")
        recover_dir = stage / row["work_name"]
        recover_bare = recover_dir / "repo.git"
        if recover_dir.is_symlink() or not recover_dir.is_dir(): raise ForgeError("pinned source recovery work is missing", "integrity")
        dir_info, bare_info = recover_dir.stat(), recover_bare.stat()
        if ((dir_info.st_dev, dir_info.st_ino) != (row["work_dev"], row["work_ino"]) or
                (bare_info.st_dev, bare_info.st_ino) != (row["source_dev"], row["source_ino"]) or
                (bare_info.st_dev, bare_info.st_ino) != (row["work_repo_dev"], row["work_repo_ino"])):
            raise ForgeError("pinned source recovery identity changed", "integrity")
        _assert_private_bare(recover_bare, row)
        if source.exists() or source.is_symlink(): raise ForgeError("source destination is occupied", "integrity")
        _stage(host, row)
        os.rename(recover_bare, source)
        parent_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(parent_fd)
        finally: os.close(parent_fd)
    if row["work_name"]:
        if row["work_name"] != "validation-work":
            raise ForgeError("validation staging identity is invalid", "integrity")
        stale = stage / row["work_name"]
        if stale.is_symlink(): raise ForgeError("validation staging identity changed", "integrity")
        if stale.exists():
            info = stale.stat()
            if row["work_dev"] is None:
                if any(stale.iterdir()) or info.st_mode & 0o077: raise ForgeError("unidentified validation work is populated", "integrity")
            elif (info.st_dev, info.st_ino) != (row["work_dev"], row["work_ino"]):
                raise ForgeError("validation staging identity changed", "integrity")
            _stage(host, row)
            shutil.rmtree(stale)
        with host._connect() as con:
            con.execute("UPDATE imports SET work_name=NULL,work_dev=NULL,work_ino=NULL,work_repo_dev=NULL,work_repo_ino=NULL WHERE import_id=?", (row["import_id"],))
    with _owned_work(host, row, stage) as work:
        bare = work / "repo.git"
        env = clean_env(); env.update({"GIT_CONFIG_NOSYSTEM":"1", "GIT_CONFIG_GLOBAL":"/dev/null", "GIT_CONFIG_SYSTEM":"/dev/null", "GIT_TERMINAL_PROMPT":"0", "GIT_NO_REPLACE_OBJECTS":"1"})
        env["GIT_TEMPLATE_DIR"] = "/dev/null"
        env["GIT_ALLOW_PROTOCOL"] = "file"
        run(["git", "init", "--bare", "--template=/dev/null", "--object-format=sha1", str(bare)])
        cfg = bare / "config"
        with cfg.open("ab") as f: f.write(b"\n[core]\n\trepositoryformatversion = 0\n\thooksPath = /dev/null\n\n[uploadpack]\n\tallowAnySHA1InWant = false\n")
        run(["git", "--git-dir", str(bare), "fetch", "--no-write-fetch-head", "--no-recurse-submodules", str(bundle), f"refs/heads/{row['branch']}:refs/heads/{row['branch']}"])
        got = run(["git", "--git-dir", str(bare), "rev-parse", f"refs/heads/{row['branch']}"]).decode().strip()
        if got != tip: raise ForgeError("selected tip mismatch", "policy")
        run(["git", "--git-dir", str(bare), "fsck", "--full", "--strict", "--no-reflogs"], output_limit=limits.STDERR_KEEP_BYTES)
        commits = run(["git", "--git-dir", str(bare), "rev-list", "--all"], output_limit=limits.IMPORT_MAX_COMMITS * 41)
        commit_ids = commits.splitlines()
        if not commit_ids or len(commit_ids) > limits.IMPORT_MAX_COMMITS: raise ForgeError("commit budget exceeded", "limits")
        commit_input = b"\n".join(commit_ids) + b"\n"
        commit_data = run(["git", "--git-dir", str(bare), "cat-file", "--batch"],
                          output_limit=limits.IMPORT_EXPANDED_BYTES + len(commit_ids) * 128,
                          input_data=commit_input)
        trees = set(); cursor = 0
        for commit in commit_ids:
            end = commit_data.find(b"\n", cursor)
            if end < 0: raise ForgeError("invalid commit metadata", "policy")
            header = commit_data[cursor:end].split(); cursor = end + 1
            if len(header) != 3 or header[0] != commit or header[1] != b"commit": raise ForgeError("invalid commit metadata", "policy")
            try: object_size = int(header[2])
            except ValueError as error: raise ForgeError("invalid commit size", "policy") from error
            if object_size > limits.IMPORT_OBJECT_BYTES or cursor + object_size >= len(commit_data): raise ForgeError("commit metadata size invalid", "limits")
            body = commit_data[cursor:cursor + object_size]; cursor += object_size
            if commit_data[cursor:cursor + 1] != b"\n": raise ForgeError("truncated commit metadata", "policy")
            cursor += 1
            headers, separator, _message = body.partition(b"\n\n")
            if not separator: raise ForgeError("invalid commit headers", "policy")
            lines = headers.split(b"\n")
            tree_lines = [line[5:] for line in lines if line.startswith(b"tree ")]
            if (len(tree_lines) != 1 or not re.fullmatch(rb"[0-9a-f]{40}", tree_lines[0]) or
                    sum(line.startswith(b"author ") for line in lines) != 1 or
                    sum(line.startswith(b"committer ") for line in lines) != 1):
                raise ForgeError("invalid commit identity headers", "policy")
            trees.add(tree_lines[0].decode("ascii"))
        if cursor != len(commit_data): raise ForgeError("extra commit metadata", "policy")
        root_trees = set(trees)
        checked_trees = set(); entries = 0
        while trees:
            tree = trees.pop()
            if tree in checked_trees: continue
            checked_trees.add(tree)
            listing = run(["git", "--git-dir", str(bare), "ls-tree", "-z", tree], output_limit=8 << 20)
            rows = listing.split(b"\0")
            if not rows or rows == [b""]:
                if tree not in root_trees: raise ForgeError("empty subtrees are unsupported", "policy")
                continue
            for item in rows:
                if not item: continue
                meta, _name = item.split(b"\t", 1)
                mode, kind, oid = meta.split(b" ", 2)
                entries += 1
                if entries > limits.IMPORT_MAX_TREE_ENTRIES: raise ForgeError("tree entry budget exceeded", "limits")
                if kind == b"tree" and mode == b"040000":
                    if oid == b"4b825dc642cb6eb9a060e54bf8d69288fbee4904":
                        raise ForgeError("empty subtrees are unsupported", "policy")
                    trees.add(oid.decode("ascii"))
                elif mode not in (b"100644", b"100755", b"120000") or kind != b"blob":
                    raise ForgeError("unsupported tree entry", "policy")
        reachable_raw = run(["git", "--git-dir", str(bare), "rev-list", "--objects", "--all", "--no-object-names"], output_limit=limits.IMPORT_MAX_OBJECTS * 42)
        reachable = set(reachable_raw.splitlines())
        objects_raw = run(["git", "--git-dir", str(bare), "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype) %(objectsize)"], output_limit=limits.IMPORT_MAX_OBJECTS * 80)
        objects = set(); actual_expanded = 0
        for line in objects_raw.splitlines():
            fields = line.split()
            if len(fields) != 3 or fields[1] not in {b"commit", b"tree", b"blob"} or not re.fullmatch(rb"[0-9a-f]{40}", fields[0]):
                raise ForgeError("invalid resolved Git object metadata", "policy")
            try: object_size = int(fields[2])
            except ValueError as error: raise ForgeError("invalid resolved object size", "policy") from error
            if object_size > limits.IMPORT_OBJECT_BYTES: raise ForgeError("resolved object exceeds size limit", "limits")
            actual_expanded += object_size
            if actual_expanded > limits.IMPORT_EXPANDED_BYTES: raise ForgeError("resolved object budget exceeded", "limits")
            objects.add(fields[0])
        if len(objects) > limits.IMPORT_MAX_OBJECTS or objects != reachable:
            raise ForgeError("bundle contains unreachable or excessive objects", "policy")
        if source.exists() or source.is_symlink():
            if row["source_dev"] is None or row["source_ino"] is None:
                raise ForgeError("refusing to adopt unpinned validated source", "integrity")
            source_info = source.stat()
            if (source_info.st_dev, source_info.st_ino) != (row["source_dev"], row["source_ino"]):
                raise ForgeError("validated source identity changed", "integrity")
            _assert_private_bare(source, row)
            current = run(["git", "--git-dir", str(source), "rev-parse", f"refs/heads/{row['branch']}"]).decode().strip()
            if current != tip: raise ForgeError("validated source identity changed", "integrity")
            expected_receipt = host.source_receipt(row, tip, source_info)
            if row["source_identity"] != expected_receipt:
                raise ForgeError("validated source receipt changed", "integrity")
            refs = run(["git", "--git-dir", str(source), "for-each-ref", "--format=%(refname)"]).splitlines()
            if refs != [f"refs/heads/{row['branch']}".encode("utf-8")]:
                raise ForgeError("validated source refs changed", "integrity")
        else:
            bare_info = bare.stat()
            receipt = host.source_receipt(row, tip, bare_info)
            _assert_private_bare(bare, row)
            with host._connect() as con:
                con.execute("UPDATE imports SET source_identity=?,source_dev=?,source_ino=?,work_repo_dev=?,work_repo_ino=? WHERE import_id=? AND source_dev IS NULL",
                            (receipt, bare_info.st_dev, bare_info.st_ino, bare_info.st_dev, bare_info.st_ino, row["import_id"]))
                if con.execute("SELECT changes()").fetchone()[0] != 1:
                    raise ForgeError("source receipt reservation changed", "integrity")
            row = get(host, row["account_id"], row["import_id"])
            _stage(host, row)
            if source.exists() or source.is_symlink(): raise ForgeError("source destination is occupied", "integrity")
            os.rename(bare, source)
            source_info = source.stat()
            if (source_info.st_dev, source_info.st_ino) != (row["source_dev"], row["source_ino"]):
                raise ForgeError("published source identity changed", "integrity")
        dfd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    return tip, source


def _ready_stage_bytes(host, row):
    import limits
    stage = _stage(host, row)
    source = stage / "source.git"
    if source.is_symlink() or not source.is_dir(): raise ForgeError("validated source is unavailable", "integrity")
    info = source.stat()
    if (info.st_dev, info.st_ino) != (row["source_dev"], row["source_ino"]):
        raise ForgeError("validated source identity changed", "integrity")
    if not _bundle_matches(stage / "bundle", row): raise ForgeError("validated bundle identity changed", "integrity")
    # Reserve allocated space conservatively, including directory/inode overhead,
    # rather than allowing thousands of small files to evade the staging quota.
    allocated = lambda stat: max(stat.st_size, stat.st_blocks * 512, 4096)
    source_bytes = allocated(info)
    for base, dirs, files in os.walk(source, followlinks=False):
        for name in dirs + files:
            item = Path(base) / name
            if item.is_symlink(): raise ForgeError("validated source contains a symlink", "integrity")
            stat = item.stat(follow_symlinks=False)
            source_bytes += allocated(stat)
            if source_bytes > 2 * limits.IMPORT_EXPANDED_BYTES:
                raise ForgeError("validated source exceeds staging budget", "limits")
    return allocated(stage.stat()) + allocated((stage / "bundle").stat()) + source_bytes


def _compact_ready_reservation(host, row):
    actual = host.retained_import_bytes(row)
    if actual > row["reserved_stage_bytes"]:
        raise ForgeError("retained import source exceeds its reservation", "integrity")
    with host._connect() as con:
        current = con.execute("SELECT state,source_dev,source_ino,reserved_stage_bytes FROM imports WHERE import_id=?", (row["import_id"],)).fetchone()
        if not current or current["state"] != "ready" or (current["source_dev"], current["source_ino"]) != (row["source_dev"], row["source_ino"]):
            raise ForgeError("ready staging reservation changed", "integrity")
        con.execute("UPDATE imports SET reserved_stage_bytes=? WHERE import_id=? AND state='ready'", (actual, row["import_id"]))
    return actual


def complete(host, account_id, import_id, failpoint=None, authorize=None, *, _locked=False):
    if get(host, account_id, import_id) is None: return None
    with nullcontext() if _locked else _lock(host, import_id):
        row = host.prepare_import(get(host, account_id, import_id))
        if not row: return None
        if row["state"] == "ready":
            _compact_ready_reservation(host, row)
            if authorize: authorize()
            return row
        stage, row = _ensure_stage(host, row)
        bundle = stage / "bundle"
        if not _bundle_matches(bundle, row): raise ForgeError("bundle upload required", "policy")
        if row["state"] == "awaiting-upload":
            if authorize: authorize()
            with host._connect() as con:
                con.execute("UPDATE imports SET state='uploaded' WHERE import_id=? AND state='awaiting-upload'", (import_id,))
            row = get(host, account_id, import_id)
        with host._connect() as con:
            con.execute("UPDATE imports SET state='validating',error=NULL WHERE import_id=? AND state!='ready'", (import_id,))
        try:
            tip, source = _validate(host, row, stage)
            if failpoint: failpoint("source_validated")
            source_info = source.stat()
            identity = host.source_receipt(row, tip, source_info)
            current = get(host, account_id, import_id)
            if current["source_identity"] != identity or (current["source_dev"], current["source_ino"]) != (source_info.st_dev, source_info.st_ino):
                raise ForgeError("validated source receipt changed", "integrity")
            with host._connect() as con:
                con.execute("UPDATE imports SET state='provisioning' WHERE import_id=? AND source_identity=? AND source_dev=? AND source_ino=?",
                            (import_id, identity, source_info.st_dev, source_info.st_ino))
            # Conversion occurs through the ordinary atomic repository reservation.
            if authorize: authorize()
            result = host.create_repo(host._account_owner(account_id), row["name"], request_id=row["request_id"],
                                      source=source, branch=row["branch"], _import_id=import_id,
                                      _publish_guard=authorize)
            if failpoint: failpoint("repo_ready_before_import_ready")
            current = get(host, account_id, import_id)
            retained_bytes = host.retained_import_bytes(current)
            if retained_bytes > current["reserved_stage_bytes"]:
                raise ForgeError("retained source exceeds its staging reservation", "integrity")
            if authorize: authorize()
            with host._connect() as con:
                con.execute("UPDATE imports SET state='ready',repo_id=?,error=NULL,reserved_stage_bytes=? WHERE import_id=?",
                            (result["repo_id"], retained_bytes, import_id))
            if failpoint: failpoint("ready")
            return get(host, account_id, import_id)
        except Exception:
            with host._connect() as con: con.execute("UPDATE imports SET state='failed',error='validation-or-provisioning-failed' WHERE import_id=? AND state!='ready'", (import_id,))
            raise


def public(host, row):
    repo = None
    if row["state"] == "ready" and row.get("repo_id"):
        repo = next((item for item in host.public_repos(row["account_id"]) if item["repo_id"] == row["repo_id"]), None)
        if not repo: raise ForgeError("ready import repository is unavailable", "integrity")
    return _public(row, repo)
