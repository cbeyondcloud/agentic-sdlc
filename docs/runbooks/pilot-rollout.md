# Pilot rollout runbook (M3-20)

> **Status: draft, waiting for owner review.** The owner set the cost and quality targets
> on 2026-10-30 (team budget 20 USD a developer a month, cost per accepted change under
> 3 USD, auto-pause at 5 USD an hour, and the quality targets below). Targets still marked
> *proposed* are the platform team's. **Every target is reviewed at the first go/no-go.**

**For:** the pilot lead (platform team), the team leads of the pilot teams, and the owner,
who takes the go/no-go. **Adapted from** Zyroflo's pilot plan (`docs/pilot-plan.md` in
sdlc-harness): it decides how often, who acts and what each number has to be.

The rollout goes in three waves (master plan, "Rollout to 1000 developers"):

| Wave | Who | Size | Length | Exit criteria (summary) |
|---|---|---|---|---|
| Pilot | 2-3 volunteer teams, Python/TypeScript repos | ~20 devs | 8 weeks | acceptance scenarios pass on their repos; no P1 incident for 2 weeks; ROI baseline captured |
| Early adopters | 10 teams across stacks | ~100 devs | 6-8 weeks | gate rejection rate and cost per accepted change within targets; onboarding 15 min or less (median) |
| General availability | the remaining teams, team by team | ~900 devs | team by team | load and support metrics healthy; training material complete |

The detail of each exit is in [Exit criteria per wave](#exit-criteria-per-wave).

---

## 1. Roles

| Role | Who | Does |
|---|---|---|
| Owner | the framework owner | takes each wave's go/no-go; approves target changes |
| Pilot lead | one platform engineer | runs this runbook; writes the weekly report; runs the kill switch drill |
| Platform on-call | platform team rota | answers incidents and support bundles during working hours |
| Team lead | one per pilot team | picks the repos, approves gates, holds the team's budget and kill switch, runs the weekly team retro |
| Champion | one developer per team | first line for teammates' questions; collects feedback |
| Developers | everyone in the pilot teams | run pipelines on real work, reject what is wrong with a note, report problems |

## 2. Choosing the cohort

Pick **2-3 teams, about 20 developers**, who volunteer. A good pilot team has:

- repos in **Python or TypeScript** with a CI that runs real tests (the agents prove their
  work through the tests; a repo without them gives the QA agent nothing to measure);
- a **mix of Windows and macOS laptops** (at least a third of each across the cohort), so
  both platforms are proven before 100 people depend on them;
- repos that are **`internal`** (the default class). Keep `confidential` and `restricted`
  repos for the early-adopter wave, once the pilot has shown the controls work;
- a team lead with time for gates (about 30 minutes a day in the first two weeks) and a
  champion who likes trying new tools;
- **no hard deadline in the next 8 weeks**: the pilot will cost them some speed at first.

Write the cohort down (teams, leads, champions, repos, OS split) in the pilot's tracking
issue before week 0.

## 3. Before the pilot (two weeks before week 0)

The platform team works through this list. Each item points at the runbook that does it.

| # | Check | How | Done when |
|---|---|---|---|
| 1 | Staging and production installed | `docs/runbooks/upgrade.md`, the Helm chart README | `readyz` green on both; staging smoke run passed |
| 2 | Policy repo set up from `templates/policy-repo` | its README (day 0 steps) | `agentic policy check .` passes; branch protection on |
| 3 | One `policy/teams/<team>.yaml` per pilot team | copy the example | `monthly_budget_usd` = 20 USD a developer a month, for the team (an org admin sets it; the lead may lower it); `auto_trip` set (below) |
| 4 | People invited, roles assigned | bulk import in files of **500 rows or fewer** (each file holds the tenant's role lock for about 9 s; DF-569) | every pilot developer has redeemed an invite |
| 5 | SMTP relay set | Helm `smtp.*` | a test invite arrives; a budget alert reaches a lead |
| 6 | CLI on the internal package index | `docs/runbooks/release.md` | `agentic --version` gives the pinned release on a clean Windows and a clean macOS laptop |
| 7 | Gateway (TokenLean LITE) reachable, optimisations off | the LITE profile | a smoke call is metered under the right team |
| 8 | Dashboards provisioned and shared | the Helm chart provisions the dashboards and the separate executive folder | each lead sees their Team view; the owner sees Executive ROI |
| 9 | **Kill switch drill** | section 9 below, on staging | a team switch stops a running run within the 30 s kill target, and after a pause, clearing it lets `agentic resume` continue |
| 10 | **ROI baseline** captured | section 7 below | two weeks of pre-pilot numbers written in the tracking issue |

Suggested auto-trip for each pilot team (the control plane pauses the team when it trips;
a person clears the switch). A team may only tighten the org's thresholds: once
`org.yaml` sets a lower `auto_trip`, a higher team value fails `agentic policy check`.

```yaml
# policy/teams/<team>.yaml
auto_trip:
  spend_usd_per_hour: 5         # owner, 2026-10-30: about one Developer run an hour at its cap
  error_rate: 0.5               # half the finished steps in the window ended in error
```

## 4. Training

| When | What | Who | Length |
|---|---|---|---|
| Week 0, day 1 | Kick-off: why, what the agents do and do not do, how gates work, how to stop a run | pilot lead, all pilot devs | 30 min (the M3-23 deck) |
| Week 0 | Onboarding: each developer installs and initialises unaided (`agentic init`, then `agentic doctor`) | each developer; the champion nearby | target 15 min or less, timed |
| Week 0 | Team-lead session: approving gates, budgets, the Team view, the kill switch | pilot lead, team leads | 45 min |
| Weeks 1-8 | Office hours, twice a week | platform team | 30 min each |
| Week 1 | First runs together: one bugfix pipeline per developer on a real, small issue | champion | 1 h |

Time each onboarding from "opens the guide" to "first `agentic doctor` all green" and write
it down: the median is an exit criterion of the next wave. Someone who gets stuck sends an
`agentic support-bundle` (redacted) to the platform channel; never a screenshot of a token.

## 5. Running the pilot, week by week

| Week | Focus | Team leads | Pilot lead |
|---|---|---|---|
| 0 | Onboard, first dry runs (`agentic run <pipeline> --dry-run`) | check everyone is onboarded | fix onboarding blockers the same day |
| 1-2 | Guided use: the **bugfix** pipeline on small real issues, then one **feature-delivery** per developer | approve or reject every gate with a note; daily 10-minute check of the Team view | daily look at Agent quality and Limits and safety; weekly report |
| 3-6 | Normal use on everyday work | weekly team retro (30 min); approve or reject learning proposals | weekly report; tune caps and budgets through policy-repo pull requests |
| 7-8 | Measure | run the acceptance scenarios on the team's own repos | assemble the go/no-go pack (section 10) |

Rules for the whole pilot:

- **Nothing merges without a person.** Agents open pull requests; people review and merge.
- **Reject with a note.** A rejected gate with a reason is how the learning loop learns
  (`agentic learn list`, `approve`, `reject`); a silent rejection teaches nothing.
- **Change policy only by pull request** to the policy repo, so every change is in the
  audit trail and the next run shows the new policy version.
- **No gateway optimisation is turned on during the pilot.** The opt-in procedure
  (`docs/runbooks/optimisation-opt-in.md`) runs after it, on the pilot's harvested cases.

## 6. The feedback loop

| Channel | Cadence | Owner | Output |
|---|---|---|---|
| Pilot chat channel | continuous | champions, platform on-call | questions answered within half a day; bugs filed |
| Gate rejections with notes | every run | developers, team leads | learning records (`agentic learn feedback`) |
| Team retro | weekly, 30 min | team lead | 3 things that worked, 3 that did not, written in the tracking issue |
| Pilot report | weekly | pilot lead | the metrics of section 7, the open issues, the decisions taken |
| Learning proposals | as they arrive | agent owners | approved or rejected within a week |

**Triage.** P1 issues are handled at once (section 8). Other bugs are triaged twice a week:
fix in the pilot, defer with a reason, or won't fix with a reason. A complaint that comes
up in two teams' retros is treated as a bug.

## 7. Metrics and dashboards

The numbers below are read weekly from the Grafana dashboards the control plane provisions.
**The pilot KPI is completion, the rest is reported**: a miss is a finding, not a verdict
(see section 10).

| What | Dashboard and panel | Watch for | Target |
|---|---|---|---|
| **Pilot KPI: person-started runs that reach a merged pull request** | Adoption: *Developers who ran a pipeline*; Run explorer; the git host's PR list | where runs stall (which step, which gate) | 40 in 8 weeks across the cohort (2 a developer) |
| Developers using it | Adoption: *Developers who ran a pipeline*, *Runs per day by team* | a developer with no run for two weeks | 80 % of the cohort active in weeks 3-8 (*proposed*) |
| Gate rejection rate | Agent quality: *Gate rejection rate by agent* | one agent above the others; a rise week on week | under 30 % per agent by week 6 |
| Correction rate after learning | Agent quality: *Correction rate per agent before and after each learning* | a learning that did not lower it | falls after each applied learning |
| Human wait at gates | Agent quality: *Human wait at gates* | runs waiting overnight | reported, never graded (a time cap rewards rubber stamps) |
| Cost | Team view: *Gateway cost*, *Cost by agent*, *Cost by user*; Org overview: *Run budgets* | a user or agent far above the rest; budgets near 80 % | each team within its monthly budget (20 USD a developer); cost per accepted change under 3 USD |
| ROI | Executive ROI: *ROI measures*, *Cost drivers by month*, *Cost per token by model* | rework rate, total cost, cost per run | baseline captured; rework rate falling |
| Limits and safety | Limits and safety: *Limit hits*, *Switch changes*, *Gateway safety events*, *Rate-limited calls* | any secret-scan block, any safety event, repeated limit hits | every safety event explained in the weekly report |
| Run health | Run explorer; stale-run mails | runs marked stale (no heartbeat) | under 2 % of runs |
| Support load | the pilot channel, support bundles | the same question three times | falling after week 2 |

**What the dashboards cannot show yet.** *Cost per accepted change*, *request to merged PR*
and *PR merge rate* stay empty on the Executive ROI dashboard until pull-request outcomes
reach the control plane (DF-785). Until then the pilot lead counts merged agent pull
requests from the git host each week and divides the team's gateway cost by them. The DORA
measures stay empty until a deployment is recorded: nulls with reasons, never zeros.

**The ROI baseline.** Before week 0, write down for each pilot repo, over the previous two
weeks: pull requests merged, median time from issue to merged PR, review rounds per PR, and
reverts. The same numbers at weeks 4 and 8 are the comparison. They come from the git host
and the issue tracker, not from the framework, so they exist before the pilot starts.

## 8. Incidents

| Severity | Examples | Response |
|---|---|---|
| **P1** | a secret or personal data sent to a model or published; code of one class sent to a tier the class does not allow; an agent change merged without a person; a team's spend more than twice its auto-trip rate for an hour; the control plane or gateway down for the cohort for more than an hour | stop the affected scope at once (section 9), page the platform on-call, tell the owner the same day, written review within 3 working days |
| P2 | a pipeline broken for one team; a wrong verdict at a gate that a person caught; repeated limit hits blocking work | fix or work around within 2 working days; in the weekly report |
| P3 | wording, slow steps, a confusing message | triaged twice a week |

The exit criterion "no P1 for 2 weeks" restarts its clock at each P1.

## 9. Kill switch and rollback

**Stop first, investigate second.** Team leads may stop their own team; org admins may stop
anything. Every switch is audited with its reason.

| To stop | Command | Who |
|---|---|---|
| one run | `agentic stop <run> --reason "..."`, in the repository where it runs | its starter (a lead stops someone else's run with a `user:` or `team:` switch) |
| a team's runs, now | `agentic killswitch on team:<slug> --reason "..."` | the team lead, an org admin |
| a team's runs, holding them to resume later | `agentic pause team:<slug> --reason "..."` | the team lead, an org admin |
| one agent version everywhere | `agentic killswitch on agent:developer@1.3.0 --reason "..."` | an org admin |
| one model or one backend | `agentic killswitch on model:<id>` / `backend:<name>` | an org admin |
| one repo or one person | `agentic killswitch on repo:<host/owner/name>` / `user:<email>` | an org admin |
| **everything** | `agentic killswitch on global --reason "..."` | an org admin |

`agentic killswitch list` shows the switches you may clear; `agentic killswitch off <id>
--reason "..."` clears one; `agentic resume --scope team:<slug>` clears a pause, and each held
run continues with `agentic resume <run>`. A killed run stays killed. Auto-trip (section 3)
pauses a team on its own when spend or errors pass the threshold.

Then roll back the cause, smallest step first:

| Cause | Roll back | Runbook |
|---|---|---|
| a new agent or pipeline version | pin the team to the previous version (`pins:` in `policy/teams/<team>.yaml`), or remove the `rollout.yaml` entry | policy-repo README, "Versions and rollout" |
| a policy change | revert the commit by pull request; the control plane activates the reverted version, and the next run shows it | policy-repo README |
| an applied learning | `agentic learn revert <id>` | CLI help |
| a gateway optimisation (none in the pilot) | remove its overlay | `docs/runbooks/optimisation-opt-in.md` |
| a CLI release | publish the previous release, raise or lower `min_cli_version` | `docs/runbooks/release.md` |
| a control-plane upgrade | Helm rollback | `docs/runbooks/upgrade.md` |
| the pilot itself | `killswitch on team:<slug>` for each pilot team, then remove their roles; developers keep working without agents | this runbook |

Run the drill (team switch on, a run stops, switch off, run resumes) on staging before week
0 and again before each wave.

## 10. Go/no-go

At the end of week 8 the pilot lead assembles the pack and the owner decides in a 45-minute
meeting with the team leads.

**The pack:** the exit criteria table below with each value and its evidence; where runs
stalled (Run explorer, by step and gate); the P1/P2 list; the weekly retros' themes; the
ROI baseline against week 8; the onboarding times; the open bugs and deferrals.

**If the KPI or a criterion is missed** (Zyroflo's rule): first read *where* runs stalled. A
pile at the requirements gate means runs are started and abandoned; a pile at review means
review costs more than it returns. Write that up as the finding, with the evidence, the
verdict and what is not claimed. Extending the pilot is a decision like any other: a written
reason, on the record.

| Decision | When |
|---|---|
| **Go** | every exit criterion met |
| **Go with conditions** | a criterion missed for a reason with a fix in hand; the conditions are named, owned and dated |
| **Extend** (once, up to 4 weeks) | the evidence is not there yet, and why is written down |
| **No-go** | a P1 without a fix, or the KPI far off with no explanation; the cohort stops (section 9) |

Record the decision, the date, who decided and the conditions in the tracking issue and in
the tracker (M3-26 for the pilot).

## Exit criteria per wave

| Wave | Criterion | Measured by | Target |
|---|---|---|---|
| Pilot | Acceptance scenarios (spec §1.3) pass on the pilot teams' own repos | the scenarios run by each team in week 7-8 | all pass |
| Pilot | No P1 incident | incident log | none in the last 2 weeks |
| Pilot | ROI baseline captured | section 7 | before week 0, and again at weeks 4 and 8 |
| Pilot | Pilot KPI | section 7 | 40 runs to a merged PR in 8 weeks |
| Pilot | Both platforms work | onboarding log, support bundles | at least 5 Windows and 5 macOS developers ran a full feature-delivery |
| Early adopters | Gate rejection rate | Agent quality | under 25 % per agent |
| Early adopters | Cost per accepted change | merged agent PRs and gateway cost (DF-785 until ingest) | under 3 USD median, and no higher than the pilot's |
| Early adopters | Onboarding time | timed onboarding | median 15 min or less (M3-23) |
| Early adopters | Stacks | cohort list | at least 4 stacks (for example Java, Go, .NET besides Python/TypeScript) |
| General availability | Control-plane load | the M3-22 targets in production: visible ingest lag p95 5 s or less (p99 15 s), errors 0.1 % or less, kill poll p99 under 2 s | for 4 weeks at early-adopter load, on the recommended 3 replicas x 1 vCPU |
| General availability | Support load | the support queue | falling per active developer for 4 weeks |
| General availability | Training material complete | docs site, deck, guides (M3-18, M3-23) | reviewed by the owner |

General availability then goes **team by team**: each team gets the training, its
`policy/teams/<team>.yaml` (budget and auto-trip) and an invite batch of at most 500 people
per import file, and its lead watches the Team view for the first two weeks.

## What this runbook does not claim

- **No pilot has run.** The targets are the owner's starting numbers (2026-10-30); the
  first go/no-go reviews every one of them against the pilot's evidence.
- **Completion is not value.** Runs that reach a merged PR show the lifecycle can be
  finished, not that it was worth its cost; the ROI comparison is where that is argued.
- **Load numbers are indicative.** M3-22 measured one laptop; production sizing is checked
  again during the early-adopter wave.
