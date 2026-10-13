import sys
import tempfile
import hashlib
from dataclasses import replace
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from errors import ForgeError
import identity
from object_store_memory import MemoryObjectStore
from repository import Candidate, Repository
from wal import (Manifest, ReplayState, apply_record, canonical_json,
                 event_id, record_fingerprint, validate_branch_ref)


def pack(seed):
    return {"kind": "canonical", "pack_key": f"packs/canonical/{seed * 64}.pack",
            "pack_sha256": seed * 64, "pack_git_checksum": seed * 40,
            "pack_size": 1, "idx_key": f"packs/canonical/{seed * 64}.idx",
            "idx_sha256": seed * 64, "idx_size": 1, "object_count": 1}


def descriptor(principal="admin", *, root_admin=True, reference="refs/heads/main"):
    payload = {"algorithm": identity.DESCRIPTOR_ALGORITHM, "principal_id": principal,
               "principal_kind": "user", "canonical_ref": reference, "team_ids": [],
               "role_ids": [], "binding_ids": [],
               "capabilities": {c: ([""] if c == "admin" and root_admin else []) for c in identity.CAPABILITIES},
               "restrictions": {c: [] for c in identity.CAPABILITIES}}
    record = {"id": "vdesc-" + hashlib.sha256(b"bastion-effective-view\0" + canonical_json(payload)).hexdigest(), **payload}
    return record


class BranchWalFoundationTest(unittest.TestCase):
    def manifest_dict(self):
        return {
            "repository_id": "a" * 32, "object_format": "sha1",
            "canonical_ref": "refs/heads/main", "canonical_tip": "1" * 40,
            "head_seq": 1,
            "wal_head": {"seq": 1, "key": "wal/records/1.json", "sha256": "2" * 64,
                         "event_id": "3" * 64, "publication_id": "b" * 32},
            "canonical_packs": [], "projection_algorithm": "projection-v2",
            "revision": 1, "updated_at": 0, "writer": "c" * 32,
        }

    def test_legacy_manifest_synthesizes_default_ref_and_extended_round_trips(self):
        legacy = Manifest.from_dict(self.manifest_dict())
        self.assertEqual({"refs/heads/main": "1" * 40}, legacy.canonical_refs)
        value = self.manifest_dict()
        value["canonical_refs"] = {"refs/heads/main": "1" * 40, "refs/heads/topic": "4" * 40}
        parsed = Manifest.from_dict(value)
        self.assertEqual(value["canonical_refs"], parsed.canonical_refs)
        self.assertEqual(value["canonical_refs"], Manifest.from_dict(parsed.to_dict()).canonical_refs)
        detached = dict(value["canonical_refs"])
        immutable = Manifest.from_dict({**value, "canonical_refs": detached})
        detached["refs/heads/side"] = "5" * 40
        self.assertNotIn("refs/heads/side", immutable.canonical_refs)
        with self.assertRaises(TypeError):
            immutable.canonical_refs["refs/heads/side"] = "5" * 40

    def test_manifest_alias_mismatch_and_namespace_conflict_rejected(self):
        value = self.manifest_dict()
        value["canonical_refs"] = {"refs/heads/main": "4" * 40}
        with self.assertRaises(ForgeError):
            Manifest.from_dict(value)
        value["canonical_refs"] = {"refs/heads/main": "1" * 40,
                                   "refs/heads/topic": "4" * 40,
                                   "refs/heads/topic/sub": "5" * 40}
        with self.assertRaises(ForgeError):
            Manifest.from_dict(value)

    def test_branch_ref_validation(self):
        self.assertEqual("refs/heads/topic/a", validate_branch_ref("refs/heads/topic/a"))
        for invalid in ("refs/heads/../evil", "refs/heads/a.lock", "refs/heads/a//b",
                        "refs/heads/a@{b", "refs/heads/a\x01b", "refs/heads/bad\\name",
                        "refs/heads/\ud800", "refs/tags/x"):
            with self.subTest(invalid=invalid), self.assertRaises(ForgeError):
                validate_branch_ref(invalid)

    def repository(self):
        store = MemoryObjectStore()
        repo = Repository(Path(tempfile.mkdtemp()), store, "a" * 32)
        admin = descriptor()
        repo.put_descriptor(admin)
        payload = {"canonical_ref": "refs/heads/main", "canonical_tip": "1" * 40,
                   "canonical_pack": pack("2"),
                   "change_claims": [{"change_id": "I-inherited", "owner_principal_id": "alice",
                                      "canonical_ref": "refs/heads/main"}]}
        repo.bootstrap(Candidate(record_fingerprint(repo.repository_id, "import", payload), payload))
        return repo, admin

    def publish_branch(self, repo, descriptor_id, ref="refs/heads/topic", source="refs/heads/main", tip="1" * 40):
        def build(manifest, _state):
            payload = {"principal_id": "admin", "descriptor_id": descriptor_id,
                       "source_ref": source, "branch_ref": ref, "canonical_tip": tip, "received_at": 7}
            return Candidate(record_fingerprint(repo.repository_id, "branch_create", payload), payload)
        return repo.publish(build)

    def test_authorized_branch_replay_and_inherited_claim_are_preserved(self):
        repo, admin = self.repository()
        self.assertEqual(2, self.publish_branch(repo, admin["id"]))
        manifest, _, replay = repo.load()
        self.assertEqual("1" * 40, manifest.canonical_refs["refs/heads/topic"])
        self.assertEqual({"owner_principal_id": "alice", "canonical_ref": "refs/heads/main"},
                         replay.change_claims["I-inherited"])
        def update(manifest, _state):
            payload = {"principal_id": "admin", "descriptor_id": admin["id"],
                       "canonical_ref": "refs/heads/main", "canonical_old": manifest.canonical_tip,
                       "canonical_new": "3" * 40, "canonical_pack": pack("4"),
                       "commits": [{"canonical": "3" * 40, "change_id": None}], "received_at": 8}
            return Candidate(record_fingerprint(repo.repository_id, "canonical_update", payload), payload)
        repo.publish(update)
        manifest, _, replay = repo.load()
        self.assertEqual("3" * 40, manifest.canonical_refs["refs/heads/main"])
        self.assertEqual("1" * 40, manifest.canonical_refs["refs/heads/topic"])
        self.assertEqual("1" * 40, replay.canonical_refs["refs/heads/topic"])
        dropped = replace(manifest, canonical_refs={"refs/heads/main": "3" * 40})
        with self.assertRaises(ForgeError):
            repo._check_replay_matches(dropped, replay)

    def test_wrong_principal_default_ref_and_non_root_admin_are_rejected(self):
        for bad in (descriptor("someone"), descriptor(root_admin=False),
                    descriptor(reference="refs/heads/other")):
            with self.subTest(principal=bad["principal_id"], root=bad["capabilities"]["admin"], ref=bad["canonical_ref"]):
                repo, _admin = self.repository()
                repo.put_descriptor(bad)
                with self.assertRaises(ForgeError):
                    self.publish_branch(repo, bad["id"])

    def test_duplicate_namespace_and_changed_source_are_rejected(self):
        repo, admin = self.repository()
        self.publish_branch(repo, admin["id"])
        self.assertEqual(2, self.publish_branch(repo, admin["id"]))
        for ref, source, tip in (("refs/heads/topic/child", "refs/heads/main", "1" * 40),
                                 ("refs/heads/other", "refs/heads/topic", "2" * 40),
                                 ("refs/heads/other", "refs/heads/missing", "1" * 40)):
            with self.subTest(ref=ref, source=source, tip=tip), self.assertRaises(ForgeError):
                self.publish_branch(repo, admin["id"], ref=ref, source=source, tip=tip)

    def test_state_copy_snapshot_and_default_update_preserve_secondary_ref(self):
        state = ReplayState(canonical_ref="refs/heads/main", canonical_tip="1" * 40,
                            canonical_refs={"refs/heads/main": "1" * 40, "refs/heads/side": "2" * 40})
        fork = state.clone_for_append()
        fork.canonical_refs["refs/heads/main"] = "3" * 40
        self.assertEqual("1" * 40, state.canonical_refs["refs/heads/main"])
        self.assertEqual("2" * 40, fork.canonical_refs["refs/heads/side"])
        snap = state.snapshot()
        with self.assertRaises(TypeError):
            snap.canonical_refs["refs/heads/x"] = "4" * 40

    def test_quota_and_zero_tips_rejected_without_mutating_replay(self):
        repo, admin = self.repository()
        _manifest, _, state = repo.load()
        state.canonical_refs = {f"refs/heads/b{i}": "1" * 40 for i in range(1023)}
        state.canonical_refs["refs/heads/main"] = "1" * 40
        self.assertEqual(1024, len(state.canonical_refs))
        before = dict(state.canonical_refs)
        payload = {"principal_id": "admin", "descriptor_id": admin["id"],
                   "source_ref": "refs/heads/main", "branch_ref": "refs/heads/new",
                   "canonical_tip": "1" * 40, "received_at": 1}
        fingerprint = record_fingerprint(repo.repository_id, "branch_create", payload)
        identifier = event_id(fingerprint)
        record = {"seq": 2, "kind": "branch_create", "event_fingerprint": fingerprint,
                  "event_id": identifier, "publication_id": "d" * 32,
                  "previous": {"seq": 1, "key": "wal/records/1.json", "sha256": "e" * 64},
                  "created_at": 1, "writer": "f" * 32, "payload": payload}
        with self.assertRaises(ForgeError):
            apply_record(state, record, descriptor_loader=repo._descriptor)
        self.assertEqual(before, state.canonical_refs)
        payload["canonical_tip"] = "0" * 40
        fingerprint = record_fingerprint(repo.repository_id, "branch_create", payload)
        record["event_fingerprint"] = fingerprint
        record["event_id"] = event_id(fingerprint)
        with self.assertRaises(ForgeError):
            apply_record(state, record, descriptor_loader=repo._descriptor)
        self.assertEqual(before, state.canonical_refs)
        value = self.manifest_dict()
        value["canonical_refs"] = {"refs/heads/main": "0" * 40}
        with self.assertRaises(ForgeError):
            Manifest.from_dict(value)


if __name__ == "__main__":
    unittest.main()
