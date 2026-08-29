"""Hosted permission-demo HTTP surface for Bastion.

The demo serves both a browser UI and the Git smart-HTTP endpoints on a
single port.  It reuses the token scheme and Git helpers from
``http_server`` and is intentionally scoped to invite-only, non-sensitive
demonstrations.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

from flask import (
    Flask,
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

from errors import ForgeError
from git import output as git_output, ref as git_ref, tree_entries
from http_server import (
    BODY_401,
    BODY_404,
    CACHE_HEADERS,
    SERVICES,
    UPLOAD_MAX_BODY,
    RECEIVE_MAX_BODY,
    decode_body,
    load_tokens,
    parse_authorization,
    parse_service,
    pkt_line,
    run_service,
    _resolve_service,
)
from identity import descriptor_for_view, resolve_principal
from state import load_state
from identity import CAPABILITIES
from validation import capability_allowed, longest_covering, normalize_prefix


class DemoHttpError(Exception):
    def __init__(
        self,
        status: int,
        body: bytes,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(f"{status}: {body.decode(errors='replace')}")
        self.status = status
        self.body = body
        self.headers = headers or {}


def _forge_root(app: Flask) -> Path:
    return Path(app.config["FORGE_ROOT"])


def _preflight(root: Path) -> None:
    state = load_state(root)
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


def _active_descriptor(root: Path, view: str) -> tuple[dict, dict, dict]:
    """Return (active_view_config, descriptor, state)."""
    state = load_state(root)
    descriptor = descriptor_for_view(state, view)
    view_state = state["views"][view]
    return view_state, descriptor, state


def _materialized_path(root: Path, view: str) -> Path:
    try:
        _repository, _manifest, _active, materialized = _resolve_service(root, view)
    except ForgeError as error:
        abort(404)
    return materialized.path


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
    app.url_map.strict_slashes = False

    @app.errorhandler(DemoHttpError)
    def handle_demo_http_error(error: DemoHttpError):
        response = make_response(error.body, error.status)
        response.content_type = "text/plain; charset=utf-8"
        for key, value in error.headers.items():
            response.headers[key] = value
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

        path = request.args.get("path", "").strip("/")
        treeish = f"HEAD:{path}" if path else "HEAD"
        try:
            entries = tree_entries(repo_path, treeish)
        except ForgeError:
            abort(404)

        visible_entries = []
        for entry_path_b, (mode, kind, oid) in sorted(entries.items()):
            full_path = f"{path}/{entry_path_b.decode()}" if path else entry_path_b.decode()
            if not capability_allowed(
                descriptor["capabilities"]["read"],
                descriptor["restrictions"]["read"],
                os.fsencode(full_path),
            ):
                continue
            visible_entries.append(
                {
                    "name": entry_path_b.decode(),
                    "path": full_path,
                    "kind": kind.decode() if isinstance(kind, bytes) else kind,
                    "oid": oid,
                }
            )

        return render_template(
            "browse.html",
            view=view,
            path=path,
            entries=visible_entries,
        )

    @app.route("/history", methods=["GET"])
    def history():
        root = _forge_root(app)
        view = _require_browser_view(root)
        repo_path = _materialized_path(root, view)

        ref = git_ref(repo_path, "HEAD")
        if not ref or ref == "0" * 40:
            commits: list[dict] = []
        else:
            raw = git_output(
                repo_path,
                "log",
                "-n",
                "50",
                "--format=%H%x00%s%x00%at",
                "HEAD",
            )
            commits = []
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

        return render_template("history.html", view=view, commits=commits)

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
            raise DemoHttpError(500, completed.stderr[:4096])
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
        raw = request.get_data()
        payload = decode_body(
            raw,
            request.headers.get("Content-Encoding"),
            UPLOAD_MAX_BODY if service == "git-upload-pack" else RECEIVE_MAX_BODY,
        )

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
            raise DemoHttpError(500, completed.stderr[:4096])
        return make_response(
            completed.stdout,
            200,
            {"Content-Type": f"application/x-{service}-result"},
        )

    return app


def _git_repo(root: Path, view: str, service: str) -> tuple[Path, Path | None]:
    try:
        _repository, _manifest, _active, materialized = _resolve_service(root, view)
    except ForgeError:
        raise DemoHttpError(404, BODY_404)
    if service == "git-upload-pack":
        return materialized.path, None
    from materialization import ensure_intake_materialized

    return ensure_intake_materialized(root, materialized), materialized.path / "objects"


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
