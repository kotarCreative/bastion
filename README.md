# Bastion

Bastion is an experimental permission-aware Git forge. Repository truth is an
immutable object-store WAL chain selected by one conditional `manifest.json`;
local canonical and projected Git repositories are disposable caches. Users
fetch only permission-bounded projected repositories with an unmodified Git
client.

The prototype demonstrates that this model is feasible with explicit
restrictions:

- submitted history is linear;
- principals (users and service accounts), immutable teams, roles, and
  bindings resolve to content-addressed effective view descriptors;
- every authorization decision uses the descriptor captured in the committed
  WAL event; view assignment generations are distinct from the immutable
  descriptor captured for administrative updates;
- accepted commits retain their exact Git IDs in the submitting view;
- other views receive synthetic commits only for changes visible to them;
- hidden-only canonical commits do not move an unaffected projected ref;
- every tree transition is authorized before immutable pack upload and manifest
  CAS;
- accepted pushes are one linked WAL event and retries are resolved from WAL;
- view materialization and database indexes are rebuildable; worker jobs are
  only freshness hints;
- logical `Change-Id` metadata links different Git representations;
- durable control-plane state lives in SQLite (default), PostgreSQL, or Cloudflare Durable Objects as
  versioned, checksummed document rows guarded by lease-based sessions.

This is a local architecture experiment, not a production security boundary.
Clerk owns browser signup, sign-in, recovery, MFA/passkeys, and account
management. Bastion owns repository invitations, principals, permissions, and
short-lived view-scoped API tokens.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Transaction and event model](docs/TRANSACTIONS.md)
- [Security model](docs/SECURITY.md)
- [Scoped permissions](.specs/019-scoped-permissions.md)
- [Testing and local operation](docs/TESTING.md)
- [Test deployment](docs/DEPLOYMENT.md)
- [AWS dev deployment](deployment/terraform/aws/README.md)
- [Cloudflare deployment](deployment/cloudflare/README.md)
- [Artifacts migration plan](docs/CLOUDFLARE_PLAN.md)
- [Alchemy deployment evaluation](docs/ALCHEMY_EVALUATION.md)
- [Roadmap](ROADMAP.md)

### Canonical branch foundation (operator-only)

Bastion provides an internal, WAL-backed inventory of canonical branches.
Trusted local/server operators can list the canonical inventory; creating a
branch at an existing canonical tip additionally verifies the specified
principal's root-administrator descriptor. Git clients and browser views use
the active descriptor's scoped projection: v3 remains default-branch-only,
while v4 can expose explicitly readable refs and accept narrow single-ref
branch pushes. Client creation must start from a currently advertised
authorized source; arbitrary historical bases, deletion, merging, and
GitHub-compatible PR workflows are not supported. The canonical inventory is
limited to 1,024 branches including the default.

For a hosted repository, run from the host CLI but pass the **repository root**
(not the host root); `owner` is the configured owner principal:

```sh
python forge.py create-branch /path/to/host/repos/OWNER/REPO feature/login --principal owner
python forge.py create-branch /path/to/host/repos/OWNER/REPO release/v1 --principal owner --from main
python forge.py list-branches /path/to/host/repos/OWNER/REPO
```

For a locally initialized single-repository forge, substitute its root path and
the actual configured root-administrator principal ID. These are operator
commands, not end-user Git branch commands. See
[Transactions](docs/TRANSACTIONS.md) and the
[branch foundation spec](.specs/018-multi-branch-foundation.md).

## Multi-repository hosting

### Scoped repository and branch policy

Operators may configure explicit repository PR actions (`pr_view`, `pr_create`,
`pr_merge`) and branch actions (`read`, `create`, `update`, `delete`) with
`--repo-allow ACTION`, `--repo-deny ACTION`, `--branch-allow ACTION=SELECTOR`,
and `--branch-deny ACTION=SELECTOR` on `add-role`. Bind the configured role to
the existing principal, then run `transition-principal` to activate it. All
actions default deny, including for administrators; path `read`/`admin`, service
account ownership, and owner status imply no PR permissions. Branch selectors
are exact full refs or terminal namespace wildcards such as
`refs/heads/feature/*`. Path permissions and branch permissions are distinct
scopes. Legacy descriptor IDs remain unchanged; a permissions-bearing role
uses the v4 descriptor format.

For v4 Git transport, `read` must explicitly cover every advertised/target ref,
including the default branch. Existing-ref pushes need target `read` and
`update`. New branches need target `read` and `create`, plus `read` on the
currently advertised source even for a pure fork; when the push adds commits,
the target also needs `update`. For example, an owner opting into all branch reads, creates
and updates must pass `--branch-allow read=refs/heads/*`,
`--branch-allow create=refs/heads/*`, and
`--branch-allow update=refs/heads/*` to `add-role`. These are not implicit
grants: v3 continues its legacy default-branch read/update behavior, while
secondary branches remain denied until an explicit v4 policy is activated.
Path capabilities still govern every commit transition independently.

The `/api/v1/session` envelope retains its exact legacy shape for v3; v4 adds
the requesting principal's normalized `permissions` field and selected
authorized ref (or `null` for an empty projection). PR rights remain policy
only: PR lifecycle/merge workflows and a UI policy editor are not implemented.
See the [scoped permissions spec](.specs/019-scoped-permissions.md) and
[scoped branch transport spec](.specs/020-scoped-branch-transport.md).

The primary hosted application uses [Cloudflare](deployment/cloudflare/README.md):
a durable host catalog, one isolated Durable Object catalog per repository,
and separate Artifacts storage branches. Container disk is disposable. Startup
approves the configured administrator but creates **no demo repository**.
Sign in, create or import repositories, approve additional host accounts, and
invite collaborators with repository-scoped permissions and credentials.

### Local alternative

The explicit local host commands provide operator-managed account approval and
independent per-repository forge roots. `serve-host` adds account sign-in,
repository discovery, browser creation/import/switching, scoped permissions and
credentials, and namespaced HTTPS Git routing. This is a persistent-local-storage
prototype, not a production multi-tenant deployment.
The host catalog must live on persistent local storage; remote database and
object-store configuration is rejected. `list-repos` is operator inventory and
may include reserved, provisioning, or failed operations; it is not an
authorization-filtered account discovery API.

```sh
python forge.py approve-host-account ./bastion-host alice --email alice@example.com
# Optional capacity; omitted means no configured account limit.
python forge.py approve-host-account ./bastion-host bob --email bob@example.com --repo-limit 20
python forge.py link-host-account ./bastion-host user_123 --email alice@example.com
python forge.py create-repo ./bastion-host alice demo --request-id demo-001 --seed-empty
python forge.py create-repo ./bastion-host alice imported --request-id import-001 \
  --source /path/to/local/git/repository --branch main
python forge.py list-repos ./bastion-host --owner alice
```

Repository names and owners are lowercase URL slugs. Request IDs are
idempotency keys; reuse with different inputs is rejected. A seeded-empty
repository contains a deterministic initial commit: clone its owner view and
make commits on top of that history. Pushing unrelated local history is not
supported. Imports pin the selected local source branch tip; a changed source
requires a new request. Owner view `owner` has all currently supported
capabilities and the account's approved email identities. Existing direct-root
administration commands remain available; the single-repository browser demo
and automatic sample-repository bootstrap have been removed.

### Serve multiple users and repositories

Approve accounts first, configure the Clerk environment described below, and
serve the explicit host root:

```sh
# Configure before initializing/migrating the catalog; reservations count.
export BASTION_HOST_REPO_LIMIT=1000
python forge.py approve-host-account ./bastion-host alice --email alice@example.com
python forge.py approve-host-account ./bastion-host bob --email bob@example.com
python forge.py serve-host ./bastion-host --host 127.0.0.1 --port 8000
```

`serve-host` refuses an unknown root or an existing single-repository forge. It
migrates an existing supported host catalog without replacing repository roots.
Use one server process: request, Git, receive, and provisioning admission are
shared across its repositories, not across multiple server processes. The default
total repository budget is 1000, persisted in the host catalog; setting
`BASTION_HOST_REPO_LIMIT` when initializing/migrating explicitly updates it.
Failed/reserved operations count against both host and configured account limits.
`BASTION_MAX_ACTIVE_PROVISIONS` defaults to 2. Existing `BASTION_MAX_ACTIVE_*`
transport limits still apply across the host's child apps.

An approved account can sign in with no repositories, create its first one, and
switch between owned/shared repositories. For new projects, browser creation is
**seeded-empty**: clone, commit, then push. For existing history, use first-push
import below or the operator's `create-repo --source` command—not a push of
unrelated history into a seeded repository.

Permissions and Credentials operate on the selected repository only. Invitations
target already approved host accounts, selected from Clerk or by verified email;
they do not approve new accounts or send email. An interrupted discovery update
is reconciled against committed repository access, never treated as a new grant.
An invitation cannot delegate a folder containing a path the administrator is
not allowed to read. A disabled membership is not reactivated by retrying an old
invitation. Membership disabling alone does **not** revoke existing repository
credentials; revoke tokens and fence repository policy separately.

Git URLs are `/<owner>/<repository>.git`; the Basic username remains your
repository **view ID**, not necessarily the URL's owner. Use the clone URL shown
in the app, enable `git config --global credential.useHttpPath true`, and create
a fetch/push token in that repository's Credentials tab. Profile/clone guidance
lists the allowed commit author emails. Host sessions and browser UI sessions
cannot substitute for Git credentials, and a token from one repository cannot
authorize another.

Without Clerk configured, the server supports operator-issued host sessions for
local testing, but does not provide an open signup/login bypass. Do not expose
the development HTTP server publicly without a TLS/authenticated deployment
plan. The local profile intentionally rejects remote adapters; use the Cloudflare
profile for hosted storage. Coordinated account deprovisioning,
distributed admission, and consistent whole-host backup/restore remain rollout
work; existing per-repository backup commands do not back up the host catalog.

### Import an existing repository

In the `serve-host` dashboard, choose **Import an existing repository**, enter
a new name and its branch, then **Prepare first push**. The repository starts
uninitialized—no seed commit or file upload. From your existing local clone:

```sh
git config --global credential.useHttpPath true
git remote add bastion https://bastion.example/alice/project.git
git push bastion main
```

Use username **`owner`** and the displayed import credential as the Git password;
never put the credential in the remote URL. It expires after one hour and only
authorizes that repository's initial push, not fetch, UI access, or later writes.
The dashboard can check status and generate a replacement if its response was
lost; a replacement invalidates the previous credential. Pending imports can be
resumed after refreshing the page. The browser does not persist raw secrets.

This slice accepts exactly one SHA-1 branch and its complete history over Git
smart HTTP(S), not remote URL cloning, tags, multiple branches, SSH, or submodule
history. Historical author emails need not match your account. LFS
pointer files remain ordinary files; external LFS objects are not transferred.
Canonical history is preserved, but projected views can have different commit
IDs, so this is not an exact GitHub mirror. **After initial import, open the repo,
issue normal repository credentials, and clone its Bastion view for ongoing
work.** Replace the cached import credential in your Git credential helper.
Later pushes retain ordinary author, ancestry, and path authorization checks.

Default import limits are a **32 MiB pack**, 128 MiB expanded data, 4 MiB per
object, 50,000 objects, 10,000 commits, and 250,000 distinct tree entries.
Upload and validation budgets are each 120 seconds. The host stages at most
512 MiB with conservative reservations; completed imports retain their source
and intake for recovery, while releasing unused workspace capacity. Empty pending
repos reserve repository capacity but no staged-byte budget; disk capacity is
reserved before accepting the first push. Configure
limits through `BASTION_IMPORT_*` in `src/limits.py`; unsafe overrides fail startup.
Admission permits two active imports globally, one per account, and two per
source address in a host process.

Retry the same Git push after a failed or lost response. Server-side
operations survive restarts and never reset a repository after later pushes.
Leaving the browser or reloading the page does not cancel the server operation.
Failed reservations continue to count against capacity; cancellation and garbage
collection are not yet implemented. Keep the host on
persistent local storage for the local profile. Cloudflare persists pinned
intake in Artifacts and reconstructs validation work after container replacement.
Upload deadlines are checked between stream reads;
any externally reachable deployment also needs a proxy/server read timeout to
interrupt stalled clients. Existing production multi-repository rollout gates
still apply.

## Quick Start

Requirements:

- Python 3.11 or newer
- Git with SHA-1 repository support

Run all tests:

```bash
poetry install
poetry run python -m pytest tests -q
node --test tests/test_spa*.js
```

Create a local forge, one principal, and one view:

```bash
python3 forge.py init /tmp/bastion-forge /path/to/source --branch main \
  --object-store filesystem
python3 forge.py add-principal /tmp/bastion-forge alice --email alice@example.test
python3 forge.py add-role /tmp/bastion-forge alice-grants \
  --allow read=shared --allow read=team-a \
  --allow modify=shared --deny read=team-a/private
python3 forge.py bind-role /tmp/bastion-forge alice-grants --principal alice
python3 forge.py add-view /tmp/bastion-forge alice --principal alice
git clone --no-local /tmp/bastion-forge/views/alice.git alice
```

Roles grant and deny path-scoped capabilities (`read create modify delete mode
symlink branch merge admin tag`); ten capabilities are adjudicated by longest
matching prefix, with equal specificity denied. Catalog additions stay dormant
until an explicit transition applies them:

```bash
python3 forge.py transition-principal /tmp/bastion-forge alice
```

Every view has a separate authenticated intake repository — the only push
target. Published view repositories are fetch-only and reject direct pushes.
Clients clone over HTTP with one token per view:

```bash
python3 forge.py add-token /tmp/bastion-forge alice
python3 forge.py serve /tmp/bastion-forge --port 8000
git clone http://alice:TOKEN@127.0.0.1:8000/alice.git alice
```

For the browser application, initialize an empty multi-repository host:

```bash
python3 -m venv .poetry
.poetry/bin/pip install poetry
.poetry/bin/poetry install
export BASTION_ROOT=/tmp/bastion-host
export BASTION_BOOTSTRAP_ADMIN_EMAIL=alice@example.test
# Set Clerk environment variables as described below.
PYTHONPATH=src .poetry/bin/poetry run python deployment/bootstrap-host.py
.poetry/bin/poetry run python forge.py serve-host "$BASTION_ROOT" --port 8000
```

Then open `http://127.0.0.1:8000/`, sign in through Clerk, and create or import a
repository. Approve other accounts before inviting them under People & permissions.

### Clerk authentication setup

Clerk is Bastion's browser identity provider. It supplies account creation,
email verification, sign-in, recovery, MFA/passkeys, session management, and
the user profile UI. Bastion continues to own repository invitations and all
authorization decisions.

1. Create an application in the [Clerk Dashboard](https://dashboard.clerk.com/).
   Enable the sign-in methods you want users to have and require email
   verification. MFA and passkeys can be enabled from the same dashboard.
2. Add the Bastion origin to the Clerk application's allowed origins. For local
   development this is `http://127.0.0.1:8000`. Use the exact HTTPS origin in a
   deployed environment, with no path or trailing slash.
3. Copy the publishable and secret keys from Clerk's **API keys** page and set
   the server environment:

```bash
export CLERK_PUBLISHABLE_KEY='pk_test_...'
export CLERK_SECRET_KEY='sk_test_...'
export CLERK_AUTHORIZED_PARTIES='http://127.0.0.1:8000'
```

`CLERK_AUTHORIZED_PARTIES` is a comma-separated list when more than one origin
is permitted. Bastion validates Clerk's `azp` claim against this list. A partial
Clerk configuration fails startup rather than falling back to another account
system.

For networkless JWT signature verification, copy the instance **JWKS public
key** from Clerk's **API keys** page and also set:

```bash
export CLERK_JWT_KEY='-----BEGIN PUBLIC KEY-----
...
-----END PUBLIC KEY-----'
```

The secret key is still required because Bastion retrieves the authenticated
user's verified email addresses from Clerk's Backend API when claiming an
invitation.

4. Initialize a host and approve an account with the same verified email:

```bash
python3 forge.py approve-host-account /tmp/bastion-host alice --email alice@example.test
```

5. Start the browser application with the Clerk environment present:

```bash
.poetry/bin/poetry run python forge.py serve-host /tmp/bastion-host --port 8000
```

Open `http://127.0.0.1:8000/` and create or sign in to the Clerk account. On
first sign-in, Bastion atomically links the Clerk user ID to the approved host
account whose declared email matches a verified Clerk address. The link is
durable and does not change if the Clerk email is later edited. No match or an
ambiguous match is denied, and creating a Clerk account alone never grants
repository access.

After sign-in, **Account** opens Clerk's profile and security management UI.
Bastion exchanges Clerk authentication for a host session; opening a repository
issues a separate one-hour, `ui`-only token bound to its view. Git tokens remain separately issued,
revocable credentials; create them from the **Credentials** tab (or
`forge.py add-token`).

For the Cloudflare deployment, also set `BASTION_BOOTSTRAP_ADMIN_EMAIL` to the
verified email that should claim the initial administrator. See
[Bastion on Cloudflare](deployment/cloudflare/README.md#first-deployment) for
the complete `wrangler secret put` sequence.

An account whose active descriptor grants root `admin` capability is treated
as a repository owner. Owners get a **People & permissions** tab where they can make an
existing file or folder private for another principal's view, or restore its
inherited visibility. Each edit publishes a new immutable descriptor and
projection epoch before the UI reports success. Within the current prototype,
one forge root is one uploaded/imported repository; start the demo server for
that root to manage it.

The **Code** tab opens an expanded file structure, including nested directories
and all paginated entries visible to the signed-in user. Folder names can be
clicked to collapse their children; files open a preview. **Browse one folder**
switches to directory-by-directory browsing, and **Refresh view** reloads the
active permissions and projected files after a privacy change. To show the entire
imported repository as the owner, the owner must also have root `read` access.

In **People & permissions**, use **Invite a collaborator** to set a unique
identifier, verified email, permission level, and allowed paths before granting
access. When Clerk is configured, **Select an existing Clerk account** searches
the live Clerk directory by name or email (at least two characters). Select a
verified account from the results, or choose **Invite by email** for someone who
has not registered yet. Already-invited accounts are marked and cannot be selected.
Directory search is available only to repository owners and requires no webhook.
Picker invitations revalidate the account server-side and bind its stable Clerk
user ID before granting access; the browser cannot substitute another email.

**Read-only** grants read access; **Contributor** also grants create,
modify, delete, mode, and symlink access within those same paths. Enter one
existing file or folder per line, or `/` for the whole repository. Other paths
are absent from their projection, including the file tree and history. Neither
level grants administrator, branch, merge, or tag capabilities. You can then
hide individual files or folders using the collaborator privacy controls.

Invitations provision access immediately; they do not send email. Share the
site URL with the invitee, who signs in through Clerk with the invited verified
email.

The **Credentials** tab lets any signed-in user mint their own Git tokens:
choose the view, a read-only (fetch) or read-and-write (fetch, push) access
level, and an expiry. The secret is shown once, and tokens can be revoked from
the same tab. A push token is offered only for a view whose policy grants a
write capability. Operators can still issue tokens with `forge.py add-token`.
For a privacy demo, open the owner account and collaborator account in separate
browser profiles, change the collaborator's privacy settings, and refresh their
view.

Use normal Git inside `alice`:

```bash
git -C alice add .
git -C alice commit -m "change"
git -C alice push origin main
```

Receive hooks synchronously validate and reconcile intake quarantine, upload
immutable canonical and submission-evidence packs, and acknowledge only after
manifest CAS. Jobs can be processed explicitly but are never correctness state:

```bash
python3 forge.py work /tmp/bastion-forge
python3 forge.py check /tmp/bastion-forge
```

Run `python3 forge.py --help` for all experimental commands.

## Cloudflare Artifacts

The Cloudflare profile runs Bastion's Python and Git engine in a Cloudflare
Container behind a Worker. A SQLite-backed Durable Object stores the persistent
control plane. A private Artifacts repository stores the WAL, canonical and
submission packs, and policy descriptors. Git materializations and the local
storage cache can be rebuilt; the hosted runtime has no live SQLite file.

Use an Artifacts remote returned by the Cloudflare API or Workers binding:

```bash
export BASTION_ARTIFACTS_TOKEN='<private repository write token>'
python3 forge.py init /tmp/bastion-artifacts /path/to/source --branch main \
  --object-store 'artifacts+https://<ACCOUNT_ID>.artifacts.cloudflare.net/git/default/bastion.git'
```

The storage branch defaults to `bastion-storage`; URI query parameters `branch`
and `token_env` select another branch or credential environment variable. The
persisted configuration contains the remote, branch, and variable name.

Migrate an existing forge with its consistent backup and restore path:

```bash
python3 forge.py backup /path/to/forge /path/to/backup
python3 forge.py restore /path/to/backup /path/to/new-forge \
  --object-store 'artifacts+https://<ACCOUNT_ID>.artifacts.cloudflare.net/git/default/bastion.git'
python3 forge.py check /path/to/new-forge
```

Restore requires unused storage keys for the repository being migrated. It
preserves repository identity, history, policy assignments, accounts, and scoped
token records. For a hosted Container, follow the deployment instructions to
stage the one-time Durable Object catalog import as well.

Artifacts credentials grant access to the entire private backing repository.
Users and agents use Bastion's projected Git endpoints and scoped tokens;
the Worker never issues backing-repository tokens to them. Artifacts stores
Bastion's durable object data as versioned Git files. Projection and intake
validation continue to run in Bastion.

Bastion is available under the [MIT license](LICENSE).
