# Office hours (M3-23)

**For:** the pilot lead, who runs this plan, and the platform engineers and champions who
staff it. **What it is:** a standing, drop-in call where anyone in the rollout brings a
problem or a question, with a person who can fix it in the room. It is part of the
[pilot rollout](../runbooks/pilot-rollout.md#4-training).

## Cadence through the waves

| Wave | Sessions | Length | Notes |
|---|---|---|---|
| Pilot, week 0 | daily, right after each onboarding session | 30 min | onboarding blockers are fixed the same day |
| Pilot, weeks 1-8 | twice a week (for example Tuesday and Thursday) | 30 min | one slot suits both Europe and the Americas where the cohort spans them |
| Early adopters (~100 developers) | twice a week, plus one session per new team in its first week | 30 min | champions from the pilot co-host |
| General availability | weekly, then fortnightly once the support queue falls for 4 weeks | 30 min | the pilot channel stays the first line |

Cancel a session only with a notice in the pilot channel a day ahead. A session with no
visitors still ends with the triage below.

## Who staffs it

| Seat | Who | Does |
|---|---|---|
| Host | the pilot lead (or a deputy on the platform team) | runs the queue, keeps time, writes the notes |
| Fixer | the platform on-call of the week | reads support bundles, changes policy by pull request, files bugs |
| Champion | one champion per pilot team, rotating | answers "how do I" questions, brings their team's open issues |
| Team lead | optional | joins for gate, budget or kill-switch questions |

At least one Windows (WSL2) and one macOS laptop are in every session, so a fix is tried
on both before anyone says it works.

## Intake template

Ask people to post this in the pilot channel before the session, or fill it in with them
at the start. Never paste a token, a key or an invite link: send an
`agentic support-bundle` (redacted; open it first) instead of a screenshot.

```text
Who:           name, team, Windows (WSL2) or macOS
What I ran:    the agentic command, exactly
What I expected:
What happened: the message, or the exit code
Run id:        if a run was involved (agentic runs)
agentic --version:
agentic doctor: the lines that are not ok
Support bundle: attached / not needed
Blocking me?   yes (cannot work) / no (annoying)
```

## From an issue to the tracker

Every issue raised in a session leaves the session as one of these, written in the pilot's
tracking issue the same day:

| Outcome | When | Where it goes |
|---|---|---|
| **Answered** | a how-to, already in the docs | a link to the page; if the answer was hard to find, a docs pull request |
| **FAQ** | the same question a second time | a new row below, by pull request |
| **Bug** | the CLI, the control plane or a pipeline does the wrong thing | a bug in the agentic repository, with the intake text and the support bundle; P1 goes to the [incident process](../runbooks/pilot-rollout.md#8-incidents) at once |
| **Tracker or DF item** | a gap that needs work later, or a known limit to accept | a row in `docs/superpowers/plans/DEFERRED.md` (DF-nn, with the target tracker row) or a new tracker row, in the same pull request that records it |
| **Policy change** | a cap, budget or allow-list is wrong for a team | a policy-repo pull request (staging first); never a local edit |
| **Won't fix** | out of scope | the reason, written down, so the next person gets the same answer |

The host reads the open items at the start of each session. A complaint that comes up in
two teams is a bug (the [triage rule](../runbooks/pilot-rollout.md#6-the-feedback-loop)).

## Session notes

One short entry per session in the tracking issue: date, who came, each issue and its
outcome, and the onboarding times collected that day (the
[facilitator sheet](facilitator-sheet.md)). The weekly pilot report counts them.

## FAQ seed

Seeded from the guides and runbooks; grow it from the sessions.

| Question | Answer |
|---|---|
| `agentic login --invite` says the link was already used | An invite works once. Ask your team lead for a new one (`agentic rbac invite`). |
| `keyring` is WARN in `agentic doctor` on WSL2 | WSL2 usually has no OS keyring, so keys are in `~/.agentic/credentials.json` (mode 0600, not encrypted). That is allowed unless your org forbids it; then it is FAIL and you need the org's WSL2 keyring setup ([developer guide](../guides/developer.md#before-you-start)). |
| Why must I use WSL2 on Windows? | Agent commands run in an OS sandbox, and native Windows has no supported one. Keep repositories inside WSL2 (`~/src`), not under `/mnt/c`. |
| `agentic init` says the repository is not set up | A team lead runs `agentic init --repo` once and merges its pull request ([team-lead guide](../guides/team-lead.md#2-set-up-a-repository)). |
| `agentic init` lists missing tools | `agentic init --install-missing` installs the user-space tools your org allows, after asking; add `~/.agentic/tools` to `PATH` with the line it prints. |
| My run exited with code 4 | It is paused at a gate, waiting for a person: `agentic status <run>` shows the next commands. |
| The run stopped at its budget | `agentic resume <run> --extra-budget 5`, within your team's budget. |
| An agent asks to run a tool that needs approval | `agentic resume <run> --allow <call>` or `--deny <call>`. |
| I closed the terminal in the middle of a run | `agentic resume <run>` continues it. |
| The gateway refused my key | `agentic login` (a new key) or a new invite, then `agentic resume <run>`. |
| The pull request was not opened | Sign in to the git host (`agentic login --for github`), then `agentic pr open <run>`. |
| My laptop is filling up | `agentic gc --dry-run`, then `agentic gc` ([CLI operations](../runbooks/cli-operations.md)). |
| What do I send the platform team? | `agentic support-bundle`, opened and checked first. Never a screenshot of a token. |
| How long did my onboarding take? | `agentic onboarding`. It is kept on your laptop only. |
| The training gateway says `cassette_exhausted` | It serves one walkthrough: Ctrl-C it, start it again, start a new run ([walkthrough](walkthrough.md#if-something-goes-wrong)). |
