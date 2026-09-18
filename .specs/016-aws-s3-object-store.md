# AWS S3 Object-Store Adapter

Status: draft.

## Contract

Bastion may store repository truth in one dedicated AWS S3 general-purpose
bucket while retaining the existing `ObjectStore` interface and WAL publication
protocol. The S3 adapter is an alternative to the filesystem adapter, not a new
repository model. `manifest.json` compare-and-swap remains the only repository
commit point; RDS rows, EFS files, S3 version history, worker jobs, and local Git
repositories do not commit repository state.

The adapter uses S3 conditional requests directly. `Create` is `PutObject` with
`If-None-Match: *`; `Update(version)` is `PutObject` with `If-Match: <etag>`.
No DynamoDB lock, lease object, read-before-write emulation, or process-local
mutex participates in correctness.

S3 ETags are opaque conditional-request validators. They are not unique object
generations and are never accepted as repository-integrity evidence. Under the
qualified single-part SSE-S3 profile, an ETag can repeat when the same bytes are
written again and can recur after an ABA content sequence. Bastion's existing
canonical JSON checks, SHA-256 object references, pack checksums, replay
validation, and independent invariant checker remain authoritative.

## Dependencies And Decisions

This specification builds on:

- [011: WAL object-store repository truth](011-wal-object-store-repository-truth.md);
- [014: storage correctness and bounded hot paths](014-storage-correctness-and-bounded-hot-paths.md);
- [015: security and performance hardening](015-security-and-performance-hardening.md).

The first qualified adapter has these fixed decisions:

- AWS S3 only; S3-compatible products are not claimed or tested.
- One Terraform-managed bucket per deployment environment.
- S3 bucket versioning is enabled, but version IDs are not used for CAS.
- SSE-S3 (`AES256`) is required for every object.
- Bucket owner enforced object ownership and all public access blocks are
  enabled; ACLs are not used.
- Existing filesystem-backed data moves through an offline backup/restore, not
  live dual-write or an online migration mode.
- EFS remains required for `repository.json`, `backend.json`, host-local state,
  intake repositories, and disposable canonical/view materializations.

## Object-Store Seam

`src/object_store.py` remains the sole external seam. The S3 adapter implements
the existing methods without adding S3 details to repository callers:

```python
get(key, *, if_none_match=None, byte_range=None) -> GetResult
head(key) -> ObjectMeta | None
put(key, body, *, mode=Create() | Update(version)) -> ObjectMeta
delete(key, *, if_version=None) -> None
list_prefix(prefix) -> list[ObjectMeta]
```

The adapter owns key mapping, conditional headers, ETag handling, checksums,
pagination, response-body closure, SDK configuration, retries, and AWS error
translation. Repository, WAL, backup, and materialization code use only the
existing interface.

The implementation accepts an injected low-level S3 client for deterministic
tests. Production construction uses the standard AWS credential provider chain
and region from repository configuration. Credentials, session tokens, role
ARNs, endpoints, and presigned URLs are never persisted in the forge root.

## Repository Configuration

New repository configurations carry an explicit numeric structural version. An
S3-backed `repository.json` uses this exact canonical shape:

```json
{
  "canonical_branch": "main",
  "object_store": {
    "bucket": "bastion-test-123456789012-us-east-1",
    "expected_bucket_owner": "123456789012",
    "kind": "s3",
    "prefix": "",
    "region": "us-east-1",
    "server_side_encryption": "AES256"
  },
  "repository_config_version": 2,
  "repository_id": "0123456789abcdef0123456789abcdef"
}
```

The version-2 filesystem shape is likewise exact:

```json
{
  "canonical_branch": "main",
  "object_store": {
    "kind": "filesystem",
    "path": "object-store"
  },
  "repository_config_version": 2,
  "repository_id": "0123456789abcdef0123456789abcdef"
}
```

Requirements:

- `bucket` is a valid DNS-compatible AWS S3 bucket name.
- `expected_bucket_owner` is the expected 12-digit AWS account ID. Every S3
  request supplies it through `ExpectedBucketOwner`; mismatch is a definite
  security/configuration failure.
- `region` is nonempty and is used to construct the S3 client explicitly.
- `prefix` is empty or a relative ASCII object-key prefix with no leading or
  trailing slash, empty component, `.` component, `..` component, NUL, or
  backslash.
- `server_side_encryption` is exactly `AES256` in this milestone.
- Unknown fields, unknown kinds, unsafe prefixes, and credential-bearing values
  fail closed before any S3 request.
- The configuration contains no endpoint override. Supporting another endpoint
  would claim an unqualified S3-compatible adapter.
- The current unversioned exact filesystem configuration remains readable as a
  persisted legacy shape. Resuming an interrupted legacy initialization
  preserves that configuration byte-for-byte rather than trying to rewrite it
  as version 2. New initialization and restore write version 2 for both
  filesystem and S3 configurations; a separate explicit upgrade or
  backup/restore may convert an accepted legacy root. Unknown versions fail
  closed.

Initialization accepts:

```text
--object-store s3://BUCKET[/PREFIX] --s3-region REGION \
  --s3-expected-bucket-owner ACCOUNT_ID
```

The URI has no user information, query, or fragment. Percent-encoded path
components are not accepted. The region and expected owner are required for S3
and rejected for filesystem storage. Deployment may supply equivalent values
through `BASTION_OBJECT_STORE`, `BASTION_S3_REGION`, and
`BASTION_S3_EXPECTED_BUCKET_OWNER`, but initialization persists the resolved
non-secret configuration above.

Initialization verifies bucket location, enabled versioning, default SSE-S3
encryption, and access before publishing the import manifest. Normal
`Repository.open` does not repeat bucket-control-plane calls on each request;
bucket policy and conditional data-plane operations enforce the runtime rules.
For a newly versioned bucket, Terraform waits the AWS-documented 15-minute
propagation interval before enabling bootstrap or any object write. Merely
observing `Status=Enabled` is not sufficient for the first deployment.

## Key Mapping

Every logical key is validated by `validate_key` before mapping. The physical
S3 key is:

```text
<prefix>/<incoming-object-store-key>
```

The leading `<prefix>/` segment is omitted when the configured prefix is empty.
`Repository` already supplies incoming keys such as
`repos/<repository_id>/manifest.json`; the adapter must not add a second
repository namespace. No caller may pass a key already containing the
deployment prefix. Mapping is injective: two valid incoming keys cannot resolve
to the same physical key. The mapped UTF-8 key must fit S3's object-key length
limit.

The repository continues to own `repos/<repository_id>` namespacing. The S3
adapter adds only the deployment prefix. `LIST` is never used to discover WAL
history, select a manifest, or decide authorization.

## Version Semantics

`Version.value` is the exact ETag string returned by S3 for the current object,
preserved and sent back without parsing or quote normalization. It means only
"this content validator matched the current object when observed." It does not
prove one unique write generation and is not ABA-safe. A successful `GET`,
`HEAD`, `PUT`, or listed object must provide a nonempty ETag; an otherwise
successful response without one is a storage failure with a potentially
ambiguous write outcome where applicable.

S3 `VersionId` values are intentionally excluded from the object-store
interface. Conditional operations evaluate the current version. Noncurrent S3
versions and delete markers are operator-recovery data only and never enter WAL
records, manifests, backups, repository snapshots, or authorization decisions.

Repository correctness narrows the general validator semantics: every valid
manifest publication changes canonical manifest bytes monotonically through at
least the WAL head, head sequence, and revision, and normal operation never
deletes or restores an earlier manifest value. After a successful manifest
`PUT`, a returned ETag equal to the previous validator is an integrity failure;
the repository stops rather than attempting another publication. This detects,
but cannot make S3 ETags into, unique generations.

This milestone explicitly accepts the residual limitation of direct S3 ETag
CAS: an adversarially constructed ETag collision is not excluded by AWS's
contract. The qualified writer is the IAM-constrained Bastion task, normal
repository transitions never write identical or earlier manifest bytes, and
the runtime role cannot delete objects. If adversarial ETag collision or a
hostile S3 administrative writer enters the threat model, this design is
insufficient and must be replaced by a separate unique-generation
linearization primitive rather than described as stronger S3 CAS.

The process-local repository snapshot cache uses a stable identity derived from
the resolved forge root, repository ID, and canonical object-store
configuration. It must not use `id(S3ObjectStore)`; repeated `Repository.open`
calls for the same root retain spec 014's conditional-manifest and suffix-load
behavior.

## Operation Semantics

### Get

- Issue `GetObject` for the mapped key.
- Send `IfNoneMatch=version.value` when `if_none_match` is supplied.
- Send an inclusive `Range: bytes=start-end` when `byte_range` is supplied.
- Reject negative, reversed, or otherwise invalid ranges before the request.
- Map `304 Not Modified` to `NotModified`.
- Map a missing current object or current delete marker to `ObjectNotFound`.
- Map `416 Invalid Range` to `ValueError`.
- Always close the streaming response body, including parse, size, checksum,
  and caller-visible error paths.
- `ObjectMeta.size` is the complete object size. For ranged responses it is
  derived from the validated `Content-Range` total, not the returned slice
  length.
- A short, oversized, malformed, or contradictory response fails as storage or
  integrity error; partial bytes are never returned as a successful object.

### Head

- Issue `HeadObject` for the mapped key.
- Return `None` only for a missing current object or current delete marker.
- Return the current ETag and complete `ContentLength` otherwise.
- Permission, throttling, transport, and service failures must not be reported
  as absence.

### Put

- Accept only `bytes`; size is known before the request.
- `Create` sends `IfNoneMatch="*"`.
- `Update(version)` sends `IfMatch=version.value`.
- Every request sends `ServerSideEncryption="AES256"`, exact `ContentLength`,
  and `ChecksumSHA256=base64(sha256(body))`.
- The first milestone uses one `PutObject` request. It does not use multipart
  upload, transfer-manager helpers, or copy operations. The S3 test profile has
  a fixed qualified per-object limit of 256 MiB. Pack builders and restore
  inventory checks enforce it from file metadata before
  calling `read_bytes` or allocating an object body; the adapter repeats the
  check before issuing any request. Initialization and restore verify every
  planned object before target mutation. The absolute S3 single-`PutObject`
  ceiling remains 5,368,709,120 bytes, but this bytes-only interface and the
  2-GiB Fargate profile do not claim to support objects near that ceiling.
- A successful response returns `ObjectMeta(key, len(body), Version(etag))`.
- For `Create`, `412 Precondition Failed` maps to `PreconditionFailed`.
- For `Update`, absence-related `404` and ETag-mismatch `412` both map to
  `PreconditionFailed`.
- `409 ConditionalRequestConflict` is resolved as described under failure and
  retry semantics; it is not treated as proof that the supplied condition was
  false.

### Delete

- `delete(key, if_version=version)` sends `DeleteObject` with
  `IfMatch=version.value`.
- `delete(key)` sends `IfMatch="*"`; a missing current object is treated as
  idempotent success.
- With a supplied version, absence-related `404` and ETag-mismatch `412` map to
  `PreconditionFailed`.
- Without a supplied version, `204`, absence-related `404`, and the `412`
  returned for a current delete marker are idempotent success.
- For `delete(key)` without a supplied version, a `409` causes one reread.
  Absence is idempotent success. If an object is current, the adapter reports
  `PreconditionFailed` without retrying; it never retries with `If-Match: *` and
  cannot delete a concurrent replacement.
- For a delete with a supplied version, a `409` reports `PreconditionFailed`
  after reread unless the same ETag is still current, in which case one retry
  uses that exact ETag.
- No unqualified delete is sent. Bucket policy may therefore require the
  `If-Match` header for all deletes.
- Normal repository publication and loading do not delete S3 objects. Deletion
  remains available for explicit maintenance and test conformance only.

### List Prefix

- Use paginated `ListObjectsV2` with the mapped prefix.
- Consume every continuation token, reject malformed or repeated tokens, and
  return results sorted by logical key.
- Strip only the exact configured deployment prefix; a returned object outside
  it is an integrity/configuration failure.
- Each result carries complete size and its opaque current ETag.
- Listing noncurrent versions is forbidden.
- Repository load, replay, and publication remain independent of `LIST`.

## Consistency And Publication

The adapter relies on AWS S3 strong consistency for successful `PUT` and
`DELETE` operations and subsequent `GET`, `HEAD`, and `LIST` requests.

For one absent current key, concurrent `Create` calls have at most one success.
`Update(previous_etag)` succeeds only while the current object's ETag matches.
Exactly-one-winner behavior for manifest updates additionally depends on every
successful publication changing the manifest bytes and resulting ETag; S3 ETags
alone do not provide an ABA-proof generation. A successful manifest update is
immediately readable as the current object. These guarantees are required from
AWS S3 itself; a local lock cannot substitute for them.

The existing repository publication sequence is unchanged:

1. Read and validate one manifest generation and its ETag.
2. Upload immutable descriptors and pack prerequisites with `Create`.
3. Upload the immutable next WAL record with `Create`.
4. Publish the next manifest with `Update(previous_etag)`.
5. On precondition failure, reload and resolve idempotency or rebuild.
6. On an ambiguous result, reread current manifest reachability by publication
   ID before reporting failure or retrying.

No S3 object other than the manifest is intentionally overwritten. Immutable
object retries accept an existing key only after reading it and proving byte
identity, as required by `Repository.put_immutable`.

## Failure And Retry Semantics

Mutating S3 requests must not have hidden SDK retries that can convert a
successful first attempt into a later `412` while losing the original outcome.
Production overrides ambient AWS retry configuration and uses a client with one
total SDK attempt for `PutObject` and `DeleteObject` (for botocore,
`total_max_attempts=1`). Read retries occur above that no-retry client or through
a separately and explicitly configured read client. Tests count actual HTTP
attempts, not only adapter calls.

For mutation requests:

- a definite `404`/`412` is translated according to the operation matrix above;
- a `409 ConditionalRequestConflict` is a definite non-commit caused by a
  concurrent operation. The adapter rereads the key. For create, it reports
  `PreconditionFailed` when an object now exists and retries once when the key
  remains absent. For update or versioned delete, it reports
  `PreconditionFailed` when the key is absent or the ETag changed and retries
  once only when the same ETag is still current. For delete without a supplied
  object-store version, absence is success and a current object produces
  `PreconditionFailed` without retry;
- connection loss, timeout, cancellation, malformed success response, or 5xx
  after the request may have reached S3 is `AmbiguousWrite`;
- authentication, authorization, invalid bucket, invalid encryption, and other
  definite request rejection is a stable `ObjectStoreError`, not ambiguity.

No mutation is blindly retried after an ambiguous transport result. In
particular, an ambiguous delete rereads current state but cannot safely delete a
replacement object. Repository immutable-put and manifest-publication callers
retain their existing byte-verification and publication-ID resolution paths.

For read-only requests, bounded standard retries with jitter are permitted for
throttling and transient 5xx/transport failures. Total attempts and total
duration are bounded and configurable within documented safe ranges. Exhaustion
returns a stable storage failure.

Repository control flow continues to receive and handle semantic
`ObjectStoreError` subclasses: `ObjectNotFound`, `NotModified`,
`PreconditionFailed`, and `AmbiguousWrite`. Only an otherwise-unhandled adapter
storage failure is translated at the repository seam to a redacted
`ForgeError(..., "storage")`. HTTP, Git, worker, backup, readiness, and CLI paths
must not leak raw botocore exceptions or turn an S3 outage into an unclassified
internal error. Client-facing outage tests verify the existing stable
service-unavailable behavior.

AWS request IDs may appear in operator logs. Credentials, authorization
headers, session tokens, object bodies, DSNs, presigned URLs, and complete
client-facing internal errors may not. Remote callers continue to receive the
existing generic storage/service-unavailable behavior.

## Integrity, Encryption, And Bucket Controls

Upload SHA-256 checksums provide transport/storage validation but do not replace
repository-level hashes. `PutObject` sends
`ChecksumSHA256=base64(sha256(body))`; a returned `ChecksumSHA256` must match and
a returned `ChecksumType` must be `FULL_OBJECT`. Full-object `GetObject` requests
send `ChecksumMode="ENABLED"` and validate a returned full-object SHA-256 only
after consuming the complete body. Arbitrary ranges are not compared with a
complete-object checksum. The qualified S3 adapter permits no checksum-less
object: all initialization and restore writes include SHA-256, and a missing
checksum on a full-object read fails closed.
Filesystem objects and local backup artifacts retain their existing independent
hash-verification behavior.

Terraform provisions:

- a dedicated general-purpose bucket with a globally unique name;
- enabled bucket versioning;
- default SSE-S3 encryption;
- bucket owner enforced object ownership;
- all four S3 public-access blocks;
- a policy denying non-TLS requests for non-AWS-service principals using both
  `aws:SecureTransport=false` and `aws:PrincipalIsAWSService=false`, so redacted
  service-to-service network context is not denied unintentionally;
- a policy denying `PutObject` unless either `If-Match` or `If-None-Match` is
  present;
- a policy denying `DeleteObject` without `If-Match`;
- a policy denying writes whose declared server-side encryption is not
  `AES256`;
- lifecycle expiration of noncurrent versions after a configurable recovery
  window, defaulting to 30 days, plus `ExpiredObjectDeleteMarker` cleanup after
  no noncurrent version remains. Delete-marker cleanup is not independently
  age-based, lifecycle execution is asynchronous, and lifecycle actions are not
  constrained by request-header bucket-policy denies.

Bucket versioning protects against operator mistakes but does not authorize
rollback. Restoring a noncurrent manifest directly over the current object is
forbidden because it can move repository truth backwards. Recovery uses the
verified backup/restore procedure.

## IAM

The ECS task role, not the execution role, receives data access. Permissions are
limited to the dedicated bucket and configured prefix.

Every allow statement is additionally constrained by `aws:ResourceAccount` or
`s3:ResourceAccount` to the persisted expected bucket-owner account. Bucket
names created in S3's shared global namespace can be reused by another account
after deletion and are not treated as owner identity.

Bucket-level permissions:

- `s3:GetBucketLocation`;
- `s3:GetBucketVersioning`;
- `s3:GetEncryptionConfiguration`;
- `s3:ListBucket`, constrained to the configured prefix.

Object-level permissions beneath the configured prefix:

- `s3:GetObject`;
- `s3:PutObject`.

The ordinary ECS runtime and restore roles do not receive `s3:DeleteObject`.
Normal publication, backup, restore, and cache rebuilding do not need it. A
separate operator/test maintenance role may receive `s3:DeleteObject` for
adapter conformance and audited cleanup; the bucket policy still requires its
`If-Match` header. An unexpected runtime call to `delete` therefore fails with a
definite redacted authorization error rather than weakening runtime IAM.

The runtime role receives no permissions to modify bucket policy, encryption,
versioning, ACLs, replication, or Object Lock, and no account-wide listing
permissions. Its bucket-control read permissions are limited to those listed
above. Terraform's provisioning identity owns bucket configuration separately.
Administrative operators may retain break-glass access, but ordinary
application writes remain subject to the bucket's conditional-header and
encryption policy.

The runtime/restore role verifies only controls covered by its permissions:
expected owner, location, enabled and stabilized versioning, default SSE-S3
encryption, and scoped data-plane access. Terraform and release qualification
establish and verify Object Ownership, Block Public Access, lifecycle, and
bucket-policy configuration; the runtime role does not gain
`GetBucketPolicy` or broader bucket-administration access for those checks.

## Backup, Restore, And Existing Data

Backup continues to pin one manifest and copy only its exact reachable closure
through the `ObjectStore` interface. It does not copy every object under the S3
prefix, noncurrent versions, delete markers, orphaned failed-attempt objects, or
unreachable garbage. Normal backup needs no `ListObjectVersions` permission.

New backups use backup format version 3. Version 3 has the exact version-2
manifest keys plus required `source_object_store_kind`, whose value is exactly
`filesystem` or `s3`; unknown fields and values fail closed. The source
`repository.json` remains a hashed provenance artifact because it contains no
AWS credentials, but restore uses it only to validate repository identity,
branch, and source configuration. It never reuses the source bucket, prefix, or
region implicitly. Existing format-version-2 filesystem backups remain
restorable.

Restore receives target storage explicitly:

```text
forge.py restore BACKUP NEW_ROOT \
  --database RUNTIME_DSN [--admin-database ADMIN_DSN] \
  --object-store s3://BUCKET/PREFIX --s3-region REGION \
  --s3-expected-bucket-owner ACCOUNT_ID
```

`--object-store filesystem[:PATH]` retains the filesystem target. S3-only flags
are invalid for a filesystem target. Omitting `--object-store` retains the
existing filesystem-under-`NEW_ROOT` default. The control-plane database backend
must still match the backup; object-store kind may change independently.

Restore may change object-store adapter kind independently of the existing
same-database-backend restriction. Filesystem-to-S3 and S3-to-filesystem restore
are supported.

An S3 target restore always uses a new forge root, a newly scoped PostgreSQL
database when PostgreSQL is selected, and a unique never-before-active S3
deployment prefix. No running task may reference the target root or prefix
during restore. Creating a manifest inside this isolated namespace does not
mutate or roll back the accepted source repository.

The restore procedure:

1. Verifies every backup artifact, hash, shape, repository fence, and database
   artifact, including the qualified 256 MiB per-object limit, before
   mutating the target.
2. Verifies the target bucket's expected owner, location, enabled and stabilized
   versioning, default SSE-S3 encryption, and scoped data-plane access; verifies
   the unused root and unique prefix; and requires that the target repository
   manifest key be absent.
3. Creates the isolated target root and database in a non-ready restore state,
   restores the database snapshot, and validates its static identity fields.
4. Uploads descriptors, packs, and WAL records using byte-identical immutable
   create semantics. Resolution of an ambiguous request within this restore
   attempt may reuse an object only after byte verification.
5. Uploads `manifest.json` last with `Create`, writes the fresh target
   configuration, and validates the database/WAL fence.
6. Marks the isolated target database `ready`, then rebuilds disposable
   materializations, runs the independent checker, and exercises authenticated
   read/write operations. `ready` permits existing validation paths to run but
   does not activate the target because no running task references `NEW_ROOT`.
7. Accepts the target only through a separate operator-controlled deployment
   switch that points the single ECS service at `NEW_ROOT`.

A failure at any stage removes the local target root and a database created only
for this attempt. It never rolls back or mutates the source. S3 objects under the
unique failed prefix, including a manifest created before a later validation
failure, are abandoned as an unreferenced failed target; restore does not delete
them or reuse that prefix. This preserves the rule that cleanup cannot remove an
unknown concurrent object. A later retry selects a fresh prefix. General cleanup
of abandoned restore prefixes is a separate audited maintenance operation.

Moving the current EFS-backed test environment to S3 is an offline operation:

1. Stop the ECS service and worker or otherwise establish the existing verified
   quiescence condition.
2. Produce and verify a normal backup.
3. Restore that backup into a new forge root, a new root-scoped database, and a
   unique S3 deployment prefix.
4. Run `forge.py check` and the deployed clone/push/cross-view smoke test.
5. Register a new task definition pointing `BASTION_ROOT` at the restored root,
   deploy exactly one task, and retain the stopped filesystem root until the
   rollback window closes.

There is no dual-write, background copying, live cutover, or fallback to the old
filesystem object store after writes are accepted by the S3-backed deployment.

## Deployment Changes

The AWS Terraform profile adds the S3 bucket, bucket policy, versioning,
encryption, lifecycle, and ECS task-role permissions. It supplies
`BASTION_OBJECT_STORE=s3://...`, `BASTION_S3_REGION`, and
`BASTION_S3_EXPECTED_BUCKET_OWNER` to bootstrap tasks. A one-time Terraform
dependency gate prevents bootstrap until 15 minutes after versioning is first
enabled.

EFS remains mounted at `/var/lib/bastion` but is no longer repository truth. It
holds the root configuration, control-plane backend record, host identity,
temporary source seed, receive/intake state, materializations, receipts, and
worker markers. Deleting those disposable Git caches must remain recoverable
from S3 plus the control-plane database.

RDS, ALB, ECR, Secrets Manager, and the single-task deployment restriction are
unchanged. S3 CAS is not permission to scale the application service above one
task while admission accounting and materialization activation remain
process-local.

## Observability

The adapter records bounded operation metrics by method and result category:

- request count and duration;
- bytes read and written;
- conditional hits and precondition failures;
- throttling and retry count;
- ambiguous writes;
- range reads and paginated list calls.

Metrics and logs distinguish definite precondition loss from storage outage and
ambiguous outcome. They do not record object bodies, credentials, or signed
requests. Object keys are omitted or reduced to safe repository-relative
categories unless an operator-only diagnostic explicitly enables hashed key
identifiers.

Readiness validates the configured root, RDS fence, and current manifest through
normal repository load. It does not use `LIST` or bucket-wide probes on every
health request.

## Verification

Unit conformance tests run the same `ObjectStore` behavior suite against memory,
filesystem, and a deterministic fake S3 client. They cover:

- key and prefix validation;
- create/update/delete conditional headers;
- opaque quoted ETag round trips;
- full and ranged GET metadata;
- 304, operation-specific 404/412, 409 reread/retry, and 416 handling;
- short/malformed response bodies and missing ETags;
- paginator continuation, repeated-token rejection, sorting, and prefix
  stripping;
- checksum generation and SSE-S3 headers;
- missing or mismatched full-object checksum rejection;
- bounded read retries and ambiguous mutation failures;
- one actual SDK/HTTP attempt for ambiguous mutations and bounded explicit
  retries only after definite 409 non-commit responses;
- stable snapshot identity across newly constructed S3 adapter instances;
- expected-owner mismatch and object-size-limit rejection before mutation;
- interrupted legacy-filesystem initialization preserving its existing
  unversioned configuration;
- redacted client-facing storage failures through HTTP, Git, worker, readiness,
  backup, and CLI paths;
- streaming-body closure on success and every failure path.

Qualification also runs against a real, disposable AWS S3 bucket. Moto,
LocalStack, MinIO, or a mocked SDK is insufficient evidence because conditional
write/delete and ETag behavior may differ from AWS. Live tests include:

- barrier-synchronized concurrent creates with exactly one winner;
- concurrent manifest updates from one ETag, with distinct valid next-manifest
  bodies, with exactly one winner;
- repeated identical bytes demonstrating that an ETag is not treated as a
  unique generation or repository-integrity hash;
- immediate GET/HEAD/LIST visibility after successful put and delete;
- conditional delete behavior for matching, stale, and missing versions;
- repository initialization, view creation, push, CAS loss, idempotent retry,
  process restart, and full replay with caches removed;
- an injected lost response after a manifest request and successful
  publication-ID resolution;
- backup from S3, restore to a fresh S3 prefix and filesystem, invariant check,
  authenticated clone, permitted push, and cross-view projection;
- failure after isolated target manifest creation proving the source deployment
  remains unchanged and the failed prefix is never activated or reused;
- bucket-policy rejection of unconditional writes/deletes, non-TLS access,
  public ACLs, and non-AES256 writes;
- ordinary runtime-role rejection of all deletes and maintenance-role
  acceptance only for condition-qualified deletes;
- confirmation that ordinary runtime credentials cannot change bucket controls
  or list outside the deployment prefix.

The incremental snapshot operation-count requirements from spec 014 still
apply: an unchanged warm load performs one conditional manifest GET and no WAL
or descriptor reads; a one-record advance reads only the new manifest body,
suffix record, and newly referenced descriptors.

Live AWS qualification is required before the adapter is declared supported.
Skipping all live S3 tests is permitted for ordinary local development but not
for release qualification.

## Execution Order

1. Add the S3 dependency and adapter with shared conformance tests.
2. Add strict repository configuration parsing and initialization preflight.
3. Run repository/WAL/invariant integration tests against real S3.
4. Generalize backup/restore and add format-version-3 compatibility tests.
5. Add Terraform bucket, policies, task-role permissions, and bootstrap wiring.
6. Deploy a fresh test environment, then perform the offline EFS-to-S3 restore
   drill.
7. Run the complete unit/integration suite and deployed smoke test before
   removing filesystem repository truth from the AWS profile.

Each phase preserves the working filesystem adapter. There is no temporary
dual-write mode.

## Non-Goals

- S3-compatible providers or custom endpoints.
- SSE-KMS, customer-provided keys, or client-side encryption.
- S3 Object Lock, legal hold, or compliance retention.
- Multipart uploads, transfer acceleration, presigned client access, or direct
  browser/Git-client access to S3.
- Cross-region replication, multi-region active-active publication, or failover
  DNS.
- WAL/pack garbage collection or deletion of unreachable publication attempts.
- Scaling hosted application replicas beyond the existing single-task boundary.
- Treating noncurrent S3 versions as repository history or a supported rollback
  mechanism.

## Acceptance

- The existing `ObjectStore` interface is unchanged and all callers remain
  storage-kind agnostic.
- S3 `Create` provides atomic put-if-absent semantics. A valid manifest
  `Update` is ETag-conditional and exactly one distinct, content-changing next
  manifest wins under concurrency; ETag reuse is not treated as ABA-proof.
- Manifest CAS remains the sole commit point; no S3 listing, version ID, EFS
  file, or database row can commit repository state.
- ETags are used only as opaque conditional validators; all existing SHA-256,
  pack, replay, authorization, and invariant checks remain in force.
- Definite precondition loss, definite rejection, transient read failure, and
  ambiguous mutation outcomes are observably distinct and safely resolved.
- Every write is TLS-protected, condition-qualified, SSE-S3 encrypted, and
  constrained to the deployment prefix by IAM and bucket policy.
- A verified backup restores offline from filesystem to S3 and from S3 to a
  fresh isolated target without overwriting an existing manifest or partially
  activating a failed restore.
- Deleting EFS materializations cannot change repository truth and a restored
  task rebuilds them from S3.
- Real AWS concurrency, ambiguity, backup/restore, IAM-negative, invariant, and
  deployed Git smoke tests pass before support is claimed.

## AWS References

- Conditional writes: https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html
- Conditional-write policy enforcement: https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes-enforce.html
- Conditional deletes: https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-deletes.html
- Strong consistency: https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html#ConsistencyModel
- ETag semantics: https://docs.aws.amazon.com/AmazonS3/latest/API/API_Object.html
- Upload checksums: https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html
- SDK retry behavior: https://docs.aws.amazon.com/sdkref/latest/guide/feature-retry-behavior.html
- Resource-account policy keys: https://docs.aws.amazon.com/AmazonS3/latest/userguide/amazon-s3-policy-keys.html#example-object-resource-account
- Lifecycle expiration: https://docs.aws.amazon.com/AmazonS3/latest/userguide/lifecycle-expire-general-considerations.html
