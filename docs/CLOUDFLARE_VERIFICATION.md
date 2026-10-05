# Cloudflare port verification

Branch: `feat/cloudflare-artifacts`.

## Results

On October 4, 2026, the complete local suite ran **564 tests successfully**,
with the one opt-in live AWS S3 test skipped. The Artifacts adapter's 18 tests
also passed again after tightening its blob ceiling to 32 MB. Worker TypeScript
checking, Wrangler packaging without container rollout, checkpoint CLI help,
and `git diff --check` passed.

## Coverage

The Artifacts adapter tests use real bare Git remotes. They cover conditional
create/update/delete, simultaneous writers, shared caches across processes,
stale versions, uncertain write outcomes, chunked objects, integrity checks,
remote outages, and deletion of the local cache. A cached ref is never used as
authority when the remote is unavailable.

The forge migration tests exercise projected pushes, hidden-only changes,
policy epochs, and backup/restore between Artifacts stores. They check retained
repository identity, WAL records, permissions, passwords, scoped tokens, and
the absence of hidden Git objects from restricted views.

The hosted runtime tests use the **production** Artifacts adapter over a local
TLS Git smart HTTP endpoint. The fixture validates its backing Bearer token;
its proxy and certificate settings apply only to the test's Git caches. Git's
separate receive hook runs without replacing the adapter. These tests cover:

- Interrupted first bootstrap, followed by deletion of all local forge/cache
  files and resumption of the same repository identity.
- Password login and actual authenticated Git HTTP clone and push.
- Permission changes and denial of access to the private control snapshot.
- A failed checkpoint after policy WAL publication. The test inspects the
  last durable SQLite snapshot, deletes local disk state, then verifies replay
  of the newer WAL and preservation of both hidden-path denials.
- Login, an existing scoped token, and Git fetch after cold recovery. A later
  permission edit preserves the other restricted path.

## Reproduce

From the repository root, with the project dependencies installed:

```sh
.venv/bin/python -m unittest discover -s tests -t .
.venv/bin/python -m unittest tests.test_object_store_artifacts \
  tests.test_artifacts_integration tests.test_cloudflare_runtime \
  tests.test_cloudflare_runtime_integration -v
git diff --check

cd deployment/cloudflare
npm ci
npm run check
npx wrangler deploy --dry-run --containers-rollout=none
```

The HTTPS fixture additionally requires Git and OpenSSL. Worker type checking
and the dry run passed with Wrangler 4.147.0 and Containers 0.3.7. The dry run
recognizes the Artifacts binding, Durable Object, and root Dockerfile. Omitting
container rollout skips image construction; it does not validate the image or
execute a Cloudflare deployment.

## Remaining live checks

This session has no accessible Docker daemon or configured Cloudflare
credentials. The Container image build and live Cloudflare provision/clone/
push/restart checks have therefore **not** run. The local TLS fixture verifies
the Git protocol integration, rather than Cloudflare service behavior.

Follow [the deployment guide](../deployment/cloudflare/README.md) to run those
checks with a Cloudflare account. The hosted profile uses SQLite and one
Container; Artifacts retains historical versions and has a repository capacity
limit. The guide describes migration and a concurrent-agent demonstration.
