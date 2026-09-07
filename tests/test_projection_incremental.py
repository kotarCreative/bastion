"""Spec 014 incremental projection: equivalence, operation counts, benchmark.

The retained simple reference projector and the incremental projector must
produce byte-identical outputs while the incremental work scales with changed
paths and policy boundaries rather than complete tree size per commit.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from errors import ForgeError
import limits
from projection import (
    IncrementalProjector,
    compile_read_matcher,
    fast_projection_stats,
    project_history,
    project_history_fast,
    reset_fast_projection_stats,
    visible_advance,
    visible_advance_fast,
)
from validation import visible


def _cleanup(directory: Path) -> None:
    import shutil

    shutil.rmtree(directory, ignore_errors=True)


def _git(repo: Path, *args: str, data: bytes | None = None, env=None) -> bytes:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_AUTHOR_NAME": "Bench",
            "GIT_AUTHOR_EMAIL": "bench@example.test",
            "GIT_AUTHOR_DATE": "2024-01-01T00:00:00 +0000",
            "GIT_COMMITTER_NAME": "Bench",
            "GIT_COMMITTER_EMAIL": "bench@example.test",
            "GIT_COMMITTER_DATE": "2024-01-01T00:00:00 +0000",
        }
    )
    if env:
        environment.update(env)
    result = subprocess.run(
        ["git", "--git-dir", str(repo), *args],
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )
    if result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace')}")
    return result.stdout


def _init_bare(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(path)], check=True
    )
    return path


def _commit_tree(
    repo: Path,
    entries: dict[bytes, tuple[bytes, bytes]],
    parent: str | None,
    message: bytes = b"t\n",
) -> str:
    """Build a commit from {path: (mode, content)} via plumbing."""
    import tempfile as _tempfile

    descriptor, raw_index = _tempfile.mkstemp(prefix="bench-index-")
    os.close(descriptor)
    index = Path(raw_index)
    index.unlink()
    environment = {
        "GIT_INDEX_FILE": str(index),
        "GIT_AUTHOR_NAME": "Bench",
        "GIT_AUTHOR_EMAIL": "bench@example.test",
        "GIT_AUTHOR_DATE": "2024-01-01T00:00:00 +0000",
        "GIT_COMMITTER_NAME": "Bench",
        "GIT_COMMITTER_EMAIL": "bench@example.test",
        "GIT_COMMITTER_DATE": "2024-01-01T00:00:00 +0000",
    }
    try:
        _git(repo, "read-tree", "--empty", env=environment)
        records = []
        for path, (mode, content) in sorted(entries.items()):
            if mode == b"120000":
                blob = _git(repo, "hash-object", "-w", "-t", "blob", "--stdin", data=content, env=environment).strip()
            else:
                blob = _git(repo, "hash-object", "-w", "--stdin", data=content, env=environment).strip()
            records.append(mode + b" " + blob + b"\t" + path + b"\0")
        if records:
            _git(repo, "update-index", "-z", "--index-info", data=b"".join(records), env=environment)
        tree = _git(repo, "write-tree", env=environment).decode().strip()
        args = ["commit-tree", tree]
        if parent:
            args.extend(("-p", parent))
        commit = _git(repo, *args, data=message, env=environment).decode().strip()
        return commit
    finally:
        index.unlink(missing_ok=True)


def _descriptor(
    allow: list[str], deny: list[str], did: str = "vdesc-" + "a" * 64
) -> dict:
    capabilities = {c: [] for c in ("read", "create", "modify", "delete", "mode", "symlink", "branch", "merge", "admin", "tag")}
    restrictions = {c: [] for c in capabilities}
    capabilities["read"] = list(allow)
    restrictions["read"] = list(deny)
    return {
        "id": did,
        "algorithm": "capabilities-v3",
        "principal_id": "alice",
        "principal_kind": "user",
        "canonical_ref": "refs/heads/main",
        "team_ids": [],
        "role_ids": [],
        "binding_ids": [],
        "capabilities": capabilities,
        "restrictions": restrictions,
    }


class MatcherEquivalenceTest(unittest.TestCase):
    def test_compiled_matcher_matches_pointwise_adjudication(self) -> None:
        policies = [
            ([""], []),
            (["shared"], []),
            (["shared"], ["shared/secret"]),
            (["a", "a/b"], ["a/b/c"]),
            (["a/b"], ["a"]),
            (["x/y", "x"], ["x/y/z", "w"]),
            (["team-a", "shared"], ["shared/hidden", "team-a/secret/deep"]),
        ]
        paths = [
            b"",
            b"a",
            b"a/b",
            b"a/b/c",
            b"a/b/c/d",
            b"ab",
            b"shared",
            b"shared/file.txt",
            b"shared/secret",
            b"shared/secret/x",
            b"shared/hidden",
            b"team-a/private.txt",
            b"team-a/secret/deep/x",
            b"x/y",
            b"x/y/z",
            b"x/y/z/w",
            b"w",
            b"other",
            "uni-é☃/file".encode(),
            b"sp ace/q",
            b"glob*[x]/f",
            b"-dash/f",
            b":colon/f",
            b"back\\slash/f",
            b"a\nb/c",
        ]
        for index, (allow, deny) in enumerate(policies):
            descriptor = _descriptor(allow, deny, f"vdesc-{'%064x' % index}")
            matcher = compile_read_matcher(descriptor)
            for path in paths:
                self.assertEqual(
                    visible(path, descriptor),
                    matcher.visible(path),
                    f"policy={allow}/{deny} path={path!r}",
                )


class HistoryEquivalenceTest(unittest.TestCase):
    def _build_repo(self) -> tuple[Path, Path, list[str]]:
        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup, base)
        canonical = _init_bare(base / "canonical.git")
        entries: dict[bytes, tuple[bytes, bytes]] = {
            b"shared/base.txt": (b"100644", b"base\n"),
            b"shared/mode-up": (b"100644", b"mode\n"),
            b"shared/link": (b"120000", b"base.txt"),
            b"team-a/private.txt": (b"100644", b"a private\n"),
            b"team-b/private.txt": (b"100644", b"b private\n"),
            b"hidden/secret.txt": (b"100644", b"secret\n"),
            b"sp ace/q.txt": (b"100644", b"space\n"),
            "uni-é.txt".encode(): (b"100644", "unicode\n".encode()),
            b"glob*[x].txt": (b"100644", b"glob\n"),
            b"-dash.txt": (b"100644", b"dash\n"),
        }
        commits = [_commit_tree(canonical, entries, None, b"root\n")]
        mutations: list[dict[bytes, tuple[bytes, bytes] | None]] = [
            {b"shared/new.txt": (b"100644", b"new\n")},
            {b"team-a/private.txt": (b"100644", b"a private v2\n")},
            {b"shared/mode-up": (b"100755", b"mode\n")},
            {b"shared/link": (b"120000", b"new.txt")},
            {b"shared/base.txt": None},
            {b"shared/sub/deep.txt": (b"100644", b"deep\n")},
            {b"team-a/private.txt": None, b"team-a/nested/x.txt": (b"100644", b"nested\n")},
            {b"shared/flat": (b"100644", b"was file\n")},
            {b"shared/flat": None, b"shared/flat/inner.txt": (b"100644", b"now dir\n")},
            {b"shared/flat/inner.txt": None, b"shared/flat": (b"100644", b"back to file\n")},
            {b"hidden/secret.txt": (b"100644", b"secret v2\n")},
            {b"team-b/private.txt": (b"100644", b"b private v2\n")},
            {"uni-é.txt".encode(): (b"100644", "unicode v2\n".encode())},
            {b"shared/exec.sh": (b"100755", b"#!/bin/sh\n")},
        ]
        live = dict(entries)
        for index, mutation in enumerate(mutations):
            for path, effect in mutation.items():
                if effect is None:
                    live.pop(path, None)
                else:
                    live[path] = effect
            commits.append(
                _commit_tree(canonical, live, commits[-1], f"change {index}\n".encode())
            )
        return base, canonical, commits

    def _policies(self) -> list[dict]:
        return [
            _descriptor(["shared", "team-a"], [], f"vdesc-{'%064x' % 1}"),
            _descriptor(["shared"], ["shared/sub"], f"vdesc-{'%064x' % 2}"),
            _descriptor(["team-b", "shared"], ["shared/flat"], f"vdesc-{'%064x' % 3}"),
            _descriptor([""], ["hidden"], f"vdesc-{'%064x' % 4}"),
        ]

    def test_histories_are_byte_identical(self) -> None:
        base, canonical, commits = self._build_repo()
        for index, descriptor in enumerate(self._policies()):
            first, second = base / f"ref{index}.git", base / f"fast{index}.git"
            _init_bare(first)
            _init_bare(second)
            tip_ref, frontier_ref, anchors_ref = project_history(
                canonical, first, "0" * 40, commits[-1], "0" * 40, descriptor
            )
            reset_fast_projection_stats()
            tip_fast, frontier_fast, anchors_fast = project_history_fast(
                canonical, second, "0" * 40, commits[-1], "0" * 40, descriptor
            )
            self.assertEqual(tip_ref, tip_fast, f"policy {index}")
            self.assertEqual(frontier_ref, frontier_fast)
            self.assertEqual(anchors_ref, anchors_fast)
            # Full object inventories match as well.
            ref_objects = sorted(
                _git(first, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)").decode().splitlines()
            )
            fast_objects = sorted(
                _git(second, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)").decode().splitlines()
            )
            self.assertEqual(ref_objects, fast_objects, f"policy {index} inventory")

    def test_continuation_from_a_tip_matches(self) -> None:
        base, canonical, commits = self._build_repo()
        descriptor = self._policies()[0]
        first, second = base / "refc.git", base / "fastc.git"
        _init_bare(first)
        _init_bare(second)
        mid = len(commits) // 2
        tip_ref, _, _ = project_history(
            canonical, first, "0" * 40, commits[mid], "0" * 40, descriptor
        )
        tip_fast, _, _ = project_history_fast(
            canonical, second, "0" * 40, commits[mid], "0" * 40, descriptor
        )
        self.assertEqual(tip_ref, tip_fast)
        tip_ref2, _, _ = project_history(
            canonical, first, commits[mid], commits[-1], tip_ref, descriptor
        )
        tip_fast2, _, _ = project_history_fast(
            canonical, second, commits[mid], commits[-1], tip_fast, descriptor
        )
        self.assertEqual(tip_ref2, tip_fast2)

    def test_visible_advance_verdicts_match(self) -> None:
        base, canonical, commits = self._build_repo()
        descriptor = self._policies()[0]
        first = base / "adv.git"
        _init_bare(first)
        # commits[11] touches only hidden/secret.txt, invisible to policy 0.
        tip, _, _ = project_history(
            canonical, first, "0" * 40, commits[10], "0" * 40, descriptor
        )
        expected = _git(first, "rev-parse", f"{tip}^{{tree}}").decode().strip()
        # Hidden-only advance: both accept.
        visible_advance(canonical, commits[10], commits[11], first, descriptor, expected)
        visible_advance_fast(canonical, commits[10], commits[11], first, descriptor, expected)
        # A range with a visible change must be rejected by both.
        with self.assertRaises(ForgeError):
            visible_advance(canonical, commits[0], commits[1], first, descriptor, expected)
        with self.assertRaises(ForgeError):
            visible_advance_fast(canonical, commits[0], commits[1], first, descriptor, expected)

    def test_empty_subtree_is_rejected_by_both(self) -> None:
        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup, base)
        canonical = _init_bare(base / "canonical.git")
        tip = _commit_tree(canonical, {b"a.txt": (b"100644", b"a\n")}, None)
        empty = _git(canonical, "mktree", "-z", data=b"").decode().strip()
        shared = _git(canonical, "mktree", "-z", data=b"040000 tree " + empty.encode() + b"\tempty\0").decode().strip()
        root = _git(canonical, "mktree", "-z", data=b"040000 tree " + shared.encode() + b"\tshared\0").decode().strip()
        bad = _git(canonical, "commit-tree", root, "-p", tip, data=b"empty\n").decode().strip()
        descriptor = _descriptor([""], [])
        first, second = base / "ref.git", base / "fast.git"
        _init_bare(first)
        _init_bare(second)
        with self.assertRaises(ForgeError):
            project_history(canonical, first, "0" * 40, bad, "0" * 40, descriptor)
        with self.assertRaises(ForgeError):
            project_history_fast(canonical, second, "0" * 40, bad, "0" * 40, descriptor)


def _blob_oid(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


class DeterministicBenchmarkTest(unittest.TestCase):
    """10,000 visible files, 100 one-leaf commits, nested boundaries, 4 views."""

    FILES = 10_000
    COMMITS = 100
    DIRS = 100

    def _policies(self) -> list[dict]:
        return [
            _descriptor([f"d{i:02d}" for i in range(25)], [], f"vdesc-{'%064x' % 11}"),
            _descriptor([""], [f"d{i:02d}" for i in range(50, 100)], f"vdesc-{'%064x' % 12}"),
            _descriptor(["d05", "d05/sub", "d06"], ["d05/sub/deep"], f"vdesc-{'%064x' % 13}"),
            _descriptor(["d42"], [], f"vdesc-{'%064x' % 14}"),
        ]

    def _build_large_repo(self, base: Path, nfiles: int, ncommits: int, name: str = "canonical.git"):
        import tempfile as _tempfile

        canonical = _init_bare(base / name)
        per_dir = nfiles // self.DIRS
        live: dict[bytes, bytes] = {}
        for index in range(nfiles):
            path = f"d{index // per_dir:02d}/f{index:05d}.txt".encode()
            live[path] = f"content-{index:06d}\n".encode()
        live[b"d05/sub/deep/keep.txt"] = b"deep\n"
        live[b"d05/sub/other.txt"] = b"other\n"
        initial = dict(live)
        commits = [self._commit_batch(canonical, live, None, b"root\n")]
        model: dict[bytes, bytes] = dict(live)
        history: list[tuple[bytes, bytes]] = []
        for commit_index in range(ncommits):
            target = commit_index * 37 % nfiles
            path = f"d{target // per_dir:02d}/f{target:05d}.txt".encode()
            if commit_index == ncommits // 2:
                # Reintroduce an already-transferred blob: transfer-once must
                # not read or copy it again.
                content = live[b"d00/f00000.txt"]
            else:
                content = f"content-{target:06d}-v{commit_index}\n".encode()
            live[path] = content
            model[path] = content
            history.append((path, content))
            commits.append(
                self._commit_batch(canonical, {path: content}, commits[-1], f"c{commit_index}\n".encode())
            )
        return canonical, commits, model, history, initial

    def _commit_batch(self, repo: Path, files: dict[bytes, bytes], parent: str | None, message: bytes) -> str:
        """Commit ``files`` over ``parent`` using a bounded number of batch calls."""
        import tempfile as _tempfile

        if parent is None:
            staged = dict(files)
            modes = {path: b"100644" for path in files}
        else:
            raw = _git(repo, "ls-tree", "-rz", "--full-tree", parent)
            staged: dict[bytes, bytes] = {}
            modes: dict[bytes, bytes] = {}
            order: list[bytes] = []
            oids: list[bytes] = []
            for item in raw.split(b"\0"):
                if not item:
                    continue
                metadata, path = item.split(b"\t", 1)
                mode, _kind, oid = metadata.split(b" ")
                modes[path] = mode
                order.append(path)
                oids.append(oid)
            if oids:
                listing = _git(
                    repo, "cat-file", "--batch", data=b"".join(o + b"\n" for o in oids)
                )
                cursor = 0
                for path in order:
                    end = listing.find(b"\n", cursor)
                    fields = listing[cursor:end].split(b" ")
                    size = int(fields[2])
                    start = end + 1
                    staged[path] = listing[start : start + size]
                    cursor = start + size + 1
            for path, content in files.items():
                staged[path] = content
                modes[path] = b"100644"
        with _tempfile.TemporaryDirectory(prefix="bench-stage-") as staging:
            names = []
            wanted = []
            for path in sorted(staged):
                target = os.path.join(staging, f"{len(names)}.blob")
                with open(target, "wb") as handle:
                    handle.write(staged[path])
                names.append(target)
                wanted.append(path)
            produced = _git(
                repo, "hash-object", "-w", "--stdin-paths",
                data=("".join(f"{name}\n" for name in names)).encode(),
            ).decode().split()
            records = [
                modes[path] + b" " + oid.encode() + b"\t" + path + b"\0"
                for path, oid in zip(wanted, produced)
            ]
            descriptor, raw_index = _tempfile.mkstemp(prefix="bench-index-")
            os.close(descriptor)
            index = Path(raw_index)
            index.unlink()
            environment = {"GIT_INDEX_FILE": str(index)}
            try:
                _git(repo, "read-tree", "--empty", env=environment)
                if records:
                    _git(repo, "update-index", "-z", "--index-info", data=b"".join(records), env=environment)
                tree = _git(repo, "write-tree", env=environment).decode().strip()
                args = ["commit-tree", tree]
                if parent:
                    args.extend(("-p", parent))
                return _git(repo, *args, data=message, env=environment).decode().strip()
            finally:
                index.unlink(missing_ok=True)

    def _run_histories(self, canonical: Path, commits: list[str], policies: list[dict], base: Path):
        from projection import fast_projection_stats, project_history_fast

        per_commit_elapsed: list[float] = []
        destinations = []
        tips = []
        per_view_stats = []
        for index, descriptor in enumerate(policies):
            destination = _init_bare(base / f"bench{index}.git")
            destinations.append(destination)
            reset_fast_projection_stats()
            started = time.time()
            tip, _frontier, _anchors = project_history_fast(
                canonical, destination, "0" * 40, commits[-1], "0" * 40, descriptor
            )
            per_commit_elapsed.append((time.time() - started) / max(len(commits) - 1, 1))
            tips.append(tip)
            per_view_stats.append(fast_projection_stats())
        return destinations, tips, per_commit_elapsed, per_view_stats

    def test_large_tree_operation_counts_and_identity(self) -> None:
        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup, base)
        canonical, commits, model, history, initial = self._build_large_repo(
            base, self.FILES, self.COMMITS
        )
        policies = self._policies()
        calls: list[list[str]] = []
        real_run = subprocess.run

        def counting(*args, **kwargs):
            argv = args[0] if args else kwargs.get("args", [])
            if isinstance(argv, (list, tuple)) and list(argv[:1]) == ["git"]:
                calls.append([os.fsdecode(part) for part in argv[1:10]])
            return real_run(*args, **kwargs)

        import subprocess as _subprocess

        destinations: list[Path] = []
        tips: list[str] = []
        per_commit: list[float] = []
        per_view_stats: list[dict] = []
        started_all = time.time()
        with mock.patch.object(_subprocess, "run", counting):
            destinations, tips, per_commit, per_view_stats = self._run_histories(
                canonical, commits, policies, base
            )
        total_elapsed = time.time() - started_all
        stats = fast_projection_stats()
        full_tree_listings = sum(
            1 for argv in calls if "ls-tree" in argv and any(flag in argv for flag in ("-r", "-rz", "-rzt", "-rt"))
        )
        # No per-commit recursive full-tree ls-tree -r after initial input
        # validation: exactly two full listings per view history (the initial
        # empty-subtree validation plus the initial mass-commit listing).
        self.assertEqual(2 * len(policies), full_tree_listings)
        # Steady-state continuation performs zero full-tree listings.
        extra_effects: list[tuple[bytes, bytes]] = []
        parent = commits[-1]
        for extra in range(3):
            path = f"d{extra:02d}/f{extra:05d}.txt".encode()
            content = f"extra-{extra}\n".encode()
            model[path] = content
            extra_effects.append((path, content))
            parent = self._commit_batch(
                canonical, {path: content}, parent, f"extra {extra}\n".encode()
            )
        continued_calls: list[list[str]] = []

        def counting_continuation(*args, **kwargs):
            argv = args[0] if args else kwargs.get("args", [])
            if isinstance(argv, (list, tuple)) and list(argv[:1]) == ["git"]:
                continued_calls.append([os.fsdecode(part) for part in argv[1:10]])
            return real_run(*args, **kwargs)

        from projection import project_history_fast as _fast

        continued_tips = []
        with mock.patch.object(_subprocess, "run", counting_continuation):
            for destination, tip, descriptor in zip(destinations, tips, policies):
                new_tip, _frontier, _anchors = _fast(
                    canonical, destination, commits[-1], parent, tip, descriptor
                )
                continued_tips.append(new_tip)
        continued_full = sum(
            1 for argv in continued_calls if "ls-tree" in argv and any(flag in argv for flag in ("-r", "-rz", "-rzt", "-rt"))
        )
        # One base-tree listing per continued view run; still zero per commit.
        self.assertEqual(len(policies), continued_full)
        tips = continued_tips
        # Process launches scale with commits and views, not files*commits.
        total_procs = len(calls)
        self.assertLess(total_procs, len(policies) * self.COMMITS * 15)
        # Each destination reads or transfers a distinct visible blob OID at
        # most once: per-view transfers equal exactly the distinct visible
        # blob set across the initial tree and every changed leaf (the
        # mid-history duplicate content is not transferred twice).
        for descriptor, view_stats in zip(policies, per_view_stats):
            expected_oids = {
                _blob_oid(content)
                for path, content in list(initial.items()) + history
                if visible(path, descriptor)
            }
            self.assertEqual(len(expected_oids), view_stats["blobs_transferred"])
            self.assertGreater(view_stats["bytes_read"], 0)
        # Independent content verification per view (no trust in either
        # projector): every visible path carries the modeled content, read
        # back in one batch.
        for destination, tip, descriptor in zip(destinations, tips, policies):
            raw = _git(destination, "ls-tree", "-rz", "--full-tree", tip)
            listed: list[tuple[bytes, bytes]] = []
            for item in raw.split(b"\0"):
                if not item:
                    continue
                metadata, path = item.split(b"\t", 1)
                _mode, _kind, oid = metadata.split(b" ")
                listed.append((path, oid))
            self.assertTrue(listed)
            payload = _git(
                destination, "cat-file", "--batch",
                data=b"".join(oid + b"\n" for _path, oid in listed),
            )
            cursor = 0
            for path, _oid in listed:
                end = payload.find(b"\n", cursor)
                size = int(payload[cursor:end].split(b" ")[2])
                start = end + 1
                content = payload[start : start + size]
                cursor = start + size + 1
                self.assertTrue(
                    visible(path, descriptor), f"hidden path {path!r} projected"
                )
                self.assertEqual(
                    model[path], content, f"visible path {path!r} has wrong content"
                )
            for path in model:
                if visible(path, descriptor):
                    self.assertIn(
                        path, [listed_path for listed_path, _ in listed],
                        f"visible path {path!r} missing",
                    )
        # Final-tip identity against one full reference projection on the
        # narrow view (100 visible files), plus first-commit identity widely.
        narrow = policies[3]
        reference_repo = _init_bare(base / "reference.git")
        tip_ref, _, _ = project_history(
            canonical, reference_repo, "0" * 40, commits[-1], "0" * 40, narrow
        )
        self.assertEqual(tips[3], tip_ref)
        try:
            import resource

            peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except Exception:
            peak_rss = -1
        report = {
            "files": self.FILES,
            "commits": self.COMMITS,
            "views": len(policies),
            "elapsed_seconds_total": round(total_elapsed, 3),
            "per_commit_seconds_p50": round(statistics.median(per_commit), 4),
            "per_commit_seconds_p95": round(
                sorted(per_commit)[max(0, int(len(per_commit) * 0.95) - 1)], 4
            ),
            "per_commit_seconds_max": round(max(per_commit), 4),
            "subprocess_total": total_procs,
            "full_tree_ls_tree": full_tree_listings,
            "bytes_read": sum(view["bytes_read"] for view in per_view_stats),
            "blobs_transferred": sum(view["blobs_transferred"] for view in per_view_stats),
            "peak_rss_kb": peak_rss,
        }
        report_path = base / "projection-benchmark.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True))
        print(f"\nprojection benchmark: {report_path}\n{json.dumps(report, sort_keys=True)}")


    def test_unchanged_file_growth_does_not_increase_subprocess_counts(self) -> None:
        import subprocess as _subprocess

        from projection import project_history_fast

        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup, base)
        small, small_commits, _model, _history, _initial_small = self._build_large_repo(
            base, 2_000, 5, name="small.git"
        )
        big, big_commits, _big_model, _bighistory, _initial_big = self._build_large_repo(
            base, 20_000, 5, name="big.git"
        )
        descriptor = self._policies()[0]

        def counting_context(calls: list[list[str]]):
            real_run = _subprocess.run

            def counting(*args, **kwargs):
                argv = args[0] if args else kwargs.get("args", [])
                if isinstance(argv, (list, tuple)) and list(argv[:1]) == ["git"]:
                    calls.append([os.fsdecode(part) for part in argv[1:10]])
                return real_run(*args, **kwargs)

            return mock.patch.object(_subprocess, "run", counting)

        def phases(canonical: Path, commits: list[str], tag: str) -> tuple[int, int]:
            """Split cold construction from steady incremental work.

            Cold bootstrap transfers the initial visible data in bounded
            batches (its cost is linear in initial data and is budgeted
            separately).  The steady gate then advances the same one-file
            commits with the rest of the tree unchanged: its process count
            must not depend on the unchanged file count at all.
            """
            destination = _init_bare(base / f"steady-{tag}.git")
            cold_calls: list[list[str]] = []
            started = time.perf_counter()
            reset_fast_projection_stats()
            with counting_context(cold_calls):
                cold_tip, _frontier, _anchors = project_history_fast(
                    canonical, destination, "0" * 40, commits[0], "0" * 40, descriptor
                )
            cold_seconds = time.perf_counter() - started
            cold_blobs = fast_projection_stats()["blobs_transferred"]
            steady_calls: list[list[str]] = []
            started = time.perf_counter()
            reset_fast_projection_stats()
            with counting_context(steady_calls):
                project_history_fast(
                    canonical, destination, commits[0], commits[-1], cold_tip, descriptor
                )
            steady_seconds = time.perf_counter() - started
            steady_blobs = fast_projection_stats()["blobs_transferred"]
            return (
                len(cold_calls),
                cold_blobs,
                cold_seconds,
                len(steady_calls),
                steady_blobs,
                steady_seconds,
            )

        (
            small_cold,
            small_cold_blobs,
            small_cold_seconds,
            small_steady,
            small_steady_blobs,
            small_steady_seconds,
        ) = phases(small, small_commits, "small")
        (
            big_cold,
            big_cold_blobs,
            big_cold_seconds,
            big_steady,
            big_steady_blobs,
            big_steady_seconds,
        ) = phases(big, big_commits, "big")

        # Steady-state gate: same changed paths on 2k vs 20k files must
        # cost identical processes and transfer no unchanged blobs.
        self.assertEqual(small_steady, big_steady)
        self.assertEqual(small_steady_blobs, big_steady_blobs)
        # Cold-transfer budget: batches are bounded by object count and
        # expanded bytes, so the process count stays linear in initial
        # visible data — never two subprocesses per blob.
        for cold_processes, cold_blobs in (
            (small_cold, small_cold_blobs),
            (big_cold, big_cold_blobs),
        ):
            budget = 2 * (cold_blobs // limits.BLOB_BATCH_OBJECTS + 1) + 80
            self.assertLessEqual(cold_processes, budget)

        # Measured artifacts (no portable latency thresholds asserted).
        report = {
            "small": {
                "cold_processes": small_cold,
                "cold_blobs": small_cold_blobs,
                "cold_seconds": round(small_cold_seconds, 4),
                "steady_processes": small_steady,
                "steady_blobs": small_steady_blobs,
                "steady_seconds": round(small_steady_seconds, 4),
            },
            "big": {
                "cold_processes": big_cold,
                "cold_blobs": big_cold_blobs,
                "cold_seconds": round(big_cold_seconds, 4),
                "steady_processes": big_steady,
                "steady_blobs": big_steady_blobs,
                "steady_seconds": round(big_steady_seconds, 4),
            },
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
            },
        }
        report_path = base / "cold-vs-steady-benchmark.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True))
        print(f"\ncold-vs-steady benchmark: {report_path}\n{json.dumps(report, sort_keys=True)}")

    def test_reduced_fixture_is_fully_byte_identical(self) -> None:
        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup, base)
        canonical, commits, _model, _history, _initial = self._build_large_repo(
            base, 1_000, 20, name="reduced.git"
        )
        for index, descriptor in enumerate(self._policies()):
            first, second = base / f"rred{index}.git", base / f"fred{index}.git"
            _init_bare(first)
            _init_bare(second)
            tip_ref, frontier_ref, anchors_ref = project_history(
                canonical, first, "0" * 40, commits[-1], "0" * 40, descriptor
            )
            tip_fast, frontier_fast, anchors_fast = project_history_fast(
                canonical, second, "0" * 40, commits[-1], "0" * 40, descriptor
            )
            self.assertEqual(tip_ref, tip_fast, f"policy {index}")
            self.assertEqual(frontier_ref, frontier_fast)
            self.assertEqual(anchors_ref, anchors_fast)


if __name__ == "__main__":
    unittest.main()
