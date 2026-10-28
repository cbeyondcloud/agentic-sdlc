# Standalone policy template

The policy for using the framework without an organisation: one person, any repo, no
control plane (spec §24, "Trying it without the org setup").

```
standalone/
├── policy/               the policy folder (the same files as an org policy repo's policy/)
│   ├── tiers.yaml        capability tiers -> models   EDIT: every model is a placeholder
│   ├── agent-types.yaml  what each kind of agent may use, and its caps
│   ├── tools.yaml        tool catalog and what every agent is denied
│   ├── classifications.yaml, limits.yaml, licences.yaml, retention.yaml, prices.yaml
│   └── org.yaml          sandbox, secret scan, local pipelines allowed
└── skills/preload.yaml   which models get their skills pre-loaded (the local tier)
```

## Getting started

From the root of the repository you want to work on (a clean checkout of its default
branch):

```
agentic init --repo --standalone     # once per repo: proposes .agentic/config.yaml on a branch
git merge agentic/init               # or push the branch and merge it by a pull request
agentic init --standalone            # each person, on each machine
```

The first time, `agentic init --standalone` builds your own policy repo in `~/.agentic/policy/standalone/`
(`AGENTIC_HOME` instead of `~/.agentic` when you set it). It holds:

- `policy/`, copied from this folder;
- the library agents and pipelines (`agents/`, `pipelines/`);
- the default skills catalog (`skills/`), with this folder's `skills/preload.yaml` rule
  applied;
- one starter file per agent type (`templates/agents/<type>.md`, used by `agentic agents new`).

Later commands (`agentic run`, `agentic status`...) find it there. Init never overwrites
it: it is your copy, and you edit it. To start again from the template, delete the folder
and run init again. To use a policy repo of your own instead, pass `--policy <folder>`.

Then:

1. In `~/.agentic/policy/standalone/policy/tiers.yaml`, replace every `change-me` model
   with a model your gateway serves. Until you do, init reports `policy: models` as
   missing.
2. Optionally adjust caps in `policy/agent-types.yaml`. Agents may only narrow their type.
3. Run `agentic init --standalone` again. It should report ready, apart from what the
   report says your machine still lacks.
4. Run a library pipeline, for example `agentic run feature-delivery --input
   request="..." --dry-run`, then the same command without `--dry-run`.

Because the layout matches an org policy repo, moving to org mode later is a copy into
that repo.

## Windows needs WSL2

Both library pipelines run in the `team` regime, and `org.yaml` keeps
`sandbox.required_for: [team, regulated]`: agent commands run only inside an OS sandbox.
macOS and Linux have one. Native Windows does not, so on Windows run agentic inside
WSL2. `agentic init` prints the exact steps, including the sandbox runtime version your
policy pins. They start with:

```
wsl --install -d Ubuntu-24.04
```

Install agentic inside Ubuntu, and clone your repositories to the WSL disk (for example
`~/src`), not under `/mnt/c`. On native Windows, `solo` runs still work: only policy
guards confine them, and they warn. `team` runs refuse to start.

## Restricted repos

The `restricted` class (`policy/classifications.yaml`) may use only the `local` tier: its
code must never leave your machine or network. Every library agent type defaults to
`balanced` or `deep-reasoning`, so a restricted repo's preflight refuses every agent
step until you choose a local model. Either:

- in that repo, copy the agents you use into `.agentic/agents/` and set `tier: local`
  in each (agent files may narrow their type: the repo's copy overrides the library's);
- or, if all your repos are restricted, set `tier: local` on the agent types in
  `policy/agent-types.yaml`.

Set `local` in `tiers.yaml` to a model served on your own machine or network. Agents
on the local tier get their skills pre-loaded (`skills/preload.yaml`), because local
models use a skill catalog poorly.

## Allowing another test command

The developer and QA types may run only the command prefixes in their `shell:` list in
`policy/agent-types.yaml`. A prefix allows that command with any further words:
`"make test"` allows `make test -j4`, never `make install`. The template lists the
common runners for Python, JavaScript, Java, Go, Rust, .NET and Make repos. To add
another, append its prefix to both types, for example `"just test"` or
`"bundle exec rspec"`. Keep it as narrow as the command you need: a bare program name
allows every subcommand.

Every agent is denied the prefixes in `policy/tools.yaml` (`deny.shell`), whatever its
type allows: publishing, rewriting history, and the forms of the dependency audits that
change dependencies or write files (`npm audit fix`, `pip-audit --fix`, `pip-audit -o`).

## A repository whose tests already fail

The pipelines take a test baseline before any code changes, and QA then asks for no
*new* failures, not for zero. Tests that error in setup and test files that fail to
collect count as failed and are named like any other failure. If the baseline's report
is refused as invalid (a miscount on a large red suite), the run pauses at that step:
`agentic show <run>` shows the report, `agentic edit <run>` lets you correct the counts
or names, and `agentic reject <run> --feedback "..."` asks the agent to measure again.
