# Real-Process Concurrency And Failpoint Testing

## Status

Ready for implementation.

This specification covers the next two incomplete items in Phase 1 of
`ROADMAP.md`:

> Add real parallel-process push tests with barriers around validation,
> canonical CAS, source publication, worker claim, ref publication, and state
> persistence.

> Add named failpoints and terminate processes at every durable transaction
> boundary.

The global invariant checker and deterministic state-machine harness are already
available. This work must use them as oracles, but it must exercise real
operating-system processes rather than reconstructing crash states in one
Python process.

## Recommendation

This is the highest-value next initiative because Bastion's primary claim is safe,
recoverable coordination across independent Git repositories and JSON state.
The current suite demonstrates the intended states, but most crash windows are
assembled directly by tests and most races are serialized within one process.
Before broadening Git semantics or adding permission lifecycle, the same
guarantees need to survive actual lock contention, hook processes, worker
processes, abrupt `SIGKILL`, and recovery from the bytes and refs those processes
left behind.

The work should combine barriers and failpoints. One inert-by-default named
checkpoint primitive can pause a process at an exact boundary. The test
controller can then either:

- start a competing process and later resume the paused process; or
- kill the paused process and exercise normal recovery.

Keeping those mechanisms together avoids two test-only instrumentation systems
and ensures concurrency and crash tests refer to the same transaction model.

## Goal

Add a bounded, deterministic Linux test suite that proves the current
version-1 forge behaves correctly when real processes overlap or die at every
authoritative publication boundary.

For each covered schedule, the suite must prove that:

- canonical and projected refs never regress;
- a canonical compare-and-swap has at most one winner;
- a rejected or losing process contributes no published tree effect;
- an unconfirmed source transaction remains a repository-wide barrier;
- the source ref is never advanced by recovery on the client's behalf;
- one projection job is not completed twice by competing workers;
- jobs for independent views can progress concurrently;
- a projected ref moved before state persistence is adopted, not recreated or
  rewound;
- state-file replacement is atomic under process termination;
- retries settle to the same published repository state as uninterrupted
  execution;
- `check_invariants` passes at every recovered stable checkpoint.

## Success Outcome

After this work, Phase 1 will have real-process evidence for its transaction and
ordering model. The next initiative can then focus on the full tree-operation,
shared-visible conflict, and rejection-immutability matrix without first having
to invent process orchestration or crash injection.

This work does not make the local filesystem layout a production security
boundary and does not replace the later PostgreSQL or network-service phases.

## Current Guarantees To Preserve

The implementation must retain all current version-1 restrictions and
guarantees:

- one SHA-1 canonical repository and one configured branch;
- linear canonical and projected history;
- exactly one ref update per push;
- immutable positive read and write prefixes;
- one filesystem state lock;
- canonical compare-and-swap publication;
- exact submitting-view commit continuity;
- a blocked event until source publication is confirmed;
- ordered, leased, at-least-once projection jobs;
- deterministic synthetic projected commits;
- forward-only ref adoption after a crash;
- no canonical alternates in view repositories.

Checkpoint instrumentation must not weaken or bypass these rules.

## Scope

### In scope

- an inert production-side checkpoint helper;
- explicit checkpoints at source, administrative, worker, projection, recovery,
  and state-file boundaries;
- a Linux process controller for tests;
- ordinary `git push` processes using the installed pre- and post-receive hooks;
- competing push processes from the same and different views;
- competing worker processes;
- worker and receive-process overlap;
- `SIGCONT` for barrier release and `SIGKILL` for crash injection;
- deterministic recovery using existing coordinator and worker APIs;
- semantic comparison with uninterrupted executions;
- cleanup of stale receiver liveness markers exposed by real process death;
- focused documentation of the new profile and replay diagnostics.

### Out of scope

- full delete, rename, executable-mode, symlink, file/directory replacement, or
  unusual-path coverage;
- the complete shared-visible conflict matrix;
- systematic immutability tests for every existing validation rejection;
- permission mutation, revocation, expansion, contraction, teams, or roles;
- replacing JSON state or the global filesystem lock;
- HTTP, SSH, authentication, or a production process supervisor;
- power loss, kernel crashes, torn filesystem sectors, or storage-controller
  cache failure;
- arbitrary scheduler fuzzing or nondeterministic stress loops;
- changes to lease duration or production retry policy;
- merges, force pushes, multiple branches, tags, gitlinks, or SHA-256;
- performance or scale claims.

The following Phase 1 roadmap items remain separate after this specification:

- full tree-operation tests;
- shared-visible conflict tests;
- comprehensive before/after checks for rejected operations.

## Design Decisions

### Linux is the required test platform

The real-process suite may depend on:

- `fcntl.flock`;
- `/proc/<pid>/stat` process start identity;
- `SIGSTOP`, `SIGCONT`, and `SIGKILL`;
- local Git receive-pack hooks.

These are already consistent with receiver-liveness behavior in `src/state.py`.
The test modules must use an explicit platform skip when those facilities are
not available. A skip is not evidence for this Phase 1 gate: the full profile
must pass on the project's reference Linux development environment. Adding a CI
provider is outside this specification because the repository has no existing
CI surface; when one is added, this profile must be a required Linux job.

### Checkpoints pause; the controller chooses the outcome

Production code must not contain a checkpoint mode that silently calls
`os._exit`, throws an arbitrary exception, or sleeps. A matching checkpoint
publishes its identity and stops its own process with `SIGSTOP`.

The test controller then makes the schedule explicit:

- `SIGCONT` means the checkpoint acts as a barrier;
- `SIGKILL` means the checkpoint acts as a failpoint.

This ensures the test knows the process reached the exact boundary before it is
killed. It also lets the same point test both lock contention and recovery.

### Checkpoints are inert by default

No checkpoint may create a file, inspect `/proc`, send a signal, alter an
exception, or change timing unless all test-control variables are present and
the configured root and point match the current call.

An ordinary CLI command, hook, or imported API with no test-control environment
must behave byte-for-byte as it did before this work, apart from fixes directly
demonstrated by the new tests.

### Tests use explicit schedules, not timing races

Tests must not use a fixed `sleep` and hope two processes overlap. Every
schedule must wait for a named marker and verify the stopped process identity
before starting the action that is intended to race with it.

A short bounded poll is acceptable only for waiting for an explicit marker,
process exit, or `/proc` state transition. Every wait needs a monotonic deadline
and a diagnostic that includes the actor command, target checkpoint, captured
stdout, and captured stderr.

### Process death is not power loss

Killing a process after `os.replace` but before directory `fsync` verifies
process-crash behavior on the running kernel. It does not prove that the rename
would survive sudden power loss. Documentation and test names must say
"process crash," not "power-loss safe."

### Recovery equivalence is semantic

Crash execution is not required to produce byte-identical `state.json` to an
uninterrupted execution. A failed attempt can legitimately consume a canonical
sequence, create an aborted transaction, increment a worker attempt count, or
leave unreachable Git objects.

Equivalence is defined in detail below. It is strict for published refs, trees,
frontiers, anchors, logical changes, and live work, while allowing durable
evidence of failed attempts.

## Required Deliverables

### New production file

- `src/failpoints.py`: inert named checkpoint implementation. It must contain no
  forge state transition logic.

### New test support files

- `tests/process_harness.py`: fixture builder, process controller, marker parser,
  authoritative snapshot, recovery helpers, and diagnostics. Its name must not
  match `test_*.py`.
- `tests/process_actor.py`: a small subprocess entrypoint for production APIs
  that need a deterministic logical worker time or a recovery-only invocation.
  It must not mutate state directly except for the focused atomic-save test.

### New test modules

- `tests/test_process_concurrency.py`: real competing push, worker, and
  receive/worker barrier schedules.
- `tests/test_process_failpoints.py`: source, administrative, worker,
  projection, recovery, and state-file crash matrix.

### Updated production files

- `src/protocol.py`: source validation, prepared-state, canonical-ref,
  canonical-state, source-ref, confirmation, and receiver-cleanup checkpoints.
- `src/service.py`: administrative transaction and shared projected-ref
  checkpoints.
- `src/state.py`: low-level atomic state-save checkpoints, recovery checkpoint,
  and stale receiver-marker cleanup proven necessary by killed hook tests.
- `src/worker.py`: claim, ready-plan, retry, and completed-state checkpoints,
  plus consistent propagation of an explicitly supplied logical `now` through
  claim, completion deferral, and failure retry calculations.

### Updated tests and documentation

- `tests/test_git_adapter.py` or a new focused unit section: prove checkpoints
  are inert with no test environment and do not leak test configuration into
  forge-created Git commands where it is not needed.
- `docs/TESTING.md`: commands, Linux requirement, checkpoint diagnostics,
  process cleanup, default profile, and semantic equivalence rules.
- `docs/TRANSACTIONS.md`: identify the now-tested process-crash windows without
  presenting failpoints as a user-facing feature. Correct the current blanket
  statement that history traversal and candidate construction never hold the
  state lock: source `pre_receive` currently performs both while locked, while
  projection workers build plans outside the lock.
- `ROADMAP.md`: update the current baseline after implementation. Do not mark
  all of Phase 1 complete because tree operations, conflicts, and broad
  rejection immutability remain.

### Files not expected to change

- `src/reconciliation.py`, `src/projection.py`, and `src/validation.py`: this
  work tests coordination, not tree semantics.
- `src/outbox.py`: existing transitions should be driven through recovery and
  worker APIs. Change it only if a real-process test demonstrates an idempotency
  or ordering defect.
- `forge.py`: do not add a user-facing failpoint command. The test actor can
  call existing APIs directly in a separate process.
- the state schema version: checkpoints are ephemeral test controls and must not
  be persisted in `state.json`.

## Checkpoint Contract

### Public test seam

`src/failpoints.py` must expose one function:

```python
def checkpoint(root: Path, name: str) -> None:
    """Pause at a named test checkpoint when explicitly armed."""
```

The exact private helpers may vary. Do not build a general fault-injection
framework, plugin system, decorator registry, socket server, or background
thread.

### Test-control environment

The controller arms one point for one actor with all of these variables:

```text
BASTION_TEST_FAILPOINT=<checkpoint-name>
BASTION_TEST_FAILPOINT_ROOT=<absolute-resolved-forge-root>
BASTION_TEST_FAILPOINT_MARKER=<absolute-unique-marker-path>
```

Behavior:

1. If none of the variables are present, return immediately.
2. If only some variables are present, fail loudly with a stable test-control
   error rather than guessing.
3. If `root.resolve()` does not equal `BASTION_TEST_FAILPOINT_ROOT`, return
   immediately.
4. If `name` does not equal `BASTION_TEST_FAILPOINT`, return immediately.
5. On a match, copy and then remove all three variables from the current
   process environment so the point cannot fire twice in that process.
6. Atomically publish a versioned JSON marker.
7. Flush the marker file and its parent directory.
8. Stop the current process with `SIGSTOP`.
9. Return normally after the controller sends `SIGCONT`.

Removing the variables only affects the matched child process. It does not
alter the test controller or receive-pack parent environment.

### Marker schema

The marker must contain:

```json
{
  "version": 1,
  "checkpoint": "source.prepared_persisted",
  "root": "/absolute/path/to/forge",
  "pid": 12345,
  "process_start": "987654321"
}
```

`process_start` must use the same Linux `/proc/<pid>/stat` field as receiver
liveness. The controller must validate both PID and start identity before every
signal so PID reuse cannot target an unrelated process.

The marker path must be unique per actor and must live in the test's control
directory outside the forge root. A pre-existing marker is a harness error; it
must not be overwritten.

### Error behavior

Malformed names, invalid marker parents, duplicate markers, and incomplete test
configuration must fail promptly and visibly. The error does not need to be a
stable user-facing forge error because the seam is test-only, but diagnostics
must include the requested checkpoint.

An unknown but well-formed checkpoint name simply never matches. The controller
will report a bounded timeout naming that point.

### Security posture

These variables are not a production administration interface. The helper is
acceptable in the prototype only because:

- it is entirely inert unless explicitly armed;
- the current local filesystem endpoint is not a hostile-user boundary;
- the future network service must construct a sanitized service environment.

`docs/TESTING.md` must warn against setting `BASTION_TEST_FAILPOINT*` in normal
operation. Future HTTP/SSH server work must strip them before invoking forge
code or Git hooks.

## Required Checkpoint Catalog

Checkpoint names are part of the test contract. Keep these exact names so a
failed schedule is understandable and replayable.

### Source push checkpoints

| Name | Exact location | Durable state at pause |
|---|---|---|
| `source.validation_complete` | After the full submitted chain is validated, authorized, reconciled, reprojected, `Change-Id` ownership is checked, and candidate objects are promoted; before a transaction or receiver marker is persisted | Canonical and source refs unchanged; no transaction is authoritative; promoted objects may be dangling |
| `source.prepared_persisted` | Immediately after the prepared transaction and receiver marker are saved | State contains `prepared`; canonical and source refs still old |
| `source.canonical_ref_published` | Immediately after successful canonical CAS | Canonical ref is new; durable transaction may still say `prepared`; source ref is old |
| `source.canonical_state_persisted` | Immediately after `canonical_committed` and the blocked event are saved | Canonical ref is new; source ref is old; event is blocked |
| `source.ref_published` | At entry to post-receive, before acquiring the state lock | Receive-pack has moved the source ref; persisted state has not yet confirmed it |
| `source.confirmation_persisted` | After post-receive acquires the lock and recovery has saved source confirmation | Source view state is applied; event is active or dispatched; receiver marker may still exist |
| `source.receiver_cleanup_persisted` | After the receiver marker is removed and post-receive state is saved, before automatic worker draining | Source publication is fully confirmed and liveness cleanup is complete |

The validation checkpoint intentionally occurs after object promotion. Dangling
objects are not authoritative publications and may survive a rejected or killed
attempt.

### Administrative canonical checkpoints

| Name | Exact location | Durable state at pause |
|---|---|---|
| `admin.prepared_persisted` | After the administrative transaction is saved as `prepared` | Canonical ref is old |
| `admin.canonical_ref_published` | Immediately after successful canonical CAS | Canonical ref is new; state may still say `prepared` |
| `admin.canonical_state_persisted` | After the active event and `fanout_pending` or `published` state are saved | Canonical update is fully coordinated |

### Worker and projection checkpoints

| Name | Exact location | Durable state at pause |
|---|---|---|
| `worker.claim_persisted` | In `run_projection_job`, after claim returns and the state lock has been released | Job is `running` with a lease; projection objects are not yet authoritative |
| `worker.plan_persisted` | After the job is saved as `ready` with its complete deterministic plan | Projected ref and persisted view still name the old tip |
| `projection.ref_published` | In shared projection publication, after the projected CAS succeeds or the exact new tip is observed, before anchors/frontier/tip are changed in memory | Projected ref may be new while persisted view and ready job remain old |
| `worker.completion_persisted` | After anchors, logical representations, view cursors, job completion, and transaction status are saved | Job is terminal and state agrees with the projected ref |
| `worker.retry_persisted` | After a claimed job failure and retry metadata are saved | Ref and view state are unchanged by the failed attempt |

`projection.ref_published` is located in code shared by worker publication and
manual sync, but the required crash case must use a worker with a durable ready
plan. Version 1 has no supported operation that leaves a view behind while also
making manual sync eligible: coordinated updates create pending jobs, and sync
rejects unfinished work. Do not construct an artificial manual-sync crash by
writing the canonical ref directly or deleting jobs from state.

### Recovery checkpoint

| Name | Exact location | Durable state at pause |
|---|---|---|
| `recovery.state_persisted` | After `recover` saves any forward transition and outbox dispatch caused by its observations | Persisted state reflects the observed authoritative refs |

Do not fire this point on a no-op recovery that does not save state.

### Atomic state-save checkpoints

| Name | Exact location | Visible file expectation |
|---|---|---|
| `state.temporary_fsynced` | After complete JSON is flushed and fsynced to `state.tmp`, before `os.replace` | `state.json` is old; `state.tmp` may contain the new state |
| `state.replaced` | After `os.replace`, before opening/fsyncing the forge directory | `state.json` is entirely new, never partial |
| `state.directory_fsynced` | After directory fsync, before returning | `state.json` is entirely new and process-level persistence work is complete |

These low-level points fire for the first matching `save_state` call in the
armed process. Their focused tests must invoke exactly one save so occurrence
counting is unnecessary.

## Placement Rules

- A semantic `*_persisted` checkpoint belongs after `save_state` returns, not
  before it.
- A `*_ref_published` checkpoint belongs after Git reports successful CAS or
  exact-tip adoption, but before corresponding JSON state is mutated or saved.
- `worker.claim_persisted` must occur after releasing the state lock so another
  worker can observe the lease.
- `source.ref_published` must occur before post-receive calls `locked`; otherwise
  recovery may confirm the source before the test can kill that window.
- No checkpoint may split mutation of the in-memory dictionary from the
  `save_state` call that makes that mutation durable unless the point is one of
  the three explicit atomic state-save checkpoints.
- Do not add checkpoints inside commit, tree, or blob construction. Those
  objects are dangling until a ref publication and are already covered by the
  publication boundary.
- Do not hold an additional test lock. The only forge lock in each schedule must
  be the real `state.lock`.

## Process Harness

### Actor types

The harness must launch these as real child processes:

- source push: ordinary `git push origin main` from a `--no-local` clone;
- administrative update: `python3 forge.py update-canonical ROOT OLD NEW`;
- normal worker smoke: `python3 forge.py work ROOT --limit 1`;
- deterministic-time worker: `python3 tests/process_actor.py worker ROOT NOW`;
- recovery only: `python3 tests/process_actor.py recover ROOT`;
- manual sync: `python3 forge.py sync ROOT VIEW`;
- atomic state save: `python3 tests/process_actor.py state-save ROOT VALUE`.

`process_actor.py worker` must call `run_projection_job(root, now=...)` directly.
This preserves a real process while allowing a killed lease to be reclaimed
without waiting 60 wall-clock seconds. It must not patch time, UUID functions,
locks, refs, or state.

Production worker APIs must honor that explicit time consistently. Add optional
`now` parameters to `complete_projection_job` and `fail_projection_job`, and
have `run_projection_job` pass its supplied value through. When `now is None`,
each API must retain its normal wall-clock behavior. When `now` is supplied,
lease expiry and every `next_attempt_at` written by that attempt must derive
from it. This is a general deterministic seam, not a test-environment override.

`process_actor.py recover` must acquire `locked(root)` and exit after recovery.
It must not call `save_state` itself or edit transaction status.

`process_actor.py state-save` may change only
`state["next_transaction_sequence"]` while holding the real lock with recovery
disabled, then call `save_state` once. This is a focused atomic-writer test, not
a transaction recovery shortcut.

### Process controller

`tests/process_harness.py` must provide one small abstraction that:

1. starts a process in its own process group with captured stdout and stderr;
2. creates a unique marker path and arms one checkpoint;
3. waits for the marker with a monotonic deadline;
4. parses and validates marker version, root, checkpoint, PID, and process start
   identity;
5. waits until `/proc` reports the marked process is stopped;
6. resumes or kills only that exact process;
7. waits for the outer actor process to finish;
8. kills the actor's process group during cleanup if any descendant survives;
9. includes all captured output in failures.

For local Git transport, the marked PID can be a pre- or post-receive hook below
the outer `git push` process. The controller must signal the marked hook PID but
own and clean up the complete outer process group.

Every test must register process cleanup before waiting for a marker. A timeout
must never leave a stopped hook holding `state.lock` for later tests.

### Deterministic fixture

The shared fixture must create:

- one canonical `main` branch;
- Alice with read/write access to `shared` and `team-a`;
- Bob with read/write access to `shared` and `team-b`;
- one hidden canonical path;
- separate `--no-local` Alice and Bob clones;
- fixed author, committer, timestamp, message, and optional `Change-Id` values;
- an administrative clone capable of placing immutable commits in canonical
  storage through a staging ref.

Do not share a mutable forge between test methods. Each schedule needs its own
temporary root so a killed actor cannot contaminate another result.

### Logical worker time

Use fixed logical times such as:

```text
claim at 1_900_000_000
retry at 1_900_000_061
```

This makes lease expiration immediate and deterministic. Test output and
comparisons must not depend on current wall-clock time, PIDs, temporary paths,
or UUID lease tokens.

## Stable Checkpoint Oracle

After a paused process is resumed and completes, or after a killed process is
recovered, the harness must settle the forge before checking it:

1. Ensure no source transaction has a live receiver process.
2. Run recovery in a fresh process.
3. Retry an unconfirmed source only when the scenario explicitly requires the
   exact client retry.
4. Run workers at deterministic times until no job is claimable or unfinished.
5. Run recovery once more.
6. Run `check_invariants(root)` in the test process.
7. Capture the authoritative snapshot. `check_invariants` already runs strict
   `git fsck --full` checks for canonical and every view, so do not repeat a
   weaker standalone fsck pass after every schedule.

Never call `check_invariants` while a process is stopped inside a documented
ref/state publication window. The checker intentionally expects a stable
checkpoint.

## Authoritative Snapshot

The harness must capture enough state to distinguish a safe recovery from a
merely passing command.

Required fields:

- canonical branch OID and complete root-to-tip commit sequence;
- each view branch OID and complete root-to-tip commit sequence;
- byte-level tree entries at each published tip, including mode and blob bytes;
- each view's `canonical_frontier`, `projected_tip`, and sorted anchors;
- sorted logical change ownership and revisions;
- accepted source mappings keyed by submitted and canonical OID;
- status of every non-aborted transaction, keyed by logical operation rather
  than a sequence-derived administrative transaction ID;
- absence of blocked events, unfinished jobs, live receiver markers, and
  unexplained pending views after settlement;
- reachable object closure checks supplied by `check_invariants`.

For a losing concurrent push, also record its unique path and `Change-Id` so the
harness can prove neither appears in canonical history, anchors, mappings, or
logical changes.

## Recovery Equivalence

Each crash family must have an uninterrupted control execution using the same
initial tree, commit metadata, source commit OIDs, and intended successful
operations.

The recovered and control executions must match exactly on:

- canonical and projected branch OIDs;
- canonical and projected commit sequences for accepted changes;
- published tree modes and bytes;
- view frontiers, tips, and anchors;
- submitting-view commit identity;
- synthetic representation OIDs;
- logical change owner and accepted revisions;
- source mappings for accepted transactions;
- final absence of active publication barriers and unfinished jobs.

The comparator may ignore only:

- absolute transaction sequence numbers and
  `next_transaction_sequence` when a killed prepared attempt consumed one;
- retained `aborted` transaction records for attempts that never published;
- worker `attempts`, expired lease tokens, retry deadlines, and historical
  `last_error` fields that are cleared or terminally irrelevant;
- unreachable Git objects;
- PIDs, process start identities, marker files, and temporary root paths.

It must not ignore:

- a different accepted canonical or projected commit;
- an extra or missing accepted mapping;
- a different anchor kind or target;
- a missing `Change-Id` representation;
- a blocked event or unfinished job after settlement;
- a receiver marker for a terminal transaction;
- any published ref or tree difference.

Normalization must be implemented once in `process_harness.py`, documented in
the assertion failure, and covered by a focused unit test. Individual scenarios
must not delete arbitrary keys until their results happen to match.

Use these logical operation keys:

- source transaction: `(view, projected_old, projected_new)`;
- administrative transaction: `(canonical_old, canonical_new)`;
- projection delivery: `(logical transaction key, view, canonical_to)`.

An aborted administrative attempt can consume a sequence, causing its later
successful retry to use different transaction, event, and job IDs. The
comparator may normalize those sequence-derived identifiers through the logical
keys above. Within either execution it must still prove IDs are unique,
references are internally consistent, and dispatch created at most one live job
per logical delivery.

## Required Parallel-Process Matrix

### P1: Same-view shared-visible competing pushes

1. Create two Alice clones at the same projected tip.
2. Give each clone a distinct authorized `shared` edit and `Change-Id`.
3. Pause push A at `source.validation_complete` while it holds the state lock.
4. Start push B before resuming A.
5. Resume A and let both receive-pack processes finish.

Expected result:

- exactly one push publishes;
- canonical history contains only the winning effect;
- the Alice ref is the winning submitted tip;
- the losing path and `Change-Id` have no canonical mapping or representation;
- no prepared or unconfirmed transaction remains;
- retrying the loser follows normal fetch/rebase behavior rather than ref
  regression.

Do not require one exact client error string. Depending on which Git layer
observes the changed source ref first, the loser may receive the forge's active
transaction/staleness error or Git's ref-lock/non-fast-forward rejection.

### P2: Cross-view hidden-disjoint competing pushes

1. Start Alice and Bob from projections of the same canonical frontier.
2. Alice changes only `team-a`; Bob changes only `team-b`.
3. Pause Alice at `source.validation_complete`.
4. Start Bob, then resume Alice.
5. Let both initial receive-pack processes finish and settle Alice's source
   confirmation and Bob's hidden-only projection job.
6. If Bob's initial push was rejected by the temporary source barrier or its
   pending projection work, retry Bob's exact unchanged source update.

Expected result:

- both source commits are eventually accepted without either user learning the
  other's private path;
- canonical history contains both effects in the serialized order;
- each source view retains its exact submitted commit;
- the other view elides the hidden-only transition;
- no update is lost when Bob reconciles against the newer canonical tree.

The first Bob process is not required to succeed. Alice releases the state lock
before receive-pack moves her source ref, so Bob can legitimately observe an
unconfirmed source barrier. Alice's confirmation also creates ordered Bob
projection work. The required property is that Bob's exact commit remains valid
after those hidden-only transitions settle and succeeds without rebase or
recreation.

### P3: Canonical publisher dies while a competitor waits

1. Pause Alice at `source.canonical_ref_published`.
2. Start a Bob push while Alice still holds the state lock.
3. Kill Alice's pre-receive hook.
4. Let Bob proceed through normal lock acquisition and recovery.

Expected result:

- canonical contains Alice's candidate exactly once;
- Alice's source ref remains old;
- Bob is rejected by the repository-wide unconfirmed-source barrier;
- recovery does not move Alice's source ref;
- an exact retry of Alice's original update confirms the existing transaction
  without another canonical commit;
- Bob can then refresh or retry according to ordinary visible-staleness rules.

### P4: Source publication barrier under real processes

Pause Alice at `source.canonical_state_persisted`, kill the pre-receive hook, and
then launch all three probes in separate processes:

- a Bob push;
- a projection worker;
- `forge.py sync` for Bob.

Each probe must reject or return no work without changing authoritative refs,
frontiers, anchors, mappings, changes, events, or jobs. The exact Alice retry
must release the barrier.

### P5: Source ref moved before confirmation

Pause Alice at `source.ref_published` in post-receive and kill that hook.

Expected result:

- Alice's remote ref already equals the submitted commit;
- canonical already contains its corresponding canonical commit;
- persisted state initially remains at the pre-confirmation boundary;
- a fresh recovery observes the source ref and confirms forward;
- recovery removes the stale receiver marker;
- secondary publication begins only after that observation;
- rerunning recovery is idempotent.

### P6: Two workers, one job

1. Create one pending visible projection job.
2. Start worker A and pause it at `worker.claim_persisted`, after lock release.
3. Start worker B at the same logical time.

Expected result:

- worker B does not claim or complete worker A's leased job;
- attempts increase only for actual claims;
- resuming A completes one deterministic representation;
- no duplicate anchor or logical revision is recorded.

### P7: Two workers, independent views

1. Create one administrative update targeting Alice and Bob.
2. Pause worker A after it claims the first sorted view job.
3. Start worker B.

Expected result:

- worker B claims and completes the other view's job while A remains stopped;
- per-view ordering remains intact;
- resuming A completes the transaction;
- a stopped worker for one view does not block an independent view after the
  claim lock is released.

This scenario validates independence within the limits of the current global
lock. It does not claim that object construction or ref publication for two
views is fully parallel while either process is in a lock-held critical section.

### P8: Receive versus claimed projection

Create an administrative update visible only to Bob, complete Alice's
hidden-only job, and leave Bob's job pending. Pause the Bob projection worker at
`worker.claim_persisted`, then start:

- a Bob push, which must reject because Bob has unfinished projection work;
- an Alice push whose visible state is current and whose edit is hidden from
  Bob, which may proceed through the canonical coordinator.

After the Alice push, resume Bob's older worker and settle later Bob jobs in
sequence. No plan may skip or overwrite the intervening canonical update.

### P9: Projected ref publisher dies

Pause a worker at `projection.ref_published` and kill it.

Expected result:

- the view ref equals the plan's new tip;
- persisted view state and the job still describe the old tip and ready plan;
- no other worker can regress the ref;
- after lease expiry, a new worker adopts the exact moved ref;
- no second synthetic commit is created or published;
- final state is semantically equal to uninterrupted worker completion.

## Required Crash Matrix

Every row below must terminate the marked process with `SIGKILL`, wait for the
outer actor to exit, recover in a fresh process, settle as directed, and run the
stable checkpoint oracle.

| Family | Kill point | Required recovery result |
|---|---|---|
| Source | `source.validation_complete` | No authoritative transaction or ref movement; retry is a fresh accepted attempt |
| Source | `source.prepared_persisted` | Prepared transaction aborts because canonical did not move; retry can succeed |
| Source | `source.canonical_ref_published` | Recovery observes canonical publication and records `source_unconfirmed`; exact source retry creates no duplicate canonical commit |
| Source | `source.canonical_state_persisted` | Blocked event remains blocked until exact source retry |
| Source | `source.ref_published` | Recovery confirms the already-moved source ref and activates fanout |
| Source | `source.confirmation_persisted` | Repeated recovery and worker dispatch are idempotent |
| Source | `source.receiver_cleanup_persisted` | Terminal source state remains terminal and no receiver marker remains |
| Admin | `admin.prepared_persisted` | Recovery aborts if canonical stayed old; a new administrative attempt can publish |
| Admin | `admin.canonical_ref_published` | Recovery observes the new canonical ref and creates/activates one event |
| Admin | `admin.canonical_state_persisted` | Repeated recovery does not duplicate jobs or regress transaction status |
| Worker | `worker.claim_persisted` | Job remains leased until deterministic expiry, then is reclaimed once |
| Worker | `worker.plan_persisted` | Durable plan is reused after lease expiry and publishes once |
| Projection | `projection.ref_published` | Moved ref is adopted from the durable ready worker plan |
| Worker | `worker.completion_persisted` | Repeated work finds no duplicate claim or representation |
| Worker | `worker.retry_persisted` | Retry remains eligible only at/after its deterministic deadline and later completes |
| Recovery | `recovery.state_persisted` | A second recovery observes the same forward state and performs no duplicate transition |

The source and worker rows may share fixtures where doing so does not hide the
expected intermediate state. Keep each checkpoint as a named `subTest` or test
method so a failure identifies one boundary directly.

## Atomic State-File Matrix

Use the focused state-save actor to increment a valid positive
`next_transaction_sequence` value exactly once.

### Kill at `state.temporary_fsynced`

- `state.json` must still contain the complete old JSON document.
- It must parse successfully.
- `state.tmp` may contain the complete new document.
- A subsequent unarmed save must replace the stale temporary file and persist
  the intended value.

### Kill at `state.replaced`

- `state.json` must contain the complete new JSON document.
- It must never be empty, truncated, mixed with the old bytes, or invalid JSON.
- A fresh process must load it and pass invariants.

### Kill at `state.directory_fsynced`

- `state.json` must contain the complete new JSON document.
- Repeating recovery or another save must preserve it.

Run each case from a fresh fixture. Do not infer power-loss guarantees from the
`state.replaced` case.

## Receiver Marker Requirements

Real hook termination exposes liveness cleanup that single-process tests do not
fully exercise.

Source confirmation can occur while the matching receive-pack parent is still
active, so `source.confirmation_persisted` may legitimately retain its marker.
The marker must be absent after either normal post-receive cleanup or a later
recovery that observes the recorded receiver is no longer active.

Required behavior:

- a marker whose PID is dead or whose process start identity differs must never
  keep a transaction active;
- stale-marker cleanup must be idempotent;
- cleanup must not remove a marker for a currently active matching receiver;
- a stale marker for an aborted prepared transaction must be removed;
- a stale marker for a source ref that recovery confirms must be removed when
  post-receive never reaches its normal unlink;
- every settled scenario must have no marker for an aborted, confirmed, or
  terminal transaction once its matching receiver has exited.

Add focused state tests for live, dead, reused-PID, aborted, and
already-source-published marker handling. Do not make recovery move the source
ref in order to clean a marker.

## Rejection And Race Assertions

This specification is not the later comprehensive rejection-immutability item,
but every rejection caused by these schedules still needs a focused assertion.

When no other process is changing the forge, capture before and after:

- canonical and all view branch refs;
- view frontiers and tips;
- anchors;
- logical changes;
- accepted mappings;
- event and job identities.

For a rejection concurrent with a winning process, isolate effects by unique
path and `Change-Id` instead of expecting the whole forge snapshot to remain
unchanged. The losing identifiers must be absent after the winner settles.

Client stderr should be checked for a stable category only where Bastion controls
the error. Do not overfit to Git's platform-specific ref-lock wording.

## Diagnostics And Replay

Every failed process test must print:

- test schedule name;
- fixture seed or fixed scenario identifier;
- actor role and full argument vector with temporary roots normalized;
- requested checkpoint;
- marker payload if reached;
- whether the controller sent `SIGCONT` or `SIGKILL`;
- outer process return code;
- captured stdout and stderr;
- canonical and view branch refs;
- transaction, event, and job status summary;
- invariant failure, if any.

Do not print environment secrets or the complete inherited environment.

Schedules are deterministic and individually addressable through `unittest`, so
no random seed mechanism is required. A failing schedule should be replayable
with a command such as:

```bash
python3 -m unittest -v \
  tests.test_process_failpoints.ProcessFailpointTest.test_worker_ref_publication
```

## Runtime And Reliability Budget

- The new default process suite should target less than 90 seconds on the
  reference Linux development machine.
- No individual marker wait should exceed 10 seconds by default.
- No actor shutdown should exceed 20 seconds before process-group cleanup.
- Lease recovery must use logical time, never a real 60-second wait.
- Tests must not retry a failed assertion to hide a race.
- Tests must not depend on process scheduling order that is not established by
  a checkpoint marker.
- All stopped or live actor process groups must be cleaned up in `addCleanup` or
  `finally` blocks.

If the complete crash matrix exceeds the budget, optimize fixture creation and
reuse immutable source setup within one test process. Do not move required rows
behind an opt-in environment variable or reduce them to direct state mutation.

## Implementation Sequence

### 1. Add and unit-test the checkpoint primitive

- Implement `src/failpoints.py`.
- Prove the unarmed call is inert.
- Prove root and name mismatches are inert.
- Prove incomplete configuration fails loudly.
- Prove one matched process writes a valid marker, stops, resumes, and does not
  stop a second time.
- Prove PID/start validation prevents signaling a mismatched process.

No forge transaction code should be instrumented until this helper and
controller cleanup are reliable.

### 2. Add process harness and state-save matrix

- Implement process-group lifecycle and marker waits.
- Add the focused one-save actor.
- Cover all three atomic state-save points.
- Confirm timeout cleanup leaves no stopped processes.

### 3. Instrument and test source and administrative publication

- Add source and admin semantic checkpoints.
- Add source crash rows in publication order.
- Add administrative crash rows.
- Add P1 through P5.
- Fix only recovery defects demonstrated by those tests, including receiver
  marker cleanup.

### 4. Instrument and test worker publication

- Add claim, plan, projection-ref, retry, and completion checkpoints.
- Add P6 through P9.
- Add worker crash rows using deterministic logical time.
- Pass explicit logical time through claim, completion deferral, and failure
  retry paths while preserving default wall-clock behavior.
- Compare recovered and uninterrupted authoritative snapshots.

### 5. Verify and document

- Run the focused process modules repeatedly without retries.
- Run the complete existing suite.
- Run syntax compilation and `git diff --check`.
- Update testing, transaction, and roadmap documentation.

## Test Commands

Focused process tests:

```bash
python3 -m unittest -v \
  tests.test_process_concurrency \
  tests.test_process_failpoints
```

Complete suite:

```bash
python3 -m unittest discover -v tests 'test_*.py'
```

Syntax and repository checks:

```bash
python3 -m py_compile forge.py tests/*.py src/*.py
git diff --check
```

The implementation report should include Linux kernel version, Python version,
Git version, focused runtime, complete-suite runtime, and test count.

## Acceptance Criteria

### Checkpoint infrastructure

- [ ] Unarmed checkpoints have no observable behavior.
- [ ] One exact root/name/marker configuration stops only the intended process.
- [ ] Marker PID and process-start identity are validated before signaling.
- [ ] The checkpoint primitive resumes normal execution after `SIGCONT`, and
      representative source and worker barrier schedules complete after resume.
- [ ] `SIGKILL` leaves only state allowed by the documented boundary.
- [ ] Timeouts clean up the complete actor process group.
- [ ] No user-facing failpoint CLI or persisted failpoint state is added.

### Parallel pushes and source publication

- [ ] Same-view competing shared-visible pushes have one winner and no losing
      published effect.
- [ ] Cross-view hidden-disjoint source commits are both eventually accepted,
      allowing a temporary barrier/pending-work rejection and exact retry.
- [ ] A process killed after canonical CAS leaves source publication
      unconfirmed and blocks competitors.
- [ ] Push, sync, and worker processes cannot cross an unconfirmed source
      barrier.
- [ ] Recovery confirms an already-moved source ref but never moves it itself.
- [ ] Exact source retry never creates a duplicate canonical commit.
- [ ] Terminal source transactions leave no stale receiver marker.

### Workers and projections

- [ ] Two workers cannot claim the same leased job.
- [ ] A worker for another view can progress after the first worker releases the
      claim lock.
- [ ] Per-view sequence ordering survives competing workers.
- [ ] A receive cannot publish through unfinished work for its own view.
- [ ] A killed projected-ref publisher is recovered by exact-tip adoption.
- [ ] Recovery creates no duplicate synthetic commit, anchor, or logical
      revision.
- [ ] Retry timing is deterministic and does not require wall-clock sleeping.

### State and recovery

- [ ] Killing before `os.replace` leaves the complete old `state.json`.
- [ ] Killing after `os.replace` leaves the complete new `state.json`.
- [ ] No test observes partial or invalid persisted JSON.
- [ ] Recovery transitions remain forward-only and idempotent.
- [ ] Recovered published state is semantically equal to uninterrupted state.
- [ ] `check_invariants` passes after every settled schedule.

### Regression safety

- [ ] Existing deterministic state-machine tests continue to pass unchanged in
      behavior.
- [ ] Existing real-Git push, cross-view, worker, service, and invariant tests
      continue to pass.
- [ ] The complete suite is deterministic across at least three consecutive
      focused process-suite runs.
- [ ] Documentation accurately distinguishes process-crash evidence from power-
      loss and production-security guarantees.

## Exit Criteria

This initiative is complete when:

1. every required checkpoint has a real `SIGKILL` recovery test;
2. every required parallel schedule uses ordinary OS processes and explicit
   markers rather than in-process reconstruction;
3. all recovered stable checkpoints pass the global invariant checker,
   including its strict Git filesystem checks;
4. recovered and uninterrupted executions have identical published histories,
   trees, frontiers, anchors, mappings, and logical representations;
5. no state/ref divergence, duplicate publication, stale terminal receiver
   marker, or later transaction crossing an unconfirmed source remains;
6. the focused process suite and complete suite pass reliably within the
   runtime budget;
7. failures identify one named schedule and checkpoint and are directly
   replayable as permanent regression tests.

Only after these criteria are met should Phase 1 proceed to the expanded Git
tree-operation, conflict, and broad rejection-immutability matrix.
