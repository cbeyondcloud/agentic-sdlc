# Command reference

Every `agentic` command and every `python -m agentic_cp` operator command, one row each.
A test fails when a command has no row here or a row names a command that does not
exist (`cli/tests/test_cli_command_reference.py`,
`control-plane/tests/test_cp_command_reference.py`).

For the flags of a command, run it with `--help`. For exit codes, run `agentic exit-codes`.

**Who:** Dev = developer, Lead = team lead, Maint = repo maintainer, Admin = org admin,
Platform = platform team, Audit = auditor or security, Exec = executive or finance.
"Org mode" means the command needs a control plane (`agentic login --for control-plane`).

## Global options

| Option | What it does |
|---|---|
| `--json` | Machine-readable output. stdout holds one JSON result. stderr is JSON Lines: a notice such as `{"started": "<run>"}` first, the error object (`error`, `exit_code`, `hint`) last. |
| `--no-color` | No colour. `NO_COLOR` works too. |
| `--version` | The installed version. |
| `-h`, `--help` | Help for any command. |

Set `AGENTIC_DEBUG=1` to see a traceback when a command fails unexpectedly.

## Set up and sign in

| Command | What it does | Who |
|---|---|---|
| `agentic version` | Show the installed version. | All |
| `agentic exit-codes` | List the exit codes every command uses. | All |
| `agentic login` | Store a key in the OS keyring: the gateway key (default), or `--for control-plane`, `--for jira`, `--for github` or a `git_hosts` host. `--invite <link>` redeems an invite mail. `--rotate` replaces your gateway key. | Dev |
| `agentic init` | Set up this repository on your machine: checks, policy, skills, tools, index. `--repo` sets up the repository itself on a new branch and opens its PR. `--standalone` uses a local policy with no control plane. | Dev, Lead |
| `agentic doctor` | Check this machine, its settings and the services a run needs. Changes nothing. | Dev, Platform |
| `agentic support-bundle` | Write a redacted zip for the platform team. | Dev |

## Find agents, pipelines, skills and policy

| Command | What it does | Who |
|---|---|---|
| `agentic agents list` | Every agent visible here. A nearer layer's agent replaces a farther one. | Dev |
| `agentic agents search` | Agents whose name, type or description contains the text. | Dev |
| `agentic agents new` | Create `.agentic/agents/NAME.md` from an agent type's template. | Dev, Maint |
| `agentic pipelines list` | Every pipeline visible here. | Dev |
| `agentic pipelines search` | Pipelines whose name contains the text. | Dev |
| `agentic pipelines new` | Create `.agentic/pipelines/NAME.yaml` from the pipeline template. | Lead, Admin |
| `agentic validate` | Check agent and pipeline files against org policy (exit 3 on an error). | Dev, Maint |
| `agentic policy show` | Tiers and models, repo classes, agent types and caps, and your team's narrowing. | Dev, Lead |
| `agentic policy check` | The policy-repo gate: policy, definitions and skill catalogs (exit 3 on a problem). | Admin, Lead |

## Run a pipeline

| Command | What it does | Who |
|---|---|---|
| `agentic run` | Run a pipeline. `--dry-run` shows the plan and spends nothing. `--from-jira PROJ-123` starts from a Jira issue. | Dev |
| `agentic runs` | This repository's unfinished runs (`--all` for every run). | Dev |
| `agentic status` | One run in detail, or the most recent runs. | Dev |
| `agentic logs` | A run's step logs, redacted. `--follow` keeps printing until the run stops. | Dev |
| `agentic cost` | What a run cost, by step, agent and child (or every run's total). | Dev |
| `agentic resume` | Continue a paused or interrupted run. `--extra-budget`, `--extra-minutes`, `--allow`/`--deny` a tool call, `--skip` a condition. `--scope` clears a pause switch. | Dev, Lead |
| `agentic stop` | End an unfinished run. Its trees are kept. | Dev |
| `agentic gc` | Remove finished runs older than a number of days (30 by default). `--dry-run` first. | Dev |
| `agentic sync` | Send run events still queued for the control plane. | Dev |

## Gates

| Command | What it does | Who |
|---|---|---|
| `agentic waiting` | Every gate and tool approval waiting for a person in this repository's runs. | Dev, Lead |
| `agentic show` | The handoffs a run waits on, and the code changes since the last gate. | Dev, Lead |
| `agentic approve` | Approve the pinned handoffs as they are. The run continues. | Dev, Lead |
| `agentic reject` | Send the work back to the gate's step with `--feedback`. | Dev, Lead |
| `agentic edit` | Edit a pinned handoff in your editor, then approve it. | Dev, Lead |

## Pull requests

| Command | What it does | Who |
|---|---|---|
| `agentic pr open` | Push a completed run's branch and open or update its pull request. | Dev |
| `agentic pr checks` | What CI says about the run's pull request. `--watch` waits; `--logs` reads failed logs. | Dev |

## Skills

| Command | What it does | Who |
|---|---|---|
| `agentic skills list` | Every skill in the org catalog, and whether it is installed as pinned. | Dev |
| `agentic skills search` | Skills whose key or notes contain the text. | Dev |
| `agentic skills new` | Create a personal skill in `~/.agentic/skills/NAME/SKILL.md`. | Dev |
| `agentic skills validate` | Check skills before use or promotion (exit 3 on a problem). | Dev, Maint |
| `agentic skills eval` | Run an agent over corpus cases with and without the skill, blind, and compare. | Dev |
| `agentic skills submit` | Propose a personal skill to a scope's owner (`--to repo`, `team` or `org`) as a pull request with its eval report. | Dev |
| `agentic skills status` | Your submissions. `--retire` retires the personal copy of a merged one. | Dev |
| `agentic skills review` | The open skill submissions of a scope. | Maint, Lead, Admin |
| `agentic skills approve` | Approve a submission as the scope's owner. | Maint, Lead, Admin |
| `agentic skills decline` | Ask for changes to a submission, with `--reason`. | Maint, Lead, Admin |
| `agentic skills backup` | Back up your personal skills and notes to the control plane. Org mode. | Dev |
| `agentic skills restore` | Bring your backed-up personal skills and notes back. Org mode. | Dev |

## Learning from corrections

Nothing changes until the owner of what changes approves a proposal.

| Command | What it does | Who |
|---|---|---|
| `agentic learn feedback` | The feedback records: each correction, who learns from it, and its lesson. | Dev, Maint |
| `agentic learn capture-pr` | Record a run's pull request review comments as feedback. | Dev |
| `agentic learn analyse` | Turn new records into one-line lessons and decide who learns (one fast-tier call per record). | Dev, Maint |
| `agentic learn attribute` | Correct which agents learn from a record. | Dev, Maint |
| `agentic learn sync` | Send feedback the control plane has not had yet. Org mode. | Dev |
| `agentic learn proposals` | The proposals made from repeated corrections. | Dev, Maint, Lead |
| `agentic learn list` | Every proposal, its status and who would decide it. | Dev, Maint, Lead |
| `agentic learn show` | The change a proposal would make, with its evidence and eval case. | Dev, Maint, Lead |
| `agentic learn propose` | Teach one correction now, without waiting for it to repeat. | Dev |
| `agentic learn edit` | Change a proposal before deciding it (its owner only). | Maint, Lead |
| `agentic learn endorse` | Back a proposal. It adds weight and never approves. | Dev |
| `agentic learn approve` | Approve a proposal you own and apply it (personal: written; shared: a pull request). | Dev, Maint, Lead |
| `agentic learn reject` | Say no. The lesson is not proposed again without new evidence. | Dev, Maint, Lead |
| `agentic learn defer` | Decide later. | Dev, Maint, Lead |
| `agentic learn revert` | Undo an applied learning. | Dev, Maint, Lead |

## Code index

| Command | What it does | Who |
|---|---|---|
| `agentic index export` | Build the index of the checked-out commit and write its export. | CI, Maint |
| `agentic index publish` | Publish an export on its own ref, never the default branch. | CI |
| `agentic index import` | Start the local index from a published export. | Dev |

## Evaluation

| Command | What it does | Who |
|---|---|---|
| `agentic eval run` | Run an agent over an eval corpus and write a report. | Admin, Maint |
| `agentic eval gate` | Compare eval reports with the quality gate's targets. | Admin |
| `agentic eval calibrate export` | Write a blinded sample of judged outputs for two people to score. | Admin |
| `agentic eval calibrate score` | Score the judge against the raters (weighted kappa). | Admin |

## People, roles and service accounts (org mode)

| Command | What it does | Who |
|---|---|---|
| `agentic rbac roles` | The tenant's roles, or one role's permissions. | Lead, Admin |
| `agentic rbac assignments` | Who holds which role, in the org or one team. | Lead, Admin |
| `agentic rbac assign` | Give a person a role. A new person is mailed an invite. | Lead, Admin |
| `agentic rbac revoke` | Take a role back. | Lead, Admin |
| `agentic rbac import` | Role assignments from CSV. A dry run unless `--apply`. Only adds. | Admin |
| `agentic rbac invite` | Mail a person a new invite. An earlier unused one stops working. | Lead, Admin |
| `agentic rbac check` | May you (or `--user`) do a permission? Exit 0 when allowed, 2 when not. | All |
| `agentic rbac mail` | The control plane's mail and why the last attempt failed, never its content. | Admin |
| `agentic rbac offboard` | End a person's access: tokens, invites and gateway keys revoked, running work stopped. | Admin |
| `agentic rbac revoke-keys` | Revoke every gateway key of a person, and nothing else. | Admin |
| `agentic rbac service-account` | Create a team's CI service account. Its token is shown once. | Lead, Admin |
| `agentic rbac service-accounts` | The tenant's or one team's service accounts. | Lead, Admin |

## Kill switch and pause (org mode)

A scope is `global`, `team:<slug>`, `user:<email>`, `repo:<host/owner/name>`,
`pipeline:<name>`, `agent:<name>[@version]`, `model:<id>` or `backend:<name>`.

| Command | What it does | Who |
|---|---|---|
| `agentic killswitch on` | Stop the runs a scope names now. Running steps are cancelled; runs end as killed. | Lead, Admin |
| `agentic killswitch off` | Clear a switch. Held runs continue on `agentic resume <run>`. | Lead, Admin |
| `agentic killswitch list` | The switches you may clear (`--all` for cleared ones too). | Lead, Admin |
| `agentic pause` | Hold the runs a scope names after their current turn. Clear with `agentic resume --scope`. | Lead, Admin |

## Audit and reports

| Command | What it does | Who |
|---|---|---|
| `agentic audit verify` | Verify run ledgers, the org chain (`--control-plane`) or an export file (`--export`), and say where a change was made. | Audit, Dev |
| `agentic audit export` | Export audit records with their chain proof, and verify the file. Org mode. | Audit |
| `agentic report chargeback` | The month's cost per team as CSV, for finance. Org mode. | Exec, Audit, Admin, Lead (own team) |

## Control-plane operator commands

The platform team runs these with the release's image, not on a laptop. Settings come
from `AGENTIC_CP_*` variables. `migrate`, `db` and `admin` need the owner role's DSN in
`AGENTIC_CP_OWNER_DATABASE_URL`, which the long-running pods do not have: run them in a
one-off pod (see "First tenant" in `deploy/helm/agentic-platform/README.md`). See the
[admin guide](admin.md).

| Command | What it does | Who |
|---|---|---|
| `python -m agentic_cp serve` | Run the API. Refuses a database that does not enforce tenant isolation. | Platform |
| `python -m agentic_cp worker` | The background worker: policy sync, CODEOWNERS, auto-trip, mail, key revocations. One leader at a time. | Platform |
| `python -m agentic_cp migrate` | Upgrade the database, re-grant the runtime role, seed roles. | Platform |
| `python -m agentic_cp db grant-runtime` | Create or re-align the runtime role. | Platform |
| `python -m agentic_cp db verify-restore` | Check a restored database: revision, guarantees, every audit chain. | Platform |
| `python -m agentic_cp db link-usage` | Point the dashboards' usage views at the gateway's tables. | Platform |
| `python -m agentic_cp db dashboard-role` | Bind one Grafana reader role to one tenant. | Platform |
| `python -m agentic_cp admin tenant create` | Create a tenant and its first org admin. Prints the token once. | Platform |
| `python -m agentic_cp admin tenant list` | List tenants. | Platform |
| `python -m agentic_cp admin tenant set-policy-repo` | Point a tenant at its policy repo. | Platform |
| `python -m agentic_cp admin tenant set-mail-from` | A tenant's own mail sender (`--clear` to remove). | Platform |
| `python -m agentic_cp admin tenant suspend` | Refuse a tenant's tokens and invites. Audited. | Platform |
| `python -m agentic_cp admin tenant resume` | Accept them again. Audited. | Platform |
| `python -m agentic_cp admin tenant delete` | Erase a suspended tenant except its audit chain. Irreversible; refused on legal hold. | Platform |
| `python -m agentic_cp admin user enable` | Re-enable an offboarded person. Then invite them again. | Platform |
| `python -m agentic_cp admin issue-token` | A recovery token for an existing user. | Platform |
| `python -m agentic_cp admin audit verify` | Verify one tenant's audit chain. | Platform, Audit |
| `python -m agentic_cp retention run` | Delete what each tenant's `retention.yaml` says has expired. `--dry-run` first. | Platform |
| `python -m agentic_cp retention gateway-config` | The gateway's retention config for one tenant. | Platform |
| `python -m agentic_cp openapi` | Write or check `control-plane/openapi.json`. | Platform (release) |
| `python -m agentic_cp dashboards` | Write or check the Grafana dashboards' JSON. | Platform (release) |
