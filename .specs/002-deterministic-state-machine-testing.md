# Deterministic State-Machine Testing

## Status

Implemented.

This specification covers the second item in Phase 1 of `ROADMAP.md`:

> Add deterministic property/state-machine tests generating canonical updates,
> user pushes, sync, retries, permission changes, worker crashes, and recovery.

The global invariant checker from the preceding roadmap item exists in
`src/invariants.py`. This work makes it the post-operation oracle for generated
sequences and adds a smaller independent tree model so the tests do not merely
ask the implementation to verify itself.

## Goal

Add a deterministic, replayable test harness that executes varied sequences of
currently supported forge operations against real Git repositories. After every
stable operation, the harness must:

- run the global invariant checker;
- compare canonical and projected trees with an independent in-memory model;
- verify the expected success or rejection outcome;
- retain enough structured trace data to replay any failure from one seed.

The default suite must be bounded enough for normal local and CI execution. A
developer must also be able to run a longer sequence or one exact failing seed
without editing the test.

## Why This Is Next

`ROADMAP.md` orders work by architectural risk. The invariant checker has now
been implemented, and the next listed risk is whether its model survives
combinations of ordinary operations rather than isolated examples.

Do not start permission lifecycle, PostgreSQL, network transport, forge product,
or scale work as part of this specification.

## Current-Model Decisions

### Permission changes

Version 1 has immutable positive read and write prefixes. There is no valid API
for changing an existing view's policy, and historical projection replay in the
checker intentionally uses the current immutable descriptor.

For this specification, the generated permission operation is **creation of a
new immutable view with a different effective descriptor**. It exercises a
permission-configuration change in the forge without mutating historical policy.

This is an explicit version-1 interpretation of the roadmap wording. If the
roadmap is later interpreted to require mutation of an existing authorization
descriptor in Phase 1, this specification is only partial and that work must use
the versioned policy model from Phase 2 rather than retroactively editing v1
history.

Do not directly edit `state["views"][name]["read"]` or `write`. Do not add policy
versions, revocation, expansion, contraction, principals, teams, roles, or deny
rules. Those belong to Phase 2. The test and `docs/TESTING.md` must state this
interpretation explicitly so view creation is not mistaken for revocation
coverage.

### Crashes

This work uses deterministic, single-process reconstruction of durable crash
states through existing split transaction and worker APIs. It does not add
failpoint infrastructure and does not kill operating-system processes. Real
parallel processes, named barriers, failpoints, and process termination are the
next separate Phase 1 items.

Every generated action must begin at a stable checkpoint and return at a stable
checkpoint. A compound crash/recovery action may cross an unstable ref/state
window internally, but must run recovery or adoption before calling
`check_invariants`.

### Property framework

Use the Python standard library: `unittest`, `random.Random`, temporary
directories, and real Git subprocesses. Do not introduce Hypothesis or another
dependency only for this work; the repository has no dependency-management
surface today.

### Supported Git model

Keep the current restrictions:

- SHA-1 only;
- one `main` branch;
- linear canonical and submitted histories;
- one ref update per push;
- no merges, force pushes, ref creation/deletion, or gitlinks;
- regular-file create, modify, and delete operations in generated sequences;
- positive read/write prefixes only.

The complete mode, symlink, rename, directory-boundary, unusual-name, and long-
path matrix remains a later Phase 1 item. Focused existing tests for those rules
must continue to pass.

## Required Deliverables

### New files

- `tests/state_machine.py`: reusable deterministic harness, reference model,
  operation implementations, trace formatting, and seed runner. Its filename
  deliberately does not match `test_*.py`.
- `tests/test_state_machine.py`: bounded default tests, focused operation tests,
  determinism/equivalence tests, and permanent regression seeds.

### Updated files

- `tests/test_invariants.py`: add the checker-readiness cases listed below.
- `src/invariants.py`: only make changes demonstrated by those readiness tests.
- `src/worker.py`: remove `lease_expires_at` when a job completes, and add a
  focused assertion to `tests/test_worker.py`. A completed job has no active
  lease, and retaining its wall-clock deadline makes otherwise identical
  ordinary pushes persist different state. Apply the cleanup in both completion
  paths: the already-covered-frontier shortcut in `claim_projection_job` and
  normal `complete_projection_job`. Both paths must also leave `lease_token` as
  `None` and remove any retained `plan`.
- `.specs/001-global-invariant-checker.md`: replace the stale status text with a
  factual implementation status. Do not claim that its full required corruption
  matrix is complete unless that matrix has actually been added.
- `docs/TESTING.md`: document the new module, default profile, exact-seed replay,
  extended runs, trace format, and the v1 interpretation of permission changes.

### Files not expected to change

- `forge.py`: no state-machine CLI command is needed.
- `src/protocol.py`, `src/service.py`, `src/state.py`, and `src/outbox.py`: use
  their existing APIs. Prefer test-side clock/token control with
  `unittest.mock.patch` over production-only injection hooks.
- `ROADMAP.md`: do not mark all of Phase 1 complete. The later Phase 1 bullets
  remain open after this work.

Beyond the required completed-lease cleanup in `src/worker.py`, if another
production change outside `src/invariants.py` proves necessary, keep it to one
general deterministic seam, explain why test-side control is insufficient, and
add a focused unit test for its unchanged default behavior.

## Checker Readiness Gate

The generated harness depends heavily on `check_invariants`. Before adding
random sequences, add these focused tests and fixes:

1. Add a failing-check read-only test. Create one minimal invariant violation,
   snapshot the already-corrupted `state.json` bytes, all canonical/view refs,
   and `git count-objects -v`, call `check_invariants`, assert its expected stable
   code, and prove all snapshots are unchanged.
2. Add a valid `ready` projection job with a retained plan and an unchanged
   projected ref. It must pass because this is a documented stable checkpoint.
3. Add a coherent plan corruption test for canonical `Change-Id` validation.
   Start with a valid retained plan for a visible canonical commit that has a
   `Change-Id`. Replace the plan anchor value, rebuild the synthetic commit so
   its bytes match that wrong value, and update the anchor key and `plan.new_tip`
   consistently. Leave the canonical commit unchanged. The current self-
   consistent plan must fail with `JOB` and detail containing
   `plan Change-Id differs from canonical commit`. `_plans` must parse the
   canonical commit's expected value rather than treating the anchor as its own
   oracle.
4. Add a coherent completed-job/frontier corruption test. Create two ordered
   administrative transactions for the same view. Make the earlier job and its
   event/transaction internally consistent as completed/published without
   advancing the view, and leave the later job pending so it durably explains
   frontier lag. The checker must reach `JOB` and report that the earlier
   completed target is ahead of the view frontier. Completed targets equal to or
   behind the frontier remain valid.
5. Run the existing healthy, pending, running, retry, source-unconfirmed, and
   corruption tests before using the checker in generated tests.

These cases are a readiness gate for this harness, not a waiver of the larger
test matrix in `.specs/001-global-invariant-checker.md`.

## Harness Public Surface

`tests/state_machine.py` must expose these concepts. Exact private helper names
may vary, but keep this boundary small enough for `tests/test_state_machine.py`
to use directly:

```python
@dataclass(frozen=True)
class Operation:
    kind: str
    arguments: dict[str, object]


@dataclass
class RunResult:
    seed: int
    steps: int
    trace: list[Operation]
    state_bytes: bytes
    refs: dict[str, dict[str, str]]


def run_scenario(seed: int, steps: int) -> RunResult:
    """Create a fresh forge, execute one deterministic sequence, and verify it."""


def replay_scenario(seed: int, operations: list[Operation]) -> RunResult:
    """Execute recorded operations directly without consulting random state."""
```

Use normal dataclasses or equivalently simple immutable records. Do not build a
generic state-machine framework or test DSL.

`run_scenario` must own and clean up its temporary directory. When an assertion
fails, cleanup may still occur because the complete replay input is the seed,
step count, and operation trace, not the temporary repository.

## Determinism Contract

The following inputs must come only from the scenario seed and step number:

- operation selection;
- view selection;
- path selection;
- create, modify, or delete selection;
- commit count for a generated push;
- file contents;
- commit messages;
- `Change-Id` values;
- author and committer timestamps;
- staging-ref names;
- logical worker clock values;
- worker lease tokens used during generated worker actions.

Use exactly one `random.Random(seed)` per scenario. Do not use module-global
random functions, `os.urandom`, `forge.new_change_id`, current wall-clock time,
or sleep.

Use a fixed timestamp formula such as:

```text
1_800_000_000 + step * 10 + commit_index
```

Use deterministic `Change-Id` values derived from SHA-1 of a stable string
containing the seed, step, and commit index. The value must start with `I`.

Patch `worker.time.time` and `worker.uuid.uuid4` around in-process generated
worker calls so failed jobs, retry deadlines, lease expirations, and lease tokens
are replayable. Mocks in the test process cannot affect workers launched by an
ordinary push's post-receive child process. Therefore ordinary push operations
must start with no pending event or unfinished job anywhere in the forge, and a
completed worker must remove `lease_expires_at` together with `lease_token`.
Healthy child workers then leave only deterministic persisted fields.

Do not assert or print real PIDs, temporary paths, or current timestamps.

Running the same seed and step count twice in fresh directories must produce:

- the same ordered operation trace;
- the same canonical branch OID;
- the same projected branch OID for every identically configured view;
- byte-identical `state.json` after all work is settled.

If exact final state differs, the test must identify the first differing field;
do not normalize away nondeterminism except receiver PID markers, which must not
exist at the final settled checkpoint.

### Deterministic Git environment

All harness Git subprocesses, including fixture setup, clones, commits, fetches,
and pushes, must use one test-only clean environment implementation. Do not call
`src/git.py::clean_env` because the harness oracle should remain independent,
but mirror its isolation contract:

- remove `GIT_DIR`, `GIT_WORK_TREE`, `GIT_IMPLICIT_WORK_TREE`,
  `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`,
  `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_COMMON_DIR`, `GIT_GRAFT_FILE`,
  `GIT_SHALLOW_FILE`, `GIT_REPLACE_REF_BASE`, `GIT_NAMESPACE`, `GIT_PREFIX`,
  `GIT_INTERNAL_SUPER_PREFIX`, and `GIT_QUARANTINE_PATH`;
- remove `GIT_CONFIG`, `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`, and every
  `GIT_CONFIG_KEY_*` and `GIT_CONFIG_VALUE_*` variable;
- set `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL` to the null device,
  `GIT_NO_REPLACE_OBJECTS=1`, `LC_ALL=C`, and `LANG=C`;
- initialize the fixture explicitly with `git init --object-format=sha1`;
- set deterministic author/committer identity and date for every commit;
- disable commit and tag signing in each working repository.

`protocol.pre_receive` copies `os.environ` for quarantine/object reads. Around
direct in-process `pre_receive` and `post_receive` calls in source crash/retry
actions, patch `os.environ` to the same clean deterministic environment. A clean
subprocess environment alone does not affect those direct calls.

## Initial Fixture

Every scenario starts from the same real-Git fixture.

### Canonical tree

Create one deterministic initial commit with these regular files:

| Path | Contents |
|---|---|
| `shared/base.txt` | `shared base\n` |
| `team-a/base.txt` | `team a base\n` |
| `team-b/base.txt` | `team b base\n` |
| `hidden/secret.txt` | `hidden secret\n` |

Set fixed author/committer identity and timestamp for the initial commit.

### Initial views

Create these two overlapping views before generated steps begin:

| View | Read prefixes | Write prefixes |
|---|---|---|
| `alice` | `shared`, `team-a` | `shared`, `team-a` |
| `bob` | `shared`, `team-b` | `shared`, `team-b` |

Clone both with `git clone --no-local`, set deterministic local Git identities,
and retain the checkouts across steps so stale-client behavior can occur.

### Addable view descriptors

The generated `add_view` action may consume each descriptor at most once:

| View | Read prefixes | Write prefixes |
|---|---|---|
| `reader` | `shared` | none |
| `team-a-bot` | `team-a` | `team-a` |
| `shared-bot` | `shared` | `shared` |

Clone a newly added writable view immediately. A read-only view does not need a
working clone because it cannot produce an authorized push.

Adding another immutable descriptor is the only generated permission operation
in this version.

## Finite Generated Data

Use a deliberately small path alphabet so failures minimize naturally and runs
do not grow without bound:

```text
shared/a.txt
shared/b.txt
team-a/a.txt
team-a/b.txt
team-b/a.txt
team-b/b.txt
hidden/a.txt
hidden/b.txt
```

Generated contents must include seed, step, and edit index, for example:

```text
seed=12648430 step=7 edit=0
```

An edit is one of:

- create an absent path;
- replace the bytes of an existing regular file;
- delete an existing path.

Only choose an edit whose precondition is true. Do not turn a requested delete
of an absent path into an empty commit. A separate focused test may generate an
intentional no-op commit because that behavior already belongs to the checker
matrix.

Bound normal generated pushes to one or two commits, with one edit per commit.
This is enough to exercise per-transition authorization without absorbing the
later full tree-operation matrix.

## Independent Reference Model

### Tree representation

Represent each snapshot as:

```python
dict[bytes, tuple[bytes, bytes]]
```

The value is `(mode, contents)`. Generated entries use mode `b"100644"`. Keep
paths and contents as bytes in the model even though the generated alphabet is
ASCII.

The model must maintain:

- the expected current canonical tree;
- the expected tree for every accepted canonical commit, keyed by its observed
  opaque OID;
- every view's immutable read and write prefixes;
- every client's expected `HEAD` OID and tree, plus whether it has an unpushed
  local commit and whether its worktree is clean;
- an outstanding source update, if a source crash has left one unconfirmed;
- an outstanding claimed worker job, if a worker crash has left one leased;
- the structured operation trace.

It is acceptable to use observed OIDs as opaque keys. Expected file and policy
semantics must be computed independently.

Admin edit preconditions are evaluated against the canonical model tree. User
edit preconditions are evaluated against that client's model `HEAD` tree, not
the canonical tree: hidden canonical paths may correctly be absent from a
checkout. Every selected user edit must produce a nonempty diff against the
client's current `HEAD`. Update the client model when a commit is created, even
if its later push rejects. `client_refresh` resets its OID, tree, unpushed flag,
and clean flag to the published view state.

### Independent policy filter

Implement a test-only prefix predicate directly in `tests/state_machine.py`:

```text
allowed when prefix is empty, path equals prefix, or path starts with prefix + "/"
```

Do not import or call `validation.allowed`, `projection.filter_tree`,
`projection.project_history`, `reconciliation.reconcile_commits`, or
`invariants._filtered` to produce an expected tree.

### Independent Git tree reader

Read actual snapshots with Git plumbing, not production tree helpers:

1. Run `git ls-tree -rz --full-tree <commit>`.
2. Parse every NUL-delimited `mode type oid<TAB>path` record.
3. Require `type == blob` for this generated subset.
4. Read contents with `git cat-file blob <oid>`.
5. Compare the resulting `dict[bytes, tuple[bytes, bytes]]` exactly with the
   model.

Do not use `src/git.py::tree_entries` in this oracle.

### Checkpoint assertions

After each stable action:

1. Call `check_invariants(forge_root)`.
2. Read canonical `refs/heads/main` and compare its complete tree with the
   model's current canonical tree.
3. Load state only to obtain each view's persisted `canonical_frontier` and
   projected tip. The model's expected canonical snapshot for that opaque
   frontier must exist.
4. Independently filter that canonical snapshot by the view's model policy and
   compare it with the complete tree at the view's published ref.
5. Require the actual projected ref to equal `view.projected_tip`.
6. For every client, compare its actual `HEAD` OID and complete tree with its
   modeled local OID and tree. If the client has no unpushed commit and its model
   tip equals the published view tip, also require that tree to equal the view's
   expected projected tree.

Using the persisted frontier to select an independently modeled canonical
snapshot does not make state authoritative for tree correctness. The global
checker separately proves that the frontier is legal and that durable lag is
explained.

### Learning canonical OIDs

After an accepted administrative update, associate the returned/new commit OID
with the model snapshot created by that edit.

After an accepted one- or two-commit source push, find its new transaction by
the deterministic submitted old/new pair. Use the ordered `mappings` only to
associate each opaque canonical OID with the corresponding independently
computed snapshot. Do not use mapping trees or production reconciliation output
as expected data.

Immediately compare every newly accepted canonical commit's actual tree with
its associated model snapshot, not only the branch tip.

## Authoritative Snapshot For Rejections

Before an operation expected to reject, capture:

- `state.json` bytes;
- canonical `refs/heads/main`;
- every view's configured projected ref;
- `state["changes"]` serialized with sorted keys;
- every transaction's `mappings` serialized with sorted keys.

After rejection and before another operation, require all of those values to be
unchanged. Do not compare physical object counts: a rejected receive may leave
well-formed unreachable objects, and dangling objects are not publications.

This is required for the generated rejection actions in this specification. It
does not claim completion of the later exhaustive rejection matrix across every
unsupported Git operation.

## Operation Grammar

Every generated step follows this order:

1. Append and execute a `recover_dispatch` operation with
   `before_step=<step>`. It acquires the ordinary state lock once and releases it
   so normal recovery and outbox dispatch run before eligibility is evaluated.
2. Run the checkpoint assertions even when stabilization changed no bytes.
3. Build a list of operations whose preconditions hold in the stabilized state.
4. Select one using the scenario's `random.Random`.
5. Capture any required rejection snapshot.
6. Execute the selected operation and run its stable checkpoint assertions.

This order is mandatory because every normal locked API invokes recovery before
its own checks. In particular, a pending administrative event becomes jobs
before a push or sync checks for unfinished work. Eligibility must inspect the
post-dispatch state, and rejection snapshots must be captured after dispatch.

`recover_dispatch` records are always present, are automatic, and do not count
toward the requested generated step count or operation-coverage totals. This
makes direct trace replay unambiguous. A stabilization failure uses the upcoming
generated step as `failed step` and `recover_dispatch` as the operation.

For this specification, **globally projection-quiescent** means every outbox
event is already `dispatched`, every projection job is `completed`, no source is
unconfirmed, and no receiver marker or outstanding worker crash exists.

For worker eligibility, reproduce the pure scheduler order from the stabilized
state: expire due `running`/`ready` leases, then order due `pending`/`retry` jobs
by `(sequence, id)` and block a candidate behind lower incomplete work for the
same view. Record the expected claimed job ID in the operation. If production
would first mark an already-covered target complete and continue, record those
IDs as `auto_completed` and assert the exact sequence before the claimed job.

Do not choose an operation and silently turn it into a no-op because its
precondition failed.

Every operation record must include all choices needed for replay. The replay
path consumes the recorded operations directly and must not call the random
generator again.

### 1. `add_view`

Preconditions:

- at least one addable descriptor remains;
- no source transaction is unconfirmed;
- no worker crash action is between ref publication and adoption.

Execution:

- call `forge.add_view` with the selected fixed descriptor;
- clone it if writable;
- add its immutable policy to the model.

Expected result:

- success;
- its frontier reaches the current canonical tip;
- its tree equals the independent policy filter;
- existing view refs do not move.

### 2. `admin_edit`

Preconditions:

- no source transaction is unconfirmed;
- the selected create/modify/delete edit is valid in the canonical model.

Execution:

- fetch/reset a dedicated admin checkout to canonical `main`;
- apply one model-selected edit;
- create one deterministic commit;
- push it to `refs/staging/state-machine-<seed>-<step>`;
- call `forge.update_canonical_ref(root, old_oid, new_oid)`;
- do not automatically drain workers.

Expected result:

- success;
- canonical tree advances exactly once;
- the outbox may remain pending and view refs may remain unchanged;
- the new canonical snapshot is added to the model.

### 3. `client_refresh`

Preconditions:

- the selected view has a working clone.

Execution:

- run `git fetch -q origin`;
- run `git reset -q --hard origin/main`;
- record the fetched remote tip and tree as the client's model state and mark it
  clean with no unpushed commit.

Expected result:

- success;
- no forge state, canonical ref, or projected ref changes.

### 4. `authorized_push`

Preconditions:

- the selected view is writable;
- its client `HEAD` equals the current view ref;
- the forge is globally projection-quiescent;
- every selected edit path matches both its model read and write prefixes.

Execution:

- create one or two deterministic linear commits in the persistent checkout;
- include a deterministic `Change-Id` on a deterministic subset, including both
  with-ID and without-ID cases across the default tests;
- run ordinary `git push -q origin main` so real hooks execute.

Expected result:

- success;
- source remote publishes the exact submitted tip;
- each submitted transition produces the model's exact canonical snapshot;
- post-receive may dispatch and drain all currently due jobs;
- the checkpoint oracle accounts for each view's resulting frontier;
- the source client remains clean at its submitted `HEAD` with no unpushed
  commit in the model.

### 5. `unauthorized_push`

Preconditions:

- the selected view has a working clone;
- its client `HEAD` equals the current view ref;
- the forge is globally projection-quiescent;
- at least one selected path fails read or write authorization.

Execution:

- create one deterministic commit changing that path;
- run ordinary `git push origin main` with `check=False`.

Expected result:

- nonzero exit;
- stderr contains the stable authorization reason, not a Python traceback;
- all authoritative rejection snapshots are unchanged;
- reset the local checkout back to `origin/main` before it is used by a later
  generated operation, and reset its modeled OID, tree, clean flag, and unpushed
  flag at the same time.

### 6. `stale_push`

Preconditions:

- the selected client's `HEAD` differs from its view's current remote tip;
- the checkout contains or can create one authorized local commit;
- the forge is globally projection-quiescent.

Execution:

- create an authorized deterministic commit when needed;
- run ordinary `git push origin main` with `check=False`.

Expected result:

- nonzero exit from Git or the hook's stale-update rule;
- all authoritative rejection snapshots are unchanged;
- leave the client stale so a later `client_refresh` is meaningful.

### 7. `server_sync`

Preconditions:

- a view exists.

Execution:

- call `forge.sync_view(root, view)`.

Expected result:

- success when the selected view has no active receive or unfinished job and
  there is no global unconfirmed source transaction;
- otherwise `ForgeError` with the corresponding stable barrier reason;
- a rejection preserves the authoritative snapshot;
- a success makes the selected view equal the filter of the current canonical
  tip without changing other view refs.

Barrier precedence must match `sync_view_locked`: the unconfirmed source view
reports `view has an active receive transaction`; another view reports
`canonical history contains an unconfirmed source publication`; an ordinary
unfinished job reports `view has pending projection work`.

### 8. `worker_complete`

Preconditions:

- at least one job is claimable at the logical clock;
- no source transaction is unconfirmed.

Execution:

- call `worker.run_projection_job(root, now=logical_now)` once with deterministic
  worker time and lease token patches.

Expected result:

- exactly one due, per-view-order-eligible job is attempted;
- a normal healthy fixture completes it;
- no higher-sequence job passes incomplete lower work for the same view;
- the target view advances or validly elides hidden-only work.

### 9. `worker_fail_then_retry`

Preconditions:

- at least one job is claimable;
- no worker crash is already outstanding;
- no source transaction is unconfirmed.

Execution:

- claim one job at `logical_now`;
- fail it with `RuntimeError("generated temporary failure")` under the patched
  deterministic worker clock;
- run the checkpoint assertions while it is in `retry`;
- advance the logical clock beyond `next_attempt_at`;
- run one worker attempt to completion.

Expected result:

- the first stable sub-checkpoint has retry status, no lease token, a stable
  error, and unchanged refs;
- the second stable sub-checkpoint completes or validly elides the same job;
- final refs and mappings equal an uninterrupted completion.

Count both stable sub-checkpoints in the trace, either as two explicit operation
records or as one operation with named `failed` and `retried` checkpoints.

### 10. `worker_crash_before_ref`

Preconditions:

- at least one job is claimable;
- no worker crash is already outstanding;
- no source transaction is unconfirmed.

Execution:

- claim the job;
- build its projection plan;
- persist status `ready` and the plan using the same fields that
  `complete_projection_job` persists before publication;
- do not move the projected ref and do not complete the job;
- retain the job ID and lease deadline in the model.

Expected result:

- this is a stable checkpoint accepted by `check_invariants`;
- the projected ref remains at `plan.old_tip`;
- the ready plan is deterministic and retained for recovery.

Direct state mutation is permitted only in this test helper to reconstruct the
documented durable boundary. Acquire `state.lock` with recovery disabled, mutate
only the selected job, and call `save_state`.

### 11. `recover_expired_worker`

Preconditions:

- `worker_crash_before_ref` left an outstanding job;
- the logical clock can advance beyond its lease expiry.

Execution:

- advance logical time past `lease_expires_at`;
- call `worker.run_projection_job` once.

Expected result:

- the expired ready lease is requeued and reclaimed;
- its retained plan is used;
- the job completes;
- the model clears the outstanding worker crash;
- authoritative output equals ordinary uninterrupted completion.

### 12. `worker_crash_after_ref_and_recover`

Preconditions:

- a claimable job targets a model snapshot whose independently filtered tree
  differs from the selected view's current model tree;
- no worker crash is already outstanding;
- no source transaction is unconfirmed.

Execution:

1. Claim the job and only then build its production plan. Eligibility must not
   call `build_projection_plan`, because plan construction writes projected
   objects and is not a read-only query.
2. Persist `ready` plus the retained plan under the lock.
3. CAS move the projected ref from `old_tip` to `new_tip` directly with Git.
4. Do **not** call the invariant checker in this unstable window.
5. Advance logical time beyond the lease.
6. Run one worker attempt so it adopts the already-published ref and persists
   anchors, frontier, representations, and completion.
7. Run the normal checkpoint assertions.

Expected result:

- recovery does not create a second projected commit;
- recovery does not rewind the ref;
- the retained plan is removed on completion;
- authoritative output equals uninterrupted completion.

### 13. `source_crash`

Preconditions:

- the selected writable client is current;
- the forge is globally projection-quiescent;
- the generated edit is authorized.

Execution:

1. Create one deterministic local commit.
2. Fetch its objects into the source bare repository without moving `main`, as
   in `tests/test_cross_view.py`.
3. Call `forge.pre_receive` with the exact `old new refs/heads/main` update while
   `os.environ` is patched to the deterministic Git environment.
4. Remove the receiver marker to model receiver death.
5. Trigger ordinary lock recovery without moving the source ref.
6. Record the old/new update as outstanding in the model.
7. Run the normal checkpoint assertions.

Expected result:

- canonical contains the accepted candidate;
- source projected ref remains old;
- transaction is `source_unconfirmed`;
- outbox event remains blocked;
- no other view crosses the canonical commit;
- later pushes, workers, and server sync are blocked; the generated grammar
  proves this through `source_barrier_probes` before retry.

### 14. `source_barrier_probes`

Preconditions:

- `source_crash` left an outstanding update;
- this outstanding update has not yet been barrier-probed.

Execution:

1. From another current writable view, create an authorized deterministic local
   commit and attempt an ordinary push.
2. Call `worker.run_projection_job` once at deterministic logical time.
3. Call `forge.sync_view` for a view other than the unconfirmed source.
4. Reset the probe client's rejected local commit to `origin/main`.
5. Reset that client's modeled OID, tree, clean flag, and unpushed flag to the
   same published state.
6. Mark the outstanding update as barrier-probed in the model.

Expected result:

- the push rejects with the unconfirmed-source reason;
- the worker reports no attempted job;
- sync raises the unconfirmed-source reason;
- each probe leaves the authoritative rejection snapshot unchanged;
- no view crosses the unconfirmed canonical commit.

### 15. `source_retry`

Preconditions:

- `source_crash` left an outstanding exact old/new update.
- `source_barrier_probes` has completed for that update.

Execution:

1. Call `forge.pre_receive` again with the same update while `os.environ` is
   patched to the deterministic Git environment.
2. CAS update the source bare ref from old to new.
3. Call `forge.post_receive` with the same update while applying deterministic
   worker clock/token patches, because this in-process call drains fanout jobs.
4. Clear the outstanding source update in the model and mark the source client
   clean at the published submitted OID/tree with no unpushed commit.

Expected result:

- no second canonical commit is created;
- source publishes the exact submitted commit;
- the blocked event activates;
- due fanout jobs may drain;
- the repository-wide barrier clears;
- canonical and projected trees match the independent model.

## Operation Selection And Coverage

Use weighted selection only among currently eligible actions. Keep weights in
one plainly visible constant so future contributors can adjust coverage.

The bounded default generated test does not pass merely because random choices
happened not to exercise difficult operations. Across the default seed set,
require coverage counters for at least:

- one create, one modify, and one delete edit;
- one administrative visible edit;
- one administrative hidden-only edit;
- one accepted source push;
- one accepted commit with a `Change-Id` and one without;
- one rejected unauthorized push;
- one stale push rejection;
- one client refresh;
- one successful server sync;
- one blocked server sync;
- one immutable view addition;
- one ordinary worker completion;
- one worker retry;
- one crash before projected ref publication and lease recovery;
- one crash after projected ref publication and adoption;
- one source-unconfirmed crash, barrier probe, and exact retry.

If relying on random selection makes this coverage fragile, prepend a short
seed-derived mandatory operation schedule and randomize only the remaining
steps. Focused test methods for the crash operations are still required.

Coverage counters are aggregated across the committed smoke and regression
seeds in the default profile. They are not enforced when
`BASTION_STATE_MACHINE_SEEDS` or `BASTION_STATE_MACHINE_STEPS` is set; an explicit
one-seed replay may intentionally be shorter than the mandatory schedule.

## Stable Checkpoints

Call `check_invariants` after every generated operation and at both stable
sub-checkpoints of retry operations.

Accepted stable states include:

- a pending administrative event;
- pending projection jobs;
- a running job whose ref has not moved;
- a ready or retry job with a valid retained plan whose ref has not moved;
- a source-unconfirmed transaction with blocked event and unchanged source ref;
- a fully settled forge.

Do not call the checker:

- after a projected ref moves but before its ready plan is adopted;
- while a source receive is still represented by a live receiver marker;
- while any helper is actively writing refs or `state.json`.

At the end of every scenario, run `source_barrier_probes` first if an outstanding
source update has not been probed, resolve the exact source retry, recover any
expired worker, and drain all jobs with advancing deterministic logical time.
Then run one final checkpoint and require all transactions and jobs to be in
terminal states.

Record each state-changing final-settling action as an operation with
`phase="epilogue"`. A failure during settling reports `failed step` equal to the
requested generated step count and names the exact epilogue operation. Replay
executes the recorded epilogue operations rather than inventing untraced cleanup.

## Crash Equivalence Oracle

Focused tests must execute the same logical visible edit through uninterrupted
and crash/recovery paths in separate fresh fixtures.

Compare these authoritative outputs exactly:

- canonical branch OID and tree;
- every projected branch OID and tree;
- every view frontier, projected tip, and anchors;
- logical `changes` records;
- transaction terminal statuses and pending views.

Worker attempt counts, historical lease deadlines, and retained error strings
are operational bookkeeping and may differ. Do not ignore differences in refs,
trees, frontiers, anchors, mappings, logical revisions, or terminal status.

Required equivalence pairs:

- ordinary worker completion versus crash before ref plus lease recovery;
- ordinary worker completion versus crash after ref plus adoption;
- ordinary successful source publication versus source-unconfirmed plus exact
  retry.

## Trace And Replay

### Trace format

Store one structured `Operation` per action. On failure, raise an assertion whose
message contains:

```text
state-machine failure
seed: <decimal>
steps: <requested count>
failed step: <zero-based index>
operation: <kind and sorted arguments>
trace:
0000 <kind> <JSON arguments with sorted keys>
0001 <kind> <JSON arguments with sorted keys>
...
```

Append the original assertion or `ForgeError` text after the trace. Trace
arguments must be JSON-serializable and contain no temporary paths, PIDs, random
lease UUIDs, or wall-clock values.

`replay_scenario` consumes the trace records directly, including automatic
`recover_dispatch` and epilogue records. It validates each recorded operation's
preconditions but never rerolls its kind or arguments. Environment-based seed
replay regenerates the same trace and is the normal developer workflow; direct
trace replay exists to test and diagnose the trace contract itself.

### Environment controls

`tests/test_state_machine.py` must support:

- `BASTION_STATE_MACHINE_SEEDS`: comma-separated integers accepted by
  `int(value, 0)`. When set, run these seeds instead of the smoke seeds, while
  still running permanent regression seeds.
- `BASTION_STATE_MACHINE_STEPS`: positive integer overriding generated sequence
  length.

Example replay:

```bash
BASTION_STATE_MACHINE_SEEDS=0xc0ffee \
BASTION_STATE_MACHINE_STEPS=80 \
python3 -m unittest -v tests.test_state_machine
```

Reject malformed environment values with a clear assertion before creating a
fixture.

### Default and extended profiles

Define small committed smoke seeds and a separate permanent regression list:

```python
SMOKE_SEEDS = (0x5EED, 0xC0FFEE)
REGRESSION_SEEDS: tuple[int, ...] = ()
DEFAULT_STEPS = 24
```

The exact smoke seeds may change during implementation if needed to satisfy the
required operation coverage, but they must remain fixed after merge.

When a generated failure exposes a product or checker bug:

1. preserve the original failing seed;
2. add the seed to `REGRESSION_SEEDS`;
3. add a smaller focused test when the failure can be expressed clearly;
4. fix the bug;
5. prove the focused test and original seed both pass.

Do not silently replace a failing smoke seed with an easier seed.

## Required Focused Tests

`tests/test_state_machine.py` must include focused tests for:

1. The pure prefix filter includes exact-prefix and descendant paths and excludes
   sibling prefixes.
2. The independent Git tree reader preserves complete bytes and modes.
3. The same seed twice produces the same trace, refs, and settled state bytes.
4. An administrative visible edit advances only after worker execution.
5. An administrative hidden-only edit advances a frontier without moving the
   affected projected tip.
6. An accepted two-commit source push applies both transitions and preserves the
   exact submitted source tip.
7. An unauthorized generated push rejects without changing authoritative state,
   refs, changes, or mappings.
8. A stale generated push rejects without changing authoritative state, refs,
   changes, or mappings.
9. Adding each fixed immutable view descriptor produces its exact filtered tree.
10. A failed worker reaches retry and later converges.
11. A ready retained plan with an unchanged ref survives the checker and lease
    recovery.
12. A ref-published retained plan is adopted after lease recovery without a
    duplicate commit.
13. A source-unconfirmed transaction blocks publication and exact retry converges
    without another canonical commit; blocked push, worker, and sync probes all
    preserve authoritative state.
14. Each crash path is equivalent to uninterrupted execution under the crash
    equivalence oracle.
15. Default smoke and regression seeds complete with all required operation
    coverage counters satisfied.
16. A deliberately injected harness assertion formats seed, failed step,
    operation, and complete replay trace deterministically.
17. Create, modify, delete, with-`Change-Id`, and without-`Change-Id` actions all
    occur in the aggregate default profile.
18. Explicit environment replay does not enforce aggregate default-profile
    coverage.

Use `subTest(seed=seed)` for the bounded seed loop, but catch and re-raise
failures with the complete trace so unittest's subtest label is not the only
diagnostic.

## Ordered Implementation Plan

Implement and verify in this order:

1. Complete the checker readiness gate in `tests/test_invariants.py` and
   `src/invariants.py`. Run `tests.test_invariants`.
2. Remove completed-job `lease_expires_at` in `src/worker.py`, assert all active
   lease fields are cleared on completion, and run `tests.test_worker`.
3. Add deterministic clean-environment command, commit, tree-reader, snapshot,
   and prefix-filter helpers in `tests/state_machine.py`. Add focused tests for
   the pure helpers.
4. Add the fixed fixture and reference model, including complete per-client
   local tree state. Prove an untouched fixture passes the checker and
   independent canonical/view tree oracle.
5. Implement deterministic admin edits, `client_refresh`, and model snapshot
   association. Add visible and hidden-only focused tests.
6. Implement the pre-step recovery/dispatch stabilization protocol and trace.
   Test that an administrative pending event is dispatched before push/sync
   eligibility and before a rejection snapshot.
7. Implement accepted one- and two-commit source pushes. Require global
   projection quiescence first, then add immediate checks for every mapped
   canonical snapshot.
8. Implement unauthorized and stale rejection actions with authoritative
   before/after snapshots.
9. Implement generated immutable view addition and its pure projection check.
10. Implement ordinary worker completion, deterministic clock/token control,
    failure, retry, and final draining.
11. Implement crash-before-ref reconstruction and lease recovery. Add the ready-
    plan stable-checkpoint and equivalence tests.
12. Implement crash-after-ref reconstruction and adoption. Determine eligibility
    from the independent model, and never call the checker inside the unpersisted
    ref/state window.
13. Implement source-unconfirmed reconstruction, barrier probes, and exact retry
    using the existing split receive pattern.
14. Add operation eligibility, weighted seeded selection, structured traces,
    direct trace replay, coverage counters, and traced final settling.
15. Add default smoke seeds, environment overrides, and the permanent regression
    list.
16. Update `.specs/001-global-invariant-checker.md` status and
    `docs/TESTING.md` accurately.
17. Run focused, full, syntax, and diff checks.

Do not begin with random operation selection. Each operation and oracle must pass
its focused test before it enters the generated grammar.

## Acceptance Criteria

This roadmap item is complete for the current version-1 model when all of the
following are true:

- The harness generates canonical updates, accepted and rejected user pushes,
  client refreshes, server sync, immutable view additions, worker execution,
  retries, worker crash states, source crash states, and recovery.
- Every generated action has explicit preconditions, expected outcome, model
  transition, and stable post-operation invariant check.
- `check_invariants` runs after every stable operation and retry sub-checkpoint.
- The independent model verifies every current canonical tree and every view's
  complete filtered tree without using production policy/projection helpers.
- Generated rejected pushes and blocked sync leave state bytes, authoritative
  refs, logical changes, and mappings unchanged.
- The same seed and step count replay to the same trace, refs, and settled state.
- Direct trace replay executes recorded recovery and epilogue actions without
  consulting random selection.
- Failure output includes the seed, requested step count, failed step, operation,
  and complete replay trace.
- Default seeds cover every required operation category.
- Crash/recovery authoritative output equals uninterrupted execution.
- Final settling leaves no source-unconfirmed transaction, running/ready/retry
  job, pending view, or state/ref divergence.
- Failing seeds are retained in `REGRESSION_SEEDS` and accompanied by focused
  tests when practical.
- Existing tests continue to pass.
- Documentation clearly states that v1 permission generation means adding
  immutable views, not changing or revoking an existing policy.
- Completed jobs retain no lease token, lease deadline, or retained plan.
- No production failpoint, process barrier, policy lifecycle, database, or
  network feature is introduced.

Run these commands from the repository root:

```bash
python3 -m unittest -v tests.test_invariants
python3 -m unittest -v tests.test_state_machine
python3 -m unittest discover -v tests 'test_*.py'
python3 -m py_compile forge.py tests/*.py src/*.py
git diff --check
```

Also run one non-default replay:

```bash
BASTION_STATE_MACHINE_SEEDS=0x1234 BASTION_STATE_MACHINE_STEPS=40 \
python3 -m unittest -v tests.test_state_machine
```

## Non-Goals

- Mutating an existing view's read or write policy.
- Policy versioning, revocation, expansion, contraction, deny rules, teams,
  roles, or service-account authorization.
- Hypothesis integration or automatic shrinking.
- Real parallel processes or synchronization barriers.
- Named failpoints or process termination.
- Exhaustive tree-operation, path-encoding, mode, symlink, rename, or conflict
  matrices.
- Exhaustive rejection immutability across every unsupported operation.
- Concurrent invariant snapshots during publication.
- Checker repair behavior.
- New CLI commands.
- PostgreSQL, network Git transport, object-maintenance compatibility, forge
  product features, or scale benchmarks.
- Merge support, force pushes, ref creation/deletion, or any item under
  `ROADMAP.md`'s deferred section.
