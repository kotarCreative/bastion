"""Repository actions and explicit full Git branch-ref permissions."""

from collections.abc import Mapping

from errors import ForgeError
from git_refs import validate_branch_ref

REPOSITORY_ACTIONS = ("pr_view", "pr_create", "pr_merge")
BRANCH_ACTIONS = ("read", "create", "update", "delete")
# Bound durable rule counts and encoded selector size to keep policies cheap to
# validate and evaluate. Counts are enforced after union/deduplication too.
MAX_RULES = 1024
MAX_SELECTOR_BYTES = 255


def _keys(value, keys, subject):
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ForgeError(f"{subject} has invalid shape")


def validate_ref_selector(selector):
    """Validate one exact full ref or terminal namespace wildcard selector."""
    if not isinstance(selector, str):
        raise ForgeError("branch selector must be a string")
    try:
        encoded = selector.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise ForgeError("branch selector is not valid UTF-8") from error
    if len(encoded) > MAX_SELECTOR_BYTES:
        raise ForgeError("branch selector is too long")
    if selector.endswith("/*"):
        base = selector[:-2]
        if base == "refs/heads":
            return selector
        validate_branch_ref(base)
        return selector
    if "*" in selector:
        raise ForgeError("branch selector contains an unsupported wildcard")
    return validate_branch_ref(selector)


def _rules(value, subject, selector=False):
    if not isinstance(value, (list, tuple)):
        raise ForgeError(f"{subject} must be a string list")
    if len(value) > MAX_RULES:
        raise ForgeError(f"{subject} has too many rules")
    if not all(isinstance(item, str) for item in value):
        raise ForgeError(f"{subject} must contain only strings")
    normalized = []
    for item in value:
        if selector:
            normalized.append(validate_ref_selector(item))
        elif item not in REPOSITORY_ACTIONS:
            raise ForgeError(f"{subject} contains an unknown action")
        else:
            normalized.append(item)
    return sorted(set(normalized))


def empty_permissions():
    """Return a new, deny-all policy in canonical serialized shape."""
    return {
        "repository": {"allow": [], "deny": []},
        "branches": {
            polarity: {action: [] for action in BRANCH_ACTIONS}
            for polarity in ("allow", "deny")
        },
    }


def normalize_permissions(raw, subject="permissions"):
    """Validate a policy and return sorted, deduplicated ordinary dict/lists."""
    _keys(raw, ("repository", "branches"), subject)
    _keys(raw["repository"], ("allow", "deny"), f"{subject}.repository")
    _keys(raw["branches"], ("allow", "deny"), f"{subject}.branches")
    result = empty_permissions()
    for polarity in ("allow", "deny"):
        result["repository"][polarity] = _rules(
            raw["repository"][polarity], f"{subject}.repository.{polarity}"
        )
        _keys(
            raw["branches"][polarity],
            BRANCH_ACTIONS,
            f"{subject}.branches.{polarity}",
        )
        for action in BRANCH_ACTIONS:
            result["branches"][polarity][action] = _rules(
                raw["branches"][polarity][action],
                f"{subject}.branches.{polarity}.{action}",
                selector=True,
            )
    return result


def combine_permissions(policies):
    """Union independent policy rules without dropping ancestor selectors."""
    accumulated = empty_permissions()
    for policy in policies:
        normalized = normalize_permissions(policy)
        for polarity in ("allow", "deny"):
            accumulated["repository"][polarity].extend(
                normalized["repository"][polarity]
            )
            for action in BRANCH_ACTIONS:
                accumulated["branches"][polarity][action].extend(
                    normalized["branches"][polarity][action]
                )
    # Deduplicate before enforcing aggregate limits; duplicate grants across
    # roles must not consume the distinct-rule budget.
    for polarity in ("allow", "deny"):
        accumulated["repository"][polarity] = sorted(
            set(accumulated["repository"][polarity])
        )
        if len(accumulated["repository"][polarity]) > MAX_RULES:
            raise ForgeError("combined repository policy has too many rules")
        for action in BRANCH_ACTIONS:
            rules = accumulated["branches"][polarity][action]
            unique = sorted(set(rules))
            if len(unique) > MAX_RULES:
                raise ForgeError("combined branch policy has too many rules")
            accumulated["branches"][polarity][action] = unique
    return accumulated


def repository_allowed(policy, action):
    normalized = normalize_permissions(policy)
    if action not in REPOSITORY_ACTIONS:
        return False
    return (
        action in normalized["repository"]["allow"]
        and action not in normalized["repository"]["deny"]
    )


def branch_allowed(policy, action, ref):
    normalized = normalize_permissions(policy)
    if action not in BRANCH_ACTIONS:
        return False
    try:
        validate_branch_ref(ref)
    except ForgeError:
        return False

    matching = {"allow": [], "deny": []}
    for polarity in ("allow", "deny"):
        for selector in normalized["branches"][polarity][action]:
            if selector.endswith("/*"):
                base = selector[:-2]
                if ref.startswith(base + "/"):
                    matching[polarity].append((len(base), False))
            elif selector == ref:
                # Exact refs outrank a namespace rule at the same textual
                # prefix length.
                matching[polarity].append((len(selector), True))
    strongest_allow = max(matching["allow"], default=(-1, False))
    strongest_deny = max(matching["deny"], default=(-1, False))
    return strongest_allow[0] >= 0 and strongest_allow > strongest_deny


def descriptor_permissions(descriptor):
    """Extract policy from a descriptor already checked by the durable validator.

    This helper is not a signature, identity, or full descriptor validator. It
    only prevents accidental legacy-policy inheritance and validates the
    permissions subdocument before callers use it.
    """
    if not isinstance(descriptor, Mapping):
        raise ForgeError("descriptor must be an object")
    algorithm = descriptor.get("algorithm")
    if algorithm == "capabilities-v3":
        if "permissions" in descriptor:
            raise ForgeError("legacy descriptor cannot contain permissions")
        return empty_permissions()
    if algorithm != "capabilities-v4" or "permissions" not in descriptor:
        raise ForgeError("descriptor does not contain a supported permissions policy")
    return normalize_permissions(descriptor["permissions"], "descriptor.permissions")


def transport_branch_allowed(descriptor, action, ref):
    """Evaluate branch transport access under the descriptor's policy version."""
    if not isinstance(descriptor, Mapping):
        return False
    if descriptor.get("algorithm") == "capabilities-v3":
        return action in ("read", "update") and ref == descriptor.get("canonical_ref")
    if descriptor.get("algorithm") != "capabilities-v4":
        return False
    try:
        return branch_allowed(descriptor_permissions(descriptor), action, ref)
    except ForgeError:
        return False


def authorized_branch_refs(descriptor, canonical_refs):
    """Return full refs visible to a descriptor, in deterministic order."""
    return sorted(
        ref for ref in canonical_refs
        if transport_branch_allowed(descriptor, "read", ref)
    )
