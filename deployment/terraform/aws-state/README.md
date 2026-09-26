# AWS Bootstrap Root

This root establishes the non-disposable trust boundary used by the application
root. It creates:

- the encrypted, versioned S3 bucket used by the application Terraform backend;
- the GitHub Actions OIDC provider, unless the account already has one;
- separate protected-workflow roles for deployment and live-S3 qualification;
- the permissions boundary required on every application-created ECS role.

It intentionally uses local state: Terraform cannot create the bucket that
stores its own state or the role through which its first apply authenticates.

From the repository root, run the bootstrap orchestrator with an AWS
administrator or dedicated account-provisioning identity:

```bash
deployment/terraform/bootstrap-aws.sh
```

The script derives `OWNER/REPOSITORY` from the Git origin, uses an account-scoped
state-bucket name, applies this root, writes ignored backend and permissions
boundary files for local use, and initializes the application root. Override
its defaults with `AWS_REGION` and `BASTION_GITHUB_REPOSITORY`.

An authenticated GitHub CLI with repository-administration access is required.
On the first run, the script creates the `aws-dev` and
`aws-s3-qualification` environments and exits before creating AWS resources.

After that first run, configure required reviewers and deployment-branch protection
for both GitHub environments. Then rerun the same command:

```bash
deployment/terraform/bootstrap-aws.sh
```

The second run verifies those protections before it creates the OIDC roles,
applies the bootstrap root, and installs the non-secret workflow variables.
Those approval rules deliberately cannot be created by repository-controlled
automation.

For a local application deployment, configure `dev.tfvars` and run:

```bash
deployment/terraform/aws/deploy-dev.sh dev.tfvars
```

Keep this root's local `terraform.tfstate` in an operator-controlled encrypted
location. It contains trust configuration but no generated application
credentials. The state bucket has `prevent_destroy`; deliberate retirement
requires removing that lifecycle guard and emptying every object version.

If the AWS account already has the GitHub OIDC provider, the bootstrap script
detects and references it without taking ownership. Direct Terraform users can
set `github_oidc_provider_arn` explicitly. Do not switch a provider already
owned by this state to the external-provider variable; `prevent_destroy` blocks
that ownership change until it is handled deliberately.
