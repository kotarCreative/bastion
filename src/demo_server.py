"""Hosted permission-demo HTTP surface for Bastion (spec 013 final form).

The demo serves the Git smart-HTTP endpoints and a small static browser
application on a single port.  Bearer tokens live in page memory only:
the server never receives, stores, or returns a credential outside the
Authorization header of a single request, sets no cookies, and applies
the spec-013 security headers and no-store responses to every route.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path

import limits
from flask import Flask, Response, abort, jsonify, make_response, request
from werkzeug.exceptions import RequestEntityTooLarge

from errors import ForgeError
from http_server import (
    BODY_401,
    BODY_404,
    HttpError,
    RECEIVE_MAX_BODY,
    SERVICES,
    UPLOAD_MAX_BODY,
    authenticate_token,
    decode_body,
    parse_git_authorization,
    parse_service,
    pkt_line,
    reject_legacy_tokens,
    run_service,
    _resolve_service,
)
from identity import resolve_principal
from state import load_state_sections
from identity import CAPABILITIES
from tokens import has_scope
from validation import capability_allowed, longest_covering


ASSET_DIR = Path(__file__).resolve().parent.parent / "templates" / "spa"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

NO_STORE = {"Cache-Control": "no-store"}


class DemoHttpError(HttpError):
    """HTTP error raised by demo routes; handled like any transport HttpError."""


def _forge_root(app: Flask) -> Path:
    return Path(app.config["FORGE_ROOT"])


def _preflight(root: Path) -> None:
    reject_legacy_tokens(root)
    state = load_state_sections(root, ["view_descriptors"])
    if state.get("repository_state") != "ready":
        raise ForgeError("forge initialization is incomplete")
    for descriptor in state.get("view_descriptors", {}).values():
        if descriptor.get("algorithm") != "capabilities-v3":
            raise ForgeError(
                "hosted demo requires descriptor algorithm capabilities-v3"
            )


def _authenticate_git(root: Path, view: str, service: str) -> dict:
    parsed = parse_git_authorization(request.headers.get("Authorization"))
    if parsed is None:
        raise DemoHttpError(
            401,
            BODY_401,
            {"WWW-Authenticate": 'Basic realm="bastion"'},
        )
    username, credential = parsed
    if username is not None and username != view:
        raise DemoHttpError(404, BODY_404)
    record = authenticate_token(root, credential)
    if record is None:
        raise DemoHttpError(
            401,
            BODY_401,
            {"WWW-Authenticate": 'Basic realm="bastion"'},
        )
    if record["view_id"] != view:
        raise DemoHttpError(404, BODY_404)
    required = "fetch" if service == "git-upload-pack" else "push"
    if not has_scope(record, required):
        raise DemoHttpError(403, b"forbidden")
    return record


# API routes resolve the view assignment and descriptor from the WAL
# snapshot, then read only the authenticated principal's catalog
# provenance (targeted row lookups, never a full catalog load).  A
# derived views/view_descriptors row never selects the active policy.


def _active_descriptor(root: Path, view: str) -> tuple[dict, dict]:
    """Return (active_view_config, descriptor) from WAL truth."""
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
    return dict(active), descriptor


def _read_bounded_body(maximum: int) -> bytes:
    """Buffer the request body in chunks without exceeding ``maximum`` bytes."""
    deadline = time.monotonic() + limits.MAX_REQUEST_SECONDS
    chunks: list[bytes] = []
    total = 0
    stream = request.stream
    try:
        while True:
            if time.monotonic() >= deadline:
                raise DemoHttpError(400, b"bad request")
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
    limits.validate()

    app = Flask(__name__)
    app.config["FORGE_ROOT"] = str(root)
    app.config["MAX_CONTENT_LENGTH"] = RECEIVE_MAX_BODY
    app.url_map.strict_slashes = False
    app.config["GIT_SLOTS"] = threading.BoundedSemaphore(limits.MAX_ACTIVE_GIT)
    app.config["RECEIVE_SLOTS"] = threading.BoundedSemaphore(limits.MAX_ACTIVE_RECEIVES)
    app.config["REQUEST_SLOTS"] = threading.BoundedSemaphore(limits.MAX_ACTIVE_REQUESTS)
    app.config["ADMISSION_LOCK"] = threading.Lock()
    app.config["ACTIVE_PER_IP"] = {}
    app.config["STOPPING"] = False

    def admit(address: str, receive: bool) -> int:
        """Per-IP admission; 0 when admitted, else 429/503."""
        receive_acquired = False
        if receive and not app.config["RECEIVE_SLOTS"].acquire(blocking=False):
            return 503
        receive_acquired = receive
        try:
            with app.config["ADMISSION_LOCK"]:
                if app.config["STOPPING"]:
                    if receive_acquired:
                        app.config["RECEIVE_SLOTS"].release()
                    return 503
                limit = limits.MAX_RECEIVES_PER_IP if receive else limits.MAX_ACTIVE_PER_IP
                active = app.config["ACTIVE_PER_IP"].get(address, 0)
                if active >= limit:
                    if receive_acquired:
                        app.config["RECEIVE_SLOTS"].release()
                    return 429
                app.config["ACTIVE_PER_IP"][address] = active + 1
                return 0
        except BaseException:
            if receive:
                app.config["RECEIVE_SLOTS"].release()
            raise

    def release_admission(address: str, receive: bool) -> None:
        if receive:
            app.config["RECEIVE_SLOTS"].release()
        with app.config["ADMISSION_LOCK"]:
            remaining = app.config["ACTIVE_PER_IP"].get(address, 0) - 1
            if remaining > 0:
                app.config["ACTIVE_PER_IP"][address] = remaining
            else:
                app.config["ACTIVE_PER_IP"].pop(address, None)

    app.config["ADMIT"] = admit
    app.config["RELEASE_ADMISSION"] = release_admission
    # Pagination cursors are HMAC'd with an ephemeral server-side key: a
    # process restart invalidates them, and no client ever sees the key.
    app.config["CURSOR_SECRET"] = secrets.token_hex(32)

    @app.after_request
    def _apply_security_headers(response):
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

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

    @app.errorhandler(404)
    def handle_not_found(_error):
        # Unknown routes get one generic response without filesystem or
        # repository path resolution.  API paths keep the stable JSON code.
        if request.path.startswith("/api/"):
            response = make_response(jsonify({"error": "not-found"}), 404)
            response.headers["Cache-Control"] = "no-store"
            return response
        return make_response(b"not found", 404)

    _ready_cache: dict = {"at": 0.0, "status": 500, "body": {"error": "not-ready"}}

    @app.route("/healthz", methods=["GET"])
    def healthz():
        return jsonify({"status": "ok"}), 200

    @app.route("/readyz", methods=["GET"])
    def readyz():
        now = time.time()
        if now - _ready_cache["at"] < 5.0:
            return jsonify(_ready_cache["body"]), _ready_cache["status"]
        try:
            from repository import Repository as _Repository

            meta = load_state_sections(root, [])
            if meta.get("repository_state") != "ready":
                raise ForgeError("forge is not ready")
            _Repository.open(root).load()
        except Exception:
            app.logger.error("readiness check failed")
            _ready_cache.update(
                {"at": now, "status": 503, "body": {"error": "not-ready"}}
            )
            return jsonify({"error": "not-ready"}), 503
        _ready_cache.update({"at": now, "status": 200, "body": {"status": "ok"}})
        return jsonify({"status": "ok"}), 200

    def _resolve_api_view(self_view: str) -> tuple[dict, dict, Path, str]:
        """Resolve the view once; bind all route work to this snapshot."""
        from http_server import _resolve_service as _resolve

        try:
            _repository, manifest, active, materialized = _resolve(
                _forge_root(app), self_view
            )
        except ForgeError:
            abort(404)
        if not materialized.path.is_dir():
            abort(404)
        view_state, descriptor = _active_descriptor(_forge_root(app), self_view)
        try:
            import api_v1 as _api_v1

            tip = _api_v1.projected_tip(materialized.path)
        except ForgeError:
            abort(404)
        return view_state, descriptor, materialized.path, tip

    def _api_auth() -> tuple[dict, str]:
        from http_server import parse_authorization as _parse_bearer

        credential = _parse_bearer(request.headers.get("Authorization"))
        record = authenticate_token(_forge_root(app), credential)
        if record is None:
            response = make_response(jsonify({"error": "auth-required"}), 401)
            response.headers["WWW-Authenticate"] = 'Bearer realm="bastion"'
            response.headers["Cache-Control"] = "no-store"
            abort(response)
        if not has_scope(record, "ui"):
            response = make_response(jsonify({"error": "forbidden"}), 403)
            response.headers["Cache-Control"] = "no-store"
            abort(response)
        return record, record["view_id"]

    def _api_session_envelope(view: str) -> dict:
        import api_v1 as _api

        view_state, descriptor = _active_descriptor(_forge_root(app), view)
        from state import load_principal_provenance

        state = load_principal_provenance(
            _forge_root(app), descriptor["principal_id"]
        )
        team_ids, binding_ids, role_ids = resolve_principal(
            state, descriptor["principal_id"]
        )
        principal = state["principals"][descriptor["principal_id"]]
        return {
            "view_id": view,
            "principal": {"id": principal["id"], "kind": principal["kind"]},
            "team_ids": team_ids,
            "role_ids": role_ids,
            "binding_ids": binding_ids,
            "descriptor": {
                "id": descriptor["id"],
                "generation": view_state["descriptor_generation"],
                "projection_epoch": view_state["projection_epoch"],
                "projected_ref": view_state["projected_ref"],
                "capabilities": _api.capability_map_object(descriptor["capabilities"]),
                "restrictions": _api.capability_map_object(descriptor["restrictions"]),
            },
        }

    @app.route("/api/v1/session", methods=["GET"])
    def api_session():
        _record, view = _api_auth()
        try:
            envelope = _api_session_envelope(view)
        except ForgeError:
            response = make_response(jsonify({"error": "service-unavailable"}), 503)
            response.headers["Cache-Control"] = "no-store"
            return response
        response = make_response(jsonify(envelope), 200)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/v1/tree", methods=["GET"])
    def api_tree():
        import api_v1 as _api

        _record, view = _api_auth()
        if len(request.query_string) > _api.MAX_QUERY_BYTES:
            return jsonify({"error": "bad-request"}), 400
        raw_b64 = request.args.get("path_b64", "")
        cursor_raw = request.args.get("cursor", "")
        try:
            limit = _api.parse_limit(request.args.get("limit"))
            prefix_raw = _api.b64decode_field(raw_b64) if raw_b64 else b""
            normalized = _api.normalize_api_path(prefix_raw)
            prefix_identity = _api.b64encode_raw(prefix_raw)
            _view_state, descriptor, repo_path, tip = _resolve_api_view(view)
        except ForgeError:
            response = make_response(jsonify({"error": "not-found"}), 404)
            response.headers["Cache-Control"] = "no-store"
            return response
        # Hidden and nonexistent prefixes share one 404.
        try:
            children = _api.direct_children(repo_path, descriptor, normalized)
            if normalized:
                allow = descriptor["capabilities"]["read"]
                deny = descriptor["restrictions"]["read"]
                leaf_ok = capability_allowed(allow, deny, os.fsencode(normalized))
                if not leaf_ok and not children and not _api._subtree_has_visible_leaf_streamed(
                    repo_path, descriptor, "HEAD", os.fsencode(normalized + "/") if normalized else b""
                ):
                    response = make_response(jsonify({"error": "not-found"}), 404)
                    response.headers["Cache-Control"] = "no-store"
                    return response
        except ForgeError:
            response = make_response(jsonify({"error": "not-found"}), 404)
            response.headers["Cache-Control"] = "no-store"
            return response
        try:
            offset, _cursor_anchor = (
                _api.parse_cursor(
                    app.config["CURSOR_SECRET"], cursor_raw, "tree", view, tip, prefix_identity
                )
                if cursor_raw
                else (0, "")
            )
        except ForgeError:
            response = make_response(jsonify({"error": "bad-request"}), 400)
            response.headers["Cache-Control"] = "no-store"
            return response
        page = children[offset : offset + limit]
        entries = []
        try:
            blob_oids = [oid for _full, _mode, kind, oid in page if kind == b"blob"]
            sizes = _api.batched_blob_sizes(repo_path, blob_oids)
            for full_b, mode, kind, oid in page:
                size = sizes.get(oid) if kind == b"blob" else None
                entries.append(
                    {
                        "path_b64": _api.b64encode_raw(full_b),
                        "path_display": _api.display(full_b),
                        "mode": mode.decode("ascii"),
                        "kind": kind.decode("ascii"),
                        "size": size,
                    }
                )
        except (ForgeError, ValueError, UnicodeDecodeError):
            response = make_response(jsonify({"error": "service-unavailable"}), 503)
            response.headers["Cache-Control"] = "no-store"
            return response
        next_offset = offset + len(page)
        next_cursor = (
            _api.mint_cursor(app.config["CURSOR_SECRET"], "tree", view, tip, prefix_identity, next_offset)
            if next_offset < len(children)
            else None
        )
        response = make_response(
            jsonify({"projected_tip": tip, "entries": entries, "next_cursor": next_cursor}),
            200,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/v1/history", methods=["GET"])
    def api_history():
        import api_v1 as _api

        _record, view = _api_auth()
        if len(request.query_string) > _api.MAX_QUERY_BYTES:
            return jsonify({"error": "bad-request"}), 400
        cursor_raw = request.args.get("cursor", "")
        try:
            limit = _api.parse_limit(request.args.get("limit"))
            _view_state, _descriptor, repo_path, tip = _resolve_api_view(view)
        except ForgeError:
            response = make_response(jsonify({"error": "service-unavailable"}), 503)
            response.headers["Cache-Control"] = "no-store"
            return response
        try:
            offset, anchor = (
                _api.parse_cursor(app.config["CURSOR_SECRET"], cursor_raw, "history", view, tip, "")
                if cursor_raw
                else (0, "")
            )
        except ForgeError:
            response = make_response(jsonify({"error": "bad-request"}), 400)
            response.headers["Cache-Control"] = "no-store"
            return response
        # Each page visits at most limit+1 commits starting after the
        # integrity-protected anchor; the full history is never enumerated.
        try:
            page, last_oid, has_more = _api.history_page(repo_path, anchor or None, limit)
            commits = [
                _api.commit_envelope(parsed)
                for parsed in _api.batched_commits(repo_path, page)
            ]
        except ForgeError:
            response = make_response(jsonify({"error": "service-unavailable"}), 503)
            response.headers["Cache-Control"] = "no-store"
            return response
        next_cursor = (
            _api.mint_cursor(
                app.config["CURSOR_SECRET"], "history", view, tip, "", offset + len(page), last_oid or ""
            )
            if has_more and last_oid
            else None
        )
        response = make_response(
            jsonify({"projected_tip": tip, "commits": commits, "next_cursor": next_cursor}),
            200,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/v1/decisions", methods=["POST"])
    def api_decisions():
        import api_v1 as _api

        _record, view = _api_auth()
        try:
            raw_body = request.get_data(cache=False)
        except RequestEntityTooLarge:
            return jsonify({"error": "bad-request"}), 400
        if len(raw_body) > _api.MAX_JSON_BODY_BYTES:
            return jsonify({"error": "bad-request"}), 400
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else None
        except (UnicodeDecodeError, ValueError):
            return jsonify({"error": "bad-request"}), 400
        if (
            not isinstance(payload, dict)
            or set(payload) != {"capability", "path_b64"}
            or not isinstance(payload["capability"], str)
            or not isinstance(payload["path_b64"], str)
        ):
            return jsonify({"error": "bad-request"}), 400
        capability = payload["capability"]
        path_b64 = payload["path_b64"]
        if capability not in CAPABILITIES:
            return jsonify({"error": "bad-request"}), 400
        try:
            raw_path = _api.b64decode_field(path_b64)
            normalized = _api.normalize_api_path(raw_path)
            path_bytes = os.fsencode(normalized)
            _view_state, descriptor = _active_descriptor(_forge_root(app), view)
            allow = descriptor["capabilities"].get(capability, [])
            deny = descriptor["restrictions"].get(capability, [])
            allowed = capability_allowed(allow, deny, path_bytes)
            longest_allow = longest_covering(path_bytes, allow)
            longest_deny = longest_covering(path_bytes, deny)
        except ForgeError:
            return jsonify({"error": "bad-request"}), 400
        body = {
            "capability": capability,
            "path_b64": path_b64,
            "allowed": allowed,
            "longest_allow": (
                {"prefix_b64": _api.b64encode_raw(os.fsencode(longest_allow)),
                 "prefix_display": _api.display(os.fsencode(longest_allow))}
                if longest_allow is not None
                else None
            ),
            "longest_deny": (
                {"prefix_b64": _api.b64encode_raw(os.fsencode(longest_deny)),
                 "prefix_display": _api.display(os.fsencode(longest_deny))}
                if longest_deny is not None
                else None
            ),
        }
        response = make_response(jsonify(body), 200)
        response.headers["Cache-Control"] = "no-store"
        return response

    def _asset(name: str, content_type: str) -> Response:
        target = (ASSET_DIR / name).resolve()
        if target.parent != ASSET_DIR.resolve() or not target.is_file():
            abort(404)
        body = target.read_bytes()
        response = Response(body, 200, content_type=content_type)
        return response

    @app.route("/", methods=["GET"])
    def landing():
        return _asset("index.html", "text/html; charset=utf-8")

    @app.route("/assets/app.css", methods=["GET"])
    def app_css():
        return _asset("app.css", "text/css; charset=utf-8")

    @app.route("/assets/app.js", methods=["GET"])
    def app_js():
        return _asset("app.js", "text/javascript; charset=utf-8")

    @app.route("/<view>.git/info/refs", methods=["GET"])
    def git_info_refs(view: str):
        root = _forge_root(app)
        service = parse_service(request.query_string.decode())
        if service is None:
            raise DemoHttpError(404, BODY_404)
        _authenticate_git(root, view, service)

        from repository import Repository as _Repository

        try:
            _repository, manifest, _active, materialized = _resolve_service(root, view)
        except ForgeError:
            raise DemoHttpError(404, BODY_404)
        if service == "git-receive-pack" and (
            manifest.head_seq >= limits.MAX_WAL_SEQUENCE
            or limits.free_below_floor(root)
        ):
            raise DemoHttpError(503, b"demo-read-only")
        repo_path = materialized.path if service == "git-upload-pack" else _git_repo(
            root, view, service
        )[0]
        alternates = None if service == "git-upload-pack" else materialized.path / "objects"
        throttled = app.config["ADMIT"](request.remote_addr or "", service == "git-receive-pack")
        if throttled:
            raise DemoHttpError(
                throttled, b"throttled" if throttled == 429 else b"server saturated"
            )
        try:
            if not app.config["GIT_SLOTS"].acquire(blocking=False):
                raise DemoHttpError(503, b"server saturated")
            try:
                protocol = request.headers.get("Git-Protocol")
                completed = run_service(
                    repo_path,
                    service,
                    True,
                    b"",
                    protocol,
                    None,
                    alternates,
                )
            finally:
                app.config["GIT_SLOTS"].release()
        finally:
            app.config["RELEASE_ADMISSION"](request.remote_addr or "", service == "git-receive-pack")
        if completed.returncode:
            app.logger.error("git advertisement failed")
            raise DemoHttpError(500, b"git advertisement failed")
        body = pkt_line(f"# service={service}\n".encode()) + b"0000" + completed.stdout
        return make_response(
            body,
            200,
            {"Content-Type": f"application/x-{service}-advertisement", **NO_STORE},
        )

    @app.route("/<view>.git/<service>", methods=["POST"])
    def git_service(view: str, service: str):
        root = _forge_root(app)
        if service not in SERVICES:
            raise DemoHttpError(404, BODY_404)
        _authenticate_git(root, view, service)

        receive = service == "git-receive-pack"
        if receive and limits.free_below_floor(root):
            raise DemoHttpError(503, b"demo-read-only")
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

        throttled = app.config["ADMIT"](request.remote_addr or "", receive)
        if throttled:
            raise DemoHttpError(
                throttled, b"throttled" if throttled == 429 else b"server saturated"
            )
        try:
            if not app.config["GIT_SLOTS"].acquire(blocking=False):
                raise DemoHttpError(503, b"server saturated")
            try:
                completed = run_service(
                    repo_path,
                    service,
                    False,
                    payload,
                    protocol,
                    extra_env,
                    alternates,
                )
            finally:
                app.config["GIT_SLOTS"].release()
        finally:
            app.config["RELEASE_ADMISSION"](request.remote_addr or "", receive)
        if completed.returncode:
            app.logger.error("git service failed")
            raise DemoHttpError(500, b"git service failed")
        return make_response(
            completed.stdout,
            200,
            {"Content-Type": f"application/x-{service}-result", **NO_STORE},
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
