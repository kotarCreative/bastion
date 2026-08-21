# Capabilities, Deny Rules, And Policy Transitions

## Status

Implemented for state schema version 3.

This specification covers the remaining core Phase 2 items of `ROADMAP.md`:

- separate read, create, modify, delete, mode, symlink, branch, merge, and
  admin capabilities;
- more-specific deny rules composed through additive role inheritance;
- read and write revocation between clone and push;
- view expansion and contraction;
- stale policy and stale anchor retention;
- explicitly non-revocable previously downloaded data.

Metadata permissions for refs, authors, messages, timestamps, topology, tags,
and `Change-Id` records remain a separate future item.

## Decisions

### Capabilities

A version-3 role grants and denies path-scoped capabilities. The capability
set is exactly:

```text
read create modify delete mode symlink branch merge admin
```

`read` governs projection visibility and remains a prerequisite for every
other effect. Data-plane effects are classified per changed path:

| Effect classification | Required capabilities |
|---|---|
| parent absent, child present | `create` |
| parent present, child absent | `delete` |
| blob object ID differs | `modify` |
| mode differs | `mode` |
| either side is mode `120000`, or the kind changes | `symlink` |

One changed path may require several capabilities at once (for example a
content and mode change requires `modify` and `mode`). Every effect also
requires `read` coverage, preserving the version-2 invariant.

`branch`, `merge`, and `admin` are recognized, resolvable capabilities with
no data-plane effect yet: merges are rejected globally, one canonical branch
is configured per forge, and `admin` gates only administrative canonical
updates.

### Deny rules

Roles carry two full capability maps:

```json
{
  "id": "developer",
  "allow": {"read": ["shared"], "create": ["shared"], "modify": ["shared"],
            "delete": [], "mode": [], "symlink": [], "branch": [],
            "merge": [], "admin": []},
  "deny":  {"read": ["shared/private"], "create": [], "modify": [],
            "delete": [], "mode": [], "symlink": [], "branch": [],
            "merge": [], "admin": []}
}
```

Resolution unions role grants per capability additively exactly as before,
separately for `allow` and `deny`, each side sorted and minimized.
Authorization adjudicates pointwise per path and capability: let `A` be the
longest allow prefix covering the path and `D` the longest deny prefix
covering it. The capability applies only when `A` exists and (`D` is absent
or strictly shorter than `A`). Equal prefixes or a longer deny win, so more-
specific deny rules override broader allows, and exact-area denies always
win. Denies never grant access. Because interior subtrees cannot be spelled
as finite prefix sets, effective areas are stored as two maps on the
descriptor rather than pre-subtracted lists:

```json
{
  "capabilities": {"read": ["shared"], "create": [], "...": []},
  "restrictions": {"read": ["shared/private"], "create": [], "...": []}
}
```

Both maps hold all nine keys. Projection visibility and every effect check
use the same longest-match adjudication against these maps.

### Descriptor algorithm

The descriptor payload replaces `read`/`write` with one `capabilities` map
holding all nine sorted minimized lists, under algorithm id
`capabilities-v1`. The ID derivation is unchanged (domain separator
`bastion-effective-view-v1\0` retained for payload hashing; the algorithm
field distinguishes payloads).

Historical descriptors written by earlier schemas remain immutable evidence.
They are validated structurally and by hash but are never recomputed from
the live catalog. Only descriptors bound to current generation-1 view
assignments must equal a fresh resolution.

### Unfrozen catalog

Version 2 rejected any catalog addition that would alter resolution for a
principal with a materialized view. Version 3 removes that freeze: catalog
records stay immutable once created, new records may be added at any time,
and their effect is dormant until an explicit policy transition applies it.
This is the sanctioned change workflow; editing state directly remains
unsupported.

### Policy transitions

```text
python3 forge.py transition-principal ROOT PRINCIPAL
```

Under the exclusive lock, requiring global quiescence (no non-terminal
transactions, no unfinished jobs, no receiver markers):

1. Recompute the principal's descriptor from the current catalog.
2. If its ID equals the principal's current assignment, succeed silently.
3. Otherwise append the descriptor to `view_descriptors` and update every
   view of that principal atomically to `(new_id, generation + 1)`.
4. If effective `read` changed for a view, build one policy-adjustment
   commit in that view repository: parent is the current projected tip,
   tree is the new-descriptor filter of the frontier snapshot. Publish it
   as a fast-forward and record its anchor:
   `{"canonical": <frontier>, "kind": "synthetic", "change_id": null,
     "descriptor_id": <new>, "descriptor_generation": <new-generation>}`.
5. Save state once after all views succeed.

Contraction removes paths from future projections and rejects pushes that
touch them; expansion adds paths and produces adjustment commits that add
files. The projected ref always moves forward.

### Stale policy and anchors

Anchors record the descriptor that produced them. After a transition,
historical anchors keep their old descriptors and remain valid evidence;
only new work uses the new assignment. An exact source-unconfirmed retry
still requires the recorded transaction assignment to equal the current
view assignment, so a transition cannot be interleaved into an unfinished
transaction because transitions require quiescence.

### Non-revocable downloads

Revocation governs server behavior only. Objects already contained in a
projected history or already downloaded by a client are explicitly
non-revocable: contraction does not rewrite existing projected history and
cannot remove data from client clones. Server-side garbage collection of
newly hidden historical objects is future work.

### Administrative authorization

`update-canonical` now requires an acting principal whose current effective
descriptor grants `admin` covering every path changed between the expected
and new canonical commits. The acting principal is recorded on the
administrative transaction.

## State Schema Version 3

Top-level keys are unchanged except `version == 3`.

Role records replace `read`/`write` with `allow`/`deny` capability maps.
Descriptor payloads use `capabilities` and algorithm `capabilities-v1`.
Administrative transactions add `principal_id` for the acting administrator.
All other record shapes are unchanged from version 2.

Normal mutating commands reject versions 1 and 2 before recovery:

```text
forge state version N requires migrate-identities
```

`migrate-identities` chains upgrades: version 1 migrates to 2 then 3;
version 2 migrates directly to 3. The version-2-to-3 step rewrites role
grant maps (`read` stays `read`; `write` expands to `create`, `modify`,
`delete`, `mode`, and `symlink`; denies empty), recomputes and appends
fresh descriptors, and repoints only current view assignments. Historical
descriptor references are preserved untouched, and generations do not
change because migration preserves effective authorization. Quiescence,
atomicity, and idempotency requirements match the version-1 migration.

## Checker Version 3

Beyond version-2 checks:

- roles validate both capability maps (normalized, minimized, sorted,
  unique lists; nine exact keys; at least one non-empty allow);
- descriptors validate by algorithm: `capabilities-v1` records recompute
  fully against the catalog when they are the current generation-1
  assignment of their views; all other referenced descriptors are checked
  structurally and by canonical hash only;
- projected replay is era-based: each anchor carries the descriptor that
  produced it; the era advances exactly when an anchor's descriptor differs
  from the active era, which is permitted only for a policy-adjustment
  anchor (synthetic, `change_id` null, same principal, monotone generation,
  tree equal to the new filter of its canonical snapshot);
- tip-tree equality uses the view's current descriptor;
- source transactions authorize with their captured descriptor's read and
  required capabilities regardless of era;
- administrative transactions verify the acting principal exists and holds
  admin coverage of every changed path in the update range.

## Out of Scope

- metadata permissions;
- nested teams and role inheritance beyond additive team bindings;
- per-view (as opposed to per-principal) policies;
- automatic application of catalog changes without an explicit transition;
- garbage collection of newly hidden historical objects;
- merge topology support behind the `merge` capability.
