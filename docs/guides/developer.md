# Developer guide

For developers on Windows or macOS. It takes you from install to your first run in about
15 minutes. Every command is in the [command reference](command-reference.md).

agentic runs a pipeline of AI agents on your repository: a business analyst, a design
agent, a developer, code review and QA. It stops at gates where a person decides, and it
ends with a pull request. Your org's policy decides which models and tools each agent may
use. You never pick a model provider; the org's gateway does.

## Before you start

You need:

| What | Why |
|---|---|
| An invite mail from your team lead | It signs you in. Ask your team lead if you have none. |
| git 2.40 or later | agentic works in git worktrees |
| [uv](https://docs.astral.sh/uv/) | installs the CLI in its own environment |
| Node.js 20.11 or later and the sandbox tools | agent commands run in an OS sandbox. `agentic init` names what is missing and how to install it |
| **Windows:** WSL2 with Ubuntu | Windows has no supported sandbox. Run agentic and keep your repositories inside WSL2 (`~/src`, not `/mnt/c`) |

**Windows: set up WSL2 first.** In PowerShell:

```powershell
wsl --install -d Ubuntu-24.04
```

Then open Ubuntu and do everything below in its bash shell. `agentic doctor` prints the
remaining sandbox steps, with the exact versions your org pins.

## First run (about 15 minutes)

**1. Install uv** (skip if `uv --version` works). macOS, Linux or WSL2:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**2. Install agentic** from your org's package index. Your platform team gives you the
index URL.

```bash
uv tool install agentic-cli --index-url https://pypi.your-org.internal/simple/
agentic --version
```

**3. Sign in** with the link from your invite mail. The link works once. Keep the quotes:
the link has a `#` in it.

```bash
agentic login --invite "https://agentic.your-org.internal/invite#..."
```

This stores your control-plane token and gateway key in the OS keyring. If it prints
`Add to ~/.agentic/config.yaml:` lines, add them to that file.

**4. Sign in to your git host**, so agentic can open pull requests (a token with
repository write access):

```bash
agentic login --for github
```

For a GitHub Enterprise or other host, declare it first: see
[user settings](../user-config.md#git-hosts-git_hosts).

**5. Set up the repository on your machine.** Run this from the repository root:

```bash
cd ~/src/your-repo
agentic init
```

`init` checks your machine, fetches the org policy and the approved skills, and prints a
readiness report. Fix each item it marks, then run it again. `agentic init
--install-missing` installs the user-space tools your org allows, after asking.

If `init` says the repository is not set up, ask your team lead to run
`agentic init --repo` (see the [team-lead guide](team-lead.md)).

**6. See what you can run:**

```bash
agentic pipelines list
agentic agents list
```

**7. Dry run.** It spends nothing. It shows the models, budgets, gates and who must
approve each gate.

```bash
agentic run feature-delivery --input request="Add CSV export to the orders page" --dry-run
```

**8. First real run:**

```bash
agentic run feature-delivery --input request="Add CSV export to the orders page"
```

The run stops at its first gate and exits with code 4 (paused, waiting for a person).
Go on to "Gates" below.

To start from a Jira issue instead, set up `jira:` once (see
[user settings](../user-config.md#jira-jira)), then:

```bash
agentic run feature-delivery --from-jira PROJ-123
```

## Gates

A gate is where a person decides. The library pipeline `feature-delivery` has three:
`requirements-approval`, `design-approval` and `review-approval`.

```bash
agentic waiting                   # every gate waiting for a person here
agentic show <run>                # the handoff and the code changes since the last gate
agentic approve <run>             # accept it; the run continues
agentic reject <run> --feedback "Use the existing export service"
agentic edit <run>                # fix the handoff yourself, then approve it
```

`agentic edit` opens `$VISUAL` or `$EDITOR` (Notepad on Windows). Your edit is checked
as a handoff before it is approved. If it is refused, run `agentic edit` again: your draft
is kept.

Some gates the run adds itself:

| Gate | Why the run waits | What to do |
|---|---|---|
| `<loop>-exhausted` (for example `build-review-exhausted`) | review or QA still fails after the loop's last round | `agentic edit` the handoff (accept a finding, or name an accepted failing test), `agentic reject --feedback` for another round, or `agentic stop`. A plain approve is refused until the loop's exit condition holds |
| `diff-budget` | a step changed more than one review batch, twice | `agentic approve` accepts it as one batch. `agentic reject --feedback` has it split. `agentic edit` changes the step's pinned handoff only, not the code. Or `agentic stop` |

Each paused run tells you its next commands in `agentic status <run>`.

## Other pauses

| Pause | Next command |
|---|---|
| The run reached its budget | `agentic resume <run> --extra-budget 5` |
| A step reached its own budget | `agentic resume <run>` retries the step with the same budget, or `agentic stop` |
| The run reached its time limit | `agentic resume <run> --extra-minutes 30` |
| An agent asks to run a tool that needs approval | `agentic resume <run> --allow <call>` or `--deny <call>` |
| A step condition could not be evaluated | `agentic resume <run> --skip` |
| Ctrl-C, a closed terminal or a crash | `agentic resume <run>` |
| Your team or org paused runs | wait; the run continues on `agentic resume <run>` once the pause is cleared |

## The pull request

The pipeline's last step opens the pull request. If it could not (for example your git
host token was missing), fix that and run:

```bash
agentic pr open <run>
agentic pr checks <run> --watch   # exit 0 only when CI is green
```

While the publish step runs, the PR body says `Status: running (when this was published;
agentic pr open refreshes it)`. `agentic pr open` again refreshes only the generated part.
Anything you write above or below it is kept.

## Daily commands

| Task | Command |
|---|---|
| My unfinished runs | `agentic runs` |
| One run in detail | `agentic status <run>` |
| Its logs, live | `agentic logs <run> --follow` |
| What it cost | `agentic cost <run>` |
| End a run | `agentic stop <run> --reason "not needed"` |
| Free disk space | `agentic gc --dry-run`, then `agentic gc` |
| My effective policy | `agentic policy show` |

## Your own agents and skills

- **A repository agent:** `agentic agents new <name> --type <type>` writes
  `.agentic/agents/<name>.md`. Write its instructions in plain English. Check it with
  `agentic validate`. It reaches everyone once your pull request is merged.
- **A personal skill:** `agentic skills new <name>` writes
  `~/.agentic/skills/<name>/SKILL.md`. It affects only your runs. Check it with
  `agentic skills validate`, measure it with `agentic skills eval <name> --agent developer`,
  and propose it to others with `agentic skills submit <name> --to repo`.
- **Back up** personal skills and notes: `agentic skills backup`, and on a new laptop
  `agentic skills restore`.

## When agents learn from your corrections

Your gate rejections, edits and pull request review comments become feedback. After
repeated similar corrections, agentic proposes a change, such as a skill or a line in
`AGENTS.md`. Nothing changes until its owner approves it.

```bash
agentic learn list
agentic learn show <proposal>
agentic learn approve <proposal>     # a personal change; a shared one goes to its owner
agentic learn endorse <proposal>     # back a shared proposal you cannot approve
```

## Scripts and CI

Use `--json`. stdout then holds one JSON result. stderr is JSON Lines: read it line by
line. A live run writes `{"started": "<run>"}` first; an error object, if any, is the
last line. In CI there is no keyring: set `AGENTIC_TOKENLEAN_API_KEY`,
`AGENTIC_CONTROL_PLANE_TOKEN` and `AGENTIC_GITHUB_TOKEN` as secrets. Your team lead
creates a CI service account with `agentic rbac service-account`.

## Getting help

1. `agentic doctor` checks your machine, settings and services, with a fix for each
   problem.
2. `agentic support-bundle` writes a redacted zip for the platform team. Open it before
   you send it.
3. Ask your team lead, or the pilot office hours during rollout.

See also: [CLI operations runbook](../runbooks/cli-operations.md) (gc, doctor, support
bundle, sandbox limits) and [user settings](../user-config.md).

## Trying it without an org

`agentic init --standalone` uses a local policy and any gateway, with no control plane
and no roles. See `templates/standalone/README.md`.
