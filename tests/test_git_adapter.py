from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from errors import ForgeError
from git import clean_env, git, verify_no_extra_objects, write_tree


class GitEnvironmentTest(unittest.TestCase):
    def test_clean_env_removes_repository_local_and_config_overrides(self) -> None:
        poisoned = {
            "PATH": os.environ.get("PATH", ""),
            "GIT_DIR": "/hidden/repository",
            "GIT_SHALLOW_FILE": "/tmp/shallow",
            "GIT_GRAFT_FILE": "/tmp/grafts",
            "GIT_REPLACE_REF_BASE": "refs/replace/custom",
            "GIT_NAMESPACE": "hidden",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/tmp/hooks",
            "UNRELATED": "preserved",
        }
        with patch.dict(os.environ, poisoned, clear=True):
            cleaned = clean_env()
        self.assertEqual("preserved", cleaned["UNRELATED"])
        for name in poisoned:
            if name.startswith("GIT_"):
                self.assertNotIn(name, cleaned)


class QuarantineClosureTest(unittest.TestCase):
    def test_oid_only_closure_rejects_extra_quarantined_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            git_dir = source / ".git"
            subprocess.run(("git", "init", "-q", str(source)), check=True)
            blob = git(git_dir, "hash-object", "-w", "--stdin", data=b"reachable\n").stdout.decode().strip()
            tree = git(
                git_dir, "mktree", data=f"100644 blob {blob}\tfile\n".encode()
            ).stdout.decode().strip()
            metadata = clean_env() | {
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.test",
                "GIT_AUTHOR_DATE": "2000-01-01T00:00:00 +0000",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.test",
                "GIT_COMMITTER_DATE": "2000-01-01T00:00:00 +0000",
            }
            commit = git(git_dir, "commit-tree", tree, data=b"initial\n", env=metadata).stdout.decode().strip()
            git(git_dir, "update-ref", "refs/heads/main", commit)
            quarantine = root / "quarantine"
            (quarantine / blob[:2]).mkdir(parents=True)
            shutil.copy2(git_dir / "objects" / blob[:2] / blob[2:], quarantine / blob[:2] / blob[2:])
            environment = clean_env() | {
                "GIT_OBJECT_DIRECTORY": str(quarantine),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(git_dir / "objects"),
                "GIT_QUARANTINE_PATH": str(quarantine),
            }
            verify_no_extra_objects(git_dir, commit, environment)
            extra = git(git_dir, "hash-object", "-w", "--stdin", data=b"unreachable\n").stdout.decode().strip()
            (quarantine / extra[:2]).mkdir(exist_ok=True)
            shutil.copy2(git_dir / "objects" / extra[:2] / extra[2:], quarantine / extra[:2] / extra[2:])
            with self.assertRaisesRegex(ForgeError, "objects unreachable"):
                verify_no_extra_objects(git_dir, commit, environment)

    def test_write_tree_rejects_non_adjacent_file_directory_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            subprocess.run(("git", "init", "-q", str(repository)), check=True)
            git_dir = repository / ".git"
            oid = git(git_dir, "hash-object", "-w", "--stdin", data=b"contents\n").stdout.decode().strip()
            entries = {
                b"a": (b"100644", b"blob", oid),
                b"a-": (b"100644", b"blob", oid),
                b"a/b": (b"100644", b"blob", oid),
            }
            with self.assertRaisesRegex(ForgeError, "file/directory conflict"):
                write_tree(git_dir, entries)


if __name__ == "__main__":
    unittest.main()
