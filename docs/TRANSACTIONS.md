# Transactions And Events

## Goals

The transaction model must provide:

- complete-push authorization before canonical ref movement;
- exact source-view commit continuity;
- monotonically ordered canonical mutations;
- recoverable publication across independent Git repositories;
- at-least-once secondary-view delivery;
- idempotent retries without ref regression;
- isolation when one projected view is unavailable.

Git refs and the state store cannot participate in one atomic transaction.
Veil therefore records durable intent, observes Git state during recovery, and
only advances transaction status forward.

## Source Push Lifecycle

```text
receive-pack quarantine
        |
        v
validate ref and complete commit chain
        |
        v
authorize every tree transition
        |
        v
construct canonical candidates outside canonical refs
        |
        v
persist prepared transaction
        |
        v
CAS canonical ref
        |
        v
persist canonical_committed + blocked outbox event
        |
        v
receive-pack publishes submitted source ref
        |
        v
confirm source publication
        |
        v
activate canonical.committed event
```

The outbox event exists durably when canonical commitment is recorded, but it
remains blocked until source ref publication is confirmed. This prevents a
later transaction or manual synchronization from exposing an unconfirmed
submission to another view.

If source publication is not confirmed, the transaction becomes
`source_unconfirmed`. Recovery never moves the source ref on the client's
behalf. An identical retry of the original `old -> new` update may finish the
source publication without creating another canonical commit.

An unconfirmed source transaction is a repository-wide publication barrier.
No later canonical submission, manual synchronization, or projection worker
may cross it.

## Transaction States

Source transactions move monotonically through:

```text
prepared
  -> canonical_committed
  -> source_unconfirmed      # retryable source outcome
  -> fanout_pending
  -> published

prepared -> aborted          # canonical CAS did not publish the candidate
```

Administrative canonical transactions omit source publication:

```text
prepared -> fanout_pending -> published
prepared -> aborted
```

Every transaction has a durable canonical sequence. Recovery processes that
sequence in order and never rewinds a newer view frontier.

## Outbox And Projection Jobs

Confirmed canonical commitment activates one idempotent event:

```text
canonical.committed:<transaction-id>
```

Dispatch creates one deterministic job per target view:

```text
project:<transaction-id>:<view-id>
```

Duplicate event delivery and dispatch are safe because identifiers are stable.
Jobs for the same view execute in canonical sequence; different views can run
independently.

Job states are:

```text
pending -> running -> ready -> completed
              |         |
              +-> retry-+
```

A worker:

1. Claims a job and lease while holding the state lock.
2. Captures the expected view frontier and projected tip.
3. Releases the lock.
4. Builds a deterministic projection plan and Git objects.
5. Persists the ready plan before ref publication.
6. Rechecks source activity, view state, and canonical ancestry.
7. CAS updates or adopts the projected ref.
8. Persists anchors, `Change-Id` representations, and completion.

If the process crashes after moving a projected ref but before state
persistence, the ready plan remains durable. A retry adopts the already-moved
ref rather than attempting to recreate or rewind it.

Expired leases return to `retry`. Completion accepts a lease token until a
different worker actually reclaims the job. A blocked or failed view does not
prevent workers from processing independent views.

## Canonical Writers

All canonical branch writers must use the forge coordinator. Direct writes to
`canonical.git` are unsupported.

Administrative updates use:

```bash
python3 forge.py update-canonical FORGE OLD_OID NEW_OID
```

Both IDs must be immutable full SHA-1 commit IDs already present in the
canonical object database. The coordinator rejects:

- expected-ref mismatch;
- non-fast-forward updates;
- merge commits;
- explicit empty subtrees;
- unsupported modes and object types.

Administrative updates create normal outbox events. An update with no configured
views completes immediately.

## Locking Rules

The filesystem state lock serializes short state and ref critical sections.
It is not held while traversing history or constructing projection objects.

The current lock order is:

1. state lock;
2. one canonical or projected ref CAS;
3. state persistence;
4. release.

Workers never acquire multiple view locks because the prototype has only the
single state lock. Production should preserve per-canonical-ref ordering and
per-view ordering while allowing unrelated repositories and views to execute
concurrently.

Receiver liveness markers store both PID and Linux process start identity to
avoid treating a reused PID as an active receive-pack process.
