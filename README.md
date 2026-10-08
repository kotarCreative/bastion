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
- [Testing and local operation](docs/TESTING.md)
- [Test deployment](docs/DEPLOYMENT.md)
- [AWS dev deployment](deployment/terraform/aws/README.md)
- [Cloudflare deployment](deployment/cloudflare/README.md)
- [Artifacts migration plan](docs/CLOUDFLARE_PLAN.md)
- [Alchemy deployment evaluation](docs/ALCHEMY_EVALUATION.md)
- [Roadmap](ROADMAP.md)

## Quick Start

Requirements:

- Python 3.11 or newer
- Git with SHA-1 repository support

Run all tests:

```bash
python3 -m unittest discover -v tests 'test_*.py'
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

Or run the invite-only browser demo (requires Poetry):

```bash
python3 -m venv .poetry
.poetry/bin/pip install poetry
.poetry/bin/poetry install
python3 forge.py add-token /tmp/bastion-forge alice
BASTION_DEMO_SECRET=$(openssl rand -hex 32) .poetry/bin/poetry run python forge.py serve-demo /tmp/bastion-forge --port 8000
```

Then open `http://127.0.0.1:8000/`, enter the token, and browse the effective
permissions, evaluate path decisions, view the projected tree and visible
history, and clone the same view with an unmodified Git client.

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

4. Invite a user by creating a Bastion principal with the same verified email,
   assigning its permissions, and creating a view. The earlier quick-start
   commands already do this for `alice`:

```bash
python3 forge.py add-principal /tmp/bastion-forge alice \
  --email alice@example.test
python3 forge.py add-role /tmp/bastion-forge alice-owner --allow admin=
python3 forge.py bind-role /tmp/bastion-forge alice-owner --principal alice
python3 forge.py add-view /tmp/bastion-forge alice \
  --principal alice --branch main
```

5. Start the browser application with the Clerk environment present:

```bash
.poetry/bin/poetry run python forge.py serve-demo \
  /tmp/bastion-forge --port 8000
```

Open `http://127.0.0.1:8000/` and create or sign in to the Clerk account. On
first sign-in, Bastion atomically links the Clerk user ID to the one invited
principal whose declared email matches a verified Clerk address. The link is
durable and does not change if the Clerk email is later edited. No match or an
ambiguous match is denied, and creating a Clerk account alone never grants
repository access.

After sign-in, **Account** opens Clerk's profile and security management UI.
Bastion exchanges the Clerk session for a one-hour, `ui`-only token bound to a
specific repository view. Git and API tokens remain separately issued,
revocable credentials.

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
email. Git tokens are issued separately using the existing CLI. For a privacy
demo, open the owner account and collaborator account in separate browser
profiles, change the collaborator's privacy settings, and refresh their view.

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
