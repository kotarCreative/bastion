"""Linked WAL and manifest-CAS publication tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import identity
from object_store import AmbiguousWrite, Update
from object_store_memory import MemoryObjectStore
from repository import Candidate, Repository
from wal import canonical_json, event_id, record_fingerprint, validate_descriptor


def pack(seed: str) -> dict:
    return {
        "kind": "canonical",
        "pack_key": f"packs/canonical/{seed * 64}.pack",
        "pack_sha256": seed * 64,
        "pack_git_checksum": seed * 40,
        "pack_size": 1,
        "idx_key": f"packs/canonical/{seed * 64}.idx",
        "idx_sha256": seed * 64,
        "idx_size": 1,
        "object_count": 1,
    }


def admin_descriptor(repository_id: str) -> dict:
    payload = {
        "algorithm": identity.DESCRIPTOR_ALGORITHM,
        "principal_id": "admin",
        "principal_kind": "user",
        "canonical_ref": "refs/heads/main",
        "team_ids": [],
        "role_ids": [],
        "binding_ids": [],
        "capabilities": {capability: [""] if capability == "admin" else [] for capability in identity.CAPABILITIES},
        "restrictions": {capability: [] for capability in identity.CAPABILITIES},
    }
    identifier = "vdesc-" + __import__("hashlib").sha256(
        b"bastion-effective-view\0" + canonical_json(payload)
    ).hexdigest()
    record = {"id": identifier}
    record.update(payload)
    return validate_descriptor(record)


class OneAmbiguousManifestStore(MemoryObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.ambiguous = True

    def put(self, key, body, *, mode):
        result = super().put(key, body, mode=mode)
        if self.ambiguous and key.endswith("/manifest.json") and isinstance(mode, Update):
            self.ambiguous = False
            raise AmbiguousWrite("response lost after apply")
        return result


class WalTest(unittest.TestCase):
    def _repository(self, store: MemoryObjectStore) -> Repository:
        repository = Repository(Path(tempfile.mkdtemp()), store, "a" * 32)
        descriptor = admin_descriptor("a" * 32)
        repository.put_descriptor(descriptor)
        payload = {
            "canonical_ref": "refs/heads/main",
            "canonical_tip": "1" * 40,
            "canonical_pack": pack("2"),
            "change_claims": [],
        }
        repository.bootstrap(Candidate(record_fingerprint("a" * 32, "import", payload), payload))
        return repository

    def test_publish_is_a_contiguous_linked_chain_without_listing(self) -> None:
        store = MemoryObjectStore()
        repository = self._repository(store)
        descriptor = admin_descriptor(repository.repository_id)

        def build(manifest, replay):
            payload = {
                "principal_id": "admin",
                "descriptor_id": descriptor["id"],
                "canonical_ref": "refs/heads/main",
                "canonical_old": manifest.canonical_tip,
                "canonical_new": "3" * 40,
                "canonical_pack": pack("4"),
                "commits": [{"canonical": "3" * 40, "change_id": None}],
                "received_at": 1,
            }
            return Candidate(record_fingerprint(repository.repository_id, "canonical_update", payload), payload)

        self.assertEqual(2, repository.publish(build))
        manifest, _version, replay = repository.load()
        self.assertEqual(2, manifest.head_seq)
        self.assertEqual([1, 2], [record["seq"] for record in replay.records])
        self.assertEqual(0, store.list_calls)

    def test_ambiguous_applied_manifest_update_is_resolved_from_the_chain(self) -> None:
        store = OneAmbiguousManifestStore()
        repository = self._repository(store)
        descriptor = admin_descriptor(repository.repository_id)

        def build(manifest, replay):
            payload = {
                "principal_id": "admin",
                "descriptor_id": descriptor["id"],
                "canonical_ref": "refs/heads/main",
                "canonical_old": manifest.canonical_tip,
                "canonical_new": "3" * 40,
                "canonical_pack": pack("4"),
                "commits": [{"canonical": "3" * 40, "change_id": None}],
                "received_at": 1,
            }
            return Candidate(record_fingerprint(repository.repository_id, "canonical_update", payload), payload)

        self.assertEqual(2, repository.publish(build))
        self.assertEqual(2, repository.load()[0].head_seq)
