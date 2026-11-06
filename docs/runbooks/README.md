# Runbooks

Step-by-step procedures. The [guides](../index.md) say who does what;
these pages say exactly how.

| Runbook | For | When |
|---|---|---|
| [Day-0 bootstrap](day-0-bootstrap.md) | platform team | once: the first deployment, tenant and org admin |
| [Docs site hosting](docs-site.md) | platform team | once, then each release: build and host the docs site |
| [Environments and promotion](environments-and-promotion.md) | platform team, org admins | dev, staging and prod; policy changes and rollouts, staging first |
| [Upgrade](upgrade.md) | platform team | each framework release, server side; raising the minimum CLI version |
| [Release](release.md) | platform team | publishing a CLI and chart release; upgrading the fleet; rollback |
| [Postgres backup and restore](postgres-backup-restore.md) | platform team | backups, point-in-time recovery, the restore drill |
| [Retention](retention.md) | platform team, auditors | retention periods, the retention job, legal holds, deleting a tenant |
| [Optimisation opt-in](optimisation-opt-in.md) | platform team | turning on a gateway optimisation, one group at a time, after an A/B run |
| [CLI operations](cli-operations.md) | developers, platform team | `agentic gc`, `doctor`, `support-bundle`, sandbox resource limits |
| [Pilot rollout](pilot-rollout.md) | pilot lead, team leads | the pilot and rollout waves, success criteria, go/no-go (draft, waiting for owner review) |
| [Office hours](../training/office-hours.md) | pilot lead, platform team | the support sessions through the rollout waves |
| [M1-64 quality gate](m1-64-quality-gate.md) | framework maintainers | the live agent quality-gate run |

Related:

- [Call flow](../architecture/call-flow.md): every step a run takes, and where in the code.
- `control-plane/README.md`: control-plane settings, guarantees, health, metrics, alerts.
- `deploy/helm/agentic-platform/README.md`: the chart's values and the first tenant.
- `deploy/org-local/README.md`: the whole org stack on one machine.
