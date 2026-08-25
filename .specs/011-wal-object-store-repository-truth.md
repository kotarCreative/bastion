# WAL Object-Store Repository Truth

## Contract

Repository truth is a canonical JSON manifest and its linked immutable WAL chain. Local Git repositories, database indexes, and worker jobs are caches. Only manifest compare-and-swap commits repository state.

All persisted structures carry numeric structural format versions and reject unknown versions or unexpected fields. The supported database schema is created directly; no historical readers, upgrade routines, or cutover modes are part of this project.

## Root Layout

```text
repository.json
backend.json
store.sqlite3                 # default control plane
object-store/repos/<id>/
  manifest.json
  wal/<seq>/<event>.json
  packs/canonical/<hash>.pack
  packs/canonical/<hash>.idx
  packs/submissions/<hash>.pack
  packs/submissions/<hash>.idx
  descriptors/<id>.json
materializations/
canonical.git                 # symlink to active generation
views/<view>.git              # symlink to active generation
intake/<view>.git             # disposable receive repository
```

## Manifest And WAL

The manifest records repository identity, object format, canonical ref and tip, head sequence and pointer, canonical pack inventory, revision, writer, and update time. The head pointer binds sequence, key, body hash, event ID, and publication ID.

Records are contiguous and point to the preceding key and hash. Record kinds are `import`, `view_create`, `submission`, `canonical_update`, and `policy_transition`. Payload validators require exact fields, immutable full OIDs, safe object keys, descriptor provenance, monotonic generations, and legal state transitions.

Event IDs and idempotency-key hashes use fixed domain-separated SHA-256 encodings. Record fingerprints contain only stable request identity. Publication IDs distinguish attempts without changing event identity.

## Immutable Packs

Pack references contain namespace kind, object-store keys, SHA-256 hashes, Git checksum, sizes, and object count. Canonical packs reconstruct complete canonical history. Submission packs preserve exact source-view commits. Descriptors are content-addressed canonical JSON objects.

Every reference is validated before replay and hash-checked during backup and invariant checking. Missing or mismatched manifest-reachable objects are corruption.

## Replay

Replay begins with exactly one import, validates every link and payload, and derives canonical state, active views, change claims, request identity, and idempotency state. A view creation projects canonical history from the empty root. A policy transition starts a parentless projection epoch. Submissions preserve exact source commits in the submitting epoch and produce deterministic synthetic commits for other views.

## Publication

Writers load one manifest generation, validate against replayed state, upload immutable prerequisites, upload the next record, and conditionally update the manifest. On a CAS loss, a writer reloads and either returns the already-committed event or rebuilds against the new head. Ambiguous writes are resolved by rereading manifest reachability.

## Control Plane

The database stores exact-shape repository metadata, identity catalogs, derived transaction/change/view indexes, projection jobs, leases, and audit entries. Repository lifecycle is `initializing` or `ready`. `repository_id` and canonical branch must agree with repository configuration and manifest truth.

Opening a write session refreshes derived indexes from WAL. Jobs may improve cache freshness but never determine commitment. The read-only invariant checker permits absent caches and rejects repository-ID fences, malformed chains, missing packs, hash mismatches, and unsafe view alternates.

## Initialization And Restore

Initialization writes the control-plane fence, canonical repository configuration, import record, canonical materialization, and administrative staging repository, then marks the repository ready. Repeating an interrupted initialization with the same branch resumes safely.

Backup pins one manifest generation and copies exactly its reachable records, packs, descriptors, configuration, token file, and control-plane snapshot. Restore verifies every copied hash and repository fence before rebuilding disposable caches.

## Acceptance

- Unknown structural versions and extra persisted fields fail closed.
- No database row, local ref, job, or hook callback can commit an event.
- Acknowledged receives are manifest-reachable.
- Unacknowledged objects remain unreachable unless their event committed before interruption.
- Cache deletion cannot change authorization or commitment.
- View object databases contain no canonical alternate and no hidden object closure.
- Retries and concurrent writers converge on one contiguous manifest chain.
