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
| `tests/test_state.py` | Mapping idempotency, frontier monotonicity, CAS abort, and receiver identity |
| `tests/test_outbox.py` | Idempotent dispatch, per-view ordering, leases, and terminal states |
| `tests/test_worker.py` | Claim/build/complete, ref adoption, retries, stale plans, and receive races |
| `tests/test_service.py` | Initialization and canonical update validation |
| `tests/test_forge.py` | Single-view ordinary Git behavior and security checks |
| `tests/test_cross_view.py` | Overlapping views, fanout, concurrency barriers, recovery, and logical identity |

Also run syntax and repository checks:

```bash
python3 -m py_compile forge.py tests/*.py src/*.py
git diff --check
```

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

Passing tests are evidence for the constrained model, not proof of production
security.

## Local Manual Setup

Create a source repository, then initialize a forge:

```bash
python3 forge.py init /tmp/veil-forge /path/to/source --branch main
```

Create overlapping views:

```bash
python3 forge.py add-view /tmp/veil-forge alice \
  --read shared --read team-a \
  --write shared --write team-a

python3 forge.py add-view /tmp/veil-forge bob \
  --read shared --read team-b \
  --write shared --write team-b
```

Clone through Git transport rather than local object linking:

```bash
git clone --no-local /tmp/veil-forge/views/alice.git alice
git clone --no-local /tmp/veil-forge/views/bob.git bob
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
python3 forge.py work /tmp/veil-forge
```

Limit a worker invocation to one ready job:

```bash
python3 forge.py work /tmp/veil-forge --limit 1
```

## Administrative Canonical Update

Do not push directly to canonical `main`. First place the new commit object in
the canonical object database through an internal staging ref, then call the
serialized coordinator with immutable full OIDs:

```bash
OLD=$(git --git-dir=/tmp/veil-forge/canonical.git rev-parse refs/heads/main)
NEW=$(git -C /path/to/admin-clone rev-parse HEAD)

git -C /path/to/admin-clone push /tmp/veil-forge/canonical.git \
  "$NEW:refs/staging/admin-update"

python3 forge.py update-canonical /tmp/veil-forge "$OLD" "$NEW"
python3 forge.py work /tmp/veil-forge
```

The coordinator rejects mutable revision names, stale expected OIDs, merges,
non-fast-forwards, empty subtrees, gitlinks, and unsupported modes.

## State Inspection

The local durable model is stored at:

```text
/tmp/veil-forge/state.json
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
