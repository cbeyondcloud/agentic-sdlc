# Admin guide

For the platform team, who deploy and run the system, and for org admins, who own the
policy, the roles and the kill switch. Every command is in the
[command reference](command-reference.md). How a run flows through the system is in
[call flow](../architecture/call-flow.md).

## What runs where

| Part | Where | Who runs it |
|---|---|---|
| `agentic` CLI and the agents | each developer's laptop (macOS, Linux, WSL2) and CI | developers |
| Gateway (TokenLean) | Kubernetes | platform team. Every model call goes through it; it holds the provider credentials, routing and spend caps |
| Control plane (`agentic_cp`): API and worker | Kubernetes, umbrella Helm chart `deploy/helm/agentic-platform` | platform team |
| Postgres | managed service or in-cluster operator | platform team |
| Grafana and Prometheus | your monitoring stack | platform team |
| Org policy repo | your git host | org admins |

To try the whole org stack on one machine, with no model calls, use `deploy/org-local`
(`up.ps1` on Windows, `up.sh` on macOS and Linux; see its README).

## Day 0: set up the platform (platform team, once)

Follow [day-0 bootstrap](../runbooks/day-0-bootstrap.md) for the full order. In short:

1. Install staging and production from the chart:
   [environments and promotion](../runbooks/environments-and-promotion.md) and
   `deploy/helm/agentic-platform/README.md`.
2. Set mail, invites and gateway keys: `publicUrl`, `smtp.*`, `tokenlean.url`,
   `tokenlean.adminUrl` and their secrets. Without `publicUrl` no invite is sent. See
   "Mail, invites and gateway keys" in `control-plane/README.md`.
3. Create the tenant and its first org admin. The token is printed once. Operator
   commands need the owner role, which the long-running pods do not have: run them in a
   one-off pod with the release's image and both DSNs ("First tenant" in the chart README).

   ```bash
   python -m agentic_cp admin tenant create --slug acme --name "Acme" --admin-email admin@acme.example
   ```

4. Create the org policy repo from `templates/policy-repo` and point the tenant at it:
   see the [policy-repo guide](policy-repo.md).
5. Publish the CLI to your internal package index: [release](../runbooks/release.md).
6. Set up backups and the restore drill: [Postgres backup and restore](../runbooks/postgres-backup-restore.md).

## Day 1: configure the org (org admins)

**Sign in.** The first org admin uses the token from `admin tenant create`:

```bash
agentic login --for control-plane --url https://agentic.acme.example
```

**Fill the policy repo.** Tiers to models, classifications, budgets, blocked licences,
the skill catalog, retention. See the [policy-repo guide](policy-repo.md).

**Assign roles.**

| Role | Give it to | Scope |
|---|---|---|
| `org-admin` | AI governance owners (2-3) | org |
| `team-lead` | each team's leads | `--team` |
| `developer`, `approver` | team members (team leads usually do this) | `--team` |
| `auditor` | security and compliance | org |
| `exec-viewer` | executives and finance (ROI and chargeback only) | org |
| `repo-maintainer` | never assigned: it comes from each repository's CODEOWNERS | repo |
| service accounts | `agentic rbac service-account`, one per CI pipeline | `--team` |

For many people at once, use a CSV file (UTF-8, header `email,role,team,handle`; the `;`
or tab separators Excel writes in some locales work too). It only adds, and it is safe to
run again:

```bash
agentic rbac import leads.csv            # shows what would change
agentic rbac import leads.csv --apply
```

Windows developers work in WSL2, which usually has no OS keyring. There `agentic login`
keeps keys in `~/.agentic/credentials.json` (readable only by the user, not encrypted)
and says so; `agentic doctor` shows it. To forbid that file, set
`credential_file_fallback: false` in `policy/org.yaml` (it applies once a laptop has the
policy, after `agentic init`) and `AGENTIC_CREDENTIAL_STORE=keyring` in the managed shell
profile (it applies from the first login); then give WSL2 a Secret Service, or the
`AGENTIC_*` variables. A file written before the policy reached a laptop is no longer
used: `agentic login` and `agentic init` warn about it and `agentic doctor` fails it, each
saying how to delete it.

Everyone new gets an invite mail. Check delivery:

```bash
agentic rbac mail --status failed
```

## Daily and weekly operations

| Task | How |
|---|---|
| Stop everything now | `agentic killswitch on global --reason "..."` |
| Hold everything after the current turn (maintenance) | `agentic pause global --reason "..."`, then `agentic resume --scope global` |
| Stop one model, backend, pipeline, agent, repo or user | `agentic killswitch on model:<id> --reason "..."` (scopes in the [command reference](command-reference.md#kill-switch-and-pause-org-mode)) |
| A person leaves | `agentic rbac offboard <email> --reason "..."`: tokens, invites and gateway keys revoked, running work stopped |
| A laptop is lost or a key leaked | `agentic rbac revoke-keys <email>`, then `agentic rbac invite <email>` |
| Undo an offboarding | `python -m agentic_cp admin user enable --tenant <slug> --email <email> --reason "..."`, then `agentic rbac invite` |
| A user lost their token | `python -m agentic_cp admin issue-token --tenant <slug> --email <email>` |
| A policy change | pull request to the policy repo, staging first: [environments and promotion](../runbooks/environments-and-promotion.md#policy-changes) |
| Roll out an agent or skill version team by team | `policy/rollout.yaml`: [environments and promotion](../runbooks/environments-and-promotion.md#agent-and-skill-rollouts) |
| Monthly chargeback | `agentic report chargeback --month 2026-10` (see below) |
| A developer's problem | ask for `agentic support-bundle`; see [CLI operations](../runbooks/cli-operations.md) |

Auto-trip pauses a team on its own when its spend or error rate passes the thresholds in
`policy/org.yaml` `auto_trip:`; see "Auto-trip" in `control-plane/README.md`.

## Per release (platform team)

1. Upgrade staging, then production: [upgrade](../runbooks/upgrade.md).
2. Publish the new CLI: [release](../runbooks/release.md).
3. After adoption, raise the minimum CLI version (`min_cli_version` in `policy/org.yaml`,
   or `minCliVersion` in Helm). Older CLIs then cannot start runs.

## Monitoring

| What | Where |
|---|---|
| Liveness and readiness | `GET /healthz`, `GET /readyz` (503 with a problem code when the database does not enforce tenant isolation) |
| Metrics | `GET /metrics` with `AGENTIC_CP_METRICS_TOKEN` |
| Alert rules | `deploy/prometheus/alerts.yml` |
| Dashboards | Grafana, provisioned by the chart. Restrict the *Agentic Executive* folder to `dashboard.view.org` holders: it shows each person's cost |
| Settings | `AGENTIC_CP_*` variables, listed in `control-plane/README.md` and `agentic_cp/settings.py` |

## Data and retention

- What is kept and for how long, legal holds, deleting a tenant: [retention](../runbooks/retention.md).
- The retention job: `python -m agentic_cp retention run --dry-run`, then without `--dry-run` (the chart runs it as a CronJob).
- Gateway optimisations, one group at a time: [optimisation opt-in](../runbooks/optimisation-opt-in.md).

## Chargeback and FinOps

`agentic report chargeback` writes one row per team for a month (default: the previous
month, UTC). Empty cells are unmeasured, never 0. Finance tools that read the FinOps
FOCUS format can map the columns like this:

| Chargeback column | FOCUS column |
|---|---|
| `month` | `BillingPeriodStart` (first day, 00:00 UTC) and `BillingPeriodEnd` (first day of the next month) |
| `team` | `SubAccountName`, or a `Tags` entry `team` |
| `cost_usd` | `BilledCost` and `EffectiveCost`, with `BillingCurrency` = `USD` |
| `attributed_usd`, `unattributed_usd` | `x_AttributedCost`, `x_UnattributedCost` |
| `tokens` | `ConsumedQuantity`, with `ConsumedUnit` = `Tokens` |
| `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` | `x_InputTokens`, `x_OutputTokens`, `x_CacheReadTokens`, `x_CacheWriteTokens` |
| `users`, `runs`, `step_executions`, `calls` | `x_Users`, `x_Runs`, `x_StepExecutions`, `x_Calls` |
| (constant) | `ChargeCategory` = `Usage`, `ServiceName` = `agentic` |

The column names are those of `agentic.orgapi.cost.CSV_COLUMNS`. A FOCUS-shaped export is
not built in; the `x_` columns are FOCUS custom columns.

## Runbooks

All runbooks are listed in the [runbooks index](../runbooks/README.md).
