# Multi-repository deployment

The primary hosted profile is [Cloudflare](../deployment/cloudflare/README.md):
Durable Objects hold host/accounts and isolated repository catalogs; Artifacts
holds code and recoverable initial-push intake. Container disk is disposable.
No deployment creates a sample repository, tester, observer, or demo tokens.

## Local Docker host

This development alternative uses a persistent volume for SQLite and filesystem
objects. It is not the Cloudflare persistence architecture.

Configure Clerk and the initial administrator, with the exact public HTTPS origin:

```sh
cp deployment/.env.example deployment/.env
# Edit deployment/.env with real Clerk keys, admin email and HTTPS origin.
docker compose --env-file deployment/.env -f deployment/compose.yml build
docker compose --env-file deployment/.env -f deployment/compose.yml run --rm bootstrap
docker compose --env-file deployment/.env -f deployment/compose.yml up -d
curl -k https://localhost:8443/readyz
```

The bootstrap creates only an approved host account. Sign in through Clerk,
create a new project or choose **Import an existing repository**, then use the
displayed namespaced Git URL and one-time import credential. After import, issue
normal repository credentials and clone its permission-aware view.

The initial administrator can approve additional verified Clerk emails in the
dashboard. Repository owners invite approved users and manage path visibility
under **People & permissions**. Account approval alone grants no existing code.

Caddy forwards browser and namespaced Git traffic to the same host dispatcher.
Projection materializations are rebuilt on demand; the old single-repository
Git service, seeded bootstrap, and demo smoke scripts are retired. Existing
single-forge volumes are not automatically adopted or deleted.

For a public host, set `BASTION_HOST`, expose ports 80/443, update
`CLERK_AUTHORIZED_PARTIES`, and use valid TLS. Keep Docker/volumes operator-only.
Use one host process, enforce upstream body/read timeouts, and retain backups of
the host catalog and every repository's catalog and objects. Consistent whole-host
backup/restore and coordinated account removal are not yet automated.

The old AWS single-forge deployment is retired, not a supported multi-repository
profile. Its infrastructure definitions are retained for operator-managed
recovery of existing installations; do not use them to launch new hosts.
