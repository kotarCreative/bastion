# Roadmap

## Current Baseline

The local prototype currently has:

- deterministic permission projections;
- writable linear projected histories;
- exact submitting-view commit continuity;
- hidden-only commit elision;
- cross-view synthetic representations;
- path-level read and write validation;
- stable logical `Change-Id` mappings;
- canonical compare-and-swap transactions;
- durable blocked/active outbox events;
- ordered at-least-once projection jobs;
- crash recovery, leases, retries, and ref adoption;
- serialized validated administrative canonical updates;
- focused and real-Git black-box tests;
- a read-only global invariant checker covering every view, ref, frontier, anchor,
  representation, and reachable object;
- deterministic property/state-machine tests with replayable seeds and an
  independent tree model;
- byte-exact tree-operation and unusual-path projection coverage;
- shared-visible modify/modify, delete/modify, and mode/content conflict tests;
- systematic rejection immutability for every major validation and barrier path;
- an experimental smart-HTTP endpoint with per-view token authorization,
  uniform unknown/unauthorized view responses, and protocol v2 support;
- principals (users and service accounts), immutable direct-member teams,
  immutable roles and role bindings, deterministic resolution, and
  content-addressed effective view descriptors bound to every view,
  captured on transactions, events, jobs, plans, anchors, and revisions;
- path-scoped capabilities (read, create, modify, delete, mode, symlink,
  branch, merge, admin, tag) with additive allow and deny maps adjudicated by
  longest match, an unfrozen catalog where additions stay dormant until
  quiescent `transition-principal` policy transitions that publish
  policy-adjustment commits, era-stamped anchors with stale-policy retention,
  explicitly non-revocable downloaded data, and admin-gated administrative
  canonical updates;
- metadata permissions: declared principal author identities enforced at
  receive time and re-verified by the checker, and a fixed future-dating skew
  window recorded as `received_at` evidence;
- production transaction storage: SQLite (default) and PostgreSQL backends
  behind one contract, versioned checksummed document rows with optimistic
  short transactions, PID-liveness lease sessions replacing the filesystem
  lock, idempotent receives with durable retry schedules, worker heartbeats,
  cancellation, dead-lettering, audit logging, observability stats,
  backup/restore, corruption detection, and legacy-JSON migration;
- an authenticated intake boundary: pushes land in per-view quarantine-
  separated intake repositories, published views are fetch-only behind a
  reject-all hook, receive hooks enforce pack/object/depth limits with
  stable rejection categories, accepted and rejected receives carry audited
  transport identity, and an SSH command shim exercises the same rules over
  a loopback backend (.specs/010-authenticated-intake-boundary.md).

The roadmap is ordered by architectural risk, not product visibility.

## Phase 1: Automated Model Confidence

Goal: make broader manual testing useful by checking invariants after arbitrary
operation sequences.

- [done] Add one global invariant checker covering every view, ref, frontier, anchor,
  representation, and reachable object.
- [done] Add deterministic property/state-machine tests generating canonical updates,
  user pushes, sync, retries, permission changes, worker crashes, and recovery.
- [done] Add real parallel-process push tests with barriers around validation,
  canonical CAS, source publication, worker claim, ref publication, and state
  persistence.
- [done] Add named failpoints and terminate processes at every durable transaction
  boundary.
- [done] Add full tree-operation tests: delete, rename, mode, symlink, directory
  boundary, non-UTF-8 names, tabs, newlines, and long paths.
- [done] Add shared-visible conflict tests: modify/modify, delete/modify, and mode/content.
- [done] Verify every rejected operation leaves authoritative refs and mappings
  unchanged.

Exit criteria:

- generated sequences preserve all documented security invariants;
- crash recovery equals uninterrupted execution;
- no state/ref divergence remains after retries;
- failing seeds become permanent regression tests.

## Phase 2: Permission Lifecycle

Goal: replace immutable view prefixes with capability-based authorization
captured in content-addressed, generation-stamped descriptors.

- [done] Define principals, teams, roles, service accounts, and immutable
  effective view descriptors (.specs/005-principals-roles-and-effective-view-descriptors.md).
- [done] Separate read, create, modify, delete, mode, symlink, branch, merge, and admin
  capabilities (.specs/007-capabilities-deny-rules-and-policy-transitions.md).
- [done] Add policy inheritance and more-specific deny rules.
- [done] Implement read and write revocation between clone and push.
- [done] Implement view expansion and contraction.
- [done] Define stale policy and stale anchor retention.
- [done] Keep previously downloaded data explicitly non-revocable.
- [done] Define metadata permissions for refs, authors, messages, timestamps, topology,
  tags, and `Change-Id` records (.specs/008-metadata-permissions.md).

Exit criteria:

- current policy authorizes every push effect;
- historical policy is used only to interpret an anchor;
- revoked paths cannot be changed or newly fetched;
- policy transitions have deterministic projected histories.

## Phase 3: Production Transaction Storage

Goal: replace JSON and the global filesystem lock.

- [done] Move repository, view, anchor, submission, logical change, outbox,
  job, and audit state to a real database: SQLite by default and PostgreSQL
  behind one identical contract (.specs/009-production-transaction-storage.md).
- [done] Use short database transactions and optimistic versions; saves are
  checksum-verified, version-guarded upserts scoped to mutated rows.
- [done] Implement partition scope keys (`global`, `canonical:<branch>`,
  `view:<name>`), PID-liveness lease stealing, and multi-process convergence
  tests proving unrelated scopes progress concurrently without loss;
  receive-path sequencing currently claims the global scope until
  multi-repository support arrives.
- [done] Add idempotency keys on the receive path and durable exponential
  retry schedules capped at ten minutes.
- [done] Add worker heartbeats, cancellation, dead-letter handling, audit
  logging, and a read-only `stats` command.
- [done] Add backups, restore verification, legacy-JSON migration,
  corruption detection naming tampered rows, and disaster-recovery tests on
  both backends.
- [done] Preserve Git-ref observation and forward-only recovery because
  PostgreSQL and Git refs cannot share one atomic transaction.

Exit criteria:

- unrelated repositories and views progress concurrently;
- one broken view never blocks unrelated work;
- database restore plus object storage restore reconstructs all refs and jobs.

## Phase 4: Real Git Server Boundary

Goal: replace filesystem-local endpoints with authenticated network services.

- [done] Add an experimental loopback smart-HTTP endpoint with per-view
  tokens as the transport boundary for security testing
  (.specs/006-smart-http-endpoint.md).
- [done] Receive pushes into a separate authenticated intake repository;
  published view repositories become fetch-only behind a reject-all hook,
  and rejected pushes never enter view or canonical storage
  (.specs/010-authenticated-intake-boundary.md).
- [done] Wrap receive-pack at the intake boundary with Git quarantine,
  explicit source publication, stable rejection categories, and pack
  size, object count, object size, depth, decompression, and timeout
  limits.
- [done] Keep `git-upload-pack` authorization-safe for smart HTTP protocol
  v2 (allow-listed protocol header, structural hidden-object safety) and
  prove the SSH command grammar through a loopback shim.
- [done] Bind audit identity to every accepted and rejected receive and
  token minting, with repository discovery that keeps unknown and
  unauthorized views indistinguishable.
- [done] Test direct OID wants of hidden canonical objects under protocol
  v0 and v2, advertisement cross-view leakage, thin packs whose bases live
  only in the view store, malformed framing, decompression bombs,
  identity-header spoofing, mirror tampering self-healing, and concurrent
  HTTP pushes (.specs/010-authenticated-intake-boundary.md).
- [ ] Re-run the want/thin-pack matrix against a streaming server and wire
      a real sshd entry point for production-style SSH.

Exit criteria:

- clients use normal Git over HTTP and SSH;
- rejected intake objects cannot enter published view storage;
- protocol behavior cannot retrieve hidden objects by ref or known OID.

## Phase 5: Object Storage And Git Compatibility

Goal: validate confidentiality through Git maintenance and advanced features.

- Test repack, garbage collection, commit graphs, bitmaps, multi-pack-index, and
  delta-base selection.
- Add safe anchor expiration and stale-clone behavior.
- Decide and implement SHA-256 repository support.
- Define shallow clone, deepen, and partial clone support.
- Define tags and signed tag projection.
- Preserve source signatures as source-view evidence and create server
  attestations binding submitted and canonical commits.
- Add Git LFS authorization.
- Evaluate shared authorization-aware object storage only after isolated view
  stores remain the reference security model.

Exit criteria:

- maintenance cannot make hidden objects reachable or usable as unauthorized
  delta bases;
- every supported Git capability has an explicit permission and protocol test.

## Phase 6: Review And Forge Product

Goal: build forge features on the proven repository boundary.

- Define logical change lifecycle, patchsets, ownership, abandonment, and merge.
- Add permission-aware repository browsing, history, blame, and diffs.
- Add review comments anchored to logical changes rather than unstable Git IDs.
- Add server-created rebase and merge workflows.
- Add status checks and protected branch rules.
- Add permission-filtered APIs, archives, webhooks, and notifications.
- Add CI with explicitly authorized source views and controls for logs, caches,
  artifacts, and secrets.

Exit criteria:

- every non-Git surface applies the same view and metadata policy;
- CI and webhooks cannot exfiltrate hidden state;
- review identity survives permitted rebases and cross-view representations.

## Phase 7: Scale And Operations

Goal: establish operational viability for large repositories and many views.

- Test at least 100,000 canonical commits and realistic merge-free branch
  histories under the initial restriction set.
- Test hundreds of views with overlapping permissions.
- Measure projection, validation, job latency, pack size, storage duplication,
  anchor growth, retry backlog, and recovery time.
- Add rate limiting, quotas, monitoring, tracing, alerting, and SLOs.
- Add algorithm-version migrations and deterministic cache invalidation.

Exit criteria:

- defined latency and storage targets are met under representative load;
- projection backlog and recovery are observable and bounded;
- an algorithm upgrade can run without ref or mapping ambiguity.

## Deferred Until Proven

- arbitrary client-created merge commits;
- force pushes;
- canonical merge topology projection;
- partial acceptance of unauthorized multi-commit pushes;
- shared canonical alternates as an endpoint security boundary;
- custom VCS replacement.

These features should not be implemented until a smaller secure subset is
operationally proven.
