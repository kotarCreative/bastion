# Multi-Repository Accounts

Status: local operator foundation and the persistent-local served slice below
are implemented. Self-service history import and production adapters remain
unimplemented; production rollout is gated on milestone 4.

## Contract

An approved account may create multiple repositories without seeking approval
for each repository. Each repository has independent history, principals,
views, permissions, and HTTPS Git credentials. Account approval is a host-level
onboarding decision, not a repository capability. Repository ownership never
grants deployment administration or unrestricted account approval.

Keep one existing forge root per repository. The host module owns account
approval, discovery, name reservation, and provisioning. Repository WAL truth
continues to own effective per-repository authorization. Neither a discovery
record nor a host session authorizes reading canonical Git objects directly.

Non-goals: open signup, SSH, forks, transfers, arbitrary multi-branch pushes,
and changing the existing projection/WAL publication protocol implicitly.

## Identity, Approval, And Membership

Host accounts have immutable account identifiers, an immutable URL owner name,
verified/approved email identities, and an optional unique Clerk user link.
An operator can approve an account before its first Clerk sign-in. A later link
must use an unambiguous verified email and must never replace an existing Clerk
link merely because the account's email changes.

An operator or an explicitly authorized onboarding invitation approves account
creation rights. An ordinary repository collaborator invitation grants only
access to that repository; it cannot silently approve repository creation or
confer host administration. Every authenticated host operation rechecks account
approval, including requests carrying a previously issued host session. Approval
is checked again when a provisioning operation becomes ready. Suspension denies
host discovery, new host sessions, creation, and repository-session exchange;
its effect on already issued repository tokens
must be implemented explicitly before suspension is exposed as a host feature.

Host records:

- `accounts`: `account_id`, `owner_name`, `clerk_user_id` (nullable), `emails`,
  `approved_at`, `created_at`, and lifecycle state.
- `repos`: opaque `repo_id`, `owner_account_id`, canonical `name`, `branch`,
  `source_kind`, immutable source identity, request id, creation time,
  provisioning state, and the underlying forge's `repository_id` once known.
- `memberships`: `(repo_id, account_id)` mapped to the repository's
  `principal_id`, with discovery/lifecycle state. The owner has one too.

Principal ids and view names are repository-local. Two repositories may both
have an `owner` view without representing the same authorization assignment.
The host repo id need not equal the forge's generated repository id; record and
verify both instead of changing `init_forge` solely to force equality.

Membership is an indexed discovery hint, not an authorization grant. Before
listing an accessible ready repository or exchanging a repository session,
validate that the mapped principal and Clerk link agree and that it has an
active view in the repository's committed WAL. Corrupt mappings fail closed.
Nonexistent and inaccessible repositories share a non-disclosing response.

Invitations need a recoverable intent linking the host account to the local
principal. Publish local access through existing repository machinery, then
activate discovery. A reconciler repairs an interrupted update; it must not
invent permissions. Removal disables discovery/session exchange immediately,
and repository policy/token revocation supplies the actual access fence.

## Layout And Storage Isolation

Local phase-1 layout:

```text
<host>/
  host.sqlite3             # durable accounts, registry, memberships, reservations
  locks/catalog.lock       # serialize first-time catalog initialization
  locks/<repo-id>.lock      # process-scoped provisioning serialization
  locks/<repo-id>.owned     # durable reservation/root-identity marker
  repos/<repo-id>/          # independent existing forge root
    backend.json
    repository.json
    store.sqlite3
    object-store/
    views/ intake/ materializations/
```

Only validated opaque ids form disk paths; owner/repository names do not.
Reject traversal, symlink substitution, and roots escaping the host. Do not
discard a preexisting directory to make a reservation succeed.

Phase 1 explicitly supports durable local SQLite and per-root filesystem object
storage only. Ambient remote backend/object-store configuration must not cause
multiple roots to share a catalog or manifest. Reject unsupported configuration
rather than silently falling back to a different deployment model.

Production isolation is a prerequisite for production multi-repo support:

- Cloudflare: a durable host catalog plus one catalog Durable Object per
  repository, keyed by host identity and immutable repo id. Container outbound
  routing must select that catalog explicitly. Today the default adapter routes
  every root to the container-keyed singleton catalog; separate directories do
  not isolate it. Each repository retains an independently recoverable config,
  WAL/object namespace, and token store.
- PostgreSQL: independent scoped repository databases, as the existing adapter
  provisions; the host catalog needs its own durable scope.
- S3/Artifacts: preserve each repository's object namespace and manifest CAS.
  Host records never become a second commit point for repository WAL events.

Current Cloudflare catalog mutations persist directly in Durable Object SQL.
Artifacts snapshots are a legacy migration mechanism, not the ongoing host
catalog persistence design. Local host SQLite must not be placed on ephemeral
container disk and described as durable.

## Authentication And Routing

Two distinct session types are required:

1. **Host session:** identifies an approved account independently of any
   repository/view. Clerk exchange succeeds for an approved account with zero
   repositories and permits host discovery/creation. Use short-lived,
   revocable, digest-only credentials with a distinct format/domain from `bst1`
   repository tokens. Host sessions are not Git credentials.
2. **Repository UI session:** an explicit exchange selects a ready repository
   and one of the account's active views after membership/WAL checks. Issue an
   existing `bst1` token with `ui` scope into that repository's token store.

Host routes: `POST /api/v1/auth/clerk`, `POST /api/v1/logout`,
`GET/POST /api/v1/repos`, and
`POST /api/v1/repos/<repo_id>/auth/session`. Logout revokes the presented host
session; repository logout remains scoped to its presented UI session.

Repository data routes use `/api/v1/repos/<repo_id>/...`, validate repository
UI credentials in the selected store, then delegate to the existing app. A
host credential, or a token from repository A, cannot authorize repository B.
Caller-supplied principal/view headers never substitute for token validation.

Git routes use `/<owner_name>/<repo_name>.git/...`. Resolve the repository from
the URL first; authenticate the existing token in that repository only; derive
the internal view from the authenticated record and verify its active WAL
assignment. Define Basic usernames as a validated local view hint (or omit the
hint for Bearer); never treat the URL owner as the authenticated collaborator.
Internal rewriting to `/<view>.git` must preserve these checks and avoid
logging credentials or scanning every repository's tokens to find a match.

Existing `bst1` credential grammar and repository token record schema remain
unchanged. Repository context comes from the selected URL/store, not a new
`repo_id` field in the token. An account-wide Credentials screen aggregates only
authorized repositories and creates/revokes credentials within one selected
repository; it does not expose browser session secrets.

## Names And Compatibility

Owner/repository URL names are canonical lowercase ASCII slugs, 1–64 characters,
starting with an alphanumeric character and otherwise containing alphanumeric
characters or hyphens. Reject noncanonical input, traversal, `.git` suffixes,
and reserved host route names. Names are unique in their respective namespace;
repository uniqueness is `(owner_account_id, name)`, enforced in storage.
Do not introduce rename semantics or case-folding aliases in the first release.

Keep existing direct-root CLI commands and `serve-demo <root>` unchanged. A new
host command is explicit; it must not guess whether an existing directory is a
host or a forge. Legacy `/<view>.git` URLs remain supported in single-root mode;
host mode does not ambiguously redirect them. A later explicit migration must
preserve repository ids, WAL, identity links, token digests, and permissions,
and supply backup/rollback checks before deleting or moving existing data.

## Creation, Retries, And Publication

Provisioning is an explicit operation with an idempotency/request id:

1. Validate the approved actor, names, branch, source, and budgets. Atomically
   reserve `(owner, name)`, request identity, repo id, and capacity in the host
   database. A reused request id with different immutable inputs is a conflict.
2. Serialize work for the reserved repo across processes. Locks release on
   process exit; a stale worker cannot publish another operation's reservation.
   Keep database transactions short rather than holding one during Git work.
3. Initialize/resume the reserved root from the recorded source identity.
   Imported source identity pins the branch tip, not just a mutable pathname.
   Recovery after a source changes must refuse rather than import different
   content under the original request id.
4. Idempotently provision the owner's user principal, approved emails/Clerk
   link, root grants for all supported capabilities (including read, writes,
   and admin), role binding, and active owner view.
5. Verify root state, repository identity, committed owner assignment, and
   owner grants before atomically publishing the owner membership and `ready`
   registry state. Recheck account approval and the reservation's identity.

States distinguish reserved/provisioning, ready, and failed. Non-ready entries
are visible only as operations to their actor/operator, never routable as ready
repositories. Lost success responses return the same ready result on retry.
Failures retain diagnosable state and resumable reservations. Recovery never
deletes unfamiliar data or treats an ambiguous storage failure as proof that
publication did not happen. Capacity reservations must count in admission.

## Empty Repositories And Existing History

The current engine requires a real canonical commit and forbids branch creation
and force/non-fast-forward updates. A truly unborn repository and first push of
unrelated existing history are therefore **not** phase-1 behavior.

Phase 1 offers:

- **Import:** operator supplies a local Git source with a supported branch.
  Import preserves the canonical source history under existing `init_forge`
  semantics; subsequent visible history is the normal deterministic projection.
- **Seeded empty:** initialize a deterministic empty-tree seed commit on the
  configured branch, then use normal provisioning. Users clone that projection
  and commit/push on top of it. Documentation must state that pushing unrelated
  local history is not supported by this mode.

Later self-service upload/import must use private, bounded staging and validate
object format, selected branch, object integrity, supported tree modes, Git
execution environment, unpacked size, and time/disk limits before publication.
Do not accept arbitrary server filesystem paths or user-controlled executable
Git configuration through the web interface.

Existing history with multiple author emails goes through import, not a bypass
of normal receive author binding. New pushed commits continue to require the
principal's approved emails. UI clone instructions must surface this requirement.
True unborn first-push support, if added, requires explicit changes to manifest,
projection, receive, author-validation, and retry contracts with tests; a seed
commit is not advertised as a substitute for that behavior.

## Limits And Acceptance Tests

Approved accounts may create multiple repos up to explicit operational budgets.
Reserve host/account capacity transactionally before provisioning. Preserve
existing per-repository receive limits and free-space checks; do not invent an
implicit default limit of one repository per account. Host-wide request/import
budgets must be in place before exposing self-service HTTP provisioning.

Required acceptance coverage, phased with the interfaces:

- Approved account with zero repositories can authenticate and create; unknown
  or unapproved account cannot. Ordinary collaborator invitation does not grant
  host creation/admin rights.
- Host-session expiry, logout revocation, digest-only storage, and suspension
  with an already issued session fail closed. Host/repository credentials cannot
  be substituted for each other, including when a caller supplies view headers.
- Two repositories with identical local principal/view names have independent
  catalogs, histories, policies, and tokens; A's token is rejected by B.
- Owner full read/write/admin assignment and declared authors are correct.
- Two owners may reuse a repository name; the same owner cannot concurrently
  reserve it twice. Request-id reuse with changed inputs is rejected.
- Lost response, injected failure at each provisioning stage, retry, and process
  restart produce one ready repo/membership without destructive cleanup.
- Membership discovery, stale mapping, invitation interruption, and revocation
  never bypass active repository WAL authorization.
- Seeded clone/commit/push works; unrelated history gets a documented refusal;
  import retains its pinned canonical history and detects mutable-source retry.
- Traversal, symlinks, oversized/malformed imports, source changes, and unsupported
  backend configuration fail closed.
- Production cold start recovers host and isolated repository catalogs; migration
  preserves old credentials and policy. Unsupported deployment modes are never
  silently enabled by local tests.

## Milestones

1. **Local host foundation (start here):** host SQLite schema and module,
   operator account approval, explicit `create-repo`/`list-repos` CLI, imported
   and seeded-empty provisioning, owner membership, reservation/recovery tests,
   and local-only configuration safeguards. No new hosted signup/routing claims.
2. **Host authentication and dispatch:** separate host sessions, repository UI
   exchange, scoped HTTP/Git routing, account discovery, and SPA repo switcher.
3. **Self-service workflows:** bounded upload/import, creation UI, recoverable
   invitations/memberships, credentials per repo, and host admission budgets.
4. **Production adapters and migration:** explicitly isolated durable catalogs,
   storage/recovery and backup contracts, deployment support, and migration
   tests. Multi-repo production rollout is blocked until these pass.

### Implemented Local Interface

`src/host.py` supplies the local host module. `forge.py` exposes
`approve-host-account` (optional per-account `--repo-limit`),
`link-host-account`, `create-repo` (`--source` or `--seed-empty`, with a required
`--request-id`), and `list-repos`. The latter is operator inventory, including
non-ready operations, not authorization-filtered account discovery. Clerk
linking through the CLI is an operator assertion of a verified identity; it is
not itself verification of a remote Clerk session.

The implemented budgets include optional per-account repository counts and a
persisted total-host count (default 1000, configured with
`BASTION_HOST_REPO_LIMIT` at catalog initialization/migration). Reservations and
failed operations count. Served admission is process-wide; distributed/import
budgets remain rollout work. `tests/test_host.py` covers the local foundation;
the remaining production acceptance cases apply when those interfaces arrive.

### Implemented Served Slice

An explicit `serve-host <host-root>` command serves the existing browser shell
and isolated repository apps on one port, using persistent local storage. It
does not change single-root mode or enable the production adapters implicitly.

The first served vertical slice includes:

- Clerk exchange to a host session, including an approved account with no repos.
  Host-session authentication never accepts a repository credential.
- `GET /api/v1/repos` returns only ready repositories for which the account's
  membership passes local identity and committed-view verification. Non-ready
  operations are not mixed into this discovery list.
- `POST /api/v1/repos` accepts `name`, `request_id`, and an optional `branch`
  (default `main`). The owner is always the authenticated account. This endpoint
  creates a seeded-empty repo only; filesystem paths, object-store/backend
  settings, and arbitrary owner identifiers are not request fields. Imported
  existing history remains operator CLI work until bounded upload is ready.
- `POST /api/v1/repos/<repo_id>/auth/session` accepts an optional `view_id` and
  exchanges a host session for a repository UI session. Multiple active views
  require explicit selection. The response includes the selected repository's
  public name, scoped Git URL, and the caller's declared author emails.
- Scoped data, credentials, permission, and invitation routes preserve existing
  repository checks. Scoped Clerk-login/config routes are not exposed as a
  bypass of host approval/membership. Git routes validate credentials in only
  the selected repository and preserve fetch/push scope and admission checks.
- Collaborator invitations in this slice target already approved host accounts.
  They create a recoverable membership intent, publish local access through the
  existing fenced invitation operation, then activate discovery. A retry or
  reconciliation may repair discovery after publication; it cannot grant
  additional policy or overwrite a different principal's identity. Inviting an
  unknown/unapproved host account does not approve that account implicitly.
- The browser retains separate host/repository credentials in memory only. It
  supplies each only to its appropriate route, clears repository data during
  switches, and discards late responses from the previous repository/session.
  A zero-repository account can create its first repo without a fake view.
  Creation/switcher UI and per-selected-repo credentials/permissions are part
  of this slice; account-wide credential aggregation can follow later.

Global host request, Git/receive, per-source/per-credential, and provisioning
admission must be enforced across repository apps, not reset by switching a URL.
Configure a total-host repository budget as well as existing per-account
capacity; all reservations count, and unrelated failures do not reset budgets.
Bound JSON parsing before allocating provisioning resources and do not expose
internal paths, storage diagnostics, Clerk secrets, or operation hashes through
discovery/creation responses. App caching is bounded and does not cache approval
or membership as permanent authority.

`src/host_server.py` supplies the explicit local dispatcher. Host schema v3 adds
digest-only sessions, persisted host capacity, and membership intents; supported
older schemas migrate atomically under the catalog lock. Serving does not create
an unknown host root. Host sessions expire after one hour; repository UI exchange
also issues one-hour `ui`-only tokens. Creation returns 201 on both first success
and successful idempotent retry, with the same public repository record.

`BASTION_MAX_ACTIVE_PROVISIONS` defaults to 2; request/Git/receive limits are
shared across child apps within one host process. These are not distributed
semaphores or production isolation claims. Host JSON is bounded at 4 KiB and
scoped mutations at 8 KiB; Git RPCs retain their existing transport limits.
Invitations support approved Clerk accounts and verified-email selection;
ambiguous or incomplete directory matches are refused. Reconciliation repairs
discovery after local publication, never recreates removed views, and cannot
reactivate an explicitly disabled membership. Deleting a membership row is not
a revocation operation: a pending intent may repair it. Explicit discovery
disable and repository policy/token fences must be coordinated by operators
until a tested removal/deprovisioning workflow exists.

Served acceptance tests in `tests/test_host_server.py`,
`tests/test_host_server_review.py`, and the SPA suites exercise two accounts owning multiple repos,
account A never listing B's private repos, zero-repo onboarding/creation, a
shared scoped collaborator view in exactly one repo, host/repo credential
separation, expired/revoked/suspended host sessions, cross-repo Git/API rejection,
seeded smart-HTTP clone/commit/push, invitation repair, stale browser responses
across switches/logout, and existing single-root behavior.
