"""Repository-scoped browser/API and Git engine used by the host dispatcher.

The repository app serves Git smart-HTTP endpoints and a static browser
application on a single port.  Bearer tokens live in page memory only:
the server never receives, stores, or returns a credential outside the
Authorization header of a single request, sets no cookies, and applies
the spec-013 security headers and no-store responses to every route.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

import limits
from flask import Flask, Response, abort, g, jsonify, make_response, request
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
from identity import resolve_principal, SUPPORTED_DESCRIPTOR_ALGORITHMS
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


class RepositoryHttpError(HttpError):
    """Repository HTTP error; handled like any transport HttpError."""


def _security_headers(clerk_origin: str | None = None) -> dict[str, str]:
    if clerk_origin is None:
        return dict(SECURITY_HEADERS)
    return {
        **SECURITY_HEADERS,
        "Content-Security-Policy": (
            "default-src 'none'; "
            f"script-src 'self' {clerk_origin} https://*.protect.clerk.com "
            "https://challenges.cloudflare.com; "
            "style-src 'self' 'unsafe-inline'; "
            f"connect-src 'self' {clerk_origin} https://*.protect.clerk.com:*; "
            "img-src 'self' data: https://img.clerk.com; "
            "frame-src https://challenges.cloudflare.com https://*.protect.clerk.com; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        ),
    }


def _forge_root(app: Flask) -> Path:
    return Path(app.config["FORGE_ROOT"])


def _preflight(root: Path) -> None:
    reject_legacy_tokens(root)
    state = load_state_sections(root, ["view_descriptors"])
    if state.get("repository_state") != "ready":
        raise ForgeError("forge initialization is incomplete")
    from wal import validate_descriptor
    for descriptor in state.get("view_descriptors", {}).values():
        algorithm = descriptor.get("algorithm")
        if algorithm not in SUPPORTED_DESCRIPTOR_ALGORITHMS:
            raise ForgeError(f"unsupported descriptor algorithm: {algorithm!r}")
        validate_descriptor(descriptor)


def _authenticate_git(root: Path, view: str, service: str) -> dict:
    parsed = parse_git_authorization(request.headers.get("Authorization"))
    if parsed is None:
        raise RepositoryHttpError(
            401,
            BODY_401,
            {"WWW-Authenticate": 'Basic realm="bastion"'},
        )
    username, credential = parsed
    if username is not None and username != view:
        raise RepositoryHttpError(404, BODY_404)
    record = authenticate_token(root, credential)
    if record is None:
        raise RepositoryHttpError(
            401,
            BODY_401,
            {"WWW-Authenticate": 'Basic realm="bastion"'},
        )
    if record["view_id"] != view:
        raise RepositoryHttpError(404, BODY_404)
    required = "fetch" if service == "git-upload-pack" else "push"
    if not has_scope(record, required):
        raise RepositoryHttpError(403, b"forbidden")
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
                raise RepositoryHttpError(400, b"bad request")
            block = stream.read(min(65536, maximum - total + 1))
            if not block:
                return b"".join(chunks)
            total += len(block)
            chunks.append(block)
            if total > maximum:
                raise RepositoryHttpError(413, b"content too large")
    except RequestEntityTooLarge as error:
        raise RepositoryHttpError(413, b"content too large") from error


def create_app(root: Path) -> Flask:
    from clerk_auth import ClerkAuthenticator, load_clerk_config

    _preflight(root)
    limits.validate()
    clerk_config = load_clerk_config()

    app = Flask(__name__)
    app.config["FORGE_ROOT"] = str(root)
    app.config["MAX_CONTENT_LENGTH"] = RECEIVE_MAX_BODY
    app.url_map.strict_slashes = False
    app.config["GIT_SLOTS"] = threading.BoundedSemaphore(limits.MAX_ACTIVE_GIT)
    app.config["RECEIVE_SLOTS"] = threading.BoundedSemaphore(limits.MAX_ACTIVE_RECEIVES)
    app.config["REQUEST_SLOTS"] = threading.BoundedSemaphore(limits.MAX_ACTIVE_REQUESTS)
    app.config["ADMISSION_LOCK"] = threading.Lock()
    app.config["ACTIVE_PER_IP"] = {}
    app.config["ACTIVE_PER_TOKEN"] = {}
    app.config["STOPPING"] = False
    app.config["CLERK_CONFIG"] = clerk_config
    app.config["CLERK_AUTHENTICATOR"] = (
        ClerkAuthenticator(clerk_config) if clerk_config is not None else None
    )
    app.config["SECURITY_HEADERS"] = _security_headers(
        clerk_config.frontend_api_origin if clerk_config is not None else None
    )

    def admit(address: str, receive: bool, token_id: str | None = None) -> int:
        """Per-IP and per-token admission; 0 when admitted, else 429/503."""
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
                # A single credential must not escape its own budget by
                # spreading across source addresses.
                if token_id is not None:
                    token_limit = (
                        limits.MAX_RECEIVES_PER_TOKEN
                        if receive
                        else limits.MAX_ACTIVE_PER_TOKEN
                    )
                    token_active = app.config["ACTIVE_PER_TOKEN"].get(token_id, 0)
                    if token_active >= token_limit:
                        if receive_acquired:
                            app.config["RECEIVE_SLOTS"].release()
                        return 429
                    app.config["ACTIVE_PER_TOKEN"][token_id] = token_active + 1
                app.config["ACTIVE_PER_IP"][address] = active + 1
                return 0
        except BaseException:
            if receive:
                app.config["RECEIVE_SLOTS"].release()
            raise

    def release_admission(address: str, receive: bool, token_id: str | None = None) -> None:
        if receive:
            app.config["RECEIVE_SLOTS"].release()
        with app.config["ADMISSION_LOCK"]:
            remaining = app.config["ACTIVE_PER_IP"].get(address, 0) - 1
            if remaining > 0:
                app.config["ACTIVE_PER_IP"][address] = remaining
            else:
                app.config["ACTIVE_PER_IP"].pop(address, None)
            if token_id is not None:
                token_remaining = app.config["ACTIVE_PER_TOKEN"].get(token_id, 0) - 1
                if token_remaining > 0:
                    app.config["ACTIVE_PER_TOKEN"][token_id] = token_remaining
                else:
                    app.config["ACTIVE_PER_TOKEN"].pop(token_id, None)

    app.config["ADMIT"] = admit
    app.config["RELEASE_ADMISSION"] = release_admission
    # Pagination cursors are HMAC'd with an ephemeral server-side key: a
    # process restart invalidates them, and no client ever sees the key.
    app.config["CURSOR_SECRET"] = secrets.token_hex(32)

    @app.before_request
    def _bound_concurrent_requests():
        """Cap total in-flight requests for the whole app.

        The per-IP and per-credential admission counters only cover the two
        Git RPC routes. Without this every /api/ route - including the
        unauthenticated Clerk exchange, which makes blocking outbound calls,
        and the tree/blob readers, which spawn Git subprocesses - would be
        bounded only by the WSGI server's thread count.
        """
        if request.path in ("/healthz", "/readyz"):
            return None
        if not app.config["REQUEST_SLOTS"].acquire(blocking=False):
            response = make_response(jsonify({"error": "server-saturated"}), 503)
            response.headers["Cache-Control"] = "no-store"
            return response
        g._bastion_request_slot = True
        return None

    @app.teardown_request
    def _release_request_slot(_exception):
        if getattr(g, "_bastion_request_slot", False):
            app.config["REQUEST_SLOTS"].release()
            g._bastion_request_slot = False

    @app.after_request
    def _apply_security_headers(response):
        for key, value in app.config["SECURITY_HEADERS"].items():
            response.headers.setdefault(key, value)
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
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
            repository, _manifest, active, materialized = _resolve(
                _forge_root(app), self_view
            )
        except ForgeError:
            abort(404)
        if not materialized.path.is_dir():
            abort(404)
        if not materialized.projected_refs:
            # A v4 principal can retain an empty browser session, but browse
            # endpoints have no authorized tree/history tip to disclose.
            abort(404)
        # Use the descriptor belonging to the exact replay snapshot that
        # produced this validated projection; never pair a new policy with an
        # old materialized object database.
        view_state = active
        try:
            descriptor = repository._descriptor(active["descriptor_id"])
        except ForgeError:
            abort(404)
        if descriptor.get("principal_id") != active.get("principal_id"):
            abort(404)
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

        from http_server import _resolve_service
        try:
            repository, _manifest, view_state, materialized = _resolve_service(
                _forge_root(app), view
            )
            descriptor = repository._descriptor(view_state["descriptor_id"])
        except ForgeError:
            raise
        if descriptor.get("principal_id") != view_state.get("principal_id"):
            raise ForgeError("active descriptor principal mismatch", "integrity")
        from state import load_principal_provenance

        state = load_principal_provenance(
            _forge_root(app), descriptor["principal_id"]
        )
        team_ids, binding_ids, role_ids = resolve_principal(
            state, descriptor["principal_id"]
        )
        principal = state["principals"][descriptor["principal_id"]]
        projected_ref = view_state["projected_ref"]
        if descriptor.get("algorithm") == "capabilities-v4":
            # v4 advertises the authorized HEAD selected by materialization;
            # do not echo the canonical default branch when it is denied.
            projected_ref = materialized.projected_ref
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
                "projected_ref": projected_ref,
                "capabilities": _api.capability_map_object(descriptor["capabilities"]),
                "restrictions": _api.capability_map_object(descriptor["restrictions"]),
                **({"permissions": _effective_permissions(descriptor)}
                   if descriptor.get("algorithm") == "capabilities-v4" else {}),
            },
        }

    def _effective_permissions(descriptor: dict) -> dict:
        from permissions import descriptor_permissions
        return descriptor_permissions(descriptor)

    @app.route("/api/v1/self/permissions/check", methods=["POST"])
    def api_check_self_permission():
        import api_v1 as _api
        _record, view = _api_auth()
        try:
            raw = _read_bounded_body(_api.MAX_JSON_BODY_BYTES)
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (RepositoryHttpError, UnicodeDecodeError, ValueError):
            return jsonify({"error": "bad-request"}), 400
        if not isinstance(payload, dict):
            return jsonify({"error": "bad-request"}), 400
        scope = payload.get("scope")
        if scope == "repository" and set(payload) == {"scope", "action"}:
            from permissions import REPOSITORY_ACTIONS, repository_allowed
            if not isinstance(payload["action"], str) or payload["action"] not in REPOSITORY_ACTIONS:
                return jsonify({"error": "bad-request"}), 400
            action = payload["action"]
            try:
                _active, descriptor = _active_descriptor(_forge_root(app), view)
                policy = _effective_permissions(descriptor)
            except ForgeError:
                return jsonify({"error": "service-unavailable"}), 503
            allowed = repository_allowed(policy, action)
        elif scope == "branch" and set(payload) == {"scope", "action", "ref"}:
            from permissions import BRANCH_ACTIONS, branch_allowed
            from git_refs import validate_branch_ref
            action, ref = payload.get("action"), payload.get("ref")
            if not isinstance(action, str) or action not in BRANCH_ACTIONS or not isinstance(ref, str):
                return jsonify({"error": "bad-request"}), 400
            try:
                validate_branch_ref(ref)
            except ForgeError:
                return jsonify({"error": "bad-request"}), 400
            try:
                _active, descriptor = _active_descriptor(_forge_root(app), view)
                policy = _effective_permissions(descriptor)
            except ForgeError:
                return jsonify({"error": "service-unavailable"}), 503
            allowed = branch_allowed(policy, action, ref)
        else:
            return jsonify({"error": "bad-request"}), 400
        response = make_response(jsonify({"allowed": allowed}), 200)
        response.headers["Cache-Control"] = "no-store"
        return response

    def _issue_ui_session(
        principal_id: str, requested_view: str | None, expires_in: str
    ):
        from http_server import add_token
        from repository import Repository

        try:
            _manifest, _version, replay = Repository.open(_forge_root(app)).load()
        except ForgeError:
            return {"error": "service-unavailable"}, 503
        views = sorted(
            view_id
            for view_id, active in replay.active_views.items()
            if active["principal_id"] == principal_id
        )
        if not views:
            return {"error": "no-repositories"}, 403
        if requested_view is None and len(views) > 1:
            return {"error": "view-required", "view_ids": views}, 409
        selected = requested_view or views[0]
        if selected not in views:
            return {"error": "forbidden"}, 403
        try:
            credential = add_token(
                _forge_root(app), selected, scopes=["ui"], expires_in=expires_in
            )
            envelope = _api_session_envelope(selected)
        except ForgeError:
            return {"error": "service-unavailable"}, 503
        return {"access_token": credential, "session": envelope}, 200

    @app.route("/api/v1/auth/config", methods=["GET"])
    def api_auth_config():
        config = app.config["CLERK_CONFIG"]
        response = make_response(
            jsonify(
                {"enabled": False}
                if config is None
                else {
                    "enabled": True,
                    "publishable_key": config.publishable_key,
                    "frontend_api_origin": config.frontend_api_origin,
                }
            ),
            200,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/v1/auth/clerk", methods=["POST"])
    def api_clerk_login():
        """Exchange a verified Clerk session for a view-scoped UI token."""
        import accounts
        from accounts import AccountLinkError
        from clerk_auth import ClerkAuthenticationError

        authenticator = app.config["CLERK_AUTHENTICATOR"]
        if authenticator is None:
            return jsonify({"error": "not-found"}), 404
        try:
            raw_body = request.get_data(cache=False)
        except RequestEntityTooLarge:
            return jsonify({"error": "bad-request"}), 400
        if len(raw_body) > 4096:
            return jsonify({"error": "bad-request"}), 400
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (UnicodeDecodeError, ValueError):
            return jsonify({"error": "bad-request"}), 400
        if not isinstance(payload, dict) or set(payload) not in (set(), {"view_id"}):
            return jsonify({"error": "bad-request"}), 400
        requested_view = payload.get("view_id")
        if requested_view is not None and not isinstance(requested_view, str):
            return jsonify({"error": "bad-request"}), 400
        try:
            clerk_user_id, verified_emails = authenticator.authenticate(request.headers)
            principal_id = accounts.link_clerk_account(
                _forge_root(app), clerk_user_id, verified_emails
            )
        except ClerkAuthenticationError:
            response = make_response(jsonify({"error": "invalid-credentials"}), 401)
            response.headers["Cache-Control"] = "no-store"
            return response
        except AccountLinkError:
            response = make_response(jsonify({"error": "not-invited"}), 403)
            response.headers["Cache-Control"] = "no-store"
            return response
        except ForgeError:
            response = make_response(jsonify({"error": "service-unavailable"}), 503)
            response.headers["Cache-Control"] = "no-store"
            return response
        body, status = _issue_ui_session(principal_id, requested_view, "1h")
        response = make_response(jsonify(body), status)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/v1/logout", methods=["POST"])
    def api_logout():
        from http_server import revoke_token

        record, _view = _api_auth()
        # Clerk exchanges mint UI-only tokens. Git/API credentials can also
        # inspect the browser surface and must not be revoked by UI sign-out.
        if set(record.get("scopes", [])) == {"ui"}:
            revoke_token(_forge_root(app), record["token_id"])
        response = make_response(jsonify({"signed_out": True}), 200)
        response.headers["Cache-Control"] = "no-store"
        return response

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

    @app.route("/api/v1/blob", methods=["GET"])
    def api_blob():
        import api_v1 as _api

        _record, view = _api_auth()
        if len(request.query_string) > _api.MAX_QUERY_BYTES:
            return jsonify({"error": "bad-request"}), 400
        raw_b64 = request.args.get("path_b64", "")
        try:
            raw_path = _api.b64decode_field(raw_b64)
            normalized = _api.normalize_api_path(raw_path)
            if not normalized:
                raise ForgeError("a file path is required")
            _view_state, descriptor, repo_path, tip = _resolve_api_view(view)
            if not capability_allowed(
                descriptor["capabilities"]["read"],
                descriptor["restrictions"]["read"],
                os.fsencode(normalized),
            ):
                raise ForgeError("no such path")
            _oid, content = _api.read_blob(repo_path, normalized)
        except ForgeError:
            response = make_response(jsonify({"error": "not-found"}), 404)
            response.headers["Cache-Control"] = "no-store"
            return response
        binary = b"\x00" in content
        text = None
        if not binary:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                binary = True
        response = make_response(
            jsonify(
                {
                    "projected_tip": tip,
                    "path_b64": _api.b64encode_raw(os.fsencode(normalized)),
                    "path_display": _api.display(os.fsencode(normalized)),
                    "size": len(content),
                    "binary": binary,
                    "text": text,
                }
            ),
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

    def _require_repository_owner(view: str) -> tuple[dict, dict]:
        view_state, descriptor = _active_descriptor(_forge_root(app), view)
        if not capability_allowed(
            descriptor["capabilities"]["admin"],
            descriptor["restrictions"]["admin"],
            b"",
        ):
            response = make_response(jsonify({"error": "forbidden"}), 403)
            response.headers["Cache-Control"] = "no-store"
            abort(response)
        return view_state, descriptor

    @app.route("/api/v1/accounts", methods=["GET"])
    def api_search_accounts():
        from clerk_auth import ClerkDirectoryError

        _record, view = _api_auth()
        _require_repository_owner(view)
        authenticator = app.config["CLERK_AUTHENTICATOR"]
        if authenticator is None:
            return jsonify({"error": "directory-disabled"}), 404
        query = request.args.get("query", "").strip()
        try:
            offset = int(request.args.get("offset", "0"))
        except ValueError:
            return jsonify({"error": "bad-request"}), 400
        if (
            len(request.query_string) > 4096 or not 2 <= len(query) <= 100
            or not 0 <= offset <= 10_000
            or set(request.args) - {"query", "offset"}
        ):
            return jsonify({"error": "bad-request"}), 400
        try:
            page = authenticator.search_accounts(query, offset)
            principals = load_state_sections(_forge_root(app), ["principals"])["principals"]
            for account in page["accounts"]:
                account["already_invited"] = any(
                    item.get("clerk_user_id") == account["clerk_user_id"]
                    or account["email"] in item.get("emails", [])
                    for item in principals.values()
                )
        except (ClerkDirectoryError, ForgeError):
            return jsonify({"error": "directory-unavailable"}), 503
        return jsonify(page), 200

    @app.route("/api/v1/invitations", methods=["POST"])
    def api_invite_collaborator():
        import api_v1 as _api
        from service import invite_collaborator
        from clerk_auth import ClerkDirectoryError

        _record, view = _api_auth()
        _require_repository_owner(view)
        raw = request.get_data(cache=False)
        if len(raw) > _api.MAX_JSON_BODY_BYTES:
            return jsonify({"error": "bad-request"}), 400
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return jsonify({"error": "bad-request"}), 400
        if (
            not isinstance(payload, dict)
            or set(payload) not in (
                {"principal_id", "email", "readable_paths", "access"},
                {"principal_id", "clerk_user_id", "readable_paths", "access"},
            )
            or not isinstance(payload.get("principal_id"), str)
            or not isinstance(payload.get("email", payload.get("clerk_user_id")), str)
            or not isinstance(payload.get("readable_paths"), list)
            or not isinstance(payload.get("access"), str)
        ):
            return jsonify({"error": "bad-request"}), 400
        if "clerk_user_id" in payload:
            authenticator = app.config["CLERK_AUTHENTICATOR"]
            if authenticator is None:
                return jsonify({"error": "directory-disabled"}), 404
            try:
                account = authenticator.get_account(payload["clerk_user_id"])
            except ClerkDirectoryError:
                return jsonify({"error": "directory-unavailable"}), 503
            if account is None or account["clerk_user_id"] != payload["clerk_user_id"]:
                return jsonify({"error": "account-unavailable"}), 400
            payload["email"] = account["email"]
        try:
            invite_collaborator(_forge_root(app), view, **payload)
        except ForgeError as error:
            if error.category in {"storage", "integrity"}:
                return jsonify({"error": "service-unavailable"}), 503
            status = 409 if error.category == "conflict" else 400
            return jsonify({"error": "invitation-conflict" if status == 409 else "bad-request"}), status
        return jsonify({"view_id": payload["principal_id"], "access": payload["access"]}), 201

    @app.route("/api/v1/permissions", methods=["GET"])
    def api_permissions():
        import api_v1 as _api
        from repository import Repository
        from service import managed_private_paths

        _record, view = _api_auth()
        actor, _descriptor = _require_repository_owner(view)
        try:
            _manifest, _version, replay = Repository.open(_forge_root(app)).load()
            state = load_state_sections(
                _forge_root(app), ["roles", "role_bindings", "principals"]
            )
            targets = []
            for view_id, active in sorted(replay.active_views.items()):
                if active["principal_id"] == actor["principal_id"]:
                    continue
                target_descriptor = Repository.open(_forge_root(app))._descriptor(active["descriptor_id"])
                targets.append(
                    {
                        "view_id": view_id,
                        "principal_id": active["principal_id"],
                        "emails": state["principals"].get(active["principal_id"], {}).get("emails", []),
                        "readable_paths": _api.capability_map_object(target_descriptor["capabilities"])["read"],
                        "can_modify": bool(target_descriptor["capabilities"]["modify"]),
                        "private_paths": [
                            _api.prefix_object(path)
                            for path in managed_private_paths(
                                state, active["principal_id"]
                            )
                        ],
                    }
                )
        except ForgeError:
            return jsonify({"error": "service-unavailable"}), 503
        response = make_response(jsonify({
            "targets": targets, "account_picker_enabled": app.config["CLERK_AUTHENTICATOR"] is not None,
        }), 200)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/v1/permissions", methods=["POST"])
    def api_update_permissions():
        import api_v1 as _api
        from service import set_path_privacy

        _record, view = _api_auth()
        _require_repository_owner(view)
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
            or set(payload) != {"view_id", "path_b64", "private"}
            or not isinstance(payload.get("view_id"), str)
            or not isinstance(payload.get("path_b64"), str)
            or not isinstance(payload.get("private"), bool)
        ):
            return jsonify({"error": "bad-request"}), 400
        try:
            raw_path = _api.b64decode_field(payload["path_b64"])
            normalized = _api.normalize_api_path(raw_path)
            paths = set_path_privacy(
                _forge_root(app), view, payload["view_id"], normalized, payload["private"]
            )
        except ForgeError as error:
            # Budget and storage refusals are server-side, not client
            # mistakes. Reporting them as 400 hides exactly the conditions
            # that decide whether the catalog edit was rolled back.
            if error.category in {"storage", "integrity", "limits"}:
                response = make_response(jsonify({"error": "service-unavailable"}), 503)
            else:
                response = make_response(jsonify({"error": "bad-request"}), 400)
            response.headers["Cache-Control"] = "no-store"
            return response
        response = make_response(
            jsonify(
                {
                    "view_id": payload["view_id"],
                    "private_paths": [_api.prefix_object(path) for path in paths],
                }
            ),
            200,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    # Git credentials: a signed-in principal mints and revokes long-lived
    # fetch/push tokens for its *own* views. The browser `ui` session token is
    # excluded here; these are the credentials an unmodified Git client uses.
    GIT_CREDENTIAL_SCOPES = ("fetch", "push")
    WRITE_CAPABILITIES = ("create", "modify", "delete", "mode", "symlink")
    MAX_CREDENTIAL_SECONDS = 365 * 86400

    def _caller_views(view: str) -> tuple[str, dict[str, dict]]:
        """Return the caller's principal id and every active view of it."""
        from repository import Repository

        repository = Repository.open(_forge_root(app))
        _manifest, _version, replay = repository.load()
        active = replay.active_views.get(view)
        if active is None:
            abort(404)
        principal_id = active["principal_id"]
        views: dict[str, dict] = {}
        for view_id, item in replay.active_views.items():
            if item["principal_id"] != principal_id:
                continue
            try:
                views[view_id] = repository._descriptor(item["descriptor_id"])
            except ForgeError:
                continue
        return principal_id, views

    def _can_push(descriptor: dict) -> bool:
        return any(
            descriptor["capabilities"].get(name) for name in WRITE_CAPABILITIES
        )

    def _git_tokens(views: dict[str, dict]) -> list[dict]:
        from http_server import list_tokens

        return [
            token
            for token in list_tokens(_forge_root(app))
            if token["view_id"] in views
            and token["scopes"]
            and set(token["scopes"]).issubset(set(GIT_CREDENTIAL_SCOPES))
        ]

    @app.route("/api/v1/credentials", methods=["GET"])
    def api_credentials():
        _record, view = _api_auth()
        try:
            principal_id, views = _caller_views(view)
            tokens = _git_tokens(views)
        except ForgeError:
            return jsonify({"error": "service-unavailable"}), 503
        response = make_response(jsonify({
            "principal_id": principal_id,
            "views": [
                {"view_id": view_id, "can_push": _can_push(descriptor)}
                for view_id, descriptor in sorted(views.items())
            ],
            "tokens": [
                {
                    "token_id": token["token_id"],
                    "view_id": token["view_id"],
                    "scopes": token["scopes"],
                    "created_at": token["created_at"],
                    "expires_at": token["expires_at"],
                    "revoked_at": token["revoked_at"],
                }
                for token in tokens
            ],
        }), 200)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/v1/credentials", methods=["POST"])
    def api_create_credential():
        import api_v1 as _api
        import tokens as _tokens
        from http_server import add_token

        _record, view = _api_auth()
        try:
            raw = request.get_data(cache=False)
        except RequestEntityTooLarge:
            return jsonify({"error": "bad-request"}), 400
        if len(raw) > _api.MAX_JSON_BODY_BYTES:
            return jsonify({"error": "bad-request"}), 400
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            return jsonify({"error": "bad-request"}), 400
        if (
            not isinstance(payload, dict)
            or set(payload) != {"view_id", "scopes", "expires_in"}
            or not isinstance(payload.get("view_id"), str)
            or not isinstance(payload.get("scopes"), list)
            or not payload.get("scopes")
            or not all(isinstance(item, str) for item in payload["scopes"])
            or not isinstance(payload.get("expires_in"), str)
        ):
            return jsonify({"error": "bad-request"}), 400
        if not set(payload["scopes"]).issubset(set(GIT_CREDENTIAL_SCOPES)):
            return jsonify({"error": "bad-request"}), 400
        try:
            expires_at = _tokens.parse_expires_in(payload["expires_in"])
        except ForgeError:
            return jsonify({"error": "bad-request"}), 400
        if (
            expires_at is None
            or expires_at - int(time.time()) > MAX_CREDENTIAL_SECONDS
        ):
            return jsonify({"error": "bad-request"}), 400
        try:
            _principal_id, views = _caller_views(view)
        except ForgeError:
            return jsonify({"error": "service-unavailable"}), 503
        target = payload["view_id"]
        if target not in views:
            return jsonify({"error": "forbidden"}), 403
        if "push" in payload["scopes"] and not _can_push(views[target]):
            # A push credential for a view with no write capability would be a
            # dead credential; refuse it rather than issue a misleading token.
            return jsonify({"error": "forbidden"}), 403
        try:
            credential = add_token(
                _forge_root(app),
                target,
                scopes=sorted(set(payload["scopes"])),
                expires_in=payload["expires_in"],
            )
        except ForgeError as error:
            if error.category in {"storage", "integrity"}:
                return jsonify({"error": "service-unavailable"}), 503
            return jsonify({"error": "bad-request"}), 400
        response = make_response(jsonify({
            "credential": credential,
            "token": {
                "view_id": target,
                "scopes": sorted(set(payload["scopes"])),
                "expires_at": expires_at,
            },
        }), 201)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/v1/credentials/<token_id>", methods=["DELETE"])
    def api_revoke_credential(token_id: str):
        import tokens as _tokens
        from http_server import revoke_token

        _record, view = _api_auth()
        if not _tokens.TOKEN_ID_RE.fullmatch(token_id):
            return jsonify({"error": "not-found"}), 404
        try:
            _principal_id, views = _caller_views(view)
            owned = {token["token_id"] for token in _git_tokens(views)}
        except ForgeError:
            return jsonify({"error": "service-unavailable"}), 503
        if token_id not in owned:
            # Unknown and other-principal token ids share one 404, so the route
            # cannot be used to probe which token ids exist.
            return jsonify({"error": "not-found"}), 404
        try:
            revoked = revoke_token(_forge_root(app), token_id)
        except ForgeError:
            return jsonify({"error": "service-unavailable"}), 503
        if not revoked:
            return jsonify({"error": "not-found"}), 404
        response = make_response(jsonify({"revoked": True, "token_id": token_id}), 200)
        response.headers["Cache-Control"] = "no-store"
        return response

    def _asset(name: str, content_type: str) -> Response:
        target = (ASSET_DIR / name).resolve()
        if target.parent != ASSET_DIR.resolve() or not target.is_file():
            abort(404)
        body = target.read_bytes()
        # A content hash lets a browser revalidate cheaply and, crucially,
        # never keep running a stale shell after a deploy. Without a
        # validator the app would appear broken until a hard reload.
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        if request.headers.get("If-None-Match") == etag:
            response = Response(status=304)
        else:
            response = Response(body, 200, content_type=content_type)
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "no-cache"
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
            raise RepositoryHttpError(404, BODY_404)

        from repository import Repository as _Repository

        # Admission is taken before intake materialization and before any
        # request body is buffered, so a client cannot force unbounded replay
        # work or per-request memory ahead of the budget checks.
        record = _authenticate_git(root, view, service)
        receive = service == "git-receive-pack"
        throttled = app.config["ADMIT"](
            request.remote_addr or "", receive, record["token_id"]
        )
        if throttled:
            raise RepositoryHttpError(
                throttled, b"throttled" if throttled == 429 else b"server saturated"
            )
        try:
            try:
                _repository, manifest, _active, materialized = _resolve_service(root, view)
            except ForgeError:
                raise RepositoryHttpError(404, BODY_404)
            if not materialized.projected_refs:
                raise RepositoryHttpError(403, b"forbidden")
            if receive and (
                manifest.head_seq >= limits.MAX_WAL_SEQUENCE
                or limits.free_below_floor(root)
            ):
                raise RepositoryHttpError(503, b"repository-read-only")
            repo_path = materialized.path if service == "git-upload-pack" else _git_repo(
                root, view, service
            )[0]
            alternates = (
                None if service == "git-upload-pack" else materialized.path / "objects"
            )
            if not app.config["GIT_SLOTS"].acquire(blocking=False):
                raise RepositoryHttpError(503, b"server saturated")
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
            app.config["RELEASE_ADMISSION"](
                request.remote_addr or "", receive, record["token_id"]
            )
        if completed.returncode:
            app.logger.error("git advertisement failed")
            raise RepositoryHttpError(500, b"git advertisement failed")
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
            raise RepositoryHttpError(404, BODY_404)

        # Admission precedes materialization and body buffering so a client
        # cannot force unbounded intake replay work or hold a large buffered
        # body ahead of the concurrency and per-credential budgets.
        record = _authenticate_git(root, view, service)
        receive = service == "git-receive-pack"
        throttled = app.config["ADMIT"](
            request.remote_addr or "", receive, record["token_id"]
        )
        if throttled:
            raise RepositoryHttpError(
                throttled, b"throttled" if throttled == 429 else b"server saturated"
            )
        try:
            if receive and limits.free_below_floor(root):
                raise RepositoryHttpError(503, b"repository-read-only")
            repo_path, alternates = _git_repo(root, view, service)
            protocol = request.headers.get("Git-Protocol")
            maximum = UPLOAD_MAX_BODY if service == "git-upload-pack" else RECEIVE_MAX_BODY
            raw = _read_bounded_body(maximum)
            payload = decode_body(raw, request.headers.get("Content-Encoding"), maximum)

            try:
                _repository, _manifest, active, _materialized = _resolve_service(root, view)
            except ForgeError:
                raise RepositoryHttpError(404, BODY_404)
            if not _materialized.projected_refs:
                raise RepositoryHttpError(403, b"forbidden")

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
                    raise RepositoryHttpError(400, b"invalid Idempotency-Key")
                extra_env["BASTION_IDEMPOTENCY_KEY"] = idempotency_key

            if not app.config["GIT_SLOTS"].acquire(blocking=False):
                raise RepositoryHttpError(503, b"server saturated")
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
            app.config["RELEASE_ADMISSION"](
                request.remote_addr or "", receive, record["token_id"]
            )
        if completed.returncode:
            app.logger.error("git service failed")
            raise RepositoryHttpError(500, b"git service failed")
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
        raise RepositoryHttpError(404, BODY_404)
    if service == "git-upload-pack":
        return materialized.path, None
    from materialization import ensure_intake_materialized

    return ensure_intake_materialized(root, materialized, manifest), materialized.path / "objects"


def make_server(root: Path, host: str = "127.0.0.1", port: int = 8000):
    app = create_app(root)
    from werkzeug.serving import make_server as _make_server

    return _make_server(host, port, app, threaded=True)
