# Global Invariant Checker

## Status

Ready for implementation.

This specification covers the first item in Phase 1 of `ROADMAP.md`:

> Add one global invariant checker covering every view, ref, frontier, anchor,
> representation, and reachable object.

It does not cover the later Phase 1 items for generated state machines,
parallel-process barriers, failpoints, expanded tree operations, conflict tests,
or systematic before/after checks for rejected operations.

## Goal

Add one read-only command that independently verifies that a forge's persisted
state, Git refs, projected histories, mappings, and reachable object closures
still satisfy the current security model.

The checker is intended to be called:

- after every completed operation in future state-machine tests;
- after recovery has reached a stable checkpoint in future crash tests;
- after manual test sequences;
- when diagnosing suspected state/ref divergence.

A healthy forge produces no output and exits successfully. A failed check names
the invariant category and affected record, view, ref, or object.

## Current Model Assumptions

The implementation must target the current version-1 prototype, not future
roadmap phases.

- One forge root represents one canonical repository.
- `state.json` has `version == 1`.
- There is one configured canonical branch.
- Canonical and projected histories are linear.
- Object IDs are full lowercase SHA-1 IDs.
- View policy consists of immutable positive read and write path prefixes.
- A view name is also its principal.
- Ref creation, deletion, force pushes, merge commits, gitlinks, and explicit
  empty subtrees are unsupported.
- Allowed leaf modes are `100644`, `100755`, and `120000`.
- Canonical refs and JSON state are separate durable authorities and may lag one
  another only in the transaction states explicitly described below.

Phase 2 will require changing historical checks to use versioned policy
descriptors. Do not add speculative policy-version compatibility in this work.

## Definitions

### Authoritative publication

- The canonical publication is the ref named by
  `refs/heads/<state.canonical_branch>` in `canonical.git`.
- A view publication is the ref named by `view.projected_ref` in that view's
  bare repository.
- Objects reachable from a view's published ref are client-visible and must be
  authorization-safe.
- Dangling objects are not publications. Existing transaction and projection
  code can safely leave unreachable objects after an abort or crash.

### Frontier

`view.canonical_frontier` is the latest canonical commit whose effect has been
examined for that view. It may advance without `view.projected_tip` moving when
all intervening canonical commits are hidden-only for that view.

### Anchor

Every commit in a persisted projected history has one anchor:

- `submitted`: the exact commit accepted from that same source view;
- `synthetic`: a deterministic representation generated for the view.

An anchor maps the projected commit ID to the canonical commit whose transition
it represents. A canonical commit can have no representation in a view when its
filtered tree is unchanged.

### Representation

`state.changes[change_id].revisions` is a secondary index over anchored
representations. It has no authorization or ancestry authority. The checker
must derive correctness from refs, trees, transactions, and anchors before it
checks this index.

### Stable checkpoint

Version 1 of the checker is observational and expects no process to be changing
the forge while it runs. A stable checkpoint can still contain:

- a `source_unconfirmed` transaction and blocked event;
- pending or retry projection jobs;
- a running job whose projected ref has not moved;
- a ready or retained projection plan whose projected ref has not moved;
- a projection failure and recorded retry state when the view repository remains
  readable.

The checker does not accept an unpersisted ref-publication window, such as a
projected ref already at `plan.new_tip` while the persisted view still names
`plan.old_tip`. Future failpoint tests must first run the existing recovery or
retry path to a stable checkpoint and then call this checker. Support for
checking concurrently mutating snapshots belongs with the later parallel-
process roadmap item.

## Public Interface

Add `src/invariants.py` with this public entry point:

```python
def check_invariants(root: Path) -> None:
    """Raise ForgeError if the forge violates a global invariant."""
```

Add the CLI command:

```text
python3 forge.py check FORGE_ROOT
```

CLI behavior:

- success: no stdout or stderr, exit code `0`;
- failure: `veil: <diagnostic>` on stderr, exit code `1` through the existing
  `ForgeError` handling;
- the command never repairs state, runs recovery, moves a ref, creates a Git
  object, dispatches an event, requeues a job, or changes a lease.

`check_invariants` may stop at the first violation. It must use the phase and
sort order in this specification so that the first diagnostic is deterministic.

## Diagnostics

Use this format for invariant failures:

```text
invariant <CODE> failed for <SUBJECT>: <DETAIL>
```

Use one of these stable codes:

| Code | Category |
|---|---|
| `STATE_SCHEMA` | JSON shape, field type, identifier, status, or sequence |
| `FORGE_LAYOUT` | Missing or orphan repository and unsafe storage mechanism |
| `CANONICAL_REF` | Configured canonical ref or coordinator continuity |
| `CANONICAL_GRAPH` | Canonical commit, tree, mode, type, or object integrity |
| `VIEW_REF` | View ref inventory, `HEAD`, or persisted-tip mismatch |
| `FRONTIER` | Invalid, regressed, or unexplained canonical frontier |
| `PROJECTION` | Projected sequence, tree, metadata, or elision mismatch |
| `ANCHOR` | Missing, extra, malformed, or incorrectly ordered anchor |
| `MAPPING` | Source transaction or reconciliation mismatch |
| `CHANGE` | Logical `Change-Id` ownership or revision mismatch |
| `OUTBOX` | Event identity, status, target, or transaction mismatch |
| `JOB` | Projection job identity, ordering, lease, plan, or pending mismatch |
| `REACHABILITY` | Corrupt, incomplete, unsupported, or unauthorized closure |

Sort map keys, view names, refs, sequences, paths, and object IDs before checking
them. Histories are always ordered root-to-tip by ancestry, never by object ID.
Display byte paths with `repr(os.fsdecode(path))` so a non-UTF-8 name never
crashes diagnostics.

## Required Validation Phases

Run the phases below in order. Validate shape before reading nested fields or
passing values to Git. Convert unexpected `KeyError`, `TypeError`, malformed Git
output, and failed Git reads into the phase's `ForgeError`; do not expose a
traceback for corrupt input.

### 1. Read-only snapshot and state schema

Acquire `state.lock` without invoking `recover`, then load and deep-copy
`state.json`. Hold the lock while collecting the canonical and view ref
inventories used by the check. Release it before expensive immutable-object
traversal.

`init_forge` must create the empty `state.lock` as part of a new forge so the
checker never needs to create it. Acquiring the lock is not a state mutation.

Require these exact top-level keys and types:

| Field | Required value/type |
|---|---|
| `version` | integer `1`, but not boolean |
| `canonical_branch` | non-empty string accepted by `git check-ref-format --branch` |
| `views` | dictionary |
| `transactions` | dictionary |
| `changes` | dictionary |
| `outbox` | dictionary |
| `projection_jobs` | dictionary |
| `next_transaction_sequence` | positive integer, but not boolean |

Before constructing a repository path or calling Git, require every dictionary
identifier to be a string and every nested view, transaction, change, event, and
job value to be a dictionary. Validate every view key with
`validate_view_name`. Also validate the core view field types needed by later
phases: principal/ref fields are strings, policy fields are lists, cursor fields
are strings, and anchors is a dictionary. This early validation prevents a
malformed view name from escaping `views/` through `view_repo`.

Every transaction, event, and job dictionary key must equal that record's `id`
field.

Persisted OID fields must be nonzero, full lowercase 40-character hexadecimal
strings. `ZERO_OID` is an in-memory replay sentinel in this checker, not a valid
version-1 persisted cursor, ref target, mapping, or transaction endpoint.

Phase 1 must validate required field presence and primitive/container types for
every nested record using the source, administrative, event, job, plan, anchor,
and revision schemas below. Later phases perform their semantic and graph
checks. In particular, Phase 5 must never read an unvalidated transaction and
Phase 9 must never read an unvalidated job or plan.

Transaction sequences must be positive integers and unique. Require:

```text
next_transaction_sequence > max(transaction.sequence)
```

Use `0` as the maximum when there are no transactions, so a newly initialized
forge with `next_transaction_sequence == 1` passes.
Do not require sequences to be gap-free; aborted work and future fixture
construction can consume a sequence.

Unknown top-level keys fail `STATE_SCHEMA` while `version == 1`. At nested
levels, require all fields listed in this specification and validate all known
optional fields, but do not reject an otherwise harmless unknown diagnostic
field. This avoids making the checker depend on incidental error metadata.

### 2. Forge and repository layout

Require:

- `canonical.git` is a readable bare SHA-1 repository;
- `views/` exists and is a directory;
- every state view has a readable bare SHA-1 repository at
  `views/<name>.git`;
- every `views/*.git` repository has a state view;
- no view uses `objects/info/alternates`;
- no view has a shallow boundary, graft file, or `refs/replace/*` ref.

A temporarily unavailable or renamed view is a failed check because the checker
cannot prove its closure. Retry-isolation tests may check unrelated behavior
while a view is unavailable, but must restore it before asserting global
invariants.

Do not inspect `work/`, temporary indexes, receiver markers,
`transaction-objects/`, quarantine directories, reflogs, or loose dangling
objects as authoritative state in this version.

### 3. Ref inventory

Enumerate refs with `git for-each-ref`, not `git show-ref`, so the checker has
the full ref name, target ID, and object type.

Canonical repository:

- the configured ref is exactly `refs/heads/<canonical_branch>`;
- it exists and resolves directly to a commit;
- every enumerated canonical ref resolves to an existing object;
- `refs/replace/*` is forbidden;
- additional canonical refs are allowed because `refs/staging/*`, imported
  source branches, and tags are object-intake/storage mechanisms;
- restrictions on linear history and tree entries apply to the configured
  canonical branch, not unrelated staging refs.

View repository, for every persisted view:

- `view.canonical_ref` and `view.projected_ref` both equal the configured
  `refs/heads/<canonical_branch>` in the current one-branch model;
- `HEAD` is symbolic and targets `view.projected_ref`;
- the only `for-each-ref` result is `view.projected_ref`;
- that ref resolves directly to a commit;
- the actual ref ID equals `view.projected_tip` at a stable checkpoint.

Any extra view branch, tag, note, staging ref, replace ref, or other advertised
ref fails `VIEW_REF`, even if its target is otherwise readable.

An initialized forge with no views is valid. Every persisted view must have a
nonzero frontier and projected tip because `add_view` performs and saves the
initial projection atomically from the state file's perspective. A zero-cursor
view is an incomplete `add_view`, not a valid persisted checkpoint.

### 4. Canonical configured history

Run the equivalent of:

```text
git fsck --full --strict --no-reflogs --no-dangling
```

against `canonical.git` with repository-local environment overrides removed and
replacement objects disabled. A nonzero result fails `CANONICAL_GRAPH`.

Walk every commit from the root through the configured canonical tip in
first-parent order. Require:

- each object is a commit;
- the root has no parent and every other commit has exactly the preceding
  canonical commit as its sole parent;
- every root tree is readable;
- no explicit empty subtree exists;
- every flattened leaf is a blob;
- every leaf mode is one of `100644`, `100755`, or `120000`;
- every referenced tree and blob exists and has the declared object type.

Use byte paths throughout. Do not decode tree names for comparisons.

### 5. View policy and frontier

For each state view, sorted by name, require:

- the view name passes `validate_view_name`;
- `principal == view_name`;
- `read` and `write` are lists of strings;
- each prefix equals `normalize_prefix(prefix)`;
- each list is sorted and duplicate-free;
- `read` is non-empty; `write` may be empty;
- `canonical_frontier` and `projected_tip` are valid nonzero OIDs;
- a nonzero frontier is a commit on the configured canonical first-parent
  history and is an ancestor of the current canonical tip.

Do not require write prefixes to be a subset of read prefixes. The current
authorization rule deliberately requires both checks at operation time, so a
write-only prefix is merely ineffective.

If a frontier is behind the current canonical tip, require durable work that
explains the lag:

- the view is in `pending_views` of a non-aborted transaction whose
  `canonical_new` is after the frontier; or
- a source transaction in `prepared`, `canonical_committed`, or
  `source_unconfirmed` forms the repository-wide publication barrier and its
  `canonical_new` is after the frontier.

The newest explaining transaction for the view must end at the current
canonical tip. This detects a direct or lost canonical advancement after the
last durable event. Hidden-only changes still require durable sequencing; they
may elide a projected commit but not the frontier update.

### 6. Source transactions and mappings

Source transactions omit `kind`. Administrative transactions use exactly
`kind == "canonical_update"`. Any other present `kind` value fails
`STATE_SCHEMA` rather than being interpreted as a source transaction.

Require these fields:

```text
id, view, principal, canonical_ref, projected_ref,
canonical_frontier_old, canonical_old, canonical_new,
projected_old, projected_new, mappings, sequence,
pending_views, status
```

Allowed source statuses are:

```text
prepared, canonical_committed, source_unconfirmed,
fanout_pending, published, aborted
```

Require:

- the referenced view exists;
- `principal == view == state.views[view].principal`;
- refs match that view's configured refs;
- `id == transaction_id(view, projected_old, projected_new)`;
- `mappings` is a non-empty list;
- pending views are unique existing views and never include the source view;
- `canonical_frontier_old` is an ancestor of `canonical_old`;
- each canonical commit from `canonical_frontier_old` through
  `canonical_old` filters to the exact `projected_old` tree; checking every
  intermediate commit prevents a visible change followed by a revert from
  being treated as hidden-only.

Validate the full submitted and canonical chains, not just their tips:

1. Start projected parent at `projected_old` and canonical parent at
   `canonical_old`.
2. For each mapping in list order, require exactly the fields `submitted`,
   `canonical`, and `change_id`.
3. Require the submitted commit's only parent to be the projected parent.
4. Require the canonical commit's only parent to be the canonical parent.
5. Compute `changed_entries(submitted_parent, submitted_child)`.
6. Run `authorize_changes` with the view's current read and write prefixes.
7. Run `apply_changes` against the canonical parent entries and require exact
   equality with the canonical child entries.
8. Filter the canonical child entries by the view's read prefixes and require
   exact equality with the submitted child entries.
9. Require canonical and submitted commit messages to be byte-identical.
10. Require `mapping.change_id == parse_change_id(submitted_message)`.
11. Advance both parents.
12. Require the final parents to equal `projected_new` and `canonical_new`.

Also validate the canonical commit metadata produced by reconciliation:

- author name, email, and timestamp equal the submitted commit author;
- committer is `Veil Forge <forge@localhost>`;
- committer timestamp equals the submitted author timestamp;
- there are no merge, signature, or replacement-object effects.

For `canonical_committed`, `source_unconfirmed`, `fanout_pending`, and
`published`, `canonical_new` must be on the configured canonical branch.
For `fanout_pending` and `published`, the source view's actual ref must contain
`projected_new`, and the source view's persisted anchors must include every
mapping. For `canonical_committed` and `source_unconfirmed` at a stable
checkpoint, the source ref and persisted view cursor remain at
`projected_old`/`canonical_frontier_old`.

`prepared` may have either `canonical_old` or `canonical_new` as the observed
canonical tip because the ref CAS and status save are separate boundaries. It
must not have an active outbox event yet. `aborted` candidates may remain as
dangling objects and have no active event or representation.

### 7. Administrative canonical transactions

Administrative transactions have `kind == "canonical_update"` and require:

```text
id, kind, sequence, canonical_ref, canonical_old,
canonical_new, pending_views, status
```

Allowed statuses are `prepared`, `fanout_pending`, `published`, and `aborted`.

Require:

- `id == f"canonical:{sequence}:{canonical_new}"`;
- the configured canonical ref is used;
- old and new IDs name commits in the canonical object database;
- old is an ancestor of new;
- every commit in `old..new` satisfies the canonical linear-history, mode,
  object-type, and no-empty-subtree rules;
- pending views are unique existing views;
- `fanout_pending` and `published` imply `canonical_new` is on the configured
  branch;
- `prepared` has no event; `aborted` has no active event or jobs.

Build the canonical transaction chain from these selected records:

- source transactions in `canonical_committed`, `source_unconfirmed`,
  `fanout_pending`, or `published`;
- administrative transactions in `fanout_pending` or `published`;
- a `prepared` source or administrative transaction only when the configured
  canonical ref is exactly its `canonical_new`, proving the ref CAS happened
  before status persistence.

Ignore aborted and uncommitted prepared records for this chain. Sort selected
records by sequence. The first record establishes the imported-history
baseline at its `canonical_old`. For every later selected record require:

```text
record.canonical_old == previous_record.canonical_new
```

Require every selected range to lie on the configured branch and require the
last selected `canonical_new` to equal the configured canonical tip. Exact
adjacency, rather than ancestry alone, detects an unrecorded direct canonical
advance between transactions.

If no records are selected, skip adjacency and tip-equality checks. The current
configured tip is then entirely within the imported-history provenance limit,
as it is for a newly initialized forge.

The checker cannot prove that commits before the first recorded transaction
passed through the coordinator because version-1 state does not persist the
initial canonical tip. Document this as a known provenance limit rather than
guessing from commit metadata.

### 8. Persisted projected history and anchors

For each view, walk the actual projected ref from root to
`view.projected_tip`. Require a linear chain. Then reconstruct the expected
representation sequence from the canonical root through
`view.canonical_frontier`.

Build a lookup from canonical commit to confirmed submitted mapping for this
view using source transactions in `fanout_pending` or `published`. A canonical
commit must have at most one submitted mapping for a given view.

Replay canonical commits in order with this algorithm:

```text
previous_projected = ZERO_OID
previous_visible_entries = no tree
expected_projected_commits = []

for canonical_commit through view.canonical_frontier:
    visible_entries = canonical entries allowed by view.read

    if canonical_commit has a confirmed submitted mapping for this view:
        expected = mapping.submitted
        expected kind = submitted
        append expected even if visible_entries did not change
    elif visible_entries != previous_visible_entries:
        expected = the next actual projected commit
        expected kind = synthetic
        append expected
    else:
        append nothing

    previous_visible_entries = visible_entries
```

The expected sequence must equal the complete actual projected commit sequence.
This catches missing anchors, extra commits, incorrect hidden-only elision, and
visible change/revert histories that happen to have the same final tree.

For every expected commit:

- there is exactly one `view.anchors[projected_oid]` record;
- there are no anchor keys outside the projected history;
- the anchor's canonical ID is the replayed canonical commit;
- the anchor kind is the replayed kind;
- the projected commit has `previous_projected` as its only parent, or no
  parent for the projected root;
- projected flat tree entries exactly equal the allowed canonical flat entries,
  including modes, object types, and blob IDs;
- the projected tree has no explicit empty subtree.

Submitted anchor shape is exactly:

```json
{"canonical": "<oid>", "kind": "submitted"}
```

It must correspond to exactly one source transaction mapping with the same
view, submitted ID, and canonical ID.

Synthetic anchor shape is:

```json
{
  "canonical": "<oid>",
  "kind": "synthetic",
  "change_id": "<string-or-null>"
}
```

For a synthetic commit require:

- `change_id == parse_change_id(canonical_commit_message)`;
- author and committer are exactly
  `Veil Projection <projection@localhost>`;
- both timestamps are exactly `946684800 +0000`;
- the message is exactly `projected canonical change\n`, followed only by
  `\nChange-Id: <id>\n` when an ID exists;
- no extra commit headers, signature, encoding field, or copied canonical
  metadata exists.

Compare the raw commit bytes to the expected bytes. This proves deterministic
synthetic identity without calling `synthetic_commit` or writing an object.

Current forge-generated commits use `git commit-tree`, so ambient system or
global Git configuration must not be able to add an `encoding` header or alter
their bytes. Update the Git adapter to isolate forge Git commands from system
and global config while continuing to read each repository's own config. Test
with a temporary hostile global config, including a non-UTF-8
`i18n.commitEncoding`, and require generated synthetic commits to retain the
exact format above.

Finally require:

```text
flat_tree(view.projected_tip)
==
filter(flat_tree(view.canonical_frontier), view.read)
```

This final equality is redundant with replay by design; keep it as a direct
check of hard invariant 6 from `docs/SECURITY.md`.

### 9. Projection plans

Every job with a `plan` field must have a structurally valid read-only plan,
even though its objects may not yet be reachable.

Require:

- job status is `running`, `ready`, or `retry`;
- plan view/ref fields match the job and state view;
- `old_frontier == job.expected_frontier`;
- `old_tip == job.expected_projected_tip`;
- `new_frontier == job.canonical_to`;
- old frontier/tip equal the persisted view cursor at this stable checkpoint;
- old frontier is an ancestor of new frontier on canonical history;
- all plan anchors are synthetic anchors;
- replaying generic projection from old frontier/tip to new frontier produces
  exactly the plan anchor mapping and `new_tip`; derive commit order from
  canonical replay and projected parent links, never dictionary insertion order;
- plan commit trees and raw synthetic metadata satisfy the same checks as
  persisted synthetic commits.

At the stable checkpoints accepted by this version, the actual view ref remains
at `old_tip`; a plan whose ref has already moved must be recovered or adopted
before calling the checker.

### 10. Logical changes and representations

Validate `changes` only after anchors and transaction mappings pass.

Derive the complete expected revision set:

- each synthetic anchor with a non-null `change_id` contributes one synthetic
  revision;
- each confirmed submitted mapping with a non-null `change_id` contributes one
  submitted revision.

Expected synthetic revision:

```json
{
  "view": "<view>",
  "projected": "<projected-oid>",
  "canonical": "<canonical-oid>",
  "kind": "synthetic"
}
```

Expected submitted revision adds:

```json
"submitted": "<same-as-projected-oid>"
```

Require:

- each `changes` key is the exact ID parsed from its source or canonical commit;
- `revisions` is a list with no duplicate dictionaries;
- actual revisions equal the derived set exactly, compared with the tuple key
  `(view, projected, canonical, kind, submitted-or-empty)`;
- every referenced view, projected commit, canonical commit, and anchor exists;
- `submitted` exists only for kind `submitted` and equals `projected`;
- kind is only `submitted` or `synthetic`.

An owned change has both `owner` and `canonical_ref`. Require the owner and ref
to agree with every source mapping in `canonical_committed`,
`source_unconfirmed`, `fanout_pending`, or `published` that uses that ID. Ignore
prepared and aborted mappings for ownership because they never reserve an ID.
An ownership reservation must be backed by at least one transaction in those
four active or confirmed statuses.

An ownerless change has neither `owner` nor `canonical_ref` and may contain only
synthetic revisions. This is valid for a `Change-Id` first observed in imported
or administratively added canonical history.

A source transaction in `canonical_committed` or `source_unconfirmed` may have
reserved ownership but no submitted revision yet. That revision becomes
required only after source publication is confirmed.

### 11. Outbox events

Allowed event statuses are `blocked`, `pending`, and `dispatched`.

For every event require:

- `id == event_id(transaction_id)`;
- exactly one event exists for its transaction;
- the referenced transaction exists;
- `type == "canonical.committed"`;
- sequence and `canonical_to` equal the transaction sequence and
  `canonical_new`;
- target views are unique, exist, and do not include a source transaction's
  source view;
- transaction `event_id` points back to the event;
- transaction `pending_views` is a subset of immutable
  `event.target_views`.

Status relationships:

| Transaction/event state | Required relationship |
|---|---|
| source `prepared` | no event |
| source `canonical_committed` or `source_unconfirmed` | one blocked event and no jobs |
| source `fanout_pending` or `published` | event pending or dispatched, never blocked |
| admin `prepared` | no event |
| admin `fanout_pending` or `published` | event pending or dispatched |
| any `aborted` transaction | no event or jobs |

A `pending` event has no jobs yet. A `dispatched` event has exactly one
deterministically named job for every target view and no additional jobs.
A blocked event must not expose any job.

`published` with no target views may still have a pending event immediately
after the administrative command; this is valid until the next dispatch pass.
A published event with one or more targets must already be dispatched and all
of its jobs must be completed.

### 12. Projection jobs and ordering

Allowed job statuses are `pending`, `running`, `ready`, `retry`, and
`completed`.

For every job require:

- `id == job_id(transaction_id, view)`;
- event and transaction exist and refer back to the same IDs;
- view exists and belongs to `event.target_views`;
- sequence and `canonical_to` equal the event and transaction values;
- `attempts` is a nonnegative integer;
- `last_error` is null or a string;
- `next_attempt_at` is numeric;
- copied expected cursors, when present, are valid OIDs.

Field-state rules:

| Status | Required operational fields |
|---|---|
| `pending` | attempts may be zero; no active token or plan |
| `running` | non-empty token, numeric expiration, expected frontier/tip; retained plan optional |
| `ready` | non-empty token, numeric expiration, expected frontier/tip, valid plan |
| `retry` | null token, retry time; retained plan optional |
| `completed` | token absent or null and no plan; old expected fields/expiration may remain |

Require at most one `running` or `ready` job per view. A higher-sequence job for
a view must not be running, ready, or completed while a lower-sequence job for
that view is incomplete.

Pending-view consistency is exact after dispatch:

- each incomplete target job's view is in `transaction.pending_views`;
- each completed target job's view is absent from `pending_views`;
- `fanout_pending` has at least one pending view;
- `published` has no pending views and all dispatched jobs are completed;
- any `view_errors` key is a target view with an incomplete job;
- completed jobs are at or behind the view's current frontier.

Do not require every retry job to have `view_errors`: lease expiry can create a
retry without recording an application error.

### 13. Reachable object closure

Run the same strict, no-reflog, no-dangling-output `git fsck` check against every
view repository with alternates and replacement objects disabled. A nonzero
result fails `REACHABILITY`.

Enumerate the complete object closure from every view ref. Because extra view
refs are already forbidden, this is the configured projected history closure.
Require:

- every reachable commit is in the validated anchored projected chain;
- every reachable tree belongs to one of those commit snapshots;
- every reachable leaf is an allowed-mode blob at a path authorized by
  `view.read` in the corresponding snapshot;
- every object can be read from the view's own object database with alternates
  disabled;
- no tag, gitlink, replacement, or unsupported object type is reachable.

Do not classify a blob ID as hidden merely because the same content ID appears
at a hidden canonical path. Git object identity is content-based, so a blob may
be legitimately present through an authorized path. Authorization is proved by
the path-filtered snapshot and anchored-history checks.

Do not fail solely because a well-formed object is physically present but
unreachable. Physical-object retention, garbage collection, and maintenance
confidentiality are Phase 5 work. The required boundary here is that no
unauthorized object is reachable or retrievable through a published view ref.

## Implementation Constraints

- Reuse read-only helpers such as `tree_entries`, `tree_oid`, `commit_message`,
  `validate_no_empty_subtrees`, `allowed`, `changed_entries`, `apply_changes`,
  `authorize_changes`, `parse_change_id`, `is_ancestor`, `event_id`, `job_id`,
  and `transaction_id` where their behavior matches this specification.
- Do not call `project_tree`, `project_history`, `synthetic_commit`,
  `build_projection_plan`, `publish_projection_plan`, `recover`,
  `dispatch_outbox`, or any worker state transition. They write objects, refs,
  or state.
- Filter flat canonical entries in memory with `allowed(path, prefixes)`.
- Parse raw commits in memory to inspect exact parent and metadata fields.
- Disable replacement-object interpretation while checking. Reject replace
  refs before trusting ordinary revision traversal.
- Ensure `clean_env` ignores system and global Git config, for example with
  `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL` set to the platform null
  device. Repository-local config must remain active.
- Do not use mutable revision expressions from state. Resolve only validated
  full OIDs and configured full ref names.
- Keep the checker in one module. Add small private helpers only for repeated
  parsing, phase failure, tree filtering, and history replay.
- Use only the Python standard library and the existing Git executable.

## Files To Change

| File | Change |
|---|---|
| `src/invariants.py` | Add the global checker and private read-only helpers |
| `src/git.py` | Isolate Git operations from ambient system/global config so generated commits remain deterministic |
| `src/service.py` | Create `state.lock` during forge initialization |
| `forge.py` | Add and dispatch `check ROOT` |
| `tests/test_invariants.py` | Add healthy-forge, valid-lag, corruption, CLI, and read-only tests |
| `docs/ARCHITECTURE.md` | Add `invariants.py` to the component table |
| `docs/TESTING.md` | Document the checker command and test module |
| `README.md` | Mention the checker beside the existing local commands |

Do not alter projection, reconciliation, transaction, or worker behavior merely
to make an invalid fixture pass. If a healthy fixture exposes a real mismatch
between this specification and current persisted behavior, update the spec or
fix the producer deliberately and add a regression test.

## Ordered Implementation Plan

Implement and verify one step at a time. Do not start with every check in one
large function.

1. Add a two-view real-Git fixture in `tests/test_invariants.py`. Create an
   initial canonical repository with shared, private, executable, and symlink
   entries; create overlapping views; submit one source change with a
   `Change-Id`; process fanout; assert the future checker passes.
2. Isolate Git commands from system/global config and add a regression test that
   creates projections under a hostile global commit-encoding setting.
3. Add `state.lock` initialization and the read-only checker entry point. Prove
   an empty forge and the healthy fixture pass without changing `state.json` or
   refs.
4. Implement state shape, identifier, OID, status, sequence, layout, and ref
   inventory checks. Add one corruption test per diagnostic category introduced.
5. Implement canonical `fsck`, linear history, tree mode/type, and empty-subtree
   checks.
6. Implement view policy, frontier ancestry, and durable-lag explanation checks.
7. Implement source and administrative transaction graph checks, including the
   full reconciliation replay for every source mapping.
8. Implement projected-history replay and bidirectional anchor checks. Add
   hidden-only and visible-change-then-revert fixtures to prove elision is
   checked per transition rather than only at the tip.
9. Implement exact raw synthetic commit checks and projection-plan checks.
10. Derive the expected `changes.revisions` index and compare it bidirectionally
   with persisted logical changes and ownership.
11. Implement outbox, job, pending-view, lease-field, and per-view ordering
    checks. Add valid pending, retry, source-unconfirmed, and viewless-event
    fixtures to prevent false positives.
12. Implement per-view `fsck` and complete reachable-closure checks. Corrupt a
    fixture by publishing an unauthorized commit under an extra ref and by
    making a hidden path reachable through the configured history.
13. Add the CLI command and assert success/failure output and exit codes.
14. Snapshot `state.json` bytes, all canonical/view refs, and
    `git count-objects -v` before and after both a passing and failing check.
    Require no change.
15. Update architecture, testing, and README documentation.
16. Run all acceptance commands.

## Required Test Matrix

### Healthy and valid in-progress cases

- Initialized forge with no views.
- One fully synchronized view.
- Two overlapping views after a submitted change and synthetic fanout.
- Canonical hidden-only change with an advanced frontier and unchanged
  projected tip.
- Multi-commit submitted source chain.
- Submitted no-op commit preserved exactly in the source view and elided in
  another view.
- Canonical staging ref present alongside the configured branch.
- Hostile system/global Git config cannot change generated commit bytes.
- Administrative event still pending dispatch.
- Pending job.
- Running job whose ref is still at the persisted tip.
- Retry job with and without a retained plan.
- `source_unconfirmed` transaction with blocked event and unchanged source ref.
- Ownerless synthetic-only `Change-Id`.
- Passing check with unrelated well-formed dangling objects left in canonical
  and view object stores.

### Required corruption cases

- Missing, malformed, or unsupported-version state.
- Duplicate transaction sequence or map key/record ID mismatch.
- Missing view repository and orphan `views/*.git` repository.
- View alternate, shallow file, graft, or replace ref.
- Missing canonical ref, canonical merge, unsupported mode, gitlink, explicit
  empty subtree, or corrupt reachable object.
- Unexpected view branch or tag.
- Actual projected ref not equal to persisted tip.
- Frontier not on canonical history or unexplained frontier lag.
- Projected tree not equal to the frontier filter.
- Missing, extra, wrong-kind, wrong-canonical, or out-of-order anchor.
- Missing synthetic commit for one side of a visible change/revert pair.
- Synthetic author, timestamp, message, parent, or `Change-Id` mismatch.
- Submitted chain, canonical chain, authorization, hidden-state preservation,
  round-trip tree, message, metadata, or mapping `Change-Id` mismatch.
- Revision missing from `changes`, extra revision, duplicate revision, wrong
  owner, or submitted field on a synthetic revision.
- Event with wrong deterministic ID, blocked event with a job, pending event
  with a job, or dispatched event missing a target job.
- Job with wrong target/sequence, invalid lease fields, invalid plan, completed
  job still pending, or higher-sequence completion before lower work.
- Reachable unanchored commit, unauthorized path, tag, gitlink, or missing local
  object.

Each corruption test should make one minimal mutation and assert the stable
diagnostic code plus the affected subject. Do not assert an entire Git-generated
error string.

## Acceptance Criteria

The roadmap item is complete when all of the following are true:

- `check_invariants(root)` examines every configured view, canonical/view ref,
  persisted frontier, projected commit, anchor, source mapping, logical
  representation, event, job, plan, and object reachable from a view ref.
- The checker independently replays every projected history from canonical
  history and current immutable policy; it does not trust tips or `Change-Id`
  indexes as proof.
- Valid hidden-only elision, exact source commit continuity, cross-view
  synthetic representations, pending work, retries, and source-unconfirmed
  barriers pass.
- Every corruption category in the required matrix fails with a deterministic
  code and subject.
- A successful or failed check leaves state bytes, refs, and object counts
  unchanged.
- `python3 forge.py check FORGE_ROOT` exits `0` silently for a healthy forge and
  exits `1` through the existing `veil: ...` error path for an unhealthy forge.
- All existing tests continue to pass.
- These commands pass:

```bash
python3 -m unittest discover -v tests 'test_*.py'
python3 -m py_compile forge.py tests/*.py src/*.py
git diff --check
```

## Non-goals

- Repairing state, moving refs, adopting a ready plan, or running recovery.
- Taking an atomic snapshot while another process publishes a source ref.
- Treating all physically present dangling objects as publications.
- Proving that rejected operations changed nothing; that needs before/after
  snapshots in the later roadmap item.
- Proving coordinator provenance for canonical commits that predate the first
  persisted transaction.
- Generating property/state-machine sequences or permanent seed regressions.
- Adding named failpoints or process termination tests.
- Expanding all tree-operation and conflict combinations.
- Versioned policy, revocation, deny rules, principals, teams, or roles.
- PostgreSQL, HTTP/SSH transport, maintenance compatibility, LFS, forge product
  features, or scale work.
- Merge support, force pushes, ref creation/deletion, or other deferred roadmap
  features.
