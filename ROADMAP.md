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
- SQLite and PostgreSQL control-plane indexes with leases and checksums;
- pinned-generation backup and verified restore;
- a read-only repository invariant checker;
- smart HTTP with per-view tokens and isolated intake repositories;
- integration, adversarial, concurrency, and failpoint coverage.

## Git Compatibility

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
