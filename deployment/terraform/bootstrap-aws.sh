#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd "${script_dir}/../.." && pwd)
bootstrap_dir="${script_dir}/aws-state"
application_dir="${script_dir}/aws"

for command in aws gh git terraform; do
  command -v "${command}" >/dev/null || {
    echo "Required command is unavailable: ${command}" >&2
    exit 1
  }
done

region="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
account_id=$(aws sts get-caller-identity --query Account --output text)

github_repository="${BASTION_GITHUB_REPOSITORY:-}"
if [ -z "${github_repository}" ]; then
  origin=$(git -C "${repository_root}" remote get-url origin)
  case "${origin}" in
    git@github.com:*) github_repository=${origin#git@github.com:} ;;
    https://github.com/*) github_repository=${origin#https://github.com/} ;;
    *)
      echo "Set BASTION_GITHUB_REPOSITORY=OWNER/REPOSITORY; origin is not a GitHub URL." >&2
      exit 1
      ;;
  esac
  github_repository=${github_repository%.git}
fi

case "${github_repository}" in
  */*) ;;
  *)
    echo "BASTION_GITHUB_REPOSITORY must use OWNER/REPOSITORY form." >&2
    exit 1
    ;;
esac

if ! gh auth status >/dev/null 2>&1; then
  echo "An authenticated GitHub CLI with repository administration access is required." >&2
  exit 1
fi

environments_ready=1
for environment in aws-dev aws-s3-qualification; do
  if ! gh api "repos/${github_repository}/environments/${environment}" >/dev/null 2>&1; then
    gh api --method PUT "repos/${github_repository}/environments/${environment}" >/dev/null
    echo "Created GitHub environment ${environment}."
    environments_ready=0
  fi
  protected=$(gh api "repos/${github_repository}/environments/${environment}" --jq '
    any(.protection_rules[]?; .type == "required_reviewers") or
    (.deployment_branch_policy.protected_branches == true) or
    (.deployment_branch_policy.custom_branch_policies == true)
  ')
  if [ "${protected}" != "true" ]; then
    echo "GitHub environment ${environment} has no reviewer or branch protection." >&2
    environments_ready=0
  fi
done

if [ "${environments_ready}" != "1" ]; then
  echo "Protect both GitHub environments, then rerun this command." >&2
  echo "No AWS bootstrap resources were changed." >&2
  exit 2
fi

oidc_arn="arn:aws:iam::${account_id}:oidc-provider/token.actions.githubusercontent.com"
oidc_args=()
terraform -chdir="${bootstrap_dir}" init -input=false
if terraform -chdir="${bootstrap_dir}" state show \
  'aws_iam_openid_connect_provider.github[0]' >/dev/null 2>&1; then
  : # This root already owns the provider; keep managing it.
elif aws iam get-open-id-connect-provider \
  --open-id-connect-provider-arn "${oidc_arn}" >/dev/null 2>&1; then
  oidc_args=(-var="github_oidc_provider_arn=${oidc_arn}")
fi

terraform -chdir="${bootstrap_dir}" apply -input=false -auto-approve \
  -var="aws_region=${region}" \
  -var="github_repository=${github_repository}" \
  "${oidc_args[@]}"

state_bucket=$(terraform -chdir="${bootstrap_dir}" output -raw bucket_name)
state_key=$(terraform -chdir="${bootstrap_dir}" output -raw application_state_key)
deploy_role=$(terraform -chdir="${bootstrap_dir}" output -raw github_deploy_role_arn)
qualification_role=$(terraform -chdir="${bootstrap_dir}" output -raw github_qualification_role_arn)
boundary=$(terraform -chdir="${bootstrap_dir}" output -raw application_permissions_boundary_arn)

umask 077
backend_file="${application_dir}/backend-dev.hcl"
backend_tmp="${backend_file}.tmp"
printf 'bucket = "%s"\nkey = "%s"\nregion = "%s"\nencrypt = true\nuse_lockfile = true\n' \
  "${state_bucket}" "${state_key}" "${region}" >"${backend_tmp}"
mv "${backend_tmp}" "${backend_file}"

boundary_file="${application_dir}/bootstrap.auto.tfvars"
boundary_tmp="${boundary_file}.tmp"
printf 'permissions_boundary_arn = "%s"\n' "${boundary}" >"${boundary_tmp}"
mv "${boundary_tmp}" "${boundary_file}"

terraform -chdir="${application_dir}" init -input=false -reconfigure \
  -backend-config="${backend_file}"

gh variable set AWS_ROLE_ARN --repo "${github_repository}" --env aws-dev --body "${deploy_role}"
gh variable set AWS_REGION --repo "${github_repository}" --env aws-dev --body "${region}"
gh variable set TF_STATE_BUCKET --repo "${github_repository}" --env aws-dev --body "${state_bucket}"
gh variable set TF_STATE_KEY --repo "${github_repository}" --env aws-dev --body "${state_key}"
gh variable set PERMISSIONS_BOUNDARY_ARN --repo "${github_repository}" --env aws-dev --body "${boundary}"
gh variable set AWS_ROLE_ARN --repo "${github_repository}" --env aws-s3-qualification --body "${qualification_role}"
echo "Configured protected GitHub environments and non-secret deployment variables."

echo "Bootstrap complete for ${account_id} in ${region}."
echo "Protect the aws-dev and aws-s3-qualification GitHub environments before deployment."
echo "Local application backend: ${backend_file}"
