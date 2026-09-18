"""Qualified AWS S3 implementation of the conditional object store."""

from __future__ import annotations

import base64
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from object_store import (
    AmbiguousWrite,
    Create,
    GetResult,
    NotModified,
    ObjectMeta,
    ObjectNotFound,
    ObjectStoreError,
    PreconditionFailed,
    StorageFailure,
    Update,
    Version,
    validate_key,
)


MAX_OBJECT_SIZE = 256 * 1024 * 1024
_BUCKET_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])?$")
_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


@dataclass(frozen=True)
class S3ObjectStoreConfig:
    bucket: str
    expected_bucket_owner: str
    region: str
    prefix: str = ""
    server_side_encryption: str = "AES256"

    def __post_init__(self) -> None:
        validate_bucket_name(self.bucket)
        if not isinstance(self.expected_bucket_owner, str) or not re.fullmatch(
            r"[0-9]{12}", self.expected_bucket_owner
        ):
            raise ValueError("expected bucket owner must be a 12-digit AWS account ID")
        if not isinstance(self.region, str) or not self.region:
            raise ValueError("S3 region must be nonempty")
        validate_prefix(self.prefix)
        if self.server_side_encryption != "AES256":
            raise ValueError("only AES256 server-side encryption is supported")


def validate_bucket_name(bucket: str) -> None:
    if (
        not isinstance(bucket, str)
        or not _BUCKET_RE.fullmatch(bucket)
        or ".." in bucket
        or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", bucket)
    ):
        raise ValueError("bucket is not a valid DNS-compatible AWS S3 bucket name")


def validate_prefix(prefix: str) -> None:
    if not isinstance(prefix, str) or not prefix or prefix.startswith("/"):
        if prefix == "":
            return
        raise ValueError("S3 prefix must be a relative object-key prefix")
    if (
        not prefix.isascii()
        or prefix.endswith("/")
        or "\\" in prefix
        or "\x00" in prefix
        or any(part in {"", ".", ".."} for part in prefix.split("/"))
    ):
        raise ValueError("S3 prefix contains an unsafe component")


def parse_s3_uri(uri: str, *, region: str, expected_bucket_owner: str) -> S3ObjectStoreConfig:
    """Parse the persisted, deliberately small S3 URI configuration."""
    if not isinstance(uri, str):
        raise ValueError("S3 object store must be an s3:// URI")
    parsed = urlsplit(uri)
    if (
        parsed.scheme != "s3"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "%" in parsed.path
    ):
        raise ValueError("S3 object store URI is invalid")
    prefix = parsed.path[1:] if parsed.path else ""
    if parsed.path.endswith("/") and prefix:
        raise ValueError("S3 object store URI prefix must not end with a slash")
    return S3ObjectStoreConfig(
        bucket=parsed.netloc,
        expected_bucket_owner=expected_bucket_owner,
        region=region,
        prefix=prefix,
    )


def _error_details(error: Exception) -> tuple[str, int | None]:
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return "", None
    detail = response.get("Error") or {}
    code = str(detail.get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if status is None:
        status = response.get("HTTPStatusCode")
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    return code, status


class S3ObjectStore:
    """Conditional object store using direct AWS S3 data-plane requests.

    The client is injectable so tests can model AWS responses without making
    the repository depend on a local S3 emulator.
    """

    def __init__(
        self,
        client: Any,
        config: S3ObjectStoreConfig,
        *,
        max_object_size: int = MAX_OBJECT_SIZE,
        read_max_attempts: int = 3,
        read_retry_delay: float = 0.01,
    ) -> None:
        if max_object_size <= 0 or max_object_size > 5_368_709_120:
            raise ValueError("invalid S3 object size limit")
        if read_max_attempts < 1:
            raise ValueError("read retry attempts must be positive")
        self.client = client
        self.config = config
        self.identity = (
            f"s3:{config.bucket}:{config.expected_bucket_owner}:{config.region}:{config.prefix}"
        )
        self.max_object_size = max_object_size
        self.read_max_attempts = read_max_attempts
        self.read_retry_delay = max(0.0, read_retry_delay)
        self.metrics = {
            "requests": 0,
            "bytes_read": 0,
            "bytes_written": 0,
            "conditional_hits": 0,
            "precondition_failures": 0,
            "throttled": 0,
            "retries": 0,
            "ambiguous_writes": 0,
            "range_reads": 0,
            "list_calls": 0,
        }

    def verify_bucket(self) -> None:
        """Verify the controls available to the runtime role before bootstrap."""
        try:
            location = self._read_request(
                "get_bucket_location",
                Bucket=self.config.bucket,
                ExpectedBucketOwner=self.config.expected_bucket_owner,
            )
            returned_region = location.get("LocationConstraint") or "us-east-1"
            if returned_region != self.config.region:
                raise StorageFailure("S3 bucket region differs from repository configuration")
            versioning = self._read_request(
                "get_bucket_versioning",
                Bucket=self.config.bucket,
                ExpectedBucketOwner=self.config.expected_bucket_owner,
            )
            if versioning.get("Status") != "Enabled":
                raise StorageFailure("S3 bucket versioning is not enabled")
            encryption = self._read_request(
                "get_bucket_encryption",
                Bucket=self.config.bucket,
                ExpectedBucketOwner=self.config.expected_bucket_owner,
            )
            rules = encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
            if not isinstance(rules, list) or not any(
                isinstance(rule, dict)
                and rule.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm") == "AES256"
                for rule in rules
            ):
                raise StorageFailure("S3 bucket default encryption is not AES256")
        except ObjectStoreError:
            raise
        except Exception as error:
            raise StorageFailure("S3 bucket verification failed") from error

    @classmethod
    def from_config(cls, config: S3ObjectStoreConfig, **kwargs: Any) -> "S3ObjectStore":
        try:
            import boto3
            from botocore.config import Config
        except ImportError as error:
            raise StorageFailure("AWS S3 support is not installed") from error
        client = boto3.client(
            "s3",
            region_name=config.region,
            config=Config(retries={"mode": "standard", "total_max_attempts": 1}),
        )
        return cls(client, config, **kwargs)

    def _physical_key(self, key: str) -> str:
        validate_key(key)
        if self.config.prefix and (key == self.config.prefix or key.startswith(self.config.prefix + "/")):
            raise ValueError("logical object key already contains the deployment prefix")
        physical = f"{self.config.prefix}/{key}" if self.config.prefix else key
        if len(physical.encode("utf-8")) > 1024:
            raise ValueError("S3 object key exceeds the 1024-byte limit")
        return physical

    def _params(self, key: str) -> dict[str, Any]:
        return {
            "Bucket": self.config.bucket,
            "Key": self._physical_key(key),
            "ExpectedBucketOwner": self.config.expected_bucket_owner,
        }

    @staticmethod
    def _etag(response: dict[str, Any]) -> Version:
        etag = response.get("ETag")
        if not isinstance(etag, str) or not etag:
            raise StorageFailure("S3 response did not include an ETag")
        return Version(etag)

    @staticmethod
    def _length(response: dict[str, Any]) -> int:
        value = response.get("ContentLength")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise StorageFailure("S3 response did not include a valid object size")
        return value

    def _read_request(self, operation: str, **params: Any) -> dict[str, Any]:
        for attempt in range(self.read_max_attempts):
            self.metrics["requests"] += 1
            try:
                response = getattr(self.client, operation)(**params)
                if not isinstance(response, dict):
                    raise StorageFailure("S3 returned a malformed response")
                return response
            except ObjectStoreError:
                raise
            except Exception as error:
                code, status = _error_details(error)
                if (
                    code in {"NotModified", "304", "NoSuchKey", "NoSuchObject", "NotFound", "404", "InvalidRange", "416"}
                    or status in {304, 404, 416}
                ):
                    raise
                transient = (
                    not code and status is None
                ) or status in {408, 425, 429, 500, 502, 503, 504} or code in {
                    "RequestTimeout",
                    "SlowDown",
                    "ServiceUnavailable",
                }
                if not transient:
                    raise self._definite_error(error) from None
                if attempt + 1 == self.read_max_attempts:
                    raise StorageFailure("S3 read failed") from None
                self.metrics["retries"] += 1
                if status == 429 or code == "SlowDown":
                    self.metrics["throttled"] += 1
                if self.read_retry_delay:
                    time.sleep(self.read_retry_delay * (attempt + 1))
        raise AssertionError("unreachable")

    @staticmethod
    def _definite_error(error: Exception) -> StorageFailure:
        code, status = _error_details(error)
        if code or status:
            return StorageFailure(f"S3 request rejected ({code or status})")
        return StorageFailure("S3 request failed")

    def _mutation_error(self, error: Exception, *, kind: str) -> Exception:
        code, status = _error_details(error)
        if code in {"PreconditionFailed", "412"} or status == 412:
            self.metrics["precondition_failures"] += 1
            return PreconditionFailed(kind)
        if code in {"NoSuchKey", "NoSuchBucket", "NotFound", "404"} or status == 404:
            self.metrics["precondition_failures"] += 1
            return PreconditionFailed(kind)
        if code in {"ConditionalRequestConflict", "409"} or status == 409:
            return error
        if (status is not None and status >= 500) or (not code and status is None):
            self.metrics["ambiguous_writes"] += 1
            return AmbiguousWrite(kind)
        return self._definite_error(error)

    def _retry_mutation_error(self, error: Exception, *, kind: str) -> Exception:
        translated = self._mutation_error(error, kind=kind)
        if translated is error:
            code, status = _error_details(error)
            if code in {"ConditionalRequestConflict", "409"} or status == 409:
                return PreconditionFailed(kind)
        return translated

    def _mutation(self, operation: str, *, kind: str, **params: Any) -> dict[str, Any]:
        self.metrics["requests"] += 1
        try:
            response = getattr(self.client, operation)(**params)
        except ObjectStoreError:
            raise
        except Exception as error:
            translated = self._mutation_error(error, kind=kind)
            if translated is error:
                raise error
            raise translated from None
        if not isinstance(response, dict):
            self.metrics["ambiguous_writes"] += 1
            raise AmbiguousWrite(kind)
        return response

    def _read_current(self, key: str) -> ObjectMeta | None:
        return self.head(key)

    def get(
        self,
        key: str,
        *,
        if_none_match: Version | None = None,
        byte_range: tuple[int, int] | None = None,
    ) -> GetResult:
        params = self._params(key)
        if if_none_match is not None:
            params["IfNoneMatch"] = if_none_match.value
        if byte_range is not None:
            if (
                not isinstance(byte_range, tuple)
                or len(byte_range) != 2
                or any(not isinstance(value, int) or isinstance(value, bool) for value in byte_range)
            ):
                raise ValueError("invalid inclusive object byte range")
            start, end = byte_range
            if start < 0 or end < start:
                raise ValueError("invalid inclusive object byte range")
            params["Range"] = f"bytes={start}-{end}"
            self.metrics["range_reads"] += 1
        else:
            params["ChecksumMode"] = "ENABLED"
        try:
            response = self._read_request("get_object", **params)
        except Exception as error:
            code, status = _error_details(error)
            if code in {"NotModified", "304"} or status == 304:
                self.metrics["conditional_hits"] += 1
                raise NotModified(key) from None
            if code in {"NoSuchKey", "NoSuchObject", "NotFound", "404"} or status == 404:
                raise ObjectNotFound(key) from None
            if code in {"InvalidRange", "416"} or status == 416:
                raise ValueError("invalid inclusive object byte range") from None
            if isinstance(error, ObjectStoreError):
                raise
            raise StorageFailure("S3 get failed") from None
        body_stream = response.get("Body")
        try:
            version = self._etag(response)
            if byte_range is None:
                total_size = self._length(response)
                expected_body_size = total_size
            else:
                content_range = response.get("ContentRange")
                match = _CONTENT_RANGE_RE.fullmatch(str(content_range))
                start, end = byte_range
                if match is None or tuple(map(int, match.groups()))[0:2] != (start, end):
                    raise StorageFailure("S3 ranged response has an invalid Content-Range")
                total_size = int(match.group(3))
                expected_body_size = end - start + 1
                if total_size <= end or self._length(response) != expected_body_size:
                    raise StorageFailure("S3 ranged response size is invalid")
            if total_size > self.max_object_size:
                raise StorageFailure("S3 object exceeds the qualified size limit")
            if body_stream is None or not hasattr(body_stream, "read"):
                raise StorageFailure("S3 response body is malformed")
            body = body_stream.read(self.max_object_size + 1)
            if not isinstance(body, bytes):
                raise StorageFailure("S3 response body is malformed")
            if byte_range is None:
                if len(body) != expected_body_size:
                    raise StorageFailure("S3 response body size differs")
                checksum = response.get("ChecksumSHA256")
                expected = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
                if response.get("ChecksumType") != "FULL_OBJECT" or checksum != expected:
                    raise StorageFailure("S3 full-object checksum is missing or invalid")
            else:
                if len(body) != expected_body_size:
                    raise StorageFailure("S3 ranged response body size differs")
        except StorageFailure:
            raise
        except Exception as error:
            raise StorageFailure("S3 response body could not be read") from error
        finally:
            if body_stream is not None and hasattr(body_stream, "close"):
                try:
                    body_stream.close()
                except Exception:
                    pass
        self.metrics["bytes_read"] += len(body)
        return GetResult(body, ObjectMeta(key, total_size, version))

    def head(self, key: str) -> ObjectMeta | None:
        params = self._params(key)
        try:
            response = self._read_request("head_object", **params)
        except Exception as error:
            code, status = _error_details(error)
            if code in {"NoSuchKey", "NoSuchObject", "NotFound", "404"} or status == 404:
                return None
            if isinstance(error, ObjectStoreError):
                raise
            raise StorageFailure("S3 head failed") from None
        size = self._length(response)
        if size > self.max_object_size:
            raise StorageFailure("S3 object exceeds the qualified size limit")
        return ObjectMeta(key, size, self._etag(response))

    def put(self, key: str, body: bytes, *, mode: Create | Update) -> ObjectMeta:
        if not isinstance(body, bytes):
            raise TypeError("object bodies must be bytes")
        if len(body) > self.max_object_size:
            raise ValueError("object exceeds the qualified size limit")
        if not isinstance(mode, (Create, Update)):
            raise TypeError("object-store puts require Create or Update")
        params = self._params(key)
        if isinstance(mode, Create):
            params["IfNoneMatch"] = "*"
        else:
            params["IfMatch"] = mode.version.value
        params.update(
            {
                "Body": body,
                "ContentLength": len(body),
                "ServerSideEncryption": self.config.server_side_encryption,
                "ChecksumSHA256": base64.b64encode(hashlib.sha256(body).digest()).decode("ascii"),
            }
        )
        try:
            response = self._mutation("put_object", kind=key, **params)
        except Exception as error:
            code, status = _error_details(error)
            if code in {"ConditionalRequestConflict", "409"} or status == 409:
                current = self._read_current(key)
                if isinstance(mode, Create):
                    if current is not None:
                        raise PreconditionFailed(key) from None
                    try:
                        response = self._mutation("put_object", kind=key, **params)
                    except Exception as retry_error:
                        raise self._retry_mutation_error(retry_error, kind=key) from None
                else:
                    if current is None or current.version != mode.version:
                        raise PreconditionFailed(key) from None
                    try:
                        response = self._mutation("put_object", kind=key, **params)
                    except Exception as retry_error:
                        raise self._retry_mutation_error(retry_error, kind=key) from None
            else:
                raise
        try:
            version = self._etag(response)
        except StorageFailure as error:
            self.metrics["ambiguous_writes"] += 1
            raise AmbiguousWrite(key) from error
        checksum = response.get("ChecksumSHA256")
        expected = params["ChecksumSHA256"]
        if response.get("ChecksumType") != "FULL_OBJECT" or checksum != expected:
            raise AmbiguousWrite(key)
        self.metrics["bytes_written"] += len(body)
        return ObjectMeta(key, len(body), version)

    def delete(self, key: str, *, if_version: Version | None = None) -> None:
        params = self._params(key)
        params["IfMatch"] = if_version.value if if_version is not None else "*"
        try:
            self._mutation("delete_object", kind=key, **params)
            return
        except Exception as error:
            code, status = _error_details(error)
            is_404 = (
                code in {"NoSuchKey", "NoSuchObject", "NotFound", "404"}
                or status == 404
                or isinstance(error, PreconditionFailed)
            )
            is_412 = (
                code in {"PreconditionFailed", "412"}
                or status == 412
                or isinstance(error, PreconditionFailed)
            )
            is_409 = code in {"ConditionalRequestConflict", "409"} or status == 409
            if if_version is None and (is_404 or is_412):
                return
            if if_version is not None and (is_404 or is_412):
                raise PreconditionFailed(key) from None
            if not is_409:
                raise
            current = self._read_current(key)
            if current is None:
                if if_version is None:
                    return
                raise PreconditionFailed(key) from None
            if if_version is None:
                raise PreconditionFailed(key) from None
            if current.version != if_version:
                raise PreconditionFailed(key) from None
            params["IfMatch"] = if_version.value
            try:
                self._mutation("delete_object", kind=key, **params)
            except Exception as retry_error:
                raise self._retry_mutation_error(retry_error, kind=key) from None

    def list_prefix(self, prefix: str) -> list[ObjectMeta]:
        if not isinstance(prefix, str) or not prefix.isascii() or "\x00" in prefix:
            raise ValueError("object-store prefix must be ASCII")
        if prefix and (prefix.startswith("/") or "\\" in prefix):
            raise ValueError("object-store prefix is not relative")
        components = prefix.split("/") if prefix else []
        # Allow a single trailing slash (e.g. "repos/id/") but reject "",
        # "." and ".." everywhere, including a bare ".." prefix.
        check = components[:-1] if prefix.endswith("/") else components
        if any(component in {"", ".", ".."} for component in check):
            raise ValueError("object-store prefix has an unsafe path component")
        if not prefix.endswith("/") and components and components[-1] in {".", ".."}:
            raise ValueError("object-store prefix has an unsafe path component")
        physical_base = f"{self.config.prefix}/" if self.config.prefix else ""
        physical_prefix = f"{physical_base}{prefix}"
        if self.config.prefix and prefix == self.config.prefix:
            raise ValueError("logical prefix already contains the deployment prefix")
        if self.config.prefix and prefix.startswith(self.config.prefix + "/"):
            raise ValueError("logical prefix already contains the deployment prefix")
        params = self._params("placeholder")
        params["Prefix"] = physical_prefix
        params.pop("Key", None)
        result: list[ObjectMeta] = []
        token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            request = {**params}
            if token is not None:
                request["ContinuationToken"] = token
            try:
                page = self._read_request("list_objects_v2", **request)
            except ObjectStoreError:
                raise
            except Exception:
                raise StorageFailure("S3 list failed") from None
            self.metrics["list_calls"] += 1
            contents = page.get("Contents", [])
            if not isinstance(contents, list):
                raise StorageFailure("S3 list response is malformed")
            for item in contents:
                if not isinstance(item, dict) or not isinstance(item.get("Key"), str):
                    raise StorageFailure("S3 list response has an invalid object")
                physical = item["Key"]
                if physical_base and not physical.startswith(physical_base):
                    raise StorageFailure("S3 list response escaped the configured prefix")
                if not physical.startswith(physical_prefix):
                    raise StorageFailure("S3 list response escaped the configured prefix")
                logical = physical[len(physical_base):]
                try:
                    validate_key(logical)
                except ValueError as error:
                    raise StorageFailure("S3 list response has an invalid object") from error
                size = item.get("Size")
                if (
                    not isinstance(size, int)
                    or isinstance(size, bool)
                    or size < 0
                    or size > self.max_object_size
                ):
                    raise StorageFailure("S3 list response has an invalid size")
                result.append(ObjectMeta(logical, size, self._etag(item)))
            truncated = page.get("IsTruncated", False)
            next_token = page.get("NextContinuationToken")
            if not truncated:
                break
            if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                raise StorageFailure("S3 list continuation token is malformed or repeated")
            seen_tokens.add(next_token)
            token = next_token
        return sorted(result, key=lambda meta: meta.key)


__all__ = [
    "MAX_OBJECT_SIZE",
    "S3ObjectStore",
    "S3ObjectStoreConfig",
    "parse_s3_uri",
    "validate_bucket_name",
    "validate_prefix",
]
