# Bastion AWS Dev Deployment

This Terraform root creates one disposable Bastion test environment on AWS. It
is for invite-only testing with synthetic data, not production repositories.

## Architecture

- A public Application Load Balancer fronts one ECS Fargate task.
- The task contains one Gunicorn web container and one projection worker.
- S3 is authoritative repository truth.
- RDS PostgreSQL stores the control plane.
- EFS stores forge configuration and disposable Git materializations.
- Secrets Manager stores separate database administration/runtime credentials,
  generated browser passwords, and generated Git/API tokens.
- ECR stores immutable image tags; ECS always uses a digest-qualified image.

The long-lived task receives only the restricted PostgreSQL runtime credential.
A separate one-off bootstrap task receives the administrator credential, creates
the runtime role/database, initializes the forge, writes generated tokens to
Secrets Manager, and exits before the service is enabled.

## Requirements

- one completed [AWS bootstrap root](../aws-state/README.md)
- a dedicated AWS test account; the deployment role can manage the networking,
  compute, database, and filesystem resources needed by this disposable stack
- AWS CLI v2, Terraform 1.10 or newer, Docker, Git, and Python 3 for local deploys
- either an ACM certificate plus matching hostname, or a restricted operator
  CIDR for temporary HTTP access
- protected `aws-dev` and `aws-s3-qualification` GitHub environments

## 1. Bootstrap AWS And GitHub Trust

Run once with a privileged AWS identity:

```bash
deployment/terraform/bootstrap-aws.sh
```

This creates the remote-state bucket, GitHub OIDC roles, and application
permissions boundary. It also writes the ignored local backend configuration
and `bootstrap.auto.tfvars`. Terraform state contains generated credentials;
restrict access to the state bucket.

If the first invocation creates GitHub environments, protect both with required
reviewers or deployment-branch rules, then rerun the same command:

```bash
deployment/terraform/bootstrap-aws.sh
```

The script verifies protection before creating OIDC roles or publishing their
ARNs.

## 2. Configure Access

The deployment workflow supports two access modes:

- configure the `DOMAIN_NAME` and `ACM_CERTIFICATE_ARN` variables in `aws-dev`,
  plus `HOSTED_ZONE_ID` when Terraform should create the Route 53 record; or
- supply an operator IPv4 `/32` when dispatching the workflow for restricted
  temporary HTTP access. The runner's current `/32` is added automatically so
  the smoke test can reach the deployment.

The HTTP mode is suitable only for a connectivity smoke test with synthetic
credentials. A CIDR allowlist does not encrypt credentials in transit; use ACM
before inviting other testers.

For a local deployment, create the ignored variable file:

```bash
cp deployment/terraform/aws/dev.tfvars.example \
  deployment/terraform/aws/dev.tfvars
# Replace the documentation CIDR or configure ACM and DNS.
```

## 3. Deploy Through GitHub

```bash
gh workflow run aws-deploy.yml -f operator_cidr=<your-public-ip>/32
```

The protected workflow assumes the bootstrap-managed deployment role, uses the
remote backend, performs the staged deployment, waits for ECS stability, reads
short-lived test tokens from Secrets Manager, runs the deployed smoke test, and
then invokes protected live-S3 qualification against the exact repository
bucket.

## 4. Deploy Locally

The same staged orchestrator remains available locally:

```bash
deployment/terraform/aws/deploy-dev.sh dev.tfvars
```

The script intentionally performs three Terraform applies:

1. Create the AWS foundation and empty ECR repository without a service.
2. Build and push a unique immutable tag, resolve its digest, and create the
   dedicated bootstrap task definition.
3. Run bootstrap successfully, then enable the long-lived ECS service using
   the exact same image digest.

This sequence prevents an empty ECR repository or uninitialized forge from
causing an ECS deployment failure. On repeat deployments, the existing service
continues using its previous digest until the new bootstrap task succeeds; the
final apply then performs the ECS rolling update.

## 5. Retrieve Credentials

The deployment prints the two secret ARNs. Retrieve their current values:

```bash
aws secretsmanager get-secret-value \
  --secret-id "$(terraform -chdir=deployment/terraform/aws output -raw demo_accounts_secret_arn)" \
  --query SecretString --output text

aws secretsmanager get-secret-value \
  --secret-id "$(terraform -chdir=deployment/terraform/aws output -raw bootstrap_tokens_secret_arn)" \
  --query SecretString --output text
```

Raw passwords and tokens are not printed to CloudWatch logs or Terraform
outputs. Re-running bootstrap stores new Git/API tokens and revokes the tokens
from the previous secret version. It intentionally does not replace passwords
on already-created accounts.

## 6. Verify

Export the tokens returned from Secrets Manager, then run:

```bash
export BASTION_URL=$(terraform -chdir=deployment/terraform/aws output -raw bastion_url)
export BASTION_TESTER_TOKEN=<tester-token>
export BASTION_OBSERVER_TOKEN=<observer-token>
export BASTION_INSECURE_TLS=0
deployment/terraform/aws/smoke-test.sh
```

Also run the invariant checker through a one-off task before treating the
environment as qualified. The deployed smoke test proves authenticated API,
clone, push, worker propagation, and cross-view filtering.

## Qualification Boundary

Hermetic S3 adapter tests run in ordinary CI. Real AWS conditional-write and
bucket-policy tests run only through the protected `AWS S3 qualification`
workflow against the exact deployed repository bucket's reserved
`qualification/` prefix. See `docs/S3_QUALIFICATION.md`.

A test deployment is qualified only after all of these pass for the deployed
commit:

- ordinary CI and PostgreSQL-backed storage tests
- protected live-S3 qualification
- deployment smoke test
- task restart with EFS materializations removed and rebuilt from S3
- backup/restore into an isolated prefix followed by invariant and clone tests

## Operations And Cleanup

The profile deliberately runs one task. Admission accounting and repository
caches are process-local, so do not increase `desired_count` without further
design work.

RDS retains seven days of automated backups, but deletion protection and final
snapshots are disabled for disposable testing. S3 noncurrent versions default
to 30-day retention. Destroying the stack permanently removes RDS, EFS, and the
repository bucket:

```bash
terraform -chdir=deployment/terraform/aws destroy \
  -var-file=dev.tfvars \
  -var='deploy_service=true' \
  -var='container_image=<the-last-deployed-repository@sha256:digest>'
```

The remote-state bucket is intentionally protected and managed by its separate
bootstrap root.
