"""Failure-safety tests for bootstrap credential publication."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import call, patch

from deployment.bootstrap_demo import store_rotated_tokens


class FakeSecretsClient:
    def __init__(
        self, fail_on_call: int | None = None, *, commit_on_failure: bool = True
    ) -> None:
        self.fail_on_call = fail_on_call
        self.commit_on_failure = commit_on_failure
        self.values: list[dict] = []
        self.current: dict = {}

    def put_secret_value(self, *, SecretId: str, SecretString: str) -> None:
        call = len(self.values) + 1
        value = json.loads(SecretString)
        self.values.append(value)
        if call != self.fail_on_call or self.commit_on_failure:
            self.current = value
        if call == self.fail_on_call:
            raise RuntimeError("injected Secrets Manager failure")

    def get_secret_value(self, *, SecretId: str) -> dict[str, str]:
        return {"SecretString": json.dumps(self.current)}


class BootstrapTokenRotationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path("/test/forge")
        self.previous = {
            "tester_token": "bst1.old-tester.secret",
            "observer_token": "bst1.old-observer.secret",
            "superseded_token_ids": ["older"],
        }

    @patch("deployment.bootstrap_demo.revoke_token")
    def test_first_write_failure_preserves_old_tokens_and_revokes_new(self, revoke) -> None:
        client = FakeSecretsClient(fail_on_call=1, commit_on_failure=False)
        with self.assertRaises(RuntimeError):
            store_rotated_tokens(
                self.root,
                client,
                "secret",
                "bst1.new-tester.secret",
                "bst1.new-observer.secret",
                self.previous,
            )
        self.assertEqual(
            revoke.call_args_list,
            [
                call(self.root, "new-tester"),
                call(self.root, "new-observer"),
            ],
        )

    @patch("deployment.bootstrap_demo.revoke_token")
    def test_committed_write_error_keeps_published_new_tokens_valid(self, revoke) -> None:
        client = FakeSecretsClient(fail_on_call=1, commit_on_failure=True)
        store_rotated_tokens(
            self.root,
            client,
            "secret",
            "bst1.new-tester.secret",
            "bst1.new-observer.secret",
            self.previous,
        )
        self.assertEqual(client.current["tester_token"], "bst1.new-tester.secret")
        self.assertNotIn("superseded_token_ids", client.current)
        self.assertEqual(revoke.call_count, 3)

    @patch("deployment.bootstrap_demo.revoke_token")
    def test_revocation_failure_leaves_new_tokens_and_pending_ids_published(self, revoke) -> None:
        revoke.side_effect = RuntimeError("injected revocation failure")
        client = FakeSecretsClient()
        with self.assertRaises(RuntimeError):
            store_rotated_tokens(
                self.root,
                client,
                "secret",
                "bst1.new-tester.secret",
                "bst1.new-observer.secret",
                self.previous,
            )
        self.assertEqual(client.values[0]["tester_token"], "bst1.new-tester.secret")
        self.assertEqual(
            client.values[0]["superseded_token_ids"],
            ["old-observer", "old-tester", "older"],
        )

    @patch("deployment.bootstrap_demo.revoke_token")
    def test_second_write_failure_keeps_recoverable_pending_version(self, revoke) -> None:
        client = FakeSecretsClient(fail_on_call=2, commit_on_failure=False)
        with self.assertRaises(RuntimeError):
            store_rotated_tokens(
                self.root,
                client,
                "secret",
                "bst1.new-tester.secret",
                "bst1.new-observer.secret",
                self.previous,
            )
        self.assertIn("superseded_token_ids", client.values[0])
        self.assertNotIn("superseded_token_ids", client.values[1])
        self.assertIn("superseded_token_ids", client.current)
        self.assertEqual(revoke.call_count, 3)


if __name__ == "__main__":
    unittest.main()
