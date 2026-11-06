---
marp: true
paginate: true
title: agentic in 30 minutes
---

# agentic in 30 minutes

Governed AI agent pipelines for software teams

The pilot kick-off (week 0, day 1). For every pilot developer and team lead.

<!--
Presenter: the pilot lead. Slides are Marp Markdown: render them with
`npx @marp-team/marp-cli docs/training/deck.md` (MIT), or present this page as it is.
Each "Presenter" comment is a speaker note. Keep to the times below; questions go to the
last slot and to office hours.
-->

---

## Agenda (30 minutes)

| # | Section | Minutes |
|---|---|---|
| 1 | What agentic is | 3 |
| 2 | Roles: who approves what | 3 |
| 3 | The daily loop | 3 |
| 4 | Gates | 3 |
| 5 | Cost and budgets | 2 |
| 6 | The kill switch | 2 |
| 7 | Safety: sandbox and secrets | 3 |
| 8 | Personal skills | 2 |
| 9 | The learning loop | 2 |
| 10 | Live demo on the sample repo | 5 |
| 11 | Getting help, and questions | 2 |
| | **Total** | **30** |

---

## 1. What agentic is (3 min)

- A **pipeline of agents** works on your repository: business analyst, design, developer,
  code review, QA.
- It **stops at gates**, where a person decides, and **ends with a pull request**. Nothing
  merges without a person.
- **Your org's policy** decides which models and tools each agent may use. Every model call
  goes through the org's gateway: you never pick a provider.
- **Every step is recorded** in a hash-chained audit trail.

What it is not: an autopilot. It drafts, you decide.

<!--
Presenter: one sentence each. The point to land: the agents do the typing, people make
every decision that matters, and the org (not the laptop) holds the keys and the rules.
-->

---

## 2. Roles: who approves what (3 min)

| Role | Does |
|---|---|
| Developer | runs pipelines, answers gates on own runs where the pipeline allows, writes personal skills |
| Approver | approves the gates assigned to them, such as the final `review-approval` |
| Team lead | approves `requirements-approval` and `design-approval`, holds the team budget and kill switch, approves team learnings |
| Repo maintainer | approves shared repo agents, skills and `AGENTS.md` changes |
| Org admin | org policy, org-wide kill switch, model and agent rollouts |
| Auditor | reads and exports the audit trail |

**Shared things** (org, team, repo) change only through their owner. **Personal things**
(your skills and notes) are yours and affect only your runs.

<!--
Presenter: in `feature-delivery`, the requirements and design gates go to the team lead,
the review gate to an approver. Pipelines change only through org admins and team leads.
-->

---

## 3. The daily loop (3 min)

```bash
agentic run feature-delivery --input request="Add CSV export to the orders page"
agentic waiting              # gates waiting for a person
agentic show <run>           # the handoff and the changes since the last gate
agentic approve <run>        # or: reject --feedback "...", or: edit
agentic runs                 # my unfinished runs
agentic pr checks <run> --watch
```

Start from an issue: `agentic run feature-delivery --from-jira PROJ-123`.

Not sure what a run would do? Add `--dry-run`: it spends nothing.

<!--
Presenter: the loop is run, wait, decide, repeat, then review the pull request like any
other. `agentic status <run>` always prints the next commands for a paused run.
-->

---

## 4. Gates (3 min)

`feature-delivery` has three: `requirements-approval`, `design-approval`, `review-approval`.

| Answer | When |
|---|---|
| `agentic approve <run>` | it is right; the run continues |
| `agentic reject <run> --feedback "..."` | it is wrong; the agent tries again with your note |
| `agentic edit <run>` | it is nearly right; fix the handoff yourself, then it is approved |

The run adds gates of its own: a review or QA loop that still fails after its last round,
or a change too big to review in one batch.

**Always reject with a note.** A silent rejection teaches nothing.

<!--
Presenter: a paused run exits with code 4; that is normal, not an error.
-->

---

## 5. Cost and budgets (2 min)

- **Dry run first:** `--dry-run` shows the models, budgets and gates, and spends nothing.
- **Each run has a budget**; at the limit it pauses: `agentic resume <run> --extra-budget 5`.
- **Each team has a monthly budget** (pilot: 20 USD a developer a month), and an
  **auto-trip** pauses the team if spend runs too fast (pilot: 5 USD an hour).
- **See it:** `agentic cost <run>`; team leads read the Team view.

Target: under 3 USD for each accepted change.

<!--
Presenter: budgets are a safety net, not a scorecard. A run that hits its budget pauses
and asks; it never overspends silently.
-->

---

## 6. The kill switch (2 min)

**Stop first, investigate second.** Every switch is audited, with its reason.

```bash
agentic stop <run> --reason "not needed"                        # one run: its starter
agentic killswitch on team:<slug> --reason "..."                # a team: team lead
agentic pause team:<slug> --reason "..."                        # hold, to resume later
agentic killswitch on global --reason "..."                     # everything: org admin
```

A switch stops running runs within 30 seconds. `agentic resume <run>` continues a paused
run once the pause is cleared; a killed run stays killed.

<!--
Presenter: we drill this on staging before week 0. If something looks wrong, pull the
switch; nobody is blamed for a pause.
-->

---

## 7. Safety: sandbox and secrets (3 min)

- **Sandbox:** agent commands run in an OS sandbox, with network only to the hosts policy
  allows. On Windows that is why agentic runs inside **WSL2**.
- **Secret scan:** content is scanned for secrets before it leaves your laptop; a hit
  blocks the call.
- **Your keys** live in the OS keyring (WSL2: `~/.agentic/credentials.json`, readable only
  by you). They are bound to you, your team and your org.
- **Data classes:** each repository is `internal`, `confidential` or `restricted`, and
  policy decides which model tiers may see it. The pilot uses `internal` repos only.
- **Never** paste a token into chat or a screenshot. `agentic support-bundle` is redacted.

<!--
Presenter: if the sandbox is missing, `agentic init` and `agentic doctor` say what to
install. A secret-scan block is a safety event: report it, do not work around it.
-->

---

## 8. Personal skills (2 min)

A skill is a short instruction file an agent loads, in plain English.

```bash
agentic skills new my-test-style          # ~/.agentic/skills/my-test-style/SKILL.md
agentic skills validate
agentic skills eval my-test-style --agent developer
agentic skills submit my-test-style --to repo     # propose it to your team
agentic skills backup                     # and on a new laptop: agentic skills restore
```

A personal skill affects only your runs. Sharing it goes through the owner of the repo or
team.

<!--
Presenter: good first skills are conventions people repeat in review comments.
-->

---

## 9. The learning loop (2 min)

Your rejections, edits and pull request review comments become feedback. After repeated
similar corrections, agentic **proposes** a change: a skill, or a line in `AGENTS.md`.

```bash
agentic learn list
agentic learn show <proposal>
agentic learn approve <proposal>     # yours; a shared one goes to its owner
agentic learn revert <proposal>      # undo it
```

Nothing changes until its owner approves it. An approval first measures the change on its
regression case, and shows you both scores.

<!--
Presenter: this is why "reject with a note" matters. The note is the lesson.
-->

---

## 10. Live demo on the sample repo (5 min)

The [15-minute walkthrough](walkthrough.md), on the `invoices` training repository, where
the presenter ran `agentic login` and `agentic init` before the session:

1. `agentic pipelines list`
2. `agentic run feature-delivery --input request="..." --dry-run`
3. `agentic training-gateway` in a second terminal: recorded answers, nothing spent
4. the real run pauses at `requirements-approval`; `agentic show`, then `agentic approve`
5. it pauses at `design-approval`; `agentic stop <run> --reason "demo"`
6. `agentic onboarding`: how long onboarding took

<!--
Presenter: rehearse it the day before on the presenter laptop, then
`agentic onboarding --reset`. Have the walkthrough open in case of a network problem: the
dry run needs the control plane; the recorded run needs only `agentic training-gateway`,
never the org's model gateway.
-->

---

## 11. Getting help, and questions (2 min)

1. `agentic doctor`: checks your machine, settings and services, with a fix for each.
2. `agentic support-bundle`: a redacted zip for the platform team. Open it before you
   send it.
3. Your team's **champion**, then the pilot channel.
4. **Office hours**, twice a week during the pilot ([office hours](office-hours.md)).

**Today:** do the [walkthrough](walkthrough.md) on your own laptop, unaided, and tell us
your `agentic onboarding` time. The target is 15 minutes.

<!--
Presenter: collect the times on the facilitator sheet (facilitator-sheet.md).
-->
