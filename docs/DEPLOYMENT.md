# Test Deployment

This deployment profile runs one Bastion instance on one Linux host. Caddy
terminates HTTPS and routes Git smart-HTTP requests to the bounded streaming
server while the browser and JSON API run in a single Gunicorn worker. A
separate worker keeps projected views current. SQLite, repository truth, and
materialized Git repositories live together on one Docker volume.

The Git service treats the reverse proxy as the network peer, so its per-IP
receive limit applies across the test instance. Per-token limits still apply
to each credential independently.

It is intended for invite-only testing with synthetic data. Keep the Docker
host and its persistent volume accessible only to operators.

## Requirements

- Docker Engine with the Compose plugin
- ports 8080 and 8443 available for local testing, or ports 80 and 443 for a
  public hostname
- at least 2 GiB of free disk after images and volumes are created

## Start A Local Test Instance

Build the image and create the sample repository before starting services:

```bash
docker compose -f deployment/compose.yml build
docker compose -f deployment/compose.yml run --rm bootstrap
```

The bootstrap command prints two `export` statements containing fresh
seven-day credentials. Save them in the current shell; raw credentials are not
stored by Bastion and cannot be displayed again.

Start the services and inspect their health. The proxy waits for the web, Git,
and projection worker health checks before accepting traffic:

```bash
docker compose -f deployment/compose.yml up -d
docker compose -f deployment/compose.yml ps
curl -k https://localhost:8443/readyz
```

Open `https://localhost:8443/` and accept Caddy's local certificate for this
test host. Enter `BASTION_TESTER_TOKEN` in the browser. Then run the full HTTP,
Git receive, worker, and cross-view smoke test:

```bash
BASTION_URL=https://localhost:8443 deployment/smoke-test.sh
```

The smoke test clones the full tester view and the restricted observer view,
proves that the observer cannot see `private/secret.txt`, pushes a new shared
file as the tester, and waits for that file to appear in the observer view.

## Use A Public Test Host

Point a DNS A/AAAA record at the server, allow inbound TCP 80 and 443, and set
the hostname and host ports before starting Compose:

```bash
cp deployment/.env.example deployment/.env
sed -i 's/BASTION_HOST=localhost/BASTION_HOST=bastion.example.com/' deployment/.env
sed -i 's/BASTION_HTTP_PORT=8080/BASTION_HTTP_PORT=80/' deployment/.env
sed -i 's/BASTION_HTTPS_PORT=8443/BASTION_HTTPS_PORT=443/' deployment/.env
docker compose --env-file deployment/.env -f deployment/compose.yml up -d
```

Caddy obtains and renews the public certificate automatically. Run the smoke
test with certificate verification enabled:

```bash
BASTION_URL=https://bastion.example.com BASTION_INSECURE_TLS=0 \
  deployment/smoke-test.sh
```

## Operations

Run the invariant checker after deployment changes and before backups:

```bash
docker compose -f deployment/compose.yml run --rm cli \
  python forge.py check /var/lib/bastion/forge
```

Create a backup inside the persistent volume:

```bash
docker compose -f deployment/compose.yml run --rm cli \
  python forge.py backup /var/lib/bastion/forge \
  /var/lib/bastion/backups/manual-001
```

Inspect service logs with `docker compose -f deployment/compose.yml logs -f`.
Stop the application with `docker compose -f deployment/compose.yml down`;
the named volumes remain. Credentials can be listed by identifier or revoked
with the `list-tokens` and `revoke-token` CLI commands.

To destroy all test data, explicitly run `docker compose -f
deployment/compose.yml down --volumes`. This permanently removes repository
truth, tokens, backups stored in the volume, and Caddy certificates.

## Current Boundary

Run one copy of each Bastion service. Admission accounting and repository
materialization caches are process-local. This profile does not claim multi-host
availability, SSH access, external identity integration, or safe handling of
sensitive production repositories.
