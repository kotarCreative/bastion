# 020 — Scoped branch transport

## Purpose

Apply the explicit branch rights in capabilities-v4 to Git smart HTTP as well
as to the browser projection. Branch permissions are independent of path
capabilities: each advertised/ref transition needs a branch grant, and each
commit transition must still satisfy the existing path-capability checks.

## Contract

* v3 remains compatible: reads and updates use only the canonical default
  branch; secondary refs are neither advertised nor accepted.
* v4 clone/fetch advertises only authorized `refs/heads/*` refs. The projected
  `HEAD` is the canonical default when readable, otherwise the first readable
  full ref in sorted order. With no readable refs, the private projection is
  empty and has an unborn neutral `HEAD`.
* A push is one branch ref update, fast-forward only. Updating an existing
  target requires `read` and `update`. Creating a target requires target
  `read` and `create`, and a source ref that is currently advertised and
  readable—even for a pure fork with no new commits. Creation starts from that
  source's current projected tip. If new commits are added, the target also
  requires `update`. Arbitrary historical bases, stale
  creation proofs, force updates, deletion, multiple refs, merges, tags, and
  ref injection are rejected. Path capabilities still govern all commit
  transitions.
* Authorization is evaluated against the current active descriptor on each
  request. Revoked refs disappear from subsequent projections, including for
  existing tokens. Empty branch rights do not revoke UI access or disclose
  canonical branch inventory/OIDs through generic Git errors.
* PR scopes describe policy only; no PR workflow or merge operation is
  provided by this transport.

## Non-goals

This is not broad GitHub-style merge compatibility. It does not accept
arbitrary history as a new branch base, multi-ref transactions, tags, or
force-pushes.
