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


def create_app(root: Path | str, *, host_catalog=None) -> Flask:
    host = host_catalog or Host(root, initialize=False, migrate=True)
    limits.validate()
    from clerk_auth import load_clerk_config
    clerk_config = load_clerk_config()
    app = Flask(__name__)
    from http_server import RECEIVE_MAX_BODY, UPLOAD_MAX_BODY
    app.config["MAX_CONTENT_LENGTH"] = max(RECEIVE_MAX_BODY, UPLOAD_MAX_BODY)
    app.config["MAX_CONTENT_LENGTH"] = max(app.config["MAX_CONTENT_LENGTH"], limits.IMPORT_PACK_BYTES + 8192)
    cache: dict[str, object] = {}
    cache_lock = threading.RLock()
    global_git = threading.BoundedSemaphore(limits.MAX_ACTIVE_GIT)
    global_receive = threading.BoundedSemaphore(limits.MAX_ACTIVE_RECEIVES)
    global_requests = threading.BoundedSemaphore(limits.MAX_ACTIVE_REQUESTS)
    global_provisions = threading.BoundedSemaphore(limits.MAX_ACTIVE_PROVISIONS)
    global_imports = threading.BoundedSemaphore(limits.MAX_ACTIVE_IMPORTS)
    admission_lock = threading.Lock()
    active_ips: dict[str, int] = {}
    active_tokens: dict[tuple[str, str], int] = {}
    active_import_actors: dict[str, int] = {}
    active_import_ips: dict[str, int] = {}
    app.config.update(HOST_REQUEST_SLOTS=global_requests, HOST_GIT_SLOTS=global_git,
                      HOST_RECEIVE_SLOTS=global_receive, HOST_PROVISION_SLOTS=global_provisions,
                      HOST_IMPORT_SLOTS=global_imports)
    app.config["HOST_CATALOG"] = host

    def admit_import(actor):
        address = request.remote_addr or ""
        account_id = actor["account_id"]
        if not global_imports.acquire(blocking=False): return None
        with admission_lock:
            if (active_import_actors.get(account_id, 0) >= limits.MAX_ACTIVE_IMPORTS_PER_ACCOUNT or
                    active_import_ips.get(address, 0) >= limits.MAX_IMPORTS_PER_IP):
                global_imports.release(); return None
            active_import_actors[account_id] = active_import_actors.get(account_id, 0) + 1
            active_import_ips[address] = active_import_ips.get(address, 0) + 1
        def release():
            with admission_lock:
                for table, key in ((active_import_actors, account_id), (active_import_ips, address)):
                    count = table.get(key, 0)
                    if count <= 1: table.pop(key, None)
                    else: table[key] = count - 1
            global_imports.release()
        return release

    @app.before_request
    def request_admission():
        from flask import g
        if request.path in ("/healthz", "/readyz"):
            return None
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
        # This path is used for routing when no membership-scoped discovery
        # result is available. Never echo the configured canonical default
        # branch here; only Host.public_repos can select a policy-readable ref.
        return {"repo_id": row["repo_id"], "owner_name": row["owner_name"], "name": row["name"], "branch": None, "git_url": f"/{row['owner_name']}/{row['name']}.git"}

    @app.after_request
    def secure(response):
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        from repository_server import _security_headers
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
        return jsonify({"enabled": False, "mode": "host", "import_limits": {"pack_bytes": limits.IMPORT_PACK_BYTES}, "import_mode": "first-push"} if cfg is None else {
            "enabled": True, "publishable_key": cfg.publishable_key,
            "frontend_api_origin": cfg.frontend_api_origin, "mode": "host",
            "import_limits": {"pack_bytes": limits.IMPORT_PACK_BYTES}, "import_mode": "first-push"})

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
            return jsonify(access_token=token, account={"account_id": account["account_id"], "owner_name": account["owner_name"], "is_host_admin": host.is_host_admin(account["account_id"])}, mode="host")
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
        return jsonify(account={"account_id": row["account_id"], "owner_name": row["owner_name"], "is_host_admin": host.is_host_admin(row["account_id"])}, mode="host")

    @app.route("/api/v1/host/accounts", methods=["GET", "POST"])
    def host_accounts():
        actor = auth()
        if not actor:
            return jsonify(error="auth-required"), 401
        if not host.is_host_admin(actor["account_id"]):
            return jsonify(error="not-found"), 404
        if request.method == "GET":
            with host._connect(read_only=True) as con:
                rows = con.execute("SELECT account_id,owner_name,emails,state FROM accounts ORDER BY owner_name LIMIT 1000").fetchall()
            return jsonify(accounts=[{"owner_name": row["owner_name"], "emails": json.loads(row["emails"]), "state": row["state"]} for row in rows])
        try:
            body = _bounded_json(4096)
            if not isinstance(body, dict) or set(body) != {"owner_name", "email"} or not isinstance(body["email"], str) or not isinstance(body["owner_name"], str) or len(body["email"]) > 254:
                return jsonify(error="bad-request"), 400
            email = body["email"].strip().lower()
            with host._connect(read_only=True) as con:
                existing = con.execute("SELECT account_id,owner_name,emails,state FROM accounts WHERE owner_name=?", (body["owner_name"],)).fetchone()
            if existing:
                if existing["state"] != "approved" or json.loads(existing["emails"]) != [email]:
                    return jsonify(error="account-approval-failed"), 409
                return jsonify(account={"owner_name": existing["owner_name"], "account_id": existing["account_id"]}), 201
            account_id = host.approve_account(body["owner_name"], [body["email"]])
            return jsonify(account={"owner_name": body["owner_name"], "account_id": account_id}), 201
        except (ValueError, UnicodeDecodeError):
            return jsonify(error="bad-request"), 400
        except ForgeError as error:
            status = 409 if error.category == "conflict" else 503 if error.category in {"storage", "integrity"} else 400
            return jsonify(error="account-approval-failed"), status

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

    @app.post("/api/v1/imports")
    def import_reserve():
        actor = auth()
        if not actor: return jsonify(error="auth-required"), 401
        try:
            body = _bounded_json(4096)
            import first_push
            row = first_push.reserve(host, actor["account_id"], body)
            return jsonify({"import": first_push.public(host, row)}), 201
        except ForgeError as error:
            status = 409 if error.category == "conflict" else 413 if error.category == "limits" else 503 if error.category in {"integrity", "storage"} else 400
            return jsonify(error="import-conflict" if status == 409 else "service-unavailable" if status == 503 else "bad-request"), status
        except (ValueError, UnicodeDecodeError): return jsonify(error="bad-request"), 400

    @app.post("/api/v1/imports/<import_id>/credentials")
    def import_credential(import_id):
        actor = auth()
        if not actor: return jsonify(error="auth-required"), 401
        release_import = admit_import(actor)
        if release_import is None: return jsonify(error="server-saturated"), 429
        try:
            if _bounded_json(4096) != {}: return jsonify(error="bad-request"), 400
            import first_push
            def authorize():
                current = auth()
                if current is None or current["account_id"] != actor["account_id"]:
                    raise ForgeError("host session is no longer valid", "authentication")
            result = first_push.issue_credential(host, actor["account_id"], import_id, authorize=authorize)
            return (jsonify(result), 201) if result else (jsonify(error="not-found"), 404)
        except ForgeError as error:
            status = 401 if error.category == "authentication" else 409 if error.category == "conflict" else 413 if error.category == "limits" else 503
            return jsonify(error="auth-required" if status == 401 else "import-failed"), status
        except (ValueError, UnicodeDecodeError): return jsonify(error="bad-request"), 400
        finally:
            release_import()

    @app.get("/api/v1/imports")
    def import_list():
        actor = auth()
        if not actor: return jsonify(error="auth-required"), 401
        import first_push
        with host._connect(read_only=True) as con:
            rows = con.execute("SELECT * FROM imports WHERE account_id=? AND mode='push' AND state!='ready' ORDER BY created_at DESC LIMIT 1000", (actor["account_id"],)).fetchall()
        return jsonify(imports=[first_push.public(host, dict(row)) for row in rows])

    @app.get("/api/v1/imports/<import_id>")
    def import_status(import_id):
        actor = auth()
        if not actor: return jsonify(error="auth-required"), 401
        import first_push, repository_import
        row = repository_import.get(host, actor["account_id"], import_id)
        return (jsonify({"import": first_push.public(host, row)}), 200) if row and row["mode"] == "push" else (jsonify(error="not-found"), 404)

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
            host.prepare_repository(repo_id)
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
                from repository_server import create_app as create_repository_app
                child = create_repository_app(root_path)
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

    def _read_bounded(maximum, seconds=30):
        if request.content_length is not None and request.content_length > maximum:
            raise ForgeError("request body is too large", "limits")
        stream = request.stream
        chunks, size = [], 0
        deadline = time.monotonic() + seconds
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

    def initial_push(owner, name, suffix, parsed):
        from http_server import parse_service
        import first_push
        challenge = {"WWW-Authenticate": 'Basic realm="bastion import"'}
        if parsed is None or parsed[0] not in (None, "owner"):
            return Response("authentication required", 401, challenge)
        credential = parsed[1]
        row = first_push.authenticate(host, credential, owner, name)
        if row is None:
            return Response("authentication required", 401, challenge)
        if request.method == "GET" and suffix == "info/refs" and parse_service(request.query_string.decode("ascii", errors="replace")) == "git-receive-pack":
            return Response(first_push.advertisement(row), content_type="application/x-git-receive-pack-advertisement")
        if request.method != "POST" or suffix != "git-receive-pack":
            return Response("not found", 404)
        if request.mimetype != "application/x-git-receive-pack-request" or request.headers.get("Content-Encoding", "identity").lower() != "identity":
            return Response("bad request", 400)
        release_import = admit_import({"account_id": row["account_id"]})
        if release_import is None: return Response("throttled", 429)
        receive_status = admit_git(row["import_id"], request.remote_addr or "", True, row["import_id"])
        if receive_status:
            release_import(); return Response("server saturated", receive_status)
        acquired = []
        try:
            for slot in (global_provisions, global_git):
                if not slot.acquire(blocking=False): return Response("server saturated", 503)
                acquired.append(slot)
            def authorize():
                current = first_push.authenticate(host, credential, owner, name)
                if current is None or current["import_id"] != row["import_id"]:
                    raise ForgeError("import credential is no longer valid", "authentication")
            import repository_import
            with repository_import._lock(host, row["import_id"]):
                try:
                    authorize()
                    first_push.reserve_intake(host, row)
                    body = _read_bounded(limits.IMPORT_PACK_BYTES + 8192, limits.IMPORT_UPLOAD_SECONDS)
                    first_push.receive(host, row, body, authorize, _locked=True)
                finally:
                    first_push.release_unused_intake(host, row)
            return Response(first_push.report(row["branch"]), content_type="application/x-git-receive-pack-result")
        except ForgeError as error:
            if error.category == "authentication": return Response("authentication required", 401, challenge)
            if error.category in {"storage", "integrity"}: return Response("service unavailable", 503)
            return Response(first_push.report(row["branch"], error=True), content_type="application/x-git-receive-pack-result")
        finally:
            for slot in reversed(acquired): slot.release()
            release_git(row["import_id"], request.remote_addr or "", True, row["import_id"])
            release_import()

    @app.route("/<path:unused>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def git_or_static(unused):
        path = "/" + unused
        match = REPO_URL.fullmatch(path)
        if not match:
            return Response("not found", 404)
        owner, name, suffix = match.groups()
        from http_server import parse_git_authorization
        parsed = parse_git_authorization(request.headers.get("Authorization"))
        if parsed and parsed[1].startswith("bsi1."):
            return initial_push(owner, name, suffix, parsed)
        with host._connect(read_only=True) as con:
            row = con.execute("SELECT r.repo_id FROM repos r JOIN accounts a ON a.account_id=r.owner_account_id WHERE a.owner_name=? AND r.name=? AND r.state='ready'", (owner, name)).fetchone()
        if not row:
            with host._connect(read_only=True) as con:
                pending = con.execute("SELECT 1 FROM imports i JOIN accounts a ON a.account_id=i.account_id WHERE a.owner_name=? AND i.name=? AND i.mode='push'", (owner, name)).fetchone()
            if pending:
                return initial_push(owner, name, suffix, parsed)
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
        from repository_server import ASSET_DIR
        return Response((ASSET_DIR / "index.html").read_bytes(), content_type="text/html; charset=utf-8")

    @app.get("/healthz")
    def health():
        return jsonify(status="ok")

    @app.get("/readyz")
    def ready():
        try:
            with host._connect(read_only=True) as con:
                con.execute("SELECT value FROM host_settings WHERE key='total_repo_limit'").fetchone()
            return jsonify(status="ok")
        except ForgeError:
            return jsonify(error="not-ready"), 503

    @app.get("/assets/<path:name>")
    def assets(name):
        from repository_server import ASSET_DIR
        path = (ASSET_DIR / name).resolve()
        if path.parent != ASSET_DIR.resolve() or not path.is_file():
            return Response("not found", 404)
        return Response(path.read_bytes(), content_type="text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8")

    return app


def serve(root, host="127.0.0.1", port=8000):
    create_app(root).run(host=host, port=port, threaded=True)
