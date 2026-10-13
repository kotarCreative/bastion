# Spec 019: Scoped repository and branch permissions

## Status and scope

This slice establishes descriptor compatibility and policy visibility. It does
not implement PR lifecycle APIs, public secondary-branch fetch or Git operations,
end-user branch operations, or a UI permission editor. Existing Git and browser
path authorization remains unchanged on the default branch. Secondary branches
remain internal/operator-only.

## Policy

Capabilities-v3 remains byte-for-byte compatible for roles without an explicit
permissions policy, preserving descriptor IDs. A role explicitly configured
with permissions opts into capabilities-v4. Its descriptor contains:

```json
{"repository":{"allow":["pr_view","pr_create","pr_merge"],"deny":[]},
 "branches":{"allow":{"read":[],"create":[],"update":[],"delete":[]},
             "deny":{"read":[],"create":[],"update":[],"delete":[]}}}
```

All actions default deny, including for administrators. Path `read` and `admin`,
owner status, and service-account ownership never imply repository/branch
permissions. Repository denies override any allow. For a branch action, exact
full Git refs and terminal namespace wildcards (`refs/heads/*`,
`refs/heads/feature/*`) are selectors. The most-specific matching selector wins;
deny wins ties. Branch selectors are evaluated as policy strings only and must
not query whether a branch exists. Branch `read` grants no path read authority.

## Serving and transitions

The server accepts and validates both descriptor algorithms at startup. The
authenticated self-session preserves the exact legacy envelope for v3 and adds
the requesting principal's normalized active captured `permissions` field only
for v4. The self permission-check route accepts one bounded, exact
repository or branch decision request and returns only `{allowed: boolean}`;
it performs no canonical lookup or branch discovery. Existing capabilities and
restrictions response fields are unchanged. Legacy v3 reports normalized
deny-all scoped permissions.

Catalog grants are dormant until a principal policy transition. Operators add a
role with `--repo-allow ACTION`, `--repo-deny ACTION`,
`--branch-allow ACTION=SELECTOR`, and `--branch-deny ACTION=SELECTOR` on
`add-role`; bind it to the existing principal, then invoke
`transition-principal`. A previously issued UI token observes the newly active
descriptor on its next request. Do not rewrite legacy descriptor payloads or
IDs.

## Follow-on integration

Next work must connect branch rules to authorization-safe branch projections
and Git advertisement/transport, preserving non-enumeration of unauthorized
refs and ensuring branch read does not bypass path filtering. Then implement
explicit PR lifecycle authorization using repository actions. Public branch
inventory, branch mutation, UI policy editing, and PR operations remain out of
scope until those slices are separately designed and tested.
