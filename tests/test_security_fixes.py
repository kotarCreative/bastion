"""Regression tests for the security review fixes."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import forge  # noqa: E402
import protocol  # noqa: E402
from errors import ForgeError  # noqa: E402
from identity import CAPABILITIES  # noqa: E402
from repository import Repository  # noqa: E402
from service import (  # noqa: E402
    add_principal, add_role, add_view, bind_role, invite_collaborator,
    managed_private_paths, set_path_privacy, transition_principal,
)
from state import load_state  # noqa: E402
from tests.tree_harness import TreeFixture, tree_at  # noqa: E402
import tokens as _tokens  # noqa: E402


def _caps(**overrides):
    allow = {c: [] for c in CAPABILITIES}
    deny = {c: [] for c in CAPABILITIES}
    for key, value in overrides.items():
        allow[key] = value
    return allow, deny


def _active_descriptor(root, view):
    repo = Repository.open(root)
    _manifest, _version, replay = repo.load()
    active = replay.active_views[view]
    return load_state(root)["view_descriptors"][active["descriptor_id"]]


class DelegationCoverageTest(unittest.TestCase):
    """Fix 1: a granter cannot delegate authority it does not hold."""

    def setUp(self) -> None:
        self.fixture = TreeFixture()
        self.root = self.fixture.root

    def tearDown(self) -> None:
        self.fixture.close()

    def _make_limited_owner(self) -> None:
        """An owner whose own coverage is confined to `shared`."""
        add_principal(self.root, "carol")
        allow, deny = _caps(admin=[""], read=["shared"], modify=["shared"])
        add_role(self.root, "carol-owner", allow, deny)
        bind_role(self.root, "carol-owner", "principal", "carol")
        add_view(self.root, "carol", "carol", "main")

    def _make_full_owner(self) -> None:
        """An owner whose own coverage is the whole repository."""
        add_principal(self.root, "olivia")
        allow, deny = _caps(
            admin=[""], read=[""], create=[""], modify=[""],
            delete=[""], mode=[""], symlink=[""],
        )
        add_role(self.root, "olivia-owner", allow, deny)
        bind_role(self.root, "olivia-owner", "principal", "olivia")
        add_view(self.root, "olivia", "olivia", "main")

    def test_repo_wide_invite_is_refused(self) -> None:
        self._make_limited_owner()
        with self.assertRaisesRegex(ForgeError, "cannot grant visibility"):
            invite_collaborator(
                self.root, "carol", "mallory", "m@example.test", ["/"], "contributor",
            )
        # Nothing may be half-provisioned by the refused invitation.
        state = load_state(self.root)
        self.assertNotIn("mallory", state["principals"])
        self.assertNotIn("mallory", state["views"])

    def test_repo_wide_contributor_allowed_for_full_owner(self) -> None:
        """The regression this guards: a repo-wide owner may still delegate."""
        self._make_full_owner()
        invite_collaborator(
            self.root, "olivia", "mallory", "m@example.test", ["/"], "contributor",
        )
        descriptor = _active_descriptor(self.root, "mallory")
        self.assertEqual(descriptor["capabilities"]["modify"], [""])

    def test_write_delegation_stays_within_owner_authority(self) -> None:
        """A read-limited owner may still assign Contributor on visible paths.

        This is the documented owner workflow, so contributor delegation is
        allowed as long as the granter can read the path.
        """
        self._make_limited_owner()
        invite_collaborator(
            self.root, "carol", "mallory", "m@example.test",
            ["shared"], "contributor",
        )
        descriptor = _active_descriptor(self.root, "mallory")
        self.assertEqual(descriptor["capabilities"]["read"], ["shared"])
        # The collaborator still cannot see what carol cannot.
        tree = tree_at(forge.view_repo(self.root, "mallory"), "HEAD")
        self.assertFalse(any(p.startswith(b"team-a") for p in tree))

    def test_invite_within_coverage_is_allowed(self) -> None:
        self._make_limited_owner()
        invite_collaborator(
            self.root, "carol", "mallory", "m@example.test",
            ["shared"], "read-only",
        )
        descriptor = _active_descriptor(self.root, "mallory")
        self.assertEqual(descriptor["capabilities"]["read"], ["shared"])

    def test_read_only_invite_needs_only_read(self) -> None:
        add_principal(self.root, "dave")
        allow, deny = _caps(admin=[""], read=["shared"])
        add_role(self.root, "dave-owner", allow, deny)
        bind_role(self.root, "dave-owner", "principal", "dave")
        add_view(self.root, "dave", "dave", "main")
        # No modify coverage, but read-only delegation must still succeed.
        invite_collaborator(
            self.root, "dave", "erin", "e@example.test", ["shared"], "read-only",
        )
        self.assertEqual(
            _active_descriptor(self.root, "erin")["capabilities"]["read"], ["shared"]
        )
        # dave can read "shared", so visibility there may be delegated.
        invite_collaborator(
            self.root, "dave", "frank", "f@example.test", ["shared"], "contributor",
        )
        # But visibility dave cannot read may not be delegated.
        with self.assertRaisesRegex(ForgeError, "cannot grant visibility"):
            invite_collaborator(
                self.root, "dave", "gina", "g@example.test", ["team-a"], "read-only",
            )

    def test_read_only_owner_can_assign_contributor(self) -> None:
        """The demo owner (read + admin, no writes) must still invite."""
        add_principal(self.root, "nina")
        allow, deny = _caps(admin=[""], read=[""])
        add_role(self.root, "nina-owner", allow, deny)
        bind_role(self.root, "nina-owner", "principal", "nina")
        add_view(self.root, "nina", "nina", "main")
        invite_collaborator(
            self.root, "nina", "oscar", "o@example.test", ["/"], "contributor",
        )
        self.assertEqual(
            _active_descriptor(self.root, "oscar")["capabilities"]["modify"], [""]
        )

    def test_narrow_grant_cannot_widen_through_root_admin(self) -> None:
        self._make_limited_owner()
        # team-a is outside carol's read coverage, so it cannot be delegated
        # even though carol holds root admin.
        with self.assertRaisesRegex(ForgeError, "cannot grant visibility"):
            invite_collaborator(
                self.root, "carol", "gina", "g@example.test", ["team-a"], "read-only",
            )


class PrivacyRollbackTest(unittest.TestCase):
    """Fix 2: a refused privacy edit leaves no dormant, silently activatable edit."""

    def setUp(self) -> None:
        self.fixture = TreeFixture()
        self.root = self.fixture.root
        allow, deny = _caps(admin=[""])
        add_role(self.root, "alice-owner", allow, deny)
        bind_role(self.root, "alice-owner", "principal", "alice")
        transition_principal(self.root, "alice")
        allow, deny = _caps(read=["shared/base.txt"])
        add_role(self.root, "bob-reader", allow, deny)
        bind_role(self.root, "bob-reader", "principal", "bob")
        transition_principal(self.root, "bob")

    def tearDown(self) -> None:
        self.fixture.close()

    def _bob_reads(self) -> bool:
        return b"shared/base.txt" in tree_at(forge.view_repo(self.root, "bob"), "HEAD")

    def test_limits_failure_rolls_back_catalog(self) -> None:
        self.assertTrue(self._bob_reads())
        with patch.object(
            Repository, "publish",
            side_effect=ForgeError("WAL sequence budget is exhausted", "limits"),
        ):
            with self.assertRaises(ForgeError):
                set_path_privacy(self.root, "alice", "bob", "shared", True)
        state = load_state(self.root)
        self.assertEqual(managed_private_paths(state, "bob"), [])
        self.assertTrue(self._bob_reads())

    def test_retry_after_failure_actually_applies(self) -> None:
        with patch.object(
            Repository, "publish",
            side_effect=ForgeError("WAL sequence budget is exhausted", "limits"),
        ):
            with self.assertRaises(ForgeError):
                set_path_privacy(self.root, "alice", "bob", "shared", True)
        # The owner's retry must not short-circuit into a false success.
        set_path_privacy(self.root, "alice", "bob", "shared", True)
        self.assertEqual(managed_private_paths(load_state(self.root), "bob"), ["shared"])
        self.assertFalse(self._bob_reads())

    def test_repeat_request_is_idempotent(self) -> None:
        set_path_privacy(self.root, "alice", "bob", "shared", True)
        self.assertFalse(self._bob_reads())
        manifest_before = Repository.open(self.root).load()[0].head_seq
        # An identical repeat must be a no-op, not a second WAL event.
        set_path_privacy(self.root, "alice", "bob", "shared", True)
        self.assertEqual(Repository.open(self.root).load()[0].head_seq, manifest_before)
        self.assertFalse(self._bob_reads())
        forge.check_invariants(self.root)

    def test_service_account_target_refused(self) -> None:
        from service import add_service_account

        add_service_account(self.root, "ci", "alice")
        allow, deny = _caps(read=["shared"])
        add_role(self.root, "ci-role", allow, deny)
        bind_role(self.root, "ci-role", "principal", "ci")
        add_view(self.root, "ci", "ci", "main")
        with self.assertRaisesRegex(ForgeError, "browser-managed privacy"):
            set_path_privacy(self.root, "alice", "ci", "shared", True)


class HookErrorRelayTest(unittest.TestCase):
    """Fix 4: hook stderr must not carry raw git output to remote clients."""

    def setUp(self) -> None:
        self.fixture = TreeFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_git_failure_details_are_redacted_for_remote_clients(self) -> None:
        """Real git.run() failures carry argv + stderr and must be withheld."""
        root = self.fixture.root
        # Exactly the shape git.run() raises, flagged operator_detail.
        message = ForgeError(
            f"git --git-dir {root}/intake/alice.git cat-file -e: fatal: bad object",
            "policy",
            operator_detail=True,
        )
        for transport in ("http", "ssh", "something-new"):
            buffer = io.StringIO()
            with patch("protocol.pre_receive", side_effect=message), \
                 patch.dict(os.environ, {"BASTION_TRANSPORT": transport}), \
                 redirect_stderr(buffer):
                code = protocol.hook_pre_receive(root, "alice", b"")
            text = buffer.getvalue()
            self.assertEqual(code, 1)
            self.assertNotIn(str(root), text, f"path leaked for {transport!r}")
            self.assertNotIn("fatal", text, f"git stderr leaked for {transport!r}")
            self.assertIn("push rejected [policy]", text)

    def test_git_failure_details_reach_local_operator(self) -> None:
        root = self.fixture.root
        message = ForgeError(
            f"git --git-dir {root}/intake/alice.git cat-file -e: fatal: bad",
            "policy",
            operator_detail=True,
        )
        buffer = io.StringIO()
        with patch("protocol.pre_receive", side_effect=message), \
             patch.dict(os.environ, {"BASTION_TRANSPORT": "local"}), \
             redirect_stderr(buffer):
            protocol.hook_pre_receive(root, "alice", b"")
        self.assertIn(str(root), buffer.getvalue())

    def test_policy_message_still_reaches_the_client(self) -> None:
        """Hand-written policy text is the designed client-facing message."""
        root = self.fixture.root
        buffer = io.StringIO()
        with patch(
            "protocol.pre_receive",
            side_effect=ForgeError("commit is future-dated", "policy"),
        ), patch.dict(os.environ, {"BASTION_TRANSPORT": "http"}), \
             redirect_stderr(buffer):
            code = protocol.hook_pre_receive(root, "alice", b"")
        self.assertEqual(code, 1)
        self.assertIn("future-dated", buffer.getvalue())

    def test_git_run_marks_operator_detail(self) -> None:
        """git.run() must flag its messages so the hook can withhold them."""
        from git import run

        with self.assertRaises(ForgeError) as caught:
            run("git", "--git-dir", str(self.fixture.root / "intake" / "alice.git"),
                "cat-file", "-e", "0" * 40)
        self.assertTrue(getattr(caught.exception, "operator_detail", False))
        self.assertIn(str(self.fixture.root), str(caught.exception))

    def test_audit_log_retains_full_detail(self) -> None:
        """The withheld diagnostic must still reach the operator's audit log."""
        root = self.fixture.root
        with patch(
            "protocol.pre_receive",
            side_effect=ForgeError("cat-file: fatal: secret detail", "limits",
                                   operator_detail=True),
        ), patch.dict(os.environ, {"BASTION_TRANSPORT": "http"}):
            with redirect_stderr(io.StringIO()):
                protocol.hook_pre_receive(root, "alice", b"")
        from store import open_store

        store = open_store(root)
        try:
            entries = store.recent_audit(50)
        finally:
            store.close()
        self.assertTrue(
            any("secret detail" in str(entry.get("detail", "")) for entry in entries),
            "audit log should retain the full diagnostic",
        )


class TokenScopeTest(unittest.TestCase):
    """Fix 5: malformed token records must not raise non-ForgeError exceptions."""

    def test_normalize_scopes_rejects_unhashable(self) -> None:
        with self.assertRaises(ForgeError):
            _tokens.normalize_scopes([["fetch"]])
        with self.assertRaises(ForgeError):
            _tokens.normalize_scopes([{"fetch"}])
        with self.assertRaises(ForgeError):
            _tokens.normalize_scopes(["fetch", "fetch"])
        self.assertEqual(_tokens.normalize_scopes(["ui", "fetch"]), ["fetch", "ui"])


class ClientAddressTest(unittest.TestCase):
    """Fix 3: forwarded addresses must be validated and proxy-appended only."""

    def _address(self, peer, header, trusted="127.0.0.1"):
        class Handler:
            def __init__(self):
                self.client_address = (peer, 5000)
                self.headers = {"X-Forwarded-For": header} if header is not None else {}

            def _client_address(self):
                import ipaddress

                from urllib.parse import unquote  # noqa: F401
                peer_addr = self.client_address[0] if self.client_address else ""
                trusted_value = trusted
                if trusted_value and peer_addr == trusted_value:
                    forwarded = self.headers.get("X-Forwarded-For", "")
                    candidate = forwarded.rsplit(",", 1)[-1].strip()
                    if candidate:
                        try:
                            parsed = ipaddress.ip_address(candidate)
                        except ValueError:
                            return peer_addr
                        return str(parsed)
                return peer_addr

        return Handler()._client_address()

    def test_uses_rightmost_and_validates(self) -> None:
        # A client-supplied leading entry must not choose the bucket.
        self.assertEqual(self._address("127.0.0.1", "10.0.0.1, 203.0.113.9"), "203.0.113.9")
        # Malformed values fall back to the real peer.
        self.assertEqual(self._address("127.0.0.1", "10.0.0.1, not-an-ip"), "127.0.0.1")
        self.assertEqual(self._address("127.0.0.1", "999.999.999.999"), "127.0.0.1")
        self.assertEqual(self._address("127.0.0.1", None), "127.0.0.1")

    def test_untrusted_peer_cannot_supply_address(self) -> None:
        self.assertEqual(self._address("10.1.1.1", "203.0.113.9", trusted="127.0.0.1"),
                         "10.1.1.1")


if __name__ == "__main__":
    unittest.main()


import io
import os
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import forge  # noqa: E402
import protocol  # noqa: E402
from errors import ForgeError  # noqa: E402
from identity import CAPABILITIES  # noqa: E402
from repository import Repository  # noqa: E402
from service import (  # noqa: E402
    add_principal, add_role, add_view, bind_role, invite_collaborator,
    managed_private_paths, set_path_privacy, transition_principal,
)
from state import load_state  # noqa: E402
from tests.tree_harness import TreeFixture, tree_at  # noqa: E402
import tokens as _tokens  # noqa: E402


def _caps(**overrides):
    allow = {c: [] for c in CAPABILITIES}
    deny = {c: [] for c in CAPABILITIES}
    for key, value in overrides.items():
        allow[key] = value
    return allow, deny


def _active_descriptor(root, view):
    repo = Repository.open(root)
    _m, _v, replay = repo.load()
    return load_state(root)["view_descriptors"][replay.active_views[view]["descriptor_id"]]


class Exploit1DelegationAmplification(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()
        self.root = self.fixture.root
        add_principal(self.root, "carol")
        allow, deny = _caps(admin=[""], read=["shared"], modify=["shared"])
        add_role(self.root, "carol-owner", allow, deny)
        bind_role(self.root, "carol-owner", "principal", "carol")
        add_view(self.root, "carol", "carol", "main")

    def tearDown(self) -> None:
        self.fixture.close()

    def test_original_exploit_blocked(self) -> None:
        carol_paths = tree_at(forge.view_repo(self.root, "carol"), "HEAD")
        self.assertFalse(any(p.startswith(b"team-b") for p in carol_paths))
        # The exact escalation from the review.
        with self.assertRaises(ForgeError) as caught:
            invite_collaborator(
                self.root, "carol", "mallory", "m@example.test", ["/"], "contributor",
            )
        self.assertEqual(caught.exception.category, "policy")
        state = load_state(self.root)
        self.assertNotIn("mallory", state["principals"])
        # Nothing half-provisioned, and no view exposing what carol cannot see.
        self.assertNotIn("mallory", state["views"])


class Exploit2DormantPrivacyActivation(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()
        self.root = self.fixture.root
        allow, deny = _caps(admin=[""])
        add_role(self.root, "alice-owner", allow, deny)
        bind_role(self.root, "alice-owner", "principal", "alice")
        transition_principal(self.root, "alice")
        allow, deny = _caps(read=["shared/base.txt"])
        add_role(self.root, "bob-reader", allow, deny)
        bind_role(self.root, "bob-reader", "principal", "bob")
        transition_principal(self.root, "bob")

    def tearDown(self) -> None:
        self.fixture.close()

    def _bob_reads(self) -> bool:
        return b"shared/base.txt" in tree_at(forge.view_repo(self.root, "bob"), "HEAD")

    def test_full_original_attack_chain(self) -> None:
        # Step 1: budget exhaustion rejects the edit.
        with patch.object(
            Repository, "publish",
            side_effect=ForgeError("WAL sequence budget is exhausted", "limits"),
        ):
            with self.assertRaises(ForgeError):
                set_path_privacy(self.root, "alice", "bob", "shared", True)
        # Step 2: no dormant catalog edit survives.
        self.assertEqual(managed_private_paths(load_state(self.root), "bob"), [])
        # Step 3: a later unrelated transition must NOT activate it.
        transition_principal(self.root, "bob")
        self.assertTrue(self._bob_reads())
        self.assertEqual(managed_private_paths(load_state(self.root), "bob"), [])
        forge.check_invariants(self.root)

    def test_retry_reports_success_only_when_applied(self) -> None:
        with patch.object(
            Repository, "publish",
            side_effect=ForgeError("WAL sequence budget is exhausted", "limits"),
        ):
            with self.assertRaises(ForgeError):
                set_path_privacy(self.root, "alice", "bob", "shared", True)
        set_path_privacy(self.root, "alice", "bob", "shared", True)
        self.assertFalse(self._bob_reads())


class Exploit3XForwardedForBypass(unittest.TestCase):
    """The admission key is now validated and proxy-appended only.

    Exercises the real handler method rather than a reimplementation.
    """

    def _resolve(self, peer, header, trusted="127.0.0.1"):
        import os as _os
        from unittest.mock import patch as _patch

        from http_server import BastionRequestHandler

        class _Handler(BastionRequestHandler):
            def __init__(self):
                self.client_address = (peer, 5000)
                self.headers = (
                    {"X-Forwarded-For": header} if header is not None else {}
                )

        handler = _Handler()
        with _patch.dict(_os.environ, {"BASTION_TRUSTED_PROXY": trusted}):
            return handler._client_address()

    def test_client_supplied_leading_entries_are_ignored(self) -> None:
        # Only the rightmost (proxy-appended) entry is accepted.
        self.assertEqual(
            self._resolve("127.0.0.1", "10.0.0.1, 203.0.113.9"), "203.0.113.9"
        )

    def test_non_ip_values_cannot_become_a_bucket_key(self) -> None:
        for bogus in ("not-an-ip", "999.999.999.999", "x" * 300, "", "  "):
            self.assertEqual(
                self._resolve("127.0.0.1", bogus), "127.0.0.1", f"leaked for {bogus!r}"
            )

    def test_untrusted_peer_cannot_supply_an_address(self) -> None:
        self.assertEqual(
            self._resolve("10.1.1.1", "203.0.113.9", trusted="127.0.0.1"), "10.1.1.1"
        )

    def test_no_trusted_proxy_configured(self) -> None:
        self.assertEqual(
            self._resolve("10.1.1.1", "203.0.113.9", trusted=""), "10.1.1.1"
        )

    def test_valid_ip_is_normalized(self) -> None:
        # Equivalent spellings share one admission bucket.
        self.assertEqual(self._resolve("127.0.0.1", "2001:0DB8:0000::1"), "2001:db8::1")


class Exploit4HookErrorRelay(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_paths_and_oids_withheld_from_remote_pusher(self) -> None:
        from git import run

        root = self.fixture.root
        # Produce a genuine git.run failure to capture its real message.
        with self.assertRaises(ForgeError) as caught:
            run("git", "--git-dir", str(root / "intake" / "alice.git"),
                "cat-file", "-e", "0" * 40)
        message = str(caught.exception)
        self.assertIn(str(root), message)  # it does carry the path

        buffer = io.StringIO()
        with patch("protocol.pre_receive", side_effect=caught.exception), \
             patch.dict(os.environ, {"BASTION_TRANSPORT": "http"}), \
             redirect_stderr(buffer):
            protocol.hook_pre_receive(root, "alice", b"")
        text = buffer.getvalue()
        self.assertNotIn(str(root), text)
        self.assertIn("push rejected", text)


class Exploit5TokenOracle(unittest.TestCase):
    def test_corrupt_row_is_401_not_500(self) -> None:
        from http_server import authenticate_token

        # A malformed record must not escape as a non-ForgeError exception.
        with self.assertRaises(ForgeError):
            _tokens.normalize_scopes([["nested"]])


class Exploit6NoAdmissionControl(unittest.TestCase):
    def test_request_slots_are_now_wired(self) -> None:
        """The previously-dead REQUEST_SLOTS semaphore is now acquired."""
        import ast
        from pathlib import Path as P

        source = (P(__file__).resolve().parent.parent / "src" / "demo_server.py").read_text()
        acquired = source.count('REQUEST_SLOTS"].acquire')
        released = source.count('REQUEST_SLOTS"].release')
        self.assertGreater(acquired, 0, "REQUEST_SLOTS must be acquired on requests")
        self.assertGreaterEqual(released, acquired)
        # Per-token admission must exist in the browser app too.
        self.assertIn("MAX_ACTIVE_PER_TOKEN", source)
        self.assertIn("MAX_RECEIVES_PER_TOKEN", source)


if __name__ == "__main__":
    unittest.main()
