# Bastion on Cloudflare Workers and Containers

This profile runs the existing Flask UI and Git smart HTTP service in one
Cloudflare Container. A single named Durable Object, `forge`, owns that
Container so SQLite writes and Git receive-pack requests share one process.
The Worker forwards browser and Git traffic to this service. It never gives a
browser an Artifacts credential.

The Worker uses the `ARTIFACTS` binding and the fixed `ARTIFACTS_REPO` value
in `wrangler.jsonc`. The authenticated `POST /_admin/provision` route creates
the private Artifacts repository if needed and mints a write token. The
Durable Object keeps the remote and token in private Durable Object storage;
it sends the token to the Container only when starting it. Tokens have a
one-year lifetime and are automatically renewed before an expired credential
would be used to start a replacement Container.

## First deployment

Cloudflare Containers require a Workers Paid plan. From this directory:

```sh
npm ci
npx wrangler login
npx wrangler types
npx wrangler deploy
npx wrangler secret put BASTION_ADMIN_SECRET
npx wrangler secret put BASTION_BOOTSTRAP_ADMIN_PASSWORD
```

Use separate high-entropy values for the provisioning secret and the initial
`admin` account password. Keep both values in a password manager. Do not put
the Artifacts token in a Worker variable or send it to a browser. The first
secret authorizes provisioning; the second is used only if the Artifacts
control snapshot does not exist and Bastion must create its initial catalog.

After deployment, provision once with the admin secret:

```sh
curl -i -X POST \
  -H "Authorization: Bearer $BASTION_ADMIN_SECRET" \
  "https://bastion-forge.<your-subdomain>.workers.dev/_admin/provision"
```

The route returns repository status only. It does not return the repository
token. Then open the Worker URL and sign in as `admin` using the bootstrap
password. The account can manage permissions through the existing UI/API.
The fresh bootstrap creates only this initial account and view; prepare extra
principals, accounts, views, and scoped tokens in a restored root before
publishing its first control snapshot when the demo needs several agents.

Cloudflare's Artifacts namespace defaults to `default` in this config. Change
that namespace and the fixed repo variable before deploying if the account
uses another namespace. Re-run `npx wrangler types` after binding changes.

## Local preview

`wrangler dev` builds the Worker and Container image. Configure the Artifacts
binding for remote development in the Wrangler config if the local Worker
needs a live Cloudflare repository, and set the two secrets with Wrangler.
Local Docker image startup also requires Docker daemon access. A preview that
cannot reach the bound Artifacts repo will fail closed rather than create a
new catalog.

The Container starts Gunicorn with one worker and one thread. Bastion
checkpoints a consistent SQLite backup plus `backend.json` and
`repository.json` into one private control object after state commits and
before a mutating HTTP response succeeds. On Container startup, it restores
that snapshot, recovers SQL indexes from the Artifacts WAL, and validates the
repository before serving requests. If a checkpoint fails, the active runtime
returns `503` and stays closed to later requests; it does not serve
authorization state that might disappear on restart.
Container startup waits up to five minutes for an instance and ten minutes for
the service port, allowing cold recovery to replay retained Artifacts history.
Gunicorn's 660-second worker timeout is aligned with Bastion's bounded
600-second Git receive request limit.

## Existing deployment migration

Run these commands from a checkout of the Bastion repository. The Cloudflare
profile requires a SQLite catalog; the current backup/restore command only
restores to the same database backend, so a PostgreSQL forge cannot be moved
directly to this profile. Keep it on the existing PostgreSQL-capable host or
first migrate it to SQLite using a separate, reviewed procedure. Using the
Artifacts object store on a PostgreSQL-capable host does not change this
Container limitation.

To move an existing SQLite forge, provision an empty target Artifacts repo
first, create a temporary private write token for the migration, and keep the
Worker URL away from users until the initial snapshot is published. The Worker
provisioning endpoint does not reveal its own token; create a separate
short-lived migration token from the Artifacts repo's token controls.

```sh
cd /path/to/bastion
export BASTION_ARTIFACTS_REMOTE='https://<account>.artifacts.cloudflare.net/git/default/bastion-private.git'
export BASTION_ARTIFACTS_TOKEN='<private migration token>'
.venv/bin/python forge.py backup /path/to/old/forge /tmp/bastion-backup
.venv/bin/python forge.py restore /tmp/bastion-backup /var/tmp/bastion-migrated \
  --object-store 'artifacts+https://<account>.artifacts.cloudflare.net/git/default/bastion-private.git?branch=bastion-storage&token_env=BASTION_ARTIFACTS_TOKEN'
# Make any required contributor principals/views/accounts in /var/tmp/bastion-migrated.
.venv/bin/python deployment/cloudflare/checkpoint.py /var/tmp/bastion-migrated
.venv/bin/python forge.py check /var/tmp/bastion-migrated
```

`checkpoint.py` refuses to overwrite an existing control snapshot and checks
that the root uses the configured Artifacts remote and `bastion-storage`
branch. Do not issue a browser request before this command finishes: without a
snapshot the Worker treats an empty repository as a new deployment and seeds
a small README repository. It refuses startup if the snapshot is missing but
repository objects or local root files already exist, and it refuses to reset
on a remote read failure. Verify the restored forge with `forge.py check`
before directing users to the new URL.

No automatic background compaction removes historical Artifacts versions.
When making a backup, retain it outside the live Artifacts repo before
replacing a deployment. Never delete the Artifacts repo as a way to reset
Bastion: it holds both repository objects and the only durable copy of the
control-plane snapshot.

## Concurrent-agent demo

The canonical branch needs files under `team-a/` and `team-b/` before these
views are created. Add two accounts and scoped views to the restored local
root before running `checkpoint.py`:

```sh
cd /path/to/bastion
ROOT=/var/tmp/bastion-migrated
.venv/bin/python forge.py add-principal "$ROOT" agent-a --email agent-a@example.invalid
.venv/bin/python forge.py set-password "$ROOT" agent-a
.venv/bin/python forge.py add-role "$ROOT" agent-a-role \
  --allow read=team-a --allow create=team-a --allow modify=team-a \
  --allow delete=team-a --allow mode=team-a --allow symlink=team-a
.venv/bin/python forge.py bind-role "$ROOT" agent-a-role --principal agent-a
.venv/bin/python forge.py add-view "$ROOT" agent-a --principal agent-a --branch main
.venv/bin/python forge.py add-token "$ROOT" agent-a \
  --scope fetch --scope push --expires-in 30d

.venv/bin/python forge.py add-principal "$ROOT" agent-b --email agent-b@example.invalid
.venv/bin/python forge.py set-password "$ROOT" agent-b
.venv/bin/python forge.py add-role "$ROOT" agent-b-role \
  --allow read=team-b --allow create=team-b --allow modify=team-b \
  --allow delete=team-b --allow mode=team-b --allow symlink=team-b
.venv/bin/python forge.py bind-role "$ROOT" agent-b-role --principal agent-b
.venv/bin/python forge.py add-view "$ROOT" agent-b --principal agent-b --branch main
.venv/bin/python forge.py add-token "$ROOT" agent-b \
  --scope fetch --scope push --expires-in 30d

.venv/bin/python forge.py check "$ROOT"
.venv/bin/python deployment/cloudflare/checkpoint.py "$ROOT"
```

Each `add-token` prints a credential once; copy it to a password manager or
another private secret store. In two separate Codex sessions, clone
`https://<worker>/agent-a.git` and `https://<worker>/agent-b.git`. When Git
prompts for Basic credentials, enter the matching view name (`agent-a` or
`agent-b`) as username and that view's scoped token as password. Do not embed
tokens in clone URLs or give either agent the Artifacts write token.

Have each session independently create a file in its allowed subtree, commit,
and push to `main`. Record for each agent its file path, commit ID, push result,
and whether it could read the other agent's subtree. Since pushes are routed
through the same named Container and Bastion WAL, the second agent may need to
fetch and rebase if the first push advances the canonical branch first. It
should then retry the push through its own view token. Finally verify each
session can fetch its own subtree and receives no files from the other's
restricted subtree.

## Capacity and recovery limits

The Container uses the `standard-1` instance type to leave memory for SQLite
snapshots and Git pack processing. Artifacts currently allows 32 MB per blob
and 1 GB per repository. The Artifacts object-store adapter chunks logical
objects (including larger packs and checkpoints) into blobs; its logical
object ceiling is 256 MiB. The 1 GB quota covers the repository's retained
Git history and prior snapshot versions, not just the latest visible data.
There is no automatic history collection or control-snapshot compaction in
this deployment. Each mutating HTTP request and each committed catalog update
can publish another full SQLite snapshot, so repeated login/logout cycles and
frequent policy changes consume retained Git history too. Monitor repo growth
and keep historical snapshots in mind. A large pack above Bastion's configured
HTTP receive-body limit is also rejected by the service.

Cloudflare Containers use ephemeral disks. The app therefore restores the
control plane from Artifacts on cold start; local files are only a working
copy. A remote Artifacts outage makes startup fail and makes mutation requests
return `503`. This keeps old permission state from silently returning, but it
means this profile depends on Artifacts availability for every checkpoint.

References: [Cloudflare Containers](https://developers.cloudflare.com/containers/),
[Container class](https://developers.cloudflare.com/containers/api/container-class/),
[Wrangler Container configuration](https://developers.cloudflare.com/containers/configuration/wrangler/),
[Artifacts Workers binding](https://developers.cloudflare.com/artifacts/api/workers-binding/),
and [Artifacts limits](https://developers.cloudflare.com/artifacts/platform/limits/).
