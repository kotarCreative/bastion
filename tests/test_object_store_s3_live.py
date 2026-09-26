"""Opt-in integration tests against an explicitly configured real AWS S3 bucket."""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from object_store import Create, NotModified, PreconditionFailed, Update
from object_store_s3 import S3ObjectStore, S3ObjectStoreConfig


OPTED_IN = os.environ.get("BASTION_TEST_AWS_S3") == "1"
REQUIRED_ENV = {
    "bucket": "BASTION_TEST_AWS_S3_BUCKET",
    "region": "BASTION_TEST_AWS_S3_REGION",
    "expected_bucket_owner": "BASTION_TEST_AWS_S3_OWNER",
    "prefix": "BASTION_TEST_AWS_S3_PREFIX",
}


@unittest.skipUnless(OPTED_IN, "set BASTION_TEST_AWS_S3=1 to run real AWS S3 tests")
class LiveS3ObjectStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        missing = [name for name in REQUIRED_ENV.values() if not os.environ.get(name)]
        if missing:
            raise RuntimeError(
                "BASTION_TEST_AWS_S3=1 requires nonempty environment variables: "
                + ", ".join(sorted(missing))
            )
        values = {field: os.environ[name] for field, name in REQUIRED_ENV.items()}
        cls.config = S3ObjectStoreConfig(**values)
        if not cls.config.prefix:
            raise RuntimeError("BASTION_TEST_AWS_S3_PREFIX must be a nonempty dedicated test prefix")
        cls.store = S3ObjectStore.from_config(cls.config)
        cls.run_prefix = f"bastion-live-{uuid.uuid4().hex}"
        cls.created_keys: set[str] = set()

    @classmethod
    def tearDownClass(cls) -> None:
        cleanup_errors: list[str] = []
        for key in cls.created_keys:
            try:
                cls.store.delete(key)
            except Exception as error:  # Cleanup failure must remain visible for a real-bucket run.
                cleanup_errors.append(f"{key}: {error!r}")
        super().tearDownClass()
        if cleanup_errors:
            raise AssertionError("S3 live-test cleanup failed: " + "; ".join(cleanup_errors))

    def key(self, name: str) -> str:
        key = f"{self.run_prefix}/{name}"
        self.created_keys.add(key)
        return key

    def test_bucket_controls_and_conditional_object_lifecycle(self) -> None:
        self.store.verify_bucket()
        key = self.key("lifecycle")
        created = self.store.put(key, b"abcdef", mode=Create())
        self.assertEqual(b"abcdef", self.store.get(key).body)
        self.assertEqual(b"bcd", self.store.get(key, byte_range=(1, 3)).body)
        self.assertEqual(created, self.store.head(key))

        with self.assertRaises(NotModified):
            self.store.get(key, if_none_match=created.version)
        with self.assertRaises(PreconditionFailed):
            self.store.put(key, b"duplicate", mode=Create())

        updated = self.store.put(key, b"updated", mode=Update(created.version))
        with self.assertRaises(PreconditionFailed):
            self.store.put(key, b"stale", mode=Update(created.version))
        listed = self.store.list_prefix(self.run_prefix + "/")
        self.assertEqual([(key, 7, updated.version)], [(item.key, item.size, item.version) for item in listed])

        with self.assertRaises(PreconditionFailed):
            self.store.delete(key, if_version=created.version)
        self.store.delete(key, if_version=updated.version)
        self.created_keys.remove(key)
        self.assertIsNone(self.store.head(key))


if __name__ == "__main__":
    unittest.main()
