from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from errors import ForgeError
from service import init_forge, update_canonical_ref
import forge

from tests.identity_harness import provision_admin


class ForgeInitializationTest(unittest.TestCase):
    def test_revision_expression_is_not_a_branch_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "forge"
            with self.assertRaisesRegex(ForgeError, "invalid branch name"):
                init_forge(root, Path(temporary), "main^{commit}")
            self.assertFalse(root.exists())

    def test_canonical_merge_is_rejected_before_ref_moves(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            subprocess.run(
                ["git", "init", "-q", "-b", "main", str(source)], check=True
            )
            for key, value in (
                ("user.name", "Fixture"),
                ("user.email", "fixture@example.test"),
            ):
                subprocess.run(["git", "config", key, value], cwd=source, check=True)
            (source / "base.txt").write_text("base\n")
            subprocess.run(["git", "add", "."], cwd=source, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=source, check=True)
            forge_root = root / "forge"
            init_forge(forge_root, source, "main")
            canonical = forge.canonical_repo(forge_root)
            checkout = root / "admin"
            subprocess.run(["git", "clone", "-q", str(canonical), str(checkout)], check=True)
            subprocess.run(["git", "config", "user.name", "Admin"], cwd=checkout, check=True)
            subprocess.run(
                ["git", "config", "user.email", "admin@example.test"],
                cwd=checkout,
                check=True,
            )
            old = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=checkout,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            ).stdout.strip()
            subprocess.run(["git", "checkout", "-q", "-b", "side"], cwd=checkout, check=True)
            (checkout / "side.txt").write_text("side\n")
            subprocess.run(["git", "add", "."], cwd=checkout, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "side"], cwd=checkout, check=True)
            side = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=checkout, text=True,
                stdout=subprocess.PIPE, check=True,
            ).stdout.strip()
            subprocess.run(["git", "checkout", "-q", "main"], cwd=checkout, check=True)
            (checkout / "main.txt").write_text("main\n")
            subprocess.run(["git", "add", "."], cwd=checkout, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "main"], cwd=checkout, check=True)
            subprocess.run(
                ["git", "merge", "-q", "--no-ff", "-m", "merge", side],
                cwd=checkout,
                check=True,
            )
            merge = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=checkout, text=True,
                stdout=subprocess.PIPE, check=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "push", "-q", "origin", f"{merge}:refs/staging/merge"],
                cwd=checkout,
                check=True,
            )
            with self.assertRaisesRegex(ForgeError, "canonical merge commits"):
                update_canonical_ref(forge_root, old, merge, provision_admin(forge_root))
            self.assertEqual(old, forge.ref(canonical, "refs/heads/main"))

    def test_canonical_update_requires_immutable_oid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=source, check=True)
            subprocess.run(
                ["git", "config", "user.email", "fixture@example.test"],
                cwd=source,
                check=True,
            )
            (source / "base").write_text("base\n")
            subprocess.run(["git", "add", "."], cwd=source, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=source, check=True)
            forge_root = root / "forge"
            init_forge(forge_root, source, "main")
            old = forge.ref(forge.canonical_repo(forge_root), "refs/heads/main")
            with self.assertRaisesRegex(ForgeError, "immutable full commit OIDs"):
                update_canonical_ref(forge_root, old, "refs/staging/next", provision_admin(forge_root))

    def test_canonical_update_without_views_completes_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=source, check=True)
            subprocess.run(
                ["git", "config", "user.email", "fixture@example.test"],
                cwd=source,
                check=True,
            )
            (source / "base").write_text("base\n")
            subprocess.run(["git", "add", "."], cwd=source, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=source, check=True)
            forge_root = root / "forge"
            init_forge(forge_root, source, "main")
            canonical = forge.canonical_repo(forge_root)
            old = forge.ref(canonical, "refs/heads/main")
            (source / "next").write_text("next\n")
            subprocess.run(["git", "add", "."], cwd=source, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "next"], cwd=source, check=True)
            new = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=source, text=True,
                stdout=subprocess.PIPE, check=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "push", "-q", str(canonical), f"{new}:refs/staging/next"],
                cwd=source,
                check=True,
            )
            update_canonical_ref(forge_root, old, new, provision_admin(forge_root))
            state = json.loads((forge_root / "state.json").read_text())
            self.assertEqual("published", next(iter(state["transactions"].values()))["status"])

    @unittest.skipIf(os.pathsep not in ":;", "unexpected object path separator")
    def test_object_path_separator_in_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / f"forge{os.pathsep}unsafe"
            with self.assertRaisesRegex(ForgeError, "object-path separator"):
                init_forge(root, Path(temporary), "main")
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
