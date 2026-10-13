# Cloudflare port verification

## Multi-repository integration — October 12, 2026

The current entry point is the empty multi-repository host, not the historical
single-forge/demo runtime described below. Its local verification passed:

- Full Python regression: **726 passed, 1 opt-in test skipped**, plus 319 unittest
  subtests. Final follow-up regression: **70 passed**, plus 33 subtests, covering
  the latest recovery barriers, bootstrap and repository transport changes.
- Cloudflare Worker: **9 Vitest tests**, TypeScript checks, regenerated bindings,
  and Wrangler dry-run packaging without deployment or Container rollout.
- Browser: all four SPA suites, including scoped host/repo authentication,
  account approval, import credentials/retries and stale-response isolation.
- Docker image build, empty-host bootstrap inside the image with networking
  disabled, and Compose configuration with an isolated validation environment.

`tests/test_cloudflare_host.py` runs real workerd SQL with an authenticated HTTPS
Artifacts fixture. It verifies empty-host onboarding, independent repository
catalogs/branches, cross-repository token rejection, native first Git push,
collaborator visibility/allowed writes/denied hidden writes, and preservation of
identities, history, policy and tokens after deleting the container workspace and
restarting workerd. Provisioning and import interruption tests recover the same
durable repository identity without re-seeding it. Catalog outages fail closed.

The test suites above are local integration evidence. A live deployment and
public-endpoint verification followed with explicit operator authorization; see
below. A complete authenticated browser journey through real Clerk is still
not verified. Whole-host backup/migration and coordinated deprovisioning remain
separate work.

### Live multi-repository deployment

On October 12, the `bastion-forge` Worker and Container image were deployed to
`https://bastion-forge.kotarcreative.workers.dev` with immediate Container rollout.
Existing Clerk and administrator secrets were retained.

- Worker version: `a65913f3-61be-4f6a-8c57-ca327787b362`.
- Container image digest:
  `sha256:5d60cf00295df30176c042c1780efdc757b55b4365456a072283548f7941221c`.
- `/healthz` and `/readyz`: HTTP 200.
- `/api/v1/auth/config`: HTTP 200, Clerk enabled, host mode, first-push import.
- `/` and `/assets/app.js`: HTTP 200, multi-repository dashboard, import and
  administrator account-approval UI present.
- Unauthenticated session, repository and host-account APIs: HTTP 401.
- Old `/owner.git` and absent `/admin/demo.git` Git routes: HTTP 404.
- Container inventory reports the new application version running; live tail
  confirms successful private host-catalog SQL requests through `ContainerProxy`.

The first health request during rollout returned Worker error 1101; subsequent
readiness and health requests succeeded. Python urllib's default client received
edge error 1010, so the live endpoint checks used curl. Operator SSH inspection
was unavailable (Wrangler's WebSocket connection returned 400); it is not used
by normal application traffic.

With explicit permission to discard old demo data, the old `bastion-storage`
branch was deleted from the `bastion-production` Artifacts repository using an
exact-ref lease. That repository itself was retained for the new host's branches.
The temporary cleanup token was revoked. Legacy singleton DO storage and the
separate preview Artifacts repository were not purged, and are not adopted by
the new host. Sign in with the configured bootstrap account to complete the
authenticated create/import/permissions journey.

## Historical single-repository verification

Branch: `feat/cloudflare-artifacts`.

## Results

On October 5, 2026, the new catalog passed eight transaction tests in Cloudflare's
Vitest/workerd runtime and eight Python integration tests against real local
Durable Object SQL. Worker and test TypeScript checks, Wrangler packaging
without Container rollout, Python compilation, and `git diff --check` passed.
The targeted catalog/storage/backup run also passed 26 tests before the final
bootstrap recovery case was added. The complete regression run passed **567 tests with one opt-in live AWS S3
test skipped** in 730.904 seconds. The final five-case DO suite was run again
after the index and bootstrap recovery changes and passed in 66.443 seconds.
Subsequent staged transport and paginated export changes passed the original
five DO tests again (65.892 seconds) and three new large-catalog/retry/restart
tests (8.574 seconds). The final DO/storage/backup regression run passed
**30 tests in 112.986 seconds**, after bulk-read pagination was added. All eight
Worker transaction tests and both TypeScript checks passed afterward.

On October 6, the final Worker also passed a clean local Docker preview against
the paid account's remote Artifacts repository. A cold `GET /healthz` started
the `ForgeContainer` and egress sidecar, reached the private catalog binding,
read Artifacts, and returned HTTP 200 in 5.119 seconds. A warm request returned
HTTP 200 in 0.009 seconds. The eight Worker tests and both TypeScript checks
passed again after the outbound-handler registration fix.

The original Artifacts profile's October 4 baseline ran 564 tests with one
opt-in AWS S3 test skipped. That evidence covers the legacy snapshot runtime;
the new profile stores SQL tables directly in `ForgeCatalog` and does not
publish SQLite checkpoints on normal mutations.

## Live Cloudflare evidence

Wrangler 4.147.0 authenticated to account `d2222d480efd386820ce7e75ab3968dc`
(`kotarcreative`). On October 5, deploying both the original configuration and
the final Durable Objects configuration with `--containers-rollout=none`
reached the Cloudflare API and failed with **10403** because the account had not
yet enabled Workers Paid. The account was upgraded before the October 6 local
preview. Remote Artifacts provisioning then succeeded and created the private
`bastion-private` repository. No Bastion production Worker was installed.

An isolated `bastion-catalog-smoke` Worker deployed successfully with the
production `ForgeCatalog` class and a private random test credential. The
production Python DB-API adapter verified schema initialization, read-your-writes,
rollback, committed writes, and rejection of stale writes against its live DO.
Unauthenticated access returned 404. A subsequent Worker redeployment preserved
the committed row and catalog revision 2:

- Initial Worker version: `47df1d5c-6753-4689-b144-92681cfecf45`.
- Redeployed version: `89e530aa-2ed1-424b-9972-a3b0da5d2ac6`.
- Persistent synthetic row: `teams.key = live-smoke`.

The smoke Worker and its associated resources were deleted after verification,
and the protected temporary credential file was removed. A final API inventory
confirmed only the pre-existing unrelated Worker remained. This establishes
live DO SQL behavior; it does not establish the full Container/Artifacts bridge.

A second isolated smoke deployment tested staged SQL streams and paginated
reads/exports against Cloudflare. `live_catalog_smoke.py` staged 100 synthetic
principal rows whose catalog exceeds 8 MiB, verified that upload left the live
table empty, and published the stream with repository configuration once.
Retrying final commit returned the same revision. After redeployment, the
production bulk-read adapter read all 100 rows with their checksums, and a
portable SQLite export preserved them exactly (**10,149,888 bytes**, revision 2).

- Staged-publication version: `f6249a55-73da-4567-8410-3731939172d3`.
- Redeployed/paginated-reader version: `da567249-f055-41e9-b1e2-316c0361ac3f`.

The second smoke Worker and protected local credential were also removed after
verification. This adds live large-catalog evidence independent of the
Container networking path.

The later local preview exercised that missing path. Wrangler built the root
Dockerfile, started the application and `proxy-everything` containers, routed
`catalog.bastion.internal` through `ContainerProxy`, and used the remote
Artifacts Git endpoint. On this Linux host, workerd first required two host
networking corrections: new network namespaces must not inherit Tailscale's
global `src_valid_mark=1`, and UFW must allow the sidecar to reach workerd's
ephemeral TCP listener on `docker0`. The deployment guide records scoped rules
that retain Tailscale's host setting and avoid allowing all bridge traffic.

## Durable Object coverage

`tests/test_durable_objects.py` starts Wrangler's real workerd runtime with
persistent test DO storage. It covers:

- Atomic transaction rollback, optimistic revision fences, and a lost commit
  response retried exactly once without duplicate writes.
- Actual authenticated Git smart HTTP clone and push, including the receive
  hook subprocess using the production DO backend.
- A private-path permission epoch and restricted clone/fetch after recovery.
- Deletion of the forge and Artifacts cache, plus restart of workerd itself:
  repository identity, Clerk identity links, scoped tokens, pushed content, and denials
  survive without any live `store.sqlite3` file.
- Interrupted schema initialization and interrupted catalog bootstrap, followed
  by recovery without resetting committed state.
- A stopped DO runtime preventing startup, without fresh bootstrap.
- One-time legacy SQLite snapshot import, retained identity/accounts/tokens,
  retained audit uniqueness indexes, and no subsequent snapshot reads.
- Portable SQLite backup export and restoration to a regular SQLite host.
- A legacy SQLite catalog larger than 8 MiB imported through bounded staging
  frames and exported through bounded pages, with exact document/checksum
  preservation after workerd restart. A concurrent mutation between export
  pages rejects the backup and removes its partial output.
- Bulk SQL reads of the same large table return all rows through bounded,
  revision-fenced pages; existing catalog callers need no pagination changes.
- Pending staged transactions surviving restart without publishing tentative
  writes, rollback discarding them, Unicode characters split across frames,
  and lost stage/commit responses retried without duplicate publication.

The Worker Vitest tests additionally inspect native SQL after speculative
queries and failed commit batches, verify separate forge identities, and check
root configuration commits and private metadata restrictions. A dedicated
Cloudflare CI job installs Wrangler before running this coverage.
The staged-upload tests also verify immutable frame identities, out-of-order
frame rejection, incomplete stream rejection, atomic rollback of failed staged
publication, and alarm cleanup of abandoned frames without catalog changes.

## Existing Artifacts and legacy snapshot coverage

The Artifacts adapter tests use real bare Git remotes. They cover conditional
create/update/delete, simultaneous writers, shared caches across processes,
stale versions, uncertain write outcomes, chunked objects, integrity checks,
remote outages, and deletion of the local cache. A cached ref is never used as
authority when the remote is unavailable.

The forge migration tests exercise projected pushes, hidden-only changes,
policy epochs, and backup/restore between Artifacts stores. They check retained
repository identity, WAL records, permissions, Clerk identity links, scoped tokens, and
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

From the repository root, with Python dependencies installed:

```sh
cd deployment/cloudflare
npm ci
npm run check
npm test
npx wrangler deploy --dry-run --containers-rollout=none
cd ../..
.venv/bin/python -m unittest tests.test_durable_objects -v
.venv/bin/python -m unittest discover -s tests -t .
git diff --check
```

The HTTPS fixture requires Git and OpenSSL. The new tests use Vitest 4.1.11,
Cloudflare Vitest plugin 1.3.6, Wrangler 4.147.0, and Containers 0.3.7. A Python-only
checkout skips the DO integration tests until `npm ci` installs Wrangler; the
new Cloudflare CI job explicitly installs it. Dry-run packaging recognizes
`FORGE`, `CATALOG`, `ContainerProxy`, Artifacts, and the root Dockerfile. Skipping
Container rollout does not build the image or test outbound interception.

`wrangler.smoke.jsonc` can reproduce isolated live catalog tests without paid
Artifacts/Containers; it must receive `BASTION_SMOKE_SECRET` through a protected
secrets file. Its namespace is separate from the production catalog. Remove
only that test Worker after checking persistence across a redeploy.
Use `deployment/cloudflare/live_catalog_smoke.py seed` and `verify` with the
isolated endpoint and protected secrets-file path; see the deployment guide
for the exact invocation. The script refuses to seed a nonempty catalog.

## Remaining live checks

Full provision/login/clone/push/restart on the hosted profile remains unverified.
Workers Paid, remote Artifacts, Docker image construction, local Container
startup, private outbound catalog routing, and cold/warm health checks are now
verified. A live Cloudflare Container deployment and end-to-end hosted Git and
policy workflow have not run.

Follow [the deployment guide](../deployment/cloudflare/README.md) to deploy the
full configuration, exercise authenticated Git and policy updates, stop/restart
the hosted Container, and verify catalog/account/token recovery through its
private outbound binding.
The local TLS fixture verifies the Artifacts Git protocol rather than live
Cloudflare Artifacts service behavior. The isolated DO smoke test verifies real
Cloudflare persistence rather than the Container networking configuration.

The goal remains incomplete until those full hosted checks pass.
