# Alchemy Deployment Evaluation

Evaluated against Alchemy `2.0.0-beta.79` on 2026-09-23.

## Decision

Keep Terraform as the only owner of the Bastion AWS environment. Consider an
isolated Alchemy pilot for disposable local or pull-request environments, but do
not adopt the Terraform-managed stack or split one environment across both
state systems.

Alchemy has typed resources for the services this deployment needs: VPC and
security groups, ECS Fargate, ECR images, ALB, RDS PostgreSQL, EFS, S3 and IAM,
Secrets Manager, CloudWatch, Route 53, and ACM. Its stages, TypeScript control
flow, deploy-time actions, and local AWS emulation could make iteration on the
three-stage image/bootstrap/service sequence more approachable.

The current tradeoffs are not favorable for the authoritative deployment:

- Alchemy remains a beta release and its AWS providers are changing quickly.
- Its S3 state store does not provide Terraform's native S3 lockfile behavior;
  GitHub concurrency would not protect against a local deployment racing CI.
- `alchemy plan` compares declarations with stored inputs; live drift requires
  a separate command, and there is no saved plan artifact to apply later.
- Secrets Manager deletion currently forces immediate deletion, unlike this
  stack's seven-day recovery windows.
- A faithful Bastion deployment still needs custom orchestration to run and
  verify the one-off bootstrap task before creating the long-lived service.
- Using Terraform for data services and Alchemy for ECS would create two state,
  drift, recovery, and destroy procedures without removing that orchestration.

## Useful Pilot Scope

If Alchemy is evaluated further, use a separate AWS sandbox account and a name
prefix that cannot overlap Terraform resources. Exercise:

1. local ECS containers with local RDS and S3 emulation;
2. digest-pinned image publication;
3. failure and retry of the one-off bootstrap task;
4. service gating on successful bootstrap;
5. EFS cache deletion and reconstruction;
6. drift detection, interrupted deployment recovery, and complete destroy.

Real AWS S3 conditional requests and bucket-policy enforcement must still use
the protected qualification workflow; local emulation is not qualification.

## Sources

- [Alchemy 2.0.0-beta.79 release](https://github.com/alchemy-run/alchemy/releases/tag/v2.0.0-beta.79)
- [AWS local development](https://alchemy.run/aws/local-development/)
- [AWS ECS resources](https://alchemy.run/aws/compute/ecs/)
- [AWS networking resources](https://alchemy.run/aws/networking/)
- [AWS state setup](https://alchemy.run/aws/setup/#state-storage)
- [State stores and locking](https://alchemy.run/state-store/)
- [Plan behavior](https://alchemy.run/cli/plan/)
- [Drift detection](https://alchemy.run/cli/drift/)
- [Resource adoption](https://alchemy.run/cli/adopting-resources/)
- [CI and GitHub OIDC](https://alchemy.run/environments/ci/#aws-with-github-oidc-recommended)
