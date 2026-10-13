# 018: Multi-branch foundation

## Initial operator slice

Repository truth records a plain mapping from full canonical branch refs to
commit IDs in `canonical_refs`. The existing `canonical_ref` and
`canonical_tip` fields continue to alias the default branch. Readers accept
legacy manifests as singleton mappings; the next publication emits the new
mapping. Existing immutable WAL records retain their original shapes.

A trusted filesystem/server operator may list the canonical inventory. Branch
creation additionally verifies that the specified principal has a
root-administrator descriptor, and creates an internal branch at the current
tip of an existing canonical branch. Creation is a WAL `branch_create` event recording principal and descriptor identity,
source/destination full refs, tip, and receipt time. It uploads no new pack and
claims no `Change-Id` ownership. The event's descriptor is part of backup's
reachable object closure even if no active view currently references it.

## Explicit non-goals

This foundation does not expose non-default refs through public clone/fetch,
push, browser browsing, or view projections. Review/PR workflows are not
implemented. It does not implement client feature branches, branch deletion,
merge, or protected-branch policy. It must not be described as completed forge
branching.

## End-user path (separate phases)

1. Define repository viewing permissions explicitly, including who may
   enumerate branch names and read each branch's objects; repository access
   alone must not silently grant access to every future branch.
2. Design branch-scoped projections and Git transport authorization, proving
   hidden refs/objects cannot be discovered through wants, advertisements,
   errors, or caches. Preserve default-branch compatibility.
3. Add authorized branch lifecycle operations and concurrency/lease semantics;
   define deletion, protection, and merge policy rather than inheriting them
   from operator-only creation.
4. Design PR/review lifecycle, source/target authorization, patchsets, and
   merge publication as distinct workflows before browser exposure.

Backup and restore pin manifest/WAL truth and immutable descriptor objects;
full-ref branch state remains WAL-derived, not an independently mutable
catalog. The canonical branch map has a hard limit of 1,024 branches including
the default branch. The existing WAL sequence cap also applies; no additional
branch-count policy beyond this map bound is introduced.
