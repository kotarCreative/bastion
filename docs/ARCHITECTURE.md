# Architecture

## Purpose

Bastion tests whether one canonical Git repository can safely support multiple
writable repositories with different path visibility while clients continue to
use ordinary Git.

The architecture separates three identities:

| Identity | Meaning |
|---|---|
| Git object ID | Exact immutable object and ancestry within one repository view |
| Canonical commit ID | Complete repository state, including hidden paths |
| `Change-Id` | Logical review identity shared by related Git representations |

A `Change-Id` does not affect Git ancestry or authorization. Git object IDs
remain authoritative for snapshots and fast-forward behavior.

## Repository Model

```text
                          canonical.git
                     complete trees and history
                               |
                  deterministic path projection
                               |
              +----------------+----------------+
              |                                 |
        views/alice.git                    views/bob.git
       shared/ + team-a/                  shared/ + team-b/
              ^                                 ^
              | fetch only                      | fetch only
       intake/alice.git                   intake/bob.git
         authenticated push target (quarantined)
              |                                 |
        ordinary Git client                ordinary Git client
```

Each view is a valid bare Git repository with its own refs and authorization-
safe object closure. It has no alternate pointing to the canonical object
database. Every view has a sibling intake repository — the only push target.
Intake objects start in Git's receive quarantine; rejected pushes are
discarded without touching published storage, and accepted pushes migrate
into canonical plus the submitting view through the validated pipeline. The
intake store reads from its sibling view's objects via an alternates entry
(never canonical), so advertised tips resolve and thin-pack fixup finds
bases among already-visible objects. View repositories carry an
unconditional reject-all pre-receive hook: pushes are refused everywhere
except the authenticated intake boundary. The intake ref mirrors the
authoritative projected tip as advertisement bookkeeping and is repaired
after every projection, confirmation, and recovery.

## Identity Model

The physical view name, the submitting principal, and the authorization policy
are three separate concepts:

| Concept | Meaning |
|---|---|
| Principal | Stable user or service-account identity; submits pushes, owns `Change-Id`s, and may declare author email addresses |
| Team | Immutable direct set of principals; grants nothing by itself |
| Role | Immutable capability maps of path-scoped allow and deny prefix grants |
| Role binding | Immutable assignment of one role to a principal or team |
| Effective view descriptor | Content-addressed snapshot of provenance plus per-capability allow and deny areas |
| Physical view | One bare repository bound to exactly one principal and descriptor generation |

Resolution is deterministic: direct teams, applicable bindings, and roles sort
lexicographically; role grants union additively per capability, separately for
allow and deny, each side minimized. A descriptor's ID is the SHA-256 of its
canonical JSON payload under a fixed domain separator. Two views of one
principal share one descriptor.

Every authorization-sensitive record (source and administrative transactions,
outbox events, projection jobs, retained plans, anchors, logical revisions)
carries `descriptor_id` and `descriptor_generation` fields captured before any
canonical compare-and-swap. Publication fails closed with
`projection descriptor changed` on any mismatch. The catalog is unfrozen:
records are immutable and may be added at any time, but their effect stays
dormant until a quiescent `transition-principal` policy transition repoints the
principal's views to a fresh descriptor generation and publishes
policy-adjustment commits.

## Projection Rules

Projection filters complete canonical snapshots by readable path prefixes.
Boundary trees are synthetic; permitted blobs retain their normal content IDs.

For each canonical first-parent commit:

1. Construct the authorized tree.
2. Compare it with the current projected tree.
3. Elide the commit if the tree is unchanged.
4. Otherwise create a deterministic synthetic commit.

Synthetic commits contain:

- the authorized projected tree;
- the current projected parent;
- sanitized author and committer identities;
- a fixed non-secret timestamp;
- a generic message;
- an optional visible `Change-Id` trailer.

Canonical authors, messages, signatures, hidden paths, and source timestamps are
not copied into synthetic commits.

## Writable Views

Suppose Alice starts from projected commit `P1` and creates `A1`:

```text
Alice:      P1 -> A1
Canonical:  C1 -> hidden-H -> A1'
```

If canonical advancement since `C1` is invisible to Alice and reconciliation
produces Alice's exact projected tree, the source view publishes Alice's
original commit object:

```text
Alice remote: P1 -> A1
```

The generated canonical commit `A1'` necessarily has a different ID because it
contains the complete canonical tree and canonical parent.

For another view, the same canonical commit may be represented differently or
elided:

```text
Bob remote: B1 -> B2
```

`A1`, `A1'`, and `B2` can all map to the same logical `Change-Id`.

## Reconciliation

Only linear submitted chains rooted at the advertised projected tip are
accepted. For every submitted commit, Bastion:

1. Reads the projected parent and child trees.
2. Computes exact add, modify, delete, mode, and symlink effects.
3. Requires `read` plus the effect capabilities for every affected path under
   the view's bound descriptor, adjudicated by longest allow-versus-deny match.
4. Rejects unsupported modes, gitlinks, empty subtrees, and unsafe structural
   collisions.
5. Applies the authorized effects to the current complete canonical tree.
6. Creates a canonical commit in a transaction-specific object directory.
7. Reprojects that candidate and requires exact tree equality with the submitted
   commit.
8. Promotes candidate objects and compare-and-swap updates the canonical ref.

The submitted root tree is never installed as the canonical root. Hidden state
is retained by construction.

## Component Boundaries

The implementation is under `src/`.

| Module | Responsibility |
|---|---|
| `identity.py` | Identifier rules, immutable catalog, prefix algebra, descriptor resolution |
| `validation.py` | Pure path, ref, metadata, and tree-transition decisions |
| `git.py` | Git process, object, tree, commit, and quarantine adapter |
| `projection.py` | Tree filtering, hidden-only elision, and synthetic commits |
| `reconciliation.py` | Canonical transition replay and round-trip verification |
| `state.py` | Session leases, mappings, and forward-only recovery |
| `store.py` | SQLite/PostgreSQL document store: versions, checksums, leases, backups |
| `outbox.py` | Event dispatch, ordered jobs, leases, and completion rules |
| `worker.py` | Projection job claim, build, publish, failure, and retry |
| `invariants.py` | Read-only global persisted-state and Git-history verification |
| `service.py` | Forge, view, intake, synchronization, and canonical update use cases |
| `protocol.py` | Intake receive-pack orchestration, limits, and source publication |
| `http_server.py` | Authenticated smart-HTTP boundary with per-view tokens and limits |
| `forge.py` | CLI and compatibility facade |

Pure security decisions are kept separate from Git subprocesses. Expensive
projection runs outside the global state lock.

## Durable State Storage

Forge state lives in a real database, not a JSON file
(.specs/009-production-transaction-storage.md). Every aggregate — principals,
teams, roles, bindings, descriptors, views, transactions, logical changes,
outbox events, projection jobs — is one document row with an optimistic
`version` and a SHA-256 `checksum`. Two backends implement the identical
contract:

- `sqlite` (default): `store.sqlite3` in WAL mode inside the forge root;
- `postgres`: chosen at `init --database postgres://...`; each forge root gets
  its own database on the shared server.

Saves are short transactions with version-guarded upserts. Sessions replace
the old filesystem lock with lease rows in `storage_locks`: scope `global`
covers recovery-bearing operations (receives, administration), and
`view:<name>` scopes are available for partitioned workers. A lease records
its owner's PID identity, so crashed holders are stolen immediately instead
of waiting out the TTL. Loading verifies every checksum; tampered rows are
refused by name. Idempotency keys, an audit log, exponential retry schedules,
dead-lettering, heartbeats, cancellation, `stats`, backup/restore, and
legacy-JSON migration complete the production-storage surface.

## Current Restrictions

- SHA-1 repositories only
- one configured canonical branch
- linear canonical and submitted history
- one ref update per push
- no client-created merge commits
- no force pushes or ref deletion/creation
- no gitlinks/submodule updates
- readable and writable positive path prefixes only
- immutable generation-1 descriptors; no policy mutation or revocation yet
- local bare-repository transport and development identity model
- direct writes to `canonical.git` are unsupported

These restrictions are deliberate proof boundaries, not intended final product
behavior.
