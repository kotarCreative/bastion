# Bastion on Cloudflare Workers, Durable Objects, and Containers

This profile runs the existing Flask UI and Git smart HTTP engine in a
Cloudflare Container. A named `ForgeContainer` Durable Object coordinates one
forge's Git work. A separate SQLite-backed `ForgeCatalog` Durable Object owns
the persistent catalog: principals, roles, views, passwords, token digests,
audit rows, leases, and WAL indexes. Artifacts owns repository objects and WAL
history. Container disk holds only disposable Git materializations, caches,
and configuration reconstructed from the catalog; it has no live SQLite file.

The storage is serverless. Python and Git computation still use the on-demand
Container. A Container outbound handler intercepts `http://catalog.bastion.internal`
and routes it through the `CATALOG` binding, keyed by the Container's DO ID.
Public Worker requests cannot reach this SQL endpoint. The Worker forwards
browser and Git traffic to Flask and never gives browsers backing credentials.

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
Durable Object catalog and legacy control snapshot do not exist and Bastion must create its initial catalog.

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

On Linux hosts running Tailscale, workerd local Containers can inherit
`net.ipv4.conf.all.src_valid_mark=1` and fail before creating the application
container. Keep that host setting enabled for Tailscale and configure new
network namespaces to use kernel defaults instead:

```sh
sudo sysctl -w net.core.devconf_inherit_init_net=2
echo 'net.core.devconf_inherit_init_net = 2' | \
  sudo tee /etc/sysctl.d/99-cloudflare-container-netns.conf
```

Do not clear the host's `src_valid_mark`; Tailscale uses it for marked reverse
path checks. If UFW denies traffic arriving on `docker0`, workerd's sidecar
also needs TCP access to its ephemeral callback listener on the bridge gateway.
A narrower rule than allowing all `docker0` traffic is:

```sh
sudo ufw allow in on docker0 from 172.17.0.0/16 to 172.17.0.1 \
  port 32768:60999 proto tcp comment 'wrangler container callbacks'
```

Adjust the subnet and ephemeral port range to the Docker bridge and host
configuration. This rule lets containers on that bridge reach host TCP
listeners in the selected range, so remove it when local Container development
is no longer needed.

The Container starts Gunicorn with one worker and one thread. Python sends
parameterized SQL through the private outbound binding. A write transaction
holds a statement log in memory; reads replay its pending writes inside a
DO SQL transaction that is rolled back immediately. Commit replays the writes
atomically with a revision fence and a durable operation receipt, so a lost
commit response can be retried without duplicate inserts. Read transactions
also check the revision on every query and fail if concurrent state changes.
There are no transactions held open across HTTP calls and no full catalog
checkpoints on ordinary mutations. SQL commits finish before success is returned.

On startup, the Container reconstructs configuration from the catalog and
repairs derived indexes from the Artifacts WAL before serving authorization
reads. Storage outages fail closed. Git pushes still require Artifacts, while
account/catalog commits go directly to Durable Object storage.

Container startup waits up to five minutes for an instance and ten minutes for
the service port, allowing cold recovery to replay retained Artifacts history.
Gunicorn's 660-second worker timeout is aligned with Bastion's bounded
600-second Git receive request limit.

## Existing deployment migration

The `v2` Wrangler migration adds `ForgeCatalog` without deleting the existing
`ForgeContainer` namespace. On the first cold start after upgrading this
profile, Bastion imports the old Artifacts SQLite snapshot into the DO tables
and stores the repository configuration in the same atomic transaction.
Passwords, token digests, audit sequences, permissions, and repository identity
are preserved. It omits old process leases and repairs indexes from the WAL.
Later starts use the DO catalog and never re-import the old snapshot. Retain
that snapshot as migration backup; rolling back Worker code alone would serve
stale catalog state after new DO mutations.

For an existing local forge, use the commands below to stage a portable SQLite
snapshot for this one-time import. PostgreSQL needs a separate conversion to a
SQLite backup first; the existing backup/restore command does not convert those
backends. DO-backed `forge.py backup` exports the same portable SQLite backup
format and can be restored to a SQLite host or staged for a fresh DO catalog.

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
replacing a deployment. Keep backups of both the catalog and repository objects.
Do not delete the Artifacts repo or either production DO namespace as a way to
reset Bastion.

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

The Container uses `standard-1` for Git pack processing. Artifacts allows
32 MB per blob and 1 GB per repository. The adapter chunks logical objects
into blobs with a 256 MiB logical object ceiling. Retained Git history counts
toward the repository quota; there is no automatic history collection.
Catalog edits no longer publish full SQLite images into Artifacts history.

The SQL bridge bounds each request and query result at 8 MiB. Large transaction
logs and snapshot imports use immutable, digest-chained 512 KiB frames stored
privately in the DO. Uploading frames does not change the live catalog revision
or apply any catalog SQL. Final commit replays the entire stream in one native
transaction together with repository configuration and an idempotency receipt.
An interrupted upload leaves the old catalog authoritative; explicit rollback
discards its frames, and a DO alarm expires abandoned uploads after 24 hours.
UTF-8 decoding preserves characters split across frame boundaries.

Bulk SQL reads also page their results under the transaction's revision fence;
Python assembles the full result expected by existing catalog callers.
Portable backups read primary-key pages with the same catalog revision on every
request. Concurrent changes fail the export and remove the partial backup.
Imports and backup tables can exceed one request's size; neither is truncated.
Ordinary write transactions still replay pending statements for read-your-writes,
so very large write batches cost more than a native single-call transaction.
Cloudflare's native limits remain: 2 MB per SQL row, 10 GB per DO, and the
configured per-request CPU limit, including final atomic import publication.
See [Durable Object limits](https://developers.cloudflare.com/durable-objects/platform/limits/).

Cloudflare Containers have ephemeral disks. A restart reconstructs Git files
and configuration while catalog tables remain in the DO. Artifacts outages
prevent repository recovery/pushes; DO outages prevent catalog access. Neither
outage is treated as permission to bootstrap a fresh forge.

## Verification without Docker

The catalog and Python integration tests run in the actual local Workers
runtime without Docker or paid services:

```sh
npm ci
npm test
npm run check
cd ../..
.venv/bin/python -m unittest tests.test_durable_objects -v
```

The Python tests start Wrangler with `wrangler.test.jsonc` and restart workerd
with the same persisted DO storage. The test Worker binds only a test catalog;
it is never used as the production entrypoint. `wrangler.smoke.jsonc` is an
isolated, authenticated live-test Worker for exercising the same catalog on
Cloudflare without Artifacts or Containers. Supply a random `BASTION_SMOKE_SECRET`
using a protected `--secrets-file`; never deploy it against production catalog
bindings. The full hosted deployment still requires Workers Paid and Docker.

For a fresh smoke Worker, run from the repository root:

```sh
.venv/bin/python deployment/cloudflare/live_catalog_smoke.py seed \
  --endpoint 'https://bastion-catalog-smoke.<subdomain>.workers.dev' \
  --secrets-file /path/to/protected-smoke-secrets.json
```

It stages over 8 MiB of synthetic catalog rows, checks
invisibility before commit and idempotent publication, then verifies bulk reads
and a portable backup. Redeploy the same Worker, then run the script with
`verify` instead of `seed` to prove persistence. Seeding refuses an existing
catalog. Delete only the isolated smoke Worker afterward with
`wrangler delete --config wrangler.smoke.jsonc --force`, then remove its local
credential file.

References: [Cloudflare Containers](https://developers.cloudflare.com/containers/),
[Container class](https://developers.cloudflare.com/containers/api/container-class/),
[Wrangler Container configuration](https://developers.cloudflare.com/containers/configuration/wrangler/),
[Artifacts Workers binding](https://developers.cloudflare.com/artifacts/api/workers-binding/),
and [Artifacts limits](https://developers.cloudflare.com/artifacts/platform/limits/).

[Container outbound bindings](https://developers.cloudflare.com/containers/configuration/workers-connections/)
and [Durable Object SQL transactions](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/#transactions)
describe the storage bridge APIs. See [verification results](../../docs/CLOUDFLARE_VERIFICATION.md)
for the live account checks and remaining deployment prerequisites.
