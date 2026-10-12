"""Explicit local multi-repository HTTP host dispatcher."""
from __future__ import annotations

import json
import re
import threading
import time
import hashlib
import io
from pathlib import Path

from flask import Flask, Response, jsonify, request

import limits
from errors import ForgeError
from host import Host, OPAQUE, _read_json, _slug

REPO_URL = re.compile(r"^/([a-z0-9][a-z0-9-]{0,63})/([a-z0-9][a-z0-9-]{0,63})\.git(?:/(.*))?$")


class _NoopSemaphore:
    """Child WSGI apps delegate all request admission to the host."""
    def acquire(self, blocking=True):
        return True

    def release(self):
        return None


def create_app(root: Path | str) -> Flask:
    host = Host(root, initialize=False, migrate=True)
    limits.validate()
    from clerk_auth import load_clerk_config
    clerk_config = load_clerk_config()
    app = Flask(__name__)
    from http_server import RECEIVE_MAX_BODY, UPLOAD_MAX_BODY
    app.config["MAX_CONTENT_LENGTH"] = max(RECEIVE_MAX_BODY, UPLOAD_MAX_BODY)
    cache: dict[str, object] = {}
    cache_lock = threading.RLock()
    global_git = threading.BoundedSemaphore(limits.MAX_ACTIVE_GIT)
    global_receive = threading.BoundedSemaphore(limits.MAX_ACTIVE_RECEIVES)
    global_requests = threading.BoundedSemaphore(limits.MAX_ACTIVE_REQUESTS)
    global_provisions = threading.BoundedSemaphore(limits.MAX_ACTIVE_PROVISIONS)
    admission_lock = threading.Lock()
    active_ips: dict[str, int] = {}
    active_tokens: dict[tuple[str, str], int] = {}
    app.config.update(HOST_REQUEST_SLOTS=global_requests, HOST_GIT_SLOTS=global_git,
                      HOST_RECEIVE_SLOTS=global_receive, HOST_PROVISION_SLOTS=global_provisions)

    @app.before_request
    def request_admission():
        from flask import g
        if not global_requests.acquire(blocking=False):
            return jsonify(error="server-saturated"), 503
        g.host_request_slot = True
        g.host_started_at = time.monotonic()

    @app.teardown_request
    def release_request(_error):
        from flask import g
        if getattr(g, "host_request_slot", False):
            global_requests.release()
            g.host_request_slot = False

    def auth():
        scheme, separator, value = request.headers.get("Authorization", "").partition(" ")
        if not separator or scheme.lower() != "bearer":
            return None
        return host.authenticate_session(value.strip())

    def admit_git(repo_id: str, address: str, receive: bool, token_id: str | None = None) -> int:
        if receive and not global_receive.acquire(blocking=False):
            return 503
        token_key = (repo_id, token_id) if token_id is not None else None
        with admission_lock:
            ip_limit = limits.MAX_RECEIVES_PER_IP if receive else limits.MAX_ACTIVE_PER_IP
            token_limit = limits.MAX_RECEIVES_PER_TOKEN if receive else limits.MAX_ACTIVE_PER_TOKEN
            if active_ips.get(address, 0) >= ip_limit or (token_key and active_tokens.get(token_key, 0) >= token_limit):
                if receive:
                    global_receive.release()
                return 429
            active_ips[address] = active_ips.get(address, 0) + 1
            if token_key:
                active_tokens[token_key] = active_tokens.get(token_key, 0) + 1
        return 0

    def release_git(repo_id: str, address: str, receive: bool, token_id: str | None = None) -> None:
        if receive:
            global_receive.release()
        token_key = (repo_id, token_id) if token_id is not None else None
        with admission_lock:
            if active_ips.get(address, 0) <= 1:
                active_ips.pop(address, None)
            else:
                active_ips[address] -= 1
            if token_key:
                if active_tokens.get(token_key, 0) <= 1:
                    active_tokens.pop(token_key, None)
                else:
                    active_tokens[token_key] -= 1

    def find_repo(repo_id: str, account_id: str | None = None):
        if not OPAQUE.fullmatch(repo_id):
            return None
        repos = host.public_repos(account_id) if account_id else None
        if repos is not None:
            return next((r for r in repos if r["repo_id"] == repo_id), None)
        with host._connect(read_only=True) as con:
            row = con.execute("SELECT r.*,a.owner_name FROM repos r JOIN accounts a ON a.account_id=r.owner_account_id WHERE r.repo_id=? AND r.state='ready'", (repo_id,)).fetchone()
        if not row:
            return None
        return {"repo_id": row["repo_id"], "owner_name": row["owner_name"], "name": row["name"], "branch": row["branch"], "git_url": f"/{row['owner_name']}/{row['name']}.git"}

    @app.after_request
    def secure(response):
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        from demo_server import _security_headers
        for key, value in _security_headers(clerk_config.frontend_api_origin if clerk_config else None).items():
            response.headers.setdefault(key, value)
        response.headers.pop("Set-Cookie", None)
        if response.status_code == 401 and request.endpoint != "clerk_login" and request.path.startswith("/api/"):
            response.headers.setdefault("WWW-Authenticate", 'Bearer realm="bastion"')
        return response

    @app.errorhandler(ForgeError)
    def host_forge_error(error):
        return jsonify(error="service-unavailable"), 503

    @app.errorhandler(404)
    def host_not_found(_error):
        return (jsonify(error="not-found"), 404) if request.path.startswith("/api/") else ("not found", 404)

    @app.errorhandler(405)
    def host_method_not_allowed(_error):
        return (jsonify(error="not-found"), 404) if request.path.startswith("/api/") else ("not found", 404)

    @app.errorhandler(413)
    def host_too_large(_error):
        return jsonify(error="bad-request"), 413

    @app.errorhandler(Exception)
    def host_unexpected_error(_error):
        if request.path.startswith("/api/"):
            return jsonify(error="service-unavailable"), 503
        return "service unavailable", 503

    @app.get("/api/v1/auth/config")
    def config():
        from clerk_auth import load_clerk_config
        cfg = load_clerk_config()
        return jsonify({"enabled": False, "mode": "host"} if cfg is None else {
            "enabled": True, "publishable_key": cfg.publishable_key,
            "frontend_api_origin": cfg.frontend_api_origin, "mode": "host"})

    @app.post("/api/v1/auth/clerk")
    def clerk_login():
        from clerk_auth import ClerkAuthenticationError
        if request.content_length is not None and request.content_length > 4096:
            return jsonify(error="bad-request"), 400
        try:
            payload = _bounded_json(4096)
            if payload != {}:
                return jsonify(error="bad-request"), 400
            from clerk_auth import ClerkAuthenticator, ClerkAuthenticationError, load_clerk_config
            cfg = load_clerk_config()
            if cfg is None:
                return jsonify(error="not-found"), 404
            user_id, emails = ClerkAuthenticator(cfg).authenticate(request.headers)
            account_id = host.link_clerk_account(user_id, emails)
            token = host.issue_session(account_id)
            with host._connect(read_only=True) as con:
                account = con.execute("SELECT account_id,owner_name FROM accounts WHERE account_id=? AND state='approved'", (account_id,)).fetchone()
            return jsonify(access_token=token, account={"account_id": account["account_id"], "owner_name": account["owner_name"]}, mode="host")
        except ClerkAuthenticationError:
            return jsonify(error="invalid-credentials"), 401
        except (ValueError, UnicodeDecodeError):
            return jsonify(error="bad-request"), 400
        except ForgeError as error:
            if error.category == "policy":
                return jsonify(error="not-invited"), 403
            if error.category == "limits":
                return jsonify(error="bad-request"), 413
            return jsonify(error="service-unavailable"), 503
        except Exception:
            app.logger.exception("Host Clerk exchange failed unexpectedly")
            return jsonify(error="service-unavailable"), 503

    @app.post("/api/v1/logout")
    def logout():
        scheme, separator, value = request.headers.get("Authorization", "").partition(" ")
        if separator and scheme.lower() == "bearer":
            host.revoke_session(value.strip())
        return jsonify(signed_out=True)

    @app.get("/api/v1/session")
    def session():
        account = auth()
        if not account:
            return jsonify(error="auth-required"), 401
        with host._connect(read_only=True) as con:
            row = con.execute("SELECT account_id,owner_name FROM accounts WHERE account_id=? AND state='approved'", (account["account_id"],)).fetchone()
        return jsonify(account={"account_id": row["account_id"], "owner_name": row["owner_name"]}, mode="host")

    @app.route("/api/v1/repos", methods=["GET", "POST"])
    def repos():
        actor = auth()
        if not actor:
            return jsonify(error="auth-required"), 401
        if request.method == "GET":
            return jsonify(repos=host.public_repos(actor["account_id"]))
        try:
            body = _bounded_json(4096)
        except (ForgeError, ValueError):
            return jsonify(error="bad-request"), 400
        if not isinstance(body, dict) or set(body) - {"name", "request_id", "branch"} or not {"name", "request_id"}.issubset(body):
            return jsonify(error="bad-request"), 400
        try:
            name = _slug(body["name"], "repository name")
            branch = body.get("branch", "main")
            if not global_provisions.acquire(blocking=False):
                return jsonify(error="server-saturated"), 503
            try:
                result = host.create_repo(actor["owner_name"], name, request_id=body["request_id"], branch=branch, seed_empty=True)
            finally:
                global_provisions.release()
            public = next(r for r in host.public_repos(actor["account_id"]) if r["repo_id"] == result["repo_id"])
            return jsonify(repo=public), 201
        except ForgeError as error:
            status = 503 if error.category in {"storage", "integrity", "limits"} else 409 if error.category == "conflict" else 400
            return jsonify(error="service-unavailable" if status == 503 else "creation-failed"), status
        except (KeyError, ValueError, StopIteration):
            return jsonify(error="creation-failed"), 409

    @app.post("/api/v1/repos/<repo_id>/auth/session")
    def exchange(repo_id):
        actor = auth()
        if not actor:
            return jsonify(error="auth-required"), 401
        repo = find_repo(repo_id, actor["account_id"])
        if not repo:
            return jsonify(error="not-found"), 404
        try:
            body = _bounded_json(4096)
        except (ForgeError, ValueError):
            return jsonify(error="bad-request"), 400
        if not isinstance(body, dict) or set(body) - {"view_id"}:
            return jsonify(error="bad-request"), 400
        views = repo["views"]
        selected = body.get("view_id")
        if selected is None and len(views) > 1:
            return jsonify(error="view-required", view_ids=views), 409
        if selected is None and views:
            selected = views[0]
        if selected not in views:
            return jsonify(error="not-found"), 404
        current_actor = auth()
        current_repo = find_repo(repo_id, current_actor["account_id"]) if current_actor else None
        if not current_actor or current_actor["account_id"] != actor["account_id"] or not current_repo or selected not in current_repo["views"]:
            return jsonify(error="not-found"), 404
        from http_server import add_token
        credential = add_token(host.root / "repos" / repo_id, selected, scopes=["ui"], expires_in="1h")
        with host._connect(read_only=True) as con:
            membership = con.execute("SELECT principal_id FROM memberships WHERE repo_id=? AND account_id=? AND state='active'", (repo_id, actor["account_id"])).fetchone()
        from state import load_state_sections
        emails = load_state_sections(host.root / "repos" / repo_id, ["principals"])["principals"].get(membership["principal_id"], {}).get("emails", [])
        try:
            env = _internal_dispatch(child_app(repo_id), "/api/v1/session", method="GET",
                                     headers={"HTTP_AUTHORIZATION": "Bearer " + credential})
            if env.status_code != 200:
                from http_server import revoke_token
                from tokens import TOKEN_ID_RE
                from http_server import authenticate_token
                record = authenticate_token(host.root / "repos" / repo_id, credential)
                if record:
                    revoke_token(host.root / "repos" / repo_id, record["token_id"])
                return jsonify(error="service-unavailable"), 503
            envelope = env.get_json()
        except Exception:
            from http_server import authenticate_token, revoke_token
            record = authenticate_token(host.root / "repos" / repo_id, credential)
            if record:
                revoke_token(host.root / "repos" / repo_id, record["token_id"])
            return jsonify(error="service-unavailable"), 503
        envelope["repo"] = {k: repo[k] for k in ("repo_id", "owner_name", "name", "branch", "git_url")}
        envelope.setdefault("principal", {})["emails"] = emails
        return jsonify(access_token=credential, session=envelope)

    def child_app(repo_id):
        with cache_lock:
            root_path = host.root / "repos" / repo_id
            # Revalidate reservation marker and physical root identity on every use.
            if not OPAQUE.fullmatch(repo_id) or root_path.is_symlink() or not root_path.is_dir():
                raise ForgeError("repository root is unavailable", "integrity")
            with host._connect(read_only=True) as con:
                row = con.execute("SELECT r.state,r.repository_id,r.inputs_hash FROM repos r WHERE r.repo_id=?", (repo_id,)).fetchone()
            if not row or row["state"] != "ready":
                raise ForgeError("repository is not ready", "policy")
            marker = _read_json(host.root / "locks" / f"{repo_id}.owned")
            info = root_path.stat()
            if (set(marker) != {"repo_id", "inputs_hash", "root_identity"} or marker.get("repo_id") != repo_id or
                marker.get("inputs_hash") != row["inputs_hash"] or marker.get("root_identity") != [info.st_dev, info.st_ino]):
                raise ForgeError("repository root identity changed", "integrity")
            from state import load_state_sections
            meta = load_state_sections(root_path, [])
            if meta.get("repository_id") != row["repository_id"]:
                raise ForgeError("repository identity changed", "integrity")
            host._check_forge_storage(root_path, meta.get("canonical_branch", ""))
            if repo_id not in cache:
                from demo_server import create_app as create_demo_app
                child = create_demo_app(root_path)
                child.config["GIT_SLOTS"] = global_git
                child.config["RECEIVE_SLOTS"] = global_receive
                child.config["REQUEST_SLOTS"] = _NoopSemaphore()
                child.config["ADMIT"] = lambda address, receive, token_id=None: admit_git(repo_id, address, receive, token_id)
                child.config["RELEASE_ADMISSION"] = lambda address, receive, token_id=None: release_git(repo_id, address, receive, token_id)
                cache[repo_id] = child
            cache[repo_id].config["FORGE_ROOT"] = str(root_path)
            if len(cache) > 32:
                cache.pop(next(iter(cache)))
            return cache[repo_id]

    @app.route("/api/v1/repos/<repo_id>/<path:suffix>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def scoped(repo_id, suffix):
        repo = find_repo(repo_id)
        if not repo:
            return jsonify(error="not-found"), 404
        allowed = {"session": {"GET"}, "tree": {"GET"}, "history": {"GET"}, "blob": {"GET"},
                   "decisions": {"POST"}, "accounts": {"GET"}, "invitations": {"POST"},
                   "permissions": {"GET", "POST"}, "credentials": {"GET", "POST"}, "logout": {"POST"}}
        credential_delete = re.fullmatch(r"credentials/[A-Za-z0-9_-]{22}", suffix) and request.method == "DELETE"
        if not credential_delete and request.method not in allowed.get(suffix, set()):
            return jsonify(error="not-found"), 404
        if suffix == "invitations" and request.method == "POST":
            child_app(repo_id)  # Invitations need the same root/confinement checks as delegated routes.
            return host_invitation(repo_id)
        if request.method == "POST":
            try:
                _replay_body(_read_bounded(8192))
            except (ForgeError, ValueError):
                return jsonify(error="bad-request"), 413
        return _dispatch(child_app(repo_id), "/api/v1/" + suffix)

    def host_invitation(repo_id):
        from http_server import authenticate_token, parse_authorization
        from repository import Repository
        from state import load_state_sections
        token = parse_authorization(request.headers.get("Authorization"))
        record = authenticate_token(host.root / "repos" / repo_id, token) if token else None
        if not record or "ui" not in record.get("scopes", []):
            return jsonify(error="auth-required"), 401
        raw = _read_bounded(8192)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return jsonify(error="bad-request"), 400
        required = {"principal_id", "readable_paths", "access"}
        if (not request.is_json or not isinstance(body, dict) or not required.issubset(body) or
                set(body) - (required | {"email", "clerk_user_id"}) or not ("email" in body or "clerk_user_id" in body) or
                not all(isinstance(body.get(k), str) for k in ("principal_id", "access")) or
                any(not isinstance(body[k], str) for k in ("email", "clerk_user_id") if k in body) or
                not isinstance(body.get("readable_paths"), list)):
            return jsonify(error="bad-request"), 400
        try:
            repo_root = host.root / "repos" / repo_id
            repository = Repository.open(repo_root)
            _manifest, _version, replay = repository.load(use_cache=False)
            actor = replay.active_views.get(record["view_id"])
            if not actor:
                return jsonify(error="forbidden"), 403
            actor_descriptor = repository._descriptor(actor["descriptor_id"])
            from validation import capability_allowed
            if not capability_allowed(actor_descriptor["capabilities"]["admin"], actor_descriptor["restrictions"]["admin"], b""):
                return jsonify(error="forbidden"), 403
            principals = load_state_sections(repo_root, ["principals"])["principals"]
            actor_principal = principals.get(actor["principal_id"], {})
            from identity import validate_identifier
            from validation import validate_view_name
            validate_identifier(body["principal_id"])
            validate_view_name(body["principal_id"])
            from validation import normalize_prefix
            from service import require_delegable_coverage
            if (body["access"] not in {"read-only", "contributor"} or
                    not 1 <= len(body["readable_paths"]) <= 100 or
                    not all(isinstance(path, str) for path in body["readable_paths"])):
                return jsonify(error="bad-request"), 400
            paths = sorted(set(normalize_prefix(path) for path in body["readable_paths"]))
            require_delegable_coverage(actor_descriptor, paths, body["access"])
            from clerk_auth import ClerkAuthenticator
            if clerk_config is None:
                return jsonify(error="account-unavailable"), 400
            directory = ClerkAuthenticator(clerk_config)
            target_user_id = body.get("clerk_user_id")
            if target_user_id is None:
                email_query = body["email"].strip().lower()
                with host._connect(read_only=True) as con:
                    matches = [row for row in con.execute("SELECT account_id,emails,clerk_user_id FROM accounts WHERE state='approved'")
                               if email_query in json.loads(row["emails"])]
                if len(matches) != 1:
                    return jsonify(error="account-unavailable"), 400
                target_user_id = matches[0]["clerk_user_id"]
                if target_user_id is None:
                    page = directory.search_accounts(email_query, 0)
                    exact = [item for item in page["accounts"] if item["email"].lower() == email_query]
                    if page["next_offset"] is not None or len(exact) != 1:
                        return jsonify(error="account-unavailable"), 400
                    target_user_id = exact[0]["clerk_user_id"]
            with host._connect(read_only=True) as con:
                owner = con.execute("SELECT account_id FROM accounts WHERE clerk_user_id=? AND state='approved'", (actor_principal.get("clerk_user_id"),)).fetchone()
                target = con.execute("SELECT account_id,emails,clerk_user_id FROM accounts WHERE clerk_user_id=? AND state='approved'", (target_user_id,)).fetchone()
            if not owner or target_user_id == actor_principal.get("clerk_user_id"):
                return jsonify(error="account-unavailable"), 400
            selected = directory.get_account(target_user_id)
            if selected is None:
                return jsonify(error="account-unavailable"), 400
            email = selected["email"].lower()
            if body.get("email") is not None and body["email"].strip().lower() != email:
                return jsonify(error="account-unavailable"), 400
            if target is None:
                with host._connect(read_only=True) as con:
                    matches = [row for row in con.execute("SELECT account_id,emails,clerk_user_id FROM accounts WHERE state='approved' AND clerk_user_id IS NULL")
                               if email in json.loads(row["emails"])]
                if len(matches) != 1:
                    return jsonify(error="account-unavailable"), 400
                host.link_clerk_account(target_user_id, [email])
                target = matches[0]
            elif email not in json.loads(target["emails"]):
                return jsonify(error="account-unavailable"), 400
            intent_payload = [repo_id, owner["account_id"], target["account_id"], body["principal_id"], email,
                              target_user_id, paths, body["access"], record["view_id"]]
            digest = hashlib.sha256(json.dumps(intent_payload, sort_keys=True).encode()).hexdigest()
            with host._connect(read_only=True) as con:
                prior_intent = con.execute("SELECT 1 FROM membership_intents WHERE repo_id=? AND account_id=?", (repo_id, target["account_id"])).fetchone()
            if not prior_intent and (body["principal_id"] in principals or body["principal_id"] in replay.active_views):
                return jsonify(error="invitation-conflict"), 409
            intent = host.reserve_membership_intent(repo_id, target["account_id"], body["principal_id"], email,
                                                     target_user_id, digest)
            active_target = any(v.get("principal_id") == body["principal_id"] for v in replay.active_views.values())
            if not active_target:
                from service import invite_collaborator
                invite_collaborator(repo_root, record["view_id"], body["principal_id"], email,
                                    paths, body["access"], target_user_id)
            host.activate_membership_intent(repo_id, target["account_id"], body["principal_id"])
            return jsonify(view_id=body["principal_id"], access=body["access"]), 201
        except ForgeError as error:
            status = 413 if error.category == "limits" else 503 if error.category in {"storage", "integrity"} else 409 if error.category == "conflict" else 400
            return jsonify(error="service-unavailable" if status == 503 else "invitation-conflict"), status
        except Exception:
            app.logger.exception("Host invitation failed unexpectedly")
            return jsonify(error="service-unavailable"), 503

    def _read_bounded(maximum):
        if request.content_length is not None and request.content_length > maximum:
            raise ForgeError("request body is too large", "limits")
        stream = request.stream
        chunks, size = [], 0
        deadline = time.monotonic() + 30
        while True:
            if time.monotonic() > deadline:
                raise ForgeError("request body timed out", "limits")
            chunk = stream.read(min(65536, maximum - size + 1))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
            if size > maximum:
                raise ForgeError("request body is too large", "limits")

    def _replay_body(raw):
        request.environ["wsgi.input"] = io.BytesIO(raw)
        request.environ["CONTENT_LENGTH"] = str(len(raw))

    def _bounded_json(maximum):
        raw = _read_bounded(maximum)
        if not raw:
            return {}
        if not request.is_json:
            raise ValueError("JSON content type required")
        return json.loads(raw.decode("utf-8"))

    def _internal_dispatch(child, path, method=None, headers=None):
        environ = request.environ.copy()
        environ["PATH_INFO"] = path
        environ["SCRIPT_NAME"] = ""
        environ["REQUEST_METHOD"] = method or request.method
        environ["QUERY_STRING"] = request.query_string.decode("latin-1")
        environ["wsgi.input"] = request.environ["wsgi.input"]
        environ["CONTENT_LENGTH"] = request.environ.get("CONTENT_LENGTH", "")
        environ["CONTENT_TYPE"] = request.environ.get("CONTENT_TYPE", "")
        environ["REMOTE_ADDR"] = request.remote_addr or ""
        for key, value in (headers or {}).items():
            environ[key] = value
        captured = {}
        def start_response(status, response_headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = response_headers
        iterable = child.wsgi_app(environ, start_response)
        status = int(captured["status"].split(" ", 1)[0])
        return Response(iterable, status=status, headers=captured["headers"])

    def _dispatch(child, path):
        return _internal_dispatch(child, path)

    @app.route("/<path:unused>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def git_or_static(unused):
        path = "/" + unused
        match = REPO_URL.fullmatch(path)
        if not match:
            return Response("not found", 404)
        owner, name, suffix = match.groups()
        with host._connect(read_only=True) as con:
            row = con.execute("SELECT r.repo_id FROM repos r JOIN accounts a ON a.account_id=r.owner_account_id WHERE a.owner_name=? AND r.name=? AND r.state='ready'", (owner, name)).fetchone()
        if not row:
            return Response("not found", 404)
        repo = find_repo(row["repo_id"])
        # Git authentication and username/view binding remain in the selected child.
        try:
            child = child_app(repo["repo_id"])
            from http_server import authenticate_token, parse_git_authorization
            parsed = parse_git_authorization(request.headers.get("Authorization"))
            if parsed is None:
                return Response("authentication required", 401, {"WWW-Authenticate": 'Basic realm="bastion"'})
            _username, token = parsed
            record = authenticate_token(host.root / "repos" / repo["repo_id"], token)
            if record is None:
                return Response("authentication required", 401, {"WWW-Authenticate": 'Basic realm="bastion"'})
            active_path = f"/{record['view_id']}.git/{suffix or ''}"
            return _dispatch(child, active_path)
        except ForgeError as error:
            if error.category in {"storage", "integrity"}:
                return Response("service unavailable", 503)
            return Response("not found", 404)
        except Exception:
            return Response("service unavailable", 503)

    @app.get("/")
    def landing():
        from demo_server import ASSET_DIR
        return Response((ASSET_DIR / "index.html").read_bytes(), content_type="text/html; charset=utf-8")

    @app.get("/assets/<path:name>")
    def assets(name):
        from demo_server import ASSET_DIR
        path = (ASSET_DIR / name).resolve()
        if path.parent != ASSET_DIR.resolve() or not path.is_file():
            return Response("not found", 404)
        return Response(path.read_bytes(), content_type="text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8")

    return app


def serve(root, host="127.0.0.1", port=8000):
    create_app(root).run(host=host, port=port, threaded=True)
