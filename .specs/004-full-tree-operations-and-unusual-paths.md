# Full Tree-Operation And Unusual-Path Testing

## Status

Implemented.

This specification covers the next Phase 1 item not already covered by an
existing numbered specification:

> Add full tree-operation tests: delete, rename, mode, symlink, directory
> boundary, non-UTF-8 names, tabs, newlines, and long paths.

The global invariant checker, deterministic state-machine harness, and real-
process failpoint suite are already present. This work adds a directed semantic
matrix for Git tree behavior. It does not extend the random state machine or
repeat the process-crash matrix.

## Goal

Prove, with real Git objects and ordinary source pushes, that Bastion preserves
authorization and exact snapshot semantics across the complete set of tree
operations named above.

Every accepted source-push case must prove all of the following:

- the source view publishes the exact submitted commit;
- the canonical tree applies only the submitted path effects and preserves
  hidden state;
- another view receives the correct synthetic representation or validly elides
  the canonical transition;
- modes, symlink target bytes, and raw pathname bytes are preserved exactly;
- all persisted mappings, anchors, frontiers, refs, and reachable view objects
  pass `check_invariants`.

Accepted pure cases prove the transition rule directly. Accepted
administrative cases prove canonical coordination, per-view synthetic or elided
projection, and global invariants; they have no submitted source commit.

Every directed rejection in this specification must prove that canonical and
view refs plus persisted authoritative state are unchanged. This is focused
coverage for the tree-operation matrix, not completion of the later roadmap item
for every possible rejected operation.

## Why This Is Next

The coordination model now has deterministic, parallel-process, and crash-
recovery coverage. The remaining untested risk is that less common but valid Git
tree transitions pass through a pipeline that currently flattens trees, applies
path effects, reconstructs trees with an index, projects them into other object
databases, and independently verifies their reachable closure.

Most of that pipeline uses raw `bytes`, but the current code still has path-
bearing `rev-list --objects` calls that decode as UTF-8 or split on newlines.
Those calls can break a valid non-UTF-8 or newline pathname. The current final
file/directory conflict checks also inspect only adjacent sorted paths and can
miss an ancestor when a sibling sorts between the two conflicting names.

This initiative must fix those demonstrated blockers, then exercise the normal
source, canonical, projection, and invariant paths end to end.

## Current Model Decisions

These are version-1 behavior, not choices left to the implementer.

### Tree representation

Production represents a snapshot as a flat mapping:

```python
dict[bytes, tuple[bytes, bytes, str]]
```

The key is the complete raw path. The value is `(mode, kind, object_id)`.
Directories are structural and do not appear as leaf entries. Explicit empty
subtrees are unsupported, but the empty root tree is valid.

Tests must compare complete snapshots. Do not use porcelain rename detection,
diff similarity, or status text as an oracle.

### Delete and rename

- Delete is one changed path whose new entry is absent.
- Rename is delete plus add in the same commit.
- There is no rename metadata, similarity threshold, or rename-specific
  permission.
- Both rename endpoints must independently match current read and write policy.
- A content-changing or mode-changing rename is still only delete plus add.

### Modes

The only supported leaf modes are:

```text
100644 regular file
100755 executable regular file
120000 symbolic link
```

Mode is part of a tree entry. A mode-only transition is a real changed path even
when the blob object ID is unchanged. Gitlinks (`160000`), tree leaves, and all
other leaf modes are rejected.

### Symlinks

A symlink is a blob with mode `120000`. Its blob bytes are the target text.
Bastion does not resolve, normalize, validate, or dereference that target.
Authorization applies to the symlink's tree path, not to a path named by its
contents.

The suite must therefore treat relative targets, absolute targets, and targets
that textually name hidden paths as inert blob data. It must not introduce a
separate symlink capability; that belongs to Phase 2.

### File and directory boundaries

A valid Git snapshot cannot contain both a file `a` and a descendant `a/b`.
Replacing one with the other is accepted only when the old conflicting leaf or
leaves disappear in the submitted snapshot and all resulting changed paths are
authorized.

A hidden canonical file can conflict with a visible descendant that a source
view tries to create. That transition must reject rather than delete or reveal
the hidden file.

With only positive prefix policy, the opposite hidden-descendant case is not a
distinct black-box authorization configuration: permission for an ancestor path
also includes its descendants. Keep the pure transition test for both conflict
directions, but do not invent deny rules or mutable policy to force an impossible
version-1 view fixture.

### Path bytes

Git tree names are byte strings. A path component may contain any byte except
NUL and `/`; `/` separates tree components. Bastion must not normalize Unicode,
decode tree paths as UTF-8, or use newline-delimited path output as an authority.

All unusual-name cases in this work use an ordinary ASCII policy prefix such as
`shared`. This isolates tree-path handling from the separate question of how a
non-UTF-8 policy string is entered and persisted.

### Long paths

Version 1 defines no Bastion path-length limit. Resource and protocol limits are
Phase 4 work. This specification therefore requires:

- one component close to, but below, the common 255-byte filesystem component
  limit; and
- one valid Git logical path longer than 4096 bytes, with every component well
  below 255 bytes.

The over-4096-byte path must be built and inspected with Git plumbing. Do not
materialize it in a worktree, pass it as a command-line path argument, or skip it
based on the host's `PATH_MAX`.

## Scope

### In scope

- pure transition and prefix-boundary tests;
- a binary-safe test-only Git plumbing harness;
- accepted source deletes, renames, mode changes, symlink lifecycle changes,
  and file/directory replacements;
- coordinated administrative versions of every operation category;
- shared-visible fanout and view-private hidden-only elision;
- focused read-but-not-write and structural rejections;
- non-UTF-8, tab, newline, near-component-limit, and over-4096-byte paths;
- exact mode, blob, path, tree, ref, frontier, anchor, mapping, and closure
  assertions;
- production fixes directly required by these tests;
- documentation and roadmap status updates after implementation.

### Out of scope

- generated or randomized tree-operation sequences;
- adding these operations to `tests/state_machine.py`;
- repeating every operation at every failpoint or process barrier;
- shared-visible modify/modify, delete/modify, or mode/content races;
- a comprehensive rejection matrix outside the cases named here;
- new policy capabilities for create, delete, mode, symlink, or rename;
- policy mutation, deny rules, revocation, teams, roles, or service accounts;
- a product path-length limit or resource quotas;
- validation of every commit imported before `init_forge`;
- malformed arbitrary Git objects beyond the explicit empty-subtree and
  file/directory-conflict fixtures;
- merges, force pushes, multiple branches, tags, gitlinks as a supported
  feature, or SHA-256.

The next roadmap items for shared-visible conflicts and broad rejection
immutability remain open after this work.

## Required Deliverables

### New test support

- `tests/tree_harness.py`: binary Git command wrapper, raw tree reader,
  accepted-tree writer, deterministic commit builder, multi-view fixture,
  administrative staging helper, source push helper, authoritative snapshots,
  and stable checkpoint assertions. Its name deliberately must not match
  `test_*.py`.

### New test modules

- `tests/test_tree_operations.py`: pure and real-Git delete, rename, mode,
  symlink, file/directory, supported/unsupported-entry, and focused rejection
  matrix.
- `tests/test_unusual_paths.py`: non-UTF-8, tab, newline, near-component-limit,
  and over-4096-byte source/admin lifecycle tests.

### Production files expected to change

- `src/git.py`: enumerate submitted closure object IDs without requesting or
  decoding pathnames.
- `src/invariants.py`: enumerate reachable view object IDs without relying on a
  pathname selected by `rev-list`.
- `src/validation.py`: add complete flat-tree ancestor conflict detection and
  use it in `apply_changes`.
- `src/git.py`: use the same complete conflict predicate before `write_tree`.
- `tests/test_git_adapter.py`: directly prove OID-only closure enumeration still
  rejects an extra quarantined object.

Keep production changes limited to those general fixes unless a required test
demonstrates another defect. Any additional production change needs one focused
regression and a short explanation in the implementation report.

### Documentation expected to change

- `docs/TESTING.md`: list the two new modules, describe the byte-level plumbing
  profile, and add the demonstrated tree behaviors.
- `ROADMAP.md`: mark only the full tree-operation bullet done and add the proven
  tree/path behavior to the current baseline. Leave the status of every earlier
  bullet unchanged and do not mark conflict or broad rejection coverage done.

### Files not expected to change

- `src/reconciliation.py` and `src/projection.py`: their existing byte-keyed
  behavior should pass once closure enumeration and conflict checks are fixed.
- `src/protocol.py`, `src/state.py`, `src/outbox.py`, and `src/worker.py`: this
  work does not change transaction semantics.
- `forge.py`: no new user-facing command is required.
- `tests/state_machine.py` and `tests/test_state_machine.py`: keep the bounded
  regular-file generator unchanged.
- `tests/process_harness.py`, `tests/test_process_concurrency.py`, and
  `tests/test_process_failpoints.py`: do not expand the process matrix.
- the state schema version.

## Production Readiness Fixes

Complete these before adding the full black-box matrix. Each fix must retain the
existing security meaning, not merely suppress a failing assertion.

### 1. OID-only submitted closure enumeration

`verify_no_extra_objects` and `copy_submitted_closure` need object IDs, not
object names. Change both `rev-list` invocations to request no names:

```text
git rev-list --objects --no-object-names <revisions...>
```

Requirements:

- output contains only lowercase SHA-1 IDs and ASCII newlines;
- do not decode any submitted pathname;
- retain the current set relationship used to reject quarantined objects that
  are not reachable from the submitted tip;
- retain the current old-tip exclusion when copying newly submitted closure;
- do not use `errors="replace"` or `surrogateescape` to hide a framing problem;
- do not parse ordinary path-bearing `rev-list --objects` output by splitting
  on spaces or newlines.

The non-UTF-8 and newline source-push tests are the required end-to-end
regressions for this change.

Also add one direct regression in `tests/test_git_adapter.py` for the security
set relationship. Create a repository with a reachable commit and blob, create a
separate loose-object directory shaped like a receive quarantine, and invoke
`verify_no_extra_objects` with the same `GIT_OBJECT_DIRECTORY`,
`GIT_ALTERNATE_OBJECT_DIRECTORIES`, and `GIT_QUARANTINE_PATH` relationship used
by receive-pack:

1. Copy only a reachable object's loose file into the quarantine and require the
   check to pass.
2. Add one unrelated blob object to that quarantine and require
   `push contains objects unreachable from the submitted tip`.

Use loose, Git-produced object files rather than packfiles or fabricated object
filenames. This focused adapter test may import the production function
because it verifies that function's quarantine contract, not an independent
tree oracle.

### 2. OID-only invariant reachability

`_reachability` currently assumes `rev-list --objects` returns one raw pathname
per blob. That assumption is invalid for tabs/newlines/non-UTF-8 quoting and is
also unnecessary because one blob OID can legitimately occur at multiple
visible paths.

Change the closure enumeration to:

```text
git rev-list --objects --no-object-names <projected-tip>
```

Then validate by object ID:

- a reachable commit OID must be in the already-derived anchored projected
  commit set;
- a reachable tree OID must be in the already-derived projected snapshot tree
  set;
- a reachable blob OID must be in the set of blob OIDs derived from the
  already-validated projected leaves;
- any tag, gitlink target, or other object type remains a failure;
- every object must still be readable from the view's own object database;
- strict `git fsck` remains required.

This does not weaken path authorization. The preceding projected-history replay
already proves every `(path, mode, blob)` in every snapshot against policy.
Reachability then proves that no additional commit, tree, or blob object is
reachable. Document this split in a short code comment only if the set types are
not self-explanatory.

For this roadmap item, the required regression is that all unusual-path healthy
closures pass without path decoding. Expanding the checker specification's
incomplete corruption matrix for every object class is not part of this
initiative.

### 3. Complete file/directory conflict detection

The current immediate-predecessor check can miss this invalid flat set:

```python
{b"a", b"a-", b"a/b"}
```

`b"a-"` sorts between the ancestor and descendant. Replace adjacency-only
logic with a complete component-ancestor check.

A compliant implementation may expose one small pure helper in
`src/validation.py`, for example a function that returns or raises on the first
deterministically sorted conflict. For every path, inspect each slash-delimited
ancestor and test membership in the full path set. Do not use raw
`path.startswith(other)` without the `b"/"` component boundary.

Use the complete predicate in both places:

- `apply_changes`, with the existing
  `tree transition crosses a file/directory authorization boundary` category;
- `git.write_tree`, with the existing
  `tree transition has a file/directory conflict` category.

Keep the existing pre-application canonical conflict check because it supplies
the authorization-boundary diagnostic for a hidden old ancestor or descendant.
The final complete check protects the resulting flat snapshot, including
multiple newly added entries.

Required pure regressions:

- `a`, `a-`, and `a/b` reject regardless of input dictionary order;
- `a`, `a/b`, and `a0` reject;
- `a`, `a-`, and `ab/c` do not conflict;
- `visible` and `visible-secret/file` do not conflict;
- accepted file-to-directory and directory-to-file replacements still pass.

## Binary Test Harness Contract

The helper must remain independent of production tree, projection, and
authorization helpers. It may call public forge use cases to exercise the
system, but expected snapshots must come from test-side Git plumbing and simple
byte predicates.

### Command environment

Provide one binary subprocess wrapper accepting `str | bytes` arguments and
optional stdin bytes. It must:

- capture stdout and stderr as bytes;
- use no `text=True` decoding;
- remove Git repository, index, quarantine, namespace, replacement, graft,
  shallow, and config override variables using the same isolation categories as
  the state-machine harness;
- set `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL` to the null device,
  `GIT_NO_REPLACE_OBJECTS=1`, `LC_ALL=C`, and `LANG=C`;
- display arguments with `repr`/`os.fsdecode` and decode stderr with replacement
  only when formatting a failed test diagnostic;
- never decode a raw tree pathname.

Do not import `git.run`, `git.tree_entries`, `git.write_tree`,
`validation.allowed`, `projection.project_tree`, or invariant private helpers
to construct expected results.

### Independent tree reader

Use:

```text
git ls-tree -rz --full-tree <commit>
```

Parse NUL-delimited records as:

```text
mode SP type SP oid TAB raw-path NUL
```

For every leaf, require `type == blob` and read its exact bytes with
`git cat-file blob <oid>`. Return enough information to assert mode, object ID,
and contents, for example:

```python
Tree = dict[bytes, tuple[bytes, str, bytes]]
```

No expected-path lookup may use `git show COMMIT:path`, because that syntax
places the path inside a textual revision expression.

### Accepted-tree writer

Build valid test commits without materializing tree paths in a worktree:

1. For a non-root commit, read the parent into a test-side mapping of raw path
   to `(mode, contents)`. For the initial root commit, start from an empty
   mapping.
2. Apply the test's explicit delete/add/replace effects to that mapping.
3. Create a temporary index path and set `GIT_INDEX_FILE` in a copied clean
   environment. The index path must not exist before Git first opens it.
4. Run `git read-tree --empty`.
5. Write each blob with `git hash-object -w --stdin`.
6. Feed all entries through `git update-index -z --index-info` using records:

   ```text
   mode SP oid TAB raw-path NUL
   ```

7. Run `git write-tree`.
8. Run `git commit-tree <tree> -p <parent>` for a non-root commit, or omit `-p`
   for the initial root, with message bytes and fixed author and committer
   identity/timestamp.
9. Compare-and-swap local `refs/heads/main` from parent to the new commit before
   an ordinary source push. For the initial root, create the absent ref with
   `git update-ref refs/heads/main <new> <zero-oid>`.
10. Remove the temporary index in `finally`.

The helper accepts only valid test entries with modes `100644`, `100755`, and
`120000`. Keep explicit malformed/unsupported tree construction in separate,
narrow helpers so normal fixtures cannot accidentally bypass the valid-tree
contract.

### Empty and gitlink tree builders

Provide focused plumbing only for rejection fixtures:

- create the empty tree with `git mktree -z` and no records;
- insert that tree below a named parent tree with NUL-delimited `git mktree -z`
  input;
- use a real commit OID with mode `160000` and type `commit` for the required
  gitlink rejection.

Do not use malformed objects for successful cases. Do not publish a rejected
gitlink tree by directly moving canonical `main`.

Keep arbitrary unsupported blob modes such as `100664` in pure
`apply_changes` tests only. Git commonly canonicalizes that mode to `100644`
when reading or constructing a tree, so this specification does not require a
black-box fixture whose mode cannot be observed through the current Git adapter.

### Independent policy filter

Use a test-only byte predicate:

```text
allowed when prefix is empty, path equals prefix bytes,
or path starts with prefix bytes plus b"/"
```

All fixture policies in these modules are ASCII, so encode them as ASCII. The
filter must compare complete `(mode, oid, contents)` entries, not only path sets.

### Deterministic metadata

Use fixed identities and monotonically assigned integer timestamps. Messages
and optional `Change-Id` values must be constants or derive only from a fixture
counter. Do not use current time, random values, `forge.new_change_id`, or UUIDs.

Exact deterministic commit IDs are not required across different temporary
root paths, because roots are not commit inputs. Within one test, expected
source identity and synthetic determinism must be asserted exactly.

## Shared Fixture

Every normal test starts from a fresh temporary forge with one canonical branch
and this minimum canonical tree:

| Path | Mode | Contents/purpose |
|---|---:|---|
| `shared/base.txt` | `100644` | shared baseline |
| `shared/delete.txt` | `100644` | shared delete source |
| `shared/rename.txt` | `100644` | shared rename source |
| `shared/mode-up` | `100644` | mode-only transition |
| `shared/mode-down` | `100755` | reverse mode transition |
| `shared/link` | `120000` | `base.txt` |
| `shared/node` | `100644` | file/directory replacement source |
| `team-a/private.txt` | `100644` | Alice-private operations |
| `team-a/private-exec` | `100755` | private-to-shared rename source |
| `team-a/private-delete.txt` | `100644` | private delete source |
| `team-b/private.txt` | `100644` | Bob-private hidden state |
| `hidden/secret.txt` | `100644` | unique hidden bytes |

Create the initial commit with the same plumbing helper, then call
`forge.init_forge`. Add these immutable views:

| View | Read | Write | Purpose |
|---|---|---|---|
| `alice` | `shared`, `team-a` | `shared`, `team-a` | ordinary source |
| `bob` | `shared`, `team-b` | `shared`, `team-b` | shared fanout/private elision |
| `limited` | `shared`, `team-a` | `shared` | readable but non-writable endpoint rejection |

Clone each source view with `git clone --no-local`. Direct plumbing may move the
clone's local `main` without updating its worktree. After the helper starts doing
that, do not mix in worktree porcelain for the same clone.

Create one separate `--no-local` administrative clone of `canonical.git`. Before
each administrative chain, fetch the current canonical tip. Build the chain on
that immutable parent, then push only the final OID to a unique
`refs/staging/tree-<counter>` ref. Call:

```python
forge.update_canonical_ref(root, expected_oid, final_oid)
forge.drain_projection_jobs(root)
```

Never push administrative commits directly to canonical `main`.

## Stable Checkpoint Oracle

The fixture must expose one assertion used after every accepted push,
administrative update, and completed operation sequence.

At each checkpoint:

1. Run `check_invariants(root)`.
2. Load `state.json` only after the independent tree expectations have been
   computed.
3. Read canonical `refs/heads/main` and compare the complete raw-byte tree with
   the independently updated canonical model.
4. Require every untouched hidden entry, including mode, blob OID, and contents,
   to equal its prior value.
5. For every view, filter the canonical snapshot at that view's persisted
   frontier using the test-only policy predicate and compare it with the complete
   tree at the published view ref.
6. Require each actual view ref to equal its persisted `projected_tip`.
7. For an accepted source push, find the transaction by
   `(view, projected_old, projected_new)` and require each mapping's canonical
   tree to equal the expected per-commit model snapshot.
8. For an accepted source push, require the source view's published tip to equal
   the exact submitted tip and its anchor kind to be `submitted`.
9. For an accepted source push, when another view sees a changed filtered tree,
   require a new synthetic anchor for the same canonical commit and require its
   projected OID not to equal the submitted OID.
10. For an accepted source push, when another view's filtered tree is unchanged,
    require its projected tip not to move, its canonical frontier to advance,
    and no anchor for that hidden-only canonical commit.
11. For an administrative update, find the transaction by
    `(canonical_old, canonical_new)` and require no submitted mapping or
    submitted anchor for that range.
12. For each administrative commit and view, require a synthetic anchor when the
    filtered tree changes; otherwise require unchanged projected tip, advanced
    frontier, and no anchor for that commit.

Use commit-count assertions only where a matrix case explicitly expects one
representation per canonical transition. Tree equality, anchor identity, and
frontier movement are the primary oracle.

### Hidden-object assertion

The initial hidden secret must use contents not shared by any visible blob or
symlink target. Record its canonical blob OID. After symlink and unusual-path
tests, require `git cat-file -e <hidden-oid>` to fail in every view repository.

Do not classify an object as hidden only by pathname. A visible blob with the
same contents legitimately has the same object ID.

## Rejection Snapshot

Immediately before the action expected to reject, capture:

- exact `state.json` bytes;
- every ref name, target OID, and peeled object type from canonical and all view
  repositories;
- the canonical and every view configured branch OID separately for a concise
  failure diagnostic;
- receiver marker names.

For an administrative rejection, push the candidate to its staging ref first,
then take the snapshot. The staging ref and candidate objects are therefore
already part of the before state.

After rejection and process exit, require the snapshot to be identical and run
`check_invariants(root)`. Do not compare loose-object counts or require dangling
objects to disappear; rejected receives and staged administrative candidates may
leave unreachable objects.

For ordinary `git push`, require a nonzero return code and one stable Bastion
error category where specified below. Do not assert Git's complete transport or
ref-lock wording.

Every independent source rejection must start from the view's current
advertised tip. Prefer a fresh fixture and fresh `--no-local` clone. If a method
has intentionally advanced that view first, fetch and hard-reset or create a new
clone, then assert the local parent OID equals the current remote OID before
building the rejected commit. Never build one rejection on top of another
rejected local commit. Otherwise Git staleness can mask the authorization or
tree-validation category this matrix intends to exercise.

## Required Pure Matrix

Add focused tests before black-box scenarios.

### Change extraction and authorization

- A rename produces exactly `{old: None, new: entry}`.
- A mode-only `100644 -> 100755` transition is reported even when the blob OID
  is unchanged.
- A symlink target change is reported as one changed `120000` blob entry.
- Rename authorization passes only when both source and destination match both
  read and write lists.
- Delete, mode, and symlink changes on a readable but non-writable path reject
  with `submission changes a path outside current authorization`.

### Prefix and byte boundaries

For an ASCII `shared` prefix:

- `b"shared/nonutf-\xff"`, `b"shared/tab\tname"`, and
  `b"shared/line\nname"` are allowed;
- `b"shared-secret/file"` and `b"sharedness/file"` are not allowed;
- root policy still allows arbitrary non-NUL bytes;
- no test decodes these paths to make the authorization decision.

### Structural transitions

- File `shared/node` to `shared/node/child` passes when the old file is deleted.
- Two leaves below `shared/node/` to file `shared/node` pass when both leaves are
  deleted.
- Leaving either conflicting old entry rejects.
- Hidden old ancestor and hidden old descendant fixtures reject.
- The non-adjacent `a`, `a-`, `a/b` regression and non-conflicting sibling cases
  from the production-fix section pass.

### Entry types

- `100644 <-> 100755`, regular-file to symlink, symlink to regular-file, and
  symlink target replacement pass through `apply_changes`.
- New or affected gitlink entries reject.
- New arbitrary unsupported blob mode rejects.
- Deleting an affected unsupported old entry remains rejected under the current
  source model.

## Required Source-Push Matrix

Each numbered case uses ordinary `git push origin main` into an installed view
hook. Direct calls to `pre_receive` do not satisfy these cases.

### S1. Shared and private delete

1. Alice deletes `shared/delete.txt` and pushes one commit.
2. Require exact Alice source identity, canonical absence, and one Bob synthetic
   representation with the path absent.
3. Alice then deletes `team-a/private-delete.txt` in a second push.
4. Require exact Alice source identity and canonical absence.
5. Require Bob's projected tip to remain unchanged while its frontier advances.
6. In a fresh fixture, have a current `limited` client delete
   `team-a/private-delete.txt`. The path is readable but not writable, so the
   push must reject with the outside-authorization category and preserve the
   complete rejection snapshot.

### S2. Rename snapshot and visibility semantics

Use separate commits so every visibility transition has one clear anchor:

1. Rename `shared/rename.txt` to `shared/renamed.txt` without changing mode or
   blob. Bob sees delete plus add in one synthetic snapshot.
2. Rename an Alice-private executable into `shared/from-private`; Bob sees an
   add only, while the executable mode and blob OID are preserved.
3. Rename a shared symlink into `team-a/from-shared-link`; Bob sees a delete only,
   while Alice and canonical preserve mode `120000` and target bytes.

Assert the submitted/canonical tree changes directly. Do not invoke or inspect
`git diff --find-renames`.

### S3. Rename endpoint authorization

Using a fresh `limited` client for each direction:

- rename `team-a/private.txt` to `shared/private-moved`;
- rename a shared path to `team-a/shared-moved`.

The first has an unauthorized delete endpoint and the second an unauthorized add
endpoint. Both must reject with the outside-authorization category and preserve
the complete rejection snapshot.

### S4. Mode-only transitions

In one Alice commit:

- change `shared/mode-up` from `100644` to `100755`;
- change `shared/mode-down` from `100755` to `100644`;
- do not change either blob's bytes.

Require the blob OIDs to remain identical across the transition, the canonical
and Alice modes to match the submission, and Bob to receive the same mode-only
tree effect synthetically.

In a second Alice-private commit, change one `team-a` entry's executable bit and
require Bob to elide it. A `limited` mode change under `team-a` must reject and
leave the rejection snapshot unchanged.

### S5. Symlink lifecycle

Exercise these transitions in order, checking a stable checkpoint after each
push:

1. Create `shared/new-link` as mode `120000` with target bytes
   `../../hidden/secret.txt`.
2. Retarget it to absolute target bytes `/not/a/server/read`.
3. Replace the symlink with a `100644` regular file whose contents happen to be
   the previous target text.
4. Replace that regular file with a symlink again.
5. Rename the symlink while preserving mode and blob OID.
6. Delete the renamed symlink.

Alice and Bob must always contain only the submitted target bytes. The canonical
hidden secret must remain unchanged and its blob must remain absent from both
view object databases.

Also create or retarget one symlink below `team-a` and require Bob to elide the
transition. A `limited` symlink create below `team-a` must reject without state
or ref movement.

### S6. File-to-directory and directory-to-file replacement

1. Replace `shared/node` with two leaves:
   `shared/node/one` and `shared/node/two`.
2. Require the old file to be absent from all published snapshots and Bob to
   receive the exact two-leaf synthetic tree.
3. Replace both leaves with a single `shared/node` executable file.
4. Require both old leaves absent, mode `100755` preserved, and exact source and
   Bob representations.

### S7. Hidden ancestor collision

Use a dedicated fresh fixture whose canonical tree contains a hidden regular
file `container`, then add a view with read/write prefix `container/visible`.
Its initial projected tree is empty.

Submit a commit adding `container/visible/child`. It must reject with
`tree transition crosses a file/directory authorization boundary`. Require the
hidden canonical file's mode, OID, and contents plus the full rejection snapshot
to remain unchanged.

### S8. Explicit empty subtree

Create a submitted commit that adds an explicit empty tree below `shared/empty`
without another semantic leaf change. The source push must reject before any
transaction or ref publication with
`canonical round-trip does not reproduce the submitted tree`. Keep the
authoritative snapshot unchanged. Do not add earlier source validation or empty
directories as a supported feature in this initiative.

### S9. Unsupported entry

Submit one commit containing a gitlink at `shared/module`. It must reject with
the unsupported entry type/mode category and preserve the rejection snapshot.

An arbitrary unsupported blob-mode tuple remains required in the pure matrix.
It does not need a black-box source fixture because Git may canonicalize that
mode before Bastion can observe it.

## Required Administrative Matrix

Administrative cases must use a staging ref and the serialized coordinator.

### A1. Valid multi-operation chain

Build a linear chain from the current canonical tip with one commit per
transition:

1. shared delete;
2. shared regular-file rename;
3. shared mode-only change;
4. shared symlink create or retarget;
5. shared file-to-directory replacement;
6. shared directory-to-file replacement.

Push the final chain OID to one staging ref, call `update_canonical_ref` once,
and drain workers. `validate_canonical_update` must inspect every intermediate
commit. Alice and Bob must each receive the exact six visible projected
transitions, and the global checker must replay all six.

Then perform one Alice-private administrative transition. Alice must receive a
synthetic representation; Bob must retain its tip and advance only its frontier.

### A2. Unsupported gitlink

From a fresh valid parent, stage a candidate containing a gitlink. Take the
rejection snapshot after staging. `update_canonical_ref` must raise
`canonical update contains an unsupported tree entry`, leave canonical `main`
and all view refs unchanged, and create no transaction or job.

Do not substitute an arbitrary blob mode for this case. The mode-transition
matrix proves supported mode behavior, while the pure matrix proves that
`apply_changes` rejects an unsupported mode tuple.

### A3. Explicit empty subtree

Stage a canonical candidate containing `shared/empty` as an explicit empty tree.
The coordinator must reject with `explicit empty subtrees are not supported`
before creating a transaction or moving canonical `main`. The staged ref may
remain. The post-rejection forge must pass invariants.

## Required Unusual-Path Matrix

Define these raw path categories in one visible table in
`tests/test_unusual_paths.py`:

```python
NON_UTF8 = b"shared/nonutf-\xff.txt"
TAB = b"shared/tab\tname.txt"
NEWLINE = b"shared/line\nname.txt"
NEAR_COMPONENT_LIMIT = b"shared/" + b"c" * 240
DEEP = b"shared/" + b"/".join(<fixed ASCII components>) + b"/leaf.txt"
```

Construct `DEEP` so that:

```python
len(DEEP) > 4096
max(len(component) for component in DEEP.split(b"/")) < 200
```

Use a fixed number and fixed contents for the components. Assert both length
conditions before creating the fixture so accidental shortening cannot silently
weaken the test.

For every category define a distinct renamed target that retains the relevant
edge property: non-UTF-8 remains non-UTF-8, tab remains present, newline remains
present, the near-limit component remains at least 230 bytes, and the deep path
remains longer than 4096 bytes.

### U1. Ordinary source lifecycle

Using Alice and raw Git plumbing:

1. Build one five-commit submitted chain, adding one path category per commit.
2. Push the chain once through ordinary receive hooks.
3. Require five source mappings, exact submitted source tip, five canonical
   snapshots, and correct Bob synthetic trees.
4. Modify the contents at all five paths in one commit and push.
5. Rename all five paths in one commit and push.
6. Delete all five renamed paths in one commit and push.
7. Run the full stable checkpoint oracle after every push.

This sequence must complete without `UnicodeDecodeError`, a Python traceback,
path truncation, C-quoted path comparison, or invariant failure.

### U2. Private unusual names

Create at least a non-UTF-8, newline, and over-4096-byte path below `team-a` in
one Alice commit. Require canonical and Alice exact preservation while Bob's tip
is unchanged and its frontier advances. Delete them in a second source push and
require the same elision behavior.

### U3. Coordinated administrative unusual names

On a fresh fixture, build one administrative commit containing all five shared
path categories. Stage it, coordinate it, and drain workers. Then coordinate a
second commit deleting all five.

Alice and Bob must receive exact raw path/mode/content snapshots for both
transitions, and `check_invariants` must pass after each. This separately proves
that projection/invariant handling does not depend on the source quarantine
path.

### U4. Exact diagnostics and no text conversion

When a byte-path assertion fails, display the path with `repr(path)` or the
equivalent `repr(os.fsdecode(path))`. Do not place a raw non-UTF-8 path directly
in JSON, a unittest method name, or a decoded subprocess result.

No test may skip the non-UTF-8 or over-4096-byte case merely because its
worktree cannot represent the path. A failure in Git object plumbing is a real
test failure and must include the Git version and failed command.

## Focused Rejection Matrix

The cases below are mandatory and use the complete rejection snapshot:

| Case | Ingress | Expected category |
|---|---|---|
| Delete readable but non-writable path | source | outside current authorization |
| Rename with non-writable source | source | outside current authorization |
| Rename with non-writable destination | source | outside current authorization |
| Mode change on non-writable path | source | outside current authorization |
| Symlink create on non-writable path | source | outside current authorization |
| Hidden ancestor/file collision | source | file/directory authorization boundary |
| Explicit empty subtree | source | canonical round-trip does not reproduce submitted tree |
| Gitlink | source | unsupported entry type or mode |
| Gitlink | admin | unsupported canonical tree entry |
| Explicit empty subtree | admin | explicit empty subtrees unsupported |

For every row:

- canonical `main` does not move;
- no configured view ref moves;
- `state.json` bytes are unchanged;
- no mapping, anchor, logical change, event, or job is added;
- no stale receiver marker remains after the source process exits;
- `check_invariants` passes after rejection.

Do not claim that this table completes the later broad rejection roadmap item.
It covers only rejection counterparts required to trust this tree-operation
initiative.

## Test Organization

Use explicit, individually addressable `unittest` methods. The implementation
may combine closely related sequential transitions in one method when the
sequence itself is the behavior under test, such as symlink lifecycle or
unusual-path create/modify/rename/delete.

The minimum method-level inventory is:

`tests/test_tree_operations.py`:

- pure change/authorization semantics;
- pure complete structural-conflict regression;
- shared/private source delete;
- rename visibility and entry preservation;
- rename endpoint rejections;
- mode-only shared/private and rejection behavior;
- symlink lifecycle and hidden-object isolation;
- file/directory replacements;
- hidden ancestor rejection;
- source empty-subtree and unsupported-entry rejection;
- valid administrative operation chain;
- administrative unsupported-entry and empty-subtree rejection.

`tests/test_unusual_paths.py`:

- plumbing helper round-trip for every raw path category before Bastion is
  involved;
- ordinary source lifecycle for all categories;
- private-path elision for representative categories;
- coordinated administrative add/delete for all categories.

Use `subTest(path=repr(path))` only for pure helper checks whose cases are
independent. Do not put the entire source lifecycle in a subtest loop sharing a
forge after a prior case can fail halfway through.

## Runtime And Reliability Budget

- The two focused modules together should target less than 60 seconds on the
  reference Linux development machine.
- Do not add sleeps, retries, random seeds, or timing assumptions.
- Reuse one fresh fixture within sequential lifecycle methods, but never share a
  mutable forge between test methods.
- Build unusual paths through plumbing rather than thousands of filesystem
  operations.
- Do not hide required cases behind an environment variable or platform skip on
  Linux.
- Non-POSIX platforms may skip only the raw non-UTF-8 filename case if Python/Git
  cannot pass arbitrary byte arguments, but the project's Phase 1 gate requires
  the complete matrix on the reference Linux environment.

## Ordered Implementation Plan

Implement in this order. Do not start by writing the complete black-box matrix
against known-broken closure parsing.

1. Add `tests/tree_harness.py` with the clean binary command wrapper, raw tree
   reader, accepted-tree writer, deterministic commit builder, and a focused
   local Git round-trip for every unusual path including `DEEP`.
2. Change submitted closure enumeration in `src/git.py` to OID-only
   `rev-list`. Add the direct reachable/extra quarantine-object adapter test and
   a focused ordinary source push containing non-UTF-8 and newline names; prove
   the hook emits no traceback.
3. Change invariant reachability in `src/invariants.py` to OID-only closure
   checking. Prove a healthy projected history with newline/non-UTF-8 names
   passes and run the existing invariant corruption tests unchanged.
4. Add the complete flat-tree conflict predicate in `src/validation.py`, use it
   from `apply_changes` and `git.write_tree`, and add the pure non-adjacent
   ancestor regressions.
5. Build the deterministic canonical fixture, three views, source clones, admin
   clone, test-only policy filter, hidden-object oracle, rejection snapshot, and
   stable checkpoint assertion.
6. Implement source delete and rename success/rejection cases. Verify both
   source mappings and Bob's synthetic/elided behavior before proceeding.
7. Implement mode-only and symlink lifecycle cases, including unchanged blob
   identity for mode-only transitions and hidden blob absence for symlink
   targets.
8. Implement valid file/directory replacements, hidden-ancestor rejection, and
   explicit empty-subtree source rejection.
9. Implement the valid administrative multi-operation chain and focused admin
   rejection cases using staging refs only.
10. Complete the shared unusual-path create/modify/rename/delete source
    lifecycle, then private elision and administrative unusual-path cases.
11. Run the focused modules, invariant tests, forge/cross-view tests, state
    machine, and process tests before the full suite.
12. Update `docs/TESTING.md` and mark only this roadmap bullet done.
13. Run all acceptance commands and record versions, runtimes, and test counts.

When a step exposes another production defect, first reduce it to one focused
test. Do not weaken a pathname, omit invariant checking, or switch from ordinary
push to a direct API merely to make the matrix pass.

## Acceptance Criteria

This roadmap item is complete when all of the following are true:

- [ ] Delete is proven through accepted shared and private source pushes and a
      coordinated administrative transition.
- [ ] Rename is proven as delete plus add within one prefix and across
      shared/private visibility boundaries.
- [ ] Both rename endpoints are independently authorized, and either
      unauthorized endpoint rejects without authoritative movement.
- [ ] `100644 -> 100755` and `100755 -> 100644` mode-only transitions preserve
      the blob OID and project correctly.
- [ ] Symlink create, retarget, regular/symlink replacement, rename, and delete
      preserve target bytes without dereferencing hidden paths.
- [ ] File-to-directory and directory-to-file replacements preserve exact
      snapshot semantics.
- [ ] Hidden file/directory collisions reject without deleting, exposing, or
      modifying hidden state.
- [ ] Complete flat-tree conflict checking catches non-adjacent sorted ancestor
      collisions.
- [ ] Explicit empty subtrees and gitlinks reject before canonical or projected
      ref movement.
- [ ] Non-UTF-8, tab, newline, near-component-limit, and over-4096-byte paths
      survive source reconciliation, canonical construction, secondary
      projection, and global invariant checking byte-for-byte.
- [ ] The unusual-path lifecycle covers create, modify, rename, and delete.
- [ ] OID-only submitted closure enumeration retains extra-object security
      semantics without decoding pathnames.
- [ ] OID-only view reachability validates every required raw-path closure
      without requesting or decoding object names.
- [ ] Every accepted source case publishes the exact submitted source commit.
- [ ] Every shared-visible case produces the expected synthetic secondary
      representation.
- [ ] Every private case advances the unrelated view frontier without moving
      its projected tip.
- [ ] Every accepted and rejected stable checkpoint passes
      `check_invariants`.
- [ ] Every directed rejection preserves exact state bytes and all pre-existing
      canonical/view refs; administrative staging refs are captured before the
      rejection snapshot.
- [ ] No test relies on decoded path output, worktree support for deep paths,
      rename heuristics, random scheduling, or current wall-clock time.
- [ ] Existing state-machine and real-process suites continue to pass without
      expanding their operation grammars.
- [ ] Documentation accurately distinguishes this directed matrix from the
      still-open shared-conflict and broad rejection roadmap items.

## Test Commands

Run the pure and focused tree modules first:

```bash
python3 -m unittest -v \
  tests.test_validation \
  tests.test_tree_operations \
  tests.test_unusual_paths
```

Run the directly affected invariant and Git behavior modules:

```bash
python3 -m unittest -v \
  tests.test_git_adapter \
  tests.test_invariants \
  tests.test_forge \
  tests.test_cross_view
```

Run the existing generated and process gates:

```bash
python3 -m unittest -v tests.test_state_machine
python3 -m unittest -v \
  tests.test_process_concurrency \
  tests.test_process_failpoints
```

Run the complete suite and static checks:

```bash
python3 -m unittest discover -v tests 'test_*.py'
python3 -m py_compile forge.py tests/*.py src/*.py
git diff --check
```

The implementation report must include:

- operating system and kernel;
- Python version;
- Git version;
- focused tree-module runtime and test count;
- complete-suite runtime and test count;
- the exact byte length and maximum component length of `DEEP`.

## Exit Criteria

The initiative is complete only when valid Git tree operations and all required
raw path categories pass through the same ordinary source and coordinated admin
boundaries used by normal ASCII regular-file changes, while preserving exact
source identity, cross-view confidentiality, deterministic projection, and
global invariants.

Only after these criteria are met should Phase 1 proceed to shared-visible
modify/modify, delete/modify, and mode/content conflict tests, followed by the
systematic rejection-immutability matrix.
