# Security Model

## Status

Bastion is an architecture and correctness prototype. Its local filesystem layout
is not a production security boundary: a user with direct access to the forge
root can read `canonical.git` regardless of Git protocol policy.

Security claims in this document apply to the modeled Git endpoint and object
closures, not to hostile users sharing the server filesystem.

## Protected Assets

- unauthorized blob contents;
- hidden filenames and tree structure where practical;
- hidden refs and canonical object IDs;
- canonical paths outside a submitter's write scope;
- canonical history and projected ref integrity;
- logical change ownership;
- transaction and audit metadata.

## Trust Boundaries

Untrusted:

- pushed commits, trees, blobs, metadata, and ancestry;
- claimed `Change-Id` trailers;
- requested refs and object IDs;
- stale clones and rewritten local history.

Trusted in the prototype:

- forge code and Git executable;
- canonical repository storage;
- immutable identity catalog and effective view descriptors in state;
- local state file and lock;
- canonical update coordinator.

Hooks receive only a physical view name. `pre_receive` derives principal,
descriptor, and generation from trusted server state; stdin, push options,
environment variables, commit metadata, and client refs cannot select or
alter identity. This separates endpoint and principal semantics but provides
no authentication: Phase 4 must bind an authenticated network identity to an
allowed endpoint.

Production must add authenticated principals, protected service storage,
database authorization, and controlled HTTP/SSH endpoints.

## Hard Invariants

1. A view must not publish an unauthorized blob, tree, commit, or ref.
2. A push must not modify a path outside current write authorization.
3. A permitted push must not delete or overwrite hidden canonical state.
4. Authorization must inspect every commit transition, not only the tip.
5. A rejected push must not move canonical or projected refs.
6. A projected tree must equal the current policy filter of its canonical
   frontier.
7. An older transaction must never regress a newer canonical frontier or ref.
8. Source publication must be confirmed before other views can expose a commit.
9. A `Change-Id` never grants read, write, or ancestry authority.
10. Canonical branch updates must pass through the serialized coordinator.

## Read Isolation

Each projected repository contains an authorization-safe reachable closure and
has no canonical alternate. Tests verify that known hidden blobs are absent
from ref reachability, direct object lookup, and direct protocol wants.

Server-side object reuse is an optimization, not an authorization boundary.
Future shared object storage must authorize every lookup and pack delta base.

Read revocation cannot remove objects already downloaded by a client. Policy
changes can only govern future server operations.

## Descriptor-Bound Authorization

Authorization never reads live policy at transaction time. Each view binds one
immutable generation-1 descriptor; source transactions capture their source and
all target descriptors before the canonical compare-and-swap, and events, jobs,
plans, anchors, and revisions copy that exact assignment. Workers revalidate
assignment equality at claim, plan persistence, publication, adoption, and
completion, failing closed with `projection descriptor changed` before any ref
movement. Recovery replays with captured descriptors and refuses to advance a
source cursor whose view no longer matches its recorded assignment.

Consequence: a worker can never publish under permissions different from those
that authorized the submission, and independently recomputable descriptor IDs
let the global checker verify every historical decision without trusting the
runtime. Catalog additions are dormant until a quiescent policy transition;
revocation then applies to future projections and pushes, while data already
projected or downloaded is explicitly non-revocable.

## Write Authorization

Roles grant and deny path-scoped capabilities (`read create modify delete mode
symlink branch merge admin tag`). For every changed path the forge classifies
its effects, requires `read` coverage plus each effect capability, and
adjudicates pointwise: the capability applies only when the longest allow
prefix covering the path exists and is strictly longer than any deny prefix
covering it. Equal prefixes or a longer deny win; denies never grant access.

## Metadata Authorization

Principals may declare immutable author email addresses at creation. A
principal with declared addresses may submit only commits authored by one of
them; undeclared identities fail closed before any object promotion, and the
global checker re-verifies every confirmed mapping against the same records.
Principals without declared addresses remain unrestricted by explicit
operator choice. Submitted commits must not be dated more than ten minutes
into the future; receive time is recorded on transactions as `received_at`
and re-verified by the checker where present. Messages stay byte-exact,
topology stays linear with `merge` reserved, views expose exactly one
projected ref with `branch` reserved, tags remain unsupported data-plane
entries with `tag` reserved, and `Change-Id` ownership rules are unchanged.

Authorization treats Git as snapshots:

- rename is delete plus add;
- deletion is an explicit path effect;
- executable-bit changes are entry changes;
- symlinks are blobs with mode `120000`;
- gitlinks are rejected;
- file/directory replacement is allowed only when every conflicting entry is an
  authorized explicit effect;
- hidden ancestor or descendant collisions are rejected.

The canonical candidate is reprojected after replay. Its projected tree must
exactly equal the submitted tree before canonical ref movement.

## Metadata

Synthetic commits intentionally expose only:

- authorized tree state;
- projected ancestry;
- generic projection identity and message;
- fixed timestamp;
- optional `Change-Id` for a visible logical change.

They do not copy canonical authors, messages, signatures, or source timestamps.
Commit count and visible topology remain observable.

`Change-Id` ownership is principal-based and scoped to the canonical ref. Two
views of one principal may continue the same change; another principal cannot
claim it, and a service account owns changes under its own ID even when a user
owns that account. Ownerless IDs introduced by canonical history remain
unclaimable. Production needs repository, branch, review-state, and role-aware
ownership rules.

## HTTP Transport

An experimental smart-HTTP endpoint serves every view with one shared token
per view (`forge.py add-token`, `forge.py serve`). Authorization happens
before any Git process starts. Unknown views and unauthorized views return
identical 404 responses, so view existence is not enumerable. Unknown or
malformed credentials return 401. Hidden objects are absent from view
repositories by construction, so direct OID wants fail inside Git without
server-side special cases.

The endpoint is a testing boundary, not a production service: there is no
TLS, no rate limiting, no identity beyond shared tokens, buffered request
bodies, and non-uniform error timing.

## Protocol And Object Risks Still Open

The following are not production-proven:

- SSH transport and production HTTP identity (TLS, principals, rate limits);
- protocol v0/v1 configuration;
- shallow and partial clone;
- thin packs and delta-base confidentiality;
- Git LFS authorization;
- repack, bitmap, commit-graph, and multi-pack-index behavior;
- signed commits and tags;
- SHA-256 object format;
- resource exhaustion and decompression limits;
- forge web, API, diff, search, archive, webhook, and CI surfaces;
- indistinguishable hidden-conflict error behavior.

See [ROADMAP.md](../ROADMAP.md) for the work required before production claims.
