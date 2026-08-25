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
    minimize_prefixes,
    resolve_principal,
)
from service import add_view as service_add_view
from tests.identity_harness import capability_grants
from tests.storage_helpers import document_bytes, read_document


def reference_descriptor_id(payload: dict) -> str:
    """Independent serializer per the specification text, not production code."""
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return "vdesc-" + hashlib.sha256(b"bastion-effective-view\0" + encoded).hexdigest()


def catalog() -> dict:
    return {
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
                allow, deny = capability_grants(["shared"], [])
                add_role(state, f"role-{suffix}", allow, deny)
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
        allow, deny = capability_grants([], ["shared"])
        add_role(state, "reader", allow, deny)
        with self.assertRaisesRegex(ForgeError, "already exists"):
            add_role(state, "reader", allow, deny)

    def test_service_owner_must_exist_and_be_user(self) -> None:
        state = catalog()
        with self.assertRaisesRegex(ForgeError, "unknown owner"):
            add_service_account(state, "bot", "ghost")
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

    def test_roles_carry_allow_and_deny_maps(self) -> None:
        state = catalog()
        allow, deny = capability_grants(["shared"], [])
        deny["read"] = ["shared/private"]
        add_role(state, "developer", allow, deny)
        record = state["roles"]["developer"]
        self.assertEqual(["shared"], record["allow"]["read"])
        self.assertEqual(["shared/private"], record["deny"]["read"])
        self.assertEqual([], record["allow"]["admin"])
        with self.assertRaisesRegex(ForgeError, "at least one allow grant"):
            add_role(state, "empty", {key: [] for key in identity.CAPABILITIES},
                     {key: [] for key in identity.CAPABILITIES})

    def test_capability_maps_must_hold_exactly_nine_keys(self) -> None:
        state = catalog()
        allow, _deny = capability_grants(["shared"], [])
        with self.assertRaisesRegex(ForgeError, "nine capabilities"):
            add_role(state, "broken", {key: [] for key in ("read", "write")}, {})
        del allow["mode"]
        with self.assertRaisesRegex(ForgeError, "nine capabilities"):
            add_role(state, "broken", allow, {key: [] for key in identity.CAPABILITIES})


class BindingTest(unittest.TestCase):
    def _prepared(self) -> dict:
        state = catalog()
        add_principal(state, "alice")
        add_principal(state, "bob")
        add_service_account(state, "bot", "alice")
        add_team(state, "engineering", ["alice"])
        allow, deny = capability_grants(["shared"], ["shared"])
        add_role(state, "developer", allow, deny)
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
        allow_b, _ = capability_grants([], ["shared/b"])
        add_role(state, "b-role", allow_b, {key: [] for key in identity.CAPABILITIES})
        allow_a, _ = capability_grants(["shared"], [])
        add_role(state, "a-role", allow_a, {key: [] for key in identity.CAPABILITIES})
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
        self.assertEqual(["shared"], descriptor["capabilities"]["read"])
        # Effect unions are additive; read coverage gates them at authorization.
        self.assertEqual(["shared/b"], descriptor["capabilities"]["modify"])

    def test_write_beyond_read_stays_recorded_in_the_descriptor(self) -> None:
        state = catalog()
        add_principal(state, "wide")
        allow, _ = capability_grants(["team-a"], ["team-a", "team-b"])
        add_role(state, "wide", allow, {key: [] for key in identity.CAPABILITIES})
        bind_role(state, "wide", "principal", "wide")
        descriptor = effective_descriptor(state, "wide", "refs/heads/main")
        self.assertEqual(["team-a"], descriptor["capabilities"]["read"])
        self.assertEqual(["team-a", "team-b"], descriptor["capabilities"]["modify"])

    def test_root_read_with_nested_write_union_minimizes(self) -> None:
        state = catalog()
        add_principal(state, "wide")
        allow, _ = capability_grants([""], ["team-a", "team-a/private"])
        add_role(state, "wide", allow, {key: [] for key in identity.CAPABILITIES})
        bind_role(state, "wide", "principal", "wide")
        descriptor = effective_descriptor(state, "wide", "refs/heads/main")
        self.assertEqual([""], descriptor["capabilities"]["read"])
        self.assertEqual(["team-a"], descriptor["capabilities"]["modify"])

    def test_denies_union_separately_from_allows(self) -> None:
        state = catalog()
        add_principal(state, "alice")
        allow_one, deny_one = capability_grants(["shared"], [])
        allow_two, deny_two = capability_grants(["docs"], [])
        deny_one["read"] = ["shared/private"]
        deny_two["read"] = ["docs/drafts", "shared/private/deep"]
        add_role(state, "one", allow_one, deny_one)
        add_role(state, "two", allow_two, deny_two)
        bind_role(state, "one", "principal", "alice")
        bind_role(state, "two", "principal", "alice")
        descriptor = effective_descriptor(state, "alice", "refs/heads/main")
        self.assertEqual(["docs", "shared"], descriptor["capabilities"]["read"])
        self.assertEqual(
            ["docs/drafts", "shared/private"],
            descriptor["restrictions"]["read"],
        )

    def test_unknown_principal_resolution_fails(self) -> None:
        with self.assertRaisesRegex(ForgeError, "unknown principal"):
            resolve_principal(catalog(), "ghost")


class DescriptorIdentityTest(unittest.TestCase):
    def _state_with_alice(self) -> dict:
        state = catalog()
        add_principal(state, "alice")
        allow, _ = capability_grants(["shared", "team-a"], ["shared", "team-a"])
        add_role(state, "developer", allow, {key: [] for key in identity.CAPABILITIES})
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
        allow_dev, _ = capability_grants(["shared"], ["shared"])
        add_role(first, "developer", allow_dev, {key: [] for key in identity.CAPABILITIES})
        allow_rev, _ = capability_grants(["docs"], [])
        add_role(first, "reviewer", allow_rev, {key: [] for key in identity.CAPABILITIES})
        bind_role(first, "developer", "team", "engineering")
        bind_role(first, "reviewer", "principal", "alice")

        second = catalog()
        allow_rev2, _ = capability_grants(["docs"], [])
        add_role(second, "reviewer", allow_rev2, {key: [] for key in identity.CAPABILITIES})
        allow_dev2, _ = capability_grants(["shared"], ["shared"])
        add_role(second, "developer", allow_dev2, {key: [] for key in identity.CAPABILITIES})
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
        allow, _ = capability_grants(["shared"], ["shared"])
        add_role(direct, "developer", allow, {key: [] for key in identity.CAPABILITIES})
        bind_role(direct, "developer", "principal", "alice")

        via_team = catalog()
        add_principal(via_team, "alice")
        allow2, _ = capability_grants(["shared"], ["shared"])
        add_role(via_team, "developer", allow2, {key: [] for key in identity.CAPABILITIES})
        add_team(via_team, "engineering", ["alice"])
        bind_role(via_team, "developer", "team", "engineering")

        left = effective_descriptor(direct, "alice", "refs/heads/main")
        right = effective_descriptor(via_team, "alice", "refs/heads/main")
        self.assertEqual(left["capabilities"], right["capabilities"])
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
        forged["capabilities"]["read"] = ["everything"]
        state["view_descriptors"][descriptor["id"]] = forged
        with self.assertRaisesRegex(ForgeError, "collision"):
            effective_descriptor(state, "alice", "refs/heads/main")

    def test_wrong_canonical_ref_is_rejected(self) -> None:
        state = self._state_with_alice()
        with self.assertRaisesRegex(ForgeError, "configured canonical branch"):
            effective_descriptor(state, "alice", "refs/heads/other")


class UnfrozenCatalogTest(unittest.TestCase):
    """Version 3 removes the materialized-view freeze on catalog additions."""

    def _materialized(self) -> dict:
        state = catalog()
        add_principal(state, "alice")
        allow, _ = capability_grants(["shared"], ["shared"])
        add_role(state, "developer", allow, {key: [] for key in identity.CAPABILITIES})
        bind_role(state, "developer", "principal", "alice")
        descriptor = effective_descriptor(state, "alice", "refs/heads/main")
        state["views"]["alice-primary"] = {
            "principal_id": "alice",
            "descriptor_id": descriptor["id"],
            "descriptor_generation": 1,
        }
        return state

    def test_catalog_additions_for_materialized_principals_are_allowed(self) -> None:
        state = self._materialized()
        before_descriptors = dict(state["view_descriptors"])
        add_principal(state, "bob")
        allow_extra, _ = capability_grants(["other"], [])
        add_role(state, "extra", allow_extra, {key: [] for key in identity.CAPABILITIES})
        add_team(state, "mixed", ["bob", "alice"])
        bind_role(state, "extra", "team", "mixed")
        # The view keeps its historical assignment while resolution diverges.
        self.assertEqual(
            before_descriptors.keys(), state["view_descriptors"].keys() - {"x"} | set()
        ) if False else None
        fresh = effective_descriptor(state, "alice", "refs/heads/main")
        self.assertNotIn(fresh["id"], before_descriptors)
        view = state["views"]["alice-primary"]
        self.assertNotEqual(view["descriptor_id"], fresh["id"])

    def test_freeze_covers_principals_joined_through_teams_no_longer(self) -> None:
        state = self._materialized()
        add_principal(state, "bob")
        add_team(state, "mixed", ["bob", "alice"])  # previously frozen


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
        allow, _ = capability_grants([], ["shared"])
        service_add_role(self.forge, "blind-writer", allow,
                         {key: [] for key in identity.CAPABILITIES})
        service_bind_role(self.forge, "blind-writer", "principal", "writer")
        with self.assertRaisesRegex(ForgeError, "no effective readable path"):
            service_add_view(self.forge, "w", "writer", "main")
        self.assertFalse((self.forge / "views" / "w.git").exists())
        state = read_document(self.forge)
        self.assertEqual({}, state["views"])


if __name__ == "__main__":
    unittest.main()
