# Metadata Permissions

## Status

Implemented for state schema version 4.

This specification closes the final Phase 2 item of `ROADMAP.md`: metadata
permissions for refs, authors, messages, timestamps, topology, tags, and
`Change-Id` records. It builds on the capability model of spec 007 and
introduces state schema version 4.

## Decisions

### Capability surface extension

The capability set grows by exactly one reserved capability:

```text
read create modify delete mode symlink branch merge admin tag
```

`tag` is recognized, resolvable, and validated everywhere capabilities are,
and gates no data-plane effect yet. Tag projection belongs to Phase 5; the
capability exists now so that policies written today survive that work.
Roles, descriptors, and every map validation move from nine to ten exact
keys. The descriptor algorithm becomes `capabilities-v2`; the domain
separator is unchanged and the algorithm field distinguishes payloads.
Existing descriptor IDs change as a result, so migration repoints current
view assignments while preserving generations, exactly as the version-2 to
version-3 step did.

### Author identity binding

Principals may declare author email addresses:

```json
{"id": "alice", "kind": "user", "emails": ["alice@example.test"]}
```

- `emails` is optional, immutable after creation, sorted, unique, lowercase,
  and validated as simple addresses;
- `add-principal` and `add-service-account` accept repeatable `--email`
  options at creation time only;
- service accounts own their own addresses and never inherit their owner's.

A principal with a non-empty `emails` set may submit only commits whose
author email address is in that set. A principal without any declared
address remains unrestricted; this preserves legacy behavior and is an
explicit operator decision rather than a default grant. Enforcement reads
the pushing view's principal record from state during `pre-receive`, before
any object promotion, and rejects the push otherwise. Author name is not
constrained; identity binding keys on the email because Git treats it as
the stable identifier.

The global checker independently re-verifies every confirmed mapping: the
canonical commit's author email must satisfy the current record of the
transaction's principal. Because catalog records are immutable, current
state equals prepare-time state by construction.

### Timestamp bounds

Submitted commits must not be dated in the future beyond a fixed skew
window of ten minutes against receive time. Both author and committer
timestamps are checked per submitted commit before reconciliation. There is
no lower bound: backdating through rebase is legitimate and preserved.
Transactions optionally record `received_at` (epoch seconds) when created by
this version or later; the checker re-verifies the bound whenever the field
is present and skips migrated historical transactions where it is absent.

### Messages

Commit messages are preserved byte-exact between submission and canonical
commit, are never rewritten by the server except for synthetic projection
commits whose deterministic form is already specified, and carry at most one
`Change-Id` trailer. No further message policy is defined in this version;
the existing trailer rules remain the complete contract.

### Topology

Submitted histories remain linear first-parent chains; merge commits are
rejected globally regardless of grants. The `merge` capability stays
reserved for future topology work behind an explicit specification change.
Canonical administrative updates continue to require linear fast-forwards.

### Refs

Every view exposes exactly one projected ref equal to the configured
canonical branch. Branch creation, deletion, renaming, and force updates
remain rejected without exception, and the `branch` capability stays
reserved. Canonical staging refs remain internal intake plumbing and are
never projected or advertised through views.

### Tags

Tag objects, tag refs, and signed-tag projection are outside this version's
behavior; pushes introducing them are rejected by the existing unsupported-
entry and single-ref rules. The `tag` capability is reserved as defined
above so metadata policies can reference it before Phase 5 activates it.

### Change-Id records

Ownership, reservation timing, reuse rejection across principals and
branches, and the synthetic-anchor `change_id` rules are unchanged from
spec 007. This version defines no additional `Change-Id` policy.

## State Schema Version 4

Top-level keys are unchanged except `version == 4`.

- Principal records gain optional `emails` (sorted, unique, lowercase list);
  all other record shapes are unchanged from version 3.
- Role `allow`/`deny` maps hold ten exact keys including `tag`.
- Descriptor payloads use algorithm `capabilities-v2` with ten-key maps.
- Source transactions may carry optional numeric `received_at`.

Normal mutating commands reject versions 1 through 3 before recovery:

```text
forge state version N requires migrate-identities
```

`migrate-identities` chains upgrades: version 1 migrates through 2 and 3 to
4, version 2 migrates through 3, version 3 migrates directly. The version-3
to version-4 step inserts an empty `tag` key into every role map, appends
fresh `capabilities-v2` descriptors, and repoints only current view
assignments; generations do not change. Quiescence, atomicity, and
idempotency requirements match earlier migrations.

## Checker Version 4

Beyond version-3 checks:

- roles validate ten-key capability maps;
- descriptors validate by algorithm `capabilities-v2` under the same
  structural, hash, and provenance-recompute rules as version 3;
- principals validate optional `emails` shape;
- confirmed mapping authors satisfy the acting principal's declared emails;
- timestamp bounds are verified against `received_at` when present.

## Out of Scope

- tag projection and signed-tag verification (Phase 5);
- author-name policy and identity attestation;
- message content policy beyond `Change-Id` trailer rules;
- merge topology behind the `merge` capability;
- branch namespace ACLs behind the `branch` capability.
