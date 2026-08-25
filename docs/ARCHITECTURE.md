# Architecture

## Repository Truth

One conditional `manifest.json` selects a contiguous chain of immutable WAL records. Records reference immutable canonical and submission-evidence packs. Manifest compare-and-swap is the only repository commit point.

The local repositories are disposable caches:

```text
object store: manifest + WAL + packs + descriptors
                         |
                  deterministic replay
                         |
          canonical.git and views/<name>.git
                         |
                 intake/<name>.git
```

`canonical.git` materializes complete history. Each view materializes only the object closure allowed by its policy descriptor and has no canonical alternate. Its intake repository advertises the projected tip and reads only from that view's object database.

## Publication

A receive validates one linear branch update in Git quarantine, checks principal identity and policy, reconciles submitted commits onto the current canonical tip, builds immutable packs, appends one linked WAL record, and conditionally updates the manifest. Failure before manifest CAS is uncommitted; success is durable even if every local cache is subsequently deleted.

Administrative canonical updates and policy transitions use the same WAL publication path. Policy transitions start parentless projection epochs so newly hidden history is not retained in the active view closure.

## Identity

Principals are users or service accounts. Immutable teams, roles, and bindings resolve into a content-addressed effective descriptor containing provenance and per-capability allow and deny prefixes. Descriptor identity is independent of a physical view name.

Every authorization-sensitive WAL record captures descriptor identity and generation. Catalog changes remain dormant until an explicit principal transition publishes a new epoch.

## Control Plane

SQLite or PostgreSQL stores catalog records, WAL-derived indexes, cache-freshness jobs, audit entries, and repository metadata. Rows are checksummed and optimistically versioned. Lease scopes serialize control-plane sessions.

The control-plane document has exact top-level fields. Repository lifecycle is `initializing` or `ready`; repository ID and canonical branch are fenced against `repository.json` and the manifest. Database indexes and projection jobs are rebuildable and never decide whether a repository event committed.

## Recovery

Opening a write session replays committed WAL records to refresh derived transactions, changes, and active-view indexes. Canonical and view caches are reconstructed on demand from manifest-reachable packs. Intake repositories are recreated from active view generations.

Backup pins one manifest generation, copies exactly its reachable WAL, packs, descriptors, configuration, and control-plane snapshot, and records hashes for restore verification.
