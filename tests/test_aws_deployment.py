"""Static contracts joining the AWS task templates and launcher scripts."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
AWS = ROOT / "deployment" / "terraform" / "aws"
BOOTSTRAP = ROOT / "deployment" / "terraform" / "aws-state"
WORKFLOWS = ROOT / ".github" / "workflows"


class AwsDeploymentContractTest(unittest.TestCase):
    def test_bootstrap_launcher_queries_the_defined_container(self) -> None:
        definition = json.loads(
            (AWS / "bootstrap_container_definition.json.tftpl").read_text()
        )
        self.assertEqual([container["name"] for container in definition], ["bastion-bootstrap"])
        launcher = (AWS / "init-forge.sh").read_text()
        self.assertIn("name==`bastion-bootstrap`", launcher)
        self.assertNotIn("name==`bastion-web`", launcher)

    def test_long_lived_tasks_receive_only_the_runtime_database_secret(self) -> None:
        definition = json.loads(
            (AWS / "container_definitions.json.tftpl").read_text()
        )
        for container in definition:
            secret_names = {secret["name"] for secret in container["secrets"]}
            environment_names = {
                variable["name"] for variable in container["environment"]
            }
            self.assertEqual(secret_names, {"BASTION_DATABASE"})
            self.assertNotIn("BASTION_ADMIN_DATABASE", environment_names)
            self.assertNotIn("BASTION_TESTER_PASSWORD", environment_names)
            self.assertNotIn("BASTION_OBSERVER_PASSWORD", environment_names)

    def test_every_application_role_uses_the_bootstrap_boundary(self) -> None:
        iam = (AWS / "iam.tf").read_text()
        self.assertEqual(iam.count("resource \"aws_iam_role\""), 4)
        self.assertEqual(
            iam.count("permissions_boundary = var.permissions_boundary_arn"), 4
        )
        variables = (AWS / "variables.tf").read_text()
        self.assertIn('variable "permissions_boundary_arn"', variables)

    def test_bootstrap_owns_oidc_roles_and_state_access(self) -> None:
        identity = (BOOTSTRAP / "identity.tf").read_text()
        self.assertIn('resource "aws_iam_openid_connect_provider" "github"', identity)
        self.assertIn('resource "aws_iam_role" "github_deploy"', identity)
        self.assertIn('resource "aws_iam_role" "github_qualification"', identity)
        self.assertIn("var.application_state_key", identity)
        self.assertIn("iam:PermissionsBoundary", identity)
        self.assertIn("CreateRequiredServiceLinkedRoles", identity)
        self.assertIn("${local.bootstrap_name}-github-deploy", identity)
        self.assertIn("prevent_destroy = true", identity)
        self.assertNotIn("AdministratorAccess", identity)
        self.assertNotIn('"secretsmanager:*"', identity)
        qualification = identity.split(
            'resource "aws_iam_role_policy" "github_qualification"', 1
        )[1]
        self.assertNotIn("s3:PutBucket", qualification)
        self.assertNotIn("s3:DeleteBucket", qualification)

    def test_workflows_use_oidc_and_protected_environments(self) -> None:
        deploy = (WORKFLOWS / "aws-deploy.yml").read_text()
        qualification = (WORKFLOWS / "aws-s3-qualification.yml").read_text()
        for workflow in (deploy, qualification):
            self.assertIn("id-token: write", workflow)
            self.assertIn("aws-actions/configure-aws-credentials", workflow)
            self.assertNotIn("AWS_ACCESS_KEY_ID", workflow)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", workflow)
            self.assertNotRegex(workflow, r"uses: [^\s]+@v\d")
        self.assertIn("environment: aws-dev", deploy)
        self.assertIn("Require successful CI for this revision", deploy)
        self.assertIn("deployment/terraform/aws/smoke-test.sh", deploy)
        self.assertIn("uses: ./.github/workflows/aws-s3-qualification.yml", deploy)
        self.assertIn("environment: aws-s3-qualification", qualification)
        self.assertIn("workflow_call:", qualification)

    def test_deploy_script_supports_automation_and_waits_for_stability(self) -> None:
        launcher = (AWS / "deploy-dev.sh").read_text()
        self.assertIn('var_file="${1:-}"', launcher)
        self.assertIn("aws ecs wait services-stable", launcher)
        self.assertIn("bootstrap_container_image", launcher)
        self.assertIn("existing service remains available", launcher)
        self.assertNotIn('var_file="${1:-dev.tfvars}"', launcher)


if __name__ == "__main__":
    unittest.main()
