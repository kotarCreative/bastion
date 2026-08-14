# Veil

Veil is an experimental permission-aware Git forge. It maintains one complete
canonical Git repository and exposes writable Git repositories containing only
the files each permission view may read. Users work with an unmodified Git
client.

The prototype demonstrates that this model is feasible with explicit
restrictions:

- submitted history is linear;
- accepted commits retain their exact Git IDs in the submitting view;
- other views receive synthetic commits only for changes visible to them;
- hidden-only canonical commits do not move an unaffected projected ref;
- every tree transition is authorized before the canonical ref moves;
- canonical and projected publication is recoverable and idempotent;
- secondary-view publication uses a durable outbox and ordered jobs;
- logical `Change-Id` metadata links different Git representations.

This is a local architecture experiment, not a production security boundary.
Authentication, network transports, resource controls, transactional database
storage, and the wider forge surface remain roadmap work.

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

Create a local forge and one view:

```bash
python3 forge.py init /tmp/veil-forge /path/to/source --branch main
python3 forge.py add-view /tmp/veil-forge alice \
  --read shared --read team-a \
  --write shared --write team-a
git clone --no-local /tmp/veil-forge/views/alice.git alice
```

Use normal Git inside `alice`:

```bash
git -C alice add .
git -C alice commit -m "change"
git -C alice push origin main
```

Receive hooks synchronously validate and reconcile the source push, then drain
ready secondary-view jobs. Jobs can also be processed explicitly:

```bash
python3 forge.py work /tmp/veil-forge
```

Run `python3 forge.py --help` for all experimental commands.
