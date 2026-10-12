# Hosted Permission Demo

## Contract

Bastion runs as a single-node HTTPS service that demonstrates repository permissions through both an unmodified Git client and an authenticated browser. A user can inspect their effective teams, roles, allow and deny prefixes, evaluate a path decision, browse only their projected tree and history, clone that same projection, and observe permitted and rejected pushes.

This milestone is an invite-only demonstration using non-sensitive data. It is not a production multi-tenant forge or a claim that downloaded data can be revoked.

## Dependency

The permission-integrity work in `.specs/012-permission-integrity-and-verifiable-authorization.md` is a hard implementation and release prerequisite. The hosted process checks descriptor algorithm `capabilities-v3` before binding a listener and starts from a newly initialized root.

## Demonstrated User Journey

An invited user must be able to:

1. Open the HTTPS landing page and enter a separately delivered token.
2. See the principal, teams, role bindings, descriptor ID, assignment generation, projection epoch, and effective capability rules represented by that token's view.
3. Select a capability and repository path and see whether it is allowed, including the winning allow and deny prefixes.
4. Browse the projected repository tree and visible history without learning hidden repository entries, refs, commits, or object IDs. Capability prefixes shown to the authenticated principal are intentional policy metadata and are not treated as confidential repository entries.
5. Copy a token-free Git remote URL and configure the token through a credential helper.
6. Clone and fetch the same projected view with an unmodified Git client.
7. For a write-enabled persona, push an allowed change and see it appear in the browser after refresh.
8. Attempt an out-of-scope change and receive a stable rejection while repository truth and all published refs remain unchanged.
9. Compare results with another separately authenticated persona to understand that one canonical repository produces different views.

The UI must explain that policy transitions affect future server operations but cannot erase objects already downloaded by a client.

## Deployment Shape

The supported demonstration deployment is deliberately narrow:

```text
Internet
   |
HTTPS reverse proxy (Caddy)
   |
Bastion HTTP process on private loopback/container network
   |
durable local POSIX application volume
   |-- object-store WAL, packs, and descriptors
   |-- SQLite control plane
   `-- disposable Git materializations
```

Requirements:

- one Linux host and one Bastion application process;
- one durable local filesystem supporting symlinks, `flock`, atomic rename, and `fsync`;
- SQLite for the control plane;
- no application replicas, network filesystem, or cross-host failover;
- a stable absolute application path because generated receive hooks name the interpreter and entry point;
- a dedicated non-root account and a forge root inaccessible to other OS users;
- Caddy or an equivalent explicitly tested reverse proxy terminating TLS and forwarding only to the private application listener;
- separate durable proxy state for ACME account and certificate data.

PostgreSQL remains supported by the wider prototype but is not part of this milestone's deployment qualification.

## HTTP Surface

The process serves Git smart HTTP plus a small same-origin web application. All routes not listed here return a generic response without filesystem path resolution.

### Public Routes

- `GET /`: static landing and token-entry application shell.
- `GET /assets/app.css`: static same-origin stylesheet.
- `GET /assets/app.js`: static same-origin application script.
- `GET /healthz`: process liveness only; it performs no repository replay.
- `GET /readyz`: cached readiness result proving the configured root is ready and its current manifest can be loaded.

Public routes disclose no view, principal, policy, ref, or repository existence. Readiness failures return a generic status body and log the internal cause server-side.

### Authenticated Demo API

- `GET /api/v1/session`: current view identity, policy provenance, descriptor assignment, and effective capability maps.
- `GET /api/v1/tree?path_b64=<bytes>&cursor=<cursor>&limit=<n>`: paginated entries reachable from the current projected ref.
- `GET /api/v1/history?cursor=<cursor>&limit=<n>`: paginated commits reachable from the current projected ref.
- `POST /api/v1/decisions`: evaluate one normalized capability and path against the current descriptor.
- `GET /api/v1/credentials`: the authenticated principal's own Git tokens (metadata only) and its views with a `can_push` flag.
- `POST /api/v1/credentials`: mint a `fetch`/`push` token for one of the caller's own views, with an expiry of at most 365 days; returns the secret once.
- `DELETE /api/v1/credentials/<token_id>`: revoke one of the caller's own Git tokens.

Every `/api/v1/*` route requires `ui` scope. Missing, malformed, expired, or revoked credentials return the same `401` JSON error and bearer challenge. A valid credential without `ui` returns `403`; it does not reveal whether another token scope would succeed.

Git credentials are self-service but strictly bounded to the caller's own
principal: a request may only name a view belonging to the authenticated
principal, a `push` scope requires that view's policy to grant a write
capability, and an expired or revoked token is indistinguishable from an unknown
one (`404`) so the route cannot probe for other principals' token ids. The
browser `ui` session token is never offered as a Git credential.

API authentication resolves the token to exactly one view before repository work begins. The trusted materialization layer may reconstruct a missing or stale view from repository truth before the API opens it. After successful materialization, API traversal reads only the completed authorization-safe projected repository. It must never traverse `canonical.git` directly, use canonical object storage as an API alternate, return canonical-only refs or commit IDs, or enumerate another view. A materialization failure returns generic `503` and is logged internally.

`/api/v1/session` may return the authenticated principal's own team IDs, role IDs, binding IDs, descriptor ID, descriptor generation, projection epoch, projected ref, and capability maps. It must not return the canonical frontier OID, token metadata, author email catalog, control-plane leases, jobs, or audit rows.

The successful response envelopes are exact-shape objects:

```text
session  { view_id, principal { id, kind }, team_ids[], role_ids[], binding_ids[],
           descriptor { id, generation, projection_epoch, projected_ref,
                        capabilities, restrictions } }
tree     { projected_tip, entries[] { path_b64, path_display, mode, kind, size },
           next_cursor }
history  { projected_tip, commits[] { oid, parent_oid, author_name_b64,
              author_name_display, author_email_b64, author_email_display,
              authored_at, authored_timezone, committer_name_b64,
              committer_name_display, committer_email_b64,
              committer_email_display, committed_at, committed_timezone,
              message_b64, message_display },
           next_cursor }
decision { capability, path_b64, allowed, longest_allow, longest_deny }
```

Nullable fields are `parent_oid`, `size`, `next_cursor`, `longest_allow`, and `longest_deny`; all other fields are required. Arrays are deterministically ordered and unknown fields are not emitted.

Git paths, policy prefixes, and commit metadata may contain arbitrary bytes. Their wire identity is RFC 4648 URL-safe base64 without padding in fields ending `_b64`; paired `_display` fields are escaped, lossy presentation strings and are never accepted as identifiers. Session capability and restriction maps encode every prefix as `{ "prefix_b64": <bytes>, "prefix_display": <text> }` rather than assuming UTF-8. The tree API returns `path_b64`, `path_display`, mode, object kind, and an integer blob size or `null` for non-blobs. The history API returns only projected commit IDs and the metadata already exposed by those projected commits, with raw author, committer, and message bytes represented losslessly. Neither endpoint adds blob-content download in this milestone.

The decision endpoint accepts exact JSON `{ "capability": <name>, "path_b64": <bytes> }`, with no extra fields. It requires exactly one supported capability and one valid normalized repository-relative byte path. Its response includes `allowed`, `longest_allow`, and `longest_deny`; each non-null match uses the same `prefix_b64` and `prefix_display` object as the session response. It evaluates policy only and never claims that an operation is otherwise valid Git input.

Successful API responses are UTF-8 `application/json` exact-shape objects. Errors use `{ "error": <stable-code> }` and do not include internal exceptions. Tree results are recursively ordered by raw path bytes; history is newest first. Limits default to 50 and may not exceed 100. Query strings, JSON bodies, base64 fields, and decoded paths have explicit byte limits. Hidden and nonexistent tree prefixes return the same `404` error.

An empty decoded `path_b64` addresses the repository root. Pagination cursors are opaque, integrity-protected, bounded in length, scoped to the authenticated view, projected tip, route, and original tree prefix where applicable, and rejected after that tip changes or the process restarts. They contain no raw path, policy, or token data visible to the client.

### Git Routes

The existing route family remains:

- `GET /<view>.git/info/refs?service=git-upload-pack`
- `GET /<view>.git/info/refs?service=git-receive-pack`
- `POST /<view>.git/git-upload-pack`
- `POST /<view>.git/git-receive-pack`

Fetch requires `fetch` scope. Receive advertisement and receive RPC require `push` scope in addition to repository policy authorization. A token for another view and an unknown view remain indistinguishable.

## Browser Application

The browser is a small static HTML, CSS, and JavaScript application served by Bastion. It has no third-party scripts, remote fonts, analytics, or cross-origin requests.

The authenticated screen contains:

- a principal and view summary;
- a team, binding, and role provenance panel;
- a capability matrix listing all allow and deny prefixes;
- an interactive path-decision inspector that highlights the two longest matching prefixes and the winner;
- a projected tree browser;
- a visible-history list;
- Git clone and credential-helper instructions without embedding the token in a URL;
- a concise explanation of projection epochs and non-revocable downloads.

Tokens are held in page memory only. They are never placed in the URL, persisted in `localStorage` or `sessionStorage`, written to cookies, included in telemetry, or rendered back into the DOM after authentication. The token input is cleared immediately after authentication. Reloading the page logs the browser out.

Repository paths, policy labels, author fields, and commit messages are untrusted display data. The application renders them only through DOM text APIs, never through HTML interpolation. Control bytes use visible escapes, malformed text cannot alter page structure, and no displayed value becomes a URL without component-safe encoding.

The application is usable on current desktop and mobile browsers. It includes keyboard-visible focus, semantic labels, sufficient contrast, and layouts that do not require horizontal page scrolling on a phone.

Responses set `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`, `Referrer-Policy: no-referrer`, and `X-Content-Type-Options: nosniff`. Scripts and styles are external same-origin files; the policy does not permit `unsafe-inline` or `unsafe-eval`. CORS is disabled. Authenticated API and Git responses use no-store cache controls.

## Token Model

`tokens.json` is removed from the hosted format. Token records live in a versioned control-plane table so issue or revoke and its audit row commit in one database transaction. The new-root requirement means no token-file migration is needed; startup rejects a hosted root containing legacy raw-token state.

The complete credential has this ASCII grammar:

```text
bst1.<token-id>.<secret>
```

`token-id` is 16 random bytes encoded as 22 unpadded base64url characters. `secret` is 32 random bytes encoded as 43 unpadded base64url characters. Parsers accept only `^bst1\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}$` and impose the exact length before decoding.

The persisted exact-shape record is keyed by token ID and contains:

```text
token_id
secret_digest
view_id
scopes[]          # fetch, push, ui
created_at
expires_at        # nullable
revoked_at        # nullable
```

Timestamps are nonnegative integer Unix seconds; a token is expired when `now >= expires_at`. Scopes are a sorted, unique, nonempty subset of `fetch`, `push`, and `ui`.

The digest is the 64-character lowercase hexadecimal encoding of `SHA256(b"bastion-http-token-v1\0" + token_id_ascii + b"\0" + secret_ascii)`. Authentication uses constant-time comparison of decoded digest bytes, including a dummy comparison for unknown well-formed token IDs. Raw secrets are returned once at issuance and are never persisted or logged.

CLI support includes:

- `add-token <root> <view> --scope ... [--expires-in <duration>]`;
- `list-tokens <root> [--view ...]`, which never prints a digest or secret;
- `revoke-token <root> <token-id>`.

`--expires-in` accepts one positive integer followed by `m`, `h`, or `d`. When no `--scope` appears, issuance defaults to `fetch` and `ui`. When at least one appears, the record contains exactly the supplied scopes, so `push` is always explicit. Revocation and expiration take effect on the next request. Rotation is issue-new then revoke-old and does not need a separate command.

Bearer authentication is accepted for the API and Git. Basic authentication remains available for Git clients, but its username must equal the routed view and its password is the complete issued credential. Documentation must not recommend token-bearing URLs. It configures `credential.useHttpPath=true`, uses a non-plaintext credential helper or an interactive prompt, and verifies that two views on one host do not reuse each other's credentials.

Backups contain only token digests and metadata in the same pinned control-plane snapshot as token audit rows. Token issue and revoke transactions append audit events without secrets; either both token state and audit commit or neither does.

## HTTP And Process Boundary

The application remains defense in depth behind the reverse proxy. It must not rely on the proxy for correctness.

### Request Framing

- Reject requests containing both `Content-Length` and `Transfer-Encoding`.
- Accept only a valid decimal nonnegative `Content-Length` or an exact supported chunked encoding.
- Validate chunk syntax, CRLF boundaries, short reads, trailer count, trailer bytes, and decoded total.
- Apply header, encoded-body, decoded-body, chunk-count, and request-duration limits.
- Stream request bodies to Git in bounded chunks rather than assembling them in process memory.
- Stream gzip decompression with decoded-size and expansion-ratio limits; never call an unbounded whole-body decompressor.
- Stream Git stdout to the client with bounded buffering and drain bounded stderr concurrently.

The reverse proxy applies equal or lower public body, header, and idle-time limits and rejects ambiguous framing before forwarding. Application limits remain authoritative. Per-IP and per-token rates are enforced in the application because the standard Caddy build is not assumed to contain third-party rate-limit modules.

### Concurrency And Subprocesses

- Bound total active requests and active Git subprocesses with explicit semaphores.
- Bound concurrent receive operations more tightly than fetches.
- Apply per-token and per-client-IP request and receive limits in the application. Trust a forwarded client address only from the configured private proxy peer.
- Start each Git RPC in its own process group.
- On timeout, client disconnect, or shutdown, terminate the process group, wait for a grace period, then kill it.
- Never return raw Git stderr or Python exception text to an HTTP client.

Authentication and route validation happen before repository replay, materialization, or subprocess creation.

### Git Outcomes

Policy, metadata, resource-limit, and stale-view rejections use HTTP `200` when Git receive-pack successfully returns an `ng` protocol result. Only stable allowlisted messages and categories from `ForgeError` may enter that result. Malformed HTTP is `400`, authentication is `401`, scope failure is `403`, unknown or cross-view routing is `404`, per-token or per-IP throttling is `429`, global saturation and read-only receive advertisement are `503`, and internal or integrity failure before response headers is a generic `500` or `503`. A Git timeout before response headers is `504`; after a streamed `200` has begun, timeout or child failure terminates the response and connection because its status can no longer change. Arbitrary subprocess stderr and exception text are neither returned nor logged. Logs contain an allowlisted category, exception class, request ID, and operator-safe context only.

### Shutdown

SIGINT and SIGTERM stop accepting new work, fail readiness, and drain active requests for a configured grace period. In-flight receives may finish within that period. After the deadline, their process groups are terminated. The process exits only after listener closure and child reaping.

## Filesystem And Configuration

Initialization creates the forge root and secret-bearing children with restrictive modes under `umask 077`. Startup refuses to serve when the root is group- or world-accessible unless an explicit development-only override is set.

The hosted command accepts environment fallbacks for the root, host, and conventional `PORT`; explicit CLI flags win. All resource limits used by HTTP and receive validation are documented in one operator reference with defaults and units. Invalid or unsafe values fail startup.

The demo uses non-sensitive repository contents. Anyone who can read the persistent volume remains able to read canonical storage and is inside the trusted boundary.

The qualified hosted defaults are a maximum manifest sequence of 1,000, maximum initial fixture pack of 16 MiB, maximum receive body of 32 MiB, 100 commits per push, 5,000 quarantined objects per push, 4 MiB per object, 64 MiB aggregate expanded object bytes per push, 1 GiB free-space floor, and 512 MiB receive/reprojection reserve. The hosted profile supports the fixture's four active views; another view requires a newly qualified reserve.

Before spawning receive-pack, the service requires free space above the floor plus reserve and valid materializations for every active view. It rechecks aggregate expanded bytes during quarantine inspection and free space before immutable upload. The publication loop rejects a candidate whenever the current manifest sequence is already 1,000, so concurrent requests cannot exceed the budget. Receive advertisement returns `503` with stable code `demo-read-only` when the sequence or initial free-space check fails; an already advertised RPC that reaches a limit returns the allowlisted `ng` category through receive-pack.

At a read-only threshold, fetch and UI may continue from already valid materializations. If a requested view is stale or missing and cannot be reconstructed without crossing the floor, readiness fails and that request returns generic `503`; the service does not promise cache reconstruction on an exhausted volume. These bounds limit replay work and cumulative immutable growth for the intentionally small fixture. Safe general-purpose pack, WAL, and materialization garbage collection remains future work; operators replace the demo volume before its documented event budget is exhausted.

## Observability

Application logs are structured JSON on stdout/stderr and include timestamp, request ID, route class, method, status, duration, authenticated token ID when available, view when authorized, byte counts, and Git service outcome. Logs never include authorization headers, raw tokens, request bodies, repository content, or raw internal exceptions.

Committed receive audit rows are recoverable from WAL and may be reconciled after a crash. Rejected receive audits are best-effort observability because no cross-store transaction can atomically bind an uncommitted rejection to SQLite; an audit-write failure is logged but does not change the Git result. Where present, receive audits retain principal, view, transport, remote address, stable outcome category, and committed event ID. Reverse-proxy forwarding of client addresses is trusted only from the configured private proxy peer.

The milestone exposes liveness and readiness but not a public metrics endpoint. Operators use structured logs, `forge.py stats`, `forge.py audit`, scheduled `forge.py check`, and backup status. A production metrics and alerting system remains future work.

## Demonstration Fixture

Provide a deterministic operator tool that initializes an empty root with a small repository containing at least:

```text
README.md
shared/guide.md
teams/red/plan.md
teams/red/private/notes.md
teams/red/private/release/checklist.md
teams/red/private/release/drafts/internal.md
teams/blue/plan.md
ops/runbook.md
```

The fixture creates users, one service account, teams, roles, bindings, views, and descriptors that demonstrate:

- public versus team-only reads;
- a broad allow with a nested deny;
- a narrow allow exception inside that deny;
- a still-narrower deny winning over an exception;
- read-only versus path-scoped create, modify, delete, mode, and symlink rights;
- direct and team-derived role bindings;
- different projections of one canonical history;
- a policy transition producing a parentless projection epoch.

The fixture's stable personas and expected final views are:

| View | Principal | Kind and owner | Declared author email | Membership | Expected visible repository data |
| --- | --- | --- | --- | --- | --- |
| `red` | `red-dev` | user | `red@example.test` | `red-team` | `README.md`, `shared`, `teams/red/plan.md`, and the release checklist; not private notes or release drafts |
| `blue` | `blue-dev` | user | `blue@example.test` | `blue-team` | `README.md`, `shared`, and `teams/blue`; no red or ops data |
| `audit` | `auditor` | user | `auditor@example.test` | none | public and both team plans; no ops or private team data; no writes |
| `release` | `release-bot` | service account owned by `red-dev` | `release@example.test` | none | the red release checklist; no sibling private notes or release drafts |

Stable roles include `public-reader`, team-bound red and blue readers/writers, `red-release-exception`, direct `auditor-reader`, and direct `release-writer`. The red policy denies `teams/red/private`, allows the narrower `teams/red/private/release`, and denies the still-narrower `teams/red/private/release/drafts`. The fixture creates the red view before binding `red-release-exception`, then performs `transition-principal` so its final assignment has generation 2, projection epoch 2, and a parentless epoch tip.

Required decision examples are: red may read the release checklist, red may not read private notes or release drafts, release-bot may modify the checklist, and auditor may not modify any path. With the listed author configuration, modifying `teams/red/plan.md` is the red persona's accepted push and modifying `teams/blue/plan.md` is its rejected push.

The tool creates no hard-coded or committed credentials. Operators issue expiring persona tokens separately after initialization. It refuses a nonempty target. Resetting the public demonstration means replacing it with a newly initialized volume; there is no destructive in-place reset command.

## Packaging And Operations

Add and maintain:

- project metadata declaring the supported Python version and executable entry point;
- a container image with pinned Python and Git compatibility targets;
- a non-root runtime user and stable application path;
- a container health check;
- a demo Compose file with Bastion, an explicitly pinned standard Caddy image, a private network, one application-data volume, and separate Caddy data/config volumes;
- a Caddy configuration for HTTPS, private upstream routing, request normalization, body and timeout limits supported by that pinned build, forwarding-header replacement, and security headers;
- an environment example containing no credentials;
- a deployment runbook covering DNS, first initialization, token issuance, startup, upgrade by replacement, backup, restore drill, log inspection, invariant checks, and rollback.

The image contains no initialized forge, token, private key, or demo credential. Application upgrade never initializes or silently rewrites an existing persisted format.

Backups are written while the demo is quiesced to a separate restricted destination, so the pinned object-store generation and control-plane snapshot include consistent token state. Platform or operator-owned backup encryption is recommended but is outside Bastion's implementation and test contract. The runbook includes a restore drill into a throwaway volume and an authenticated `git ls-remote` check against the restored service.

## Verification

CI and local release qualification run:

- Python compilation and the complete existing unit suite;
- the permission-integrity tests from spec 012;
- startup rejection of pre-spec-012 repository and descriptor formats before listener bind;
- token grammar, digest, transactional issue/audit, scope, expiry, revocation, backup, and redaction tests;
- `fetch`-only and `push`-only credentials being unable to call any demo API;
- authenticated API tests proving each persona sees only its own policy and projection;
- direct hidden-path, hidden-object, unknown-view, cross-view token, pagination, cursor-integrity, restart-staleness, and tip-staleness probes;
- unusual non-UTF-8 and control-byte paths and metadata round-tripping through base64 without display-string confusion;
- strict request-framing, compressed-body, slow-client, body-limit, concurrency-limit, timeout, and child-process cleanup tests;
- stable Git protocol and HTTP outcomes for policy, metadata, limit, conflict, timeout, saturation, and integrity failures;
- browser smoke tests at desktop and mobile viewport sizes, including hostile path and commit metadata XSS probes;
- a built-container test that runs as non-root against a persistent volume;
- a reverse-proxy HTTPS test for clone, fetch, permitted push, rejected push, immediate token revocation, spoofed forwarding headers, security headers, and framing limits;
- two path-scoped Git credentials operating sequentially against different views on the same host without collision;
- aggregate expanded-object rejection, receive-space reservation, event-budget enforcement under concurrent publication, and low-free-space transitions, including `503` for a stale view that cannot be rebuilt;
- bounded RSS and cumulative disk-growth measurements for the qualified four-view fixture and event budget;
- restart persistence, graceful SIGTERM, backup, restore, and post-restore clone tests;
- a log scan proving issued credentials and authorization headers never appear.

The PostgreSQL matrix is not required for this milestone. Supported Python and Git versions are recorded in build output and the compatibility documentation.

## Non-Goals

- anonymous public write access;
- production OAuth, passkeys, SSO, or enterprise identity lifecycle;
- production SSH transport;
- multiple application replicas or multi-host leases;
- remote object storage or stateless application instances;
- arbitrary branches, merges, force pushes, tags, signed artifacts, Git LFS, or SHA-256 repositories;
- blob-content browsing, search, blame, archives, pull requests, reviews, status checks, webhooks, or CI execution;
- a general administration API for editing principals, teams, roles, or bindings;
- retroactive revocation of already downloaded Git objects.

## Acceptance

- A fresh operator can follow the runbook from empty volume to a valid HTTPS clone without editing source code.
- The same authenticated persona sees consistent policy, tree, and history through the browser and Git.
- Seeded personas visibly receive different object-safe projections of one canonical repository.
- Policy metadata is visible only with `ui` scope, while arbitrary Git path and metadata bytes round-trip without becoming executable browser content.
- A write-enabled token can change only paths allowed by its captured descriptor; an out-of-scope push leaves repository truth unchanged.
- Tokens are scoped, expiring, revocable, hashed at rest, transactionally audited, absent from URLs, and absent from logs.
- Request memory, concurrency, subprocess lifetime, event count, qualified fixture growth, and shutdown behavior are explicitly bounded and adversarially tested.
- Restart preserves committed data, and backup restore produces a cloneable equivalent deployment.
- The service runs as one non-root process behind HTTPS with a durable local POSIX volume and makes no production or multi-tenant security claim.
