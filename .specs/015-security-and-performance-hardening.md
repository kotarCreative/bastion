# Security And Performance Hardening

Status: executed on the September 6, 2026 working tree (Git 2.55.0, CPython
3.14.6, Linux). All eleven work packages plus verification are implemented
with the regression coverage listed under each acceptance item; the full
existing unit/integration suite passes (487 tests, 236 subtests) plus the new
spec regression files. Recorded benchmark artifacts live under
`.benchmarks/`. Remaining release prerequisites are disclosed at the end of
this file.

## Contract

Bastion rejects inputs that cannot be safely materialized before committing
them, prevents concurrent administration from restoring revoked permissions,
enforces scopes on every authenticated surface, and bounds request processing
before consuming excessive resources. Backup and session behavior remain
consistent under supported operation schedules. Warm reads and incremental
updates avoid work proportional to unrelated historical data.

This specification converts the September 5, 2026 code review into executable
work. The findings describe the working tree reviewed that day, including
uncommitted hosted API/token changes; they are not claims about a release.
Reproduce against the execution-time tree before changing code. Preserve
already completed work and turn closed findings into regression coverage.

## Dependencies And Scope

- [011: Repository truth](011-wal-object-store-repository-truth.md) owns manifest
  CAS, WAL, immutable packs, and recovery semantics.
- [012: Permission integrity](012-permission-integrity-and-verifiable-authorization.md)
  owns descriptor semantics and independent authorization verification.
- [013: Hosted demo](013-hosted-permission-demo.md) owns the browser, token
  scopes, request/process limits, security headers, and hosted resource defaults.
- [014: Storage correctness](014-storage-correctness-and-bounded-hot-paths.md)
  owns storage sessions, immutable snapshots, targeted reads, and projection
  optimization.

This spec adds regressions and closes observed implementation gaps in those
contracts. It does not replace spec 013's page-memory-only credentials with
cookie sessions, expand the hosted deployment beyond one Linux node, or
introduce an online distributed backup guarantee. Hosted numerical limits and
HTTP/Git error semantics come from spec 013 rather than a second configuration
profile. A temporary legacy route retained during implementation must enforce
the same authorization as its replacement; final hosted routes follow spec 013.

## Invariants To Preserve

- Manifest CAS remains the only repository commit point.
- Validate every submitted transition, including intermediate commits whose
  effects are later reversed; preserve hidden canonical state and exact
  submitting-view commit IDs.
- Active view object stores contain exactly the authorized reachable closure,
  with no canonical alternate or hidden/dangling objects.
- Authorization uses committed descriptor assignments; derived database rows,
  cursors, caches, and worker jobs never grant authority.
- Validate new generations before activation. Never mutate an activated view's
  Git objects or refs to implement an optimization.
- Cache absence or contradiction triggers verified reconstruction or a stable
  failure. The independent checker never treats cache assertions as evidence.
- Preserve CAS retry/idempotency semantics, lease ownership, checksums, and
  detection of stale and ABA writes.

## Execution Order

| Phase | Work | Completion gate |
| --- | --- | --- |
| A | Object admission, policy fencing, scope enforcement | Malformed pushes and authorization races cannot commit/expose invalid state |
| B | Streaming, admission budgets, browser credentials | Spec-013 security/resource regressions pass |
| C | Backup consistency and session baselines | Restore drill and repeated-save/concurrent-write tests pass |
| D | Immutable snapshots and suffix materialization | Warm and unrelated-change operations avoid historical copying/reconstruction |
| E | Bounded API traversal and batched projection/reconciliation | Operation-count, memory, and reference-equivalence checks pass |

Keep changes reviewable by work package. For each package, add a failing
behavioral regression, implement the fix, and run the affected existing tests.
Do not weaken correctness assertions to meet performance targets.

## A1. Validate Submitted Objects Before Publication

Entry points: `src/materialization.py:initialize_intake`,
`src/protocol.py:prepare_submission`, `src/artifacts.py`.

Evidence: a commit with two `author` headers failed strict Git fsck with
`multipleAuthors`, but an intake push succeeded and advanced WAL sequence 3 to
4. The next view resolution failed materialization validation. Enabling
`receive.fsckObjects=true` in a fresh fixture rejected the same push and left
sequence 3 fetchable.

Requirements:

- Enable receive-side object checking for every created or reused intake.
- Ensure admission rejects object defects that would fail required strict
  post-publication materialization checks. Pack checksums alone are insufficient.
- Perform required checks in quarantine before immutable submission/canonical
  uploads and manifest CAS; do not rely on a later fetch discovering corruption.
- Preserve safe Git protocol rejection behavior and redact internal details.

Acceptance: duplicate-author and other selected malformed-object cases reject
through actual Git receive, leave manifest and published refs unchanged, and
permit a subsequent valid push and fetch. Exercise both HTTP and the local
intake harness, fresh/reused intake generations, and replay with caches removed.

## A2. Fence View Creation Against Policy Transitions

Entry points: `src/service.py:add_view`, `transition_principal`,
`src/wal.py:apply_record`, `src/invariants.py`.

Evidence: creation captures a descriptor before publication. Interleaving a
real permission transition after capture but before publication lets the new
view retain a file revoked from the principal's existing view.

Requirements:

- Resolve and validate the principal's assigned policy against the same WAL
  snapshot used to build each creation candidate, including every CAS retry.
- If an intervening transition invalidates the captured assignment, rebuild
  against the committed assignment or reject with a stable conflict.
- Preserve dormant catalog semantics: adding a catalog role alone does not
  silently change the assignment used by an existing principal's new view.
- Enforce assignment consistency in replay and the independent checker, not
  only the administrative entry point. Use existing WAL-derived assignment
  information where sufficient; document any necessary persistent-shape change.

Acceptance: deterministic barriers cover creation before/after revocation,
creation losing CAS to a transition, and multiple creations for one principal.
After both operations finish, no newly committed view exposes the revoked path.
An existing client retaining previously downloaded objects is not a failure.

## A3. Enforce Scopes On Every Surface

Entry points: `src/demo_server.py`, `src/http_server.py`, `src/tokens.py`.

Evidence: a push-only credential receives 403 from `/api/v1/session` but can
log in and read `/blob?path=visible/file.txt&raw=1`.

Requirements: centralize token validation and required-scope enforcement;
authenticate before replay, materialization, or Git execution. Remove legacy
browser routes outside the final spec-013 surface. While retained, they require
`ui` and never accept a push-only credential for content or policy access.

Acceptance: a route-by-scope matrix covers fetch-only, push-only, ui-only,
combined, malformed, expired, revoked, and cross-view credentials for browser,
API, advertisements, and RPCs. Unauthorized requests do no repository work.
Preserve unknown-view/cross-view indistinguishability and next-request revocation.

## B1. Bound HTTP Reads, Decompression, And Child Processes

Entry points: `src/http_server.py:decode_body`, `_read_chunked`, `run_service`,
`src/demo_server.py`, `src/git.py:run`, `src/protocol.py:enforce_pack_limits`,
`src/projection.py:IncrementalProjector._store_blobs`.

Evidence: 8,175 gzip bytes expanded to 8 MiB and approximately 21.4 MiB traced
allocation before rejection under a 1 MiB cap. A negative chunk size reached
`read(-1)` and consumed over 1 MiB under a 1 KiB cap. The decisions route reads
under the global cap before checking its 16 KiB limit. RPC output is buffered,
generic Git calls lack deadlines, and 50-object projection batches do not bound
expanded bytes.

Requirements:

- Implement spec-013 strict framing, including rejection of ambiguous length
  headers, signed/invalid chunk sizes, short reads, and malformed CRLF; bound
  chunk/trailer counts, headers, bytes, and total request duration.
- Enforce route-specific limits while reading. Incremental gzip decoding must
  enforce cumulative output/ratio caps before allocating the complete output.
  Handle concatenated, truncated, and trailing compressed data explicitly.
- Stream request and response bodies with bounded buffering and backpressure;
  drain bounded stderr concurrently. Avoid replacing memory exhaustion with
  unbounded temporary-file growth.
- Bound active requests and Git jobs, with tighter receive limits and per-IP/
  token admission. Apply budgets before replay/materialization as well as RPCs.
- Give Git work explicit deadlines appropriate to its operation. RPC process
  groups include hooks and descendants; timeout, disconnect, shutdown, and
  exceptions release slots and terminate/reap owned processes.
- Enforce spec-013 aggregate expanded-object, disk-reservation, and event-budget
  admission. Count object bytes before loading contents; bound transfer batches
  by bytes, including one large object. Clean staging promptly.
- Validate resource configuration at startup and preserve the prescribed
  before/after-response-header HTTP and Git outcomes.

Acceptance: bounded-size adversarial fixtures cover gzip bombs, negative
chunks, ambiguous headers, excessive trailers, slow clients, large stdout/
stderr, saturation, disconnect, timeout, and SIGTERM. Assert maximum read/output
block sizes, cumulative limits, bounded RSS, slot release, and no surviving
descendants. Verify rejected receives leave repository truth unchanged and that
exhausted resource budgets fail safely before accepting unrecoverable work.

## B2. Complete Browser Credential And Header Requirements

Entry points: `src/demo_server.py`, `templates/demo/`, spec-013 browser assets.

Evidence: the current signed Flask cookie contains the complete bearer token
and lacks `Secure`; signing does not encrypt its contents.

Requirements: implement spec 013's page-memory-only bearer flow; remove cookie
authentication and raw-token session storage. Clear the input after login,
discard credentials on reload, use token-free remote URLs, and implement the
specified security headers and no-store responses. Render untrusted content
safely. Persistent server/browser sessions are outside this work.

Acceptance: browser tests inspect cookies, local/session storage, URLs, DOM,
logs, and telemetry for credentials; none may persist the token. Reload logs
out. Hostile path/metadata display tests and HTTPS/header checks pass.

## C1. Enforce Backup Consistency And Verify Restore Usability

Entry points: `src/store.py:backup_forge`, `_backup_repository`,
`_restore_repository`, `src/state.py:recover`.

Evidence: a publication and index recovery between the WAL pin and database
dump produced a backup that restored successfully with WAL sequence 3 and index
sequence 4. The next catalog write failed with an ahead-of-truth error. The
documented quiesced backup workflow was not the failing scenario.

Requirements:

- Enforce the supported quiesced workflow: block/drain mutating participants
  before snapshot capture, or refuse backup without a verifiable quiescence
  condition. Include WAL publication, catalog/token changes, and derived-index
  writers. A database lease ignored by receives is not a complete barrier.
- Record fence metadata from the database snapshot actually exported, not an
  independently loaded earlier document.
- Validate repository identity and the database/WAL fence before accepting a
  restored target. Behind-head derived rows may be rebuilt from the pinned WAL.
  Ahead-head or otherwise inconsistent snapshots must fail cleanly unless an
  explicit validated repair reconstructs only derived state.
- Never discard or invent authoritative catalog/token state to hide an
  inconsistent snapshot. Preserve transactional token audit and lease removal.
- A failed restore leaves no accepted partial target and does not damage an
  unrelated/pre-existing database. Online distributed snapshots remain deferred.

Acceptance: barrier-controlled publication/index/token changes during backup
are excluded or rejected. Test equal, behind, ahead, and wrong-identity fences.
The restore drill checks invariants, authenticated clone/fetch, a catalog write,
and a permitted push on a throwaway target; successful return alone is not proof.

## C2. Refresh Session Baselines After Successful Saves

Entry points: `src/state.py:locked`, `save_state`, `src/store.py:save_document`.

Evidence: creating and saving a principal, then adding its email and saving
again within one locked session conflicts with the session's own first write.
Loaded row versions can also precede recovery writes.

Requirements: after commit, refresh the baseline and exact committed versions
for affected rows, including recovery. Do not use a later unprotected read that
could adopt another writer's versions. A failed save leaves the last committed
baseline intact; unchanged tables need no full reload.

Acceptance: repeated insert/update/delete and no-op saves work within one
session; recovery followed by a write works. Concurrent updates, deletions,
creation collisions, lease loss, and ABA changes still conflict atomically.

## D1. Return Immutable Snapshots Without Historical Copies

Entry points: `src/repository.py:Repository.load`, `src/wal.py:ReplayState`,
request resolution in `src/http_server.py` and `src/demo_server.py`.

Evidence: unchanged loads deep-copy all replay records/indexes. A synthetic
probe using independent copies of a real record allocated approximately 1.3 MB
at 1,000 records and 13 MB at 10,000; these are allocation observations, not
production latency measurements.

Requirements: fulfill spec 014's immutable reader snapshots and private writer
state. Warm read resolution must not traverse/copy historical records or
indexes; reuse one pinned request snapshot for compatible phases. Preserve
conditional manifest validation, suffix checks, independent roots, and cache
eviction correctness. Do not expose mutable cached dictionaries to callers.

Acceptance: mutation attempts cannot alter another reader or future load.
Compare 1,000/10,000-record warm reads with unchanged active-view data: zero
historical record copies/visits and existing conditional-I/O gates hold. Record
CPU and allocations as well as I/O. Test concurrent snapshot advancement and
writer isolation. Synthetic history fixtures do not override hosted event caps.

## D2. Reuse Safe Materializations Across WAL Suffixes

Entry points: `src/materialization.py`, `src/worker.py`, `src/artifacts.py`.

Evidence: creating Carol's view caused Alice to build a new generation despite
an unchanged projected tip; stale canonical generations reinstall all packs.

Requirements:

- Separate the verified WAL frontier from projected content identity. Validate
  relevant suffix events rather than rebuilding from the epoch on every global
  head change. A proved unaffected view reuses its Git generation.
- For changed views/canonical history, reuse verified immutable objects in a
  private candidate and install required new data without recopied full history.
- Keep progress receipts outside immutable active Git contents where necessary;
  bind them to descriptor, epoch, frontier, and generation, and publish safely.
- Do not reuse revoked history across a policy epoch. Full validation and exact
  closure remain required before activating newly constructed view generations.
- Stale work cannot replace a newer active generation or claim newer progress.
  Existing readers can finish on their captured generation.
- Enforce spec-013 growth budgets. General WAL/pack/generation garbage
  collection is deferred; do not delete generations without a reader-safe design.

Acceptance: unrelated view creation and hidden-only updates preserve the
unaffected tip and avoid generation creation/object transfer. Visible changes
use suffix work. Test policy transitions, stale workers, crashes around receipt/
activation publication, restart, damaged receipts, and full cache deletion.
Retain direct hidden-object and dangling-object rejection tests.

## E1. Bound API Traversal And Batch Metadata Reads

Entry points: `src/api_v1.py`, `src/demo_server.py`.

Requirements:

- Validate query/path/cursor syntax and integrity before expensive traversal;
  resolve the current tip once and bind the remaining work to that snapshot.
- Use an integrity-protected next-commit cursor and at most `limit + 1` commits
  per history page; never enumerate full history then slice an offset.
- Preserve cursor scope, tip/epoch/restart invalidation, byte-safe paths, and
  exact API envelopes from spec 013.
- Use batched Git metadata/size reads. Tree pages must not recursively enumerate
  unrelated subtrees; use immutable tree/descriptor-bound summaries or an index
  where raw Git tree encoding requires initial directory enumeration. Bound and
  measure that cold construction separately from page reads.
- Fetch only required principal/provenance rows for a response; adding unrelated
  catalog rows must not cause full-table reads. Captured descriptor provenance
  remains distinct from dormant live catalog additions.

Acceptance: first/subsequent history pages visit only their bounded commit
range. Tree page work does not scale with unrelated descendants. Git metadata
process counts are batched, not one per entry. Test malformed/stale/cross-view
cursors, concurrent publication, empty pages, unusual bytes, and hidden paths.

## E2. Remove Quadratic Object Lookup And Whole-Tree Push Work

Entry points: `src/projection.py:IncrementalProjector._store_blobs`,
`src/reconciliation.py:reconcile_commits`, `src/git.py`.

Requirements: use sets/indexed membership for object existence, transfer each
required distinct object at most once per destination construction, and stream
under B1's byte budget. Reuse parsed state across submitted commits and apply
changed paths; eliminate per-blob subprocess pairs and repeated full-tree
round-trip copies. Preserve every per-commit authorization/collision/round-trip
check and reference-equivalent tree IDs, commit IDs, and object inventories.

Acceptance: benchmark one-file changes against increasing unchanged trees,
including the spec-014 10,000-file/100-commit/four-view fixture. After initial
validation, unchanged-file growth must not add full-tree scans or per-file Git
processes for the same changes. Count object lookups, subprocesses, bytes,
staging disk, and peak RSS; include large blobs, not only tiny text files.

## Verification And Handoff

The review exercised 154 distinct existing tests: 153 passed and one assertion
failed in
`DeterministicBenchmarkTest.test_unchanged_file_growth_does_not_increase_subprocess_counts`
(`tests/test_projection_incremental.py`): 109 versus 289 processes for 2,000
versus 20,000 files. It includes cold construction, where 50-object transfer
batches scale with initial data. Separate cold-transfer budgets from steady
incremental-work gates; do not simply relax or delete the test. Report measured
timings rather than asserting portable latency thresholds.

Convert the evidence above into repository tests; execution must not depend on
temporary review scripts or files under `/tmp`. Use deterministic barriers for
races, small bounded malicious fixtures, and temporary forge roots only.

Before completion:

- Run the complete existing unit/integration suite plus each new regression.
  Run socket-dependent tests in an environment permitting loopback binding.
- Run the independent checker and reference-equivalence tests, crash/CAS retry
  tests, and hosted security/resource tests affected by the implementation.
- Exercise shared storage/session/backup changes on SQLite and live PostgreSQL
  where supported. The review did not test live PostgreSQL; mocked tests do not
  establish backend qualification. Record unavailable environments explicitly.
- Record benchmark environment, warm/cold operation counts, memory, disk, and
  timing results as reproducible artifacts.
- Update security, architecture, testing, and deployment/backup documentation
  for final behavior. Record any persisted-format compatibility decision before
  changing WAL shapes; never silently rewrite an existing deployment.
- Mark phases complete only with test evidence. Disclose remaining release
  prerequisites rather than treating this spec as proof of production readiness.

## Execution Checklist

- [x] A1: strict pre-publication object admission
- [x] A2: policy-transition/view-creation fencing
- [x] A3: authorization scope matrix across all routes
- [x] B1: bounded framing, streaming, processes, memory, and disk admission
- [x] B2: memory-only browser credentials and response protection
- [x] C1: enforced backup consistency and usable restore drill
- [x] C2: repeated-save and recovery baseline correctness
- [x] D1: immutable snapshots without historical read copies
- [x] D2: verified suffix materialization and unaffected-view reuse
- [x] E1: bounded pagination and batched/targeted metadata access
- [x] E2: linear object lookup and incremental reconciliation
- [x] Full regression results, benchmark artifacts, and documentation handoff

## Execution Notes And Remaining Release Prerequisites

- A1 was reproduced against the execution-time Git (2.55.0): duplicate
  `author` headers are accepted by this Git's strict fsck, so the forge now
  enforces exactly-one-author/committer headers itself (both through
  `receive.fsckObjects` on every created or reused intake and a forge-level
  header check inside quarantine admission). Missing author/committer, bad
  parent, and malformed-header cases reject through real receive; the
  poisoned-store scenario cannot ship because admission runs before any
  immutable upload or manifest CAS.
- B1's free-space gate uses the spec-013 defaults (1 GiB floor + 512 MiB
  reserve, env-overridable). Test environments with less free disk can set
  `BASTION_FREE_FLOOR_BYTES`/`BASTION_RESERVE_BYTES`; the tests patch the
  limit directly so the suite stays environment-independent.
- Browser-level acceptance for B2 (real cookie/storage/DOM inspection in a
  live browser) was implemented as static asset assertions plus server-side
  contract tests; no browser automation runner exists in this repository, so
  a manual browser pass or a Playwright harness remains a release
  prerequisite for full B2 evidence.
- Live PostgreSQL qualification was not performed: `BASTION_TEST_PG` was
  unavailable in the execution environment, so C1/C2 storage behavior is
  qualified on SQLite only. The postgres fence readers are implemented but
  untested against a live server; record that before relying on them.
- Benchmark artifacts under `.benchmarks/` record this environment's
  measured timings and process counts; they are not portable thresholds.
- Production readiness still requires the items in ROADMAP.md (TLS, real
  sshd integration, protected storage, production identity, general
  WAL/pack/generation GC) — this spec is not proof of production readiness.
