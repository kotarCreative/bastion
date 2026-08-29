# Permission Integrity And Verifiable Authorization

## Contract

Every effective capability prefix that can affect longest-prefix adjudication is preserved in its descriptor. Every administrative canonical commit transition is authorized by one immutable descriptor captured in the committed WAL event. The read-only invariant checker independently verifies the Git evidence and authorization claims represented by repository truth.

This work is a correctness gate for any hosted demonstration. It does not make the HTTP transport production-ready and does not add a web product surface; those are specified separately.

## Problems

The current implementation has four related gaps:

- allow and deny prefixes are minimized independently, which can remove a nested prefix that would win after the two maps are compared;
- administrative updates authorize only the net tree difference between the old and new tips, not every intermediate commit transition;
- `canonical_update` records identify the principal but not the immutable descriptor used to authorize the update;
- the invariant checker validates pack hashes and active-view object closure, but not the graph, policy, projection, or metadata claims documented as hard invariants.

The prefix issue can fail open. Given `allow = ["shared/exception"]` and `deny = ["shared", "shared/exception/private"]`, independently removing the nested deny leaves only `deny = ["shared"]`; the longer allow then incorrectly grants `shared/exception/private/file`.

## Prefix Semantics

Capability maps store normalized, sorted, unique path prefixes without ancestor minimization. This applies independently to every capability in both `capabilities` and `restrictions`.

For one capability and path:

1. Find the longest component-boundary allow prefix covering the path.
2. Deny when no allow covers the path.
3. Find the longest component-boundary deny prefix covering the path.
4. Allow when no deny covers the path.
5. Otherwise allow only when the allow prefix is strictly longer than the deny prefix.

Equal specificity is denied. A nested allow may override a broader deny, and a still-narrower deny may override that allow. Prefix order and role or binding insertion order never affect the result.

`normalize_capability_map` and `effective_grants` must therefore normalize path syntax and perform only sorting and exact deduplication. No helper used to construct a descriptor may remove a prefix merely because an ancestor exists in the same map. Descriptor validation independently requires every persisted capability and restriction list to equal its normalized, sorted, exact-deduplicated form.

## Descriptor Version

The descriptor algorithm changes from `capabilities-v2` to `capabilities-v3`. The v3 hash preimage remains `b"bastion-effective-view\0"` followed by canonical ASCII JSON of every descriptor field except `id`, including `algorithm`. Control-plane and object-store records must be semantically equal exact-shape objects; only object-store descriptors are required to use the canonical byte encoding. Any changed prefix set necessarily receives a new ID.

This project does not provide persisted-format migrations because it is a greenfield implementation. Manifest, repository configuration, WAL records, backups, control-plane schema, and materialization cursors intentionally omit numeric format-version fields. Persisted structures are validated against their exact current shape on every read and replay; tests and the hosted demo initialize new roots.

## Administrative Authorization

`update-canonical` resolves one fresh effective descriptor from a locked, checksummed catalog snapshot for the acting principal before repository publication begins. Catalog additions remain dormant for view submission and projection until `transition-principal`, but an explicit administrative command uses the catalog snapshot current when that command starts. Resolution, semantic control-plane persistence, and canonical descriptor object upload must produce the same descriptor ID.

The captured descriptor must:

- identify the acting principal and configured canonical ref;
- be persisted in `descriptors/<id>.json` before the WAL record can commit;
- remain fixed for the publication attempt and any ambiguous-write resolution;
- be the only policy input used to authorize the update;
- remain available after catalog additions or control-plane index reconstruction.

Descriptor generation is a view-assignment concept and does not apply to a viewless administrative action. A `canonical_update` captures `descriptor_id`, while view-originated records continue to capture both descriptor ID and assignment generation. Documentation must make this distinction explicit.

### Per-Commit Validation

For the exact first-parent range `canonical_old..canonical_new`, process commits oldest first. For each commit:

1. Require a linear fast-forward transition from the preceding OID.
2. Reject unsupported entries, merges, empty subtrees, and malformed graph endpoints as today.
3. Compare the complete parent and child trees.
4. Collect every changed leaf path, including paths changed and later restored within the submitted range.
5. Require the captured descriptor's `admin` capability for each changed path using the standard allow/deny adjudicator.
6. Advance the parent only after that transition passes.

Any failed transition rejects the entire administrative range before immutable pack upload or manifest movement. Authorization must not collapse the range into an old-tip versus new-tip diff.

### Receive Time

A submission captures one integer `received_at` before metadata validation. The same value defines the future timestamp deadline and is written into the WAL payload. Validation must not sample wall-clock time again for that request. This makes the committed timestamp decision reproducible after clock adjustments.

## WAL Shape

The new exact `canonical_update` payload is:

```text
principal_id
descriptor_id
canonical_ref
canonical_old
canonical_new
canonical_pack
commits[] { canonical, change_id }
```

The descriptor ID is part of the event fingerprint. WAL validation loads the descriptor and requires its principal and canonical ref to match the event. Replay fails closed when the descriptor is missing, malformed, content-address mismatched, or bound to another principal or ref.

Backup reachability includes every descriptor referenced by `canonical_update`. Restore verifies those descriptor objects before accepting the root.

## Global Invariant Checker

`forge.py check` remains read-only with respect to the forge root, object store, and control-plane database. It pins and validates one manifest generation before opening catalog evidence. It then reads only immutable principal and author-email rows through a dedicated backend path: SQLite opens in read-only mode without changing pragmas or sidecars, and PostgreSQL begins a read-only transaction without bootstrap or database-creation DDL. It does not load or validate rebuildable transaction, change, view, job, lease, or audit rows. It may create private temporary Git repositories outside the root and must remove them on success or failure. Missing disposable caches remain legal.

The checker validates the following stages from manifest-reachable data rather than trusting database-derived indexes, materialization cursors, active symlinks, or worker jobs. Immutable principal and author-email records are authoritative control-plane catalog input because they are not present in descriptors or WAL; the checker loads their exact checksummed rows but does not trust derived transaction, change, or view indexes.

### Repository And Graph

- Validate the manifest, linked WAL chain, exact record shapes, descriptor hashes, pack hashes, pack indexes, and repository fences.
- Install referenced packs into temporary repositories in WAL order.
- Invoke graph-dependent record validation for every import, submission, and canonical update.
- Require every recorded range and mapping list to equal the exact oldest-first first-parent range represented by its packs.
- Require canonical and submitted endpoints to exist with the expected Git object types.

### Authorization

- Recompute every administrative commit transition against the event's captured descriptor.
- Recompute every submitted tree transition against the submission descriptor and its required `read`, `create`, `modify`, `delete`, `mode`, and `symlink` effects.
- Require each reconciled canonical tree to preserve hidden canonical entries and to reproject exactly to the submitted tree.
- For submissions, reject descriptor principal, ref, assignment-generation, or projection-epoch mismatches.
- For view creation, verify the initial descriptor, principal, ref, generation 1, and projection epoch 1.
- For policy transitions, verify old and new assignment IDs, generations, epochs, principals, refs, and exact active-view coverage required by replay.
- For canonical updates, verify descriptor principal and ref without inventing a view assignment generation or epoch.

### Metadata And Logical Changes

- Recheck declared author email bindings for every exact submitted commit.
- Recheck future timestamp bounds against the event's recorded receive time.
- Parse `Change-Id` only from the final trailer block and reconstruct ownership in WAL order.
- Require imported and administratively introduced ownerless claims, principal-owned submission claims, and canonical refs to match replayed claim state.

### Projection And Isolation

- Reconstruct each active view's current projection epoch in a temporary repository from WAL records and immutable submission evidence.
- Require its projected ref, tip tree, and visible history to match deterministic replay under the assigned descriptor.
- Require the temporary view object inventory to equal the reachable closure of its single advertised ref and contain no alternate.
- When an active on-disk cache exists, retain the current exact-closure and no-alternate checks without treating that cache as evidence for repository truth.

Failures use stable categories such as `REPOSITORY`, `GRAPH`, `AUTHORIZATION`, `METADATA`, `PROJECTION`, and `ISOLATION`, name the WAL sequence or view where practical, and never include hidden path contents in output intended for remote clients.

## Documentation

Update `README.md`, `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, `docs/TRANSACTIONS.md`, and `docs/TESTING.md` so they describe implemented behavior exactly.

In particular:

- distinguish view assignment generations from administrative descriptor capture;
- state that explicit administrative updates use a fresh catalog snapshot while view operations use their assigned descriptor until transition;
- remove references to obsolete plans, anchors, revisions, and worker descriptor checks that are not part of current WAL replay;
- state which hard invariants `forge.py check` independently verifies;
- document the descriptor algorithm change and the intentional absence of format-version fields in this greenfield implementation;
- consistently describe the complete set of ten capabilities;
- keep the statement that downloaded data cannot be revoked retroactively.

## Tests

Unit and integration tests must cover at least:

- broad allow, nested deny, and narrower allow;
- broad deny, nested allow, and narrower deny, proving the narrower deny is not discarded;
- equivalent role unions in different creation and binding orders producing identical descriptors;
- exact duplicate prefixes being removed without removing ancestors or descendants;
- persisted descriptors with unsorted, duplicate, non-normalized, or ancestor-minimized capability lists being rejected even when their content-addressed ID is recomputed;
- read projection and every write effect using the corrected descriptor semantics;
- an administrative range that changes an unauthorized path and later restores it being rejected;
- a mixed multi-commit administrative range with every transition authorized being accepted;
- a `canonical_update` carrying and resolving its descriptor ID after catalog changes and index recovery;
- the canonical-update fingerprint changing with descriptor ID and retaining one captured ID across retry and ambiguous-write resolution;
- rejection of an unauthorized intermediate administrative transition before any canonical pack upload;
- missing, corrupted, wrong-principal, and wrong-ref administrative descriptors failing WAL replay;
- backup and restore retaining administrative descriptors;
- one captured receive time governing both timestamp admission and later checker verification, including a simulated clock adjustment;
- checker detection of invalid mapping ranges, unauthorized transitions, bad author bindings, bad timestamps, malformed final trailers, incorrect claims, incorrect projections, descriptor assignment mismatches, pack/index inconsistencies, and hidden dangling objects;
- stable checker failure categories and redaction of hidden content;
- checker success with every disposable Git cache deleted;
- filesystem snapshots proving successful and failed checks do not alter forge-owned files, plus logical row, version, and checksum equality proving neither SQLite nor PostgreSQL control-plane state changes;
- restore verifies the backup format name and leaves no target behind on error; no versioned format preflights are required.

The existing compile, unit, adversarial, process, Git compatibility, and backup suites must continue to pass after these changes.

## Acceptance

- No capability prefix that can win longest-prefix adjudication is removed from a descriptor.
- No administrative commit transition can touch a path outside captured `admin` coverage, even when a later commit restores the path.
- Every submission and canonical update references the immutable descriptor used for its authorization decision; assignment-changing records retain their separately validated old and new descriptors.
- Repository replay and backup include administrative descriptors in their reachable closure.
- `forge.py check` independently validates the documented graph, authorization, metadata, projection, and isolation invariants without mutating repository state.
- No versioned legacy persisted data exists to be reinterpreted; all persisted structures are validated against their exact current shape.
