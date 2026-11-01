# Team-lead guide

For team leads. You onboard your team, set up its repositories, narrow team policy,
decide team gates and learning proposals, and pause your team when needed. Onboarding a
team takes about an hour. Every command is in the [command reference](command-reference.md).

First set yourself up as a developer: follow the [developer guide](developer.md) up to
`agentic doctor`.

## What a team lead may do

| Task | You | Who else |
|---|---|---|
| Assign `developer` and `approver` in your team | yes | org admins: any role |
| Set up a repository (`agentic init --repo`) | yes | developers with the permission |
| Change your team's policy file and team agents, pipelines and skills | by policy-repo PR | org admins |
| Lower your team's monthly budget | by policy-repo PR | org admins change any value, up or down |
| Approve team-level gates and team-scope learning proposals | yes | org admins |
| Kill or pause your own team's runs | yes | org admins: any scope |
| Create a CI service account for your team | yes | org admins |

Repository maintainers come from each repository's CODEOWNERS for `.agentic/` and
`AGENTS.md`. They approve changes to that repository's shared agents, skills and memory.
Pipelines are changed only by team leads and org admins.

## 1. Add your people

```bash
agentic rbac assign alice@your-org.example developer --team payments
agentic rbac assign bob@your-org.example approver --team payments
agentic rbac assignments --team payments
```

A person new to the org gets an invite mail. The link works once and for 72 hours by
default. To send a new one (the old one stops working):

```bash
agentic rbac invite alice@your-org.example
```

If no invite arrives, ask an org admin to check `agentic rbac mail --status failed`.

## 2. Set up a repository

Run this from the repository root, on a clean checkout of the default branch:

```bash
agentic init --repo --team payments --pipeline feature-delivery
```

It never commits to the default branch. It puts these files on a new branch,
`agentic/init`, and opens a pull request:

| File | What it is |
|---|---|
| `.agentic/config.yaml` | the repository's classification, team, pipelines and components |
| `AGENTS.md` | a first draft of shared repository knowledge, only when the repo has none. Review and edit it |
| `.gitattributes` | your file, with an agentic block added |
| `.github/workflows/agentic-index-export.yml` | the nightly index workflow, only when org policy turns it on |

`--classification` sets the data class (default: the policy's). `--no-pr` only prepares
the branch. Merge the pull request; then each developer runs `agentic init`.

## 3. Narrow your team's policy

Your team's file is `policy/teams/<team>.yaml` in the org policy repo. You change it by
pull request; CODEOWNERS makes you its reviewer. A team can only narrow org policy:

| Key | What it does |
|---|---|
| `allowed_tiers` | the capability tiers your team may use |
| `monthly_budget_usd` | the team's monthly gateway spend cap. You may only lower it |
| `limits_max` | lower caps per agent type (budget, turns, minutes) |
| `deny` | commands and paths your agents may never use, on top of the org's |
| `skill_packs` | opt-in skill packs from the org catalog |
| `pins` | keep a version of an org agent or pipeline during a rollout |
| `auto_trip` | lower spend-rate or error-rate thresholds for the automatic pause |

Team agents, pipelines, pipeline overlays for one repository and team skills live in
`teams/<team>/` in the same repository. See the [policy-repo guide](policy-repo.md).
Run `agentic policy check` on your branch before you open the pull request.

## 4. Decide gates

```bash
agentic waiting                     # gates waiting in this repository's runs
agentic show <run>
agentic approve <run> --notes "Matches the agreed design"
agentic reject <run> --feedback "Reuse the existing queue"
```

The control plane mails a gate's approvers when a run waits. A gate with four-eyes needs
someone other than the run's starter.

## 5. Skills and learning proposals

```bash
agentic skills review --to team             # open skill submissions for your team
agentic skills approve <pr-number> --to team
agentic skills decline <pr-number> --to team --reason "Add an eval case for retries"
agentic learn list --status pending
agentic learn show <proposal>
agentic learn approve <proposal>            # team-scope proposals: a policy-repo PR
```

Rejected proposals are not proposed again without new evidence.

## 6. CI service account

A service account can only trigger runs. It never approves a gate. Give each pipeline its
own account: all jobs that share a token share one rate limit.

```bash
agentic rbac service-account payments-nightly --team payments --days 90
```

The token is shown once. Put it in your CI system's secrets as
`AGENTIC_CONTROL_PLANE_TOKEN`.

## 7. Pause or stop your team's runs

```bash
agentic pause team:payments --reason "Release freeze"         # runs hold after this turn
agentic resume --scope team:payments                          # end the pause
agentic killswitch on team:payments --reason "Runaway cost"   # stop now; runs end as killed
agentic killswitch list
agentic killswitch off <id> --reason "Fixed"
```

A held run continues with `agentic resume <run>` once its pause is cleared. A killed run
stays killed. The control plane may also pause your team on its own when its spend or
error rate passes the auto-trip threshold; you get a mail.

## 8. Weekly check

You get a mail when your team reaches 80 % and 100 % of its monthly budget. Open the
team dashboard in Grafana: cost, limit hits and gate rejection rates. Adjust
team policy by pull request. Ask your org admin for the monthly chargeback file
(`agentic report chargeback --team <team>`).

See also: [pilot rollout runbook](../runbooks/pilot-rollout.md) and the
[runbooks index](../runbooks/README.md).
