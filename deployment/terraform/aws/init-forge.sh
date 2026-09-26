#!/usr/bin/env bash
set -euo pipefail

# Usage: init-forge.sh <cluster-name> <task-definition-arn> <subnets> <security-group-id> <log-group> <aws-region>
# Example (values come from Terraform outputs):
#   init-forge.sh bastion-test arn:aws:ecs:us-east-1:123456789012:task-definition/bastion-test-bootstrap:1 'subnet-xxx,subnet-yyy' sg-zzz /ecs/bastion-test us-east-1

cluster="${1:?cluster name is required}"
task_definition="${2:?task definition family is required}"
subnets="${3:?subnets are required}"
security_group="${4:?security group id is required}"
log_group="${5:?log group name is required}"
region="${6:-us-east-1}"

# Convert comma-separated subnet IDs into a JSON-quoted list.
quoted_subnets=""
IFS=',' read -ra subnet_ids <<<"${subnets}"
for id in "${subnet_ids[@]}"; do
  quoted_subnets="${quoted_subnets}\"${id}\","
done
quoted_subnets="${quoted_subnets%,}"

echo "Running Bastion bootstrap task on cluster ${cluster}..."

family_revision=${task_definition##*/}
family=${family_revision%:*}
running_task=$(aws ecs list-tasks \
  --region "${region}" \
  --cluster "${cluster}" \
  --family "${family}" \
  --desired-status RUNNING \
  --query 'taskArns[0]' \
  --output text)
if [ -n "${running_task}" ] && [ "${running_task}" != "None" ]; then
  echo "A bootstrap task is already running: ${running_task}" >&2
  exit 1
fi

client_token=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
run_output=$(aws ecs run-task \
  --region "${region}" \
  --cluster "${cluster}" \
  --task-definition "${task_definition}" \
  --launch-type FARGATE \
  --platform-version 1.4.0 \
  --client-token "${client_token}" \
  --started-by bastion-deploy \
  --network-configuration "awsvpcConfiguration={subnets=[${quoted_subnets}],securityGroups=[\"${security_group}\"],assignPublicIp=ENABLED}" \
  --output json)

task_arn=$(printf '%s' "${run_output}" | python3 -c 'import json, sys; payload=json.load(sys.stdin); print(payload.get("tasks", [{}])[0].get("taskArn", ""))')
if [ -z "${task_arn}" ]; then
  echo "AWS did not start the bootstrap task:" >&2
  printf '%s\n' "${run_output}" >&2
  exit 1
fi
echo "Bootstrap task started: ${task_arn}"

stop_on_signal() {
  echo "Stopping interrupted bootstrap task ${task_arn}..." >&2
  aws ecs stop-task \
    --region "${region}" \
    --cluster "${cluster}" \
    --task "${task_arn}" \
    --reason "Deployment launcher interrupted" >/dev/null || true
  exit 130
}
trap stop_on_signal INT TERM

echo "Waiting for bootstrap task to stop..."
aws ecs wait tasks-stopped \
  --region "${region}" \
  --cluster "${cluster}" \
  --tasks "${task_arn}"
trap - INT TERM

# The JMESPath backticks are literals, not shell expansion.
# shellcheck disable=SC2016
exit_code=$(aws ecs describe-tasks \
  --region "${region}" \
  --cluster "${cluster}" \
  --tasks "${task_arn}" \
  --query 'tasks[0].containers[?name==`bastion-bootstrap`].exitCode' \
  --output text)

echo "Bootstrap task stopped (exit code ${exit_code}). Fetching logs..."
aws logs filter-log-events \
  --region "${region}" \
  --log-group-name "${log_group}" \
  --filter-pattern "" \
  --limit 100 \
  --query 'events[*].message' \
  --output text || true

if [ "${exit_code}" != "0" ]; then
  echo "Bootstrap task failed with exit code ${exit_code:-unknown}" >&2
  exit 1
fi

echo "Bootstrap completed. Credentials are in the Secrets Manager outputs."
