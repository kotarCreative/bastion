#!/usr/bin/env bash
set -euo pipefail

# Usage: init-forge.sh <cluster-name> <task-definition-family-or-arn> <subnets> <security-group-id> <log-group> <aws-region>
# Example (values come from Terraform outputs):
#   init-forge.sh bastion-test bastion-test 'subnet-xxx,subnet-yyy' sg-zzz /ecs/bastion-test us-east-1

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

run_output=$(aws ecs run-task \
  --region "${region}" \
  --cluster "${cluster}" \
  --task-definition "${task_definition}" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[${quoted_subnets}],securityGroups=[\"${security_group}\"],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[
    {"name":"bastion-web","command":["/app/deployment/bootstrap-demo.sh"]},
    {"name":"bastion-worker","command":["sh","-c","exit 0"]}
  ]}' \
  --query 'tasks[0].taskArn' \
  --output text)

task_arn="${run_output}"
echo "Bootstrap task started: ${task_arn}"

echo "Waiting for bootstrap task to stop..."
aws ecs wait tasks-stopped \
  --region "${region}" \
  --cluster "${cluster}" \
  --tasks "${task_arn}"

exit_code=$(aws ecs describe-tasks \
  --region "${region}" \
  --cluster "${cluster}" \
  --tasks "${task_arn}" \
  --query 'tasks[0].containers[?name==`bastion-web`].exitCode' \
  --output text)

if [ "${exit_code}" != "0" ]; then
  echo "Bootstrap task failed with exit code ${exit_code}" >&2
fi

echo "Bootstrap task stopped (exit code ${exit_code}). Fetching logs..."
aws logs filter-log-events \
  --region "${region}" \
  --log-group-name "${log_group}" \
  --filter-pattern "" \
  --limit 100 \
  --query 'events[*].message' \
  --output text || true

echo ""
echo "To view logs continuously, run:"
echo "  aws logs tail ${log_group} --region ${region} --follow"

exit "${exit_code}"
