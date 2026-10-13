# Roadmap

## Current Baseline

Bastion currently provides:

- immutable object-store WAL repository truth selected by manifest CAS;
- disposable canonical, projected-view, and intake Git materializations;
- deterministic path projection and capability-based authorization;
- users, service accounts, teams, roles, bindings, and content-addressed policy descriptors;
- exact submitting-view commit continuity and synthetic cross-view representations;
- `Change-Id` ownership across Git representations;
- synchronous receive validation with immutable canonical and evidence packs;
- WAL-backed request idempotency and crash-safe publication;
- optional projection jobs for cache freshness;
- SQLite, PostgreSQL and Cloudflare Durable Object control-plane indexes with leases and checksums;
- pinned-generation backup and verified restore;
- a read-only repository invariant checker;
- smart HTTP with per-view tokens and isolated intake repositories;
- integration, adversarial, concurrency, and failpoint coverage.

## Multi-User Forge Priority

The product goal is a multi-user Git hosting alternative with stronger
repository- and path-level permissions, not just a single-repository demo.
Track the implementation contract in
[Multi-Repository Accounts](.specs/017-multi-repo-accounts.md).

1. **Independent repositories:** approved accounts own multiple forge roots,
   with isolated catalogs, histories, permissions, and credentials. Local hosting
   and the Cloudflare host/per-repository catalog adapter are implemented.
2. **Usable hosted workflow:** account sessions independent of repository views,
   private repository discovery, creation and switching in the browser, scoped
   collaborator discovery, and namespaced HTTPS clone/push. The served slice is
   implemented locally and on Cloudflare with Artifacts-backed objects and
   disposable container disk. Initial host admins can approve accounts in the
   dashboard; live rollout and distributed admission remain operations work.
3. **Bring existing code:** bounded, recoverable self-service history import,
   with clear author/branch constraints. Bounded single-branch first-push import
   using Git smart HTTP(S) and pending-operation recovery is implemented for
   local and Cloudflare hosts, with digest-pinned Artifacts intake for cold recovery.
   Remote URL imports, raw-history owner-view compatibility,
   cancellation, and staged source garbage collection remain separate work.
   Seeded-empty creation must not pretend
   to accept an unrelated existing local history by a normal first push.
4. **Operate safely:** isolated production catalogs/object stores, TLS,
   distributed quotas, deprovisioning and credential revocation, auditability,
   and tested backup/restore and migration for the whole host.
5. **Review and integration workflows:** permission-aware code review, protected
   branches, status checks, and authorized CI/artifact access. These remain
   necessary work before claiming broad GitHub workflow compatibility.

At every stage, repository discovery is not authorization, cached identity is
not permanent authority, and a policy must restrict both Git objects and browser
metadata. Administrative rights on one repository must never grant host-wide
rights or access to another owner's private repositories.

## Git Compatibility

### Scoped branch transport

Capabilities-v4 now applies explicit branch `read`, `create`, and `update`
permissions to a private multi-ref projection and narrow single-ref HTTP Git
transport. v3 remains default-branch compatible; its secondary refs stay
denied. This does not implement branch deletion, arbitrary-history imports,
merge workflows, or PR lifecycle. Repository PR permissions remain policy
only, not an implemented PR feature. See [spec 018](.specs/018-multi-branch-foundation.md),
[spec 019](.specs/019-scoped-permissions.md), and
[spec 020](.specs/020-scoped-branch-transport.md).

Remaining work includes richer client workflows, policy UI and PR lifecycle,
plus compatibility/security verification for policy contraction, canonical
frontier races, cold-cache rebuilds, and additional Git protocol edge cases.

- Maintain the [Git compatibility matrix](docs/GIT_COMPATIBILITY.md) for repack,
  garbage collection, commit graphs, bitmaps, multi-pack-index, protocol
  versions, exact OID wants, and shallow history.
- Expand maintenance coverage to cruft, stale indexes, missing packs, and
  adversarial delta-base selection.
- Define partial clone support and shallow behavior across policy epochs.
- Decide and implement SHA-256 repository support.
- Define tags, signed tags, and source-signature attestations.
- Add Git LFS authorization.

## Server Boundary

- Complete Clerk production rollout with TLS, webhook-driven deprovisioning,
  distributed rate limiting, quotas, and administrator invitation management.
- Add a production SSH entry point.
- Re-run hidden-object and thin-pack tests against both transports.
- Add monitoring, tracing, alerting, and service-level objectives.

## Forge Product

- Define review lifecycle, patchsets, abandonment, and merge.
- Add permission-aware browsing, history, blame, diffs, APIs, archives, and webhooks.
- Add protected branches, status checks, and server-created rebase workflows.
- Add CI with explicit source-view authorization and artifact controls.

## Scale

- Test large histories and hundreds of overlapping views.
- Measure publication latency, projection latency, pack growth, and recovery time.
- Add safe pack compaction, cache invalidation, and format upgrade procedures.

## Deferred

- arbitrary client-created merge commits;
- force pushes;
- partial acceptance of multi-commit pushes;
- shared canonical alternates as an endpoint security boundary;
- custom VCS replacement.
