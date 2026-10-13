import subprocess
from pathlib import Path
from types import SimpleNamespace

from branch_projection import build_scoped_projection
from git import git, has_exact_object_closure


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def test_builder_exports_only_authorized_fork_refs_and_closure(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "config", "user.name", "Test")
    _git(work, "config", "user.email", "test@example.invalid")
    (work / "public").write_text("base\n")
    (work / "secret").write_text("private\n")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "base")
    canonical = tmp_path / "canonical.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(canonical)], check=True)
    base = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-qb", "feature")
    (work / "public").write_text("feature\n")
    _git(work, "commit", "-qam", "feature")
    fork_tip = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "main")
    (work / "secret").write_text("denied-only\n")
    _git(work, "commit", "-qam", "private main update")
    main_tip = _git(work, "rev-parse", "HEAD")
    subprocess.run(["git", "--git-dir", str(canonical), "fetch", "-q", str(work),
                    f"{main_tip}:refs/heads/main", f"{fork_tip}:refs/heads/feature"], check=True)

    projected_ref = "refs/heads/main"
    active = {"epoch_start_seq": 1, "projection_epoch": 1, "descriptor_id": "d",
        "epoch_branches": {"refs/heads/main": base, "refs/heads/feature": fork_tip}}
    start = {"kind": "view_create", "payload": {"canonical_frontier": base, "epoch_branches": {
        "refs/heads/main": base, "refs/heads/feature": fork_tip,
    }}}
    pure_fork = {"view_id": "v", "descriptor_id": "d", "projection_epoch": 1,
        "source_ref": "refs/heads/feature", "canonical_ref": "refs/heads/forked",
        "canonical_old": fork_tip, "canonical_frontier_old": fork_tip,
        "canonical_new": fork_tip, "projected_old": "0" * 40,
        "projected_new": "0" * 40, "mappings": [], "submission_pack": None}
    replay = SimpleNamespace(active_views={"v": active}, records=[start,
        {"kind": "branch_submission", "payload": pure_fork}])
    descriptor = {"algorithm": "capabilities-v4", "canonical_ref": projected_ref,
        "capabilities": {"read": [""], "update": [""]}, "restrictions": {"read": [], "update": []},
        "permissions": {"repository": {"allow": [], "deny": []}, "branches": {
                "allow": {"read": ["refs/heads/feature", "refs/heads/forked"], "create": [], "update": [], "delete": []},
            "deny": {"read": [], "create": [], "update": [], "delete": []}}}}
    destination = tmp_path / "generation.git"
    subprocess.run(["git", "init", "--bare", "-q", str(destination)], check=True)
    manifest = SimpleNamespace(canonical_refs={"refs/heads/main": main_tip, "refs/heads/feature": fork_tip,
        "refs/heads/forked": fork_tip})
    result = build_scoped_projection(tmp_path, "v", canonical, destination, manifest, replay, None, descriptor)

    assert set(result) == {"refs/heads/feature", "refs/heads/forked"}
    assert result["refs/heads/forked"] == result["refs/heads/feature"]
    assert git(destination, "show", f"{result['refs/heads/feature']}:public").stdout == b"feature\n"
    assert git(destination, "show", f"{result['refs/heads/feature']}:secret").stdout == b"private\n"
    assert git(destination, "cat-file", "-e", f"{main_tip}^{{commit}}", check=False).returncode != 0
    assert has_exact_object_closure(destination)
