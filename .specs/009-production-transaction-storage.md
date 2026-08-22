# Production Transaction Storage

## Status

Implemented for storage schema version 1 (forge document schema version 5).

This specification implements Phase 3 of `ROADMAP.md`: durable forge state
moves from one JSON file guarded by a global filesystem lock into a real
database with short transactions, optimistic versions, lease-based session
locks, idempotency keys, durable retry schedules, worker heartbeats,
cancellation, dead-letter handling, observability, backups, corruption
detection, and disaster recovery tests. Git-ref observation and forward-only
recovery are preserved unchanged because the database and Git refs still
cannot share one atomic transaction.

## Decisions

### Backends

Two backends implement one schema shape and one behavioral contract:

- `sqlite` (default): a single `store.sqlite3` database inside the forge root,
  WAL mode, `busy_timeout` 30 seconds. Zero additional dependencies; the
  local prototype and every test run on it.
- `postgres`: selected by passing `--database postgres://...` to `init`, or
  by `BASTION_DATABASE` for tests. Requires `psycopg` (version 3). The DSN
  is recorded locally so every later process connects identically, and every
  forge root receives its own database (`bastion_<hash-of-root>`) on the
  shared server so many forges coexist without collisions.

The backend choice is stored in `root/backend.json`, written once at
initialization (or migration) and treated as immutable afterwards. A missing
or unreadable backend record is an error naming the root; a forge is never
silently reopened on another backend.

The document layout, version rules, checksum rules, lock protocol, and all
state transitions are byte-for-byte identical across backends. Tests run the
same core matrix against both.

### Tables

Each aggregate becomes one table of JSON documents with explicit key columns:

```text
schema_migrations   (version PK, applied_at)
storage_locks       (scope PK, owner, heartbeat_at, expires_at)
forge_meta          (single row: canonical_branch, next_transaction_sequence)
principals          (key = principal id)
teams               (key = team id)
roles               (key = role id)
role_bindings       (key = binding id)
view_descriptors    (key = descriptor id)
views               (key = view name)
transactions        (key = transaction id)
changes             (key = Change-Id)
outbox_events       (key = event id)
projection_jobs     (key = job id)
idempotency_keys    (key = key, records request fingerprint + recorded outcome)
audit_log           (monotonic seq, kind, subject, detail, at)
```

Every document row carries `version INTEGER` (optimistic concurrency) and
`checksum TEXT` (SHA-256 over the canonical JSON encoding). The nested
`state.json` shape survives as the in-memory domain model: loading
materializes the legacy dict from rows; saving decomposes the dict back into
rows. Nothing outside this module may touch tables directly.

### Short transactions and optimistic versions

`save_state` is one short database transaction: read current
`(version, checksum)` per row, upsert changed documents with `version + 1`
guarded by `WHERE version = ?`, delete vanished keys, commit. There are no
long-held write transactions and no whole-document rewrites of unrelated
aggregates when their content is unchanged.

Under scoped sessions the save diffs against the session's post-recovery
baseline: rows this session did not mutate are never written, so concurrent
writers on other scopes cannot be clobbered or deleted by stale snapshots.
Rows mutated by two scopes simultaneously resolve through the optimistic
version guard into one clean conflict error. Unscoped saves (initialization,
migration, single-writer tooling) keep whole-section semantics.

`load_state` verifies every checksum before materializing. A mismatch raises
an error naming the corrupt aggregate; the checker refuses to run against a
forge with tampered rows instead of silently interpreting them.

### Session leases replace the filesystem lock

`locked()` no longer takes `flock` on `state.lock`. It acquires lease rows in
`storage_locks`, one per declared scope:

- scope `global`: everything that runs recovery or requires global quiescence
  (receives, administrative updates, transitions, view creation, sync);
- scope `view:<name>`: projection-worker claim/fail/complete sections, which
  never recover;
- scope `canonical:<branch>`: reserved for multi-repository canonical
  sequencing; receives currently hold `global` because they observe refs of
  every view during recovery.

A lease row records its owner token, last heartbeat, and expiry. Owner
tokens embed `<uuid>:<pid>:<process-start-time>`; acquisition treats a lease
whose owning process no longer exists as free immediately, so crashed
holders are stolen instantly instead of blocking for the TTL. Acquisition is
atomic across all requested scopes in one transaction. Every `save_state`
inside the session verifies ownership and heartbeats each held lease before
committing; a lost lease aborts the save with an explicit error instead of
writing state under someone else's exclusion window. Release deletes the
rows. There is no filesystem cleanup step and no stale-lock recovery
command.

Worker sections hold only `view:<name>`. Two workers projecting unrelated
views therefore proceed concurrently, and a receive's git-heavy validation no
longer blocks job completion for other views. The unconfirmed-source check
inside worker claim remains but is advisory under scoped locking:
completion still validates frontier/tip freshness through the existing CAS
in `publish_projection_plan`, and receives remain globally serialized, so at
most one unconfirmed source publication can exist at any time.

Transaction sequence allocation happens only under `global`; workers never
allocate sequences.

### Failpoint mapping

JSON-writer checkpoints keep firing at equivalent database boundaries so
existing failpoint flows behave identically:

```text
state.temporary_fsynced   after rows are written, before COMMIT
state.replaced            after successful COMMIT
state.directory_fsynced    immediately after COMMIT (WAL durability covers fsync)
```

Semantic checkpoints (`*.prepared_persisted`, `*.ref_published`, ...) are
unchanged and still fire after the matching `save_state` returns.

### Idempotency keys

A receive may carry an idempotency key via `BASTION_IDEMPOTENCY_KEY` (set by
the HTTP server from an `Idempotency-Key` header). The key maps to a request
fingerprint `(view, old, new)` plus the recorded outcome. Lookup happens
before ref validation, so a replay whose outcome was already recorded
returns success even though the ref has since moved; a replayed key with a
different fingerprint is rejected by name. Only successful preparation is
recorded — rejections leave nothing durable — and the record commits in the
same short transaction family that first persists the prepared transaction,
so crash-replay can never double-apply.

### Retry schedules, heartbeats, cancellation, dead letters

Projection jobs gain `max_attempts` (default 5), exponential retry delay
`min(RETRY_SECONDS * 2**(attempts-1), 600)`, and optional `heartbeat_at`.
Exhausting attempts moves the job to status `dead`, records the terminal error
in `view_errors`, appends an audit-log entry, and leaves the transaction
`fanout_pending` until an operator intervenes.

`heartbeat <root> <token>` extends a running lease; `cancel-job` moves a
non-running (or expired-lease) job to `cancelled`; `requeue-job` moves a dead
or cancelled job back to `pending` with attempts reset. All three are
audited.

### Observability

`stats <root>` prints backend, schema version, per-status counts for
transactions/events/jobs, oldest pending event age, active leases with
expiry, dead-letter count, and store size. It is read-only and must work on a
quiescent forge without mutating anything.

### Backup, restore, corruption detection

`backup <root> <destination>` produces a consistent backup directory
containing the database dump (SQLite backup API / per-table COPY for
Postgres), a manifest (schema version, backend, created-at, canonical and
view ref tips), and a filesystem copy of `canonical.git` and every view
repository. `restore <backup> <new-root>` reconstructs the forge; the exit
criterion is that restore yields a forge where `check` passes and every job
and event survives with identical content.

Corruption detection has three layers: checksum verification on every load,
structural validation of required fields while materializing, and the
existing global invariant checker operating unchanged above the store.

### Migration

`migrate-storage <root> --database <uri>` imports a legacy JSON forge in one
short transaction: create the store, write all aggregates from the parsed
document, verify by re-loading and comparing, then atomically rename
`state.json` to `state.json.imported` and write `backend.json`. A failed
import leaves the JSON forge untouched. Fresh `init` creates the store
directly; JSON is no longer written anywhere.

## Invariants

- At most one session owns a scope lease at any instant.
- No `save_state` ever commits without holding every lease its session
  acquired, freshly heartbeated within the same transaction.
- Every persisted document row's checksum matches its content on read.
- Versions strictly increase; a guarded update that matches zero rows aborts
  the whole save.
- Rejected operations change no row, no ref, no receiver marker.
- Recovery remains forward-only, driven by observed Git refs exactly as
  before; the database never invents history.
- Restore-from-backup reproduces all rows, jobs, events, and reachable refs.

## Testing

Unit and integration:

- [x] both backends pass the same core matrix (versions bump, conflicts
      abort, deletes propagate, checksums verify);
- [x] lease exclusivity: concurrent sessions serialize; expired leases are
      stealable; a stolen lease aborts the previous owner's next save;
- [x] migration imports a legacy forge bit-for-bit and rejects corrupt input
      without side effects;
- [x] jobs: retry schedule grows exponentially, exhaustion dead-letters,
      heartbeat extends, cancel/requeue behave and audit;
- [x] idempotency: replay returns recorded outcome once; fingerprint
      mismatch rejects;
- [x] backup/restore round-trip passes `check` with identical rows and refs.

Adversarial:

- [x] racing writers on the same scope: exactly one wins, loser sees a clean
      lease error and changes nothing;
- [x] killed processes at every mapped failpoint converge to the same state
      as uninterrupted execution;
- [x] tampered rows (bit flips in docs, versions, checksums) are detected
      and named, never silently interpreted;
- [x] hostile identifiers (quotes, newlines, unicode, path separators) survive
      parameterized round-trips;
- [x] wrong backend record, unknown schema version, and foreign DSNs are
      refused loudly;
- [x] duplicate event dispatch and duplicate job completion stay idempotent;
- [x] a permanently broken view dead-letters without blocking unrelated views'
      progress.
