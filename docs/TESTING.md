# Testing

Run the complete suite:

```bash
poetry install
poetry run python -m pytest tests -q
node --test tests/test_spa*.js
```

The repository engine and host tests require dependencies managed by Poetry:

```bash
python3 -m venv .poetry
.poetry/bin/pip install poetry
.poetry/bin/poetry install
.poetry/bin/poetry run python -m pytest tests/test_repository_server.py tests/test_host*.py -q
```

Run the complete suite through Poetry:

```bash
.poetry/bin/poetry run python -m pytest tests -q
```

Compile all Python entry points:

```bash
python3 -m py_compile forge.py src/*.py tests/*.py
```

## Coverage Areas

- `test_wal.py`, `test_invariants.py`: linked records, manifest CAS, object closure, and read-only checking.
- `test_permission_integrity.py`: descriptor v3 prefix semantics, captured administrative descriptors, received-time verification, backup descriptor reachability, and checker failure categories.
- `test_forge.py`, `test_cross_view.py`, `test_tree_operations.py`: projection and publication behavior.
- `test_rejection_immutability.py`, `test_adversarial.py`, `test_transport_adversarial.py`: fail-closed validation and hidden-object isolation.
- `test_identity.py`, `test_capabilities.py`, `test_metadata.py`: principals, descriptors, policy epochs, and metadata authorization.
- `test_http_server.py`, `test_http_security.py`, `test_transport_security.py`: authenticated intake and Git wire behavior.
- `test_repository_server.py`, `test_hosted_api_013.py`: repository-scoped security headers, Bearer-only API, bounded pagination, and Git smart-HTTP endpoints.
- `test_cloudflare_host.py`: real workerd SQL and authenticated Artifacts HTTPS, empty-host onboarding, cross-repo isolation, real first-push import, hidden-path read/write enforcement and cold recovery without durable disk.
- `test_durable_objects.py`: SQL revision fencing, staged commit/rollback, lost-response idempotency and portable catalog migration/export.
- `test_host*.py`, `test_first_push_import.py`, `test_repository_import*.py` and `test_spa*.js`: host authentication, discovery, provisioning, invitations and resumable Git imports including browser race handling. Pytest is required to discover function-based tests.
- `test_object_admission_015.py`: strict pre-publication object admission through real Git receive, HTTP, and the direct intake harness.
- `test_policy_fencing_015.py`: deterministic view-creation/policy-transition barriers and assignment-consistency enforcement in replay and the independent checker.
- `test_scope_matrix_015.py`: the route-by-scope credential matrix across browser, API, advertisement, and RPC surfaces.
- `test_bounded_transport_015.py`: bounded framing, gzip bombs, chunk attacks, slow clients, admission, Git deadlines with process-group teardown, and shutdown.
- `test_session_baselines_015.py`, `test_backup_consistency_015.py`: session baseline refresh, quiesced backup fencing, ahead-of-truth restore refusal/repair, and the full restore drill.
- `test_materialization_reuse_015.py`: immutable warm snapshots with zero historical copies and suffix materialization reuse across WAL appends.
- `test_bounded_api_015.py`: bounded history/tree pagination, batched Git metadata reads, and targeted provenance rows.
- `test_git_compatibility.py`: Git maintenance, protocol-version, exact-OID, and shallow-history compatibility.
- `test_storage.py`, `test_storage_adversarial.py`, `test_worker.py`: exact control-plane shape, leases, checksums, jobs, and concurrency.
- `test_process_failpoints.py`, `test_process_concurrency.py`: real-process crashes and publication races.
- `test_production_storage.py`: pinned backup and verified restore.
- `test_storage_correctness_014.py`, `test_projection_incremental.py`: snapshot counts, bounded hot paths, and the cold-vs-steady projection benchmark (measured artifacts printed and recorded under `.benchmarks/`).

Set `BASTION_TEST_PG` to run the storage matrix against PostgreSQL. Named failpoints use `BASTION_FAILPOINT`, `BASTION_FAILPOINT_ROOT`, `BASTION_FAILPOINT_RUN_ID`, and `BASTION_FAILPOINT_MARKER`; tests launch isolated child processes and verify manifest reachability after termination.

Rejected-operation tests compare repository manifests, object inventories, control-plane documents, refs, and visible histories before and after the request. Cache reconstruction tests delete local repositories and require deterministic replay from repository truth.

## Local PostgreSQL Matrix

Start the disposable PostgreSQL 16 service and wait for it to become healthy:

```bash
docker compose -f deployment/compose.postgres-test.yml up -d --wait
```

Run the storage backend matrix against SQLite and PostgreSQL:

```bash
BASTION_ADMIN_DATABASE=postgresql://postgres:postgres@127.0.0.1:55432/postgres \
  BASTION_TEST_PG=postgresql://postgres:postgres@127.0.0.1:55432 \
  .poetry/bin/poetry run python -m unittest tests.test_storage -v
```

Exercise the same restricted runtime-role provisioning used by AWS bootstrap:

```bash
BASTION_ADMIN_DATABASE=postgresql://postgres:postgres@127.0.0.1:55432/postgres \
  BASTION_DATABASE=postgresql://bastion_runtime:runtime-test-password@127.0.0.1:55432/postgres \
  .poetry/bin/poetry run python deployment/provision-postgres.py \
  /var/lib/bastion/forge
```

Stop the service and discard its tmpfs-backed databases:

```bash
docker compose -f deployment/compose.postgres-test.yml down
```

Set `BASTION_TEST_PG_PORT` before both commands if port `55432` is unavailable, and use the same port in both DSNs. `BASTION_ADMIN_DATABASE` is the explicit one-shot provisioning connection. `BASTION_TEST_PG` is a server base URI without a database path; the harness derives and persists a separate database name for each temporary forge. The test account can create those databases; these credentials and the Compose file are for local testing only.

## CI And Test Boundaries

The ordinary CI workflow is hermetic with respect to cloud services. It runs the complete suite on Python 3.11 and 3.12, runs the storage matrix against an ephemeral PostgreSQL service, validates Terraform without configuring a remote backend, builds the Docker image, and validates both Compose files. Unit tests use memory, filesystem, SQLite, PostgreSQL, or injected fakes and do not establish compatibility with AWS S3.

Live AWS behavior is intentionally outside pull-request CI. The manually dispatched, environment-protected workflow is described in [S3 qualification](S3_QUALIFICATION.md). A green ordinary CI run is not evidence that the S3 adapter is qualified against AWS.
