# Authenticated Intake Boundary

## Status

Ready for implementation.

This specification implements the core of Phase 4 (Real Git Server
Boundary). Pushes over the network no longer enter published view storage:
they land in a separate, quarantine-separated intake repository that is the
only push target. View repositories become fetch-only. The transport layer
gains resource limits, stable opaque errors, audit identity, and a strict
SSH-style command grammar exercised through a loopback shim.

## Goal

- Rejected or malicious pushes can never place objects into published view
  or canonical storage; intake quarantine absorbs them.
- Clients use unmodified Git: fetch from the view, push to the forge, over
  HTTP and over an SSH command grammar with identical server-side rules.
- Every accepted and rejected receive records who, how, and what in the
  durable audit log.
- Resource abuse (giant packs, deep chains, huge objects, slow requests) is
  bounded by explicit limits enforced before validation work begins.

## Non-Goals

- TLS termination, real sshd deployment, per-IP rate limiting;
- multi-branch pushes (the one-authorized-branch rule is unchanged);
- shared object stores between views (Phase 5 decision);
- changing validation, reconciliation, projection, outbox, or worker logic.

## Layout And Repositories

```text
root/
  canonical.git/            unchanged; never a push target
  views/<view>.git/         fetch-only published storage
  intake/<view>.git/        authenticated push target
```

- `add-view` creates both repositories. The intake repository carries an
  objects alternate pointing at its sibling view store (`objects/info/
  alternates`), so advertised tips resolve, thin-pack fixup finds bases,
  and newly projected synthetic commits are visible without re-copying.
  The alternate chain must never include canonical storage.
- View repositories get an unconditional reject-all `pre-receive` hook with
  a stable message: direct pushes (local or network) are refused; intake is
  the only write path.
- Intake hooks validate the trusted wrapper context when present
  (`BASTION_VIEW` must match the target repository). A bare local
  receive-pack against an intake repo remains the trusted operator path:
  same-user processes already own the filesystem, and the network boundary
  is where client influence is mediated.
- The intake ref `refs/heads/<branch>` mirrors `projected_tip`. It is set
  at view creation, after every confirmed receive, and repaired during
  recovery/synchronization whenever it diverges. It is bookkeeping only;
  the persisted cursor remains authoritative.

## Wrapped Receive Pipeline

The HTTP handler spawns, for `git-receive-pack`:

```text
git --git-dir root/intake/<view>.git receive-pack --stateless-rpc
```

with `GIT_ALTERNATE_OBJECT_DIRECTORIES=<view>/objects` preset so Git's own
quarantine machinery extends visibility to quarantine + view + intake
stores inside hooks. Identity travels in trusted environment keys
(`BASTION_VIEW`, `BASTION_PRINCIPAL_ID`, `BASTION_TRANSPORT`,
`BASTION_REMOTE_ADDR`) that clients cannot influence; client-supplied
values are stripped first.

Hook ordering replicates today's semantics exactly:

1. **intake pre-receive**: parse updates; enforce idempotent replay,
   stale/force/deletion rules, quiescence gates, metadata permissions,
   tree authorization, canonical reconciliation into candidate objects,
   Change-Id rules; promote candidates into canonical; publish the
   canonical ref by CAS; then publish the submitting view ref
   old->new explicitly (receive-pack no longer does this); persist the
   transaction as `canonical_committed`; enqueue outbox events.
2. receive-pack migrates quarantine into the intake store and moves the
   intake mirror ref.
3. **intake post-receive**: unlink the receiver marker, repair the mirror,
   drain ready projection jobs.

Crash windows keep forward-only recovery: if the process dies between 1
and 2, recovery sees the view ref already advanced, completes fanout, and
repairs the mirror. A retried push whose recorded outcome was
`source_unconfirmed` publishes the view ref explicitly and proceeds.

Rejections inside pre-receive print one stable opaque line chosen from a
fixed category table (`policy`, `limits`, `integrity`, `state`, `conflict`)
plus exit nonzero, so quarantine is discarded and stderr carries no forge
internals beyond what existing hook output already exposed.

## Transport Limits

HTTP layer (before any Git process runs):

- request body cap for receive: 256 MiB default (`BASTION_MAX_PACK_BYTES`);
- gzip decompression ratio cap 32x and decompressed cap equal to the body
  cap; violations are `400`;
- subprocess timeouts: upload-pack 300 s, receive-pack 600 s.

Intake pre-receive (from the quarantined pack):

- maximum commits per push: 4096 (`rev-list --count`);
- maximum quarantined object count: 100000;
- maximum single object size: 64 MiB (`cat-file --batch-check`);
- `receive.maxInputSize` configured on intake repos as a Git-level backstop
  equal to the HTTP body cap.

All limit rejections use the stable `limits` category.

## Upload-Pack Hardening

- `GIT_PROTOCOL` passthrough is allow-listed to exactly `version=2`
  (optional `=<digits>`); anything else is dropped, not forwarded.
- Hidden-object safety stays structural: view stores contain no hidden
  objects, so v0 want-lines, v2 fetches, and direct OID wants fail inside
  Git. New tests prove both protocol versions reject unknown/hidden OIDs.

## SSH Command Grammar

Production SSH requires a real sshd calling a forge entry point; this
experiment proves the grammar and routing with a loopback shim:

- `tools/bastion-ssh` parses the exact argument shapes Git produces
  (`[-p port] [-o opts] user@host "git-upload-pack 'path'"`),
- accepts only `git-upload-pack` / `git-receive-pack` with a single-quoted
  absolute path resolving to `<view>.git`; all else exits 127 with a fixed
  message,
- maps the configured identity to the view (mismatch is a hard error),
  authenticates with the same token model, and relays bytes to the same
  loopback HTTP backend, so SSH-style traffic exercises identical server
  rules.

Fetches relay as sequential stateless protocol v2 request/response
exchanges; pushes terminate on pack-completion detection (the shim parses
pack framing rather than waiting for client EOF, which Git does not
guarantee over interactive transports). Fully streamed interactive Git
sessions over this shim remain experimental; production deployments would
call the forge entry point from a real sshd. Tests drive it through
`GIT_SSH_COMMAND` and direct deterministic exchanges.

## Audit Identity

Accepted receives record `receive.accept` and rejected ones
`receive.reject` with subject `<view>/<principal>` and detail containing
transport, remote address, old/new OIDs, and the category or transaction
id. Token minting records `token.issue`.

## Repository Discovery, Authentication, Branch ACLs

- Route grammar is unchanged; `/canonical.git`, `/intake/*`, and nested
  paths stay unreachable (404 indistinguishable from unknown views).
- Tokens authorize exactly one view for upload and intake receive; unknown
  view and unauthorized view remain byte-identical 404s.
- Branch ACLs remain the single authorized branch rule with CAS old-value
  checks against the mirrored tip.

## Deliverables

- `src/layout.py`: `intake_repo`.
- `src/service.py`: intake creation/config/hooks, mirror maintenance in
  sync and transitions.
- `src/protocol.py`: intake-mode pre/post-receive, explicit view-ref
  publication, limits, categories, audit events.
- `src/http_server.py`: wrapped intake spawning, identity env stripping,
  GIT_PROTOCOL allowlist, body/decompression caps, per-service timeouts.
- `tools/bastion-ssh`: SSH command shim.
- Tests: fixture migration to the intake boundary plus
  `tests/test_transport_security.py`.

## Required Test Matrix

### End-to-end

- Clone/fetch/push/fanout over HTTP still pass; push now leaves view store
  byte-identical until acceptance (rejected push adds zero objects).
- Local `git push` directly to a view repo fails with the stable message.
- Local `git receive-pack` on intake without wrapper context fails.

### Quarantine integrity

- Thin pack whose bases live only in the view store is accepted: the
  alternates chain makes view-only projection objects resolvable, proven by
  intake-side object probes plus a successful post-fanout push.
- Canonical-hidden objects are unresolvable from the intake namespace
  (`cat-file` denial), so thin-pack fixup against them cannot succeed.
- Objects unreachable from the submitted tip are rejected (existing rule)
  and leave nothing behind.

### Limits

- Oversized HTTP body -> 400; oversized declared pack -> `limits`.
- Depth/object-count/object-size violations rejected with `limits`.
- Malformed gzip and decompression bombs -> 400 without spawning Git.

### Protocol hardening

- Direct OID want of hidden/canonical objects fails under v0 and v2.
- Unknown capabilities and garbage pkt-lines fail without state change.
- `GIT_PROTOCOL` injection values are dropped.

### Audit and SSH grammar

- Accept/reject audit entries carry principal + transport.
- Shim rejects wrong-view usernames, non-git commands, traversal paths.
- Full clone/fetch/push round trip via `GIT_SSH_COMMAND` + `ssh://` URL.

## Acceptance Criteria

- [ ] No route writes into published storage before full validation.
- [ ] Rejected pushes leave view stores, canonical refs, state, and
      mappings byte-identical (rejection-immutability method).
- [ ] All prior suites pass with pushes routed through intake.
- [ ] Limits, categories, audit entries, and SSH grammar are covered.
