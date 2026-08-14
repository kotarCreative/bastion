from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from git import clean_env


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


if __name__ == "__main__":
    unittest.main()
