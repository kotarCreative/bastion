"""Hosted permission-demo HTTP surface for Bastion.

The demo serves both a browser UI and the Git smart-HTTP endpoints on a
single port.  It reuses the token scheme and Git helpers from
``http_server`` and is intentionally scoped to invite-only, non-sensitive
demonstrations.
"""

from __future__ import annotations

import base64
import os
import secrets
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qsl

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge

from errors import ForgeError
from git import clean_env, git as git_command, ref as git_ref, tree_entries
from http_server import (
    BODY_401,
    BODY_404,
    CACHE_HEADERS,
    HttpError,
    RECEIVE_MAX_BODY,
    SERVICES,
    UPLOAD_MAX_BODY,
    decode_body,
    load_tokens,
    parse_authorization,
    parse_service,
    pkt_line,
    run_service,
    _resolve_service,
)
from identity import resolve_principal
from state import load_state, load_state_sections
from identity import CAPABILITIES
from validation import capability_allowed, longest_covering, normalize_prefix


class DemoHttpError(HttpError):
    """HTTP error raised by demo routes; handled like any transport HttpError."""


def _forge_root(app: Flask) -> Path:
    return Path(app.config["FORGE_ROOT"])


def _preflight(root: Path) -> None:
    state = load_state_sections(root, ["view_descriptors"])
    if state.get("repository_state") != "ready":
        raise ForgeError("forge initialization is incomplete")
    for descriptor in state.get("view_descriptors", {}).values():
        if descriptor.get("algorithm") != "capabilities-v3":
            raise ForgeError(
                "hosted demo requires descriptor algorithm capabilities-v3"
            )


def _authenticate_git(root: Path, view: str) -> None:
    credential = parse_authorization(request.headers.get("Authorization"))
    if credential is None:
        raise DemoHttpError(
            401,
            BODY_401,
            {"WWW-Authenticate": 'Basic realm="bastion"'},
        )
    tokens = load_tokens(root)
    if credential not in tokens:
        raise DemoHttpError(
            401,
            BODY_401,
            {"WWW-Authenticate": 'Basic realm="bastion"'},
        )
    if tokens[credential] != view:
        raise DemoHttpError(404, BODY_404)


def _browser_view(root: Path) -> str:
    token = session.get("token")
    if not token:
        return ""
    tokens = load_tokens(root)
    if token not in tokens:
        session.pop("token", None)
        return ""
    return tokens[token]


def _require_browser_view(root: Path) -> str:
    view = _browser_view(root)
    if not view:
        abort(redirect(url_for("landing")))
    return view


# Browser session and decision routes resolve the view assignment and
# descriptor from the WAL snapshot, then read only the authenticated
# principal's catalog provenance. A derived views/view_descriptors row never
# selects the active policy. These routes never load unrelated derived
# history (transactions/changes) or job rows.
_BROWSER_CATALOG_SECTIONS = [
    "principals",
    "teams",
    "roles",
    "role_bindings",
]


def _active_descriptor(root: Path, view: str) -> tuple[dict, dict, dict]:
    """Return (active_view_config, descriptor, state) from WAL truth."""
    from repository import Repository

    try:
        repository = Repository.open(root)
        _manifest, _version, replay = repository.load()
    except ForgeError:
        abort(404)
    active = replay.active_views.get(view)
    if active is None:
        abort(404)
    try:
        descriptor = repository._descriptor(active["descriptor_id"])
    except ForgeError:
        abort(404)
    if descriptor.get("principal_id") != active.get("principal_id"):
        abort(404)
    try:
        state = load_state_sections(root, _BROWSER_CATALOG_SECTIONS)
    except ForgeError:
        abort(404)
    view_state = dict(active)
    return view_state, descriptor, state


def _materialized_path(root: Path, view: str) -> Path:
    try:
        _repository, _manifest, _active, materialized = _resolve_service(root, view)
    except ForgeError as error:
        abort(404)
    return materialized.path


BLOB_DISPLAY_LIMIT = 512 * 1024
BLOB_STREAM_LIMIT = 8
BLOB_STREAM_TIMEOUT = 300.0

_blob_stream_slots = threading.BoundedSemaphore(BLOB_STREAM_LIMIT)


def _display(raw: bytes) -> str:
    """Lossy presentation text for arbitrary Git path bytes."""
    return raw.decode("utf-8", "replace")


def _path_b64(raw: bytes) -> str:
    """URL-safe base64 identity for arbitrary Git path bytes."""
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _path_argument() -> str:
    """Byte-preserving ``path`` parameter decoded with surrogate escapes.

    ``path_b64`` carries the authoritative raw path bytes; a plain ``path``
    query parameter remains supported for legible URLs.  Flask's decoded
    ``request.args`` is intentionally avoided for ``path`` because it
    replaces malformed UTF-8 instead of preserving it.
    """
    encoded = request.args.get("path_b64", "")
    if encoded:
        try:
            raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        except (UnicodeEncodeError, ValueError):
            abort(404)
        return raw.decode("utf-8", "surrogateescape")
    for key, value in parse_qsl(
        request.query_string.decode("latin-1"),
        keep_blank_values=True,
        encoding="latin-1",
    ):
        if key == "path":
            return value.encode("latin-1").decode("utf-8", "surrogateescape")
    return ""


def _requested_path() -> str:
    """Normalized request path; "" addresses the root.  Invalid input is 404."""
    try:
        normalized = normalize_prefix(_path_argument())
    except ForgeError:
        abort(404)
    if "\x00" in normalized:
        abort(404)
    return normalized


def _leaf_allowed(descriptor: dict, normalized: str) -> bool:
    return capability_allowed(
        descriptor["capabilities"]["read"],
        descriptor["restrictions"]["read"],
        os.fsencode(normalized),
    )


def _visible_leaves(repo_path: Path, descriptor: dict, normalized: str) -> list[bytes]:
    """Authorized leaf paths beneath the tree prefix, "" covering the root."""
    allow = descriptor["capabilities"]["read"]
    deny = descriptor["restrictions"]["read"]
    prefix_b = os.fsencode(f"{normalized}/") if normalized else b""
    try:
        leaves = tree_entries(repo_path, "HEAD", recursive=True)
    except ForgeError:
        return []
    return [
        leaf_b
        for leaf_b in leaves
        if leaf_b.startswith(prefix_b) and capability_allowed(allow, deny, leaf_b)
    ]


def _crumbs(normalized: str) -> list[dict]:
    """Breadcrumb links that preserve raw path bytes."""
    crumbs: list[dict] = []
    accumulated = ""
    for part in normalized.split("/"):
        accumulated = f"{accumulated}/{part}" if accumulated else part
        raw = os.fsencode(accumulated)
        crumbs.append({"name": _display(raw), "path_b64": _path_b64(raw)})
    return crumbs


def _blob_metadata(repo_path: Path, path: str) -> tuple[bytes, bytes, str] | None:
    """Return (mode, kind, oid) for a leaf path, or None when absent."""
    parent, _, leaf = path.rpartition("/")
    treeish = f"HEAD:{parent}" if parent else "HEAD"
    try:
        entries = tree_entries(repo_path, treeish, recursive=False)
    except ForgeError:
        return None
    return entries.get(os.fsencode(leaf))


def _spawn_cat_file(repo_path: Path, kind: bytes, oid: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["git", "--git-dir", os.fsencode(repo_path), "cat-file", kind, oid],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=clean_env(),
    )


def _reap(process: subprocess.Popen, timeout: float = 5.0) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _arm_watchdog(process: subprocess.Popen) -> threading.Timer:
    timer = threading.Timer(BLOB_STREAM_TIMEOUT, process.kill)
    timer.daemon = True
    timer.start()
    return timer


def _blob_head(repo_path: Path, kind: bytes, oid: str, limit: int = 8192) -> bytes:
    """Return the first ``limit`` bytes of a blob without reading it whole."""
    process = _spawn_cat_file(repo_path, kind, oid)
    timer = _arm_watchdog(process)
    try:
        return process.stdout.read(limit)
    finally:
        timer.cancel()
        _reap(process)


def _stream_blob(repo_path: Path, kind: bytes, oid: str):
    """Yield chunks of a blob under concurrency and duration limits."""
    if not _blob_stream_slots.acquire(blocking=False):
        raise DemoHttpError(503, b"too many concurrent blob downloads")
    process = None
    timer = None
    try:
        process = _spawn_cat_file(repo_path, kind, oid)
        timer = _arm_watchdog(process)
        while True:
            block = process.stdout.read(65536)
            if not block:
                break
            yield block
    finally:
        if timer is not None:
            timer.cancel()
        if process is not None:
            _reap(process)
        _blob_stream_slots.release()


def _read_bounded_body(maximum: int) -> bytes:
    """Buffer the request body in chunks without exceeding ``maximum`` bytes."""
    chunks: list[bytes] = []
    total = 0
    stream = request.stream
    try:
        while True:
            block = stream.read(min(65536, maximum - total + 1))
            if not block:
                return b"".join(chunks)
            total += len(block)
            chunks.append(block)
            if total > maximum:
                raise DemoHttpError(413, b"content too large")
    except RequestEntityTooLarge as error:
        raise DemoHttpError(413, b"content too large") from error


def create_app(root: Path) -> Flask:
    _preflight(root)

    template_dir = Path(__file__).resolve().parent.parent / "templates" / "demo"

    app = Flask(
        __name__,
        template_folder=str(template_dir),
    )
    app.config["FORGE_ROOT"] = str(root)
    app.secret_key = os.environ.get(
        "BASTION_DEMO_SECRET",
        secrets.token_hex(32),
    )
    app.config["MAX_CONTENT_LENGTH"] = RECEIVE_MAX_BODY
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.url_map.strict_slashes = False

    @app.errorhandler(HttpError)
    def handle_http_error(error: HttpError):
        response = make_response(error.body, error.status)
        response.content_type = "text/plain; charset=utf-8"
        for key, value in error.headers.items():
            response.headers[key] = value
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def handle_too_large(_error: RequestEntityTooLarge):
        response = make_response(b"content too large", 413)
        response.content_type = "text/plain; charset=utf-8"
        return response

    @app.route("/", methods=["GET"])
    def landing():
        root = _forge_root(app)
        if _browser_view(root):
            return redirect(url_for("whoami"))
        return render_template("landing.html")

    @app.route("/login", methods=["POST"])
    def login():
        root = _forge_root(app)
        token = request.form.get("token", "").strip()
        tokens = load_tokens(root)
        if token not in tokens:
            flash("Invalid token", "error")
            return redirect(url_for("landing"))
        session["token"] = token
        return redirect(url_for("whoami"))

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        session.pop("token", None)
        return redirect(url_for("landing"))

    @app.route("/whoami", methods=["GET"])
    def whoami():
        root = _forge_root(app)
        view = _require_browser_view(root)
        view_state, descriptor, state = _active_descriptor(root, view)
        team_ids, binding_ids, role_ids = resolve_principal(state, descriptor["principal_id"])
        principal = state["principals"][descriptor["principal_id"]]
        roles = [state["roles"][rid] for rid in role_ids]

        if request.args.get("format") == "json":
            return jsonify(
                {
                    "view": view,
                    "principal": {
                        "id": principal["id"],
                        "kind": principal["kind"],
                        "emails": principal.get("emails", []),
                    },
                    "teams": team_ids,
                    "bindings": binding_ids,
                    "roles": roles,
                    "descriptor_id": descriptor["id"],
                    "descriptor_generation": view_state["descriptor_generation"],
                    "projection_epoch": view_state["projection_epoch"],
                    "capabilities": descriptor["capabilities"],
                    "restrictions": descriptor["restrictions"],
                }
            )

        return render_template(
            "whoami.html",
            view=view,
            principal=principal,
            teams=team_ids,
            bindings=binding_ids,
            roles=roles,
            descriptor=descriptor,
            descriptor_generation=view_state["descriptor_generation"],
            projection_epoch=view_state["projection_epoch"],
        )

    @app.route("/evaluate", methods=["GET", "POST"])
    def evaluate():
        root = _forge_root(app)
        view = _require_browser_view(root)
        view_state, descriptor, _state = _active_descriptor(root, view)

        path = ""
        capability = "read"
        result = None

        if request.method == "POST":
            path = request.form.get("path", "").strip().lstrip("/")
            capability = request.form.get("capability", "read").strip()
            if capability not in CAPABILITIES:
                flash("Invalid capability", "error")
            else:
                try:
                    normalized = normalize_prefix(path)
                    path_bytes = os.fsencode(normalized)
                    allow = descriptor["capabilities"].get(capability, [])
                    deny = descriptor["restrictions"].get(capability, [])
                    allowed = capability_allowed(allow, deny, path_bytes)
                    result = {
                        "path": normalized,
                        "capability": capability,
                        "allowed": allowed,
                        "allow": longest_covering(path_bytes, allow),
                        "deny": longest_covering(path_bytes, deny),
                    }
                except ForgeError as error:
                    flash(str(error), "error")

        return render_template(
            "evaluate.html",
            view=view,
            capabilities=CAPABILITIES,
            path=path,
            capability=capability,
            result=result,
        )

    @app.route("/browse", methods=["GET"])
    def browse():
        root = _forge_root(app)
        view = _require_browser_view(root)
        view_state, descriptor, _state = _active_descriptor(root, view)
        repo_path = _materialized_path(root, view)

        path = _requested_path()
        allow = descriptor["capabilities"]["read"]
        deny = descriptor["restrictions"]["read"]
        prefix_b = os.fsencode(f"{path}/") if path else b""
        try:
            children = tree_entries(
                repo_path, f"HEAD:{path}" if path else "HEAD", recursive=False
            )
        except ForgeError:
            abort(404)
        visible = _visible_leaves(repo_path, descriptor, path)
        if path and not visible:
            abort(404)
        populated: set[bytes] = set()
        for leaf_b in visible:
            rest = leaf_b
            while True:
                rest, _, _ = rest.rpartition(b"/")
                if not rest:
                    break
                populated.add(rest)

        visible_entries = []
        for entry_path_b, (mode, kind, oid) in sorted(children.items()):
            full_path_b = prefix_b + entry_path_b
            if kind == b"tree":
                if full_path_b not in populated:
                    continue
            elif not capability_allowed(allow, deny, full_path_b):
                continue
            visible_entries.append(
                {
                    "name": _display(entry_path_b),
                    "path": _display(full_path_b),
                    "path_b64": _path_b64(full_path_b),
                    "kind": kind.decode() if isinstance(kind, bytes) else kind,
                    "oid": oid,
                }
            )

        raw_path = os.fsencode(path)
        if request.args.get("format") == "json":
            return jsonify(
                {
                    "view": view,
                    "path": _display(raw_path),
                    "path_b64": _path_b64(raw_path),
                    "entries": visible_entries,
                }
            )

        return render_template(
            "browse.html",
            view=view,
            path=_display(raw_path),
            crumbs=_crumbs(path),
            entries=visible_entries,
        )

    @app.route("/blob", methods=["GET"])
    def blob():
        root = _forge_root(app)
        view = _require_browser_view(root)
        view_state, descriptor, _state = _active_descriptor(root, view)
        repo_path = _materialized_path(root, view)

        normalized = _requested_path()
        if not normalized:
            abort(404)
        entry = _blob_metadata(repo_path, normalized)
        if entry is None or entry[1] in (b"tree", b"commit"):
            if not _visible_leaves(repo_path, descriptor, normalized):
                abort(404)
            return redirect(
                url_for("browse", path_b64=_path_b64(os.fsencode(normalized)))
            )
        if not _leaf_allowed(descriptor, normalized):
            abort(404)
        mode, kind, oid = entry
        raw_path = os.fsencode(normalized)

        size = int(git_command(repo_path, "cat-file", "-s", oid).stdout.strip())

        if raw := request.args.get("raw"):
            generator = _stream_blob(repo_path, kind, oid)
            try:
                first_chunk = next(generator)
            except StopIteration:
                first_chunk = b""
            binary = mode != b"120000" and b"\0" in first_chunk
            content_type = (
                "text/plain; charset=utf-8"
                if mode == b"120000" or not binary
                else "application/octet-stream"
            )

            def generate():
                yield first_chunk
                yield from generator

            return Response(generate(), 200, {"Content-Type": content_type, **CACHE_HEADERS})

        sample = _blob_head(repo_path, kind, oid)
        binary = mode != b"120000" and b"\0" in sample
        too_large = not binary and size > BLOB_DISPLAY_LIMIT

        if request.args.get("format") == "json":
            if binary or too_large:
                content: bytes | None = None
            else:
                content = git_command(repo_path, "cat-file", kind, oid).stdout
            return jsonify(
                {
                    "view": view,
                    "path": _display(raw_path),
                    "path_b64": _path_b64(raw_path),
                    "kind": kind.decode(),
                    "mode": mode.decode(),
                    "oid": oid,
                    "size": size,
                    "binary": binary,
                    "too_large": too_large,
                    "content": None if content is None else content.decode("utf-8", "replace"),
                }
            )

        if binary or too_large:
            text = None
        else:
            text = git_command(repo_path, "cat-file", kind, oid).stdout.decode("utf-8", "replace")

        return render_template(
            "blob.html",
            view=view,
            path=_display(raw_path),
            path_b64=_path_b64(raw_path),
            crumbs=_crumbs(normalized),
            symlink=mode == b"120000",
            binary=binary,
            too_large=too_large,
            size=size,
            text=text,
        )

    @app.route("/history", methods=["GET"])
    def history():
        root = _forge_root(app)
        view = _require_browser_view(root)
        view_state, descriptor, _state = _active_descriptor(root, view)
        repo_path = _materialized_path(root, view)

        path = _requested_path()
        if path and not (
            _leaf_allowed(descriptor, path)
            or _visible_leaves(repo_path, descriptor, path)
        ):
            abort(404)
        raw_path = os.fsencode(path)

        ref = git_ref(repo_path, "HEAD")
        commits: list[dict] = []
        if ref and ref != "0" * 40:
            arguments = [
                "--literal-pathspecs",
                "log",
                "-n",
                "50",
                "--format=%H%x00%s%x00%at",
                "HEAD",
            ]
            if path:
                arguments.extend(("--", path))
            raw = git_command(repo_path, *arguments).stdout.decode("utf-8", "replace")
            for line in raw.splitlines():
                if "\0" not in line:
                    continue
                oid, message, timestamp = line.split("\0", 2)
                commits.append(
                    {
                        "oid": oid,
                        "message": message,
                        "timestamp": int(timestamp),
                    }
                )

        if request.args.get("format") == "json":
            return jsonify(
                {
                    "view": view,
                    "path": _display(raw_path),
                    "path_b64": _path_b64(raw_path),
                    "commits": commits,
                }
            )

        return render_template(
            "history.html",
            view=view,
            path=_display(raw_path),
            path_b64=_path_b64(raw_path),
            commits=commits,
        )

    @app.route("/<view>.git/info/refs", methods=["GET"])
    def git_info_refs(view: str):
        root = _forge_root(app)
        _authenticate_git(root, view)
        service = parse_service(request.query_string.decode())
        if service is None:
            raise DemoHttpError(404, BODY_404)

        repo_path, alternates = _git_repo(root, view, service)
        protocol = request.headers.get("Git-Protocol")
        completed = run_service(repo_path, service, True, b"", protocol, None, alternates)
        if completed.returncode:
            app.logger.error(
                "git advertisement failed for view %s: %s",
                view,
                completed.stderr[:4096].decode(errors="replace"),
            )
            raise DemoHttpError(500, b"git advertisement failed")
        body = pkt_line(f"# service={service}\n".encode()) + b"0000" + completed.stdout
        return make_response(
            body,
            200,
            {"Content-Type": f"application/x-{service}-advertisement", **CACHE_HEADERS},
        )

    @app.route("/<view>.git/<service>", methods=["POST"])
    def git_service(view: str, service: str):
        root = _forge_root(app)
        _authenticate_git(root, view)
        if service not in SERVICES:
            raise DemoHttpError(404, BODY_404)

        repo_path, alternates = _git_repo(root, view, service)
        protocol = request.headers.get("Git-Protocol")
        maximum = UPLOAD_MAX_BODY if service == "git-upload-pack" else RECEIVE_MAX_BODY
        raw = _read_bounded_body(maximum)
        payload = decode_body(raw, request.headers.get("Content-Encoding"), maximum)

        try:
            _repository, _manifest, active, _materialized = _resolve_service(root, view)
        except ForgeError:
            raise DemoHttpError(404, BODY_404)

        extra_env = {
            "BASTION_VIEW": view,
            "BASTION_TRANSPORT": "http",
            "BASTION_REMOTE_ADDR": request.remote_addr or "",
        }
        if service == "git-receive-pack":
            extra_env["BASTION_PRINCIPAL_ID"] = active["principal_id"]
        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key and service == "git-receive-pack":
            if len(idempotency_key) > 256 or not idempotency_key.strip():
                raise DemoHttpError(400, b"invalid Idempotency-Key")
            extra_env["BASTION_IDEMPOTENCY_KEY"] = idempotency_key

        completed = run_service(repo_path, service, False, payload, protocol, extra_env, alternates)
        if completed.returncode:
            app.logger.error(
                "git %s failed for view %s: %s",
                service,
                view,
                completed.stderr[:4096].decode(errors="replace"),
            )
            raise DemoHttpError(500, b"git service failed")
        return make_response(
            completed.stdout,
            200,
            {"Content-Type": f"application/x-{service}-result"},
        )

    return app


def _git_repo(root: Path, view: str, service: str) -> tuple[Path, Path | None]:
    try:
        _repository, manifest, _active, materialized = _resolve_service(root, view)
    except ForgeError:
        raise DemoHttpError(404, BODY_404)
    if service == "git-upload-pack":
        return materialized.path, None
    from materialization import ensure_intake_materialized

    return ensure_intake_materialized(root, materialized, manifest), materialized.path / "objects"


def make_server(root: Path, host: str = "127.0.0.1", port: int = 8000):
    app = create_app(root)
    from werkzeug.serving import make_server as _make_server

    return _make_server(host, port, app, threaded=True)


def serve(root: Path, host: str, port: int) -> None:
    app = create_app(root)
    print(
        f"bastion: serving demo on http://{host}:{port}",
        file=sys.stderr,
        flush=True,
    )
    app.run(host=host, port=port, threaded=True)
