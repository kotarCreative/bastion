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
    def test_single_repository_bootstrap_is_retired(self) -> None:
        definition = json.loads(
            (AWS / "bootstrap_container_definition.json.tftpl").read_text()
        )
        self.assertEqual([container["name"] for container in definition], ["bastion-bootstrap"])
        self.assertIn("retired", definition[0]["command"][2])
        self.assertFalse((AWS / "init-forge.sh").exists())

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
        self.assertFalse((WORKFLOWS / "aws-deploy.yml").exists())
        qualification = (WORKFLOWS / "aws-s3-qualification.yml").read_text()
        for workflow in (qualification,):
            self.assertIn("id-token: write", workflow)
            self.assertIn("aws-actions/configure-aws-credentials", workflow)
            self.assertNotIn("AWS_ACCESS_KEY_ID", workflow)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", workflow)
            self.assertNotRegex(workflow, r"uses: [^\s]+@v\d")
        self.assertIn("environment: aws-s3-qualification", qualification)
        self.assertIn("workflow_call:", qualification)

    def test_demo_deploy_launchers_are_removed(self) -> None:
        self.assertFalse((AWS / "deploy-dev.sh").exists())
        self.assertFalse((AWS / "smoke-test.sh").exists())


if __name__ == "__main__":
    unittest.main()
