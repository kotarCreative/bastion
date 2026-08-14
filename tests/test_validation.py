from __future__ import annotations

import unittest

from errors import ForgeError
from layout import ZERO_OID
from validation import (
    allowed,
    apply_changes,
    authorize_changes,
    changed_entries,
    normalize_prefix,
    parse_change_id,
    parse_updates,
    validate_ref_update,
    validate_view_name,
)


def blob(oid: str = "1" * 40, mode: bytes = b"100644") -> tuple[bytes, bytes, str]:
    return mode, b"blob", oid


class PathPolicyTest(unittest.TestCase):
    def test_prefix_matches_only_whole_path_component(self) -> None:
        self.assertTrue(allowed(b"visible", ["visible"]))
        self.assertTrue(allowed(b"visible/file.txt", ["visible"]))
        self.assertFalse(allowed(b"visible-secret/file.txt", ["visible"]))
        self.assertFalse(allowed(b"visibl", ["visible"]))

    def test_root_prefix_allows_every_path(self) -> None:
        self.assertTrue(allowed(b"any/raw/\xff/path", [""]))

    def test_prefix_normalization_and_validation(self) -> None:
        self.assertEqual("visible", normalize_prefix("/visible/"))
        self.assertEqual("", normalize_prefix("/"))
        for value in ("../secret", "/a/../b", r"a\b", "a//b"):
            with self.subTest(value=value), self.assertRaises(ForgeError):
                normalize_prefix(value)

    def test_view_names_cannot_escape_layout(self) -> None:
        for value in ("../alice", "/tmp/alice", "alice/bob", "", ".alice"):
            with self.subTest(value=value), self.assertRaises(ForgeError):
                validate_view_name(value)
        validate_view_name("team-security.1")


class TreeTransitionTest(unittest.TestCase):
    def test_changed_entries_reports_add_modify_and_delete(self) -> None:
        parent = {b"delete": blob("1" * 40), b"modify": blob("2" * 40)}
        child = {b"add": blob("3" * 40), b"modify": blob("4" * 40)}
        self.assertEqual(
            {
                b"add": blob("3" * 40),
                b"delete": None,
                b"modify": blob("4" * 40),
            },
            changed_entries(parent, child),
        )

    def test_rename_mode_and_symlink_effects_are_exact(self) -> None:
        renamed = changed_entries(
            {b"shared/old": blob("1" * 40)},
            {b"shared/new": blob("1" * 40)},
        )
        self.assertEqual({b"shared/old": None, b"shared/new": blob("1" * 40)}, renamed)
        self.assertEqual(
            {b"shared/file": blob("1" * 40, b"100755")},
            changed_entries(
                {b"shared/file": blob("1" * 40)},
                {b"shared/file": blob("1" * 40, b"100755")},
            ),
        )
        link = (b"120000", b"blob", "2" * 40)
        self.assertEqual(
            {b"shared/link": link},
            changed_entries({b"shared/link": blob("1" * 40)}, {b"shared/link": link}),
        )

    def test_authorization_requires_both_read_and_write(self) -> None:
        changes = {b"visible/file": blob()}
        authorize_changes(changes, ["visible"], ["visible"])
        for read, write in ((["other"], ["visible"]), (["visible"], ["other"])):
            with self.subTest(read=read, write=write), self.assertRaises(ForgeError):
                authorize_changes(changes, read, write)

    def test_rename_requires_both_endpoints_to_be_readable_and_writable(self) -> None:
        changes = {b"team-a/old": None, b"shared/new": blob()}
        with self.assertRaisesRegex(ForgeError, "outside current authorization"):
            authorize_changes(changes, ["shared", "team-a"], ["shared"])
        with self.assertRaisesRegex(ForgeError, "outside current authorization"):
            authorize_changes(
                {b"shared/old": None, b"team-a/new": blob()},
                ["shared", "team-a"],
                ["shared"],
            )

    def test_apply_changes_preserves_unaffected_hidden_state(self) -> None:
        hidden = blob("a" * 40)
        result = apply_changes(
            {b"hidden/secret": hidden, b"visible/file": blob("b" * 40)},
            {b"visible/file": blob("c" * 40), b"visible/new": blob("d" * 40)},
        )
        self.assertEqual(hidden, result[b"hidden/secret"])
        self.assertEqual(blob("c" * 40), result[b"visible/file"])

    def test_apply_changes_handles_authorized_delete(self) -> None:
        result = apply_changes({b"visible/file": blob()}, {b"visible/file": None})
        self.assertEqual({}, result)

    def test_file_directory_replacement_is_order_independent(self) -> None:
        old_file = {b"visible": blob("1" * 40)}
        to_directory = {
            b"visible": None,
            b"visible/file": blob("2" * 40),
        }
        self.assertEqual(
            {b"visible/file": blob("2" * 40)},
            apply_changes(old_file, to_directory),
        )
        old_directory = {b"visible/file": blob("2" * 40)}
        to_file = {
            b"visible/file": None,
            b"visible": blob("3" * 40),
        }
        self.assertEqual(
            {b"visible": blob("3" * 40)},
            apply_changes(old_directory, to_file),
        )

    def test_hidden_ancestor_and_descendant_collisions_are_rejected(self) -> None:
        fixtures = (
            ({b"container": blob()}, {b"container/visible/file": blob("2" * 40)}),
            ({b"container/hidden": blob()}, {b"container": blob("2" * 40)}),
        )
        for canonical, changes in fixtures:
            with self.subTest(canonical=canonical), self.assertRaisesRegex(
                ForgeError, "file/directory authorization boundary"
            ):
                apply_changes(canonical, changes)

    def test_complete_file_directory_conflict_detection(self) -> None:
        for entries in (
            {b"a": blob(), b"a-": blob(), b"a/b": blob()},
            {b"a/b": blob(), b"a-": blob(), b"a": blob()},
            {b"a": blob(), b"a/b": blob(), b"a0": blob()},
        ):
            with self.subTest(entries=entries), self.assertRaisesRegex(
                ForgeError, "file/directory authorization boundary"
            ):
                apply_changes({}, entries)
        self.assertEqual(
            {b"a": blob(), b"a-": blob(), b"ab/c": blob()},
            apply_changes({}, {b"a": blob(), b"a-": blob(), b"ab/c": blob()}),
        )
        self.assertEqual(
            {b"visible": blob(), b"visible-secret/file": blob()},
            apply_changes({}, {b"visible": blob(), b"visible-secret/file": blob()}),
        )

    def test_supported_mode_and_symlink_transitions_apply(self) -> None:
        original = {b"shared/file": blob("1" * 40), b"shared/link": blob("2" * 40)}
        symlink = (b"120000", b"blob", "3" * 40)
        result = apply_changes(
            original,
            {b"shared/file": blob("1" * 40, b"100755"), b"shared/link": symlink},
        )
        self.assertEqual(blob("1" * 40, b"100755"), result[b"shared/file"])
        self.assertEqual(symlink, result[b"shared/link"])

    def test_unsupported_old_and_new_entries_are_rejected(self) -> None:
        gitlink = (b"160000", b"commit", "1" * 40)
        fixtures = (
            ({b"module": gitlink}, {b"module": None}),
            ({}, {b"module": gitlink}),
            ({}, {b"file": blob(mode=b"100664")}),
        )
        for canonical, changes in fixtures:
            with self.subTest(changes=changes), self.assertRaisesRegex(
                ForgeError, "unsupported entry"
            ):
                apply_changes(canonical, changes)


class MetadataAndRefTest(unittest.TestCase):
    def test_change_id_must_be_in_final_trailer_block(self) -> None:
        change_id = "I1234567890abcdef"
        self.assertEqual(
            change_id,
            parse_change_id(f"subject\n\nbody\n\nChange-Id: {change_id}\n".encode()),
        )
        self.assertIsNone(
            parse_change_id(f"Change-Id: {change_id}\n\nbody without trailer\n".encode())
        )

    def test_duplicate_change_id_trailers_are_rejected(self) -> None:
        with self.assertRaises(ForgeError):
            parse_change_id(b"subject\n\nChange-Id: Ione\nChange-Id: Itwo\n")

    def test_ref_update_accepts_only_current_configured_branch(self) -> None:
        old = "1" * 40
        new = "2" * 40
        view = {"projected_ref": "refs/heads/main", "projected_tip": old}
        update = parse_updates(f"{old} {new} refs/heads/main\n".encode())
        self.assertEqual((old, new, "refs/heads/main"), validate_ref_update(view, update))
        for raw in (
            f"{old} {new} refs/heads/other\n",
            f"{ZERO_OID} {new} refs/heads/main\n",
            f"{'3' * 40} {new} refs/heads/main\n",
            f"{old} {ZERO_OID} refs/heads/main\n",
            f"{old} {new} refs/heads/main\n{old} {new} refs/heads/other\n",
        ):
            with self.subTest(raw=raw), self.assertRaises(ForgeError):
                validate_ref_update(view, parse_updates(raw.encode()))

    def test_malformed_ref_update_is_rejected(self) -> None:
        for update in (
            b"old new\n",
            b"not-an-oid " + b"2" * 40 + b" refs/heads/main\n",
        ):
            with self.subTest(update=update), self.assertRaises(ForgeError):
                parse_updates(update)

    def test_non_utf8_ref_is_parsed_without_traceback_then_rejected(self) -> None:
        old = "1" * 40
        new = "2" * 40
        update = parse_updates(
            old.encode() + b" " + new.encode() + b" refs/heads/\xff\n"
        )
        view = {"projected_ref": "refs/heads/main", "projected_tip": old}
        with self.assertRaisesRegex(ForgeError, "branch update is not authorized"):
            validate_ref_update(view, update)


if __name__ == "__main__":
    unittest.main()
