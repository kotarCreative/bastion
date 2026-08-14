from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import forge
from tests.tree_harness import Effects, TreeFixture, build_commit, command, oid, tree_at


NON_UTF8 = b"shared/nonutf-\xff.txt"
TAB = b"shared/tab\tname.txt"
NEWLINE = b"shared/line\nname.txt"
NEAR_COMPONENT_LIMIT = b"shared/" + b"c" * 240
DEEP = b"shared/" + b"/".join([b"d" * 150] * 28) + b"/leaf.txt"

PATH_CASES = (
    (NON_UTF8, b"shared/renamed-\xff.txt"),
    (TAB, b"shared/renamed\tname.txt"),
    (NEWLINE, b"shared/renamed\nname.txt"),
    (NEAR_COMPONENT_LIMIT, b"shared/" + b"r" * 240),
    (DEEP, DEEP.rsplit(b"/", 1)[0] + b"/renamed-leaf.txt"),
)


class UnusualPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_binary_plumbing_round_trips_every_path_category(self) -> None:
        self.assertGreater(len(DEEP), 4096)
        self.assertLess(max(map(len, DEEP.split(b"/"))), 200)
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            command("git", "init", "-q", "-b", "main", str(repository))
            effects: Effects = {
                path: (b"100644", f"initial-{index}\n".encode())
                for index, (path, _renamed) in enumerate(PATH_CASES)
            }
            commit = build_commit(repository, None, effects, b"raw paths\n", 1_700_000_000)
            actual = tree_at(repository, commit)
            self.assertEqual(set(effects), set(actual))
            for path, (mode, contents) in effects.items():
                with self.subTest(path=repr(path)):
                    self.assertEqual((mode, contents), (actual[path][0], actual[path][2]))

    def test_source_lifecycle_for_all_raw_path_categories(self) -> None:
        additions: list[Effects] = [
            {path: (b"100644", f"initial-{index}\n".encode())}
            for index, (path, _renamed) in enumerate(PATH_CASES)
        ]
        old, new = self.fixture.push_source_chain("alice", additions)
        transaction = next(
            tx for tx in self.fixture.state()["transactions"].values()
            if tx.get("view") == "alice" and tx.get("projected_old") == old and tx.get("projected_new") == new
        )
        self.assertEqual(5, len(transaction["mappings"]))
        self.assertEqual(new, oid(forge.view_repo(self.fixture.root, "alice"), "refs/heads/main"))

        modified = {
            path: (b"100644", f"modified-{index}\n".encode())
            for index, (path, _renamed) in enumerate(PATH_CASES)
        }
        self.fixture.push_source("alice", modified)
        renamed: Effects = {}
        for path, target in PATH_CASES:
            mode, _object_id, contents = self.fixture.canonical_model[path]
            renamed[path] = None
            renamed[target] = (mode, contents)
        self.fixture.push_source("alice", renamed)
        self.fixture.push_source("alice", {target: None for _path, target in PATH_CASES})

    def test_private_raw_paths_elide_for_unrelated_view(self) -> None:
        private_cases = (
            (b"team-a/nonutf-\xff.txt", b"nonutf\n"),
            (b"team-a/line\nname.txt", b"newline\n"),
            (b"team-a/" + b"d" * 150 + b"/" + b"/".join([b"p" * 150] * 28) + b"/leaf.txt", b"deep\n"),
        )
        self.assertGreater(len(private_cases[-1][0]), 4096)
        bob_tip = self.fixture.state()["views"]["bob"]["projected_tip"]
        effects = {path: (b"100644", contents) for path, contents in private_cases}
        self.fixture.push_source("alice", effects)
        bob = self.fixture.state()["views"]["bob"]
        self.assertEqual(bob_tip, bob["projected_tip"])
        self.assertEqual(oid(forge.canonical_repo(self.fixture.root), "refs/heads/main"), bob["canonical_frontier"])
        self.fixture.push_source("alice", {path: None for path, _contents in private_cases})
        self.assertEqual(bob_tip, self.fixture.state()["views"]["bob"]["projected_tip"])

    def test_administrative_add_and_delete_all_raw_path_categories(self) -> None:
        additions: Effects = {
            path: (b"100644", f"admin-{index}\n".encode())
            for index, (path, _renamed) in enumerate(PATH_CASES)
        }
        self.fixture.admin_update([additions])
        for path in additions:
            self.assertIn(path, self.fixture.canonical_model)
        self.fixture.admin_update([{path: None for path in additions}])
        for path in additions:
            self.assertNotIn(path, self.fixture.canonical_model)


if __name__ == "__main__":
    unittest.main()
