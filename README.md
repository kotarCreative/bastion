# Bastion

Bastion is an experimental permission-aware Git forge. Repository truth is an
immutable object-store WAL chain selected by one conditional `manifest.json`;
local canonical and projected Git repositories are disposable caches. Users
fetch only permission-bounded projected repositories with an unmodified Git
client.

The prototype demonstrates that this model is feasible with explicit
restrictions:

- submitted history is linear;
- principals (users and service accounts), immutable teams, roles, and
  bindings resolve to content-addressed effective view descriptors;
- every authorization decision uses the descriptor captured in the committed
  WAL event; view assignment generations are distinct from the immutable
  descriptor captured for administrative updates;
- accepted commits retain their exact Git IDs in the submitting view;
- other views receive synthetic commits only for changes visible to them;
- hidden-only canonical commits do not move an unaffected projected ref;
- every tree transition is authorized before immutable pack upload and manifest
  CAS;
- accepted pushes are one linked WAL event and retries are resolved from WAL;
- view materialization and database indexes are rebuildable; worker jobs are
  only freshness hints;
- logical `Change-Id` metadata links different Git representations;
- durable control-plane state lives in SQLite (default) or PostgreSQL as
  versioned, checksummed document rows guarded by lease-based sessions.

This is a local architecture experiment, not a production security boundary.
Authentication, network transports, and the wider forge surface remain
roadmap work.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Transaction and event model](docs/TRANSACTIONS.md)
- [Security model](docs/SECURITY.md)
- [Testing and local operation](docs/TESTING.md)
- [Roadmap](ROADMAP.md)

## Quick Start

Requirements:

- Python 3.11 or newer
- Git with SHA-1 repository support

Run all tests:

```bash
python3 -m unittest discover -v tests 'test_*.py'
```

Create a local forge, one principal, and one view:

```bash
python3 forge.py init /tmp/bastion-forge /path/to/source --branch main \
  --object-store filesystem
python3 forge.py add-principal /tmp/bastion-forge alice --email alice@example.test
python3 forge.py add-role /tmp/bastion-forge alice-grants \
  --allow read=shared --allow read=team-a \
  --allow modify=shared --deny read=team-a/private
python3 forge.py bind-role /tmp/bastion-forge alice-grants --principal alice
python3 forge.py add-view /tmp/bastion-forge alice --principal alice
git clone --no-local /tmp/bastion-forge/views/alice.git alice
```

Roles grant and deny path-scoped capabilities (`read create modify delete mode
symlink branch merge admin tag`); ten capabilities are adjudicated by longest
matching prefix, with equal specificity denied. Catalog additions stay dormant
until an explicit transition applies them:

```bash
python3 forge.py transition-principal /tmp/bastion-forge alice
```

Every view has a separate authenticated intake repository — the only push
target. Published view repositories are fetch-only and reject direct pushes.
Clients clone over HTTP with one token per view:

```bash
python3 forge.py add-token /tmp/bastion-forge alice
python3 forge.py serve /tmp/bastion-forge --port 8000
git clone http://alice:TOKEN@127.0.0.1:8000/alice.git alice
```

Or run the invite-only browser demo (requires Poetry):

```bash
python3 -m venv .poetry
.poetry/bin/pip install poetry
.poetry/bin/poetry install
python3 forge.py add-token /tmp/bastion-forge alice
BASTION_DEMO_SECRET=$(openssl rand -hex 32) .poetry/bin/poetry run python forge.py serve-demo /tmp/bastion-forge --port 8000
```

Then open `http://127.0.0.1:8000/`, enter the token, and browse the effective
permissions, evaluate path decisions, view the projected tree and visible
history, and clone the same view with an unmodified Git client.

Use normal Git inside `alice`:

```bash
git -C alice add .
git -C alice commit -m "change"
git -C alice push origin main
```

Receive hooks synchronously validate and reconcile intake quarantine, upload
immutable canonical and submission-evidence packs, and acknowledge only after
manifest CAS. Jobs can be processed explicitly but are never correctness state:

```bash
python3 forge.py work /tmp/bastion-forge
python3 forge.py check /tmp/bastion-forge
```

Run `python3 forge.py --help` for all experimental commands.
