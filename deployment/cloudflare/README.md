# Bastion on Cloudflare Workers, Durable Objects, and Containers

This profile runs the multi-repository Flask host and Git smart HTTP engine in
one Cloudflare Container. The named `ForgeContainer` coordinates host requests.
A host `ForgeCatalog` owns approved accounts, memberships, sessions and import
reservations. Each repository has its own `ForgeCatalog` for principals, roles,
views, Clerk links, token digests, audit rows, leases and WAL indexes.

Artifacts owns repository objects and WAL history in separate server-controlled
`bastion-repo-<opaque-id>` branches. The `bastion-host-storage` branch holds
digest-pinned initial import intake. All branches share one private backing
Artifacts repository and its quota; they are not independent billing containers.
Users never receive its write token. Disk holds only reconstructible config,
Git caches and validation work; no live SQLite file or durable disk is required.

The storage is serverless. Python and Git computation still use the on-demand
Container. A Container outbound handler intercepts `http://catalog.bastion.internal`
and routes `/host/*` and `/repos/<opaque-id>/*` through the `CATALOG` binding to
distinct DO names scoped by the stable Container DO ID. Unscoped routes fail.
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
npx wrangler secret put BASTION_BOOTSTRAP_ADMIN_EMAIL
npx wrangler secret put CLERK_PUBLISHABLE_KEY
npx wrangler secret put CLERK_SECRET_KEY
npx wrangler secret put CLERK_AUTHORIZED_PARTIES
# Optional: only needed for networkless JWT signature verification.
# Leave unset to use Clerk's live JWKS endpoint (recommended).
npx wrangler secret put CLERK_JWT_KEY
```

Use a high-entropy provisioning secret and set the bootstrap administrator to
an email verified by the intended Clerk account. `CLERK_AUTHORIZED_PARTIES`
must be the exact public Worker origin, with no path. Configure that origin and
the desired signup, email verification, MFA, and passkey methods in the Clerk
Dashboard. Bastion does not require a custom JWT template. Do not put the
Artifacts token in a Worker variable or send it to a browser.

`CLERK_JWT_KEY` is optional. An invalid or stale static key prevents sign-in
even when the other Clerk settings are correct. Omit it from bulk secret
uploads when using live JWKS verification. If a static key is already set,
remove it with `npx wrangler secret delete CLERK_JWT_KEY` and restart the
Container so the running Python process loads the updated environment.

After deployment, provision once with the admin secret:

```sh
curl -i -X POST \
  -H "Authorization: Bearer $BASTION_ADMIN_SECRET" \
  "https://bastion-forge.<your-subdomain>.workers.dev/_admin/provision"
```

The route returns repository status only. It does not return the repository
token. Then open the Worker URL and sign in through Clerk with the bootstrap
administrator email. Bootstrap approves only the initial `admin` host account.
The dashboard starts empty: there is no pre-seeded repository or demo token.

Use **Create a repository** for a new project (clone its initial empty-tree
history before committing), or **Import an existing repository** for local code.
The import screen reserves a name and gives a one-hour, initial-push-only
credential. Push the selected branch to `/<owner>/<repository>.git` with username
`owner`. Intake is saved to Artifacts before validation/provisioning receipts;
retries reconstruct it after Container replacement without changing the tip.
After import, issue normal credentials and clone the Bastion view for later work.
Projected commit IDs can differ from original history.

The initial administrator can **Approve a host account** in the dashboard, then
that user signs in through Clerk. Approval grants no code access. Repository
owners invite already approved users and hide/restore file or folder paths in
**People & permissions**. Invitations do not send email. Other users cannot
approve accounts; the admin API requires the persisted bootstrap account ID.

Cloudflare's Artifacts namespace defaults to `default` in this config. Change
that namespace and the fixed repo variable before deploying if the account
uses another namespace. Re-run `npx wrangler types` after binding changes.

Production uses the `bastion-production` repository. Keep local previews on a
separate repository (for example, override `ARTIFACTS_REPO` with `bastion-private`
using `wrangler dev --var ARTIFACTS_REPO:bastion-private`). A local preview's
catalog is local to workerd, even when its Artifacts repository is remote.
Never attach a fresh host catalog to a production backing repository as a reset.
Use distinct preview storage: existing data is not adopted into a new host.

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

On repository access, the Container reconstructs configuration from its catalog and
repairs derived indexes from the Artifacts WAL before serving authorization
reads. Storage outages fail closed. Git pushes still require Artifacts, while
account/catalog commits go directly to Durable Object storage.

Container startup waits up to five minutes for an instance and ten minutes for
the service port, allowing cold recovery to replay retained Artifacts history.
Gunicorn's 660-second worker timeout is aligned with Bastion's bounded
600-second Git receive request limit.

## Existing single-repository deployments

The existing DO classes and migration history are retained. New catalogs use
new scoped names, not the old singleton catalog. Legacy Artifacts branches and
snapshots are left untouched; the runtime no longer imports/seeds a demo root.
Upgrading presents a new empty host, **not** migrated access to the old forge.
Take backups before upgrading and keep the old deployment available privately
if its identities, tokens or policy must be preserved. Do not delete namespaces
or the backing repository to reset the app.

To bring code into the new host, sign in and import its selected Git branch.
This transfers code/history, not old principals, permissions, tokens or membership.
An automated whole-host migration/backup/restore flow is not yet provided.
The portable-catalog import helper remains an explicit operator utility and is
not called by startup. Keep independent backups of catalogs and objects.

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
.venv/bin/python -m pytest tests/test_durable_objects.py tests/test_cloudflare_host.py -q
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
