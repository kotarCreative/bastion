# Security Model

## Status

Veil is an architecture and correctness prototype. Its local filesystem layout
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
- view policy configuration;
- local state file and lock;
- canonical update coordinator.

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

## Write Authorization

The prototype grants positive read and write path prefixes. A changed path must
match both current read and write scope.

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

`Change-Id` ownership is scoped to the prototype principal/view and canonical
ref. Another principal cannot claim the same ID. Production needs repository,
branch, review-state, and role-aware ownership rules.

## Protocol And Object Risks Still Open

The following are not production-proven:

- smart HTTP and SSH authentication;
- protocol v0/v1/v2 configuration;
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
