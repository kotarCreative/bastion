"""Bounded Clerk directory searches expose only verified account identities."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tests import identity_harness  # Makes src imports available.
from clerk_auth import ClerkAuthenticator, ClerkDirectoryError


def clerk_user(user_id="user_reader", *, verified=True, **changes):
    values = dict(
        id=user_id, banned=False, locked=False, deprovisioned=False,
        first_name="Demo", last_name="Reader", username="reader",
        primary_email_address_id="email_primary",
        email_addresses=[SimpleNamespace(
            id="email_primary", email_address="Reader@Example.test",
            verification=SimpleNamespace(status="verified" if verified else "unverified"),
        )],
        private_metadata={"secret": "never return this"},
    )
    values.update(changes)
    return SimpleNamespace(**values)


class ClerkDirectoryTest(unittest.TestCase):
    def setUp(self):
        self.auth = object.__new__(ClerkAuthenticator)
        self.auth._client = Mock()

    def test_search_is_bounded_and_omits_ineligible_users_and_metadata(self):
        self.auth._client.users.list.return_value = [
            clerk_user(), clerk_user("user_unverified", verified=False),
            clerk_user("user_banned", banned=True), clerk_user("user_locked", locked=True),
            clerk_user("user_deleted", deprovisioned=True),
        ]
        page = self.auth.search_accounts("reader", 20)
        self.auth._client.users.list.assert_called_once_with(request={
            "query": "reader", "offset": 20, "limit": 21, "order_by": "-created_at",
        })
        self.assertEqual(page, {"accounts": [{
            "clerk_user_id": "user_reader", "name": "Demo Reader", "email": "reader@example.test",
        }], "next_offset": None})

    def test_pagination_uses_raw_results_even_if_page_is_filtered(self):
        self.auth._client.users.list.return_value = [clerk_user(verified=False)] * 21
        self.assertEqual(self.auth.search_accounts("reader", 0), {"accounts": [], "next_offset": 20})

    def test_selected_account_is_revalidated(self):
        self.auth._client.users.get.return_value = clerk_user()
        self.assertEqual(self.auth.get_account("user_reader")["email"], "reader@example.test")
        self.auth._client.users.get.return_value = clerk_user(banned=True)
        self.assertIsNone(self.auth.get_account("user_reader"))
        self.assertIsNone(self.auth.get_account("invalid"))

    def test_directory_errors_are_wrapped(self):
        self.auth._client.users.list.side_effect = RuntimeError("private API details")
        with self.assertRaises(ClerkDirectoryError):
            self.auth.search_accounts("reader", 0)
        self.auth._client.users.get.side_effect = RuntimeError("private API details")
        with self.assertRaises(ClerkDirectoryError):
            self.auth.get_account("user_reader")
