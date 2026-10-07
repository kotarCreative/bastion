#!/usr/bin/env bash
set -euo pipefail

module_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd "${module_dir}/../../.." && pwd)
var_file="${1:-}"

for command in aws docker git python3 terraform; do
  command -v "${command}" >/dev/null || {
    echo "Required command is unavailable: ${command}" >&2
    exit 1
  }
done

var_args=()
if [ -n "${var_file}" ]; then
  if [ ! -f "${module_dir}/${var_file}" ] && [ ! -f "${var_file}" ]; then
    echo "Terraform variable file not found: ${var_file}" >&2
    exit 1
  fi
  if [ -f "${module_dir}/${var_file}" ]; then
    var_file=$(python3 -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "${module_dir}/${var_file}")
  else
    var_file=$(python3 -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "${var_file}")
  fi
  var_args=(-var-file="${var_file}")
fi

tf() {
  terraform -chdir="${module_dir}" "$@"
}

existing_image=""
if tf state show 'aws_ecs_service.main[0]' >/dev/null 2>&1; then
  cluster=$(tf output -raw ecs_cluster_name)
  service=$(tf output -raw ecs_service_name)
  current_task_definition=$(aws ecs describe-services \
    --region "$(tf output -raw aws_region)" \
    --cluster "${cluster}" \
    --services "${service}" \
    --query 'services[0].taskDefinition' \
    --output text)
  # The JMESPath backticks are literals, not shell expansion.
  # shellcheck disable=SC2016
  existing_image=$(aws ecs describe-task-definition \
    --region "$(tf output -raw aws_region)" \
    --task-definition "${current_task_definition}" \
    --query 'taskDefinition.containerDefinitions[?name==`bastion-web`].image | [0]' \
    --output text)
  if [[ ! "${existing_image}" =~ @sha256:[0-9a-f]{64}$ ]]; then
    echo "Existing ECS service does not use a digest-qualified image: ${existing_image}" >&2
    exit 1
  fi
  echo "Refreshing the AWS foundation while the existing service remains available..."
  tf apply "${var_args[@]}" \
    -var='deploy_service=true' \
    -var="container_image=${existing_image}" \
    -var="bootstrap_container_image=${existing_image}"
else
  echo "Provisioning the AWS foundation without an ECS service..."
  tf apply "${var_args[@]}" \
    -var='deploy_service=false' \
    -var='container_image=' \
    -var='bootstrap_container_image='
fi

region=$(tf output -raw aws_region)
repository_url=$(tf output -raw ecr_repository_url)
repository_name=$(tf output -raw ecr_repository_name)
registry=${repository_url%%/*}
tag="$(git -C "${repository_root}" rev-parse --short=12 HEAD)-$(date -u +%Y%m%d%H%M%S)"

aws ecr get-login-password --region "${region}" |
  docker login --username AWS --password-stdin "${registry}"
docker build --platform linux/amd64 --tag "${repository_url}:${tag}" "${repository_root}"
docker push "${repository_url}:${tag}"

digest=$(aws ecr describe-images \
  --region "${region}" \
  --repository-name "${repository_name}" \
  --image-ids "imageTag=${tag}" \
  --query 'imageDetails[0].imageDigest' \
  --output text)
if [[ ! "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "ECR returned an invalid image digest: ${digest}" >&2
  exit 1
fi
image="${repository_url}@${digest}"

echo "Creating the digest-pinned bootstrap task definition..."
if [ -n "${existing_image}" ]; then
  tf apply "${var_args[@]}" \
    -var='deploy_service=true' \
    -var="container_image=${existing_image}" \
    -var="bootstrap_container_image=${image}"
else
  tf apply "${var_args[@]}" \
    -var='deploy_service=false' \
    -var='container_image=' \
    -var="bootstrap_container_image=${image}"
fi

cluster=$(tf output -raw ecs_cluster_name)
task_definition=$(tf output -raw bootstrap_task_definition_arn)
subnets=$(tf output -json public_subnet_ids | python3 -c 'import json,sys; print(",".join(json.load(sys.stdin)))')
security_group=$(tf output -raw ecs_security_group_id)
log_group=$(tf output -raw cloudwatch_log_group)

"${module_dir}/init-forge.sh" \
  "${cluster}" "${task_definition}" "${subnets}" \
  "${security_group}" "${log_group}" "${region}"

echo "Starting the long-lived service..."
tf apply "${var_args[@]}" \
  -var='deploy_service=true' \
  -var="container_image=${image}" \
  -var="bootstrap_container_image=${image}"

service=$(tf output -raw ecs_service_name)
echo "Waiting for ECS service ${service} to stabilize..."
aws ecs wait services-stable \
  --region "${region}" \
  --cluster "${cluster}" \
  --services "${service}"

echo "Deployment complete: $(tf output -raw bastion_url)"
echo "Git/API tokens secret: $(tf output -raw bootstrap_tokens_secret_arn)"
