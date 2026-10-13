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

`canonical.git` materializes complete history. Each view materializes only the object closure allowed by its policy descriptor and has no canonical alternate. View validation compares every stored object with the advertised ref's reachable closure; a generation containing dangling or cruft objects is replaced rather than served. Its intake repository advertises the projected tip and reads only from that view's object database.

## Publication

A receive validates one linear branch update in Git quarantine, checks principal identity and policy, reconciles submitted commits onto the current canonical tip, builds immutable packs, appends one linked WAL record, and conditionally updates the manifest. Failure before manifest CAS is uncommitted; success is durable even if every local cache is subsequently deleted.

Administrative canonical updates and policy transitions use the same WAL publication path. Policy transitions start parentless projection epochs so newly hidden history is not retained in the active view closure.

## Cloudflare Artifacts Storage

The Artifacts adapter maps logical object keys to Git blobs in a private storage
branch. Each body has separate version metadata with a fresh opaque generation,
so changing an object back to earlier bytes does not make a stale version valid.
A storage mutation creates a commit from the observed remote ref and pushes with
an exact expected ref. If another storage writer wins, the adapter rechecks the
object's precondition before retrying. Uncertain writes retain the existing
ambiguous-outcome recovery behavior at the repository layer.

The Artifacts branch commit serializes storage changes; the Bastion manifest
still selects the committed WAL chain. Packs uploaded before a rejected manifest
publication remain unreferenced objects. The local Artifacts Git cache is
disposable and remote branch queries determine freshness.

The hosted Cloudflare profile uses a Worker with an Artifacts binding and a
singleton Container for the existing Git processes and Python application.
Because Container filesystems are ephemeral, the SQLite catalog and credential
state are checkpointed to the private Artifacts storage before mutation responses
are acknowledged. Cold start restores that checkpoint and replays committed WAL
events. The backing repository includes hidden source and credential records,
so its server token never becomes a client credential. Clients access only
Bastion's permission-bounded projection and authenticated intake endpoints.

## Identity

Principals are users or service accounts. Immutable teams, roles, and bindings resolve into a content-addressed effective descriptor (algorithm `capabilities-v3`) containing provenance and per-capability allow and deny prefixes. Prefix lists are normalized, sorted, and exact-deduplicated but never ancestor-minimized, so every prefix that can win longest-match adjudication is preserved. Descriptor identity is independent of a physical view name.

View operations capture the assigned descriptor identity and generation. Administrative canonical updates resolve one fresh descriptor from the locked catalog snapshot and capture its identity; the descriptor object is uploaded as an immutable prerequisite before the WAL record commits. Catalog changes remain dormant until an explicit principal transition publishes a new epoch.

## Control Plane

SQLite or PostgreSQL stores catalog records, WAL-derived indexes, cache-freshness jobs, audit entries, and repository metadata. Rows are checksummed and optimistically versioned. Lease scopes serialize control-plane sessions.

The control-plane document has exact top-level fields. Repository lifecycle is `initializing` or `ready`; repository ID and canonical branch are fenced against `repository.json` and the manifest. Database indexes and projection jobs are rebuildable and never decide whether a repository event committed.

## Recovery

Opening a write session replays committed WAL records to refresh derived transactions, changes, and active-view indexes. Canonical and view caches are reconstructed on demand from manifest-reachable packs. Intake repositories are recreated from active view generations.

Backup pins one manifest generation, copies exactly its reachable WAL, packs, descriptors, configuration, and control-plane snapshot, and records hashes for restore verification. The capture runs under the `global` storage lease and verifies quiescence after the database export (unchanged manifest generation; fence metadata read from the exported snapshot itself). Restore validates artifact hashes, repository identity, and the database/WAL fence before mutating the target; an ahead-of-truth snapshot fails cleanly unless an explicit derived-state repair rebuilds only WAL-derived rows. Repository configuration, manifest, WAL records, and backups intentionally omit format-version fields because this is a greenfield implementation with no legacy formats to support.

## Snapshots And Materialization Reuse

Readers receive immutable snapshot projections of the replayed WAL: a warm unchanged load performs one conditional manifest probe and returns the cached read-only projection with zero historical copies (one freeze per installed snapshot replaces a deep copy per read). Writers fork private copy-on-write candidates from the cache-private state, so a reader's mutation attempts fail instead of corrupting the cache or another reader.

Materialization reuse binds to per-view content identity — descriptor assignment, projection epoch, ref, and projected tip — not to the global WAL head. Unrelated records (another principal's view creation, hidden-only canonical commits) never rebuild a proved generation. A changed view links the previous generation's verified immutable objects into a private candidate and applies only the affecting suffix, and canonical generations install only the packs a tip advance added. Progress receipts bind to per-view identity and progress rather than the global head, so a restart reuses the generation instead of reconstructing from the epoch. Full validation (strict fsck plus exact closure) remains mandatory before any new generation is activated, and policy transitions never reuse history across an epoch.

## Hosted Demo

`forge.py serve-host` runs the multi-repository Flask application on one port.
Cloudflare uses the same dispatcher with a host DO catalog, separate repository
DO catalogs, and isolated Artifacts branches; container disk is disposable.
The local alternative uses SQLite and filesystem objects. Clerk sign-in yields
a host session; opening a repository yields a separate view-scoped UI token.
Git routes are `/<owner>/<repository>.git` and require repository-specific Git
credentials. Browser credentials remain in page memory. No startup seeds a demo
repository. A TLS reverse proxy or Cloudflare Worker is required for public use.
The repository engine validates `capabilities-v3` descriptors and exposes scoped
decisions, trees and history with bounded integrity-protected cursors. Limits
and Git process lifecycle are documented in `src/limits.py` and [SECURITY.md](SECURITY.md).
