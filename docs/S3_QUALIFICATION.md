# AWS S3 Qualification

The `AWS S3 qualification` GitHub Actions workflow is the only live-cloud test harness in this repository. It is manually dispatched and is separate from ordinary, hermetic CI because it assumes an AWS role and mutates a real S3 bucket.

## Protected Environment

Create a GitHub environment named `aws-s3-qualification` and configure required reviewers or another deployment protection rule. The workflow's `environment` declaration makes the job wait for that protection; repository YAML cannot create or enforce the protection rule itself.

Configure these environment values:

| Kind | Name | Purpose |
| --- | --- | --- |
| Variable | `AWS_ROLE_ARN` | Bootstrap-managed IAM role assumed through GitHub OIDC. An ARN is not secret material. |

`deployment/terraform/bootstrap-aws.sh` creates the role, configures the
environment variable when GitHub CLI access is available, and restricts trust
to this repository and environment
(`sub: repo:OWNER/REPOSITORY:environment:aws-s3-qualification`). The role can
access only `qualification/` in buckets matching this deployment's account and
name family. It cannot alter bucket controls.

The deployment workflow passes the exact repository bucket, account, and region
to the reusable qualification workflow. A manual dispatch requires those three
non-secret values as inputs. The application bucket has versioning, default
SSE-S3 (`AES256`) encryption, Block Public Access, and the conditional-request
policy provisioned by the application Terraform root.

The bucket policy must deny `PutObject` without `If-Match` or `If-None-Match`, deny `DeleteObject` without `If-Match`, deny non-AES256 writes, and deny insecure transport. Qualification intentionally verifies that an unconditional SDK write is rejected.

## Running Qualification

Ordinarily, dispatch **AWS dev deployment**; qualification runs after its smoke
test. To rerun qualification alone, select **AWS S3 qualification**, enter the
application outputs `repository_bucket`, `repository_bucket_owner`, and
`aws_region`, then approve the protected environment deployment. Each attempt
uses `qualification/<run-id>-<attempt>` as a fresh prefix. The workflow obtains
short-lived credentials through OIDC and prints only the bucket and test
prefix, never credentials or signed requests.

The live harness verifies:

- bucket region, enabled versioning, and default AES256 encryption;
- conditional create, update, stale-update rejection, and conditional delete;
- immediate GET, HEAD, and LIST visibility after writes and deletion;
- exactly one winner for barrier-synchronized concurrent creates and updates;
- repeated identical bytes producing a repeated ETag, so ETags are not treated as unique generations;
- bucket-policy rejection of an unconditional write.

Current objects under the run prefix are conditionally deleted in a `finally`
block. Because bucket versioning is required, noncurrent versions and delete
markers remain until the bucket lifecycle policy expires them. The application
bucket also expires abandoned current and noncurrent objects under
`qualification/` after seven days.

## Evidence And Residual Gaps

Record the workflow URL, commit SHA, run attempt, region, bucket, and unique prefix in release evidence. A passing run qualifies only the exercised adapter and AWS behavior. It does not qualify S3-compatible services, another bucket policy, another region, or credentials with different permissions.

The harness does not yet cover the complete live scenarios specified for production support: repository initialization and Git/view workflows, publication-ID recovery after an injected lost response, S3-to-S3 and S3-to-filesystem backup/restore, failed-restore isolation, authenticated clone/push and cross-view projection, public ACL rejection, non-TLS transport, runtime-versus-maintenance role separation, bucket-control denial, or out-of-prefix LIST denial. Those remain release-blocking gaps if full spec-016 S3 support is claimed. Mocked or hermetic tests cannot close them.
