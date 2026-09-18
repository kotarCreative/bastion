# Bastion AWS Test Deployment (Terraform)

This Terraform root module deploys a single Bastion test instance to AWS using
Fargate, RDS PostgreSQL, EFS, and an Application Load Balancer. It is intended
for hosting and testing the codebase, not as a production multi-tenant setup.

## What is included

- **VPC** with public subnets for the ALB/ECS tasks and private subnets for RDS.
- **NAT gateways** so tasks in public subnets can reach RDS in private subnets.
- **ECR** repository for the Bastion container image.
- **RDS PostgreSQL** for the control-plane store (replacing local SQLite).
- **Secrets Manager** secret for the PostgreSQL DSN.
- **EFS** with an access point for `/var/lib/bastion` (object store and Git
  materializations).
- **ECS Fargate** service running two containers:
  - `bastion-web`: Gunicorn serving the WSGI app (browser UI + Git smart-HTTP).
  - `bastion-worker`: projection worker loop.
- **Application Load Balancer** with optional HTTPS (ACM certificate).
- **Route 53** alias record when `domain_name` and `hosted_zone_id` are supplied.

## What you must still provide

1. A container image pushed to the created ECR repository.
2. TLS certificate in ACM if you want HTTPS (recommended).
3. A Route 53 hosted zone if you want a friendly DNS name.

## Deploy

```bash
cd deployment/terraform/aws
terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

After apply, Terraform prints:

- `bastion_url` — the URL to reach the service.
- `ecr_repository_url` — where to push the image.
- `bootstrap_command` — the exact command to seed demo data.

## Build and push the image

```bash
cd ../../..  # repository root
aws ecr get-login-password --region <region> | \
  docker login --username AWS --password-stdin <ecr_repository_url>

docker build -t bastion:aws .
docker tag bastion:aws <ecr_repository_url>:latest
docker push <ecr_repository_url>:latest
```

## Bootstrap demo data

Run the one-off bootstrap task. It creates the seed repository, initializes
Bastion against RDS, and prints tester/observer credentials to CloudWatch Logs.

```bash
./init-forge.sh <cluster-name> <task-definition-family> '<subnets>' <security-group-id> /ecs/<cluster-name> <region>
```

The exact values are in the `bootstrap_command` Terraform output. Credentials
are also visible in the CloudWatch log group `/ecs/<name>` under the
`bastion-web` container stream.

## Smoke test

```bash
export BASTION_URL=<bastion_url>
export BASTION_TESTER_TOKEN=<token-from-logs>
export BASTION_OBSERVER_TOKEN=<token-from-logs>
export BASTION_INSECURE_TLS=0

./smoke-test.sh
```

## Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `name_prefix` | `bastion` | Resource name prefix. |
| `environment` | `test` | Environment tag. |
| `aws_region` | `us-east-1` | AWS region. |
| `domain_name` | `""` | Public DNS name (optional). |
| `hosted_zone_id` | `""` | Route 53 hosted zone ID (optional). |
| `acm_certificate_arn` | `""` | ACM certificate ARN for HTTPS (optional). |
| `container_image` | `""` | Image URI; empty uses ECR repo `:latest`. |
| `db_instance_class` | `db.t4g.micro` | RDS instance class. |
| `ecs_task_cpu` | `1024` | Fargate task CPU. |
| `ecs_task_memory` | `2048` | Fargate task memory. |

## Cleanup

```bash
terraform destroy
```

`force_delete` is enabled on the ECR repository so destroy succeeds even when
images exist. The EFS and RDS resources will be deleted; make sure you have no
data you want to keep.

## Limitations

- This deploys **one** Bastion task. Admission accounting and repository caches
  are process-local, so do not scale the service to more than one task without
  further design changes.
- The object store currently lives on EFS (filesystem-backed). S3-backed object
  storage is not yet implemented.
- The ALB is the visible network peer, so per-IP admission limits apply to the
  ALB rather than individual clients.
