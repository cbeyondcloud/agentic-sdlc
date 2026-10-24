# Org policy-repo template

The starting point of an organisation's `agentic-org-policy` repository: the one place
where the org decides which models each capability tier means, what each kind of agent
may do, which pipelines exist, which skills are approved and who may change any of it
(spec §4.3, D30, D33). The control plane syncs this repository, validates every commit
and serves the accepted version to every laptop. Developers never edit it locally: a
locally edited policy is refused.

```
policy-repo/
├── policy/                   org policy (org admins)
│   ├── tiers.yaml            capability tiers -> models   EDIT: every model is a placeholder
│   ├── agent-types.yaml      what each kind of agent may use, and its caps (D35)
│   ├── classifications.yaml  repo classes; `restricted`: local models only, no personal
│   │                         skills or memory (decision 89)
│   ├── tools.yaml            tool catalog and the org deny list (D31)
│   ├── limits.yaml           concurrency, skill admission limits
│   ├── licences.yaml         blocked dependency licences, licences a skill may carry
│   ├── org.yaml              repo patterns, required steps, sandbox, scans  EDIT
│   ├── rollout.yaml          versions team by team, deprecations (spec §12.5)
│   ├── eval.yaml, retention.yaml, prices.yaml
│   └── teams/<team>.yaml     one team's narrowing (it can only narrow)
├── agents/                   org agents          (copy library/agents here)
├── pipelines/                org pipelines       (copy library/pipelines here; D33)
├── skills/catalog.yaml       approved skills, pinned by hash (copy library/skills here)
├── teams/<team>/             a team's layer: agents/, pipelines/ (+ overlays/), skills/
├── CODEOWNERS                pipelines/ -> org admins, teams/<team>/ -> that team's leads
└── .github/workflows/policy-check.yml   the pull request gate
```

## Setting it up (day 0)

1. Create the repository from this folder (for example `agentic-org-policy` in your git
   host's organisation) and copy in the framework's library, which this template does not
   duplicate:

   ```
   cp library/agents/*.md        <policy-repo>/agents/
   cp library/pipelines/*.yaml   <policy-repo>/pipelines/
   rm -r <policy-repo>/skills && cp -r library/skills <policy-repo>/skills
   ```

   The library's `skills/catalog.yaml` replaces this template's (it lists the vendored
   default skills with their pins and the same exclusions).
2. Edit `policy/tiers.yaml`: replace every `change-me` model with a model your gateway
   serves, and give `eval-judge` a model from another family than the agents' models.
3. Edit `policy/org.yaml`: `allowed_repo_patterns` (your host and organisation), the
   sandbox's `allowed_domains` (your package mirrors) and, if you run one, the tool mirror.
4. Teams: for each team, copy `policy/teams/payments.yaml` to `policy/teams/<team>.yaml`,
   copy `teams/payments/` to `teams/<team>/`, and add the team's two lines to CODEOWNERS.
   Delete the `payments` example if you have no such team.
5. In CODEOWNERS, replace the example handles (`@your-org/agentic-org-admins`,
   `@your-org/payments-leads`) with your org's teams.
6. Turn on branch protection for the default branch: require pull requests, turn on
   **Require review from Code Owners**, and require the `policy-check` status check.
   Without branch protection, CODEOWNERS only suggests reviewers and anyone with write
   access could change a pipeline (D33).
7. Run the gate locally once - it is what the workflow runs on every pull request:

   ```
   agentic policy check .
   ```

8. Point the control plane at the repository:

   ```
   python -m agentic_cp admin tenant set-policy-repo --tenant <org> --repo <https url>
   ```

   The control plane fetches the branch, validates it with the same code as the CLI and
   activates it; a commit that does not validate is stored as rejected, and the previous
   version stays active.

## Who changes what

| Path | Owner (CODEOWNERS) | Notes |
|---|---|---|
| `policy/`, `agents/`, `pipelines/`, `skills/` | org admins | org-wide; pipelines are admin-controlled (D33) |
| `policy/teams/<team>.yaml` | that team's leads, or org admins | can only narrow org policy |
| `teams/<team>/` | that team's leads | the team's agents, pipelines, per-repo overlays (`pipelines/overlays/<repo>.yaml`) and skills |

People's roles (org admin, team lead, developer, approver, auditor) are assigned in the
control plane, not here; repo maintainers come from each repository's own CODEOWNERS.
Agent roles - what a Business Analyst or a Developer agent may use - are the agent types
in `policy/agent-types.yaml`, and a team narrows their caps in `policy/teams/<team>.yaml`
(`limits_max`).

## Pipeline overlays

A team tailors an org pipeline for one repository with an overlay,
`teams/<team>/pipelines/overlays/<repo>.yaml` (the file name is the repository's name;
`repo: host/owner/name` inside binds it to exactly one repository). Overlays live only
here, so only the team's leads and the org admins change them (D33). An overlay may add
steps, remove steps that are not org-required, turn `four_eyes` on, set a gate's
`on_reject` (to an earlier step), tighten the regime and lower `max_minutes`. It can
never remove or weaken a step or gate listed in `policy/org.yaml` `required_steps`, nor
remove the step an org-required gate approves: `agentic policy check`, the control plane's
sync and every laptop refuse it with the rule named. List the work as well as the gates
in `required_steps` (the template requires the build loop's developer, code review and
QA), or an overlay could remove it. When a removed step had a gate after it that is not
org-required, the gate re-anchors to the previous remaining step (and the run's plan says
so). The policy check and the sync also run the preflight checks that hold on every
laptop on each team's pipelines, overlays applied: required steps present, `on_reject`
targets, conditions, and the handoffs each step needs. The control plane checks the overlay's hash like the pipeline's: a run
with an edited overlay, another team's overlay, or without the overlay that applies is
refused. `teams/payments/pipelines/overlays/payments-api.yaml` is an example, commented
out until the library's pipelines are copied in (an overlay of a missing pipeline is
refused, and this template must sync as it is).

## Versions and rollout

Roll a new version of an agent or pipeline out team by team in `policy/rollout.yaml`:
keep the previous version under `agents/versions/<name>/<version>.md` or
`pipelines/versions/<name>/<version>.yaml`, and list the teams that get the new one. A
team's leads may pin a version (`pins:` in `policy/teams/<team>.yaml`). A deprecated
version warns until its `blocked_from` date and is refused from it, by the CLI and by the
control plane's own clock.

Resolution order, for an org or team agent or pipeline:

1. the team's pin, when it pins that definition;
2. else, when `rollout.yaml` lists the org definition and the team is not in its
   `teams`, the entry's `previous` version (a team's own definition, in `teams/<team>/`,
   is the team's to pin and never held back);
3. else the current file.

Then the team's overlay for the repository applies (to pipelines), and the chosen
version's deprecation. A repository's own `.agentic/` definitions are never versioned.

## Skills

- **Approved skills** live in `skills/` (`catalog.yaml` plus `<source>/<name>/` folders),
  each pinned by sha256. `agentic policy check` admits every catalogued skill: licence
  (from `policy/licences.yaml` `skills:`, never from the catalog under review), format,
  size (from `policy/limits.yaml` `skills:`), and a static scan. Skills with scripts need
  a named reviewer (`scripts_reviewed_by`).
- **Team skills** live in `teams/<team>/skills/` with their own `catalog.yaml`, owned by
  the team's leads, under the same org licences and limits.
- **Promotion.** A developer turns experience into a personal skill (`agentic skills new`),
  evaluates it (`agentic skills eval`) and runs `agentic skills submit --to team` or
  `--to org`: a pull request to this repository adding the skill and its catalog entry,
  with the eval report in its description. The scope owner reviews it with
  `agentic skills review` and `agentic skills approve` (checked against their role in the
  control plane), and a person merges it.
- **Opt-in plugin.** A team may enable the full superpowers plugin for the steps it
  delegates to an agent-CLI backend that loads plugins, never for the native loop:
  vendor it into `skills/plugins/superpowers/`, catalogue it under `plugins:` (pinned,
  reviewed, with its visual companion excluded) and add it to the team's
  `backend_plugins`.

## Restricted repos

The `restricted` class may use only the `local` tier, so its code never leaves your
machines, and it turns personal skills and personal memory off: only reviewed, shared
skills shape agents there. Give `local` a model served inside your network before a
restricted repository runs its first pipeline.
