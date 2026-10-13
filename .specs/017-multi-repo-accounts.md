# Multi-Repository Accounts

Status: local hosting and the Cloudflare multi-repository adapter are implemented,
including bounded single-branch first-push import and disk-independent recovery.
Live deployment validation, whole-host backup/migration, distributed admission
and coordinated deprovisioning remain rollout gates in milestone 4.

### Cloudflare integration

`CloudflareHost` uses the existing parameterized DO SQL bridge for a distinct host
catalog and one scoped catalog per opaque repository ID. Repository objects/WAL
are isolated in Artifacts branches. Initial intake is saved under a digest-pinned
host key before source validation receipts, so cold recovery revalidates exactly
the same bytes. Source identity is independent of the replacement disk's inode.
Ready repositories recover from their own catalogs/WAL; retries never re-seed
them. Host/admin sessions, repo UI tokens, Git tokens and import credentials are
separate scopes. Only the persisted initial host administrator may approve
accounts through the browser; repository owners cannot grant host privileges.

The production entry point and local Docker example use the host dispatcher.
The old single-repository browser demo and sample bootstrap are removed. Legacy
Cloudflare catalog names/branches are not deleted or automatically adopted.
Tests exercise actual workerd SQL and an authenticated Artifacts HTTPS fixture,
not a live production deployment. All Artifacts branches currently share one
backing repository quota; request serialization is single-host, not distributed.

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

Keep existing direct-root administration CLI commands. The single-repository
browser demo entry point is retired by the Cloudflare multi-repo integration. A new
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

The ordinary engine requires a real canonical commit and forbids branch creation
and force/non-fast-forward updates. The local foundation originally offered only
operator import and seeded-empty creation; normal pushes still cannot replace
that seed with unrelated history.

Supported source modes:

- **Import:** operator supplies a local Git source with a supported branch.
  Import preserves the canonical source history under existing `init_forge`
  semantics; subsequent visible history is the normal deterministic projection.
- **Seeded empty:** initialize a deterministic empty-tree seed commit on the
  configured branch, then use normal provisioning. Users clone that projection
  and commit/push on top of it. Documentation must state that pushing unrelated
  local history is not supported by this mode.
- **First-push import:** reserve an uninitialized host operation, authenticate the
  owner's initial Git push, and initialize from its validated complete history.
  No unborn manifest/view or seed commit is published. The separate bootstrap
  receiver creates an ordinary ready forge only after validation/provisioning.

Self-service first-push import uses private, bounded staging and validates
object format, selected branch, object integrity, supported tree modes, Git
execution environment, unpacked size, and time/disk limits before publication.
Do not accept arbitrary server filesystem paths or user-controlled executable
Git configuration through the web interface.

Existing history with multiple author emails goes through import, not a bypass
of normal receive author binding. New pushed commits continue to require the
principal's approved emails. UI clone instructions must surface this requirement.
The implemented first-push contract below explicitly separates bootstrap from
ordinary manifest/projection/receive author validation and tests its retries.
Seeded creation is not advertised as a substitute for that behavior.

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
failed operations count. Served admission is process-wide; distributed
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
   settings, and arbitrary owner identifiers are not request fields. Existing
   history uses the separate bounded import routes below or the operator CLI.
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

`src/host_server.py` supplies the explicit local dispatcher. Host schema v5 retains
digest-only sessions, persisted host capacity, and membership intents and adds
durable import operations, staging reservations, ownership receipts, and digest-only
first-push credentials; supported
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

### Implemented Git-Native First-Push Import

The dashboard accepts a new repository name and selected branch. It reserves an
uninitialized repository and issues an expiring import credential. The owner
then pushes directly from an existing local Git repository:

```sh
git remote add bastion https://bastion.example/alice/project.git
git push bastion main
```

No client bundle, archive, or file upload is required. Only one SHA-1 branch
and its complete reachable history are imported. Remote URL cloning, SSH,
multiple branches/tags, SHA-256, submodule history, and external LFS objects are
not part of this slice. Historical authors need not match the importer; this
exception applies only to initialization. All subsequent writes use normal
repository credentials, author binding, ancestry, and path permissions.

Host-authenticated routes (never repository UI/Git/import credentials):

- `POST /api/v1/imports`: JSON containing exactly `name`, `branch`, `request_id`.
  Atomically reserve the approved actor's name/request and account/host capacity.
  Return 201 `{import: {import_id, name, branch, state, repo, git_url, username}}`;
  `repo` is null before ready and `username` is `owner`. Identical retries return
  the same operation; changed inputs conflict with imports and seeded creation.
- `POST /api/v1/imports/<id>/credentials`: bounded `{}`. Return 201
  `{credential, expires_at, username}` once; only a domain-separated digest is
  persisted. A replacement invalidates the previous credential. One credential
  record per operation bounds rotation storage. Already-ready operations refuse
  issuance. This is not a host session or a normal repository credential.
- `GET /api/v1/imports/<id>`: owner-only status, unknown/foreign operations both
  404. States: `awaiting-push`, `uploaded`, `validating`, `provisioning`, `ready`,
  `failed`. Errors are generic; no internal paths, diagnostics, digests, or OIDs.
- `GET /api/v1/imports`: the actor's pending/failed first-push operations, bounded
  by the host repository count and a 1000-record response cap. The dashboard can
  resume instructions/status after refresh and issue a replacement credential;
  raw secrets are never recovered or stored in browser persistence.

Git bootstrap routes use only `bsi1` import credentials and bind the account,
repository namespace, and Basic username (`owner`; Bearer is also supported):

- `GET /<owner>/<repo>.git/info/refs?service=git-receive-pack`: advertise an empty
  ref set with bounded supported capabilities. A ready operation advertises
  only its original imported ref for lost-response acknowledgements, never the
  current canonical frontier. Current membership and owner read/admin policy
  must still authorize the credential before returning that original ref.
- `POST /<owner>/<repo>.git/git-receive-pack`: require exactly one create command
  (`old=0`, selected branch, nonzero SHA-1 tip) and a full self-contained pack.
  Reject deletes, updates, other refs, multi-ref pushes, unsupported capabilities,
  incomplete packs, extra objects, or external delta bases before publication.
  Return standard pkt-line report-status success/rejection without diagnostics.
  The credential never authorizes upload-pack, UI APIs, or later mutations.

Host config advertises `import_mode: "first-push"` and `import_limits.pack_bytes`.
Browser instructions contain no embedded credential, and passwords are visible
only on explicit reveal/copy. Status checks and credential issuance remain
host-scoped even while another repository is open. Logout/account/intent changes
invalidate pending callbacks and clear secrets. Completion never switches an
already-open workspace. The bundle upload and completion HTTP routes are removed.

Repository capacity is reserved before accepting a push. Disk workspace capacity
is reserved transactionally under the operation lock before reading the receive
body; empty pending repos consume no staged-byte reservation. An invalid command
or preflight releases unused intake capacity only when no bytes/source were
pinned. Conversion into a repository reservation counts exactly once, including
failures. Known-invalid, unpublished sources can be corrected; once a source or
repository is committed, retries may only initialize/acknowledge that same tip.
Different pack compression/order for the same tip is not a different repository.

Reuse the private pack/history validator and ordinary forge/owner provisioning.
The server's internal ref envelope is a staging detail, not a bundle-upload
feature. Pin source receipts, disk ownership/inodes, root identities, and uploaded
bytes atomically. Restart/retry never adopts populated unknown roots, follows
symlinks, deletes unfamiliar files, restores removed grants/memberships, or resets
later pushes. Schema v4 staged data remains intact and is not silently converted
to a first-push operation during v5 migration.

Check compressed/expanded sizes and delta targets before Git sees the pack.
Sanitized Git validation checks strict object integrity, full reachable closure,
all historical commit/tree structures, supported modes, and no empty subtrees,
hooks, external configuration, alternates, shallow state, or extra refs. Import
and receive/provision/Git admission are shared across the host process. Credential
expiry, rotation, or account suspension during validation blocks publication.

Canonical source history is retained. Served history remains the existing
permission projection and can have different commit IDs: after initial import,
clone the Bastion view and issue normal Git credentials for ongoing work. This
slice is not an exact mirror or a raw-history owner-view compatibility change.

Defaults: 32 MiB pack, 128 MiB expanded data, 4 MiB per object, 50,000 objects,
10,000 commits, 250,000 distinct tree entries, 120-second receive/validation
budgets, and 512 MiB staging. Ready sources/intake remain available for recovery;
unused workspace reservations are released. Failed reservations count;
cancellation and garbage collection remain deferred. WSGI read deadlines are
checked between reads, so a server/proxy read timeout remains required. Ordinary
forge provisioning also retains per-command Git deadlines.

`tests/test_first_push_import.py` covers real `git push`, discovery, normal clone
and permitted/denied pushes; credential scoping/rotation/expiry; historical authors;
malformed commands/refs/packs; pre-Git expansion checks; corrected sources; quota
conversion; process/reply loss; disabled membership; and v4 data preservation.
Private validator/recovery regressions remain in `tests/test_repository_import*.py`.
`tests/test_spa_import.js` checks first-push instructions, status/credential retries,
pending-list recovery, secret clearing, and auth/workspace races. Existing seeded
creation, operator import, and single-root behavior remain unchanged.
