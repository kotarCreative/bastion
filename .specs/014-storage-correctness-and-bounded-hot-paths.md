# Storage Correctness And Bounded Hot Paths

## Contract

Bastion preserves its manifest-CAS repository truth and exact projected-object
isolation while removing three uncovered concurrency and restore hazards and
making normal repository access incremental. PostgreSQL leases are mutually
exclusive and durable, PostgreSQL restore never derives executable SQL from
backup names, concurrent receives never share temporary object storage, and
unchanged requests do not repeatedly scan repository history, control-plane
history, or complete Git object databases.

Performance changes are not permitted to weaken authorization, WAL validation,
pack verification, projected closure, retry determinism, or the independent
`forge.py check` path. Caches and materialization receipts remain disposable
derived state. A missing, stale, or contradictory optimization must fall back to
verified reconstruction or fail closed; it must never become repository truth.

## Dependencies And Scope

This work builds on the repository and authorization contracts in
`.specs/011-wal-object-store-repository-truth.md` and
`.specs/012-permission-integrity-and-verifiable-authorization.md`.

The following valid hosted-service findings are already owned by
`.specs/013-hosted-permission-demo.md` and are release prerequisites rather than
duplicated requirements here:

- digest-only, scoped, expiring, revocable tokens and transactional token audit;
- memory-only browser credentials and removal of legacy `tokens.json`;
- generic client errors, redacted structured logs, and browser security headers;
- strict request framing, streamed request/decompression/response handling, and
  bounded request memory;
- request, Git-process, receive, per-IP, and per-token concurrency limits;
- validated resource-limit configuration and the qualified event/disk budget;
- paginated, bounded browser tree and history APIs.

PostgreSQL is outside the qualified hosted-demo deployment in spec 013, but it
is a supported control-plane backend in the wider prototype. Its correctness
requirements therefore belong here. The incremental repository,
materialization, projection, and worker requirements apply to both backends.

## Confirmed Problems

The current implementation has the following actionable gaps:

- PostgreSQL lease acquisition reads a row and then performs an unconditional
  upsert. Concurrent contenders can both decide an absent or stale lease is
  available, after which the later upsert replaces the first owner.
- `heartbeat_session` updates a PostgreSQL lease and closes an uncommitted
  transaction, so it can report an expiry that was never persisted.
- every ordinary PostgreSQL `open_store` creates a new backend instance and
  reconnects to the administrative database to attempt `CREATE DATABASE`.
- PostgreSQL restore derives table identifiers from `*.ndjson` filenames and
  interpolates them into SQL. A crafted backup filename can execute additional
  SQL during restore. PostgreSQL backup also omits audit rows and emits ephemeral
  storage leases.
- database backup metadata does not inventory or hash configuration or
  control-plane artifacts, and SQLite snapshots retain live lease rows.
- receive candidate storage is named only from `(view, old, new)` and is removed
  before use. Concurrent identical receives can delete or reuse each other's
  Git object directory.
- every `Repository.load` walks and validates the complete linked WAL and
  repeatedly fetches descriptors even when the manifest is unchanged.
- hot Git and demo routes call the bulk `load_document` path, which opens a new
  store and reads every row from every control-plane document table.
- every default locked session performs full WAL recovery, and saving one
  changed aggregate still inventories every control-plane table. Updates use a
  version fetched at save time rather than the caller's loaded baseline, and
  deletes are not version-qualified, so stale scoped writes can overwrite or
  delete a concurrent change.
- warm canonical and projected generations run full `git fsck`; projected views
  additionally run a complete object census and reachable-object walk on every
  resolution. A process restart cannot establish the expected projected tip
  from durable derived state and rebuilds an otherwise valid generation. A new
  projected generation is currently activated after `fsck` but before its first
  exact-closure check.
- the production projector flattens the complete canonical tree and recopies
  every visible blob for every projected commit. Reconciliation performs the
  same full-tree work for each submitted transition.
- each worker claim and completion independently reloads repository truth and
  invokes default recovery; draining many views multiplies historical work by
  the number of jobs.

Recent batching of commit-parent inspection, empty-subtree validation, receive
audit, and some intake callers is retained. Those closed problems must not be
reintroduced. The token-file cache is superseded rather than retained when spec
013 removes `tokens.json`.

## PostgreSQL Lifecycle

Database provisioning is an initialization and restore operation, never an
ordinary connection side effect.

- `initialize_store` and PostgreSQL restore may use a separately supplied
  one-shot administrative DSN to create the root-scoped database and grant its
  runtime owner. The administrative DSN is never persisted.
- The configured and persisted runtime DSN uses a role that lacks `CREATEDB`.
  If no administrative DSN is supplied, initialization requires the scoped
  database to have been provisioned already.
- `open_store`, reads, writes, storage readiness checks, workers, backup, and
  invariant checks connect directly to the existing scoped database through the
  runtime DSN. They issue no `CREATE DATABASE`. Process-only liveness checks do
  not access PostgreSQL or repository storage.
- A missing scoped database produces one stable storage error. Normal open does
  not silently create or initialize it.
- Long-running services use one bounded process-local connection pool per exact
  scoped runtime DSN. The default maximum is 8 connections, configurable from 1
  through 64. Checkout defaults to 5 seconds and is configurable from 0.1
  through 30 seconds; saturation fails with a stable service-unavailable result.
  One process-local request phase or control-plane session checks out at most
  one connection at a time and always returns or invalidates it on exit.
- Short-lived CLI commands may use one direct scoped connection rather than
  starting a pool. Connection management must not share one psycopg connection
  concurrently between threads.
- Database credentials and complete DSNs are never included in client errors,
  audit details, or normal logs.

SQLite retains its durable WAL configuration. Read-only snapshots use a
deferred/read transaction and do not acquire the `BEGIN IMMEDIATE` writer
reservation. Mutating operations, lease operations, and schema creation retain
an explicit write transaction. This specification does not change
`synchronous=FULL` without separate durability measurements.

## Lease Semantics

Lease acquisition is one all-or-nothing transaction over a sorted, unique list
of scopes. Duplicate or unsorted caller input is normalized before SQL so all
contenders lock scopes in the same order.

For each requested PostgreSQL scope, the implementation must atomically do one
of the following while holding the conflicting row lock, or use an equivalent
transactional/advisory-lock protocol:

1. Insert the missing lease for the caller.
2. Replace a lease whose expiry is no later than the transaction's sampled
   current time.
3. Replace a lease proven to belong to a dead process on the same host instance.
4. Report the scope busy without changing its owner or expiry.

An unlocked read followed by an unconditional conflict update is forbidden.
If any requested scope is busy, the transaction changes none of them. A retry
begins a new transaction and samples time again. Serialization failures,
deadlocks, and lock timeouts are bounded by the existing acquisition deadline
and retried without leaking backend exception text to remote callers.

The dead-process shortcut is local-only. Owner identity contains a random
host-local forge identifier, the current Linux boot ID when available, PID, and
process start time. The host-local identifier is atomically created with mode
`0600`, is not copied into backups, and is shared by processes using that local
forge root. An owner from another or an unknown host/boot is considered live
until expiry; a coincident local PID may not steal it early. If a reliable local
host identity cannot be established, dead-process optimization is disabled and
expiry remains authoritative.

Heartbeats accept the exact expected scope set; discovering only the scopes that
still happen to carry an owner token is insufficient. `heartbeat` CLI callers
provide one or more `--scope` values, while an in-process session retains its
acquired set. The implementation verifies and updates the exact count in one
explicit transaction and commits before returning the new expiry. If any
expected scope is absent or has a different owner, the heartbeat rolls back
every update and reports lease loss. Release is likewise transactional.
`save_document` continues to refresh and verify every required lease in the same
transaction as the protected writes.

SQLite and PostgreSQL must expose the same observable lease behavior: exactly
one live owner per scope, all-or-nothing multi-scope acquisition, immediate
same-host dead-owner recovery, expiry-based foreign-owner recovery, committed
heartbeats, and owner-qualified release.

## Backup And Restore Safety

The backup manifest records every copied artifact other than itself. Its exact
inventories cover object-store objects, `repository.json`, a sanitized backend
kind record, and the SQLite image or PostgreSQL table exports. Every entry has a
category, relative ASCII path, byte size, and SHA-256 digest. Restore accepts no
unlisted files in those namespaces and verifies all artifacts before creating
or modifying a target root or database.

The source `backend.json` is not copied because a PostgreSQL DSN may contain
credentials and network location. The backup records only `sqlite` or
`postgres`; restore receives target connection configuration separately. A
backup restores only to the same backend kind in this milestone. A requested
cross-backend restore fails before target mutation rather than selecting files
according to the requested target backend.

Configuration and database artifacts receive the same regular-file,
no-symlink, bounded-read treatment as object-store artifacts. Qualified default
limits are 256 MiB per control-plane artifact, 1 GiB total control-plane bytes,
8 MiB per NDJSON line, and 1,000,000 rows per table. Unsafe or non-integer limit
configuration fails before restore. The exact table set bounds artifact count;
unknown table artifacts are never tolerated.

The PostgreSQL snapshot contains exactly:

- the singleton `forge_meta` row;
- every declared document table, including the token table introduced by spec
  013 when that spec is implemented;
- `audit_log`, preserving its sequence, event association, and other fields.

`storage_locks` is ephemeral coordination state and is not backed up or
restored for either backend. A SQLite backup transaction removes leases from the
snapshot without changing the live source. A restored database begins with no
leases. Extra, missing, duplicate, symlinked, non-regular, oversized, or
hash-mismatched table artifacts reject the backup before deletion or insertion
occurs.

PostgreSQL exports every table through one read-only `REPEATABLE READ`
transaction so document rows, tokens, and audit rows represent one database
snapshot. SQLite uses its online backup snapshot. Neither backend promises an
object-store/database distributed transaction; the spec-013 hosted backup
remains quiesced, and wider backup records the pinned WAL head and database
`last_indexed_seq` so inconsistency is detected and derived rows can be rebuilt.

Restore chooses tables from a fixed code-owned allowlist. A filename, JSON
field, row key, or manifest value is never interpolated into SQL. Dynamic
identifiers use the backend's identifier-composition API even after allowlist
validation; row values remain bound parameters. Restore does not execute more
than one SQL statement per intended call.

Each NDJSON row has an exact shape and bounded line length. Keys, versions,
checksums, JSON values, and audit fields are type-checked before database
mutation. Document checksums are recomputed, keys are unique within a table,
the singleton metadata row occurs exactly once, and malformed JSON or trailing
unknown fields fail closed.

All PostgreSQL table deletion and insertion occurs in one transaction. The audit
sequence generator is advanced transactionally so its next value is strictly
greater than every restored audit sequence. Any parse, checksum, constraint, or
database error rolls back the complete restore. The existing spec-012
requirement that a failed restore leave no accepted target root remains in
force; a database created solely for the failed restore is also removed or left
provably empty and unusable until a later explicit restore.

## Receive Workspace Isolation

Every call that prepares a canonical candidate creates a new private workspace
with `tempfile.mkdtemp` or an equivalent exclusive primitive below a
mode-`0700` transaction parent. The name may contain a diagnostic request hash,
but uniqueness comes from an unpredictable exclusive suffix rather than from
`(view, old, new)`.

- Every publication attempt, including a manifest-CAS retry of the same event,
  owns a different workspace.
- No request performs `rmtree` on a path before exclusively creating and owning
  it.
- Candidate cleanup is idempotent and removes only the workspace captured by
  that candidate. A losing attempt cannot remove a winner's or peer's objects.
- Build failure, rejected authorization, lost CAS, ambiguous-write resolution,
  normal success, and process shutdown all use the same ownership rule.
- Crash leftovers are unreachable garbage. A later bounded cleanup pass may
  remove only workspaces proven not to belong to a live process; startup never
  blindly clears the shared parent.
- Workspace paths and candidate objects never enter WAL payloads, event
  fingerprints, audit rows, or client messages.

Concurrent byte-identical receives may both report the already-committed result
when idempotency permits it, but exactly one WAL event and one canonical
transition become manifest-reachable.

## Incremental Repository Snapshots

`Repository.load` returns an immutable repository snapshot containing the exact
manifest version, validated manifest, validated replay state, linked head, and
descriptors used by that state. A process-local cache keeps at most the latest
snapshot per repository/configuration identity.

- An unchanged load performs one conditional `manifest.json` GET using the
  cached object-store version. The filesystem store recognizes an unchanged
  object from metadata retained with the prior validated read, without reading,
  hashing, or parsing the manifest body again. `NotModified` returns the
  immutable cached snapshot without historical WAL or descriptor reads.
- When the manifest advances, loading walks only the new suffix back to the
  cached head. The first suffix record's exact `previous` sequence, key, and
  hash must identify that cached head before the suffix is applied oldest-first;
  the cached manifest head separately binds its event and publication IDs.
- A non-monotonic manifest, a same-sequence different head, or a suffix that
  does not reach the cached head is an integrity failure. The implementation
  must not serve the stale snapshot as fallback.
- Each descriptor ID is fetched and canonically validated at most once per
  snapshot advance. Descriptor cache entries are keyed by normalized
  object-store/configuration identity, repository ID, and descriptor ID and are
  immutable. Two restored roots sharing a repository ID establish descriptor
  presence independently.
- Callers cannot mutate a cached replay object. Publication derives a private
  copy-on-write candidate state; it does not deep-copy immutable historical WAL
  payloads merely to append one record.
- Writers continue to use the exact manifest object-store version from their
  snapshot for CAS. A process-local cache cannot make a stale write commit.
- After a successful local CAS, the new validated snapshot may be installed in
  the cache. A competing process is observed by the next conditional manifest
  request and advanced through the same suffix validation.
- Cache eviction, process restart, or disabled caching falls back to the current
  complete replay behavior. Cache contents are never persisted as repository
  truth and are never used by `forge.py check` as evidence.

At manifest sequence 1,000, 100 unchanged loads perform 100 conditional
manifest metadata probes, zero manifest body reads/hashes/parses, zero WAL
reads, and zero descriptor reads. Advancing by one record performs one
non-cached manifest body read/validation, one WAL-record read, and no more than
one read for each newly referenced descriptor. The manifest's cumulative pack
inventory remains subject to the event bound in spec 013; this milestone does
not change its persisted shape. These are deterministic operation-count and
byte-read requirements, not timing estimates.

## Targeted Control-Plane Access

The existing `load_document` operation remains available for backup,
administrative catalog commands, explicit diagnostics, and tests that require a
complete logical snapshot. It is not a request-resolution API.

Hot paths use checksummed, indexed accessors for only the rows they need:

- Git authentication reads one token record as specified by spec 013.
- Fetch resolution reads repository metadata and resolves the active view from
  the WAL snapshot; it does not load transactions, changes, jobs, teams, roles,
  or unrelated control-plane descriptor rows.
- Receive resolution additionally reads only the submitting principal metadata
  required for author validation. The captured authorization descriptor still
  comes from immutable repository truth.
- Browser session and decision routes first resolve the view assignment and
  descriptor from the WAL snapshot, then read only the authenticated
  principal's catalog provenance. A derived `views` or `view_descriptors` row
  never selects the active policy. These routes do not load unrelated derived
  history or job rows.
- A single process-local request phase reuses one store handle or pooled
  connection. A parent HTTP process returns its authentication connection
  before spawning Git; the receive hook child may independently check out one
  connection for its process-local phase.

Primary-key indexes satisfy direct document and token lookups. Add a
`storage_locks(owner)` index for owner heartbeat lookup. Additional indexes are
added only for an exercised query shape; the existence of JSON documents alone
does not justify blanket expression or GIN indexes.

A locked write session records row versions and checksums with its loaded
baseline. Saving one changed aggregate queries and writes only changed or
deleted keys and the lease/meta rows required by the transaction. It does not
inventory every unchanged table. Every update predicate matches the version and
checksum observed by that caller; every delete is likewise conditional on the
loaded baseline. A stale update or delete fails rather than overwriting a row
changed after load. Rows added concurrently outside the baseline remain
untouched.

Derived-index recovery is incremental:

1. Read the repository fence and `last_indexed_seq`.
2. Pin the current repository snapshot.
3. Fail if the database sequence is ahead of repository truth.
4. If the sequences match, perform no WAL replay and no transaction, change,
   view, or job table rebuild.
5. If the database is behind, apply only committed records after
   `last_indexed_seq` and advance affected rows plus the sequence atomically.

An explicit repair path may discard and fully reconstruct derived transactions,
changes, active-view rows, and jobs from WAL. Routine `locked()` entry does not
perform that reconstruction when the indexed sequence already matches.
Immutable catalog rows are not overwritten by recovery.

With 10,000 irrelevant transaction, change, and completed-job rows, a fetch or
receive request issues no unqualified query against those tables and its SQL
statement count is independent of their row count. After initialization, 100
PostgreSQL store opens make zero administrative-database connections and issue
zero database-creation statements. SQL tracing tests enforce query shapes and
counts without relying on wall-clock thresholds.

## Materialization Validation

Full validation remains mandatory when a canonical or view generation is built.
A view generation is activated only after its cursor, single ref, hook
configuration, lack of unsafe alternates/grafts/shallow state, strict `fsck`,
and exact physical-versus-reachable object closure all pass.

After successful activation, a checksummed derived materialization receipt
records the repository ID, manifest head identity, view ID, descriptor ID,
assignment generation, projection epoch, canonical frontier, projected ref and
tip, and exact activated generation. The receipt is written only after full
validation and is never copied into WAL or treated as a commit point.

- On process restart, a receipt that exactly matches current WAL state and the
  on-disk cursor supplies the expected projected tip. The process performs one
  full validation of that existing generation and may then accept it without
  rebuilding.
- A missing, corrupt, stale, future, or contradictory receipt causes verified
  reconstruction. Trusting a self-consistent on-disk cursor and ref without an
  independently stored expected tip remains forbidden.
- The process keeps an in-memory validation registration bound to the receipt
  and active generation. Normal requests perform lightweight cursor/ref/hook
  identity checks but do not repeat `fsck`, `cat-file --batch-all-objects`, or a
  full `rev-list` for an unchanged registered generation.
- The application never mutates an activated view generation. Receives continue
  to target disposable intake generations.
- `forge.py sync` and `forge.py check` perform full validation rather than using
  the request fast path. Existing redirected-hook, alternate, false-cursor,
  stale-ref, missing-object, and dangling-object probes continue to rebuild or
  fail closed.
- Deleting all receipts and process caches remains safe: views reconstruct from
  manifest-reachable data and receive new receipts.

One hundred warm resolutions of one unchanged view execute no additional
`fsck`, `--batch-all-objects`, full `rev-list`, pack installation, or generation
creation after the first successful registration. A restart performs at most
one full validation and creates no replacement generation when its receipt and
files are valid.

## Incremental Projection

The production projector replaces flat whole-tree filtering with a
boundary-aware tree projector. Policy prefixes are compiled once per descriptor
into an immutable byte-oriented matcher preserving capabilities-v3 longest
allow/deny semantics.

For a canonical subtree and policy position, the projector determines one of
three states:

- wholly visible: retain the canonical tree OID and record its reachable
  objects for later safe materialization;
- wholly hidden: omit the subtree without enumerating its descendants;
- policy boundary: enumerate that tree's direct children, recurse only where a
  descendant rule can change the result, and write a deterministic projected
  tree from retained entries.

Canonical input validation remains separate and complete. Reusing a wholly
visible tree does not bypass allowed-mode, gitlink, graph, empty-subtree, or
object-existence checks required when canonical evidence enters repository
truth.

Projection may use the canonical object database as a temporary process-local
alternate while constructing a private generation. Before activation it batch
materializes every canonical object reachable from the projected ref into the
view's own object database, removes the temporary alternate, and passes full
closure validation. An activated view still has exactly one ref, no canonical
alternate, and no hidden or dangling object.

Across a projected history, unchanged visible blobs and wholly visible trees
are materialized at most once per destination generation. Object transfer uses
one or a bounded number of batch `cat-file`/`pack-objects` operations rather
than one process pair per blob per commit.

Incremental canonical and submitted transitions apply changed paths to the
previous canonical/projected trees instead of flattening both snapshots for
each commit. They must retain all existing semantics for deletion, modes,
symlinks, file/directory replacement, hidden ancestor/descendant conflicts,
per-commit authorization, hidden-state preservation, and exact submitting-view
round trips. A later commit restoring a path does not hide an unauthorized or
unsupported intermediate transition.

The implementation includes a deterministic benchmark fixture with 10,000
visible files, 100 linear commits changing one leaf each, nested allow/deny
boundaries, and four views. CI enforces these operation-count properties:

- no per-commit recursive full-tree `ls-tree -r` after initial input validation;
- increasing unchanged files from 10,000 to 100,000 does not increase Git
  subprocess count for the same changed paths;
- each destination reads or transfers a distinct visible blob OID at most once;
- historical object reads and process launches are proportional to changed
  paths, policy-boundary depth, and commits, not `files * commits * views`;
- optimized projected tree IDs, commit IDs, visible histories, reconciliation
  mappings, and final object inventories are byte-identical to a retained
  simple reference projector.

The benchmark records elapsed p50/p95/max, subprocess counts, bytes read,
distinct objects transferred, and peak RSS as JSON. Timing is reported for
regression analysis; deterministic operation counts and output equivalence are
the CI gate until a pinned benchmark runner is defined.

## Worker Batching

`drain_projection_jobs` pins one repository snapshot and performs one
incremental derived-index recovery for all immediately runnable jobs targeting
that manifest generation. Materialization work does not hold a database lease
or transaction. Each claim and completion remains a short owner-checked state
transition.

- A job records the exact target manifest head. Completion marks only that head
  materialized; it never claims a newer head observed after work began.
- If publication advances during a batch, affected jobs remain or become
  pending for the newer sequence after the current short transaction.
- A failed view does not roll back successful independent view
  materializations and retains the existing bounded retry/dead-letter behavior.
- Historical WAL and descriptor reads for one pinned generation are independent
  of view count.
- Multi-view policy transition and restore callers pass their pinned manifest
  to every intake materialization; they do not reload WAL once per view.

Draining 100 jobs for one unchanged manifest performs at most one complete
snapshot acquisition, one incremental recovery decision, and one final
conditional manifest probe. It does not walk historical WAL separately for
claim, materialization, and completion of each job.

## Verification

Unit and integration coverage must include:

- a barrier-controlled PostgreSQL race in which two owners contend for one
  absent, expired, and same-host-dead lease, proving exactly one live owner;
- multi-scope PostgreSQL contention proving rollback leaves no partial lease;
- heartbeat persistence observed from a second PostgreSQL connection after the
  heartbeat caller closes, plus rollback on one lost scope;
- a foreign-host owner not being stolen before expiry despite a coincident PID;
- normal PostgreSQL open under a role without `CREATEDB`, with no connection to
  the administrative database;
- PostgreSQL restore filenames containing quotes, semicolons, comments,
  whitespace, Unicode, and known-table prefixes, all rejected without executing
  unintended SQL;
- missing, extra, duplicate, symlinked, oversized, hash-mismatched, malformed,
  wrong-checksum, and duplicate-key database backup artifacts;
- PostgreSQL backup/restore preserving audit sequence and event IDs while
  restoring no leases;
- restore rollback and target cleanup after failure at each parse and database
  mutation stage;
- two barrier-synchronized receives with identical view, old OID, new OID, and
  idempotency key using distinct workspaces and committing one event;
- a lost-CAS retry and a killed receive proving peer workspaces are untouched;
- repository snapshot cold load, unchanged conditional load, one-record suffix,
  competing-process advance, malformed suffix, cache eviction, and descriptor
  memoization operation counts;
- SQL tracing for targeted fetch, receive, browser-policy, no-op recovery, one
  changed catalog row, and one-record derived recovery;
- warm materialization, process restart, missing receipt, false cursor,
  redirected hook, unsafe alternate, missing object, and dangling object;
- reference-versus-optimized projection over random nested policies, unusual
  byte paths, modes, symlinks, file/directory replacements, hidden conflicts,
  policy epochs, source-evidence commits, and administrative updates;
- the deterministic large-tree operation-count benchmark and worker batch
  budgets defined above.

PostgreSQL correctness tests are required in CI when PostgreSQL support is
claimed; silently skipping the entire lease and restore matrix is not release
qualification for that backend. The complete existing compile, unit,
adversarial, process, failpoint, backup, invariant, and Git compatibility suites
continue to pass.

## Non-Goals

- Treating a same-user process with write access to the forge root or process
  environment as hostile; that remains outside the documented prototype
  boundary.
- Changing the intentional SHA-1-only Git object format in this milestone.
- Weakening `synchronous=FULL`, pack hash checks, activation `fsck`, exact object
  closure, or the exhaustive independent invariant checker.
- Optimizing object-store `LIST`, which repository loading does not use, or
  adding speculative database indexes without an exercised query.
- WAL/pack garbage collection, compaction, shallow/partial clone, remote object
  stores, multiple hosted application replicas, or a general production SLO.
- Replacing the hosted token, HTTP streaming, throttling, browser, or deployment
  requirements already specified by spec 013.

## Acceptance

- PostgreSQL never grants one live scope to two owners, and every reported
  heartbeat is committed before return.
- Ordinary PostgreSQL access neither provisions databases nor requires
  administrative privileges.
- No backup-controlled value can become a SQL identifier or additional
  statement, and failed restore leaves no partially accepted target.
- Concurrent identical receives use private workspaces and converge on one
  manifest-reachable event without cross-cleanup.
- An unchanged repository load reads only the conditional manifest, and one new
  event reads and validates only its suffix and new descriptors.
- Git and browser request resolution never scans unrelated transaction, change,
  or job tables; no-op sessions do no full recovery.
- A validated generation pays full integrity and closure checks once per process,
  not once per request, while explicit sync/check still detects tampering.
- Projection work scales with changed paths and policy boundaries rather than
  complete tree size per commit, with byte-identical authorization-safe output.
- Worker historical work is bounded per pinned manifest generation rather than
  multiplied by job count.
- Every optimization is disposable and fail-closed; deletion of databases'
  derived rows, receipts, and process caches cannot change repository truth or
  authorization outcomes.
