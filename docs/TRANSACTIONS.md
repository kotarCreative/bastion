# Transactions

## Commit Point

The object-store manifest is the only repository commit point. Pack and WAL uploads are immutable prerequisites. A conditional manifest update commits a record; unreferenced uploaded objects are harmless garbage.

## Receive Flow

1. Resolve the active view and captured descriptor from WAL replay.
2. Validate exactly one fast-forward update to the configured branch.
3. Inspect quarantine limits, commit topology, metadata, tree entries, and path capabilities.
4. Reconcile submitted commits onto the current canonical tip.
5. Build canonical and submission-evidence packs.
6. Upload immutable packs and a linked WAL record.
7. Compare-and-swap `manifest.json`.
8. Return success; caches and indexes may catch up independently.

The event fingerprint makes retries deterministic. An optional HTTP idempotency key is repository-scoped, hashed into the committed record, and may not be reused for another request.

## Record Kinds

- `import`: initial canonical branch, pack, and existing logical-change claims.
- `view_create`: principal, descriptor generation, projection epoch, and canonical frontier.
- `submission`: canonical transition, exact submitted evidence, mappings, transport identity, and optional idempotency hash.
- `canonical_update`: authorized administrative fast-forward, captured descriptor identity, canonical pack, and received-at timestamp.
- `policy_transition`: atomic descriptor and projection-epoch changes for all active views of one principal.

Each record has an exact validated shape, contiguous sequence, previous-record pointer, publication ID, event fingerprint, and event ID. This greenfield implementation intentionally omits format-version fields because no legacy formats exist.

## Descriptor Capture

View submissions and creations record the assigned descriptor identity and generation; the view keeps that assignment until a policy transition. Administrative canonical updates resolve one fresh descriptor from the catalog snapshot current when the command starts and capture its identity in the event fingerprint and payload. The captured descriptor is the only policy input used to authorize the update, remains fixed for retries and ambiguous-write resolution, and is uploaded as an immutable object before the WAL record commits.

## Derived Work

Database transaction rows, change rows, active-view rows, audit records, and projection jobs are derived control-plane data. Losing them cannot revoke or create a committed event. Workers materialize the current manifest generation and may retry, cancel, or dead-letter freshness jobs without changing repository truth.

## Concurrency

Writers optimistically build from a pinned manifest. If manifest CAS loses, publication reloads repository truth and either recognizes the same event as committed or rebuilds against the winning generation. Canonical extension validation and descriptor checks run again before a later attempt can commit.
