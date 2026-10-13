"""Receive-envelope guards for permission-scoped branch transport."""

import pytest
import subprocess
from pathlib import Path
from types import SimpleNamespace

from errors import ForgeError
from layout import ZERO_OID
from layout import intake_repo
from permissions import empty_permissions, transport_branch_allowed
from identity import empty_capability_map
from repository import Repository
from service import add_role, bind_role, create_branch, transition_principal
from tests.transport import attach_intake_push
from validation import select_advertised_branch_base, validate_branch_update_shape


def _descriptor():
    permissions = empty_permissions()
    permissions["branches"]["allow"]["read"] = ["refs/heads/*"]
    permissions["branches"]["allow"]["create"] = ["refs/heads/topic"]
    permissions["branches"]["allow"]["update"] = ["refs/heads/main"]
    return {"algorithm": "capabilities-v4", "permissions": permissions}


def test_v4_branch_permissions_are_independent_actions():
    descriptor = _descriptor()
    assert transport_branch_allowed(descriptor, "read", "refs/heads/topic")
    assert transport_branch_allowed(descriptor, "create", "refs/heads/topic")
    assert not transport_branch_allowed(descriptor, "update", "refs/heads/topic")
    assert transport_branch_allowed(descriptor, "update", "refs/heads/main")
    assert not transport_branch_allowed(descriptor, "create", "refs/heads/main")


def test_branch_envelope_only_allows_one_nondeletion_heads_ref():
    new = "1" * 40
    assert validate_branch_update_shape([(ZERO_OID, new, "refs/heads/topic")]) == (
        ZERO_OID,
        new,
        "refs/heads/topic",
    )
    for updates in (
        [],
        [(ZERO_OID, new, "refs/tags/topic")],
        [(ZERO_OID, ZERO_OID, "refs/heads/topic")],
        [(ZERO_OID, new, "refs/heads/a"), (ZERO_OID, new, "refs/heads/b")],
    ):
        with pytest.raises(ForgeError):
            validate_branch_update_shape(updates)


def test_creation_base_is_nearest_current_tip_and_rejects_hidden_frontier_ambiguity():
    submitted = ["n" * 40, "f" * 40, "m" * 40]
    sources = {
        "f" * 40: [("refs/heads/feature", "c" * 40)],
        "m" * 40: [("refs/heads/main", "d" * 40)],
    }
    assert select_advertised_branch_base(submitted, sources, 10) == (
        "refs/heads/feature", "f" * 40, "c" * 40,
    )
    sources["f" * 40].append(("refs/heads/hidden-fork", "e" * 40))
    with pytest.raises(ForgeError, match="ambiguous"):
        select_advertised_branch_base(submitted, sources, 10)
    sources["f" * 40].pop()
    with pytest.raises(ForgeError):
        select_advertised_branch_base(submitted, sources, 0)
    with pytest.raises(ForgeError):
        select_advertised_branch_base(submitted, {"x" * 40: [("refs/heads/main", "d" * 40)]}, 10)


def _real_v4_receive(tmp_path, *, update_topic=True):
    """Build a real bare-intake fixture and attach an actual Git client."""
    import forge
    import identity_harness
    from tests.identity_harness import provision_admin

    source = Path(tmp_path) / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.test")):
        subprocess.run(["git", "-C", str(source), "config", key, value], check=True)
    (source / "team-a").mkdir()
    (source / "team-a" / "base.txt").write_text("base\n")
    (source / "hidden").mkdir()
    (source / "hidden" / "secret.txt").write_text("hidden\n")
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "base"], check=True)
    root = Path(tmp_path) / "forge"
    forge.init_forge(root, source, "main")
    admin = provision_admin(root)
    create_branch(root, "feature", admin)
    identity_harness.add_view(root, "alice", ["team-a"], ["team-a"], "main")
    policy = empty_permissions()
    policy["branches"]["allow"]["read"] = ["refs/heads/main", "refs/heads/feature", "refs/heads/topic", "refs/heads/topic-with-commits"]
    policy["branches"]["allow"]["update"] = ["refs/heads/main", "refs/heads/feature"]
    if update_topic:
        policy["branches"]["allow"]["update"].append("refs/heads/topic")
    policy["branches"]["allow"]["create"] = ["refs/heads/topic", "refs/heads/topic-with-commits"]
    add_role(
        root, "alice-scoped-branches", empty_capability_map(),
        empty_capability_map(), permissions=policy,
    )
    bind_role(root, "alice-scoped-branches", "principal", "alice")
    try:
        transition_principal(root, "alice")
    except KeyError as error:
        # Keep the end-to-end cases executable as soon as the concurrently
        # landing WAL epoch_branches field is available. Do not mask any
        # unrelated materializer/protocol failure.
        if error.args == ("epoch_branches",):
            pytest.skip("scoped materializer requires WAL epoch_branches support")
        raise

    checkout = Path(tmp_path) / "scoped-client"
    subprocess.run(
        ["git", "clone", "-q", "--no-local", str(forge.view_repo(root, "alice")), str(checkout)],
        check=True,
    )
    for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.test")):
        subprocess.run(["git", "-C", str(checkout), "config", key, value], check=True)
    attach_intake_push(root, checkout, "alice")
    return SimpleNamespace(root=root, source=source, close=lambda: None), checkout


def _git(checkout, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(checkout), *args], check=check,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def test_real_receive_creates_from_current_advertised_feature_and_preserves_hidden_tree(tmp_path, monkeypatch):
    import forge
    fixture, checkout = _real_v4_receive(tmp_path)
    try:
        _git(checkout, "checkout", "-q", "feature")
        source_tip = _git(checkout, "rev-parse", "HEAD").stdout.strip()
        source_canonical_tip = Repository.open(fixture.root).load()[0].canonical_refs["refs/heads/feature"]
        _git(checkout, "push", "origin", "HEAD:refs/heads/topic")

        manifest, _version, replay = Repository.open(fixture.root).load()
        event = next(
            record for record in reversed(replay.records)
            if record["kind"] == "branch_submission"
        )
        payload = event["payload"]
        assert payload["source_ref"] == "refs/heads/feature"
        assert payload["canonical_old"] == source_canonical_tip
        assert payload["canonical_ref"] == "refs/heads/topic"
        assert payload["receive_old"] == ZERO_OID
        assert payload["projected_old"] == source_tip
        assert payload["canonical_new"] == manifest.canonical_refs["refs/heads/topic"]
        canonical = forge.canonical_repo(fixture.root)
        hidden = _git(checkout, "rev-parse", "refs/remotes/origin/feature:hidden/secret.txt", check=False)
        assert hidden.returncode != 0  # hidden objects were never sent to the client
        hidden_oid = _git(fixture.source, "rev-parse", "HEAD:hidden/secret.txt").stdout.strip()
        assert _git(canonical, "cat-file", "-e", hidden_oid, check=False).returncode == 0
        # Simulate a dropped success response and exact, unkeyed client retry.
        from protocol import pre_receive
        assert ("alice", "refs/heads/topic", ZERO_OID, payload["projected_new"]) in replay.requests
        monkeypatch.setenv("BASTION_VIEW", "alice")
        monkeypatch.setenv("BASTION_PRINCIPAL_ID", "alice")
        monkeypatch.setenv("BASTION_TRANSPORT", "test")
        head_before = manifest.head_seq
        pre_receive(fixture.root, "alice", f"{ZERO_OID} {payload['projected_new']} refs/heads/topic\n".encode())
        assert Repository.open(fixture.root).load()[0].head_seq == head_before
        assert "hidden/secret.txt" in _git(canonical, "ls-tree", "-r", "--name-only", payload["canonical_new"]).stdout
    finally:
        fixture.close()


def test_real_receive_allows_pure_fork_without_revalidating_inherited_metadata(tmp_path):
    fixture, checkout = _real_v4_receive(tmp_path)
    try:
        source_tip = _git(checkout, "rev-parse", "refs/remotes/origin/feature").stdout.strip()
        _git(checkout, "push", "origin", "refs/remotes/origin/feature:refs/heads/topic")
        _manifest, _version, replay = Repository.open(fixture.root).load()
        event = next(record for record in reversed(replay.records) if record["kind"] == "branch_submission")
        payload = event["payload"]
        assert payload["projected_old"] == source_tip
        assert payload["projected_new"] == source_tip
        assert not payload["mappings"]
        assert payload["canonical_pack"] is None
        assert payload["submission_pack"] is None
    finally:
        fixture.close()


def test_real_receive_creates_branch_with_linear_commits_from_exact_visible_tip(tmp_path):
    fixture, checkout = _real_v4_receive(tmp_path)
    try:
        _git(checkout, "checkout", "-q", "feature")
        advertised_source = _git(checkout, "rev-parse", "HEAD").stdout.strip()
        (Path(checkout) / "team-a" / "created.txt").write_text("branch commit\n")
        _git(checkout, "add", "team-a/created.txt")
        _git(checkout, "commit", "-m", "create branch with commit")
        new_tip = _git(checkout, "rev-parse", "HEAD").stdout.strip()
        _git(checkout, "push", "origin", "HEAD:refs/heads/topic")

        manifest, _version, replay = Repository.open(fixture.root).load()
        event = next(record for record in reversed(replay.records) if record["kind"] == "branch_submission")
        payload = event["payload"]
        assert payload["source_ref"] == "refs/heads/feature"
        assert payload["projected_old"] == advertised_source
        assert payload["projected_new"] == new_tip
        assert len(payload["mappings"]) == 1
        assert manifest.canonical_refs["refs/heads/topic"] == payload["canonical_new"]
        from materialization import ensure_canonical_materialized
        canonical = ensure_canonical_materialized(fixture.root, manifest, Repository.open(fixture.root))
        canonical_tree = _git(
            canonical, "ls-tree", "-r", "--name-only",
            payload["canonical_new"],
        ).stdout.splitlines()
        assert "hidden/secret.txt" in canonical_tree
        assert "team-a/created.txt" in canonical_tree
    finally:
        fixture.close()


def test_read_create_without_update_allows_pure_fork_but_denies_new_commits(tmp_path):
    fixture, checkout = _real_v4_receive(tmp_path, update_topic=False)
    try:
        source_tip = _git(checkout, "rev-parse", "refs/remotes/origin/feature").stdout.strip()
        _git(checkout, "push", "origin", "refs/remotes/origin/feature:refs/heads/topic")
        manifest, _version, replay = Repository.open(fixture.root).load()
        pure = next(record for record in reversed(replay.records) if record["kind"] == "branch_submission")
        assert pure["payload"]["projected_new"] == source_tip
        assert not pure["payload"]["mappings"]

        _git(checkout, "checkout", "-q", "feature")
        (Path(checkout) / "team-a" / "new.txt").write_text("new commit\n")
        _git(checkout, "add", "team-a/new.txt")
        _git(checkout, "commit", "-m", "commit requires update")
        before = Repository.open(fixture.root).load()[0]
        canonical_pack_count = len(before.canonical_packs)
        rejected = _git(checkout, "push", "origin", "HEAD:refs/heads/topic-with-commits", check=False)
        assert rejected.returncode != 0
        after, _version, replay_after = Repository.open(fixture.root).load()
        assert after.head_seq == before.head_seq
        assert after.canonical_refs == before.canonical_refs
        assert len(after.canonical_packs) == canonical_pack_count
        assert len([r for r in replay_after.records if r["kind"] == "branch_submission"]) == 1
        assert manifest.canonical_refs["refs/heads/topic"] == after.canonical_refs["refs/heads/topic"]
    finally:
        fixture.close()


def test_real_receive_rejects_unreadable_multiref_and_delete_without_manifest_change(tmp_path):
    fixture, checkout = _real_v4_receive(tmp_path)
    try:
        before = Repository.open(fixture.root).load()[0]
        denied_target = _git(
            checkout, "push", "origin",
            "refs/remotes/origin/feature:refs/heads/not-readable", check=False,
        )
        assert denied_target.returncode != 0
        after_denied, _version, _replay = Repository.open(fixture.root).load()
        assert after_denied.head_seq == before.head_seq

        _git(checkout, "checkout", "-q", "feature")
        (Path(checkout) / "team-a" / "multi.txt").write_text("multi\n")
        _git(checkout, "add", "team-a/multi.txt")
        _git(checkout, "commit", "-m", "multi-ref update")
        multi = _git(
            checkout, "push", "origin",
            "HEAD:refs/heads/main", "HEAD:refs/heads/feature", check=False,
        )
        assert multi.returncode != 0
        after_multi, _version, _replay = Repository.open(fixture.root).load()
        assert after_multi.head_seq == before.head_seq

        deletion = _git(checkout, "push", "origin", ":refs/heads/feature", check=False)
        assert deletion.returncode != 0
        after_delete, _version, _replay = Repository.open(fixture.root).load()
        assert after_delete.head_seq == before.head_seq
    finally:
        fixture.close()




def test_real_receive_updates_existing_branch_after_creation(tmp_path, monkeypatch):
    fixture, checkout = _real_v4_receive(tmp_path)
    try:
        _git(checkout, "checkout", "-q", "feature")
        manifest_before_fork = Repository.open(fixture.root).load()[0]
        main_tip_before_fork = manifest_before_fork.canonical_refs["refs/heads/main"]
        feature_tip_before_fork = manifest_before_fork.canonical_refs["refs/heads/feature"]
        _git(checkout, "push", "origin", "HEAD:refs/heads/topic")
        _git(checkout, "fetch", str(intake_repo(fixture.root, "alice")),
             "refs/heads/topic:refs/remotes/origin/topic")
        _git(checkout, "checkout", "-q", "-B", "topic", "refs/remotes/origin/topic")
        (Path(checkout) / "team-a" / "received.txt").write_text("updated\n")
        _git(checkout, "add", "team-a/received.txt")
        _git(checkout, "commit", "-m", "update scoped branch")
        expected = _git(checkout, "rev-parse", "HEAD").stdout.strip()
        _git(
            checkout, "config", "remote.origin.receivepack",
            "env BASTION_VIEW=alice BASTION_PRINCIPAL_ID=alice "
            "BASTION_TRANSPORT=test BASTION_IDEMPOTENCY_KEY=topic-update git-receive-pack",
        )
        _git(checkout, "push", "origin", "HEAD:refs/heads/topic")
        manifest, _version, replay = Repository.open(fixture.root).load()
        assert manifest.canonical_refs["refs/heads/topic"] != ZERO_OID
        event = next(
            record for record in reversed(replay.records)
            if record["kind"] == "branch_submission"
            and record["payload"]["canonical_ref"] == "refs/heads/topic"
            and record["payload"]["projected_new"] == expected
        )
        assert event["payload"]["source_ref"] is None
        assert event["payload"]["receive_old"] == event["payload"]["projected_old"]
        assert manifest.canonical_refs["refs/heads/main"] == main_tip_before_fork
        assert manifest.canonical_refs["refs/heads/feature"] == feature_tip_before_fork
        from protocol import pre_receive
        monkeypatch.setenv("BASTION_VIEW", "alice")
        monkeypatch.setenv("BASTION_PRINCIPAL_ID", "alice")
        monkeypatch.setenv("BASTION_TRANSPORT", "test")
        monkeypatch.setenv("BASTION_IDEMPOTENCY_KEY", "topic-update")
        head_before = manifest.head_seq
        pre_receive(
            fixture.root, "alice",
            f"{event['payload']['receive_old']} {expected} refs/heads/topic\n".encode(),
        )
        assert Repository.open(fixture.root).load()[0].head_seq == head_before
        monkeypatch.setenv("BASTION_IDEMPOTENCY_KEY", "different-key")
        with pytest.raises(ForgeError, match="idempotency key"):
            pre_receive(
                fixture.root, "alice",
                f"{event['payload']['receive_old']} {expected} refs/heads/topic\n".encode(),
            )

        # A valid branch grant cannot broaden the path-write capabilities.
        (Path(checkout) / "elsewhere.txt").write_text("outside grant\n")
        _git(checkout, "add", "elsewhere.txt")
        _git(checkout, "commit", "-m", "outside path grant")
        before_rejection = Repository.open(fixture.root).load()[0].head_seq
        rejected = _git(checkout, "push", "origin", "HEAD:refs/heads/topic", check=False)
        assert rejected.returncode != 0
        assert Repository.open(fixture.root).load()[0].head_seq == before_rejection
    finally:
        fixture.close()
