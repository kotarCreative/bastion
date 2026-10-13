"""Decision-table tests for permission normalization and evaluation."""

import subprocess
from pathlib import Path

import pytest

from errors import ForgeError
from permissions import (
    BRANCH_ACTIONS,
    MAX_RULES,
    MAX_SELECTOR_BYTES,
    branch_allowed,
    combine_permissions,
    empty_permissions,
    normalize_permissions,
    repository_allowed,
    validate_ref_selector,
)


def policy():
    return empty_permissions()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra=[]),
        lambda value: value.pop("repository"),
        lambda value: value.update(repository=[]),
        lambda value: value["repository"].update(extra=[]),
        lambda value: value["repository"].pop("allow"),
        lambda value: value["branches"].update(extra={}),
        lambda value: value["branches"]["allow"].update(admin=[]),
        lambda value: value["branches"]["deny"].pop("delete"),
    ],
)
def test_rejects_malformed_roots_and_scope_keys(mutate):
    malformed = policy()
    mutate(malformed)
    with pytest.raises(ForgeError):
        normalize_permissions(malformed)


@pytest.mark.parametrize(
    "path,value",
    [
        (("repository", "allow"), "pr_view"),
        (("repository", "deny"), [None]),
        (("branches", "allow", "read"), "refs/heads/main"),
        (("branches", "deny", "update"), [1]),
        (("branches", "allow", "read"), ["refs/heads/main", 2]),
    ],
)
def test_rejects_wrong_containers_and_list_item_types(path, value):
    malformed = policy()
    target = malformed
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(ForgeError):
        normalize_permissions(malformed)


@pytest.mark.parametrize("action", ["read", "admin", "rootadmin", "pr_view", ""])
def test_repository_actions_are_explicit_and_unknown_actions_deny(action):
    configured = policy()
    configured["repository"]["allow"] = ["pr_create"]
    assert not repository_allowed(configured, action)


@pytest.mark.parametrize(
    "selector",
    [
        "refs/heads/feature/*/nested",
        "refs/heads/*feature*",
        "refs/heads/feature*",
        "refs/heads/feature/@{upstream}",
        "refs/heads/feature?",
        "refs/heads/feature[abc]",
        "refs/heads/feature\\name",
        "refs/heads/feature name",
        "refs/heads/feature\x00name",
        "refs/heads/feature\x7f",
        "refs/heads/feature\ud800",
        "refs/heads",
    ],
)
def test_rejects_invalid_selectors(selector):
    with pytest.raises(ForgeError):
        validate_ref_selector(selector)


@pytest.mark.parametrize(
    "selector",
    [
        "refs/heads/main",
        "refs/heads/feature/café",
        "refs/heads/*",
        "refs/heads/feature/*",
    ],
)
def test_accepts_full_refs_unicode_and_terminal_namespace_selectors(selector):
    assert validate_ref_selector(selector) == selector


def test_selector_byte_limit_boundary():
    prefix = "refs/heads/"
    at_limit = prefix + "a" * (MAX_SELECTOR_BYTES - len(prefix.encode("utf-8")))
    assert len(at_limit.encode("utf-8")) == MAX_SELECTOR_BYTES
    assert validate_ref_selector(at_limit) == at_limit
    with pytest.raises(ForgeError):
        validate_ref_selector(at_limit + "a")
    assert validate_ref_selector("refs/heads/" + "é" * 100)


@pytest.mark.parametrize(
    "selector,ref,expected",
    [
        ("refs/heads/*", "refs/heads/main", True),
        ("refs/heads/topic/*", "refs/heads/topic/child", True),
        ("refs/heads/topic/*", "refs/heads/topic", False),
        ("refs/heads/topic/*", "refs/heads/topic-lookalike/x", False),
    ],
)
def test_namespace_wildcards_match_descendants_only(selector, ref, expected):
    configured = policy()
    configured["branches"]["allow"]["read"] = [selector]
    assert branch_allowed(configured, "read", ref) is expected


def test_exact_allow_beats_namespace_deny_and_exact_deny_beats_namespace_allow():
    configured = policy()
    configured["branches"]["allow"]["read"] = ["refs/heads/topic/*"]
    configured["branches"]["deny"]["read"] = ["refs/heads/topic/secret"]
    assert not branch_allowed(configured, "read", "refs/heads/topic/secret")
    configured["branches"]["allow"]["read"].append("refs/heads/topic/secret")
    configured["branches"]["deny"]["read"] = ["refs/heads/topic/*"]
    assert branch_allowed(configured, "read", "refs/heads/topic/secret")


def test_equal_rules_across_roles_deny_and_policy_is_not_mutated():
    first = policy()
    second = policy()
    first["branches"]["allow"]["read"] = ["refs/heads/main"]
    second["branches"]["deny"]["read"] = ["refs/heads/main"]
    original_first = repr(first)
    combined = combine_permissions([first, second])
    assert not branch_allowed(combined, "read", "refs/heads/main")
    assert repr(first) == original_first


def test_independent_actions_have_no_implicit_grants():
    configured = policy()
    configured["repository"]["allow"] = ["pr_create"]
    configured["branches"]["allow"]["read"] = ["refs/heads/main"]
    assert not repository_allowed(configured, "pr_view")
    assert not repository_allowed(configured, "pr_merge")
    assert not branch_allowed(configured, "update", "refs/heads/main")
    assert not branch_allowed(configured, "admin", "refs/heads/main")


def test_normalization_sorts_deduplicates_and_retains_ancestors_without_mutation():
    configured = policy()
    configured["branches"]["allow"]["read"] = [
        "refs/heads/z/*",
        "refs/heads/a/child",
        "refs/heads/a/*",
        "refs/heads/a/*",
    ]
    before = repr(configured)
    normalized = normalize_permissions(configured)
    assert normalized["branches"]["allow"]["read"] == [
        "refs/heads/a/*",
        "refs/heads/a/child",
        "refs/heads/z/*",
    ]
    assert repr(configured) == before


def test_empty_policy_returns_independent_nested_containers():
    left = empty_permissions()
    right = empty_permissions()
    left["branches"]["allow"]["read"].append("refs/heads/main")
    left["repository"]["allow"].append("pr_view")
    assert right["branches"]["allow"]["read"] == []
    assert right["repository"]["allow"] == []


def test_combine_deduplicates_before_aggregate_limit_and_rejects_distinct_overflow():
    duplicate_policy = policy()
    duplicate_policy["branches"]["allow"]["read"] = ["refs/heads/*"] * MAX_RULES
    combined = combine_permissions([duplicate_policy, duplicate_policy])
    assert combined["branches"]["allow"]["read"] == ["refs/heads/*"]

    too_many = policy()
    too_many["branches"]["allow"]["read"] = [
        f"refs/heads/b{i:04d}" for i in range(MAX_RULES + 1)
    ]
    with pytest.raises(ForgeError):
        combine_permissions([too_many])


def test_raw_rule_count_over_limit_is_rejected():
    configured = policy()
    configured["repository"]["allow"] = ["pr_view"] * (MAX_RULES + 1)
    with pytest.raises(ForgeError):
        normalize_permissions(configured)


def test_frozen_mapping_and_tuple_policies_are_evaluable_and_bad_shape_raises():
    from types import MappingProxyType

    frozen = MappingProxyType(
        {
            "repository": MappingProxyType({"allow": ("pr_view",), "deny": ()}),
            "branches": MappingProxyType(
                {
                    polarity: MappingProxyType(
                        {
                            action: (("refs/heads/main",) if polarity == "allow" and action == "read" else ())
                            for action in BRANCH_ACTIONS
                        }
                    )
                    for polarity in ("allow", "deny")
                }
            ),
        }
    )
    assert repository_allowed(frozen, "pr_view")
    assert branch_allowed(frozen, "read", "refs/heads/main")
    malformed = MappingProxyType({"repository": {}, "branches": {}})
    with pytest.raises(ForgeError):
        repository_allowed(malformed, "unknown")


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("refs/heads/valid", True),
        ("refs/heads/café", True),
        ("refs/heads/with space", False),
        ("refs/heads/bad..name", False),
        ("refs/heads/.hidden", False),
        ("refs/heads/trailing.lock", False),
    ],
)
def test_full_ref_validation_tracks_git_check_ref_format(ref, expected):
    git = subprocess.run(
        ["git", "check-ref-format", ref],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    ours = True
    try:
        validate_ref_selector(ref)
    except ForgeError:
        ours = False
    assert ours is expected
    assert (git.returncode == 0) is expected


def test_terminal_wildcard_validation_matches_git_base_ref_rules():
    for selector in ("refs/heads/*", "refs/heads/team/*"):
        base = selector[:-2]
        if base == "refs/heads":
            assert validate_ref_selector(selector) == selector
        else:
            assert subprocess.run(
                ["git", "check-ref-format", base], check=False
            ).returncode == 0
            assert validate_ref_selector(selector) == selector
