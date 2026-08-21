# Testing And Local Operation

## Automated Suite

Run the complete suite from the repository root:

```bash
python3 -m unittest discover -v tests 'test_*.py'
```

The current suite contains focused unit tests and black-box tests using real
Git repositories, hooks, clones, fetches, rebases, and pushes.

| Test module | Coverage |
|---|---|
| `tests/test_validation.py` | Prefix matching, authorization, modes, tree effects, refs, and `Change-Id` parsing |
| `tests/test_git_adapter.py` | Git environment isolation |
| `tests/test_tree_operations.py` | Directed source and administrative delete, rename, mode, symlink, directory-boundary, and rejection behavior |
| `tests/test_unusual_paths.py` | Raw non-UTF-8, tab, newline, near-component-limit, and over-4096-byte path lifecycles |
| `tests/test_state.py` | Mapping idempotency, frontier monotonicity, CAS abort, and receiver identity |
| `tests/test_outbox.py` | Idempotent dispatch, per-view ordering, leases, and terminal states |
| `tests/test_worker.py` | Claim/build/complete, ref adoption, retries, stale plans, and receive races |
| `tests/test_service.py` | Initialization and canonical update validation |
| `tests/test_forge.py` | Single-view ordinary Git behavior and security checks |
| `tests/test_cross_view.py` | Overlapping views, fanout, concurrency barriers, recovery, and logical identity |
| `tests/test_invariants.py` | Read-only global persisted-state and Git-history checks |
| `tests/test_state_machine.py` | Deterministic real-Git state-machine, replay, crash/recovery, and independent tree-model checks |
| `tests/test_failpoints.py` | Inert-by-default checkpoint primitive and marker identity |
| `tests/test_process_concurrency.py` | Real-process competing push, worker, and receive/worker barrier schedules |
| `tests/test_process_failpoints.py` | Source, administrative, worker, projection, recovery, and state-file crash matrix |
| `tests/test_shared_conflicts.py` | Shared-visible modify/modify, delete/modify, and mode/content conflict resolution |
| `tests/test_rejection_immutability.py` | Systematic verification that rejected operations leave authoritative state unchanged |
| `tests/test_http_unit.py` | HTTP route grammar, credential parsing, gzip bodies, and token management |
| `tests/test_http_server.py` | Clone, push, fanout, protocol v2, and shallow clones over loopback HTTP |
| `tests/test_http_security.py` | Authentication, cross-view isolation, traversal probes, hidden-object wants, and state immutability over HTTP |

Also run syntax and repository checks:

```bash
python3 -m py_compile forge.py tests/*.py src/*.py
git diff --check
```

## Deterministic State Machine

`tests/test_state_machine.py` runs two fixed smoke seeds (`0x5eed` and
`0xc0ffee`) with 24 generated steps. Each step records an automatic recovery
and outbox-dispatch checkpoint followed by a selected operation. The harness
uses a separate byte-level tree model and Git plumbing reader, then calls the
global invariant checker after every stable checkpoint.

Replay one or more seeds, optionally with a longer run, without editing tests:

```bash
BASTION_STATE_MACHINE_SEEDS=0xc0ffee \
BASTION_STATE_MACHINE_STEPS=80 \
python3 -m unittest -v tests.test_state_machine
```

Seeds accept Python integer syntax, including hexadecimal. Invalid seed lists
or non-positive step counts fail before creating a fixture. An explicit seed or
step override is intended for replay and does not enforce aggregate smoke
coverage.

On failure, the test prints a replayable structured trace:

```text
state-machine failure
seed: 12648430
steps: 24
failed step: 7
operation: authorized_push {"...": "..."}
trace:
0000 recover_dispatch {"before_step": 0}
0001 admin_edit {"...": "..."}
```

`tests/state_machine.py` also exposes `replay_scenario(seed, operations)` for
direct execution of a recorded trace without selecting new random operations.
The version-1 permission-change operation only adds one of the fixed immutable
view descriptors. It does not mutate, revoke, expand, or contract any existing
view policy, so it is not revocation coverage.

## Demonstrated Behavior

The tests currently demonstrate:

- exact source commit continuity across repeated pushes;
- hidden-only canonical advancement without projected ref movement;
- visible-stale rejection and ordinary fetch/rebase recovery;
- complete multi-commit rejection when a later commit is unauthorized;
- branch, force, and merge restrictions;
- hidden object absence from projected object databases;
- hidden file/directory collision preservation;
- overlapping Alice/Bob views with bidirectional writes;
- synthetic cross-view commits linked by `Change-Id`;
- cross-view source confirmation barriers;
- ordered event fanout and independent-view retry isolation;
- process crash states around canonical and projected ref publication;
- administrative canonical update validation;
- canonical merge and non-fast-forward rejection.
- exact tree snapshots for deletes, renames, mode-only transitions, symlink targets, and file/directory replacement;
- byte-preserving source and administrative projection of non-UTF-8, tab, newline, and long paths;
- OID-only closure validation, including quarantined extra-object rejection;
- shared-visible modify/modify, delete/modify, and mode/content conflicts serialized through fetch/rebase;
- rejection immutability for force pushes, stale updates, merge commits, non-linear history, unauthorized paths, unsupported entries, structural conflicts, duplicate `Change-Id` ownership, administrative validation failures, and sync barriers.

The directed tree modules use `tests/tree_harness.py`, an independent binary Git
plumbing helper. It reads `ls-tree -rz` records and writes temporary-index trees
without decoding raw pathnames or materializing deep logical paths in a worktree.
They complement, rather than expand, the generated state-machine and process
crash matrices.

Passing tests are evidence for the constrained model, not proof of production
security.

## Real-Process Concurrency And Failpoint Tests

These tests require Linux because they depend on `fcntl.flock`, `/proc/<pid>/stat`,
`SIGSTOP`, `SIGCONT`, `SIGKILL`, and local Git receive-pack hooks.  They are
skipped automatically on other platforms, but the full Phase 1 gate requires them
to pass on the reference Linux development environment.

Run the focused process suite:

```bash
python3 -m unittest -v \
  tests.test_process_concurrency \
  tests.test_process_failpoints
```

The harness launches ordinary `git push` processes, `forge.py` administrative
updates, and `tests/process_actor.py` workers as real child processes.  A named
checkpoint writes a versioned JSON marker and stops its own process with
`SIGSTOP`.  The test controller then either sends `SIGCONT` to exercise a barrier
or `SIGKILL` to inject a process crash.  Checkpoint markers are written under the
test control directory, never inside the forge root, and the checkpoint
environment is stripped from the stopped process so a single matched point cannot
fire twice.

Do not set `BASTION_TEST_FAILPOINT*` variables in normal operation.  They are a
test-only seam and must be stripped by any future HTTP/SSH server before invoking
forge code or Git hooks.

### Semantic equivalence rules

Crash recovery is not required to produce byte-identical `state.json`.  The
process harness compares authoritative snapshots that ignore:

- absolute transaction sequence numbers and `next_transaction_sequence` when a
  killed prepared attempt consumed one;
- retained `aborted` transaction records;
- worker `attempts`, expired lease tokens, retry deadlines, and historical
  `last_error` fields;
- unreachable Git objects;
- PIDs, process start identities, marker files, and temporary root paths.

They must match exactly on published refs, histories, trees, frontiers, anchors,
logical changes, accepted source mappings, and terminal job/event states.

## Local Manual Setup

Create a source repository, then initialize a forge:

```bash
python3 forge.py init /tmp/bastion-forge /path/to/source --branch main
```

Create overlapping views:

```bash
python3 forge.py add-view /tmp/bastion-forge alice \
  --read shared --read team-a \
  --write shared --write team-a

python3 forge.py add-view /tmp/bastion-forge bob \
  --read shared --read team-b \
  --write shared --write team-b
```

Clone through Git transport rather than local object linking:

```bash
git clone --no-local /tmp/bastion-forge/views/alice.git alice
git clone --no-local /tmp/bastion-forge/views/bob.git bob
```

Generate an optional review identity:

```bash
python3 forge.py new-change-id
```

Place it in the final commit trailer block:

```text
Change-Id: I0123456789abcdef0123456789abcdef01234567
```

Process pending projection jobs:

```bash
python3 forge.py work /tmp/bastion-forge
```

Limit a worker invocation to one ready job:

```bash
python3 forge.py work /tmp/bastion-forge --limit 1
```

Check all persisted state, refs, projected histories, and reachable view objects
without modifying the forge:

```bash
python3 forge.py check /tmp/bastion-forge
```

## Administrative Canonical Update

Do not push directly to canonical `main`. First place the new commit object in
the canonical object database through an internal staging ref, then call the
serialized coordinator with immutable full OIDs:

```bash
OLD=$(git --git-dir=/tmp/bastion-forge/canonical.git rev-parse refs/heads/main)
NEW=$(git -C /path/to/admin-clone rev-parse HEAD)

git -C /path/to/admin-clone push /tmp/bastion-forge/canonical.git \
  "$NEW:refs/staging/admin-update"

python3 forge.py update-canonical /tmp/bastion-forge "$OLD" "$NEW"
python3 forge.py work /tmp/bastion-forge
```

The coordinator rejects mutable revision names, stale expected OIDs, merges,
non-fast-forwards, empty subtrees, gitlinks, and unsupported modes.

## State Inspection

The local durable model is stored at:

```text
/tmp/bastion-forge/state.json
```

Useful sections are:

```text
views
transactions
changes
outbox
projection_jobs
```

Do not edit state during normal manual testing. Tests mutate it only to recreate
precise crash windows.

## Manual Test Priorities

Before broad interactive testing, add automated coverage from the first roadmap
phase. When manually testing the current model, focus on:

1. repeated normal push/fetch/rebase cycles across two views;
2. source retry after an intentionally failed ref publication;
3. stopping and restarting `work` between job claim and completion;
4. taking one view repository unavailable and restoring it;
5. checking `git fsck --full` and hidden object absence after each sequence.

Record exact commands, Git version, state transitions, canonical and projected
OIDs, and any error output.
