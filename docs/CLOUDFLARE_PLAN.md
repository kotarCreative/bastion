# Cloudflare Artifacts migration

Branch: `feat/cloudflare-artifacts`.

## Outcome

Bastion runs behind a Cloudflare Worker with its existing permission based Git
projections. Cloudflare Artifacts stores repository truth. A SQLite-backed Durable Object
stores the hosted control plane. Container disk holds disposable Git
materializations and reconstructed configuration, with no live SQLite database.

## Implementation

1. Implement the conditional object-store contract over a private Artifacts Git
   repository. Store immutable packs, WAL records, descriptors, and manifests as
   Git blobs. Each object has a unique version. Update the storage branch with
   an exact expected Git ref so competing writers cannot overwrite one another.
2. Integrate Artifacts into initialization, configuration validation, backup,
   and restore. Migrate an existing forge with its consistent backup and restore
   path so repository identity, policies, history, accounts, and tokens survive.
3. Add a Worker using the Artifacts binding and a singleton Cloudflare Container
   running Python and Git. Store the hosted catalog and account database in a
   separate Durable Object using atomic SQL transactions. Import legacy Artifacts
   snapshots once, reconstruct configuration at cold start, and replay newer WAL records.
4. Verify the adapter with actual Git remotes, competing writers, ambiguous
   writes, stale versions, and cache loss. Exercise permission isolation and
   migration, then inspect deployment configuration and restart recovery.

## Permission boundary

The private storage repo contains canonical data, submission evidence, and
control-plane credentials. Its token is a server credential. Artifacts tokens
have repository scope, so client credentials continue to be Bastion tokens.
Clients fetch projected repositories and push through authenticated intake;
they never receive the backing repository URL/token through a platform route.
Policy transitions retain the existing parentless projection epochs, preventing
newly hidden history from remaining reachable in the active projection.

## Acceptance evidence

See [verification coverage and commands](CLOUDFLARE_VERIFICATION.md) for local
evidence and the remaining live deployment checks.

- A new branch exists and contains the port.
- Artifacts Git is the authoritative durable backend; local cache deletion
  does not lose repository data.
- Exact expected-ref publication and unique per-object versions retain
  conditional-write and retry semantics, including rejection of stale writes.
- Existing clients can clone and push through permission-bounded projections;
  hidden objects remain absent from those repositories.
- Backup/restore can move an existing forge to Artifacts without changing its
  repository identity or permissions.
- Worker configuration uses documented Artifacts and Containers bindings;
  backing tokens remain private.
- The Durable Object catalog survives Container disk loss and catalog edits
  are durable before a success response, without full SQLite checkpoints.
- Documented setup and a concurrent-agent demonstration can be run by another
  developer. A live deployment additionally requires a Cloudflare account with
  Workers Paid and Artifacts access.

## Competition references

The [competition brief](https://blog.cloudflare.com/next-git-platform-on-cloudflare/)
requires Workers and Artifacts, a demonstration of multiple agents working
concurrently, a permissively licensed source repository, running instructions,
and a 5–10 minute demonstration video. Its announced submission deadline is
October 14, 2026. Creating and submitting the video is a separate release task.

API references:

- [Artifacts Workers binding](https://developers.cloudflare.com/artifacts/api/workers-binding/)
- [Artifacts Git protocol](https://developers.cloudflare.com/artifacts/api/git-protocol/)
- [Artifacts REST API](https://developers.cloudflare.com/artifacts/api/rest-api/)
