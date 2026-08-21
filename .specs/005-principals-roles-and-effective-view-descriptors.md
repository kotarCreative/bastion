# Principals, Roles, And Immutable Effective View Descriptors

## Status

Implemented for state schema version 2. Principals, service accounts, teams,
roles, bindings, content-addressed descriptors, descriptor capture across
transactions, events, jobs, plans, anchors, and revisions, principal-based
`Change-Id` ownership, the version-2 checker, and the quiescent migration are
present with focused pure, integration, and migration tests including process-
crash atomicity at both state-save failpoints.

This specification covered the first incomplete item in Phase 2 of
`ROADMAP.md`:

> Define principals, teams, roles, service accounts, and immutable effective
> view descriptors.

It establishes the identity and authorization-snapshot foundation needed by
the remaining Phase 2 items. It does not implement operation-specific
capabilities, deny rules, revocation, view expansion or contraction, stale
policy retention, or metadata permissions.

## Recommendation

Implement this item before any mutable permission behavior.

The current version-1 model uses one string as all of the following:

- the physical projected repository name;
- the submitting principal;
- the source endpoint used for fanout exclusion;
- the `Change-Id` owner;
- the job-ordering key.

It also stores raw `read` and `write` prefixes directly on the mutable view
record. Source validation, projection workers, recovery, and the invariant
checker all read those unversioned lists. Editing them in place would
reinterpret old submissions and old projected history, and a worker could
publish a plan built under different permissions from the ones currently
stored.

This initiative must first separate those identities and bind every
authorization-sensitive operation to one immutable descriptor. Later policy
transitions can then replace a view's descriptor pointer without mutating or
guessing the policy that authorized historical work.

## Goal

Add a version-2 state model in which:

- a principal is a stable identity and is either a user or service account;
- a team is an immutable direct set of principals;
- a role is an immutable set of the current coarse read/write prefix grants;
- an immutable role binding assigns one role to a principal or team;
- a deterministic effective view descriptor records the exact principal,
  branch, resolution provenance, and effective read/write policy;
- a physical view endpoint binds one principal to one descriptor generation;
- source authorization, projection, recovery, and invariant replay use the
  captured descriptor rather than live role resolution;
- `Change-Id` ownership is principal-based rather than endpoint-name-based;
- version-1 forges can be upgraded through one explicit, quiescent, state-only
  migration without moving refs or rewriting Git objects.

After this work, multiple endpoints may belong to one principal, and a service
account may own an endpoint independently of its human owner. Every current
view remains immutable: there is still no supported operation that changes its
descriptor or generation.

## Success Outcome

The item is complete when a source transaction can answer all of these
questions from durable state alone:

1. Which physical view received the push?
2. Which principal submitted it?
3. Was that principal a user or service account?
4. Which immutable descriptor authorized every submitted effect?
5. Which descriptor was captured for every secondary target before canonical
   compare-and-swap?
6. Which descriptor produced each submitted or synthetic representation?
7. Can the checker independently recompute and verify those descriptors?

No answer may depend on treating a view name as a principal or on reading a
role binding added after the transaction was prepared.

## Current Guarantees To Preserve

This work must preserve all version-1 Git and transaction behavior not
explicitly changed below:

- SHA-1 repositories and one configured canonical branch;
- linear canonical, submitted, and projected histories;
- exact submitting-view commit identity;
- hidden-only commit elision;
- deterministic synthetic commits with unchanged bytes and object IDs;
- current add/modify/delete/mode/symlink authorization semantics;
- complete-push validation before canonical ref movement;
- canonical compare-and-swap and source-confirmation barriers;
- ordered at-least-once projection jobs and forward-only ref adoption;
- path-byte preservation and unusual-path support;
- rejection immutability;
- isolated view object stores without canonical alternates.

The descriptor is authorization metadata. Do not add it to synthetic commit
messages, headers, timestamps, or tree contents. A behavior-preserving
migration must leave every canonical and projected Git object ID unchanged.

## Scope

### In scope

- state schema version 2;
- users and service accounts as principal kinds;
- immutable, direct-member teams;
- immutable roles containing current positive read/write prefix grants;
- immutable role bindings for principal and team subjects;
- deterministic role resolution and prefix canonicalization;
- content-addressed effective view descriptors;
- descriptor generation `1` on every current physical view;
- descriptor capture on source transactions, fanout targets, events, jobs,
  retained plans, anchors, and logical revisions;
- principal-based `Change-Id` ownership;
- multiple physical views for one principal;
- service-account source pushes and ownership;
- explicit identity/catalog/view CLI commands;
- dual version-1/version-2 read-only invariant checking;
- explicit quiescent version-1-to-version-2 migration;
- updates to all deterministic, process, tree, conflict, and rejection test
  fixtures so they exercise version 2.

### Out of scope

- changing a principal, team, role, binding, or existing view in place;
- deleting, renaming, disabling, or reusing an identity or policy ID;
- nested teams;
- role inheritance;
- allow/deny rules or precedence;
- separate create, modify, delete, mode, symlink, branch, merge, or admin
  capabilities;
- policy transitions, descriptor generation greater than `1`, revocation,
  expansion, or contraction;
- authentication, credentials, tokens, HTTP, or SSH;
- authorizing administrative canonical updates by role;
- metadata authorization;
- changing `Change-Id` branch scope, ownership transfer, or team ownership;
- changing projection ancestry or object-store handling;
- online migration while active receives or workers exist;
- replacing JSON state or the global filesystem lock.

The production projection benchmark's separate allow/deny model is not a
policy implementation and must not be imported into this work.

## Terminology And Required Decisions

### Principal

A principal is the only identity that may submit a source push or own a
`Change-Id`. It has a stable, case-sensitive ID and one kind:

```text
user
service_account
```

A service account is a principal, not a team and not a role. It has one owning
user principal for administrative provenance. The owner gains no permissions
from that relationship and does not own the service account's changes.

### Team

A team is an immutable, direct set of principal IDs. Users and service accounts
may both be members. A team cannot contain another team. Team membership grants
nothing by itself; it only makes team role bindings applicable.

### Role

For this item, a role contains only the existing coarse positive prefix grants:

```text
read
write
```

This is intentionally temporary. The next roadmap item will replace these two
coarse classes with operation-specific capabilities. Do not pre-implement that
capability model here.

### Role binding

A role binding immutably assigns one role to either:

- one principal; or
- one team.

Bindings are additive. There are no explicit denies. The same role reached by
multiple bindings contributes its grants once to the effective result, while
all contributing binding IDs remain in descriptor provenance.

### Physical view

A physical view is one bare repository under `views/<view-name>.git`, its one
projected ref, frontiers, anchors, and job-ordering stream. A view name remains
the stable endpoint and filesystem identity. It is not a principal ID.

One view has exactly one principal. One principal may have multiple views. A
view is never shared by multiple principals.

### Effective view descriptor

An effective view descriptor is an immutable, content-addressed authorization
snapshot. It includes both effective grants and the role/team/binding
provenance used to derive them. Two descriptors with identical path access but
different principals or provenance intentionally have different IDs.

The descriptor does not include the physical view name. Two views for the same
principal, branch, and resolution provenance therefore share one descriptor.

### Descriptor generation

Generation belongs to a physical view binding, not to a descriptor. Every view
created or migrated by this item has generation `1`.

The generation field exists as a compare-and-swap boundary for later policy
transitions. This item must reject any stored generation other than `1` and
must not expose an API to increment it.

## Identifier Rules

Principal, team, role, and view IDs use the existing simple identifier grammar:

```text
^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$
```

Rules:

- IDs are case-sensitive and are never case-folded;
- each namespace is independent, so a principal and role may share an ID;
- a view ID remains safe to use in a repository path;
- user-provided IDs containing path separators, whitespace, characters outside
  the grammar, or more than 64 characters reject before state changes;
- principal, team, role, binding, descriptor, transaction, event, and job map
  keys must equal each record's `id` field; the `views` map key is the physical
  view ID and its record intentionally has no duplicate `id` field;
- no ID may be renamed, deleted, or reused by this item.

Add generic identifier validation in the new identity module. Keep
`validate_view_name` as a view-specific wrapper or alias so existing path-safety
callers retain a precise diagnostic.

## Immutable Catalog Rules

Principal, team, role, and binding records are append-only. Creation of an
already-used ID rejects even if the requested record is byte-identical.

There are no update commands. A team receives its complete member list when it
is created. A role receives its complete grant lists when it is created.

Once any view exists for a principal, reject catalog additions that would alter
fresh resolution for that principal:

- creating a team containing that principal;
- creating a binding directly for that principal;
- creating a binding for a team containing that principal.

Use this stable error category:

```text
principal already has a materialized view; permission changes are not supported
```

Creating unrelated principals, teams, roles, or bindings remains allowed. A
principal may create additional views, but all of its views must resolve to the
same descriptor while this item is the current model.

These restrictions are deliberate. Later Phase 2 work will replace them with a
sequenced operation that creates a new immutable descriptor generation.

## Prefix And Resolution Algebra

### Role grant normalization

On role creation:

1. Run every raw prefix through `normalize_prefix`.
2. Sort and deduplicate the normalized strings.
3. Remove a descendant prefix when an already retained component ancestor
   covers it.
4. Treat the empty prefix as repository root and therefore as covering every
   other prefix.

Coverage uses path-component boundaries. `shared` covers `shared/a` but not
`shared-secret`.

A role must contain at least one read or write grant. A role may be read-only or
write-only because multiple roles compose before an effective descriptor is
validated.

### Applicable bindings

For principal `P`:

1. Select every immutable team whose member list contains `P`.
2. Select every binding whose subject is principal `P`.
3. Select every binding whose subject is one of those teams.
4. Sort team IDs, binding IDs, and resulting role IDs lexicographically.
5. Reject dangling references rather than ignoring them.

No recursive team or role traversal is permitted.

Descriptor provenance is intentionally complete rather than effect-minimized:

- `team_ids` contains every direct team containing `P`, including a team with
  no role binding;
- `binding_ids` contains every applicable direct or team binding, including a
  duplicate route to an already selected role;
- `role_ids` contains every distinct role referenced by those bindings,
  including a role whose grants contribute no final effective prefix.

Only the final `read` and `write` lists are effect-minimized.

### Effective read

Effective read is the minimized component-aware union of `read` grants from all
resolved roles.

The result must be non-empty before a view can be created. A principal and its
catalog records may exist without readable access, but `add-view` must fail with:

```text
principal has no effective readable path
```

### Effective write

Current source authorization requires both read and write access. Store only
the actual effective write area:

```text
effective_write = minimize(union(role.write) intersect effective_read)
```

Compute prefix intersections component-wise. For each read prefix `R` and write
prefix `W`:

- if `R` covers `W`, contribute `W`;
- if `W` covers `R`, contribute `R`;
- otherwise contribute nothing.

Then sort, deduplicate, and minimize the result. This preserves version-1
authorization even when a write grant is broader than a read grant while
removing misleading write-only space from the effective descriptor.

Examples:

| Role read union | Role write union | Effective write |
|---|---|---|
| `shared` | `shared` | `shared` |
| `shared/a` | `shared` | `shared/a` |
| root | `team-a`, `team-a/private` | `team-a` |
| `team-a` | `team-b` | empty |

Do not alter `allowed` path-byte behavior. The descriptor stores normalized
strings and existing authorization converts them with `os.fsencode`.

## Descriptor Schema And Identity

Use this descriptor shape:

```json
{
  "id": "vdesc-<64 lowercase hex characters>",
  "algorithm": "effective-view-v1",
  "principal_id": "alice",
  "principal_kind": "user",
  "canonical_ref": "refs/heads/main",
  "team_ids": ["engineering"],
  "role_ids": ["developer", "shared-reader"],
  "binding_ids": [
    "principal:alice:shared-reader",
    "team:engineering:developer"
  ],
  "read": ["shared", "team-a"],
  "write": ["shared", "team-a"]
}
```

The ID is computed from every field except `id`:

```python
payload = json.dumps(
    descriptor_without_id,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=True,
).encode("ascii")
descriptor_id = "vdesc-" + hashlib.sha256(
    b"bastion-effective-view-v1\0" + payload
).hexdigest()
```

The domain separator, JSON options, field names, algorithm string, and list
ordering are part of the stable contract. Do not use dictionary insertion
order, pretty-printed JSON, current time, random data, or a database sequence.

Descriptor construction must:

- verify the principal record and kind;
- recompute applicable teams and bindings;
- read immutable role definitions;
- apply the exact resolution algebra above;
- use the configured full canonical ref;
- return an existing byte-equal descriptor when the computed ID is already in
  `view_descriptors`;
- fail `STATE_SCHEMA`/`DESCRIPTOR` if an existing record with that ID has
  different content.

The hash is a deterministic identity, not a defense against an attacker who can
rewrite trusted `state.json`.

## Version-2 State Schema

### Top-level state

New forges must initialize this exact top-level shape:

```json
{
  "version": 2,
  "canonical_branch": "main",
  "principals": {},
  "teams": {},
  "roles": {},
  "role_bindings": {},
  "view_descriptors": {},
  "views": {},
  "transactions": {},
  "changes": {},
  "outbox": {},
  "projection_jobs": {},
  "next_transaction_sequence": 1
}
```

Unknown top-level fields fail the version-2 checker.

### Principal records

User:

```json
{"id": "alice", "kind": "user"}
```

Service account:

```json
{
  "id": "build-bot",
  "kind": "service_account",
  "owner_principal_id": "alice"
}
```

The owner must exist and have kind `user`. A user record must not contain
`owner_principal_id`.

### Team records

```json
{
  "id": "engineering",
  "members": ["alice", "build-bot"]
}
```

Members are sorted, unique principal IDs. An empty team is allowed. A team ID
in `members` is invalid unless a principal with the same string independently
exists; there is no nested-team interpretation.

### Role records

```json
{
  "id": "developer",
  "read": ["shared", "team-a"],
  "write": ["shared", "team-a"]
}
```

Lists must already be normalized, sorted, duplicate-free, and minimized.

### Binding records

Principal binding:

```json
{
  "id": "principal:alice:developer",
  "subject_kind": "principal",
  "subject_id": "alice",
  "role_id": "developer"
}
```

Team binding:

```json
{
  "id": "team:engineering:developer",
  "subject_kind": "team",
  "subject_id": "engineering",
  "role_id": "developer"
}
```

The deterministic binding ID is
`<subject_kind>:<subject_id>:<role_id>`. User IDs cannot contain `:`, so this is
unambiguous. The subject and role must exist.

### View records

Replace `principal`, `read`, and `write` with:

```json
{
  "principal_id": "alice",
  "descriptor_id": "vdesc-...",
  "descriptor_generation": 1,
  "canonical_ref": "refs/heads/main",
  "projected_ref": "refs/heads/main",
  "canonical_frontier": "<oid>",
  "projected_tip": "<oid>",
  "anchors": {}
}
```

The descriptor principal and canonical ref must equal the view fields. The
descriptor must equal a fresh resolution of the current immutable catalog for
that principal. All views for one principal must therefore have the same
descriptor ID in this version.

### Descriptor references

Represent a captured view assignment everywhere as these two fields:

```json
{
  "descriptor_id": "vdesc-...",
  "descriptor_generation": 1
}
```

Do not store a copied read/write list on transactions, jobs, or plans. Resolve
the immutable descriptor by ID and verify the generation against the physical
view.

### Source transactions

Replace `principal` with `principal_id` and add source descriptor fields:

```json
{
  "view": "alice-primary",
  "principal_id": "alice",
  "descriptor_id": "vdesc-...",
  "descriptor_generation": 1,
  "target_descriptors": {
    "bob": {
      "descriptor_id": "vdesc-...",
      "descriptor_generation": 1
    }
  }
}
```

All existing transaction fields remain. `target_descriptors` is captured in the
prepared transaction before canonical compare-and-swap. Its keys are every
physical target view except the source view. `pending_views` remains the
mutable subset that has not completed.

The source descriptor authorizes the complete submitted chain. It must never be
replaced with fresh role resolution during recovery or invariant replay.

Keep the existing transaction ID formula based on physical view plus old/new
Git IDs. Descriptor mutation is unsupported in this item, so it cannot collide
across generations. A later descriptor-transition specification must revisit
the ID before allowing generation changes.

### Administrative transactions

Administrative transactions also capture `target_descriptors` before canonical
compare-and-swap. The keys are every physical view existing when the transaction
is prepared. Do not add an acting administrator principal; admin authorization
is a later capability item.

### Events and jobs

An outbox event copies the transaction's complete `target_descriptors` map and
retains `target_views` as its sorted key list. Dispatch must create each job with
the matching descriptor ID and generation.

A projection job adds:

```json
{
  "descriptor_id": "vdesc-...",
  "descriptor_generation": 1
}
```

Job IDs and per-view ordering continue to use the physical view name. Do not
partition jobs by principal or descriptor, because two repositories for one
principal still have independent refs and histories.

### Projection plans

Every plan adds descriptor ID and generation. Plan construction uses that
descriptor's `read` list. Plan persistence, projected-ref compare-and-swap,
exact-tip adoption, and completion must each require the plan assignment to
equal the current view assignment. For a worker plan, they must additionally
require:

```text
plan descriptor == job descriptor == current view descriptor
plan generation == job generation == current view generation == 1
```

A plan built by initial view projection or manual `sync` has no job and checks
only against the current view. Every anchor in any plan already uses the final
descriptor-bearing anchor schema and must equal the plan assignment.

A mismatch must raise `projection descriptor changed` before ref movement. It
is state corruption in this item; future lifecycle work will define orderly
supersession rather than weakening this comparison.

### Anchors

Submitted anchor:

```json
{
  "canonical": "<oid>",
  "kind": "submitted",
  "descriptor_id": "vdesc-...",
  "descriptor_generation": 1
}
```

Synthetic anchor:

```json
{
  "canonical": "<oid>",
  "kind": "synthetic",
  "change_id": "<string-or-null>",
  "descriptor_id": "vdesc-...",
  "descriptor_generation": 1
}
```

Initial projection, source confirmation, worker completion, and ref adoption
must all record the descriptor that produced the representation.

### Logical changes and revisions

Rename owned change fields:

```text
owner -> owner_principal_id
```

Retain `canonical_ref` as the branch scope. Repository scope remains implicit in
the one-forge-root state file.

Every revision adds descriptor ID and generation copied from its matching
anchor. The revisions list remains a secondary index; the invariant checker
must derive expected values from validated anchors and source transactions.

Ownership rules:

- two views for the same principal may submit revisions of the same
  `Change-Id` on the same canonical ref;
- a different principal, including a service account owned by that user, may
  not claim it;
- a service account owns its changes under its own principal ID;
- teams and roles never own changes;
- an ownerless ID first introduced by canonical/admin history remains
  unclaimable, preserving current behavior;
- exact source-unconfirmed retry uses the original transaction principal and
  descriptor and additionally requires that the current view assignment still
  matches them. It does not rerun role resolution.

## Production API

Add `src/identity.py` as the only module that creates or resolves identity and
descriptor records. It should expose a small public surface equivalent to:

```python
def add_principal(state: dict, principal_id: str) -> None: ...

def add_service_account(
    state: dict, service_id: str, owner_principal_id: str
) -> None: ...

def add_team(state: dict, team_id: str, members: list[str]) -> None: ...

def add_role(
    state: dict, role_id: str, read: list[str], write: list[str]
) -> None: ...

def bind_role(
    state: dict,
    role_id: str,
    subject_kind: str,
    subject_id: str,
) -> str: ...

def effective_descriptor(
    state: dict, principal_id: str, canonical_ref: str
) -> dict: ...

def descriptor_for_view(state: dict, view_name: str) -> dict: ...
```

Exact private helper names may vary. Catalog functions are pure in-memory state
mutations; service-layer wrappers own locking and `save_state`.

Change projection and reconciliation APIs to accept an immutable descriptor
record rather than independent raw read/write arguments:

```python
authorize_changes(changes, descriptor)
project_tree(..., descriptor, ...)
visible_advance(..., descriptor, ...)
project_history(..., descriptor)
reconcile_commits(..., descriptor, ...)
```

A descriptor passed to these helpers is a fully resolved and validated
`view_descriptors` record. Internally `authorize_changes` uses
`descriptor["read"]` and `descriptor["write"]`, and projection helpers use
`descriptor["read"]`.

`allowed(path, prefixes)` remains a small pure primitive. No data-plane caller
may resolve roles directly or read `view["read"]`/`view["write"]`, because those
fields no longer exist. The legacy version-1 checker may retain a private pure
prefix-based authorization helper so it can validate old state; that helper is
not a version-2 runtime authorization path.

## CLI Contract

Add these commands:

```text
python3 forge.py add-principal ROOT PRINCIPAL
python3 forge.py add-service-account ROOT SERVICE --owner USER_PRINCIPAL
python3 forge.py add-team ROOT TEAM [--member PRINCIPAL ...]
python3 forge.py add-role ROOT ROLE [--read PREFIX ...] [--write PREFIX ...]
python3 forge.py bind-role ROOT ROLE (--principal PRINCIPAL | --team TEAM)
python3 forge.py add-view ROOT VIEW --principal PRINCIPAL [--branch BRANCH]
python3 forge.py migrate-identities ROOT
```

Use repeatable `--member`, `--read`, and `--write` options. `bind-role` must use
an argparse mutually exclusive required group. Keep `--branch` defaulting to
`main` and retain the one-configured-branch restriction.

Remove `--read` and `--write` from `add-view`. Do not retain a second raw-prefix
path that bypasses role resolution.

The Python facade should expose corresponding service functions. Tests may use
a shared helper that creates one principal, one role, one binding, and one view,
but production `add_view` itself must accept only `principal_id`, never raw
grants.

Catalog commands:

- acquire the existing state lock;
- require state version 2 before recovery or mutation;
- run the ordinary recovery/dispatch prelude before beginning the catalog
  operation;
- perform all reference and freeze checks;
- save the operation once on success, apart from any separate save performed by
  the recovery prelude;
- leave state bytes unchanged on rejection.

Rejection snapshots are taken after the recovery prelude reaches its stable
checkpoint. Recovery effects are not attributed to the rejected catalog
operation.

Successful commands are silent. Errors use the existing
`bastion: <message>` CLI path.

## Hook And Local Identity Boundary

Keep hook scripts passing only the physical view name:

```text
pre-receive ROOT VIEW
post-receive ROOT VIEW
```

The hook argument selects a server-configured endpoint. `pre_receive` must load
that view and obtain its `principal_id`, descriptor ID, and generation from
trusted state. It must not accept a principal, role, team, or descriptor from
stdin, push options, environment variables, commit metadata, or a client ref.

This separates endpoint and principal semantics but does not provide
authentication. Direct filesystem access remains outside the security boundary,
and Phase 4 must bind an authenticated network identity to an allowed endpoint.

## Required Data-Flow Changes

### View creation

Under the state lock:

1. Validate the physical view name and configured branch.
2. Require the principal to exist.
3. Resolve or reuse its exact effective descriptor.
4. Require every existing view for that principal to use the same descriptor.
5. Create the view record with generation `1` and no raw grants.
6. Initialize the bare repository and hooks.
7. Build initial projection using the descriptor.
8. Store descriptor fields on every initial synthetic anchor/revision.
9. Save state only after successful initial projection, preserving current
   behavior.

On an ordinary Python/Git exception before save, remove a repository created by
that attempt if it did not exist at entry. Process-crash-atomic view creation is
not added by this item; an abrupt kill can still leave an orphan repository and
the checker must continue to report it.

### Source validation

While holding the existing lock:

1. Resolve the view assignment once.
2. Load and validate its immutable descriptor.
3. Validate stale visible history with descriptor `read`.
4. Authorize and round-trip every submitted transition with descriptor `read`
   and `write`.
5. Validate `Change-Id` ownership with `principal_id`.
6. Before canonical CAS, persist the source descriptor and the complete target
   descriptor snapshot in the prepared transaction.

No source-validation step may call fresh role resolution after the prepared
transaction is persisted.

### Source confirmation and recovery

Recovery must use the transaction's principal and source descriptor when it:

- reserves ownership;
- creates submitted anchors;
- creates submitted revisions;
- activates the blocked event.

Before applying source state, require the physical view still to match the
captured descriptor and generation. A mismatch is corruption in this item and
must not advance the source cursor or activate fanout.

### Canonical fanout

Both source and administrative transactions capture target descriptors before
canonical CAS. Event creation copies that exact map. Dispatch copies one target
assignment to each job. A role or view lookup at event-dispatch time must not
choose the descriptor.

### Worker execution

A claim copies the job descriptor and generation plus current cursors into its
snapshot. It loads `view_state` and the descriptor record only after requiring
that the view assignment equals the job assignment.

The retained plan carries the same assignment. Completion checks it before
plan persistence and again immediately before publication. Ref adoption after
a process crash uses the retained descriptor-bound plan and records matching
anchor/revision metadata.

## Global Invariant Checker Version 2

Retain the existing stable codes and add:

| Code | Category |
|---|---|
| `IDENTITY` | Principal, service owner, team, role, or binding semantics |
| `DESCRIPTOR` | Descriptor hash, resolution, view binding, or captured assignment |

The version-2 checker must run identity and descriptor validation after base
state shape and before refs/frontiers.

### Identity checks

Require:

- exact record shapes and key/ID equality;
- valid IDs and kinds;
- service owners exist and are users;
- team members exist, are sorted, and are unique;
- role grants are normalized, sorted, unique, minimized, and nonempty in
  aggregate;
- bindings have deterministic IDs and valid subjects/roles;
- no principal with a view has a catalog record whose creation should have been
  blocked by the materialized-view freeze rule.

The last check may be implemented by requiring every view descriptor to equal a
fresh resolution of the complete current catalog and all views for a principal
to share it.

### Descriptor checks

For every descriptor:

- require exact shape and `algorithm == "effective-view-v1"`;
- recompute the effective read/write lists from its principal, teams, roles,
  and bindings;
- require every listed binding to apply to the listed principal;
- require the listed team/role/binding sets to equal fresh current resolution;
- recompute the canonical serialization and ID exactly;
- require a valid configured canonical ref;
- reject a descriptor record whose ID or effective lists differ.

For every view, require matching principal, descriptor, canonical ref, and
generation `1`.

### Historical source checks

Replace all historical use of current `view.read`/`view.write` with the source
transaction's descriptor. Require its principal to equal the transaction
principal and source view assignment.

Every confirmed submitted anchor and revision must carry the same descriptor
assignment as its source transaction.

### Projected history checks

For this item, all anchors in one view must use that view's one current
descriptor and generation. Replay every canonical transition through that
descriptor. Continue to verify hidden-only elision, exact submitted mappings,
synthetic bytes, tip filtering, and reachable closure.

Do not add multi-generation replay until view policy transitions exist.

### Event, job, and plan checks

Require exact bidirectional equality:

```text
transaction.target_descriptors
== event.target_descriptors
== descriptor assignment selected by each target job
== retained plan assignment, when present
```

`target_views` must equal sorted target-map keys. `pending_views` remains the
incomplete subset. Source target maps must exclude only the physical source
view, not every view belonging to the same principal.

The checker cannot independently prove that a historical target map contained
every view existing at preparation time because version 2 does not add a view-
creation journal. It must not compare old targets with the current view
inventory. Target completeness is a producer-time invariant tested immediately
after prepared-state persistence; the checker proves referenced-view validity
and exact transaction/event/job/plan copy equality. A durable view-creation
journal is outside this item.

### Change checks

Derive each revision's descriptor from its validated matching anchor. Require
the secondary index to match exactly. Validate owned changes against source
transaction `principal_id` and retain canonical-ref scope.

### Read-only and deterministic behavior

Version-2 checks remain read-only, deterministic, and sorted. Passing and
failing checks must leave state bytes, refs, object counts, catalog maps, and
descriptors unchanged.

## Version-1 Compatibility And Migration

### Runtime support boundary

New `init` creates version 2. Normal mutating commands must reject version 1
before calling `recover` with:

```text
forge state version 1 requires migrate-identities
```

Only these operations support version 1 after this implementation:

- `check`, using the existing version-1 checker semantics;
- `migrate-identities`.

Do not make normal runtime code interpret both schemas.

### Migration preconditions

`migrate-identities` accepts only a healthy, quiescent version-1 forge. Require:

- the version-1 global checker passes;
- every transaction is `published` or `aborted`;
- every event is `dispatched`;
- every job is `completed`;
- every published transaction has an empty `pending_views` list; an aborted
  transaction may retain its original target list because it has no live work;
- `view_errors` is absent or empty;
- every completed job satisfies `job.get("lease_token") is None`, has no
  `lease_expires_at`, and has no retained `plan`; retained expected cursor
  fields are allowed;
- no receiver marker exists;
- every actual canonical/view ref equals its persisted stable cursor;
- no command or worker is running outside the lock.

If a source transaction is unconfirmed, the operator must complete its exact
retry before migration. This item does not invent an administrative abort.

### Deterministic transformation

For each version-1 view named `V`:

1. Create user principal `V`.
2. Use the full lowercase hexadecimal SHA-256 of `V`'s UTF-8 bytes as the valid
   64-character legacy role ID, and create that role from the view's old
   read/write lists, applying the new canonicalization without changing
   effective authorization.
3. Create the corresponding direct principal binding.
4. Create the deterministic descriptor for principal `V` and the configured
   canonical ref.
5. Replace view `principal/read/write` with
   `principal_id/descriptor_id/descriptor_generation`.

Version 1 cannot reveal whether an old view represented a human or service
account, so migration deliberately defaults every old view to `user`. Operators
who need different identity provenance must create a new version-2 forge or add
new service-account endpoints after migration; do not guess from names ending
in `-bot`.

Backfill descriptor ID and generation onto:

- every anchor using its containing view;
- every logical revision using `revision.view`;
- every source transaction using `transaction.view`;
- source `principal_id` from old `principal`;
- every event target using the target view;
- every completed job using `job.view`.

For transaction target maps, use the corresponding event's immutable
`target_views` when an event exists. For an aborted transaction without an
event, use its retained original `pending_views`. Preserve `pending_views`
itself.

Rename owned change `owner` to `owner_principal_id`. Preserve ownerless changes
as ownerless.

Remove every version-1-only field and set `version` to `2` only in the complete
candidate state.

Preserve existing transaction, event, job, receiver, view, and Git object IDs.
Quiescence means no exact retry or active lease depends on rewriting those
identifiers.

### Atomicity and idempotency

Migration is one state-file replacement under the existing exclusive lock. It
must not:

- move or create a Git ref;
- write a Git object;
- reinstall hooks;
- create, remove, or rename a view repository;
- dispatch an event or claim a job.

Refactor invariant checking so the public checker and migration can invoke one
internal full-check path with an already loaded state and already collected ref
snapshot. That internal path must not acquire `state.lock` or reload
`state.json`.

Migration must acquire the lock with recovery disabled, collect refs, run the
complete version-1 check against the locked snapshot, verify quiescence, build
the candidate, and run the complete version-2 check against the candidate and
same immutable ref snapshot before `save_state`. This avoids both a self-
deadlock and a check-then-lock race. After releasing the lock, run the public
version-2 checker as a redundant end-to-end assertion.

Invoking migration on a healthy version-2 forge succeeds silently without
saving it again. Any other state version rejects.

Existing `state.temporary_fsynced`, `state.replaced`, and
`state.directory_fsynced` failpoints cover migration persistence. A process
killed before replacement must leave a complete valid version-1 state; a
process killed after replacement must leave a complete valid version-2 state.
This remains process-crash evidence, not a power-loss claim.

## Required Deliverables

### New production module

- `src/identity.py`: identifier validation, immutable catalog creation,
  component-aware prefix minimization/intersection, role resolution,
  descriptor serialization/hash, and descriptor lookup validation.

### Production files to update

| File | Required change |
|---|---|
| `src/service.py` | Initialize v2; add locked catalog operations; create descriptor-bound views and initial projections; capture admin targets |
| `src/protocol.py` | Resolve endpoint to principal/descriptor; authorize with descriptor; persist source and target assignments |
| `src/validation.py` | Accept descriptors in authorization while retaining byte-level `allowed` behavior |
| `src/projection.py` | Project and check visible advance from immutable descriptors |
| `src/reconciliation.py` | Use one descriptor for authorization and round-trip projection |
| `src/state.py` | Require runtime schema v2; recover descriptor-bound transactions; principal ownership fields; migration support helpers |
| `src/outbox.py` | Copy captured transaction target assignments into events and jobs |
| `src/worker.py` | Validate descriptor assignments at claim, plan, publication, adoption, and completion |
| `src/invariants.py` | Dispatch v1/v2 checks; add identity/descriptor and cross-record assignment validation |
| `forge.py` | Add catalog/migration commands; change `add-view` arguments and facade |

Do not change synthetic commit construction or Git object formats.

### New tests

- `tests/test_identity.py`: pure catalog, resolution, hashing, freeze, and
  service-account tests.
- `tests/test_identity_integration.py`: principal/view separation,
  descriptor-bound pushes/fanout, and ownership tests.
- `tests/test_identity_migration.py`: v1 checker compatibility, migration,
  rejection, idempotency, and process-crash atomicity.
- `tests/identity_harness.py`: optional non-test support for creating a simple
  principal/role/binding/view fixture. It must not construct expected
  descriptors with production helpers in tests intended to verify hashing or
  resolution.

### Existing tests to update

Update all fixtures that call raw-prefix `add_view` or hand-build affected state:

- validation, state, outbox, worker, service, forge, and cross-view tests;
- global invariant tests and corruption fixtures;
- deterministic state-machine fixture and immutable-view-add operation;
- process concurrency/failpoint fixture and semantic snapshot normalizer;
- tree-operation and unusual-path harness;
- shared-conflict and rejection-immutability fixtures.

Existing tests should keep their current behavior and operation grammars. Do
not add random identity changes to the state machine or repeat every process
failpoint for catalog creation. Add directed descriptor-bound crash coverage
where listed below.

### Documentation to update after implementation

- `README.md`: identity/catalog/view setup commands and state-v2 migration.
- `docs/ARCHITECTURE.md`: separate principal, effective descriptor, and
  physical view; add `identity.py` to the component table.
- `docs/SECURITY.md`: principal-based ownership, local endpoint identity limit,
  immutable descriptor authorization, and remaining revocation gap.
- `docs/TRANSACTIONS.md`: descriptor capture before CAS and worker publication
  checks.
- `docs/TESTING.md`: new modules and migration/focused commands.
- `.specs/001-global-invariant-checker.md`: note that its v1 assumptions remain
  the legacy checker contract and version 2 is extended by this specification.
- `ROADMAP.md`: mark only the principals/teams/roles/service-account/descriptor
  bullet done and update the baseline. Leave every later Phase 2 bullet pending.

## Required Test Matrix

### Pure identity and descriptor cases

- Valid and invalid IDs in every namespace.
- User and service-account creation.
- Missing, non-user, and self-referential service owner rejection.
- Empty, one-member, and mixed user/service-account teams.
- Missing and duplicate team member rejection.
- Role normalization, deduplication, and component-aware minimization.
- Root prefix coverage and sibling-prefix non-coverage.
- Write/read prefix intersection in both ancestor directions.
- Read-only, write-only, and composed roles.
- Direct principal binding and direct team binding.
- Missing role, principal, or team binding rejection.
- Duplicate binding rejection.
- Deterministic applicable team, binding, and role ordering.
- Descriptor ID equality for different state insertion orders.
- Descriptor ID inequality for different principals or provenance with equal
  effective grants.
- Existing descriptor collision with different payload rejection.
- Principal with no effective read cannot create a view.
- Catalog additions affecting a principal with a view reject without state
  mutation.

Expected descriptor IDs in hash tests must be computed by a small independent
test serializer using the specified literal domain separator and JSON options,
not by calling the production descriptor-ID helper.

### Principal and endpoint integration

- View name differs from principal ID and source transaction records both
  correctly.
- Two physical views for one user share one descriptor.
- A source push excludes only its physical source view; the other view for the
  same principal receives a synthetic representation when visible.
- The same principal may continue one `Change-Id` from its second current view.
- A different user with identical roles cannot claim that `Change-Id`.
- A service account can push and owns the resulting change under its own ID.
- The service account's human owner cannot claim that ID solely through the
  owner relationship.
- Team role grants produce the exact expected projected tree.
- Read-only effective descriptors reject source changes without moving refs or
  state.
- Hook stdin, commit metadata, and inherited environment cannot override the
  state-selected principal or descriptor.

### Descriptor-bound transaction and recovery cases

- Prepared source transaction contains the source descriptor and complete
  target descriptor map before canonical CAS.
- Prepared administrative transaction captures all current target descriptors
  before canonical CAS.
- Blocked event copies the exact target map.
- Dispatch copies the exact target assignment to each job.
- Claimed and ready jobs preserve it in retained plans.
- Worker completion records it on synthetic anchors and revisions.
- Source confirmation records it on submitted anchors and revisions.
- Process crash after canonical CAS recovers the original transaction
  descriptor and does not resolve roles again.
- Process crash after projected ref publication adopts the exact retained
  descriptor-bound plan.
- Independently corrupting transaction, event, job, plan, anchor, revision, or
  view descriptor IDs fails with `DESCRIPTOR` and never moves a ref.
- Generation `0`, `2`, boolean, or missing fails deterministically.

Add descriptor assertions to existing source and projection failpoint controls;
do not duplicate the complete kill matrix.

### Invariant cases

- Empty version-2 forge.
- Healthy user, service-account, team, direct-role, and team-role states.
- Two endpoints for one principal.
- Source-unconfirmed, pending, running, ready, retry, and completed states with
  coherent descriptor assignments.
- Corrupt principal kind, service owner, team member, role prefix, binding ID,
  descriptor hash, descriptor provenance, view binding, transaction source
  assignment, target map, anchor, revision, job, and plan.
- Version-1 healthy and corruption tests retain their existing diagnostics.
- Passing and failing version-2 checks are read-only.

### Migration cases

- Empty version-1 forge.
- One and two version-1 views with distinct policies.
- Completed source and administrative transactions.
- Submitted and synthetic anchors with owned and ownerless changes.
- Hidden-only frontier advancement.
- Completed events/jobs retained in state.
- Every old view becomes a user, even when named `team-a-bot`.
- Effective authorization, refs, histories, trees, anchor canonical/kind
  relationships, and Git object IDs are unchanged after migration; anchors and
  revisions gain only the required descriptor metadata.
- Transaction/event/job IDs and hook files are unchanged.
- A second migration invocation is a byte-preserving no-op.
- Pending, source-unconfirmed, running, ready, retry, leased, errored, or
  receiver-active v1 states reject migration without changing bytes or refs.
- Kill at each atomic state-save point leaves either a checker-valid v1 or
  checker-valid v2 state, never mixed JSON.

## Rejection Immutability

First run the ordinary recovery/dispatch prelude when that command normally
uses it. Then, for every rejected catalog, view, descriptor, or migration
operation, snapshot:

- exact `state.json` bytes;
- canonical and all view refs;
- identity/catalog/descriptor maps;
- view repository inventory;
- receiver marker inventory.

Require exact equality afterward. Do not compare dangling Git object counts for
a failed view initialization, but require any repository created by an ordinary
handled exception to be cleaned up.

A corrupt descriptor discovered during source validation, worker claim,
publication, or recovery must fail closed before canonical/projected ref
movement attributable to that operation.

## Ordered Implementation Plan

Implement in this order:

1. Add `src/identity.py` with ID validation, prefix minimization/intersection,
   immutable catalog records, resolution, canonical serialization, and hashing.
   Complete independent pure tests before touching Git workflows.
2. Add version dispatch to state loading and invariant checking. Preserve the
   existing checker as the version-1 path and make normal mutating APIs reject
   v1 before recovery.
3. Change `init_forge` to create the empty v2 schema. Add catalog service APIs
   and CLI commands with rejection-immutability tests.
4. Change view creation to accept a principal and materialize generation-1
   descriptors. Update one simple forge fixture and prove initial projection
   and invariant checking pass.
5. Change validation, projection, and reconciliation APIs to accept a
   descriptor. Remove all production reads of `view["read"]` and
   `view["write"]`.
6. Bind source transactions and principal-based `Change-Id` ownership to the
   source descriptor. Add different-view/same-principal and service-account
   integration tests.
7. Capture target descriptors in source/admin prepared transactions before
   CAS, then copy them through events and jobs. Add focused recovery tests.
8. Bind worker claim, plan construction, ready persistence, ref publication,
   adoption, anchors, revisions, and completion to the job assignment.
9. Implement the complete v2 invariant schema, identity/descriptor checks,
   historical source replay, projected replay, and cross-record equality.
10. Implement the quiescent v1 migration and pure candidate validator. Add
    migration fixtures and atomic process-crash tests.
11. Update all existing fixture helpers and hand-built state records. Keep
    expected tree/policy oracles independent.
12. Run focused identity, service, invariant, cross-view, worker, and migration
    tests.
13. Run deterministic state-machine, process, tree, conflict, and rejection
    suites. Add descriptor fields to semantic snapshot comparisons rather than
    normalizing them away.
14. Update documentation and mark only this roadmap bullet done.
15. Run all acceptance commands and record versions, runtimes, and test counts.

Do not start by mechanically adding optional descriptor fields to v1 records.
Complete the pure descriptor contract and explicit schema boundary first.

## Acceptance Criteria

This roadmap item is complete when all of the following are true:

- [ ] New forges use exact state schema version 2.
- [ ] Users and service accounts are distinct principal kinds with stable IDs.
- [ ] Service-account ownership grants no implicit Git or `Change-Id` authority.
- [ ] Teams contain only direct, immutable principal members.
- [ ] Roles and bindings are immutable and resolve deterministically.
- [ ] Effective read/write algebra is component-aware and independently tested.
- [ ] Descriptor IDs are deterministic under the specified canonical
      serialization and domain separator.
- [ ] Physical view names, principal IDs, descriptors, and job-ordering keys
      have separate semantics.
- [ ] Every view binds one principal and one descriptor at generation `1`.
- [ ] Source authorization uses the captured descriptor for every commit
      transition and round-trip projection.
- [ ] Source and administrative transactions capture all target descriptors
      before canonical compare-and-swap.
- [ ] Events, jobs, retained plans, anchors, and revisions retain exact
      descriptor provenance.
- [ ] Worker publication and ref adoption fail closed on any descriptor or
      generation mismatch.
- [ ] Two views for one principal can share `Change-Id` ownership while another
      principal with equal roles cannot.
- [ ] Existing canonical/projected Git commit bytes and OIDs do not change due
      solely to descriptor metadata.
- [ ] The v2 invariant checker independently recomputes descriptors and checks
      all cross-record assignments.
- [ ] The v1 checker remains available and read-only.
- [ ] Normal mutation rejects v1 before recovery; explicit migration is the only
      upgrade path.
- [ ] Healthy quiescent v1 state migrates without ref, object, history, mapping,
      or hook changes.
- [ ] Non-quiescent migration rejects without authoritative changes.
- [ ] Migration process crashes leave a complete checker-valid v1 or v2 state.
- [ ] Every catalog and descriptor rejection preserves exact state bytes and
      refs.
- [ ] Existing invariant, state-machine, process, tree, conflict, and rejection
      suites pass with descriptor assertions retained.
- [ ] No mutable policy, deny, revocation, expansion, contraction,
      operation-specific capability, metadata permission, or network
      authentication is introduced.

## Test Commands

Run focused identity and migration tests first:

```bash
python3 -m unittest -v \
  tests.test_identity \
  tests.test_identity_integration \
  tests.test_identity_migration
```

Run directly affected state and Git workflow modules:

```bash
python3 -m unittest -v \
  tests.test_validation \
  tests.test_state \
  tests.test_outbox \
  tests.test_worker \
  tests.test_service \
  tests.test_forge \
  tests.test_cross_view \
  tests.test_invariants
```

Run generated and real-process gates:

```bash
python3 -m unittest -v tests.test_state_machine
python3 -m unittest -v \
  tests.test_process_concurrency \
  tests.test_process_failpoints
```

Run tree, conflict, and rejection gates:

```bash
python3 -m unittest -v \
  tests.test_tree_operations \
  tests.test_unusual_paths \
  tests.test_shared_conflicts \
  tests.test_rejection_immutability
```

Run the complete suite and repository checks:

```bash
python3 -m unittest discover -v tests 'test_*.py'
python3 -m py_compile forge.py tests/*.py src/*.py
git diff --check
```

The implementation report must include:

- operating system and kernel;
- Python and Git versions;
- focused and complete-suite runtimes and test counts;
- one independently computed example descriptor payload and ID;
- migration before/after state versions and proof that all refs/OIDs match;
- any production file changed beyond the required deliverables and the focused
  test that justified it.

## Exit Criteria

The initiative is complete only when identity provenance and effective
authorization are durable, immutable, deterministic, and independently
checkable across source validation, canonical commitment, fanout, worker crash
recovery, projected publication, and logical-change indexing.

Only after these criteria pass should Phase 2 proceed to separate read, create,
modify, delete, mode, symlink, branch, merge, and admin capabilities. Policy
mutation or revocation must not be implemented by editing a version-2
descriptor or view record in place.
