# Roadmap

## Initial MVP: hosted, permission-aware code review

**Working definition (confirm or adjust before expanding scope):** a repository
owner can create or import a private repository, grant another account explicit
repository and branch permissions, and collaborate through feature branches and
pull requests (PRs). An explicitly authorized reviewer can inspect the PR's
projected changes; an authorized integrator can safely integrate it; both can
fetch the resulting branch. Unauthorized users must not learn PR existence or
its metadata through any UI, API, Git ref, count, notification, or error.

This is a small collaboration MVP, not GitHub feature parity. MVP means the
following end-to-end flow works on the supported hosted deployment, with tests
for authorization boundaries, stale requests, and recovery:

1. An approved account creates a private repository or imports a bounded
   single-branch history.
2. The owner explicitly grants a collaborator repository PR rights and the
   branch rights needed for their role. The collaborator clones/fetches and
   pushes a feature branch within those grants.
3. An authorized contributor creates a PR against a named target branch.
   Authorized reviewers can list and inspect it, including its projected
   patchset, without gaining access to unrelated paths or branches.
4. The PR can be closed or safely integrated into its target. Stale targets,
   conflicts, and unauthorized operations fail closed; integration is
   idempotent and does not lose accepted data. Collaborators can fetch the
   resulting authorized ref.
5. Owners can manage grants and revoke access. Policy changes take effect
   across credentials, sessions, and repository views; whole-host recovery and
   documented operations are adequate for the intended pilot.

### Implemented foundation

The following is implemented; do not reopen it as MVP foundation work:

- Independent private repositories, account sessions, browser discovery,
  creation/switching, scoped collaborator discovery, and namespaced HTTPS
  clone/push (local and Cloudflare host paths).
- Bounded single-branch first-push import and pending-operation recovery,
  including digest-pinned Artifacts intake for cold recovery.
- Immutable object-store WAL repository truth selected by manifest CAS;
  disposable canonical/projected/intake Git materializations; deterministic
  path projection and capability authorization.
- Exact submitting-view commit continuity, synthetic cross-view
  representations, `Change-Id` ownership across Git representations, synchronous
  receive validation, immutable canonical/evidence packs, WAL-backed request
  idempotency, and crash-safe publication.
- Optional projection jobs; SQLite, PostgreSQL, and Cloudflare Durable Object
  control-plane indexes with leases/checksums; pinned-generation repository
  backup and verified restore; read-only invariant checker.
- Smart HTTP with per-view tokens and isolated intake repositories.
- Durable canonical refs/frontiers and branch state independent of disposable
  materializations; private multi-ref projection with exact reachable closure
  and no hidden-object wants.
- Capabilities-v4 explicit branch `read`, `create`, and `update` enforcement,
  including default-branch enforcement; v3 remains default-branch compatible
  and denies secondary refs. Narrow single-ref linear receive safely creates
  from the current advertised projected tip. See [spec 018](.specs/018-multi-branch-foundation.md),
  [spec 019](.specs/019-scoped-permissions.md), and
  [spec 020](.specs/020-scoped-branch-transport.md).
- Integration, adversarial, concurrency, and failpoint coverage. At the
  2026-10-13 checkpoint (baseline commits `f6bc354` branch foundation and
  `1d70b70` scoped permissions + branch transport): 863 tests passed, 1
  skipped (347 subtests), and all 4 browser tests passed.

These are not implemented merely because policy actions exist: there is no
complete PR lifecycle yet. In particular, `pr_view`, `pr_create`, and
`pr_merge` policy actions do not constitute PR storage, APIs, UI, review, or
integration behavior.

### Remaining MVP work (priority order)

#### P0. Agree and specify PR semantics before implementation

- Write the next contract (suggested `.specs/021-pull-requests.md`) covering
  PR identity, source/target repositories and refs, patchset snapshots, status
  transitions, authorization, concurrency, recovery, and audit evidence.
- Decide the minimal lifecycle: proposed default is `open` -> `closed` or
  `merged`, with immutable patchset identities and explicit source/target
  canonical tips. Treat that as a proposal, not an already-agreed product
  decision. Draft state, reopen, comments, approvals, and notifications need
  not block MVP unless requirements say otherwise.
- Define a durable WAL/manifest-backed source of truth for PR records and
  transitions. Do not make a disposable projection or independently
  authoritative SQLite catalog the sole authority. Specify idempotency,
  CAS/stale-tip behavior, crash recovery, and audit records.
- Explicitly choose a bounded safe integration strategy. Recommended MVP
  candidates: server-side fast-forward only, or server-created squash with
  snapshot bounds and tested source/target mappings. Decide whether to defer
  conflicts and merge commits; never silently promise arbitrary merge/rebase
  history. Define how branch-aware `Change-Id` ownership and canonical to
  projected OID mappings work across source/target views. Reject stale tips
  and conflicts without data loss; state exactly which OID continuity is
  promised when fetching the integrated result.

#### P0. Build the permission-closed PR lifecycle

- Implement authorized create, list, detail, patchset, close, and chosen
  integration operations. Map each operation to its corresponding policy
  action: current actions are only `pr_view`, `pr_create`, and `pr_merge` (there
  is no `pr_close`). Decide whether close uses a new explicit action or an
  existing narrowly scoped authority; likewise specify who may refresh/add a
  patchset and which `pr_view` checks apply to every read surface. Enforce the
  selected action with repository scope and source/target branch checks; do not
  require all three actions for every operation.
- PR access must be an explicit grant: no owner/admin, path-read, service
  account, branch permission, or existence of an endpoint implicitly grants
  `pr_view`. PR permission alone grants no file/path/branch access. A PR's
  projected diff must not reveal denied paths, object IDs, names, counts, or
  metadata. Apply the same rules to pagination, errors, refs, APIs, and
  secondary surfaces.
- Add adversarial tests for unauthorized create/list/read/diff/close/merge,
  cross-repository and cross-branch cases, enumeration, revoked/stale grants,
  hidden paths, stale tips, retries, failpoints, and cold recovery.

#### P0. Make grants and the workflow usable in the browser

- Add an explicit owner workflow to grant/revoke repository PR rights and
  branch `read`/`create`/`update` rights to invited collaborators. The current
  filepath-role invitation UI does not replace this scoped-grant workflow;
  scoped rights currently require operator CLI/configuration.
- Add a PR list/detail/create flow and projected patchset diff for authorized
  reviewers; expose only authorized refs in branch selection. Add clear empty,
  denied, stale, and conflict states without leaking hidden names or OIDs.
- Support intentional v3-to-v4 policy migration and safe policy contraction.
  Principal transitions/revocation must fence credentials and in-memory views
  across repositories/branches, not only disable account membership.
- Test the complete owner -> collaborator -> PR -> review -> integration ->
  fetch flow in browser and Git clients.

#### P0 for hosted pilot: operational readiness

- Verify the intended deployment's Clerk/auth configuration, TLS, and
  production account approval/invitation process. Cloudflare serving paths
  already exist; this is rollout/verification, not a new hosting implementation.
- Add or document coordinated deprovisioning and revocation for every
  repository token/policy/view. Membership disable alone is not revocation.
- Establish distributed admission, quotas/rate limits, monitoring and basic
  audit/alerts. For an internal pilot, document operator procedures and strict
  resource caps for failed imports and staged sources. Fully automated
  self-service cancellation and staged-source garbage collection are post-MVP.
  Verify whole-host backup/restore, including host/account/catalog
  metadata (current repository backup alone is not a whole-host recovery plan),
  with a cold recovery drill for the actual deployment.
- A tightly scoped internal pilot may use documented manual procedures for
  rollout and operations; do not claim production-wide readiness until these
  gates are verified.

### Suggested next-session entry point

1. Read this section and specs 018-020, then inspect `src/permissions.py`,
   `src/identity.py`, `src/wal.py`, `src/repository.py`,
   `src/branch_projection.py`, `src/materialization.py`, `src/protocol.py`,
   `src/repository_server.py`, and `templates/spa/app.js`. Start with
   `tests/test_branch_transport_end_to_end.py`,
   `tests/test_branch_transport_invariants.py`, `tests/test_permission_api.py`, and
   `tests/test_spa*.js` for existing behavior and test patterns.
2. Agree the open choices in the PR semantics section (especially lifecycle and integration
   strategy) and write `.specs/021-pull-requests.md` with authorization and
   recovery acceptance tests before building the PR feature.
3. Implement a vertical slice: durable PR record + explicit authorization +
   projected patchset read, then browser create/list/detail; add the selected
   integration path only after its cross-view mapping and recovery tests.
4. Re-run focused security/recovery tests, the full suite, and browser tests;
   update this roadmap from verified behavior.

   Validation commands: `.venv/bin/python -m pytest tests -q` and
   `node --test tests/test_spa*.js`.

For MVP, propose same-repository source and target branches. Cross-repository
fork PRs are post-MVP unless the PR spec explicitly chooses to include them.

## Post-MVP: compatibility and broader forge

Keep these separate from the initial workflow unless product requirements
change:

- Remote URL imports, raw-history owner-view compatibility, import
  cancellation, and staged-source garbage collection.
- PR comments, approvals, notifications/webhooks, archives, blame, and richer
  history browsing; protected branches, status checks, CI and artifact access.
- Branch deletion, arbitrary-history bases, arbitrary client-created merge
  commits, force pushes, multi-ref receives, tags, shallow/partial clone,
  SHA-256, SSH, and Git LFS.
- Broader compatibility/performance work: [Git compatibility matrix](docs/GIT_COMPATIBILITY.md) (repack, GC,
  commit graphs, bitmaps, MIDX, protocol versions, exact OID wants), cruft and
  stale-index recovery, adversarial delta-base tests, large histories and many
  overlapping views, pack compaction, format upgrades, and measured SLOs.
- Production SSH entry point and broad GitHub workflow compatibility.

## Product and security invariants

- Repository discovery is not authorization; cached identity is not permanent
  authority. Administrative rights on one repository do not grant host-wide or
  cross-owner access.
- Policy must restrict both Git objects and browser/API metadata. PR visibility
  is explicit and is not implied by path, branch, owner, or service-account
  access.
- Keep arbitrary client-created merge commits, force pushes, partial acceptance
  of multi-commit pushes, and shared canonical alternates as an endpoint
  security boundary deferred unless a later spec explicitly changes that scope.
