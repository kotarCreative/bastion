from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import forge
import identity
from errors import ForgeError
from identity import (
    add_principal,
    add_role,
    add_service_account,
    add_team,
    bind_role,
    effective_descriptor,
    intersect_prefixes,
    minimize_prefixes,
    resolve_principal,
)
from service import add_view as service_add_view


def reference_descriptor_id(payload: dict) -> str:
    """Independent serializer per the specification text, not production code."""
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return "vdesc-" + hashlib.sha256(b"bastion-effective-view-v1\0" + encoded).hexdigest()


def catalog() -> dict:
    return {
        "version": 2,
        "canonical_branch": "main",
        "principals": {},
        "teams": {},
        "roles": {},
        "role_bindings": {},
        "view_descriptors": {},
        "views": {},
    }


class IdentifierTest(unittest.TestCase):
    def test_valid_identifiers_in_every_namespace(self) -> None:
        for value in ("alice", "build-bot", "Team.A_1", "a", "x" * 64):
            with self.subTest(value=value):
                state = catalog()
                suffix = value[:56]
                add_principal(state, value)
                add_team(state, f"team-{suffix}", [value])
                add_role(state, f"role-{suffix}", ["shared"], [])
                bind_role(state, f"role-{suffix}", "principal", value)

    def test_invalid_identifiers_rejected(self) -> None:
        for value in (
            "", ".hidden", "-lead", "has space", "slash/slash", "a" * 65, "tab\t",
        ):
            with self.subTest(value=value), self.assertRaises(ForgeError):
                add_principal(catalog(), value)


class PrincipalTest(unittest.TestCase):
    def test_user_and_service_account_records(self) -> None:
        state = catalog()
        add_principal(state, "alice")
        self.assertEqual({"id": "alice", "kind": "user"}, state["principals"]["alice"])
        add_service_account(state, "bot", "alice")
        self.assertEqual(
            {
                "id": "bot",
                "kind": "service_account",
                "owner_principal_id": "alice",
            },
            state["principals"]["bot"],
        )

    def test_duplicate_creation_rejects_even_if_byte_equal(self) -> None:
        state = catalog()
        add_principal(state, "alice")
        with self.assertRaisesRegex(ForgeError, "already exists"):
            add_principal(state, "alice")
        add_role(state, "reader", [], ["shared"])
        with self.assertRaisesRegex(ForgeError, "already exists"):
            add_role(state, "reader", [], ["shared"])

    def test_service_owner_must_exist_and_be_user(self) -> None:
        state = catalog()
        with self.assertRaisesRegex(ForgeError, "unknown owner"):
            add_service_account(state, "bot", "ghost")
        add_service_account(state, "bot-a", "alice") if False else None
        add_principal(state, "alice")
        add_service_account(state, "bot", "alice")
        with self.assertRaisesRegex(ForgeError, "owner must be a user"):
            add_service_account(state, "nested", "bot")

    def test_team_members_must_exist_be_sorted_and_unique(self) -> None:
        state = catalog()
        add_principal(state, "alice")
        add_service_account(state, "bot", "alice")
        add_team(state, "mixed", ["bot", "alice"])
        self.assertEqual(["alice", "bot"], state["teams"]["mixed"]["members"])
        add_team(state, "empty", [])
        self.assertEqual([], state["teams"]["empty"]["members"])
        with self.assertRaisesRegex(ForgeError, "unknown team member"):
            add_team(state, "broken", ["ghost"])
        with self.assertRaisesRegex(ForgeError, "already exists"):
            add_team(state, "mixed", ["alice"])


class PrefixAlgebraTest(unittest.TestCase):
    def test_normalization_sort_dedup_and_minimization(self) -> None:
        self.assertEqual(
            ["shared", "team-a"],
            minimize_prefixes(["team-a/sub/x", "shared", "team-a", "shared/a/b"]),
        )
        self.assertEqual([""], minimize_prefixes(["", "anything/else"]))
        self.assertEqual([], minimize_prefixes([]))

    def test_coverage_is_component_aware(self) -> None:
        self.assertEqual(
            ["shared", "shared-secret"],
            minimize_prefixes(["shared", "shared-secret"]),
        )
        self.assertEqual(["shared"], minimize_prefixes(["shared", "shared/file"]))
        self.assertEqual(
            ["shared", "shared-secret/deep/file"],
            minimize_prefixes(["shared-secret/deep/file", "shared"]),
        )

    def test_intersection_keeps_the_narrower_area(self) -> None:
        self.assertEqual(["shared/a"], intersect_prefixes(["shared"], ["shared/a"]))
        self.assertEqual(["shared/a"], intersect_prefixes(["shared/a"], ["shared"]))
        self.assertEqual([], intersect_prefixes(["team-a"], ["team-b"]))
        self.assertEqual(["team-a"], minimize_prefixes(intersect_prefixes([""], ["team-a", "team-a/private"])))
        self.assertEqual(["shared/x/y"], intersect_prefixes(["shared/x/y"], ["shared"]))

    def test_roles_may_be_read_only_or_write_only(self) -> None:
        state = catalog()
        add_role(state, "read-only", ["shared"], [])
        self.assertEqual((["shared"], []), (state["roles"]["read-only"]["read"], state["roles"]["read-only"]["write"]))
        add_role(state, "write-only", [], ["shared"])
        with self.assertRaisesRegex(ForgeError, "at least one read or write grant"):
            add_role(state, "empty", [], [])


class BindingTest(unittest.TestCase):
    def _prepared(self) -> dict:
        state = catalog()
        add_principal(state, "alice")
        add_principal(state, "bob")
        add_service_account(state, "bot", "alice")
        add_team(state, "engineering", ["alice"])
        add_role(state, "developer", ["shared"], ["shared"])
        return state

    def test_deterministic_binding_ids_and_rejection_of_duplicates(self) -> None:
        state = self._prepared()
        first = bind_role(state, "developer", "principal", "alice")
        second = bind_role(state, "developer", "team", "engineering")
        self.assertEqual("principal:alice:developer", first)
        self.assertEqual("team:engineering:developer", second)
        with self.assertRaisesRegex(ForgeError, "already exists"):
            bind_role(state, "developer", "principal", "alice")

    def test_bindings_require_existing_subjects_and_roles(self) -> None:
        state = self._prepared()
        with self.assertRaisesRegex(ForgeError, "unknown role"):
            bind_role(state, "missing", "principal", "alice")
        with self.assertRaisesRegex(ForgeError, "unknown principal"):
            bind_role(state, "developer", "principal", "ghost")
        with self.assertRaisesRegex(ForgeError, "unknown team"):
            bind_role(state, "developer", "team", "ghost")


class ResolutionTest(unittest.TestCase):
    def _resolved(self) -> tuple[dict, str]:
        state = catalog()
        add_principal(state, "alice")
        add_team(state, "z-team", ["alice"])
        add_team(state, "a-team", ["alice"])
        add_team(state, "idle", ["alice"])  # no bindings; still in provenance
        add_role(state, "b-role", [], ["shared/b"])
        add_role(state, "a-role", ["shared"], [])
        bind_role(state, "a-role", "team", "a-team")
        bind_role(state, "b-role", "principal", "alice")
        bind_role(state, "a-role", "team", "z-team")  # duplicate route to a-role
        return state, "alice"

    def test_resolution_is_sorted_complete_and_minimized_once(self) -> None:
        state, principal = self._resolved()
        teams, bindings, roles = resolve_principal(state, principal)
        self.assertEqual(["a-team", "idle", "z-team"], teams)
        self.assertEqual(
            ["principal:alice:b-role", "team:a-team:a-role", "team:z-team:a-role"],
            bindings,
        )
        self.assertEqual(["a-role", "b-role"], roles)
        descriptor = effective_descriptor(state, principal, "refs/heads/main")
        # Effective write keeps only the narrower area inside effective read:
        # b-role's write of shared/b is inside a-role's read of shared.
        self.assertEqual(["shared"], descriptor["read"])
        self.assertEqual(["shared/b"], descriptor["write"])

    def test_write_beyond_read_is_removed_from_the_descriptor(self) -> None:
        state = catalog()
        add_principal(state, "wide")
        add_role(state, "wide", ["team-a"], ["team-a", "team-b"])
        bind_role(state, "wide", "principal", "wide")
        descriptor = effective_descriptor(state, "wide", "refs/heads/main")
        self.assertEqual(["team-a"], descriptor["read"])
        self.assertEqual(["team-a"], descriptor["write"])

    def test_root_read_with_nested_write_union_minimizes(self) -> None:
        state = catalog()
        add_principal(state, "wide")
        add_role(state, "wide", [""], ["team-a", "team-a/private"])
        bind_role(state, "wide", "principal", "wide")
        descriptor = effective_descriptor(state, "wide", "refs/heads/main")
        self.assertEqual([""], descriptor["read"])
        self.assertEqual(["team-a"], descriptor["write"])

    def test_unknown_principal_resolution_fails(self) -> None:
        with self.assertRaisesRegex(ForgeError, "unknown principal"):
            resolve_principal(catalog(), "ghost")


class DescriptorIdentityTest(unittest.TestCase):
    def _state_with_alice(self) -> dict:
        state = catalog()
        add_principal(state, "alice")
        add_role(state, "developer", ["shared", "team-a"], ["shared", "team-a"])
        bind_role(state, "developer", "principal", "alice")
        return state

    def test_id_matches_independent_reference_serializer(self) -> None:
        state = self._state_with_alice()
        descriptor = effective_descriptor(state, "alice", "refs/heads/main")
        payload = {key: value for key, value in descriptor.items() if key != "id"}
        self.assertEqual(reference_descriptor_id(payload), descriptor["id"])

    def test_insertion_order_does_not_change_identity(self) -> None:
        first = catalog()
        add_principal(first, "alice")
        add_principal(first, "bob")
        add_team(first, "engineering", ["alice", "bob"])
        add_role(first, "developer", ["shared"], ["shared"])
        add_role(first, "reviewer", ["docs"], [])
        bind_role(first, "developer", "team", "engineering")
        bind_role(first, "reviewer", "principal", "alice")

        second = catalog()
        add_role(second, "reviewer", ["docs"], [])
        add_role(second, "developer", ["shared"], ["shared"])
        add_principal(second, "bob")
        add_principal(second, "alice")
        add_team(second, "engineering", ["bob", "alice"])
        bind_role(second, "reviewer", "principal", "alice")
        bind_role(second, "developer", "team", "engineering")

        left = effective_descriptor(first, "alice", "refs/heads/main")
        right = effective_descriptor(second, "alice", "refs/heads/main")
        self.assertEqual(left["id"], right["id"])
        self.assertEqual(left, right)

    def test_different_provenance_changes_identity(self) -> None:
        direct = catalog()
        add_principal(direct, "alice")
        add_role(direct, "developer", ["shared"], ["shared"])
        bind_role(direct, "developer", "principal", "alice")

        via_team = catalog()
        add_principal(via_team, "alice")
        add_role(via_team, "developer", ["shared"], ["shared"])
        add_team(via_team, "engineering", ["alice"])
        bind_role(via_team, "developer", "team", "engineering")

        left = effective_descriptor(direct, "alice", "refs/heads/main")
        right = effective_descriptor(via_team, "alice", "refs/heads/main")
        self.assertEqual(left["read"], right["read"])
        self.assertNotEqual(left["binding_ids"], right["binding_ids"])
        self.assertNotEqual(left["id"], right["id"])

    def test_view_name_is_not_part_of_identity(self) -> None:
        state = self._state_with_alice()
        one = effective_descriptor(state, "alice", "refs/heads/main")
        two = effective_descriptor(state, "alice", "refs/heads/main")
        self.assertEqual(one, two)

    def test_collision_with_different_payload_is_rejected(self) -> None:
        state = self._state_with_alice()
        descriptor = effective_descriptor(state, "alice", "refs/heads/main")
        forged = copy.deepcopy(descriptor)
        forged["read"] = ["everything"]
        state["view_descriptors"][descriptor["id"]] = forged
        with self.assertRaisesRegex(ForgeError, "collision"):
            effective_descriptor(state, "alice", "refs/heads/main")

    def test_wrong_canonical_ref_is_rejected(self) -> None:
        state = self._state_with_alice()
        with self.assertRaisesRegex(ForgeError, "configured canonical branch"):
            effective_descriptor(state, "alice", "refs/heads/other")


class FreezeTest(unittest.TestCase):
    def _materialized(self) -> dict:
        state = catalog()
        add_principal(state, "alice")
        add_role(state, "developer", ["shared"], ["shared"])
        bind_role(state, "developer", "principal", "alice")
        effective_descriptor(state, "alice", "refs/heads/main")
        state["views"]["alice-primary"] = {
            "principal_id": "alice",
            "descriptor_id": next(iter(state["view_descriptors"])),
            "descriptor_generation": 1,
        }
        return state

    def test_catalog_changes_for_materialized_principal_are_rejected(self) -> None:
        state = self._materialized()
        before = copy.deepcopy(state)
        add_principal(state, "bob")
        add_role(state, "extra", ["other"], [])  # unrelated records stay allowed
        with self.assertRaisesRegex(ForgeError, "materialized view"):
            add_team(state, "new-team", ["alice"])
        with self.assertRaisesRegex(ForgeError, "materialized view"):
            bind_role(state, "extra", "principal", "alice")
        add_team(state, "bob-team", ["bob"])
        bind_role(state, "extra", "team", "bob-team")
        del state["teams"]["bob-team"]
        del state["role_bindings"]["team:bob-team:extra"]
        self.assertEqual(before["teams"], state["teams"])
        self.assertEqual(before["role_bindings"], state["role_bindings"])

    def test_freeze_covers_principals_joined_through_teams(self) -> None:
        state = self._materialized()
        add_principal(state, "bob")
        with self.assertRaisesRegex(ForgeError, "materialized view"):
            add_team(state, "mixed", ["bob", "alice"])


class ServiceIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        forge.run("git", "init", "-q", "-b", "main", str(source))
        forge.run("git", "--git-dir", str(source / ".git"), "config", "user.name", "Fixture")
        forge.run("git", "--git-dir", str(source / ".git"), "config", "user.email", "fixture@example.test")
        shared = source / "shared"
        shared.mkdir(parents=True)
        (shared / "base.txt").write_text("base\n")
        forge.run("git", "--git-dir", str(source / ".git"), "add", "-A")
        forge.run(
            "git",
            "--git-dir",
            str(source / ".git"),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.test",
            "commit",
            "-q",
            "-m",
            "initial",
        )
        self.forge = self.root / "forge"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_view_requires_effective_read(self) -> None:
        from service import add_principal as service_add_principal
        from service import add_role as service_add_role
        from service import bind_role as service_bind_role
        from service import init_forge

        init_forge(self.forge, self.root / "source", "main")
        service_add_principal(self.forge, "writer")
        service_add_role(self.forge, "blind-writer", [], ["shared"])
        service_bind_role(self.forge, "blind-writer", "principal", "writer")
        entrypoint = Path(__file__).resolve().parents[1] / "forge.py"
        with self.assertRaisesRegex(ForgeError, "no effective readable path"):
            service_add_view(self.forge, "w", "writer", "main", entrypoint)
        self.assertFalse((self.forge / "views" / "w.git").exists())
        state = json.loads((self.forge / "state.json").read_text())
        self.assertEqual({}, state["views"])


if __name__ == "__main__":
    unittest.main()
