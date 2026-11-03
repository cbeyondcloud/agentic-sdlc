# Policy-repo guide

For org admins and team leads who change the org policy repo, and for the reviewers of
those changes. The policy repo is where the org decides which models each capability tier
means, what each kind of agent may do, which pipelines exist, which skills are approved,
and who may change any of it.

The full setup steps and every file are in `templates/policy-repo/README.md`. This page
is the short version.

## How a change reaches laptops

1. You open a pull request. The `policy-check` workflow runs `agentic policy check` on it.
2. A code owner reviews it. Branch protection must require code-owner review and the
   `policy-check` status.
3. You merge. The control plane fetches the branch, validates it with the same code as the
   CLI, and activates it. A commit that does not validate is stored as rejected; the
   previous version stays active.
4. Laptops pick up the active version from the control plane (`agentic doctor` shows the
   age of a laptop's copy). Each run records the policy version it used.

Developers never edit policy locally: a locally edited policy is refused. Each
environment tracks its own branch, so a change goes to staging first:
[environments and promotion](../runbooks/environments-and-promotion.md#policy-changes).

## What lives where

| Path | What | Owner |
|---|---|---|
| `policy/tiers.yaml` | capability tiers to models. Replace every `change-me` placeholder | org admins |
| `policy/agent-types.yaml` | what each kind of agent may use, and its caps | org admins |
| `policy/classifications.yaml` | repository data classes. `restricted`: local models only, no personal skills or memory | org admins |
| `policy/tools.yaml` | tool catalog and the org deny list | org admins |
| `policy/limits.yaml` | concurrency, skill admission limits, local disk and gc defaults | org admins |
| `policy/licences.yaml` | blocked dependency licences, licences a skill may carry | org admins |
| `policy/org.yaml` | allowed repo patterns, required steps, sandbox, scans, `auto_trip`, `min_cli_version` | org admins |
| `policy/rollout.yaml` | agent and pipeline versions team by team, deprecations | org admins |
| `policy/eval.yaml`, `retention.yaml`, `prices.yaml` | quality-gate targets, retention periods, model prices | org admins |
| `policy/teams/<team>.yaml` | one team's narrowing (it can only narrow) | that team's leads, or org admins |
| `agents/`, `pipelines/` | org agents and pipelines (copy `library/` here) | org admins |
| `skills/catalog.yaml` | approved skills, pinned by hash | org admins, with security reviewing |
| `teams/<team>/` | a team's agents, pipelines, per-repo overlays and skills | that team's leads |
| `CODEOWNERS` | who reviews what; `policy/`, `skills/` and this file also name the security group (`@your-org/agentic-security`) as reviewers | org admins |

People's roles are assigned in the control plane, not here. Repository maintainers come
from each repository's own CODEOWNERS.

## Common changes

| Change | Edit | Notes |
|---|---|---|
| Point a tier at another model | `policy/tiers.yaml` | the model must be one the gateway serves. `agentic doctor` checks it on laptops |
| Allow a new repository host or org | `policy/org.yaml` `allowed_repo_patterns` | `init` refuses a repo that matches no pattern |
| Let a command keep an environment variable | `policy/org.yaml` `sandbox.pass_env` | see [user settings](../user-config.md) |
| Sandbox resource limits | `policy/org.yaml` `sandbox.limits` | see [CLI operations](../runbooks/cli-operations.md#sandbox-resource-limits-sandboxlimits) |
| Add a team | copy `policy/teams/payments.yaml` and `teams/payments/`, add two CODEOWNERS lines | then the team lead assigns people |
| A team's budget | `policy/teams/<team>.yaml` `monthly_budget_usd` | team leads may only lower it |
| Tailor an org pipeline for one repository | `teams/<team>/pipelines/overlays/<repo>.yaml` | cannot remove or weaken an org-required step or gate |
| Roll a new agent version out | `policy/rollout.yaml`, keep the old one under `agents/versions/` | team leads may pin a version |
| Approve a skill | `skills/catalog.yaml` (from `agentic skills approve --to org`) | admission checks run in `policy check` |
| Raise the minimum CLI version | `policy/org.yaml` `min_cli_version` | only after the release is adopted: [upgrade](../runbooks/upgrade.md#raise-the-minimum-cli-version) |
| Turn on the auto-pause | `policy/org.yaml` `auto_trip:` | see "Auto-trip" in `control-plane/README.md` |

## Check before you push

From your policy repo checkout:

```bash
agentic policy check .
```

Exit 0 means the commit would be accepted. Exit 3 lists each problem with its file and
rule. This is the same check the control plane runs.

## Point the control plane at the repo

The platform team runs this once per tenant (see the [admin guide](admin.md)):

```bash
python -m agentic_cp admin tenant set-policy-repo --tenant acme --repo https://git.acme.example/org/agentic-org-policy.git
```

A private repository needs the tenant's git credential file; see "Operating it" in
`control-plane/README.md`.
