# Testing

Run the complete suite:

```bash
python3 -m unittest discover -s tests -t . -v
```

The demo server tests require Flask, which is managed by Poetry:

```bash
python3 -m venv .poetry
.poetry/bin/pip install poetry
.poetry/bin/poetry install
.poetry/bin/poetry run python -m unittest tests.test_demo_server -v
```

Run the complete suite through Poetry:

```bash
.poetry/bin/poetry run python -m unittest discover -s tests -t . -v
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
- `test_demo_server.py`: Flask-hosted permission demo UI, capability evaluation, projected browsing, and Git smart-HTTP endpoints.
- `test_git_compatibility.py`: Git maintenance, protocol-version, exact-OID, and shallow-history compatibility.
- `test_storage.py`, `test_storage_adversarial.py`, `test_worker.py`: exact control-plane shape, leases, checksums, jobs, and concurrency.
- `test_process_failpoints.py`, `test_process_concurrency.py`: real-process crashes and publication races.
- `test_production_storage.py`: pinned backup and verified restore.

Set `BASTION_TEST_PG` to run the storage matrix against PostgreSQL. Named failpoints use `BASTION_FAILPOINT`, `BASTION_FAILPOINT_ROOT`, `BASTION_FAILPOINT_RUN_ID`, and `BASTION_FAILPOINT_MARKER`; tests launch isolated child processes and verify manifest reachability after termination.

Rejected-operation tests compare repository manifests, object inventories, control-plane documents, refs, and visible histories before and after the request. Cache reconstruction tests delete local repositories and require deterministic replay from repository truth.
