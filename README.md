# agentic

Governed AI agent pipelines for software teams. A pipeline of agents (business analyst,
design, developer, code review, QA) works on your repository, stops at gates where a
person decides, and ends with a pull request. The org's policy decides which models and
tools each agent may use, and every step is recorded in a hash-chained audit trail.

agentic is provider-agnostic: every model call goes through the org's gateway, which
maps capability tiers to whatever models the org chooses.

## Which guide do I read?

| You are | Read |
|---|---|
| A developer | [Developer guide](docs/guides/developer.md): install to first run in about 15 minutes |
| A team lead | [Team-lead guide](docs/guides/team-lead.md): onboard your team and repositories |
| On the platform team, or an org admin | [Admin guide](docs/guides/admin.md): deploy, configure, operate |
| In security or compliance | [Auditor guide](docs/guides/auditor.md): verify and export the audit trail |
| Changing org policy | [Policy-repo guide](docs/guides/policy-repo.md) |
| Looking up a command | [Command reference](docs/guides/command-reference.md) |
| Running a procedure | [Runbooks](docs/runbooks/README.md) |

## Quick start (developer)

```bash
uv tool install agentic-cli --index-url https://pypi.your-org.internal/simple/
agentic login --invite "<the link from your invite mail>"
cd ~/src/your-repo
agentic init
agentic run feature-delivery --input request="Add CSV export" --dry-run
```

On Windows, run agentic inside WSL2. The [developer guide](docs/guides/developer.md) has
every step.

To try it alone, without an org: `agentic init --standalone` (see
`templates/standalone/README.md`).

## Repository layout

| Folder | What |
|---|---|
| `cli/` | the `agentic` command (`agentic_cli`) |
| `core/` | the engine, policy, agents, sandbox, gateway transport (`agentic`) |
| `commons/` | shared ledger, gates, guards and worktrees (`sdlc_commons`) |
| `control-plane/` | the org server: tenants, roles, policy sync, audit, dashboards (`agentic_cp`) |
| `library/` | the built-in agents, pipelines, skills and eval corpus |
| `templates/` | the org policy-repo and standalone templates |
| `deploy/` | the Helm chart and the local org stack |
| `docs/` | guides, runbooks, architecture, specs and plans |

## Developing agentic itself

A uv workspace on Python 3.12:

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest cli/tests
```

`scripts/check.py` runs the full CI. Licence: MIT (see `LICENSE` and
`THIRD_PARTY_NOTICES.md`).
