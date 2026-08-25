"""Canonical encoding, validation, and replay for the Bastion WAL."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import identity
from errors import ForgeError


FORMAT_VERSION = 1
OBJECT_FORMAT = "sha1"
PROJECTION_ALGORITHM = "projection-v2"
OID_RE = re.compile(r"^[0-9a-f]{40}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
UUID_RE = re.compile(r"^[0-9a-f]{32}$")
DESCRIPTOR_RE = re.compile(r"^vdesc-[0-9a-f]{64}$")
KINDS = {"import", "view_create", "submission", "canonical_update", "policy_transition"}


def canonical_json(value: object) -> bytes:
    """The only JSON encoding used for repository-owned objects."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def event_id(fingerprint: dict) -> str:
    return sha256(b"bastion-event\0" + canonical_json(fingerprint))


def idempotency_key_hash(repository_id: str, key: str) -> str:
    _uuid(repository_id, "repository id")
    if not isinstance(key, str):
        raise ForgeError("idempotency key must be text")
    return sha256(
        b"bastion-idempotency\0"
        + repository_id.encode("ascii")
        + b"\0"
        + key.encode("utf-8")
    )


def _fail(message: str) -> None:
    raise ForgeError(f"invalid WAL object: {message}", "integrity")


def _exact(value: object, fields: set[str], subject: str) -> dict:
    if not isinstance(value, dict):
        _fail(f"{subject} must be an object")
    missing = fields - set(value)
    extra = set(value) - fields
    if missing or extra:
        _fail(f"{subject} has unexpected fields")
    return value


def _string(value: object, subject: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        _fail(f"{subject} must be a {'nonempty ' if nonempty else ''}string")
    return value


def _integer(value: object, subject: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        _fail(f"{subject} must be an integer >= {minimum}")
    return value


def _oid(value: object, subject: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not OID_RE.fullmatch(value):
        _fail(f"{subject} must be a lowercase SHA-1 OID")
    return value


def _hash(value: object, subject: str) -> str:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        _fail(f"{subject} must be a lowercase SHA-256 hash")
    return value


def _uuid(value: object, subject: str) -> str:
    if not isinstance(value, str) or not UUID_RE.fullmatch(value):
        _fail(f"{subject} must be a lowercase UUID hex value")
    return value


def _key(value: object, subject: str) -> str:
    key = _string(value, subject)
    if not key.isascii() or "\\" in key or "\x00" in key:
        _fail(f"{subject} is not an ASCII object key")
    for part in key.split("/"):
        if not part or part in {".", ".."}:
            _fail(f"{subject} has an unsafe component")
    return key


def _descriptor_id(value: object, subject: str) -> str:
    if not isinstance(value, str) or not DESCRIPTOR_RE.fullmatch(value):
        _fail(f"{subject} must be a descriptor ID")
    return value


def _ref(value: object, subject: str) -> str:
    reference = _string(value, subject)
    if not reference.startswith("refs/heads/"):
        _fail(f"{subject} must be a branch ref")
    return reference


def _pack_ref(value: object, expected_kind: str | None = None) -> dict:
    fields = {
        "kind",
        "pack_key",
        "pack_sha256",
        "pack_git_checksum",
        "pack_size",
        "idx_key",
        "idx_sha256",
        "idx_size",
        "object_count",
    }
    result = _exact(value, fields, "pack reference")
    kind = result["kind"]
    if kind not in {"canonical", "submission"}:
        _fail("pack reference has an unknown kind")
    if expected_kind and kind != expected_kind:
        _fail("pack reference has the wrong kind")
    prefix = {
        "canonical": "packs/canonical/",
        "submission": "packs/submissions/",
    }[kind]
    pack_key = _key(result["pack_key"], "pack reference pack_key")
    idx_key = _key(result["idx_key"], "pack reference idx_key")
    if not pack_key.startswith(prefix) or not idx_key.startswith(prefix):
        _fail("pack reference namespace does not match its kind")
    if not pack_key.endswith(".pack") or not idx_key.endswith(".idx"):
        _fail("pack reference names must end in .pack/.idx")
    _hash(result["pack_sha256"], "pack reference pack_sha256")
    _hash(result["idx_sha256"], "pack reference idx_sha256")
    _oid(result["pack_git_checksum"], "pack reference pack_git_checksum")
    _integer(result["pack_size"], "pack reference pack_size", 1)
    _integer(result["idx_size"], "pack reference idx_size", 1)
    _integer(result["object_count"], "pack reference object_count", 0)
    return copy.deepcopy(result)


def validate_descriptor(record: object) -> dict:
    fields = {
        "id", "algorithm", "principal_id", "principal_kind", "canonical_ref",
        "team_ids", "role_ids", "binding_ids", "capabilities", "restrictions",
    }
    record = _exact(record, fields, "descriptor")
    identifier = _descriptor_id(record["id"], "descriptor id")
    payload = {key: value for key, value in record.items() if key != "id"}
    if payload["algorithm"] != identity.DESCRIPTOR_ALGORITHM:
        _fail("descriptor algorithm is unsupported")
    _string(payload["principal_id"], "descriptor principal")
    if payload["principal_kind"] not in identity.PRINCIPAL_KINDS:
        _fail("descriptor principal kind is invalid")
    _ref(payload["canonical_ref"], "descriptor canonical_ref")
    for name in ("team_ids", "role_ids", "binding_ids"):
        values = payload[name]
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            _fail(f"descriptor {name} must be a string list")
        if values != sorted(set(values)):
            _fail(f"descriptor {name} must be sorted and unique")
    for name in ("capabilities", "restrictions"):
        values = payload[name]
        if not isinstance(values, dict) or set(values) != set(identity.CAPABILITIES):
            _fail(f"descriptor {name} has an invalid shape")
        if any(not isinstance(item, list) or not all(isinstance(path, str) for path in item) for item in values.values()):
            _fail(f"descriptor {name} has invalid paths")
    if identity.compute_descriptor_id(payload) != identifier:
        _fail("descriptor ID does not match its canonical payload")
    return copy.deepcopy(record)


@dataclass(frozen=True)
class WalHead:
    seq: int
    key: str
    sha256: str
    event_id: str
    publication_id: str

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "key": self.key,
            "sha256": self.sha256,
            "event_id": self.event_id,
            "publication_id": self.publication_id,
        }


@dataclass(frozen=True)
class Manifest:
    repository_id: str
    canonical_ref: str
    canonical_tip: str
    head_seq: int
    wal_head: WalHead
    canonical_packs: tuple[dict, ...]
    revision: int
    updated_at: int
    writer: str
    object_format: str = OBJECT_FORMAT
    projection_algorithm: str = PROJECTION_ALGORITHM

    def to_dict(self) -> dict:
        return {
            "format_version": FORMAT_VERSION,
            "repository_id": self.repository_id,
            "object_format": self.object_format,
            "canonical_ref": self.canonical_ref,
            "canonical_tip": self.canonical_tip,
            "head_seq": self.head_seq,
            "wal_head": self.wal_head.to_dict(),
            "canonical_packs": [copy.deepcopy(item) for item in self.canonical_packs],
            "projection_algorithm": self.projection_algorithm,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "writer": self.writer,
        }

    @classmethod
    def from_dict(cls, value: object) -> "Manifest":
        fields = {
            "format_version",
            "repository_id",
            "object_format",
            "canonical_ref",
            "canonical_tip",
            "head_seq",
            "wal_head",
            "canonical_packs",
            "projection_algorithm",
            "revision",
            "updated_at",
            "writer",
        }
        record = _exact(value, fields, "manifest")
        if record["format_version"] != FORMAT_VERSION:
            _fail("manifest format_version is unsupported")
        repository_id = _uuid(record["repository_id"], "manifest repository_id")
        if record["object_format"] != OBJECT_FORMAT:
            _fail("manifest object_format is unsupported")
        if record["projection_algorithm"] != PROJECTION_ALGORITHM:
            _fail("manifest projection_algorithm is unsupported")
        head_seq = _integer(record["head_seq"], "manifest head_seq", 1)
        head = _exact(
            record["wal_head"],
            {"seq", "key", "sha256", "event_id", "publication_id"},
            "manifest wal_head",
        )
        if _integer(head["seq"], "manifest wal_head seq", 1) != head_seq:
            _fail("manifest wal_head seq differs from head_seq")
        wal_head = WalHead(
            head_seq,
            _key(head["key"], "manifest wal_head key"),
            _hash(head["sha256"], "manifest wal_head sha256"),
            _hash(head["event_id"], "manifest wal_head event_id"),
            _uuid(head["publication_id"], "manifest wal_head publication_id"),
        )
        if not wal_head.key.startswith("wal/records/") or not wal_head.key.endswith(".json"):
            _fail("manifest wal_head key is outside WAL records")
        packs = record["canonical_packs"]
        if not isinstance(packs, list):
            _fail("manifest canonical_packs must be a list")
        validated_packs = tuple(_pack_ref(item, "canonical") for item in packs)
        if len({item["pack_key"] for item in validated_packs}) != len(validated_packs):
            _fail("manifest canonical packs are duplicated")
        return cls(
            repository_id,
            _ref(record["canonical_ref"], "manifest canonical_ref"),
            _oid(record["canonical_tip"], "manifest canonical_tip") or "",
            head_seq,
            wal_head,
            validated_packs,
            _integer(record["revision"], "manifest revision", 1),
            _integer(record["updated_at"], "manifest updated_at", 0),
            _uuid(record["writer"], "manifest writer"),
        )


@dataclass
class ReplayState:
    repository_id: str | None = None
    head_seq: int = 0
    head_record: dict | None = None
    canonical_ref: str | None = None
    canonical_tip: str | None = None
    canonical_packs: list[dict] = field(default_factory=list)
    active_views: dict[str, dict] = field(default_factory=dict)
    change_claims: dict[str, dict] = field(default_factory=dict)
    events: dict[str, dict] = field(default_factory=dict)
    requests: dict[tuple[str, str, str, str], list[str]] = field(default_factory=dict)
    idempotency: dict[str, dict] = field(default_factory=dict)
    records: list[dict] = field(default_factory=list)

    def clone(self) -> "ReplayState":
        return copy.deepcopy(self)


def record_fingerprint(repository_id: str, kind: str, payload: dict) -> dict:
    _uuid(repository_id, "repository id")
    if kind == "import":
        result = {
            "kind": kind,
            "repository_id": repository_id,
            "canonical_ref": payload.get("canonical_ref"),
            "canonical_tip": payload.get("canonical_tip"),
        }
    elif kind == "view_create":
        result = {
            "kind": kind,
            "repository_id": repository_id,
            **{key: payload.get(key) for key in (
                "view_id", "principal_id", "projected_ref", "descriptor_id",
                "descriptor_generation", "projection_epoch", "canonical_frontier",
            )},
        }
    elif kind == "submission":
        result = {
            "kind": kind,
            "repository_id": repository_id,
            **{key: payload.get(key) for key in (
                "view_id", "projected_ref", "projected_old", "projected_new",
                "descriptor_id", "descriptor_generation", "projection_epoch",
            )},
        }
    elif kind == "canonical_update":
        result = {
            "kind": kind,
            "repository_id": repository_id,
            **{key: payload.get(key) for key in (
                "principal_id", "canonical_ref", "canonical_old", "canonical_new",
            )},
        }
    elif kind == "policy_transition":
        views = payload.get("views")
        result = {
            "kind": kind,
            "repository_id": repository_id,
            "principal_id": payload.get("principal_id"),
            "canonical_frontier": payload.get("canonical_frontier"),
            "views": views,
        }
    else:
        _fail("record has an unknown kind")
    return result


def validate_record(value: object, *, repository_id: str | None = None) -> dict:
    fields = {
        "format_version",
        "seq",
        "kind",
        "event_fingerprint",
        "event_id",
        "publication_id",
        "previous",
        "created_at",
        "writer",
        "payload",
    }
    record = _exact(value, fields, "WAL record")
    if record["format_version"] != FORMAT_VERSION:
        _fail("record format_version is unsupported")
    seq = _integer(record["seq"], "record seq", 1)
    kind = record["kind"]
    if kind not in KINDS:
        _fail("record kind is unsupported")
    fingerprint = record["event_fingerprint"]
    if not isinstance(fingerprint, dict):
        _fail("record event_fingerprint must be an object")
    event = _hash(record["event_id"], "record event_id")
    if event_id(fingerprint) != event:
        _fail("record event_id does not match its fingerprint")
    fingerprint_repository = _uuid(fingerprint.get("repository_id"), "fingerprint repository_id")
    if repository_id is not None and fingerprint_repository != repository_id:
        _fail("record repository_id differs from manifest")
    if fingerprint.get("kind") != kind:
        _fail("record fingerprint kind differs from envelope")
    previous = record["previous"]
    if seq == 1:
        if previous is not None:
            _fail("sequence-one record has a previous reference")
    else:
        previous = _exact(previous, {"seq", "key", "sha256"}, "record previous")
        if _integer(previous["seq"], "record previous seq", 1) != seq - 1:
            _fail("record previous sequence is not contiguous")
        previous["key"] = _key(previous["key"], "record previous key")
        previous["sha256"] = _hash(previous["sha256"], "record previous sha256")
    _integer(record["created_at"], "record created_at", 0)
    _uuid(record["writer"], "record writer")
    _uuid(record["publication_id"], "record publication_id")
    if not isinstance(record["payload"], dict):
        _fail("record payload must be an object")
    normalized = copy.deepcopy(record)
    normalized["event_fingerprint"] = copy.deepcopy(fingerprint)
    normalized["previous"] = copy.deepcopy(previous)
    return normalized


def _claims(value: object) -> list[dict]:
    if not isinstance(value, list):
        _fail("change_claims must be a list")
    result: list[dict] = []
    last = ""
    for item in value:
        claim = _exact(item, {"change_id", "owner_principal_id", "canonical_ref"}, "change claim")
        change_id = _string(claim["change_id"], "change claim id")
        if change_id <= last:
            _fail("change claims are not sorted and unique")
        last = change_id
        owner = claim["owner_principal_id"]
        if owner is not None:
            _string(owner, "change claim owner")
        result.append({
            "change_id": change_id,
            "owner_principal_id": owner,
            "canonical_ref": _ref(claim["canonical_ref"], "change claim ref"),
        })
    return result


def _mappings(value: object) -> list[dict]:
    if not isinstance(value, list) or not value:
        _fail("submission mappings must be a nonempty list")
    result: list[dict] = []
    submitted: set[str] = set()
    canonical: set[str] = set()
    for item in value:
        mapping = _exact(item, {"submitted", "canonical", "change_id"}, "submission mapping")
        submitted_oid = _oid(mapping["submitted"], "submission mapping submitted") or ""
        canonical_oid = _oid(mapping["canonical"], "submission mapping canonical") or ""
        if submitted_oid in submitted or canonical_oid in canonical:
            _fail("submission mappings contain duplicate OIDs")
        submitted.add(submitted_oid)
        canonical.add(canonical_oid)
        change_id = mapping["change_id"]
        if change_id is not None:
            _string(change_id, "submission mapping Change-Id")
        result.append({"submitted": submitted_oid, "canonical": canonical_oid, "change_id": change_id})
    return result


def _require_descriptor(
    descriptor_id: str, loader: Callable[[str], dict] | None, principal: str, reference: str
) -> None:
    if loader is None:
        return
    descriptor = validate_descriptor(loader(descriptor_id))
    if descriptor["id"] != descriptor_id:
        _fail("descriptor object key and ID differ")
    if descriptor.get("principal_id") != principal or descriptor.get("canonical_ref") != reference:
        _fail("descriptor does not match view provenance")


def _validate_payload(
    state: ReplayState, record: dict, loader: Callable[[str], dict] | None
) -> None:
    kind = record["kind"]
    payload = record["payload"]
    fingerprint = record_fingerprint(record["event_fingerprint"]["repository_id"], kind, payload)
    if fingerprint != record["event_fingerprint"]:
        _fail("record fingerprint does not match payload")
    if kind == "import":
        fields = {"canonical_ref", "canonical_tip", "canonical_pack", "change_claims"}
        value = _exact(payload, fields, "import payload")
        _ref(value["canonical_ref"], "import canonical_ref")
        _oid(value["canonical_tip"], "import canonical_tip")
        _pack_ref(value["canonical_pack"], "canonical")
        _claims(value["change_claims"])
        return
    if kind == "view_create":
        fields = {
            "view_id", "principal_id", "projected_ref", "descriptor_id",
            "descriptor_generation", "projection_epoch", "canonical_frontier",
        }
        value = _exact(payload, fields, "view_create payload")
        _string(value["view_id"], "view id")
        _string(value["principal_id"], "view principal")
        _ref(value["projected_ref"], "view projected_ref")
        descriptor_id = _descriptor_id(value["descriptor_id"], "view descriptor_id")
        _integer(value["descriptor_generation"], "view descriptor_generation", 1)
        _integer(value["projection_epoch"], "view projection_epoch", 1)
        _oid(value["canonical_frontier"], "view canonical_frontier")
        _require_descriptor(descriptor_id, loader, value["principal_id"], state.canonical_ref or "")
        return
    if kind == "submission":
        fields = {
            "idempotency_key_hash", "view_id", "principal_id", "descriptor_id",
            "descriptor_generation", "projection_epoch", "canonical_ref", "canonical_old",
            "canonical_new", "projected_ref", "projected_old", "projected_new",
            "canonical_frontier_old", "canonical_pack", "submission_pack", "mappings",
            "received_at", "transport", "remote_identity",
        }
        value = _exact(payload, fields, "submission payload")
        if value["idempotency_key_hash"] is not None:
            _hash(value["idempotency_key_hash"], "submission idempotency_key_hash")
        for field in ("view_id", "principal_id"):
            _string(value[field], f"submission {field}")
        descriptor_id = _descriptor_id(value["descriptor_id"], "submission descriptor_id")
        _integer(value["descriptor_generation"], "submission descriptor_generation", 1)
        _integer(value["projection_epoch"], "submission projection_epoch", 1)
        _ref(value["canonical_ref"], "submission canonical_ref")
        _ref(value["projected_ref"], "submission projected_ref")
        for field in ("canonical_old", "canonical_new", "projected_old", "projected_new", "canonical_frontier_old"):
            _oid(value[field], f"submission {field}")
        _pack_ref(value["canonical_pack"], "canonical")
        _pack_ref(value["submission_pack"], "submission")
        _mappings(value["mappings"])
        _integer(value["received_at"], "submission received_at", 0)
        transport = _string(value["transport"], "submission transport")
        if not transport.isascii():
            _fail("submission transport is not ASCII")
        if value["remote_identity"] is not None:
            _string(value["remote_identity"], "submission remote_identity", nonempty=False)
        _require_descriptor(descriptor_id, loader, value["principal_id"], value["canonical_ref"])
        return
    if kind == "canonical_update":
        fields = {"principal_id", "canonical_ref", "canonical_old", "canonical_new", "canonical_pack", "commits"}
        value = _exact(payload, fields, "canonical_update payload")
        _string(value["principal_id"], "canonical update principal")
        _ref(value["canonical_ref"], "canonical update ref")
        _oid(value["canonical_old"], "canonical update old")
        _oid(value["canonical_new"], "canonical update new")
        _pack_ref(value["canonical_pack"], "canonical")
        if not isinstance(value["commits"], list) or not value["commits"]:
            _fail("canonical update commits must be nonempty")
        seen: set[str] = set()
        for item in value["commits"]:
            commit = _exact(item, {"canonical", "change_id"}, "canonical update commit")
            oid = _oid(commit["canonical"], "canonical update commit OID") or ""
            if oid in seen:
                _fail("canonical update commits are duplicated")
            seen.add(oid)
            if commit["change_id"] is not None:
                _string(commit["change_id"], "canonical update Change-Id")
        return
    fields = {"principal_id", "canonical_frontier", "views"}
    value = _exact(payload, fields, "policy_transition payload")
    _string(value["principal_id"], "policy principal")
    _oid(value["canonical_frontier"], "policy canonical_frontier")
    if not isinstance(value["views"], list):
        _fail("policy views must be a list")
    previous = ""
    for item in value["views"]:
        view = _exact(
            item,
            {"view_id", "old_descriptor_id", "old_generation", "old_projection_epoch", "new_descriptor_id", "new_generation", "new_projection_epoch"},
            "policy view",
        )
        view_id = _string(view["view_id"], "policy view id")
        if view_id <= previous:
            _fail("policy views are not sorted and unique")
        previous = view_id
        _descriptor_id(view["old_descriptor_id"], "policy old descriptor")
        _descriptor_id(view["new_descriptor_id"], "policy new descriptor")
        _integer(view["old_generation"], "policy old generation", 1)
        _integer(view["new_generation"], "policy new generation", 1)
        _integer(view["old_projection_epoch"], "policy old epoch", 1)
        _integer(view["new_projection_epoch"], "policy new epoch", 1)


def apply_record(
    state: ReplayState,
    value: object,
    *,
    record_ref: dict | None = None,
    descriptor_loader: Callable[[str], dict] | None = None,
) -> None:
    """Apply one verified record and enforce every metadata transition."""
    record = validate_record(value, repository_id=state.repository_id)
    if record["seq"] != state.head_seq + 1:
        _fail("record sequence is not the next contiguous sequence")
    if state.head_seq:
        previous = record["previous"]
        assert isinstance(previous, dict)
        expected = state.head_record or {}
        if previous["seq"] != state.head_seq or (
            expected and (previous["key"] != expected.get("key") or previous["sha256"] != expected.get("sha256"))
        ):
            _fail("record previous reference differs from the committed head")
    elif record["previous"] is not None:
        _fail("initial record has a previous reference")
    if state.repository_id is None:
        state.repository_id = record["event_fingerprint"]["repository_id"]
    _validate_payload(state, record, descriptor_loader)
    payload = record["payload"]
    kind = record["kind"]
    if kind == "import":
        if state.canonical_ref is not None:
            _fail("import is not the first record")
        state.canonical_ref = payload["canonical_ref"]
        state.canonical_tip = payload["canonical_tip"]
        state.canonical_packs = [copy.deepcopy(payload["canonical_pack"])]
        for claim in _claims(payload["change_claims"]):
            state.change_claims[claim["change_id"]] = {
                "owner_principal_id": claim["owner_principal_id"],
                "canonical_ref": claim["canonical_ref"],
            }
    elif kind == "view_create":
        view_id = payload["view_id"]
        if view_id in state.active_views:
            _fail("view already exists")
        if payload["canonical_frontier"] != state.canonical_tip:
            _fail("view canonical frontier is not the current canonical tip")
        state.active_views[view_id] = {
            "principal_id": payload["principal_id"],
            "projected_ref": payload["projected_ref"],
            "descriptor_id": payload["descriptor_id"],
            "descriptor_generation": payload["descriptor_generation"],
            "projection_epoch": payload["projection_epoch"],
            "canonical_frontier": payload["canonical_frontier"],
            "epoch_start_seq": record["seq"],
        }
    elif kind == "submission":
        view = state.active_views.get(payload["view_id"])
        if view is None:
            _fail("submission source view does not exist")
        required = ("principal_id", "descriptor_id", "descriptor_generation", "projection_epoch", "projected_ref")
        if any(payload[field] != view[field] for field in required):
            _fail("submission does not match its active source view")
        if payload["canonical_ref"] != state.canonical_ref or payload["canonical_old"] != state.canonical_tip:
            _fail("submission does not extend the current canonical tip")
        if payload["canonical_frontier_old"] != view["canonical_frontier"]:
            _fail("submission canonical frontier differs from source view")
        mappings = _mappings(payload["mappings"])
        if mappings[0]["submitted"] == payload["projected_old"] or mappings[-1]["submitted"] != payload["projected_new"] or mappings[-1]["canonical"] != payload["canonical_new"]:
            _fail("submission mapping endpoints differ from payload")
        request = (payload["view_id"], payload["projected_ref"], payload["projected_old"], payload["projected_new"])
        if request in state.requests:
            for event in state.requests[request]:
                prior = state.records[state.events[event]["seq"] - 1]["payload"]
                if prior["projection_epoch"] == payload["projection_epoch"]:
                    _fail("submission request was already committed in this epoch")
        key_hash = payload["idempotency_key_hash"]
        if key_hash is not None and key_hash in state.idempotency:
            _fail("submission idempotency key was already committed")
        for mapping in mappings:
            change_id = mapping["change_id"]
            if not change_id:
                continue
            claim = state.change_claims.get(change_id)
            if claim is not None and (
                claim["owner_principal_id"] != payload["principal_id"]
                or claim["canonical_ref"] != payload["canonical_ref"]
            ):
                _fail("Change-Id belongs to another principal or branch")
            if claim is None:
                state.change_claims[change_id] = {
                    "owner_principal_id": payload["principal_id"],
                    "canonical_ref": payload["canonical_ref"],
                }
        state.canonical_packs.append(copy.deepcopy(payload["canonical_pack"]))
        state.canonical_tip = payload["canonical_new"]
        for active in state.active_views.values():
            active["canonical_frontier"] = payload["canonical_new"]
        state.requests.setdefault(request, []).append(record["event_id"])
        if key_hash is not None:
            state.idempotency[key_hash] = {
                "event_id": record["event_id"],
                "event_fingerprint": copy.deepcopy(record["event_fingerprint"]),
            }
    elif kind == "canonical_update":
        if payload["canonical_ref"] != state.canonical_ref or payload["canonical_old"] != state.canonical_tip:
            _fail("canonical update does not extend the current tip")
        commits = payload["commits"]
        if commits[-1]["canonical"] != payload["canonical_new"]:
            _fail("canonical update commits do not end at new tip")
        state.canonical_packs.append(copy.deepcopy(payload["canonical_pack"]))
        state.canonical_tip = payload["canonical_new"]
        for active in state.active_views.values():
            active["canonical_frontier"] = payload["canonical_new"]
        for commit in commits:
            change_id = commit["change_id"]
            if change_id and change_id not in state.change_claims:
                state.change_claims[change_id] = {
                    "owner_principal_id": None,
                    "canonical_ref": payload["canonical_ref"],
                }
    else:
        if payload["canonical_frontier"] != state.canonical_tip:
            _fail("policy transition is not at the current canonical tip")
        expected = sorted(
            view_id for view_id, view in state.active_views.items()
            if view["principal_id"] == payload["principal_id"]
        )
        if [item["view_id"] for item in payload["views"]] != expected:
            _fail("policy transition does not cover exactly the active principal views")
        for item in payload["views"]:
            active = state.active_views[item["view_id"]]
            if (
                active["descriptor_id"] != item["old_descriptor_id"]
                or active["descriptor_generation"] != item["old_generation"]
                or active["projection_epoch"] != item["old_projection_epoch"]
                or item["new_generation"] != item["old_generation"] + 1
                or item["new_projection_epoch"] != item["old_projection_epoch"] + 1
            ):
                _fail("policy transition has an invalid view generation")
            _require_descriptor(
                item["new_descriptor_id"], descriptor_loader,
                active["principal_id"], state.canonical_ref or "",
            )
            active["descriptor_id"] = item["new_descriptor_id"]
            active["descriptor_generation"] = item["new_generation"]
            active["projection_epoch"] = item["new_projection_epoch"]
            active["epoch_start_seq"] = record["seq"]
    existing = state.events.get(record["event_id"])
    if existing is not None:
        _fail("event ID is duplicated in WAL")
    head = {
        "seq": record["seq"],
        "key": record_ref.get("key") if record_ref else "",
        "sha256": record_ref.get("sha256") if record_ref else "",
        "event_id": record["event_id"],
        "publication_id": record["publication_id"],
    }
    state.head_seq = record["seq"]
    state.head_record = head
    state.events[record["event_id"]] = {
        "seq": record["seq"],
        "event_fingerprint": copy.deepcopy(record["event_fingerprint"]),
        "publication_id": record["publication_id"],
    }
    state.records.append(copy.deepcopy(record))


def validate_record_objects(record: dict, canonical_repo, view_repo=None) -> None:
    """Verify graph-dependent endpoints after packs are installed locally."""
    from git import first_parent_commits, git, output

    payload = record["payload"]
    kind = record["kind"]
    if kind == "import":
        if git(canonical_repo, "cat-file", "-e", f"{payload['canonical_tip']}^{{commit}}", check=False).returncode:
            _fail("import canonical tip is missing from canonical packs")
    elif kind == "submission":
        canonical = first_parent_commits(canonical_repo, payload["canonical_old"], payload["canonical_new"])
        mappings = payload["mappings"]
        if canonical != [item["canonical"] for item in mappings]:
            _fail("submission canonical mappings are not an exact first-parent range")
        if view_repo is not None:
            submitted = output(view_repo, "rev-list", "--reverse", f"{payload['projected_old']}..{payload['projected_new']}").splitlines()
            if submitted != [item["submitted"] for item in mappings]:
                _fail("submission evidence mappings are not an exact first-parent range")
    elif kind == "canonical_update":
        commits = first_parent_commits(canonical_repo, payload["canonical_old"], payload["canonical_new"])
        if commits != [item["canonical"] for item in payload["commits"]]:
            _fail("canonical update commits are not an exact first-parent range")
