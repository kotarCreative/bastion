#!/usr/bin/env python3
"""Deterministic Git plumbing benchmark for path-authorized projections."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


POLICIES = {
    "A": ([""], ["arch/arm64"]),
    "B": (["drivers/net", "net", "include"], [""]),
    "C": (["fs", "mm", "include"], [""]),
    "D": (["arch/x86", "include"], [""]),
    "E": ([""], ["security", "crypto", "arch/arm64"]),
    "F": (["drivers/net", "drivers/usb", "drivers/gpu", "fs/ext4", "fs/xfs", "net/ipv4", "net/ipv6", "include/linux", "include/uapi", "arch/x86", "kernel", "lib", "block", "sound", "virt", "tools", "ipc", "init", "samples", "firmware"], [""]),
}


def run(args, *, env=None, input=None) -> bytes:
    return subprocess.run(args, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          check=True, env=env).stdout


def git(repo: Path, *args: str, env=None, input=None) -> bytes:
    return run(["git", "--git-dir", str(repo), *args], env=env, input=input)


@dataclass
class Policy:
    ident: str
    allow: list[str]
    deny: list[str]

    def status(self, path: str) -> int:
        """Return 1 if wholly allowed, -1 if wholly denied, 0 if intersected."""
        path = path.rstrip("/")
        rules = [(rule, True) for rule in self.allow] + [(rule, False) for rule in self.deny]
        # A rule below this tree can change access within it, so it cannot be reused.
        if any(rule != path and (path == "" or rule.startswith(path + "/")) for rule, _ in rules):
            return 0
        applicable = [(len(rule), allowed) for rule, allowed in rules if rule == "" or path == rule or path.startswith(rule + "/")]
        if not applicable:
            return -1
        longest = max(length for length, _ in applicable)
        # At equal specificity denial wins, matching the prototype policy semantics.
        return 1 if all(allowed for length, allowed in applicable if length == longest) else -1


class Projector:
    def __init__(self, canonical: Path, destination: Path, policy: Policy):
        self.canonical, self.destination, self.policy = canonical, destination, policy
        self.destination.mkdir(parents=True, exist_ok=True)
        git(destination, "init", "--bare", "--quiet")
        self.env = os.environ | {"GIT_OBJECT_DIRECTORY": str(destination / "objects"),
                                 "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(canonical / "objects")}
        self.trees: dict[str, list[tuple[str, str, str]]] = {}
        self.created_trees = 0
        self.boundaries = 0

    def entries(self, oid: str) -> list[tuple[str, str, str]]:
        if oid not in self.trees:
            raw = git(self.canonical, "ls-tree", "-z", oid).decode()
            self.trees[oid] = [(item.split(" ", 2)[0], item.split("\t", 1)[0].rsplit(" ", 1)[1], item.split("\t", 1)[1]) for item in raw.split("\0") if item]
        return self.trees[oid]

    def tree(self, oid: str, prefix: str = "") -> str | None:
        status = self.policy.status(prefix)
        if status == 1:
            return oid
        if status == -1:
            return None
        self.boundaries += 1
        kept = []
        for mode, child, name in self.entries(oid):
            child_path = f"{prefix}/{name}" if prefix else name
            if mode == "040000":
                result = self.tree(child, child_path)
                if result:
                    kept.append((mode, result, name))
            elif self.policy.status(child_path) == 1:
                kept.append((mode, child, name))
        if not kept:
            return git(self.destination, "mktree", env=self.env).decode().strip()
        payload = b"".join(f"{mode} {typ} {oid}\t{name}\0".encode() for mode, oid, name in kept for typ in ["tree" if mode == "040000" else "commit" if mode == "160000" else "blob"])
        result = git(self.destination, "mktree", "-z", env=self.env, input=payload).decode().strip()
        if result != oid:
            self.created_trees += 1
        return result

    def commit(self, canonical_oid: str, parents: list[str], tree: str, timestamp: str) -> str:
        env = self.env | {"GIT_AUTHOR_NAME": "Bastion Projection", "GIT_AUTHOR_EMAIL": "projection@localhost",
                          "GIT_COMMITTER_NAME": "Bastion Projection", "GIT_COMMITTER_EMAIL": "projection@localhost",
                          "GIT_AUTHOR_DATE": f"{timestamp} +0000", "GIT_COMMITTER_DATE": f"{timestamp} +0000"}
        args = ["commit-tree", tree]
        for parent in parents:
            args.extend(["-p", parent])
        return git(self.destination, *args, env=env, input=b"projected\n").decode().strip()


def commits(repo: Path, tip: str, count: int) -> list[str]:
    return git(repo, "rev-list", "--topo-order", "--reverse", "-n", str(count), tip).decode().splitlines()


def benchmark(canonical: Path, output: Path, policy: Policy, selected: list[str]) -> dict:
    destination = output / f"policy-{policy.ident}.git"
    if destination.exists(): shutil.rmtree(destination)
    projector = Projector(canonical, destination, policy)
    mapping, rows, timings = {}, [], []
    merges = placeholders = cuts = 0
    started = time.perf_counter()
    for oid in selected:
        started_commit = time.perf_counter()
        tree, *parents, timestamp = git(canonical, "show", "-s", "--format=%T%x00%P%x00%at", oid).decode().strip().split("\0")
        parent_oids = parents[0].split() if parents else []
        mapped = [mapping[p] for p in parent_oids if p in mapping]
        cuts += len(parent_oids) - len(mapped)
        projected_tree = projector.tree(tree) or git(destination, "mktree", env=projector.env).decode().strip()
        projected = projector.commit(oid, mapped, projected_tree, timestamp)
        placeholder = len(mapped) == 1 and projected_tree == rows[-1]["projected_tree_oid"]
        placeholders += placeholder
        merges += len(parent_oids) > 1
        mapping[oid] = projected
        rows.append({"canonical_oid": oid, "policy_id": policy.ident, "projected_oid": projected,
                     "projected_tree_oid": projected_tree, "placeholder_boolean": placeholder,
                     "canonical_parent_count": len(parent_oids), "projected_parent_count": len(mapped)})
        timings.append(time.perf_counter() - started_commit)
    git(destination, "update-ref", "refs/heads/main", mapping[selected[-1]], env=projector.env)
    reachable = git(destination, "rev-list", "--objects", "--all", env=projector.env).decode().splitlines()
    types = {"blob": [0, 0], "tree": [0, 0], "commit": [0, 0], "tag": [0, 0]}
    object_ids = [line.split()[0] for line in reachable]
    type_lines = git(destination, "cat-file", "--batch-check=%(objectname) %(objecttype)", env=projector.env,
                     input=("\n".join(object_ids) + "\n").encode()).decode().splitlines()
    reused_ids = []
    for line in type_lines:
        oid, typ = line.split()
        # Objects physically in the destination are new. Alternate-only objects are canonical reuse.
        own = (destination / "objects" / oid[:2] / oid[2:]).exists()
        types[typ][0 if own else 1] += 1
        if not own:
            reused_ids.append(oid)
    # Materialize only projected-reachable canonical objects in a separate shared pool.
    # The finished projection never points at the canonical object database.
    safe_pool = output / f"policy-{policy.ident}-safe.git"
    if safe_pool.exists(): shutil.rmtree(safe_pool)
    git(safe_pool, "init", "--bare", "--quiet")
    pack = run(["git", "--git-dir", str(canonical), "pack-objects", "--stdout", "--no-reuse-delta", "--no-reuse-object"], input=("\n".join(reused_ids) + "\n").encode())
    run(["git", "--git-dir", str(safe_pool), "index-pack", "--stdin", "--fix-thin"], input=pack)
    alternate = destination / "objects" / "info" / "alternates"
    alternate.parent.mkdir(parents=True, exist_ok=True)
    alternate.write_text(str(safe_pool / "objects") + "\n")
    secure_env = os.environ | {"GIT_OBJECT_DIRECTORY": str(destination / "objects")}
    git(destination, "fsck", "--full", env=secure_env)
    with (output / f"policy-{policy.ident}-mapping.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys()); writer.writeheader(); writer.writerows(rows)
    return {"policy": policy.ident, "commits": len(rows), "placeholders": placeholders, "placeholder_pct": placeholders / len(rows) * 100,
            "canonical_merges": merges, "projected_merges": sum(r["projected_parent_count"] > 1 for r in rows), "cut_parent_edges": cuts,
            "runtime_seconds": time.perf_counter() - started, "boundary_nodes": projector.boundaries, "new_trees_created": projector.created_trees,
            "object_counts": {key: {"new": value[0], "reused": value[1], "reuse_pct": value[1] / sum(value) * 100 if sum(value) else 100} for key, value in types.items()},
            "incremental_seconds": {"p50": statistics.median(timings), "p95": sorted(timings)[int(.95 * (len(timings)-1))], "p99": sorted(timings)[int(.99 * (len(timings)-1))], "max": max(timings)}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("canonical", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--commits", type=int, default=10_000)
    parser.add_argument("--policies", default="A,B,C,D,E,F")
    args = parser.parse_args()
    tip = git(args.canonical, "rev-parse", "HEAD").decode().strip()
    selected = commits(args.canonical, tip, args.commits)
    results = {"canonical_tip": tip, "window": len(selected), "strategy": "oldest selected commit is projection root; parent edges outside the window are recorded", "policies": []}
    args.output.mkdir(parents=True, exist_ok=True)
    for ident in args.policies.split(","):
        results["policies"].append(benchmark(args.canonical, args.output, Policy(ident, *POLICIES[ident]), selected))
    (args.output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__": main()
