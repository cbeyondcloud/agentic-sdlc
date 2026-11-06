# agentic

Governed AI agent pipelines for software teams. A pipeline of agents (business analyst,
design, developer, code review, QA) works on your repository, stops at gates where a
person decides, and ends with a pull request. Your org's policy decides which models and
tools each agent may use, and every step is recorded in a hash-chained audit trail.

agentic is provider-agnostic: every model call goes through your org's gateway, which
maps capability tiers to whatever models the org chooses. You never pick a provider.

## Start here, by role

| You are | Start with | Then |
|---|---|---|
| **A developer** | [Developer guide](guides/developer.md): install to first run in about 15 minutes | [15-minute walkthrough](training/walkthrough.md) on the sample repo, [command reference](guides/command-reference.md) |
| **A team lead** | [Team-lead guide](guides/team-lead.md): onboard your team and repositories | [Pilot rollout](runbooks/pilot-rollout.md), [kill switch and pause](runbooks/pilot-rollout.md#9-kill-switch-and-rollback) |
| **On the platform team, or an org admin** | [Admin guide](guides/admin.md): deploy, configure, operate | [Day-0 bootstrap](runbooks/day-0-bootstrap.md), [all runbooks](runbooks/README.md), [hosting this site](runbooks/docs-site.md) |
| **In security or compliance** | [Auditor guide](guides/auditor.md): verify and export the audit trail | [Retention](runbooks/retention.md), [call flow](architecture/call-flow.md) |
| **Changing org policy** | [Policy-repo guide](guides/policy-repo.md) | [Environments and promotion](runbooks/environments-and-promotion.md) |
| **Running the training** | [30-minute deck](training/deck.md) | [Office hours](training/office-hours.md), [facilitator sheet](training/facilitator-sheet.md) |

## First run, in five commands

```bash
agentic login --invite "<the link from your invite mail>"
cd ~/src/your-repo
agentic init
agentic pipelines list
agentic run feature-delivery --input request="Add CSV export" --dry-run
```

On Windows, run agentic inside WSL2. The [developer guide](guides/developer.md) has every
step, and `agentic onboarding` tells you how long yours took.

## Getting help

1. `agentic doctor` checks your machine, settings and services, with a fix for each
   problem.
2. `agentic support-bundle` writes a redacted zip for the platform team. Open it before you
   send it.
3. Ask your team lead, or come to [office hours](training/office-hours.md) during the pilot.

This site is built from the `docs/` folder of the agentic repository; the pages there are
the single source. It loads nothing from the internet.
