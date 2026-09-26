"""Hermetic tests for the qualified S3 object-store adapter."""

from __future__ import annotations

import base64
import hashlib
import sys
import unittest
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from object_store import (
    AmbiguousWrite,
    Create,
    NotModified,
    ObjectNotFound,
    PreconditionFailed,
    StorageFailure,
    Update,
    Version,
)
from object_store_s3 import (
    S3ObjectStore,
    S3ObjectStoreConfig,
    parse_s3_uri,
    validate_bucket_name,
    validate_prefix,
)


OWNER = "123456789012"
BUCKET = "bastion-test-bucket"
REGION = "us-west-2"
PREFIX = "deployments/test"


class FakeClientError(Exception):
    def __init__(self, code: str | None = None, status: Any = None) -> None:
        super().__init__(code or status or "transport failure")
        self.response: dict[str, Any] = {}
        if code is not None:
            self.response["Error"] = {"Code": code}
        if status is not None:
            self.response["ResponseMetadata"] = {"HTTPStatusCode": status}


class ScriptedS3Client:
    """Small strict fake: every client call must have a queued outcome."""

    def __init__(self, **outcomes: list[Any]) -> None:
        self.outcomes = defaultdict(deque)
        for operation, values in outcomes.items():
            self.outcomes[operation].extend(values)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, operation: str):
        def call(**params: Any) -> Any:
            self.calls.append((operation, params))
            if not self.outcomes[operation]:
                raise AssertionError(f"unexpected S3 call: {operation}({params!r})")
            outcome = self.outcomes[operation].popleft()
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return call


class FakeBody:
    def __init__(self, body: Any = b"", *, read_error: Exception | None = None) -> None:
        self.body = body
        self.read_error = read_error
        self.read_sizes: list[int] = []
        self.closed = False

    def read(self, size: int) -> Any:
        self.read_sizes.append(size)
        if self.read_error is not None:
            raise self.read_error
        return self.body

    def close(self) -> None:
        self.closed = True


def checksum(body: bytes) -> str:
    return base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")


def full_get(body: bytes, *, etag: str = '"v1"', stream: FakeBody | None = None) -> dict[str, Any]:
    return {
        "Body": stream or FakeBody(body),
        "ContentLength": len(body),
        "ETag": etag,
        "ChecksumType": "FULL_OBJECT",
        "ChecksumSHA256": checksum(body),
    }


def put_response(body: bytes, *, etag: str = '"v1"') -> dict[str, Any]:
    return {
        "ETag": etag,
        "ChecksumType": "FULL_OBJECT",
        "ChecksumSHA256": checksum(body),
    }


def config(**overrides: Any) -> S3ObjectStoreConfig:
    values = {
        "bucket": BUCKET,
        "expected_bucket_owner": OWNER,
        "region": REGION,
        "prefix": PREFIX,
    }
    values.update(overrides)
    return S3ObjectStoreConfig(**values)


def store(client: ScriptedS3Client, **kwargs: Any) -> S3ObjectStore:
    return S3ObjectStore(client, config(), read_retry_delay=0, **kwargs)


class S3ConfigurationTest(unittest.TestCase):
    def test_valid_configuration_and_uri(self) -> None:
        parsed = parse_s3_uri(
            "s3://bastion-test-bucket/deployments/test",
            region=REGION,
            expected_bucket_owner=OWNER,
        )
        self.assertEqual(config(), parsed)
        self.assertEqual(
            f"s3:{BUCKET}:{OWNER}:{REGION}:{PREFIX}",
            S3ObjectStore(ScriptedS3Client(), parsed).identity,
        )

    def test_bucket_validation_rejects_non_dns_and_ip_names(self) -> None:
        invalid = ["ab", "A-bucket", "-bucket", "bucket-", "a..b", "127.0.0.1", 1]
        for bucket in invalid:
            with self.subTest(bucket=bucket), self.assertRaises(ValueError):
                validate_bucket_name(bucket)  # type: ignore[arg-type]

    def test_config_rejects_owner_region_encryption_and_unsafe_prefixes(self) -> None:
        invalid_prefixes = ["/root", "root/", "a//b", "a/./b", "a/../b", "a\\b", "caf\N{LATIN SMALL LETTER E WITH ACUTE}"]
        for prefix in invalid_prefixes:
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                validate_prefix(prefix)
        invalid_configs = [
            {"expected_bucket_owner": "123"},
            {"region": ""},
            {"server_side_encryption": "aws:kms"},
        ]
        for values in invalid_configs:
            with self.subTest(values=values), self.assertRaises(ValueError):
                config(**values)

    def test_parse_uri_rejects_ambiguous_or_unsafe_forms(self) -> None:
        invalid = [
            "https://bucket/prefix",
            "s3:///prefix",
            "s3://user@bucket/prefix",
            "s3://bucket/prefix/",
            "s3://bucket/a%2Fb",
            "s3://bucket/prefix?x=1",
            "s3://bucket/prefix#fragment",
        ]
        for uri in invalid:
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                parse_s3_uri(uri, region=REGION, expected_bucket_owner=OWNER)

    def test_constructor_and_physical_key_limits_are_enforced_before_io(self) -> None:
        with self.assertRaises(ValueError):
            S3ObjectStore(ScriptedS3Client(), config(), max_object_size=0)
        with self.assertRaises(ValueError):
            S3ObjectStore(ScriptedS3Client(), config(), read_max_attempts=0)
        client = ScriptedS3Client()
        adapter = store(client)
        for key in [PREFIX, PREFIX + "/object", "../object", "x" * 1025]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                adapter.head(key)
        self.assertEqual([], client.calls)


class S3BucketVerificationTest(unittest.TestCase):
    def test_verify_bucket_checks_owner_region_versioning_and_encryption(self) -> None:
        client = ScriptedS3Client(
            get_bucket_location=[{"LocationConstraint": REGION}],
            get_bucket_versioning=[{"Status": "Enabled"}],
            get_bucket_encryption=[{
                "ServerSideEncryptionConfiguration": {
                    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
                }
            }],
        )
        store(client).verify_bucket()
        expected = {"Bucket": BUCKET, "ExpectedBucketOwner": OWNER}
        self.assertEqual(
            [
                ("get_bucket_location", expected),
                ("get_bucket_versioning", expected),
                ("get_bucket_encryption", expected),
            ],
            client.calls,
        )

    def test_verify_bucket_accepts_legacy_empty_us_east_location(self) -> None:
        client = ScriptedS3Client(
            get_bucket_location=[{}],
            get_bucket_versioning=[{"Status": "Enabled"}],
            get_bucket_encryption=[{
                "ServerSideEncryptionConfiguration": {
                    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
                }
            }],
        )
        S3ObjectStore(client, config(region="us-east-1"), read_retry_delay=0).verify_bucket()

    def test_verify_bucket_rejects_wrong_region_disabled_versioning_or_encryption(self) -> None:
        cases = [
            ScriptedS3Client(get_bucket_location=[{"LocationConstraint": "eu-west-1"}]),
            ScriptedS3Client(
                get_bucket_location=[{"LocationConstraint": REGION}],
                get_bucket_versioning=[{"Status": "Suspended"}],
            ),
            ScriptedS3Client(
                get_bucket_location=[{"LocationConstraint": REGION}],
                get_bucket_versioning=[{"Status": "Enabled"}],
                get_bucket_encryption=[{
                    "ServerSideEncryptionConfiguration": {
                        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}}]
                    }
                }],
            ),
        ]
        for client in cases:
            with self.subTest(calls=list(client.outcomes)), self.assertRaises(StorageFailure):
                store(client).verify_bucket()


class S3WriteTest(unittest.TestCase):
    def test_create_and_update_are_fully_qualified_conditional_writes(self) -> None:
        first, second = b"created", b"updated"
        client = ScriptedS3Client(put_object=[put_response(first), put_response(second, etag='"v2"')])
        adapter = store(client)
        created = adapter.put("objects/a", first, mode=Create())
        updated = adapter.put("objects/a", second, mode=Update(created.version))
        common = {
            "Bucket": BUCKET,
            "Key": PREFIX + "/objects/a",
            "ExpectedBucketOwner": OWNER,
            "ServerSideEncryption": "AES256",
        }
        self.assertEqual(
            ("put_object", {**common, "IfNoneMatch": "*", "Body": first, "ContentLength": 7, "ChecksumSHA256": checksum(first)}),
            client.calls[0],
        )
        self.assertEqual(
            ("put_object", {**common, "IfMatch": '"v1"', "Body": second, "ContentLength": 7, "ChecksumSHA256": checksum(second)}),
            client.calls[1],
        )
        self.assertEqual(Version('"v2"'), updated.version)
        self.assertEqual(14, adapter.metrics["bytes_written"])

    def test_put_validates_body_mode_and_size_before_io(self) -> None:
        client = ScriptedS3Client()
        adapter = store(client, max_object_size=3)
        with self.assertRaises(TypeError):
            adapter.put("a", "bad", mode=Create())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            adapter.put("a", b"ok", mode=object())  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            adapter.put("a", b"long", mode=Create())
        self.assertEqual([], client.calls)

    def test_put_translates_precondition_definite_and_ambiguous_failures(self) -> None:
        cases = [
            (FakeClientError("PreconditionFailed", 412), PreconditionFailed),
            (FakeClientError("AccessDenied", 403), StorageFailure),
            (FakeClientError("InternalError", 500), AmbiguousWrite),
            (OSError("connection dropped"), AmbiguousWrite),
        ]
        for error, expected in cases:
            with self.subTest(expected=expected.__name__):
                adapter = store(ScriptedS3Client(put_object=[error]))
                with self.assertRaises(expected):
                    adapter.put("objects/a", b"body", mode=Create())

    def test_put_requires_etag_and_confirmed_full_object_checksum(self) -> None:
        responses = [
            {"ChecksumType": "FULL_OBJECT", "ChecksumSHA256": checksum(b"body")},
            {"ETag": '"v1"', "ChecksumType": "FULL_OBJECT", "ChecksumSHA256": "wrong"},
            None,
        ]
        for response in responses:
            with self.subTest(response=response):
                adapter = store(ScriptedS3Client(put_object=[response]))
                with self.assertRaises(AmbiguousWrite):
                    adapter.put("objects/a", b"body", mode=Create())

    def test_create_conflict_reconciles_absence_and_retries_once(self) -> None:
        response = put_response(b"body")
        client = ScriptedS3Client(
            put_object=[FakeClientError("ConditionalRequestConflict", 409), response],
            head_object=[FakeClientError("NotFound", 404)],
        )
        adapter = store(client)
        self.assertEqual(Version('"v1"'), adapter.put("objects/a", b"body", mode=Create()).version)
        self.assertEqual(["put_object", "head_object", "put_object"], [name for name, _ in client.calls])

    def test_conflict_retry_is_bounded_and_second_conflict_is_precondition(self) -> None:
        client = ScriptedS3Client(
            put_object=[
                FakeClientError("ConditionalRequestConflict", 409),
                FakeClientError("ConditionalRequestConflict", 409),
            ],
            head_object=[FakeClientError("NotFound", 404)],
        )
        with self.assertRaises(PreconditionFailed):
            store(client).put("objects/a", b"body", mode=Create())
        self.assertEqual(2, sum(name == "put_object" for name, _ in client.calls))

    def test_update_conflict_only_retries_when_current_version_matches(self) -> None:
        client = ScriptedS3Client(
            put_object=[FakeClientError("ConditionalRequestConflict", 409)],
            head_object=[{"ContentLength": 4, "ETag": '"other"'}],
        )
        with self.assertRaises(PreconditionFailed):
            store(client).put("objects/a", b"body", mode=Update(Version('"wanted"')))
        self.assertEqual(["put_object", "head_object"], [name for name, _ in client.calls])


class S3ReadTest(unittest.TestCase):
    def test_get_verifies_checksum_size_and_closes_body(self) -> None:
        body = FakeBody(b"content")
        client = ScriptedS3Client(get_object=[full_get(b"content", stream=body)])
        adapter = store(client, max_object_size=20)
        result = adapter.get("objects/a", if_none_match=Version('"old"'))
        self.assertEqual(b"content", result.body)
        self.assertEqual(("objects/a", 7, Version('"v1"')), (result.meta.key, result.meta.size, result.meta.version))
        self.assertEqual([21], body.read_sizes)
        self.assertTrue(body.closed)
        self.assertEqual(
            ("get_object", {
                "Bucket": BUCKET,
                "Key": PREFIX + "/objects/a",
                "ExpectedBucketOwner": OWNER,
                "IfNoneMatch": '"old"',
                "ChecksumMode": "ENABLED",
            }),
            client.calls[0],
        )
        self.assertEqual(7, adapter.metrics["bytes_read"])

    def test_get_closes_body_on_checksum_mismatch_and_read_failure(self) -> None:
        bad_checksum = FakeBody(b"body")
        read_failure = FakeBody(read_error=OSError("broken stream"))
        responses = [
            {**full_get(b"body", stream=bad_checksum), "ChecksumSHA256": "wrong"},
            {**full_get(b"body", stream=read_failure)},
        ]
        for response, body in zip(responses, [bad_checksum, read_failure]):
            with self.subTest(read_error=body.read_error):
                with self.assertRaises(StorageFailure):
                    store(ScriptedS3Client(get_object=[response])).get("objects/a")
                self.assertTrue(body.closed)

    def test_get_translates_not_modified_not_found_and_invalid_range(self) -> None:
        cases = [
            (FakeClientError("NotModified", 304), {}, NotModified),
            (FakeClientError("NoSuchKey", 404), {}, ObjectNotFound),
            (FakeClientError("InvalidRange", 416), {"byte_range": (9, 10)}, ValueError),
        ]
        for error, kwargs, expected in cases:
            with self.subTest(expected=expected.__name__):
                with self.assertRaises(expected):
                    store(ScriptedS3Client(get_object=[error])).get("objects/a", **kwargs)

    def test_range_get_uses_inclusive_range_and_validates_content_range(self) -> None:
        stream = FakeBody(b"bcd")
        client = ScriptedS3Client(get_object=[{
            "Body": stream,
            "ContentLength": 3,
            "ContentRange": "bytes 1-3/6",
            "ETag": '"v1"',
        }])
        adapter = store(client)
        result = adapter.get("objects/a", byte_range=(1, 3))
        self.assertEqual(b"bcd", result.body)
        self.assertEqual(6, result.meta.size)
        self.assertEqual("bytes=1-3", client.calls[0][1]["Range"])
        self.assertNotIn("ChecksumMode", client.calls[0][1])
        self.assertTrue(stream.closed)

    def test_invalid_range_inputs_do_not_call_s3(self) -> None:
        client = ScriptedS3Client()
        adapter = store(client)
        for value in [(-1, 2), (2, 1), (True, 2), [1, 2], (1, 2, 3)]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                adapter.get("objects/a", byte_range=value)  # type: ignore[arg-type]
        self.assertEqual([], client.calls)

    def test_malformed_range_response_is_rejected_and_closed(self) -> None:
        for content_range, length, body_bytes in [
            ("bytes 0-2/6", 3, b"bcd"),
            ("bytes 1-3/3", 3, b"bcd"),
            ("bytes 1-3/6", 2, b"bc"),
        ]:
            stream = FakeBody(body_bytes)
            response = {"Body": stream, "ContentLength": length, "ContentRange": content_range, "ETag": '"v"'}
            with self.subTest(content_range=content_range, length=length), self.assertRaises(StorageFailure):
                store(ScriptedS3Client(get_object=[response])).get("objects/a", byte_range=(1, 3))
            self.assertTrue(stream.closed)

    def test_head_returns_metadata_or_none_and_enforces_size_limit(self) -> None:
        client = ScriptedS3Client(
            head_object=[
                {"ContentLength": 4, "ETag": '"v1"'},
                FakeClientError("NotFound", 404),
                {"ContentLength": 6, "ETag": '"v2"'},
            ]
        )
        adapter = store(client, max_object_size=5)
        self.assertEqual(4, adapter.head("a").size)  # type: ignore[union-attr]
        self.assertIsNone(adapter.head("missing"))
        with self.assertRaises(StorageFailure):
            adapter.head("large")

    def test_transient_reads_retry_to_success_with_bounded_metrics(self) -> None:
        client = ScriptedS3Client(head_object=[
            FakeClientError("SlowDown", 503),
            FakeClientError("RequestTimeout", 408),
            {"ContentLength": 1, "ETag": '"v"'},
        ])
        adapter = store(client, read_max_attempts=3)
        self.assertEqual(1, adapter.head("a").size)  # type: ignore[union-attr]
        self.assertEqual(3, len(client.calls))
        self.assertEqual(2, adapter.metrics["retries"])
        self.assertEqual(1, adapter.metrics["throttled"])

    def test_transient_reads_stop_at_attempt_limit_and_definite_errors_do_not_retry(self) -> None:
        transient = ScriptedS3Client(head_object=[OSError("drop"), OSError("drop")])
        with self.assertRaises(StorageFailure):
            store(transient, read_max_attempts=2).head("a")
        self.assertEqual(2, len(transient.calls))
        definite = ScriptedS3Client(head_object=[FakeClientError("AccessDenied", 403)])
        with self.assertRaises(StorageFailure):
            store(definite, read_max_attempts=3).head("a")
        self.assertEqual(1, len(definite.calls))


class S3ListDeleteTest(unittest.TestCase):
    def test_list_prefix_paginates_unqualifies_and_sorts(self) -> None:
        client = ScriptedS3Client(list_objects_v2=[
            {
                "Contents": [{"Key": PREFIX + "/repos/z", "Size": 2, "ETag": '"z"'}],
                "IsTruncated": True,
                "NextContinuationToken": "next",
            },
            {
                "Contents": [{"Key": PREFIX + "/repos/a", "Size": 1, "ETag": '"a"'}],
                "IsTruncated": False,
            },
        ])
        adapter = store(client)
        self.assertEqual(["repos/a", "repos/z"], [meta.key for meta in adapter.list_prefix("repos/")])
        base = {"Bucket": BUCKET, "ExpectedBucketOwner": OWNER, "Prefix": PREFIX + "/repos/"}
        self.assertEqual(("list_objects_v2", base), client.calls[0])
        self.assertEqual(("list_objects_v2", {**base, "ContinuationToken": "next"}), client.calls[1])
        self.assertEqual(2, adapter.metrics["list_calls"])

    def test_list_rejects_unsafe_input_without_io(self) -> None:
        client = ScriptedS3Client()
        adapter = store(client)
        for prefix in ["/repos", "a\\b", "a//b", "..", PREFIX, PREFIX + "/repos", "caf\N{LATIN SMALL LETTER E WITH ACUTE}"]:
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                adapter.list_prefix(prefix)
        self.assertEqual([], client.calls)

    def test_list_rejects_escaped_objects_bad_sizes_and_repeated_tokens(self) -> None:
        cases = [
            [{"Contents": [{"Key": "outside/a", "Size": 1, "ETag": '"v"'}]}],
            [{"Contents": [{"Key": PREFIX + "/a", "Size": -1, "ETag": '"v"'}]}],
            [
                {"Contents": [], "IsTruncated": True, "NextContinuationToken": "same"},
                {"Contents": [], "IsTruncated": True, "NextContinuationToken": "same"},
            ],
        ]
        for pages in cases:
            with self.subTest(pages=pages), self.assertRaises(StorageFailure):
                store(ScriptedS3Client(list_objects_v2=pages)).list_prefix("")

    def test_delete_is_qualified_conditional_and_missing_unconditional_is_idempotent(self) -> None:
        client = ScriptedS3Client(
            delete_object=[{}, {}, FakeClientError("NoSuchKey", 404)],
        )
        adapter = store(client)
        adapter.delete("objects/a", if_version=Version('"v1"'))
        adapter.delete("objects/b")
        adapter.delete("objects/missing")
        self.assertEqual('"v1"', client.calls[0][1]["IfMatch"])
        self.assertEqual("*", client.calls[1][1]["IfMatch"])
        for _, params in client.calls:
            self.assertEqual(BUCKET, params["Bucket"])
            self.assertEqual(OWNER, params["ExpectedBucketOwner"])
            self.assertTrue(params["Key"].startswith(PREFIX + "/objects/"))

    def test_conditional_delete_missing_or_mismatched_is_precondition(self) -> None:
        errors = [FakeClientError("NotFound", 404), FakeClientError("PreconditionFailed", 412)]
        for error in errors:
            with self.subTest(error=error), self.assertRaises(PreconditionFailed):
                store(ScriptedS3Client(delete_object=[error])).delete("a", if_version=Version('"v"'))

    def test_delete_conflict_reconciles_and_retries_at_most_once(self) -> None:
        client = ScriptedS3Client(
            delete_object=[
                FakeClientError("ConditionalRequestConflict", 409),
                FakeClientError("ConditionalRequestConflict", 409),
            ],
            head_object=[{"ContentLength": 1, "ETag": '"v"'}],
        )
        with self.assertRaises(PreconditionFailed):
            store(client).delete("a", if_version=Version('"v"'))
        self.assertEqual(["delete_object", "head_object", "delete_object"], [name for name, _ in client.calls])


if __name__ == "__main__":
    unittest.main()
