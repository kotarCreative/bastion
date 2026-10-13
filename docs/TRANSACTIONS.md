# Transactions

## Commit Point

The object-store manifest is the only repository commit point. Pack and WAL uploads are immutable prerequisites. A conditional manifest update commits a record; unreferenced uploaded objects are harmless garbage.

## Receive Flow

1. Resolve the active view and current descriptor from WAL replay.
2. Validate exactly one authorized fast-forward update to a branch ref. v3 is
   restricted to its configured default branch; v4 requires target `read` and
   `update` for an existing ref. Creating a ref requires target `read` and
   `create`, plus `read` on the current advertised source even for a pure
   fork. Creation starts from that projected tip; if it adds commits, target
   `update` is also required.
3. Inspect quarantine limits, commit topology, metadata, tree entries, and path capabilities.
4. Reconcile submitted commits onto the current canonical frontier for that
   branch. All commit transitions remain subject to path capabilities.
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
- `branch_create`: root-administrator creation of an internal canonical ref at an existing canonical tip, capturing principal, descriptor, source ref, destination ref, tip, and receipt time. It does not upload a pack or claim `Change-Id` ownership.

HTTP transport does not accept force updates, deletions, multiple refs, tags,
merge commits, arbitrary historical branch bases, or stale branch-creation
proofs. These rejections happen before repository truth is changed. PR policy
rights do not imply a merge workflow.

Each record has an exact validated shape, contiguous sequence, previous-record pointer, publication ID, event fingerprint, and event ID. Existing immutable records are unchanged. Manifests now carry a plain-dict `canonical_refs` mapping of full branch refs to SHA-1 tips; `canonical_ref` and `canonical_tip` remain default-branch aliases. Legacy manifests synthesize a singleton mapping when read and are upgraded on the next publication.

Branch refs are public through HTTP only when the active view's transport policy
authorizes them and a validated private projection contains their object
closure. Canonical operator administration remains separate. Deletion, merge,
and PR workflows are not implemented; end-user branch transport is limited to
the operations documented above.

## Descriptor Capture

View submissions and creations record the assigned descriptor identity and generation; the view keeps that assignment until a policy transition. Administrative canonical updates resolve one fresh descriptor from the catalog snapshot current when the command starts and capture its identity in the event fingerprint and payload. The captured descriptor is the only policy input used to authorize the update, remains fixed for retries and ambiguous-write resolution, and is uploaded as an immutable object before the WAL record commits.

## Derived Work

Database transaction rows, change rows, active-view rows, audit records, and projection jobs are derived control-plane data. Losing them cannot revoke or create a committed event. Workers materialize the current manifest generation and may retry, cancel, or dead-letter freshness jobs without changing repository truth.

## Concurrency

Writers optimistically build from a pinned manifest. If manifest CAS loses, publication reloads repository truth and either recognizes the same event as committed or rebuilds against the winning generation. Canonical extension validation and descriptor checks run again before a later attempt can commit.
