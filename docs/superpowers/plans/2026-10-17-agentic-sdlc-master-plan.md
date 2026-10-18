# Agentic SDLC Framework — Implementation Plan (master + tracker)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the provider-agnostic, enterprise-governed agentic SDLC framework described in the design spec, delivering v1 as three milestones (M1 standalone core → M2 org governance → M3 scale and operations) after a short spike phase, and tracking Phases 2–4 as epics.

**Architecture:** One Python core library used by a CLI (standalone mode) and by a small FastAPI control plane (org mode). All model calls go through TokenLean (LITE profile); Zyroflo (sdlc-harness) artefacts are extracted into a shared `commons` package; OSS libraries (Pydantic AI, python-frontmatter, Claude Agent SDK, codex, srt, code-review-graph…) do the heavy lifting.

**Tech Stack:** Python 3.12, uv workspace, Pydantic v2, Pydantic AI v2, python-frontmatter, ruamel.yaml, httpx, openai/anthropic SDKs, Typer/Cyclopts + Rich + questionary, FastAPI + Postgres + Alembic, OpenTelemetry, Grafana, Helm, pytest.

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md` (r2; r3 sync is task P0-02). Research: `reports/Agentic SDLC OSS reuse.md`, `research_notes/Agentic SDLC OSS reuse/*.md` (incl. `sdlc_harness_comparison.md`, `tokenlean_observability_audit.md`, `agent_skills.md`).

## Context

The user wants a framework of specialised AI agents (BA, Design, Developer, Code Review, QA, custom) chained into pipelines with human gates, child agents under admin limits, schema-validated handoffs, layered org/team/repo governance, RBAC, kill switch, tamper-evident audit, dashboards and a human-approved learning loop, working on any repo, provider-agnostic via the user's TokenLean proxy, and reusing the user's Zyroflo harness and OSS wherever possible. Requirements were clarified over 30 decisions (spec §2, D1–D30). This plan turns the spec into an executable, trackable sequence. The intended outcome is the spec §1.3 acceptance test passing on the org stack, ready for a pilot with ~20 developers.

**Execution method (recommended): Subagent-driven** — a fresh implementer per task plus a fresh reviewer gate, because ~170 tasks across tightly interfaced sub-plans (ledger, engine, policy) make a shipped interface mistake expensive; the alternative is Native (I implement in-session, one whole-branch review at the end).

## How this plan is organised and executed

1. **Milestones.** v1 is delivered as three milestones, each ending in working, demonstrable software and a go/no-go:
   - **M1 — Standalone core**: one pipeline (`feature-delivery`) runs end to end on a laptop in standalone mode (no control plane, no RBAC): agents, gates, review loop, ledger, sandbox, skills, GitHub PR. Exit = acceptance scenarios 1, 2 and 4 in standalone mode **and the agent quality gate**.
   - **M2 — Org governance**: control plane, policy sync, RBAC, kill switch, audit chain, dashboards, TokenLean team level and key API, Codex backend, Jira. Exit = acceptance scenario 3 (admin override), kill-switch propagation ≤ 30 s, audit verify + export.
   - **M3 — Scale and operations**: Helm deployment and environments, ROI dashboard, learning loop, release engineering, load test, pilot kit. Exit = full §1.3 suite on staging, load test passed, pilot go/no-go.
2. **Tracker** (below) lists every work item with milestone-sequential IDs (`P0-nn`, `S1–S14` for spikes, `M1-nn`, `M2-nn`, `M3-nn`, `E2/E3/E4-nn`). Legacy work codes for TokenLean items (`ST-n`) and spikes (`Sn`) are kept in the item text because the spec cites them. All start `Pending`.
3. **Sub-plans** (SP1–SP21) group rows by component; the **Sub-plan** column shows the grouping. **SP1 (definitions and policy) is detailed below in TDD steps.** Every later sub-plan gets its own detailed TDD plan written (writing-plans skill) into `docs/superpowers/plans/` immediately before it starts, using its tracker rows as scope — so each plan reflects spike results and code that exists by then.
4. **First execution step** (P0-01): copy this file to `docs/superpowers/plans/2026-10-17-agentic-sdlc-master-plan.md` and the tracker to `docs/superpowers/plans/TRACKER.md` in the repo; the repo copy of the tracker is the one updated from then on.
5. **Status values**: `Pending` → `In progress` → `Done` (code written, tests written and passing locally and in CI, committed) · `Blocked` (with reason) · `Dropped` (with reason, recorded in the de-scope register `docs/de-scope-register.md`, Zyroflo convention).
6. **Updating the tracker**: the commit that completes an item also flips its row to `Done` and records the commit SHA in the Notes column.
7. **External repos**: TokenLean items (`ST-n`) are done in `d:\token-optimisation` on a feature branch; Zyroflo items in the Zyroflo repo on a feature branch. Nothing is pushed to any remote without the user's explicit go-ahead.
8. **Spikes** produce a record `docs/spikes/Sxx-<name>.md` (question, method, result, decision) and throwaway code under `spikes/` (never imported by product code). If a spike changes a decision, the spec is updated and the change noted in its revision line.

## Effort, team and timeline

**Estimate convention.** `PW` = person-weeks of one engineer, including tests, review and docs for that row. Estimates are **±50 % at this stage**; they are re-estimated at the start of each milestone when its detailed sub-plans are written, and the tracker keeps both numbers.

**Team assumption (editable — change here and the timeline follows).**

| Role | Allocation | Covers |
|---|---|---|
| Engineer A — core/engine | 1.0 | SP1, SP3, SP4, SP6, SP5 |
| Engineer B — CLI/backends/sandbox/codeintel | 1.0 | SP7, SP8, SP9, SP13, SP14 |
| Engineer C — control plane/dashboards/integrations | 1.0 | SP15, SP16, SP17, SP21 |
| Engineer D — TokenLean, Zyroflo commons, skills, security | 1.0 | SP2, SP10, SP11, SP12, SP19 |
| Platform/SRE | 0.5 from M3 | Helm, environments, backups, load test |
| Owner (Sumit) | part-time | Product decisions, org admin role, pilot, spec |

**Totals and calendar** (4 engineers ≈ 4 PW per week; +20 % buffer for integration and rework):

| Phase / milestone | PW | Calendar at 4 engineers | Ends with |
|---|---|---|---|
| Phase 0 — bootstrap and spikes | ~15 | ~4 weeks (spikes run in parallel) | Spike review, spec r3 |
| M1 — standalone core | ~70 | ~17 weeks (≈ 4 months) | Standalone demo + agent quality gate |
| M2 — org governance | ~54 | ~13 weeks (≈ 3 months) | Org demo: admin override, kill switch, audit |
| M3 — scale and operations | ~31 | ~8 weeks (≈ 2 months, with SRE) | Pilot-ready on staging |
| **v1 total** | **~170** | **~42 weeks ≈ 10 months; ~12 months with buffer** | |

**What moves the timeline.** The critical path is the engine chain in M1 (definitions → transport → agent runtime → engine → CLI → agents → acceptance); more engineers shorten M2/M3 more than M1. Six engineers ≈ 8–9 months. Scope levers if 12 months is too long (each is a clean cut to phase 2): learning loop (−8 PW), ROI dashboard + chargeback (−2), Codex backend (−3), domain-graph adapter + Zyroflo graph extraction (−2), personal skills (−1.5), Windows sandbox hardening via WSL2-only policy (−1.5). The owner decides at each milestone retro.

## Global Constraints

- Python **3.12**; dependency management with **uv** workspace; all dependency versions pinned in `uv.lock`.
- **OSS-first** (standing instruction): research GitHub before building any component; only permissive licences (MIT, Apache-2.0, BSD, ISC, PostgreSQL) as dependencies. **No** GPL/AGPL/LGPL-linked, SSPL, BSL, PolyForm, Commons-Clause or other non-OSI dependencies. CC-BY-SA content only as opt-in, after legal review.
- Must run on **Windows, macOS and Linux**; CI matrix covers all three. No symlinks in generated repo files.
- **Every model call goes through TokenLean** via base_url; no direct provider calls from product code.
- TokenLean is used as-is (same codebase) with the **LITE profile** (ST-6): no fork. Behaviour-changing optimisations stay off for agent traffic unless enabled through the A/B opt-in process (ST-7).
- Identity sent only in framework headers: `X-Agentic-Team`, `X-Agentic-User`, `X-Agentic-Repo`, `X-Agentic-Pipeline`, `X-Agentic-Run-Id`, `X-Agentic-Step`, `X-Agentic-Agent`, `X-Agentic-Parent-Agent`, `X-Agentic-Tier`, `X-Agentic-Policy-Version`, plus W3C `traceparent`. **Never** send `X-Step-Name`, `X-Workflow-ID` or `X-Session-ID` for identity.
- Unmeasured values are **null, never 0** (Zyroflo metrics doctrine).
- Ledger hash (Zyroflo-compatible): `"sha256:" + sha256(json.dumps(body, sort_keys=True, separators=(",",":"), ensure_ascii=False))` where `body` = entry minus `hash` and `signature`; `prev` = previous entry's `hash`, first entry `prev = ""`.
- Restricted fields (tier/model, effort, temperature, backend, tools, shell, skills, limits, children) are **never merged** across layers; org policy (+ team narrowing, tighten-only) is their only source; definitions only *request*.
- **Pipelines are admin-controlled (D33)**: in org mode, pipeline definitions (and per-repo overlays) live only in the policy repo — `pipelines/` (org admins) and `teams/<team>/pipelines/` (that team's leads) — and change only by a PR approved through CODEOWNERS with branch protection. Repo maintainers and developers request changes but cannot make them. Repo-level `.agentic/pipelines/` is ignored unless org policy sets `allow_repo_pipelines: true`. A run uses only the approved, hash-matched version; a locally edited pipeline is refused. Standalone mode (no org) keeps local pipelines.
- **Deny lists (D31)**: org policy, team policy and each agent file may declare `deny` rules (`shell` command prefixes, `paths` globs). Deny lists are the one thing that is **merged** across layers — as a union, because adding a restriction is always safe; a deny always beats an allow; child agents inherit their parent's denies.
- No telemetry to third parties; strip vendor telemetry from reused components (e.g. superpowers visual companion).
- Files in target repos: only `.agentic/`, `AGENTS.md`, `.gitattributes` entries and `.github/workflows/agentic-index-nightly.yml`.
- **Agent quality is a gate, not an afterthought**: no built-in agent ships in M1 without passing the agent quality gate (M1-64); no agent/skill version is promoted later without the eval harness (M1-63).

## Users and roles — who does what, when

Roles are simple RBAC (spec §12.3): a role is a named permission set; admins assign roles to people, optionally scoped to a team. One person can hold several roles.

### People and their roles

| Persona | Role(s) assigned | Typical headcount (1000-person org) | What they do | Tools they use |
|---|---|---|---|---|
| **Platform team** (operates the system) | Org admin (+ cluster access outside the app) | 2–4 | Install and upgrade the Kubernetes deployment, run TokenLean, back up Postgres, monitor health, publish CLI releases to the internal index | Helm, kubectl, Grafana, runbooks |
| **Org admin** (AI governance owner) | Org admin | 2–3 | Owns the policy repo: tier→model mapping, allowed providers, classifications, org limits and budgets, tool and skill catalog; assigns team leads; flips the global kill switch | Policy repo PRs, `agentic rbac`, `agentic killswitch`, org dashboards |
| **Security / compliance** | Auditor (+ reviewer on policy repo PRs) | 2–5 | Reviews policy changes, approves skills with scripts, reads audit trails, runs `audit verify` / exports for auditors, sets retention and trace-capture policy | Audit explorer, `agentic audit export`, policy repo reviews |
| **Executive / finance** | Executive viewer *(default role: `dashboard.view.org`, ROI and chargeback only)* | 5–20 | Reviews ROI, adoption and cost per team monthly | Executive ROI dashboard, chargeback CSV |
| **Team lead** | Team lead (team-scoped) | ~80–100 | Onboards their team, narrows team policy (tiers, budgets), publishes team agents/pipelines, approves team-level gates, raises team budget, pauses their team | `agentic rbac` (team scope), team dashboard, team policy file PRs |
| **Developer** | Developer | ~800–900 | Initialises repos, runs pipelines, approves own-run gates where the regime allows, writes repo-level agents in plain English, writes personal skills | `agentic` CLI, editor, GitHub PRs |
| **Repo maintainer** | Repo maintainer *(derived from the repo's CODEOWNERS for `.agentic/` and `AGENTS.md`; no manual assignment)* | 1–3 per repo | Owns the repo's **shared** agents, skills and memory (not pipelines — D33): approves PRs that change them, approves repo-scope learning proposals, reviews personal-skill promotion requests for the repo | GitHub PR review, `agentic learn approve`, `agentic skills review` |
| **Approver / reviewer** | Approver (team-scoped) | as needed | Approves gates that require a named role (e.g. architecture review, four-eyes merges) | `agentic approve`, PR review |
| **CI / bots** | Service account (team-scoped) | per team | Nightly index refresh; later CI-triggered pipelines; cannot approve gates | GitHub Actions |

### Who does what, when

| When | Who | Activity |
|---|---|---|
| **Day 0** — platform setup (once) | Platform team | Deploy TokenLean, control plane, Postgres, Grafana/OTel to dev → staging → prod; create the org policy repo from the template; register GitHub app/tokens and Jira connector; configure SMTP; publish CLI to internal index |
| **Day 1** — org configuration (once, then ongoing) | Org admin + Security | Fill policy repo: tiers, providers, classifications, budgets, blocked licences, skill catalog, retention; seed roles; assign team leads (CSV bulk import); issue proxy keys |
| **Team onboarding** (per team, ~1 hour) | Team lead | Assign Developer/Approver roles to the team; set team budget and allowed tiers; pick pilot repos; run `agentic init --repo` on them (opens the PR with shared files + nightly index workflow) |
| **Developer onboarding** (per person, ~15 min) | Developer | See "Developer onboarding" below |
| **Daily** | Developers | Run pipelines, review and approve gates, merge PRs |
| **Daily (automatic)** | Service account | Nightly index refresh on each initialised repo |
| **Weekly** | Team leads | Team dashboard: cost, limit hits, gate rejection rates; adjust team policy; approve team-scope learning proposals |
| **Weekly / on demand** | Org admin | Policy changes via PR (staging first, then prod); agent/skill version rollouts team by team; kill switch when needed |
| **Monthly** | Executives / finance | ROI dashboard + chargeback export |
| **Quarterly / on audit** | Security / compliance | Audit export with chain proof, retention review, skill catalog review |
| **Per release** | Platform team | Upgrade framework (staging → prod), raise `min_cli_version`, announce |

### Default permission matrix

**Ownership rule (D30):** anything **shared** (org, team or repo scope: skills, memory, agents) is read-only for users and changed only by the **owner of that scope** — org admin (org), team lead (team), repo maintainer (repo). **Pipelines are stricter (D33): only org admins and team leads.** Anything **personal** (the user's own skills and notes) is owned and edited by that user and affects only their runs.

| Permission | Org admin | Team lead | Repo maintainer | Developer | Approver | Auditor | Exec viewer | Service acct |
|---|---|---|---|---|---|---|---|---|
| Trigger runs / cancel own | ✅ | ✅ | ✅ | ✅ | — | — | — | ✅ (trigger) |
| Approve gates | ✅ | ✅ team | ✅ repo | ✅ own runs if regime allows | ✅ assigned | — | — | — |
| Create / edit **personal** skills and memory | ✅ own | ✅ own | ✅ own | ✅ own | ✅ own | — | — | — |
| Propose changes to shared repo items (PR / `skills submit` / learning proposal) | ✅ | ✅ | ✅ | ✅ | ✅ | — | — | — |
| Approve shared **repo** agents, skills, memory (AGENTS.md) and repo-scope learning proposals | ✅ | ✅ team repos | ✅ repo | — | — | — | — | — |
| Publish / approve **team** agents, pipelines (incl. per-repo overlays), skills and team-scope learning proposals | ✅ | ✅ team | — | — | — | — | — | — |
| Change pipelines (any scope, D33) | ✅ | ✅ team | — | — (request only) | — | — | — | — |
| Approve **org** skills (catalog), org agents/pipelines, guardrail proposals; edit org policy | ✅ | — | — | — | — | — | — | — |
| Narrow team policy / raise team budget | ✅ | ✅ team | — | — | — | — | — | — |
| Kill switch / pause | ✅ global | ✅ team | — | — | — | — | — | — |
| Assign roles | ✅ | ✅ team (Developer/Approver only) | — | — | — | — | — | — |
| Dashboards | org | team | repo | own | team | org | org (exec views) | — |
| Audit / traces | org | team | repo | own runs | — | org | — | — |

---

## Developer onboarding

### Prerequisites (checked automatically by `agentic init`)

Python 3.12 (or the bundled installer), git ≥ 2.40, uv or pipx; access to the internal package index; GitHub access to the repo; on Windows: WSL2 **or** Codex CLI for sandboxing (S6 decides the default); optional: Claude Code / Codex CLI if the team uses those backends.

### First-time setup (about 15 minutes)

| Step | Developer does | What happens |
|---|---|---|
| 1 | Receives an invite email (sent when the team lead assigns the Developer role) with a one-time link to fetch their **proxy key** and **control-plane token** | Keys are bound to user + team + tenant |
| 2 | `uv tool install agentic --index-url <internal index>` | CLI installed at the org's pinned version |
| 3 | `agentic login` | Stores keys in the OS keychain; verifies with TokenLean and the control plane |
| 4 | `cd <repo>` · `agentic init` | Checks prerequisites and sandbox; fetches org policy; installs the approved skills used by the repo's pipeline agents; pulls the committed index; prints a readiness report with fixes for anything failing |
| 5 | `agentic pipelines list` · `agentic agents list` | Shows what the org and team have published |
| 6 | `agentic run feature-delivery --from-jira PROJ-123 --dry-run` | Preflight only: resolved models, budgets, gates, who must approve — no spend |
| 7 | `agentic run feature-delivery --from-jira PROJ-123` | First real run; pauses at the design gate |
| 8 | `agentic status` → `agentic approve <run> --gate design-approval` (or `reject --feedback "…"` / `edit`) | Run continues; ends with a PR linked to the run and its handoffs |

If the repo was never initialised, the developer (or team lead) runs `agentic init --repo` once first; it puts `.agentic/`, `AGENTS.md`, `.gitattributes` and the nightly index workflow on a new branch and opens a normal PR (never commits to `main` directly).

### What `agentic init` downloads onto the developer's machine

Python packages arrive **once, when the CLI is installed** (`uv tool install agentic` from the internal index, into an isolated tool environment — never into the repo's own virtualenv): `agentic` + `sdlc_commons` and their pinned dependencies (pydantic, pydantic-ai, httpx, openai, anthropic, python-frontmatter, ruamel.yaml, typer/cyclopts, rich, questionary, keyring, opentelemetry-sdk, tree-sitter + grammars for the domain graph). **`agentic init` installs no Python packages and changes nothing system-wide.** It only fetches user-space artefacts, each pinned by version **and sha256**, from the org's internal mirror (URLs set in org policy), reusing Zyroflo's `tools/third_party.py` + `third_party/manifest.json` approach:

| Artefact | Needed for | Stored at | When fetched |
|---|---|---|---|
| Org policy snapshot (signed version id) | Preflight, limits, tiers | user cache `~/.agentic/policy/<org>/` (not the repo) | Always (org mode) |
| Approved skills used by the agents in this repo's pipelines (+ authoring skills for admin roles only) — list below | Agent skills | repo `.agentic/local/skills/` (git-ignored) | Always; only skills in the org catalog, hash-verified |
| `gitleaks` binary | Secret scan before any content leaves the laptop | user cache `~/.agentic/tools/gitleaks/<ver>/` (shared across repos) | Always (mandatory guardrail) |
| `osv-scanner`, `ast-grep` binaries | Review pre-pass, dependency vulnerabilities | `~/.agentic/tools/…` | If the repo's pipelines include Review/QA (default yes) |
| Symbol-graph adapter (`code-review-graph` wheel into the CLI's tool env, or `codebase-memory-mcp` binary) | Code intelligence | CLI tool env / `~/.agentic/tools/…` | Only the adapter named in `.agentic/config.yaml` |
| ScanCode | Licence scan of new dependencies | `~/.agentic/tools/…` | Only if org policy enables licence scanning |
| Committed dependency index | Impact analysis | already in the repo (`git pull`); local overlay built in `.agentic/local/index-overlay/` | No download |

**Skills `init` downloads (default catalog, spec §11.2; all vendored into the org catalog, adapted to remove harness-specific orchestration, pinned by hash).** Selection rule: union of the `skills:` lists of every agent used by the pipelines enabled for this repo; optional packs only if team policy opts in; authoring skills only if the user holds Org admin or Team lead. "Pre" = loaded into the agent's context at start; "on-demand" = listed in the catalog, loaded when the agent activates it.

| Agent | Skill | Source (licence) | Loading |
|---|---|---|---|
| Business Analyst | `interview-me` / `grill-with-docs` (questions routed to a human gate) | addyosmani/agent-skills · mattpocock/skills (MIT) | pre |
| Business Analyst | `spec-driven-development` / `to-spec` | addyosmani · mattpocock (MIT) | pre |
| Business Analyst | `domain-modeling` | mattpocock (MIT) | on-demand |
| Design | `documentation-and-adrs` | addyosmani (MIT) | pre |
| Design | `writing-plans` | obra/superpowers (MIT) | pre |
| Design | `api-and-interface-design` | addyosmani (MIT) | on-demand |
| Design | `improve-codebase-architecture` | mattpocock (MIT) | on-demand |
| Developer | `test-driven-development` | superpowers (MIT) | pre |
| Developer | `verification-before-completion` | superpowers (MIT) | pre |
| Developer | `systematic-debugging` | superpowers (MIT) | on-demand |
| Developer | `incremental-implementation` | addyosmani (MIT) | on-demand |
| Developer | `receiving-code-review` | superpowers (MIT) | on-demand |
| Code Review | `code-review-and-quality` (five-axis) | addyosmani (MIT) | pre |
| Code Review | review checklist from `requesting-code-review` (subagent dispatch removed) | superpowers (MIT) | pre |
| Code Review | `security-and-hardening` | addyosmani (MIT) | on-demand |
| QA | `verification-before-completion` | superpowers (MIT) | pre |
| QA | `webapp-testing` / `browser-testing-with-devtools` | anthropics/skills (Apache-2.0 subset) · addyosmani (MIT) | on-demand |
| QA | TDD testing-anti-patterns reference | superpowers (MIT) | on-demand |
| Admins / team leads only | `skill-creator`, `writing-skills` | anthropics (Apache-2.0) · superpowers (MIT) | on-demand |

About 16 distinct skills for a repo using `feature-delivery` (≤ 3 pre-loaded per agent); **superpowers is the largest source** (7 defaults + `writing-skills` for admins + `brainstorming` opt-in). **Full superpowers plugin (team opt-in)**: a team may enable the complete superpowers plugin, pinned and from the org catalog, **only for steps delegated to the `claude-code` or `codex` backends** (installed into that backend's workspace config for the run, never into our native loop), with the visual-companion telemetry disabled. **Opt-in packs** (team policy): Trail of Bits security/testing (`differential-review`, `insecure-defaults`, `supply-chain-risk-auditor`, `static-analysis`, `sharp-edges`, `property-based-testing`, `mutation-testing`) — CC-BY-SA-4.0, after legal review; `brainstorming` (superpowers) for interactive BA sessions. **Never downloaded for the native loop**: superpowers `using-superpowers`, `subagent-driven-development`, `executing-plans`, `using-git-worktrees`, `finishing-a-development-branch` (duplicate the orchestrator; available only via the full-plugin opt-in above); Caveman; anthropics docx/pdf/pptx/xlsx (source-available); anything without a confirmed licence. Each default must pass the A/B promotion gate (M1-63 eval harness / S10) before GA; a skill that shows no benefit is removed from the default catalog.

### Custom skills — capturing senior developers' experience

Anyone can write a skill in the open Agent Skills format (a folder with `SKILL.md`: name, description, plain-English instructions, optional reference files and scripts). Typical custom skills: team coding conventions, "how we handle retries/idempotency in payments", migration review checklist, incident lessons, internal framework usage.

Skills (and memory) come in two kinds, following the ownership rule (D30):

| Kind | Scope | Where it lives | Who edits | Review before use | Reaches |
|---|---|---|---|---|---|
| **Personal** | User | `~/.agentic/skills/<name>/` and personal notes `~/.agentic/memory/` (on the user's machine, synced to the control plane for backup/audit) | The user, freely | `agentic skills validate` on save; **instruction-only**, cannot widen tools/models/limits; can be disabled per repo classification by policy | Only that user's runs |
| **Shared** | Repo | `.agentic/skills/<name>/` + `AGENTS.md` in the repo (committed) | Repo maintainers (CODEOWNERS) via PR | PR review; `skills validate` in CI; instruction-only; repo must be marked trusted | Everyone's runs in that repo |
| **Shared** | Team | policy repo `skills/teams/<team>/` | Team lead via policy-repo PR | Admission checks (licence, validate, size, script scan); A/B eval recommended | That team's repos |
| **Shared** | Org | policy repo `skills/` catalog | Org admin via policy-repo PR | Full admission checks + human review of scripts + **A/B promotion gate** (M1-63) | Everyone, rolled out team by team |

**Shared skills are read-only on developer machines.** They are downloaded pinned by sha256; before every run the CLI verifies the hashes, and a locally edited shared skill is restored from the catalog and logged (`skills.tamper_restored`). Precedence when names clash: shared org/team/repo skills that an agent definition references cannot be overridden by a personal skill of the same name — personal skills are added alongside, never replace.

**Authoring flow** (CLI, built on Anthropic `skill-creator` + superpowers `writing-skills` "test the skill first" method):

1. `agentic skills new <name>` — scaffolds a **personal** `SKILL.md` from a template.
2. Write the instructions in plain English; add examples/reference files; use it in your own runs (`agentic run … --skill <name>` or list it in your personal agent overrides).
3. `agentic skills validate` — format (`skills-ref`), size budget, no secrets, no scripts, no permission widening.
4. `agentic skills eval <name> --agent developer --cases <corpus>` — runs the agent with and without the skill on replayed past changes (Zyroflo corpus replay) and reports outcome, cost and tokens.
5. **Request promotion** when it is generic and useful to others: `agentic skills submit <name> --to repo|team|org` opens a PR (repo) or policy-repo PR (team/org) with the eval report; the scope owner reviews (`agentic skills review`) and approves or declines; on approval the control plane pins its hash and rolls it out, and the personal copy is retired.

Every skill activation is logged with kind (personal/shared), name, version and hash — in the run ledger, so reviewers and auditors can see what shaped an agent's output — and the agent-quality dashboard shows whether it helps. When several users hold similar personal skills, the Learning Analyst suggests promoting one.

### Learning loop — agents learn from human corrections, only with explicit approval (D29)

Applies to **every functional agent** (BA, Design, Developer, Code Review, QA, custom). Nothing about an agent's skills, memory or instructions changes unless a human explicitly approves a specific proposal. Builds on Zyroflo `feedback.py` (captures corrections at gates) and `learning.py` (repeated corrections → gated proposals with undo). Delivered in M3 (rows M3-11…M3-16).

**1. Capture — every human correction becomes a feedback record** (stored in the run ledger and sent to the control plane):

| Signal | Example |
|---|---|
| Gate rejection with feedback | Design gate: "use the existing queue, don't add a new one" |
| Gate edit (diff between agent output and human-edited handoff) | Team lead rewrites two acceptance criteria in the requirements handoff |
| GitHub PR review comment / "changes requested" on an agent-made PR | "Wrap this call in our `with_retry()` helper" |
| Review-agent miss | Human requests a change on lines the Code Review agent passed without a finding |
| QA miss (phase 2) | Defect traced to a change QA passed |

**2. Attribute — which agent should learn.** Rules first, then a built-in **Learning Analyst** agent (`fast`/`balanced` tier) with a confidence score; the human can correct attribution.

| Correction | Agents that learn |
|---|---|
| Code change requested in PR review | Developer (made it) **and** Code Review (missed it) |
| Requirements edited / rejected at gate, or PR comment about wrong behaviour vs intent | Business Analyst |
| Design gate rejected, or review comment citing architecture/dependencies | Design |
| Missing/weak tests flagged by a human | QA (and Developer if TDD skipped) |
| Any custom agent's handoff edited or rejected | That agent |

**3. Detect repeats — proposals only after a pattern.** The Learning Analyst normalises each record into a one-line lesson and clusters similar lessons per agent and scope (repo/team). A cluster becomes a proposal when it reaches the **threshold in org/team policy (default: 3 similar corrections within 30 days)**. A user can still explicitly ask for a single correction to be taught now: `agentic learn propose --from <feedback-id>` (this counts as an explicit request).

**4. Propose — a concrete, reviewable change**, shown as a diff with the evidence (the corrections that triggered it):

| Proposal type | Target | v1 / later |
|---|---|---|
| Personal skill or personal note (lesson specific to one user's corrections) | `~/.agentic/skills/`, `~/.agentic/memory/` | v1 |
| Repo memory entry | `AGENTS.md` (shared repo knowledge) | v1 |
| Shared skill update or new skill | repo skill (`.agentic/skills/`) or team/org skill (policy repo) | v1 |
| Agent instruction addition | repo/team agent definition (`skills:` or body) | v1 |
| Regression eval case | the corrected example added to the agent's eval corpus (M1-63) | v1, always attached |
| Review checklist / pre-pass rule (e.g. ast-grep rule), shell deny rule, policy guardrail | policy repo | phase 2 (E2-05) |

**5. Approve — explicit, per proposal, by the owner of what changes (D30).** Commands: `agentic learn list | show <id> | approve <id> | edit <id> | reject <id> | defer <id>`; pending proposals are shown at the end of the run and in an email digest to the approver.

| Proposal changes… | Approved by | Developer who gave the feedback |
|---|---|---|
| Their own personal skill / note | That user | approves |
| Shared repo item (AGENTS.md, repo skill, repo agent) | Repo maintainer (applied via PR) | can **endorse** (adds weight), cannot approve |
| Team skill / team agent | Team lead (policy-repo PR) | can endorse |
| Org skill / org agent / guardrail (phase 2) | Org admin (policy-repo PR) | can endorse |

The Learning Analyst picks the narrowest sensible target: a lesson that only one user's corrections support becomes a **personal** proposal; a lesson supported by several users in one repo becomes a **repo** proposal; across a team's repos, a **team** proposal. Rejected proposals are remembered so the same lesson is not proposed again unless new evidence arrives.

**6. Apply, measure, undo.** Approved changes land as PRs; the eval case runs before and after; the agent-quality dashboard shows correction rates per agent before/after each learning; `agentic learn revert <id>` undoes it. Audit events: `learning.feedback_captured`, `.proposed`, `.approved`, `.rejected`, `.applied`, `.reverted`.

**Never installed by `init`** (checked and reported with fix instructions only): Python itself, git, WSL2, Docker/Podman, Node.js, Claude Code, Codex CLI, `srt`. `agentic init --install-missing` may install only the *user-space* ones among these (Claude Code, Codex CLI, `srt`) at the org-pinned versions from the internal mirror, after showing exactly what will be installed and asking for confirmation. Every fetch is logged in the readiness report and in the audit trail (`init.tool_fetched`).

### Repo-root and safety checks before `init` does anything

`agentic init` and `agentic init --repo` refuse (exit code 2, clear message, nothing written) unless **all** of these hold. Other commands (`run`, `status`, …) work from any subfolder by resolving the repo root themselves.

| # | Check | How | On failure |
|---|---|---|---|
| 1 | Inside a git working tree | `git rev-parse --is-inside-work-tree` = `true`; not a bare repo | "Not a git repository" |
| 2 | Current directory **is** the repo root | real path of cwd == real path of `git rev-parse --show-toplevel` (case-insensitive on Windows, symlinks/junctions resolved) | "Run from the repo root: `cd <toplevel>`" |
| 3 | Not inside an agentic-created worktree or a submodule | cwd not under any `.agentic/local/worktrees/`; `git rev-parse --show-superproject-working-tree` empty (unless the parent repo's config lists the submodule as a component) | "This is a run worktree / submodule — run init in the main repo" |
| 4 | Not a dangerous location | refuse filesystem/drive roots and the user's home directory, even if they are git repos | "Refusing to initialise <path>" |
| 5 | Repo is allowed by org policy (org mode) | `origin` remote URL matches `allowed_repo_patterns` in org policy (e.g. `github.com/acme/*`) | "Repo not approved for agentic use — ask your team lead" |
| 6 | Developer `init`: repo already set up | `.agentic/config.yaml` exists in the committed tree with a valid classification | "Repo not initialised — run `agentic init --repo` (or ask your team lead)" |
| 7 | `init --repo`: permission and clean state | user holds `agent.edit.repo` for this team; working tree clean; on the default branch | Explains the missing permission or the uncommitted changes |
| 8 | No nested setup | no other `.agentic/config.yaml` above or below the root within the same repo | "Found another .agentic at <path>" |

`init` is idempotent: re-running it only refreshes policy, skills and tools, and reports what changed.

### Daily loop

`agentic run <pipeline> …` → review handoffs at gates (`agentic status`, `approve`/`reject`/`edit`) → review and merge the PR → check own cost (`agentic cost`). Writing a custom agent = adding a Markdown file under `.agentic/agents/` in plain English; `agentic validate` checks it against policy before use.

### Getting help

`agentic doctor` (diagnoses setup), `agentic support-bundle` (redacted bundle for the platform team), docs site (quickstart, developer guide), team lead, and the pilot office hours during rollout.

### Trying it without the org setup

Standalone mode: `agentic init --standalone` with a local policy file and any TokenLean instance — no control plane, no RBAC — for evaluation or personal repos. This is exactly what M1 delivers.

### Rollout to 1000 developers (from the M3-20 pilot runbook)

| Wave | Who | Size | Exit criteria |
|---|---|---|---|
| Pilot | 2–3 volunteer teams, Python/TS repos | ~20 devs | Acceptance scenarios pass on their repos; no P1 incidents for 2 weeks; ROI baseline captured |
| Early adopters | 10 teams across stacks | ~100 devs | Gate rejection rate and cost per accepted change within targets; onboarding ≤ 15 min median |
| General availability | remaining teams, team by team | ~900 devs | Load and support metrics healthy; training material complete |

---

## Deployment landscape

### Topology (cloud-agnostic Kubernetes)

```
 Developer laptops (Win/mac/Linux)          GitHub (cloud or Enterprise)          Jira
 ┌──────────────────────────────┐   PRs,    ┌──────────────────────────────┐   ┌──────────┐
 │ agentic CLI + core           │◄─────────►│ target repos (.agentic/,     │   │ issues   │
 │ worktrees + sandbox          │  clone    │   committed index)           │   └────▲─────┘
 │ Claude Code / Codex (opt.)   │           │ org policy repo              │        │ REST
 └──┬──────────────┬────────────┘           │ Actions: nightly index (bot) │        │
    │ model calls  │ events, policy,        └──────────────┬───────────────┘        │
    │ (HTTPS)      │ kill switch (HTTPS)                   │ webhook/pull           │
    ▼              ▼                                       ▼                        │
 ┌──────────────────────────── Kubernetes cluster (per environment) ──────────────┼──┐
 │  ns: tokenlean            ns: agentic                    ns: observability     │  │
 │  ┌──────────────┐        ┌─────────────────────────┐    ┌───────────────────┐  │  │
 │  │ TokenLean    │◄───────│ control-plane API (HPA) │    │ Grafana           │  │  │
 │  │ proxy (HPA)  │ routing│ policy-sync worker      │───►│ Prometheus        │  │  │
 │  │ Redis, Qdrant│ rules  │ notifier (SMTP)         │    │ OTel collector    │  │  │
 │  │ Langfuse     │        │ retention/alert jobs    │────┼──► Jira/GitHub    ─┼──┘  │
 │  └──────┬───────┘        └────────────┬────────────┘    └─────────▲─────────┘     │
 │         │                             │                           │               │
 │         └──────────────┬──────────────┴───────────────────────────┘               │
 │                        ▼                                                          │
 │            Postgres (managed service or operator, HA, PITR backups)               │
 │            schemas: tokenlean (usage_events, audit) · agentic (runs, audit, rbac) │
 └───────────────┬───────────────────────────────────────────────────────────────────┘
                 ▼ HTTPS egress (allow-listed)
        LLM providers (Anthropic, OpenAI, Google, …) / self-hosted models
 Internal package index (Artifactory/Nexus/…): agentic CLI releases, pinned skills bundle
 Org SMTP relay: notifications
```

### Components

| Component | Runs where | Scaling / HA | Data |
|---|---|---|---|
| `agentic` CLI + core | Developer laptops, CI runners | n/a | Local `.agentic/local/` (runs, ledgers, overlay index), offline event queue |
| TokenLean, **LITE profile** (shared, one tenant, teams under it) | K8s `tokenlean` namespace | Horizontal autoscale; Redis required; Qdrant and Langfuse optional | Postgres `tokenlean` schema; Langfuse only if trace capture enabled |
| Control plane API | K8s `agentic` namespace | Stateless, ≥ 2 replicas, HPA | Postgres `agentic` schema |
| Policy-sync worker | K8s `agentic` | 1 active (leader election) | Reads policy repo; writes policy versions |
| Notifier, retention, alert/auto-trip jobs | K8s `agentic` (CronJobs/workers) | 1 active each | — |
| Grafana, Prometheus, OTel collector | K8s `observability` (or existing org stack) | Org standard | Dashboards provisioned from repo |
| Postgres | Managed (Cloud SQL / RDS / Azure DB) or operator (CloudNativePG) | HA + point-in-time recovery | Both schemas; audit tables insert-only role |
| GitHub Actions bot | GitHub | per repo | Commits nightly index |
| Internal package index | Org's existing Artifactory/Nexus/registry | Org standard | CLI wheels, SBOMs |

### TokenLean LITE deployment folder (ST-8: local part M1-67, cloud part M3-03)

Lives in the TokenLean repo (same codebase, D26), separate from the full-stack `docker-compose.yml` and GCP `infra/`:

```
d:\token-optimisation\deploy\lite\
├── README.md                     # what LITE is, prerequisites, local vs cloud quickstart
├── config/
│   └── agentic.yaml              # the LITE profile (ST-6): gateway on, behaviour-changing optimisations off
├── local/                        # developer laptop / CI / framework dev environment
│   ├── docker-compose.lite.yml   # proxy + Postgres + Redis; compose profiles `qdrant`, `langfuse` (optional)
│   ├── .env.lite.template        # provider keys, admin key, ports
│   ├── up.sh      / up.ps1       # create .env if missing, pull, start, wait for health
│   ├── down.sh    / down.ps1     # stop (keep volumes) ; --purge removes volumes
│   └── smoke.sh   / smoke.ps1    # chat + messages + responses call, checks x-tokenlean-* headers and usage row
└── cloud/                        # any Kubernetes (cloud-agnostic)
    ├── helm/tokenlean-lite/      # Chart.yaml, values.yaml, templates (Deployment+HPA, Service, Ingress,
    │                             #   ConfigMap from config/agentic.yaml, Secret refs, migration Job, Redis optional)
    ├── values/
    │   ├── dev.yaml
    │   ├── staging.yaml
    │   └── prod.yaml             # replicas, resources, external Postgres/Redis, ingress host, TLS
    ├── install.sh   / install.ps1    # helm upgrade --install with chosen env values; runs DB migrations
    ├── upgrade.sh   / upgrade.ps1
    ├── uninstall.sh / uninstall.ps1
    └── smoke.sh     / smoke.ps1      # same checks as local, against the ingress URL
```

### What goes into the TokenLean LITE stack

LITE is a profile, not a new edition: it inherits TokenLean's existing **OSS / commercial image split**. Orgs run LITE on the **OSS image by default** (D15); the same profile runs on the commercial image when an org has a commercial licence, which adds the portal and commercial APIs. The admin and usage "portal" in OSS LITE is the **framework control plane (CLI/API now, web UI in phase 3) + Grafana** (D27).

| Area | LITE on OSS image (default) | Added on commercial image |
|---|---|---|
| Gateway (OpenAI/Anthropic/Responses endpoints, routing, failover, prompt caching, teams, spend caps, `usage_events`, cost headers, G29–G31 guardrails) | ✅ | — |
| Per-user key API `/admin/keys` with user + team binding, Postgres key store (**new OSS, ST-9 / M2-34**) | ✅ | portal key screens |
| Routing-rules and team-limits admin endpoint (**new OSS, ST-10 / M2-35**) | ✅ | portal routing/tool-policy screens |
| Existing OSS admin endpoints: `/admin/tool-governance`, `/admin/alert-webhook`, `/admin/usage-export` | ✅ | — |
| Grafana dashboards (OSS set, below) | ✅ | Pitch Comparison |
| Customer portal UI, audit read API, signed attestation, GDPR tooling, FinOps, webhook delivery | ❌ (framework covers audit, alerts, dashboards) | ✅ |

**TokenLean dashboards in LITE** (provisioned into the same Grafana as the framework dashboards; a `team` variable added once ST-1 lands — ST-11 / M2-36):

| Dashboard | In LITE | Why |
|---|---|---|
| Live Calls, Requests Explorer, Billing, Latency Breakup, SLA, Trust & Safety, Trends | ✅ | Proxy health, cost, latency, guardrail events — useful to platform team and admins |
| Quarterly | ✅ | Long-range cost; savings panels show caching only while optimisations are off |
| Per Call | ✅ only when Langfuse is enabled | Depends on the `langfuse-postgres` datasource |
| Tenant Overview | ❌ | Single tenant in an org deployment; replaced by the framework's Team view |
| Pitch Comparison | ❌ | Commercial |

**Admin tasks — where each is done in OSS LITE**

| Task | Who | Done in | TokenLean side |
|---|---|---|---|
| Issue / revoke / rotate a user's proxy key | Control plane (on role assign / offboard), admins via `agentic rbac` | M2-17 / M2-18 | `/admin/keys` (ST-9, M2-34) |
| Create team, set team spend cap and limits | Org admin / team lead via policy repo | M2-02 → M2-09 | team level (ST-1, M2-30) + admin endpoint (ST-10, M2-35) |
| Tier → model routing, classification restrictions | Org admin via policy repo | M2-09 | admin endpoint (ST-10) |
| Provider API keys, TokenLean settings | Platform team | Helm values + cluster secrets (M3-03) | config / env |
| Tool allow-lists | Org admin via policy repo | enforced by framework (M1-04, SP8 rows) | `/admin/tool-governance` (read) |
| Usage and cost views | Admins, team leads, execs | Grafana (TokenLean set + framework dashboards) | `usage_events`, Prometheus |
| Budget / spend alerts | Control plane | M2-16 | events seam (delivery not needed) |
| Security events (PII, injection, guardrails) | Security | Trust & Safety dashboard + audit explorer SQL panel over TokenLean `audit_events` | existing OSS audit rows |
| Kill switch at proxy level | Control plane | E2-09 | deny rules (phase 2) |
| GDPR erasure, signed attestation | Security | phase 4, or commercial image | commercial |

The framework's umbrella chart (below) consumes `cloud/helm/tokenlean-lite` as its TokenLean sub-chart, and the framework's local `docker-compose` (M2-39) includes `local/docker-compose.lite.yml`. Both paths therefore deploy the same LITE profile.

Delivered as one **umbrella Helm chart** (`agentic-platform`) with sub-charts for control plane and TokenLean, values files per environment, and the option to point at an existing Postgres, Grafana or TokenLean.

### Environments

| | Dev | Staging | Prod |
|---|---|---|---|
| Purpose | Platform team builds and tests the framework itself | Rehearse upgrades, policy changes and new agent/skill versions with real models on test repos | All 1000 users |
| Policy repo branch tracked | `dev` | `staging` | `main` |
| TokenLean | Own instance, cheap tiers only | Own instance, prod model mapping, low caps | Shared org instance |
| Data | Synthetic | Test repos, short retention | Real; retention per policy |
| Users | Platform team | Platform team, org admins, pilot team leads | Everyone |
| CLI version | Pre-release | Release candidate | Released; `min_cli_version` enforced |

**Promotion flows**

- **Framework upgrades**: release candidate → staging (acceptance suite + load test) → prod; then raise `min_cli_version` after a grace period.
- **Policy changes**: PR to `staging` branch → staging control plane validates and applies → smoke runs → PR `staging` → `main` → prod applies; every run records the policy version.
- **Agent / skill versions**: published in the policy repo → eval gate (M1-63) → rolled out team by team in prod (`rollout:` list).

### Network and data flows

| From → To | Protocol | Carries |
|---|---|---|
| CLI → TokenLean | HTTPS (Anthropic/OpenAI-compatible) | Prompts, X-Agentic-* headers, traceparent |
| CLI → control plane | HTTPS (REST) | Events, audit entries, policy fetch, kill-switch polls, heartbeats |
| CLI → GitHub / Jira | HTTPS | Clone/push branches, open PRs, read issues, post comments |
| Control plane → TokenLean | HTTPS (admin API) | Routing rules from policy; (phase 2) kill-switch deny rules |
| Control plane → policy repo | HTTPS (git pull / webhook) | Policy versions |
| TokenLean → LLM providers | HTTPS (egress allow-list) | Model calls |
| Grafana → Postgres / Prometheus | SQL / PromQL | Dashboards |

### Operations (platform team)

Backups and PITR for Postgres (daily restore drill in staging); monitoring via control-plane health metrics and alert rules (M2-12); upgrades via Helm with migration jobs; secrets via the cluster's secret manager (External Secrets / Sealed Secrets); TLS via cert-manager; runbooks for kill switch, key revocation, restore and upgrade (M3-02, M3-18).

### Sizing (starting point, validated by the M3-22 load test)

Control plane: 2–3 replicas × 1 vCPU / 1 GB; Postgres: 4 vCPU / 16 GB with HA standby; TokenLean: per its own sizing guidance, scaled on request rate; observability: org standard. Revisit after the pilot wave.

---

## Executive cost breakdown (owner request, part of M3-01)

The executive report shows total cost decomposed into drivers, for the **whole org** and for **each user** (and filterable by team, pipeline, agent and month):

```
total cost = users × sessions per user × turns per session × requests per turn × tokens per request × cost per token
```

| Factor | Definition in this framework | Computed as | Source |
|---|---|---|---|
| users | People who ran ≥ 1 pipeline in the period (per-user view: 1) | count of distinct run owners | control plane `runs` |
| sessions per user | One session = one pipeline run | runs ÷ users | control plane `runs` |
| turns per session | One turn = one agent step executed (each loop iteration and each child agent counts) | step executions ÷ runs | run ledger `step_started` events (ingested) |
| requests per turn | One request = one model API call | model calls ÷ step executions | TokenLean `usage_events` joined by `tags.run_id` + `tags.step` (ST-2) |
| tokens per request | Input + output + cache-read + cache-write tokens per call (also shown split) | tokens ÷ model calls | TokenLean `usage_events` |
| cost per token | Effective blended USD rate (varies with model mix and cache hits) | actual cost ÷ tokens | TokenLean `cost_actual_usd` |

**Reconciliation rule:** every factor is a ratio of measured totals, so the product equals the actual total cost exactly; the report shows `product of factors` next to `sum of actual cost` and flags any difference > 0.5 % (a data-quality alert, e.g. untagged calls). Calls without run tags are reported as a separate "unattributed" line, never hidden.

**Why this shape:** it tells an executive *which lever moved* — more users (adoption), more runs per user (usage), longer runs (pipeline design / review loops), chattier agents (prompt/skill quality), bigger prompts (context size), or pricier tokens (model mix, cache misses).

**Also shown:** month-over-month change attributed to each factor; per-user table sortable by any factor; a **what-if** panel (e.g. "users = 1000, everything else at current median" → projected monthly cost); per-model split of cost per token; cached-token share.

## Enterprise coverage review (1000-person rollout)

Each function the owner asked for, mapped to the tracker rows that deliver it.

| Enterprise function | Delivered by |
|---|---|
| **Observability** — who/which team/agent did what and why, sessions, turns, tokens, cost | M1-12/13 cost capture and budgets; M1-43 OTel + TokenLean trace join; M1-47 structured logs + `agentic logs`; M2-20 central sinks; M2-37/38 dashboards; M2-31/33 (ST-2/ST-5); M2-12 control-plane health/alerts |
| **Audit** — tamper-evident, code + config, who/what/why | M1-06/07 hash-chained ledger; M1-22 digest-pinned approvals; M1-45 git trailers; M2-05 chained audit table; M2-26 `audit verify`; M2-02 policy versions; M2-13 audit export; M2-55 tamper E2E |
| **Executive dashboards / ROI** | M3-01 Executive ROI dashboard + chargeback export + cost-driver breakdown per org and per user (users × sessions × turns × requests × tokens × cost/token) (cost per accepted change, request→merged-PR cycle time, DORA lead time and change-failure rate via Zyroflo `metrics.dora`, rework rate, PR merge rate, human wait time, TokenLean savings, monthly cost per team) |
| **Roles** | M2-03 RBAC + default roles + repo maintainers from CODEOWNERS; M2-25 CLI; M1-22 gate roles/four-eyes; M2-14 service accounts + bulk import; M2-18 provisioning |
| **Guardrails** | M1-04 tool/tier/backend/classification enforcement; M1-31 shell policy + Zyroflo guards; M1-41 secret scan before egress; M1-42 untrusted content; M2-48 pre-pass + SCA + blocked licences; M2-49 handoff PII scan; TokenLean G29–G31 |
| **Safe execution** | M1-30 worktree per step; M1-32/33 OS sandbox; M2-42 egress limits; M3-10 resource limits; M2-24 diff budget; M1-54 `--dry-run`; P0-03 architecture test |
| **Kill switch** | M2-19 per-step/turn check with pause-when-stale; M2-06 scoped switch + audit; M2-10 maintenance pause; M2-11 auto-trip; M2-25 CLI; M2-54 ≤ 30 s propagation test; E2-09 TokenLean deny rules |
| **No runaway process** | M1-17 child limits; M1-18 stuck detector; M1-13 budgets; M1-24 loop caps; M1-26 timeouts; M2-22 process-group cleanup; M2-08 concurrency; M2-16 budget alerts; M3-07 heartbeat + zombie detection; M3-08 `agentic gc` |
| **Provider agnostic** | D2 TokenLean-only calls; M1-03/04 tiers; M1-27 capability matrix; M2-09 routing generation; P0-03 architecture test |
| **Agent quality** | M1-59 golden handoffs + eval corpus; M1-63 eval harness; M1-64 agent quality gate; M2-46 skill eval; M3-11…16 learning loop; M2-38 agent-quality dashboard |
| **Operating at 1000 seats** | M2-15 versioned rollout; M2-08 min CLI version; M2-20 offline queue; M3-02/03/04 Helm + environments; M3-05 day-0 bootstrap; M2-17 key rotation; M3-21 release engineering; M3-09 `doctor` + support bundle; M3-20 pilot runbook; M3-22 load test; M3-23 onboarding kit |

## Review Focus

1. **Windows line endings / paths in digests and ledger** — a handoff checked out with CRLF must produce the same digest as LF, or approvals break across OSes. Expect stable digests (normalise via `.gitattributes eol=lf` for `.agentic/**` and hash bytes as stored). Test owned by M1-06.
2. **Process killed mid-write** (laptop sleep, Ctrl-C) during `run.json` or ledger append — expect atomic writes (temp file + replace) and a resumable run with an intact chain. Tests owned by M1-21 and M1-06.
3. **Control plane unreachable in org mode** — expect events queued locally and replayed idempotently later, and runs to **pause** (not continue) once kill-switch state is older than the cache expiry. Tests owned by M2-20 and M2-19.
4. **Streamed model responses without TokenLean cost headers** — expect the budget ledger to fall back to the provider usage chunk, mark cost `estimated`, and still enforce limits. Test owned by M1-12.
5. **Human edits a handoff at a gate and breaks its front matter** — expect a precise validation error, approval refused, and the original handoff left untouched. Tests owned by M1-05 (parsing) and M1-22 (gate flow).

---

## Tracker

Status for every row: **Pending** at plan approval. Columns: `PW` = estimated person-weeks (±50 %); `Done when` is the acceptance check for the row; `Notes` records the completing commit SHA.

### Phase 0 — Bootstrap and spikes (~15 PW, ~4 weeks)

| ID | Sub-plan | Item | Depends on | Done when | PW | Status | Notes |
|---|---|---|---|---|---|---|---|
| P0-01 | SP0 | Repo bootstrap: git init, uv workspace (`core`, `commons`, `cli`, `control-plane`), ruff, pytest, pre-commit, CI matrix (Windows/macOS/Linux), MIT LICENSE, `.gitattributes eol=lf`; copy spec, research, this plan and `TRACKER.md` into the repo | — | `uv run pytest` and `uv run ruff check .` green locally and in CI on all 3 OSes; tracker committed | 1 | Pending | |
| P0-02 | SP0 | Spec revision **r3**: fold in decisions D26–D33 (D31 agent deny lists, D32 opencode backend + Antigravity spike, D33 pipelines admin-controlled), the enterprise-review additions, and the "Users and roles", "Developer onboarding", "Deployment landscape", skills, learning-loop and executive cost breakdown sections from this plan; renumber spec §21 spikes to match | P0-01 | Spec r3 committed; every D-number and spike referenced in this plan exists in the spec; owner sign-off | 1 | Pending | |
| P0-03 | SP0 | Architecture test (import-linter): `openai`/`anthropic` SDKs importable only from `agentic.transport`; supply-chain CI: pip-audit, gitleaks on the repo, licence allow-list check of `uv.lock` | P0-01 | CI job fails on a deliberate violation of each rule and passes on main | 0.5 | Pending | |
| S1 | Spike | TokenLean cost capture through Pydantic AI (stream + non-stream) | P0-01 | Record shows headers/body read on both paths or names the gap and the fallback | 1 | Pending | |
| S2 | Spike | Claude Agent SDK end-to-end through TokenLean (managed settings, base URL, headers, `--bare`, JSON schema output) | P0-01 | Record with a working lock-down config or the blocking gap | 1 | Pending | |
| S3 | Spike | Codex `exec` through a TokenLean Responses endpoint stub | P0-01 | Record naming required TokenLean work for ST-4 | 1 | Pending | |
| S4 | Spike | Archon / OpenHands behind a proxy — re-confirm build-vs-adopt | P0-01 | Record; D1/D17 confirmed or spec amended | 1 | Pending | |
| S5 | Spike | Code-graph bake-off (code-review-graph, codebase-memory-mcp, Graphify, Zyroflo domain-graph) with corpus replay + judging protocol, Windows + Linux, Python/TS/Java repos | P0-01 | Record names the default symbol adapter with recall@20 and precision@10 per tool | 2 | Pending | |
| S6 | Spike | Windows sandbox strength: `srt` vs Codex sandbox vs WSL2 | P0-01 | Record names the Windows default and what it cannot block | 1 | Pending | |
| S7 | Spike | ACP adapter stability (phase-2 input) + **Antigravity CLI** check (D32): ACP support, routing through TokenLean (custom base URL), licence/terms, Windows support | P0-01 | Record: ACP adapter verdict; Antigravity usable as `acp:antigravity`, needs a dedicated adapter, or not viable | 1 | Pending | |
| S8 | Spike | Pydantic AI deferred-tool serialisation across processes (gate pause/resume) | P0-01 | Record with a resumed run across two processes or the gap | 0.5 | Pending | |
| S9 | Spike | Committed shard index in `main`: size growth, merge behaviour, LFS need | P0-01 | Record with numbers from 2 real repos | 0.5 | Pending | |
| S10 | Spike | Skill and agent A/B method (judging protocol, blinding, sample size) | P0-01 | Record + eval harness sketch used by M1-63 | 1 | Pending | |
| S11 | Spike | Serena (GPL) as separate process — legal (only if multilspy insufficient) | S5 | Record (may be `Dropped`) | 0.25 | Pending | |
| S12 | Spike | Docker Desktop licensing at 1000 seats; Podman alternative | — | Record with recommendation | 0.25 | Pending | |
| S13 | Spike | Safe expression evaluator: cel-python vs simpleeval | P0-01 | Record naming the choice with licence | 0.5 | Pending | |
| S14 | Spike | Zyroflo reuse packaging: shared `commons` package (Zyroflo stays stdlib-only) vs vendoring; slice plan for `policy.py` guard rules | P0-01 | Record with package boundaries, Zyroflo impact and a dry-run extraction of `entry_hash` | 1.5 | Pending | |
| P0-04 | SP0 | Spike review: go/no-go, re-estimate M1, spec amendments if any decision changed | S1–S14 | Decisions recorded in spec revision line; M1 rows re-estimated; owner sign-off | 0.5 | Pending | |

### M1 — Standalone core (~70 PW, ~17 weeks)

Exit: acceptance scenarios 1, 2 and 4 pass in **standalone mode** on a laptop (M1-68…70), cross-platform CI green (M1-71), **agent quality gate passed** (M1-64), demo and go/no-go (M1-72).

| ID | Sub-plan | Item | Depends on | Done when | PW | Status | Notes |
|---|---|---|---|---|---|---|---|
| M1-01 | SP1 | `AgentDefinition` model + Markdown loader, incl. optional `deny:` block (`shell` command prefixes, `paths` globs) — D31 | P0-01 | `core/tests/definitions/test_agent.py` passes (5 tests in detailed plan) | 0.5 | Pending | |
| M1-02 | SP1 | `PipelineDefinition` model + YAML loader | P0-01 | `test_pipeline.py` passes (5 tests) | 0.5 | Pending | |
| M1-03 | SP1 | `OrgPolicy` model + policy-dir loader + policy version (incl. `allowed_repo_patterns`, `tool_mirror_url`, `licence_scan`, `allow_repo_pipelines` (D33, default false)) | P0-01 | `test_policy_load.py` passes (6 tests) | 1 | Pending | |
| M1-04 | SP1 | Layered discovery + `resolve_agent` (tiers, team narrowing, clamping, violations; deny lists = union of org + team + agent, deny beats allow; repo-level pipelines ignored with a warning unless `allow_repo_pipelines` — D33) | M1-01, M1-03 | `test_resolve.py` + `test_discovery.py` pass (8 tests) | 1 | Pending | |
| M1-05 | SP1 | Handoff envelope + kind models + parser + produces/consumes compatibility | M1-01, M1-02 | `test_handoffs.py` passes (6 tests incl. Review Focus 5) | 1 | Pending | |
| M1-06 | SP2 | Commons ledger core: `entry_hash`, chain append/verify, `digest_of`, DSSE sign/verify (from Zyroflo `approve.py`/`events.py`/`state.py`) | S14 | Ported Zyroflo ledger tests pass; CRLF-vs-LF digest equality test; kill-mid-append leaves chain verifiable | 1.5 | Pending | |
| M1-07 | SP2 | Event types extended with agentic fields (team, agent, parent, tier, policy_version; limit/kill/skill events); JSON schema generated | M1-06 | Schema file generated from code; unknown field rejected; Zyroflo event fixtures still validate | 0.5 | Pending | |
| M1-08 | SP2 | Guard rules from `policy.py` L3215–4105: `deny_commands` tokeniser, protected paths, push target, diff budget | S14 | Ported guard tests pass incl. `sudo`/`xargs`/PowerShell unwrapping cases | 1 | Pending | |
| M1-09 | SP2 | Worktree manager from `worktree.py` with components passed as argument | S14 | Ported tests pass on 3 OSes; rollback on partial creation verified | 1 | Pending | |
| M1-10 | SP2 | Lifecycle content as data: P0–P11 SOPs, LIFECYCLE, STEP_SPEC, review rubric, gate-regime table | S14 | Loader returns all 12 stages with front-matter fields; rubric weights sum to 10 | 0.5 | Pending | |
| M1-11 | SP3 | httpx client factory stamping `X-Agentic-*` + `traceparent`; forbidden-header guard | M1-04 | Tests: all 10 headers present; sending `X-Step-Name`/`X-Workflow-ID`/`X-Session-ID` raises | 0.5 | Pending | |
| M1-12 | SP3 | Cost capture: `x-tokenlean-*` headers, body `_token_opt`, stream usage-chunk fallback marked `estimated` | M1-11, S1, M1-65 | Tests on recorded responses: exact cost on non-stream; `estimated` on stream without headers (Review Focus 4) | 1 | Pending | |
| M1-13 | SP3 | Budget ledger: run → step → child slices, reservations, limit-hit signals | M1-12 | Tests: child cannot exceed slice; step limit hit raises `LimitHit(kind="budget")`; totals reconcile | 1 | Pending | |
| M1-14 | SP3 | Corporate network: `HTTPS_PROXY`/`NO_PROXY` and custom CA bundle honoured by every HTTP client | M1-11 | Test with a local proxy and self-signed CA passes | 0.5 | Pending | |
| M1-15 | SP4 | `AgentRunner` interface + Pydantic AI implementation; identity headers and budget wired in | M1-04, M1-11, S8 | Tests with `TestModel`: run completes, usage recorded, pause/resume across processes | 2 | Pending | |
| M1-16 | SP4 | Agent tools: read, search, edit, shell (via sandbox), codeintel, `activate_skill` | M1-15 | Each tool has a unit test; tool not in allow-list is invisible to the agent | 1.5 | Pending | |
| M1-17 | SP4 | Child spawner: count / concurrency / depth / budget / turns / time limits, narrowing, partial results; child deny = parent deny ∪ child's own (never smaller) | M1-15, M1-13 | One test per limit; child with wider tools than parent refused; partial result carries `stopped:` reason | 1.5 | Pending | |
| M1-18 | SP4 | Stuck detector (port of OpenHands `StuckDetector` with attribution) + review/developer oscillation check | M1-15 | One test per pattern (repeat, error loop, ping-pong, monologue, oscillation) | 1 | Pending | |
| M1-19 | SP5 | Handoff body templates per kind (spec-kit / Zyroflo / MADR adapted) | M1-05 | Templates render for all 5 kinds and parse back valid | 1 | Pending | |
| M1-20 | SP5 | Output validation retry loop (errors fed back once, then gate) | M1-19, M1-15 | Test: invalid output → retry with errors → valid; twice invalid → gate | 0.5 | Pending | |
| M1-21 | SP6 | Pipeline state machine, run folder, atomic `run.json`, resume from last completed step | M1-04, M1-06 | Tests: every transition; kill-mid-write (Review Focus 2) resumes cleanly | 2 | Pending | |
| M1-22 | SP6 | Gates: regimes (solo/team/regulated), four-eyes, digest-pinned approvals in ledger, approve/reject/edit | M1-21, M1-06 | Tests: approval pins digests; edited handoff invalidates approval; broken edit refused (Review Focus 5); four-eyes blocks self-approval | 1.5 | Pending | |
| M1-23 | SP6 | `when`/`until` expression evaluator (S13 choice) over handoff front-matter fields | S13 | Tests: field access, comparisons, no attribute/import escape | 0.5 | Pending | |
| M1-24 | SP6 | Loops (`until`, `max_iterations`, `on_exhausted`) with Zyroflo review exit rule | M1-21, M1-23 | Tests: loop ends on `open_blocking == 0`; exhaustion escalates to gate | 1 | Pending | |
| M1-25 | SP6 | Preflight, standalone checks (tier/model resolve, backend installed, tools/skills within policy, limits clamped, handoff compatibility) with readable report | M1-21, M1-04 | One test per check; report names the failing step and policy value | 1 | Pending | |
| M1-26 | SP6 | Step and run wall-clock timeouts → pause for human | M1-21, M1-13 | Tests with fake clock: step and run timeouts pause with reason | 0.5 | Pending | |
| M1-27 | SP7 | Backend protocol + capability matrix (ignore/warn/block for unhonoured settings) | M1-15 | Tests: temperature on claude-code → policy `block` refuses, `warn` logs | 0.5 | Pending | |
| M1-28 | SP7 | `native` backend | M1-27 | Conformance tests pass with `TestModel` | 0.5 | Pending | |
| M1-29 | SP7 | `claude-code` backend (Claude Agent SDK, locked settings, JSON schema output) | M1-27, S2 | Contract tests against pinned Claude Code version pass; base URL forced to TokenLean | 1.5 | Pending | |
| M1-30 | SP8 | Worktree-per-step integration | M1-09, M1-21 | Test: each code step runs in its own worktree branched from the previous result; discard = rollback | 0.5 | Pending | |
| M1-31 | SP8 | Shell-policy format + compiler to Claude Code permission format + Zyroflo guards; enforces the resolved deny lists (shell prefixes refused, denied paths unwritable) on every backend | M1-08, M1-29 | Tests: rule file compiles; destructive command refused; protected path write refused | 1 | Pending | |
| M1-32 | SP8 | `srt` sandbox wrapper (Linux/macOS) | S6, M1-30 | Tests (skipped on Windows only): egress to non-allowed host blocked; write outside worktree blocked | 1 | Pending | |
| M1-33 | SP8 | Windows sandbox path (S6 choice) + detection and guidance in `init` | S6, M1-30 | Windows CI: sandbox detected and enforced, or `init` reports exactly what to install | 1.5 | Pending | |
| M1-34 | SP9 | `codeintel` interface + agent tools (dependents, impact, tests_for, path, overview, context_pack) | M1-16 | Tests with a fake adapter cover every tool | 1 | Pending | |
| M1-35 | SP9 | Symbol-level adapter (S5 winner) | M1-34, S5 | Adapter tests on fixture repo: dependents and tests_for return expected sets | 1.5 | Pending | |
| M1-36 | SP9 | Index layout `.agentic/index/`, local overlay, `.gitattributes`, freshness check | M1-35, S9 | Tests: stale shard detected; overlay never written to committed path | 1 | Pending | |
| M1-37 | SP10 | Skills catalog format, hash pinning, installer; selection = agents in enabled pipelines + opt-in packs + authoring skills for admin roles; shared skills read-only with restore + `skills.tamper_restored` | M1-04 | Tests: developer never receives authoring skills; opt-in pack absent unless enabled; edited shared skill restored and logged | 1.5 | Pending | |
| M1-38 | SP10 | Skill loader: catalog injection, `activate_skill`, pre-load for weak models | M1-37, M1-16 | Tests: catalog ≤ 100 tokens/skill; activation outside allow-list refused; pre-load path for local models | 1 | Pending | |
| M1-39 | SP10 | Skill admission checks (licence, `skills-ref validate`, size, script scan) | M1-37 | Tests: each check rejects a bad fixture | 1 | Pending | |
| M1-40 | SP10 | Vendor default skills per role + NOTICE; exclusions per spec §11.4; telemetry stripped | M1-37, M1-39 | Catalog validates; excluded skills absent; grep finds no telemetry URLs | 1 | Pending | |
| M1-41 | SP11 | Secret scan before egress (gitleaks/detect-secrets), block/mask per policy + audit event | M1-11 | Tests: planted secret blocked before any HTTP call; masked variant passes | 1 | Pending | |
| M1-42 | SP11 | Untrusted-content wrapping for repo/Jira/web/tool output | M1-16 | Tests: injected instruction in a file is labelled data; prompt structure intact | 0.5 | Pending | |
| M1-43 | SP12 | OTel spans with pinned `gen_ai.*` semconv; TokenLean `X-Trace-ID` stored beside span | M1-11 | Tests: span attributes match pinned schema; trace id recorded | 1 | Pending | |
| M1-44 | SP12 | Local sink (run folder) for events/audit; sink interface for M2 | M1-07 | Tests: every engine event lands in `events.jsonl` with valid chain | 0.5 | Pending | |
| M1-45 | SP12 | Git commit trailers + PR body run/handoff links | M1-22 | Tests: commits carry all 6 trailers; PR body links run and handoffs | 0.5 | Pending | |
| M1-46 | SP12 | `AGENTS.md` read + propose-update in handoffs | M1-16 | Tests: agent reads nearest AGENTS.md; proposal field populated, file untouched | 0.5 | Pending | |
| M1-47 | SP12 | Structured per-step logs (JSON, levels, secret redaction) + `agentic logs <run> [--step]` | M1-21, M1-41 | Tests: redaction of planted secret; log per step readable via CLI | 1 | Pending | |
| M1-48 | SP13 | Credential provider (env → OS keyring) | P0-01 | Tests: env wins in CI; keyring used otherwise; missing key gives actionable error | 0.5 | Pending | |
| M1-49 | SP14 | CLI skeleton, config, output conventions, exit codes | M1-04 | `agentic --help` lists commands; exit-code table tested | 0.5 | Pending | |
| M1-50 | SP14 | Repo-root and safety guard (8 checks) shared by `init` commands | M1-49, M1-03 | One test per check on Windows, macOS, Linux | 1 | Pending | |
| M1-51 | SP14 | Hash-verified tool fetcher from org mirror (Zyroflo `third_party.py` pattern): gitleaks, osv-scanner, ast-grep, symbol adapter, ScanCode; `init.tool_fetched` event | M1-49, M1-03 | Tests: bad hash refused; offline uses cache; mirror URL from policy | 1 | Pending | |
| M1-52 | SP14 | `agentic init --repo`: shared files on a new branch + PR (no `.agentic/pipelines/` in org mode unless policy allows it — D33) | M1-49, M1-36, M1-50 | Tests on temp repos: PR branch contains exactly the allowed files; refusal per guard case | 1 | Pending | |
| M1-53 | SP14 | `agentic login` + `agentic init` (prereqs, policy cache, skills, tool fetch, index overlay, readiness report; `--install-missing`; `--standalone`) | M1-49, M1-48, M1-37, M1-33, M1-51, M1-50 | Tests: init writes nothing outside `~/.agentic` and `.agentic/local`; readiness report lists every check | 1.5 | Pending | |
| M1-54 | SP14 | `run` (incl. `--dry-run`), `resume`, `status`, `runs` | M1-49, M1-21, M1-22, M1-24, M1-25, M1-26 | Tests: dry-run spends nothing and prints resolved plan; resume continues a paused run | 1.5 | Pending | |
| M1-55 | SP14 | Gate UX: `approve`, `reject --feedback`, `edit` ($EDITOR), Rich display of handoff + diff | M1-54, M1-22 | Tests: each verb updates ledger; edit re-validates before approval | 1 | Pending | |
| M1-56 | SP14 | `validate`, `agents|pipelines|skills list/search`, `agents new <name>` (copies `templates/agent/agent.md`), `pipelines new <name>` (copies `templates/pipeline/pipeline.yaml`), `policy show` | M1-49 | Tests: validate reports policy violations with field names | 0.5 | Pending | |
| M1-57 | SP14 | `cost` (run/step/agent/child breakdown) | M1-54, M1-13 | Test: totals match ledger to the cent | 0.5 | Pending | |
| M1-58 | SP17 | GitHub adapter: branch push, open PR with run links, CI status watch (Zyroflo `release.py` flow) | M1-45 | Contract tests against a test repo pass | 1 | Pending | |
| M1-59 | SP18 | **Agent quality assets**: 3 golden handoffs per kind; eval corpus harvested from 2 pilot repos (Zyroflo `corpus_harvest`) with ≥ 20 scoping, 20 review and 10 defect cases | M1-05, M1-19 | Corpus committed under `library/eval/`; every golden handoff parses; case counts met | 1.5 | Pending | |
| M1-60 | SP18 | Built-in agents BA, Design, Developer, Code Review, QA — instructions from Zyroflo SOPs (P0–P6, P10), rubric-based review, baseline-tests-at-intake | M1-04, M1-10, M1-40 | Definitions validate against policy; smoke tests with `TestModel` produce schema-valid handoffs | 2 | Pending | |
| M1-61 | SP18 | Pipelines `feature-delivery`, `bugfix` | M1-60, M1-24 | Both validate and dry-run; compatibility check clean | 0.5 | Pending | |
| M1-62 | SP18 | Standalone policy template (`~/.agentic/policy.yaml`) with default tiers, limits, classifications | M1-03 | Template loads; `init --standalone` uses it | 0.5 | Pending | |
| M1-63 | SP18 | **Agent eval harness**: corpus replay + judging protocol; per-agent metrics (schema validity, rubric score, tests-pass rate, defect recall, cost); runs in CI on fixture corpus | M1-59, M1-60, S10 | `agentic eval run --agent <x>` produces a report; CI job runs it on the fixture corpus nightly | 1.5 | Pending | |
| M1-64 | SP18 | **Agent quality gate** (initial targets, recalibrated at M1 retro): all agents 100 % schema-valid after ≤ 1 retry; BA & Design median blind rubric ≥ 7/10 over 20 cases; Developer ≥ 50 % eval tasks pass tests first attempt, ≥ 75 % after one review loop; Code Review ≥ 70 % recall of seeded defects with ≤ 30 % false findings; QA catches ≥ 80 % of seeded failing criteria; median cost per run recorded as baseline | M1-63, M1-67 | Gate report committed with every metric vs target; owner signs off or scope/retro adjusts targets | 1 | Pending | |
| M1-65 | SP19 | TokenLean **ST-3**: `x-tokenlean-cost-usd` header + cost on streams (final SSE event / trailer) | — | TokenLean tests: header on non-stream; final cost event on stream; existing suite green | 1.5 | Pending | |
| M1-66 | SP19 | TokenLean **ST-6** LITE profile (initial): `config/profiles/agentic.yaml` — gateway on; compression, G05 step cache, G11, G12, G10 off; Qdrant/Langfuse optional | M1-65 | Profile boots with Postgres + Redis only; byte-faithful pass-through test except caching | 1 | Pending | |
| M1-67 | SP19 | TokenLean **ST-8 (local)**: `deploy/lite/local/` compose + `up/down/smoke` (bash + PowerShell) | M1-66 | `up` + `smoke` pass on Windows, macOS, Linux | 1 | Pending | |
| M1-68 | SP20 | **Acceptance 1 (standalone)**: `feature-delivery` on the sample existing repo — gates, review loop, worktree, PR | all M1 rows above | Scripted E2E passes on CI with recorded model responses and once live via local TokenLean | 1 | Pending | |
| M1-69 | SP20 | **Acceptance 2**: custom plain-English agent ("Docs Writer") added to the pipeline without code | M1-68 | E2E passes; agent file is the only change | 0.5 | Pending | |
| M1-70 | SP20 | **Acceptance 4**: same pipeline on a new empty repo | M1-68 | E2E passes from `git init` + `agentic init --repo --standalone` | 0.5 | Pending | |
| M1-71 | SP20 | Cross-platform CI green for M1 scope (CLI, sandbox, init, E2E) | M1-68, M1-69, M1-70 | CI matrix green 3 consecutive runs | 1 | Pending | |
| M1-72 | SP20 | **M1 demo + retro**: live demo to owner; agent quality gate result; re-estimate M2; decide scope levers | M1-64, M1-71 | Retro notes committed; M2 rows re-estimated; go/no-go recorded | 0.5 | Pending | |
| M1-73 | SP20 | **Call-flow documentation** (owner request): `docs/architecture/call-flow.md` — Mermaid sequence diagram + step table of a full `feature-delivery` run from `agentic run` to the final PR/ledger entry, with every default step, gate, loop, policy/preflight check, kill-switch check, budget ledger update, sandbox, TokenLean call and audit event; each box marked as implemented (file:function) or planned (tracker row). Draft after M1-05; updated as components land; final at M1-72 | M1-05 | Draft committed after SP1; final version matches the running system at the M1 demo | 1 | Pending | |

### M2 — Org governance (~55.5 PW, ~13–14 weeks)

Exit: acceptance scenario 3 (admin override) on the org stack (M2-53), kill-switch propagation ≤ 30 s (M2-54), audit verify + export with tamper detection (M2-55), demo and go/no-go (M2-56).

| ID | Sub-plan | Item | Depends on | Done when | PW | Status | Notes |
|---|---|---|---|---|---|---|---|
| M2-01 | SP15 | Control-plane skeleton: FastAPI, Postgres, Alembic, token auth, OpenAPI | P0-01 | API tests; `alembic upgrade head` on empty DB; auth rejects bad token | 1.5 | Pending | |
| M2-02 | SP15 | Policy sync from git policy repo: pull/webhook, schema validation, versioning, serve; tracked branch per environment | M2-01, M1-03 | Tests: invalid policy rejected and previous version kept; CLI receives version id; branch configurable | 1.5 | Pending | |
| M2-03 | SP15 | RBAC: roles, permissions, assignments (team scope), default roles seed (incl. Executive viewer), Repo maintainer derived from CODEOWNERS, check API | M2-01, M1-58 | One test per permission-matrix cell; CODEOWNERS change updates maintainers | 2 | Pending | |
| M2-04 | SP15 | Ingest API for run/step/gate/audit events with chain verification and idempotency | M2-01, M1-06, M1-07 | Tests: broken chain rejected; duplicate replay is a no-op | 1.5 | Pending | |
| M2-05 | SP15 | Hash-chained audit table per tenant + insert-only DB role (TokenLean `audit/enforcement.py` pattern) | M2-01, M1-06 | Tests: UPDATE/DELETE denied for app role; chain verifies | 1 | Pending | |
| M2-06 | SP15 | Kill-switch API: scopes (global/team/user/repo/pipeline/agent/model), who, reason, audit | M2-03, M2-05 | Tests: scope matching; team lead limited to own team; every change audited | 1 | Pending | |
| M2-07 | SP15 | Email notifications (gate waiting, budget, invites) via org SMTP relay | M2-01 | Tests with a capture SMTP server | 0.5 | Pending | |
| M2-08 | SP15 | Min CLI version + per-user/team concurrency caps | M2-01, M2-03 | Tests: old CLI refused with upgrade message; cap enforced | 0.5 | Pending | |
| M2-09 | SP15 | TokenLean routing-rule and team-limit generation from tiers/classifications/team policy, applied via ST-10 | M2-02, M2-30, M2-35 | Tests: generated rules match fixture; applied and read back from TokenLean | 1 | Pending | |
| M2-10 | SP15 | Graceful pause ("maintenance mode": finish current turn, checkpoint, hold) distinct from hard kill; resume | M2-06, M2-19 | Tests: paused run resumes without loss; killed run does not | 1 | Pending | |
| M2-11 | SP15 | Auto-trip rules: team spend-rate or error-rate above threshold → team pause + alert + audit event | M2-10, M2-07, M2-37 | Tests with synthetic spend series trip and reset correctly | 1 | Pending | |
| M2-12 | SP15 | Control-plane health endpoints, Prometheus metrics, alert rules (ingest lag, queue depth, errors) | M2-01 | Tests; `promtool check rules` passes | 1 | Pending | |
| M2-13 | SP15 | Audit export API + CLI (`agentic audit export --team --from --to`, JSONL/CSV, chain proof) | M2-05, M2-26 | Tests: export verifies independently with the proof | 0.5 | Pending | |
| M2-14 | SP15 | Service accounts (team-scoped keys, `run.trigger` only, expiry) + bulk role import from CSV | M2-03, M1-48 | Tests: service account cannot approve gates; CSV import idempotent | 1 | Pending | |
| M2-15 | SP15 | Versioned agents/pipelines/skills: team-by-team rollout, pinning, deprecation warn → block | M2-02 | Tests: team not in `rollout:` keeps old version; deprecated blocks after date | 1 | Pending | |
| M2-16 | SP15 | Budget threshold alerts at 80 % / 100 % for run and team (email; webhook seam) | M2-07, M1-13 | Tests: one alert per threshold crossing | 0.5 | Pending | |
| M2-17 | SP15 | Proxy-key rotation and revocation flow via TokenLean `/admin/keys`; CLI prompts re-auth | M1-48, M2-03, M2-34 | Tests: revoked key rejected within TTL; CLI message actionable | 0.5 | Pending | |
| M2-18 | SP15 | User provisioning: role assignment → invite email with one-time link for proxy key + control-plane token; offboarding revokes both | M2-03, M2-07, M2-17, M2-34 | Tests: link single-use; offboarded user cannot run | 1 | Pending | |
| M2-19 | SP6 | Kill-switch checks per step and per turn in the engine; pause when switch state older than cache expiry | M1-21, M2-06 | Tests incl. Review Focus 3 (stale state pauses) | 1 | Pending | |
| M2-20 | SP12 | Control-plane sink with offline queue and idempotent replay; personal-skill/memory sync | M1-44, M2-04 | Tests: events queued offline replay once; order preserved (Review Focus 3) | 1.5 | Pending | |
| M2-21 | SP6 | Preflight org checks: RBAC, kill switch, CLI version, concurrency, team budget remaining, repo classification; **pipeline integrity (D33)**: the pipeline run must byte-match the approved version from the synced policy (hash), else refused | M1-25, M2-03, M2-08 | One test per check; a locally edited pipeline is refused with the approved version's hash in the message | 0.5 | Pending | |
| M2-22 | SP6 | Process-group cleanup: backend child processes terminated on stop/kill/timeout (POSIX groups, Windows job objects) | M1-21, M1-29 | Tests on 3 OSes: no orphan survives a kill | 1 | Pending | |
| M2-23 | SP6 | Pipeline overlays, org-required steps/gates, gate re-anchoring; overlays live in the policy repo (`teams/<team>/pipelines/overlays/<repo>.yaml`) so only team leads / org admins can change them (D33) | M1-22, M2-02 | Tests: overlay cannot remove required gate; removed step re-anchors gate | 1 | Pending | |
| M2-24 | SP6 | Diff-size budget per review batch | M1-08, M1-22 | Tests: Developer splits work when budget exceeded | 0.5 | Pending | |
| M2-25 | SP14 | Admin commands: `rbac`, `killswitch`, `pause` | M1-49, M2-03, M2-06 | Tests: permission refusal per role | 1 | Pending | |
| M2-26 | SP14 | `agentic audit verify` (local ledgers + control-plane chain) | M1-49, M1-06, M2-05 | Tests: tampered entry detected and located | 0.5 | Pending | |
| M2-27 | SP2 | Commons connector mapping (`dig`, `spec_for`) from Zyroflo `connect.py` | S14 | Ported tests pass | 0.5 | Pending | |
| M2-28 | SP2 | Commons domain-graph package from Zyroflo `tools/graph` (+ tests, fixture estates) | S14 | Graph suite passes (neo4j tests optional) | 1 | Pending | |
| M2-29 | SP2 | Zyroflo consumes commons (Zyroflo repo branch; stdlib-only path preserved) | M1-06…M1-10, M2-27, M2-28 | Zyroflo full test suite green on its branch | 1 | Pending | |
| M2-30 | SP19 | TokenLean **ST-1**: team level under tenant (key→team, column, team spend caps, team config overlays) | — | TokenLean tests green; team cap returns 429 | 2 | Pending | |
| M2-31 | SP19 | TokenLean **ST-2**: `tags` JSONB on `usage_events` from allow-listed `X-Agentic-*` headers | — | Tests: tags stored; non-allow-listed header ignored | 1 | Pending | |
| M2-32 | SP19 | TokenLean **ST-4**: OpenAI Responses endpoint `/v1/responses` | — | Codex smoke call succeeds; usage row written | 1.5 | Pending | |
| M2-33 | SP19 | TokenLean **ST-5**: W3C `traceparent` extraction + `gen_ai.*` span attributes | — | Tests: proxy span is child of caller span | 1 | Pending | |
| M2-34 | SP19 | TokenLean **ST-9**: OSS `/admin/keys` create/revoke/rotate/list with user + team binding; Postgres key store; `issue-key.sh` extended | M2-30 | Tests: issued key resolves user + team; revoked key rejected within TTL | 1.5 | Pending | |
| M2-35 | SP19 | TokenLean **ST-10**: OSS admin endpoint for routing rules and team limits (validated, versioned, hot-applied, audited) | M2-30 | Tests: invalid fragment rejected; rollback to previous version | 1 | Pending | |
| M2-36 | SP19 | TokenLean **ST-11**: LITE dashboard set (8 OSS dashboards + Per Call when Langfuse) with `team` variable | M2-30, M1-67 | Provisioning test loads set; team filter works on seeded data | 0.5 | Pending | |
| M2-37 | SP16 | SQL views joining framework tables with TokenLean `usage_events` via tags | M2-04, M2-31 | Tests on seeded DB: cost per run/step/agent reconciles | 1 | Pending | |
| M2-38 | SP16 | Seven Grafana dashboards (org, team, run explorer, agent quality, limits & safety, audit explorer, adoption) provisioned alongside the LITE set | M2-37 | Provisioning test loads all; each panel query returns rows on seeded data | 2 | Pending | |
| M2-39 | SP16 | docker-compose local org stack (control plane + Postgres + Grafana + TokenLean LITE) | M2-01, M2-38, M1-67 | `docker compose up` + smoke passes | 0.5 | Pending | |
| M2-40 | SP7 | `codex` backend (`codex exec --json --output-schema`) through TokenLean Responses; shell policy compiled to Codex execpolicy | M1-27, S3, M2-32 | Contract tests against pinned Codex version pass | 1.5 | Pending | |
| M2-41 | SP7 | Runner conformance tiers (Zyroflo `RUNNER_CONTRACT.md` / `provider_smoke`) for native, claude-code, codex, opencode | M1-28, M1-29, M2-40, M2-57 | Conformance suite green for all four | 1 | Pending | |
| M2-42 | SP8 | Egress restriction config (TokenLean + allow-listed registries) | M1-32, M1-33 | Tests: blocked host refused inside sandbox | 0.5 | Pending | |
| M2-43 | SP9 | Domain-graph adapter (commons) | M1-34, M2-28 | Tests on Zyroflo fixture estates | 1 | Pending | |
| M2-44 | SP9 | Nightly index workflow template (from Zyroflo `snapshot-publish.yml`, nightly, service account) | M1-36, M2-14 | actionlint clean; dry run commits only changed shards | 0.5 | Pending | |
| M2-45 | SP10 | Personal skills and memory: `~/.agentic/skills/`, `~/.agentic/memory/`, instruction-only, no permission widening, never shadow shared, disable-able per classification, synced, in ledger | M1-37, M1-38, M2-20 | Tests: used only in owner's runs; cannot shadow shared skill; blocked in restricted repo; ledger shows hash | 1 | Pending | |
| M2-46 | SP10 | Skill authoring and promotion: `skills new/validate/eval/submit --to/review`; eval report via harness; personal copy retired on promotion | M1-37, M1-38, M1-39, M2-45, M1-63, M2-03 | Tests: submit opens PR with report; non-owner cannot approve; script in repo/personal skill rejected | 1.5 | Pending | |
| M2-47 | SP10 | Full superpowers plugin opt-in for claude-code/codex steps (pinned, per-run workspace config, telemetry off) | M1-37, M1-29, M2-40 | Tests: native loop never receives orchestration skills; plugin present only when flag on | 0.5 | Pending | |
| M2-48 | SP11 | Review pre-pass + dependency scan (Zyroflo `prepass.py`/`sca.py`, OSV-Scanner, ScanCode, blocked licences) | M1-08, S14 | Tests: seeded secret/vuln/blocked licence each produce a finding | 1.5 | Pending | |
| M2-49 | SP11 | Handoff secret/PII scan before central publish (block/mask per policy, audited) | M1-41, M2-20 | Tests: planted PII masked before ingest | 0.5 | Pending | |
| M2-50 | SP17 | Jira adapter: read via connector mapping, comment run link, optional transition | M2-27 | Contract tests against Jira sandbox | 1 | Pending | |
| M2-51 | SP18 | Policy-repo template (tiers, limits, roles, classifications, licences, skills catalog, CODEOWNERS: `pipelines/` → org admins, `teams/<team>/` → that team's leads — D33) | M1-03, M2-03 | Template validates; control plane syncs it on a fresh install | 0.5 | Pending | |
| M2-52 | SP19 | TokenLean **ST-6 (final)**: LITE profile with team level; cloud values | M2-30, M1-66 | Profile tests green with teams enabled | 0.5 | Pending | |
| M2-53 | SP20 | **Acceptance 3 (org)**: admin changes tier mapping and a tool restriction in the policy repo; next run honours both; run record shows policy version | M2-02, M2-09, M1-68 | Scripted E2E passes on the compose org stack | 0.5 | Pending | |
| M2-54 | SP20 | Kill-switch propagation test: switch flipped → running pipeline stops within 30 s and ≤ 1 turn; no orphan processes | M2-19, M2-06, M2-22 | E2E timing test passes 5/5 runs | 0.5 | Pending | |
| M2-55 | SP20 | Audit E2E: `audit verify` + `audit export` on a real run; tampered entry detected | M2-26, M2-13 | E2E passes | 0.5 | Pending | |
| M2-57 | SP7 | `opencode` backend (D32): `opencode run --format json`, lock-down profile via `OPENCODE_CONFIG_CONTENT` (provider `baseURL` → TokenLean, model forced) and `OPENCODE_PERMISSION` compiled from the shell policy and deny lists; wrapped in `srt`/container (OpenCode has no own sandbox); verify `baseURL` against official docs first | M1-27, M1-31, M1-32 | Contract tests against pinned OpenCode version pass; calls appear in TokenLean usage with X-Agentic headers; a denied command is refused | 1.5 | Pending | |
| M2-56 | SP20 | **M2 demo + retro**: org demo (admin override, kill switch, dashboards, audit); re-estimate M3; scope levers | M2-53, M2-54, M2-55, M2-38 | Retro notes committed; go/no-go recorded | 0.5 | Pending | |

### M3 — Scale and operations (~31.5 PW, ~8 weeks)

Exit: full §1.3 suite on staging (M3-25), Helm install from runbook alone (M3-05), load test passed (M3-22), learning loop E2E (M3-16), pilot go/no-go (M3-26).

| ID | Sub-plan | Item | Depends on | Done when | PW | Status | Notes |
|---|---|---|---|---|---|---|---|
| M3-01 | SP16 | Executive ROI dashboard + monthly chargeback export (cost per accepted change, request→merged-PR cycle time, DORA lead time & change-failure rate via Zyroflo `metrics.dora`, rework rate, PR merge rate, human wait time, TokenLean savings, cost per team) **+ cost-driver breakdown for org and each user: users × sessions/user × turns/session × requests/turn × tokens/request × cost/token, with reconciliation, unattributed line, month-over-month attribution and what-if panel** (see "Executive cost breakdown") | M2-37, M1-58 | Dashboard loads on seeded data; CSV export matches SQL totals; product of the six factors equals summed actual cost within 0.5 % on seeded data, for org and for each user; untagged calls appear as "unattributed" | 2.5 | Pending | |
| M3-02 | SP16 | Umbrella Helm chart `agentic-platform` (control plane, workers, TokenLean sub-chart, observability provisioning; BYO Postgres/Grafana/TokenLean); Postgres backup/PITR + restore drill; upgrade runbook | M2-01, M2-39, M3-03 | `helm lint`; install on kind; restore drill executed and documented | 3 | Pending | |
| M3-03 | SP19 | TokenLean **ST-8 (cloud)**: `deploy/lite/cloud/helm/tokenlean-lite` + `install/upgrade/uninstall/smoke` scripts | M1-67, M2-52 | `helm lint` + `cloud/install` + `cloud/smoke` pass on kind | 1 | Pending | |
| M3-04 | SP16 | Environments dev/staging/prod: values files, External Secrets, cert-manager TLS, policy branch per env, promotion runbooks | M3-02, M2-02 | Staging installed from chart; promotion dry run documented | 1.5 | Pending | |
| M3-05 | SP16 | Day-0 bootstrap runbook + script: policy repo from template, GitHub app/Jira connector, SMTP, first org admin, roles, CLI to internal index, smoke run | M3-04, M2-51, M2-03, M3-21 | Fresh staging bootstrapped end to end from the runbook alone | 1 | Pending | |
| M3-06 | SP15 | Retention jobs per data type (`retention.yaml`) mapped to TokenLean retention; trace capture default off, team opt-in — implements the policy defined in **OR-01** | M2-05, OR-01 | Tests: expired rows removed; audit kept longest | 1 | Pending | |
| M3-07 | SP15 | Run heartbeat from CLI; zombie detection → mark stale, alert, release concurrency slot | M2-04, M2-08, M1-21 | Tests: run without heartbeat > N min marked stale and slot freed | 1 | Pending | |
| M3-08 | SP14 | `agentic gc`: prune finished worktrees/run folders by age; disk cap warning in `status` | M1-30, M1-21 | Tests: only finished runs pruned; cap warning shown | 0.5 | Pending | |
| M3-09 | SP14 | `agentic doctor` + `agentic support-bundle` (redacted); doctor checks the policy repo has branch protection with required code-owner review (D33) | M1-53, M1-47 | Tests: bundle contains no secrets; doctor reports every prerequisite | 1 | Pending | |
| M3-10 | SP8 | Sandbox resource limits: CPU, memory, disk per step (cgroups/containers; best-effort on Windows) | M1-32, M1-33 | Tests: runaway process limited on Linux; documented behaviour on Windows | 1 | Pending | |
| M3-11 | SP21 | Learning loop **L1** — feedback capture: gate rejections, gate edit diffs, GitHub PR review comments / changes requested, review-agent misses; records in ledger + control plane (Zyroflo `feedback.py`) | M1-22, M1-58, M2-04 | One test per signal type | 1.5 | Pending | |
| M3-12 | SP21 | **L2** — attribution rules + Learning Analyst agent with confidence; human correction of attribution | M3-11, M1-15, M1-60 | Tests: PR code comment → Developer + Code Review; requirements edit → BA | 1.5 | Pending | |
| M3-13 | SP21 | **L3** — lesson normalisation and clustering per agent + scope; threshold from policy (default 3 in 30 days); rejected-lesson memory (Zyroflo `learning.py`) | M3-12, M1-03 | Tests: 2 similar → none, 3 → proposal; rejected not re-proposed | 1.5 | Pending | |
| M3-14 | SP21 | **L4** — proposals: personal skill/note, AGENTS.md entry, shared skill edit/new, agent instruction, + regression eval case; diff + evidence | M3-13, M2-46, M1-46, M1-63 | Tests: proposal contains diff, evidence and eval case; narrowest scope chosen | 1.5 | Pending | |
| M3-15 | SP21 | **L5** — approval UX: end-of-run prompt, email digest, `agentic learn list/show/approve/edit/reject/defer/endorse/propose --from`; owner-only approval; endorse for developers | M3-14, M2-03, M2-07, M1-49, M2-45 | Tests: nothing applied without approve; developer cannot approve shared; personal approvable by its user | 1 | Pending | |
| M3-16 | SP21 | **L6** — apply via PR, eval before/after, dashboard panel (correction rate per agent), `agentic learn revert`, audit events | M3-15, M1-58, M2-38 | E2E: 3 corrections → proposal → approve → PR → revert restores previous | 1 | Pending | |
| M3-17 | SP20 | Cross-platform CI green for full v1 scope | all M1–M3 | CI matrix green 3 consecutive runs | 0.5 | Pending | |
| M3-18 | SP20 | Docs: README, developer guide, team-lead guide, admin guide, auditor guide, policy-repo guide, runbooks | M3-16 | Docs reviewed by owner; every CLI command documented | 2 | Pending | |
| M3-19 | SP20 | Resolve spec §22 open questions (email relay, default classification, budgets, refresh on merge) | — | Spec updated; defaults in policy template | 0.25 | Pending | |
| M3-20 | SP20 | Pilot rollout runbook (from Zyroflo `docs/pilot-plan.md`): cohort, success criteria, training, feedback loop, go/no-go | M3-18 | Runbook reviewed by owner | 0.5 | Pending | |
| M3-21 | SP20 | Release engineering: semver releases, signed artefacts, SBOM, THIRD_PARTY notices, publish to internal index, upgrade path | P0-03 | Release workflow produces signed wheel + SBOM; install from index works | 1.5 | Pending | |
| M3-22 | SP20 | Load test: 1000 concurrent runs reporting to control plane; ingest lag and error targets met | M2-04, M2-12 | Report: p95 ingest lag and error rate within targets; sizing updated | 1.5 | Pending | |
| M3-23 | SP20 | Onboarding kit: docs site, sample repo, 30-min training deck, office-hours plan; onboarding ≤ 15 min measured | M3-18, M1-53 | 5 pilot developers onboard unaided in ≤ 15 min median | 1.5 | Pending | |
| M3-24 | SP19 | TokenLean **ST-7**: optimisation opt-in process — enable one group at a time after A/B on the eval harness | M1-66, M1-63 | Documented procedure + first A/B record | 0.5 | Pending | |
| M3-25 | SP20 | **Acceptance (staging)**: full §1.3 suite (scenarios 1–4) against the staging org stack with real models | M3-04, M2-53 | All four pass on staging; results committed | 1 | Pending | |
| M3-26 | SP20 | **M3 demo + pilot go/no-go** | M3-25, M3-22, M3-16, M3-23 | Go/no-go recorded; pilot wave scheduled | 0.5 | Pending | |

### Open requirements (to be defined later; not yet estimated)

Requirements the owner has raised but deliberately left undefined. Each must be defined (and its rows estimated) before the tasks that depend on it start.

| ID | Requirement | What must be decided | Blocks | Status | Notes |
|---|---|---|---|---|---|
| OR-01 | **Data retention policy** | Retention period and end-of-life action (delete / pseudonymise / archive) for each data type: local run folders and worktrees, run ledgers and handoffs, prompts and responses (Langfuse traces), TokenLean `usage_events`, control-plane run/event tables, the audit chain (must stay verifiable after deletion), personal skills and memory, learning-loop feedback records, eval corpora, logs and support bundles, backups. Also: legal hold, per-user erasure requests, regional/regulatory constraints, who may change the policy, and how deletion is proven. | M3-06 (retention jobs), E4-01, E4-02 | Open — to be defined later | Raised by owner 2026-10-18. Until defined: nothing is deleted automatically; trace capture stays off by default. |

### Phases 2–4 — epics (each broken down into its own plan when started)

| ID | Epic | Status |
|---|---|---|
| E2-01 | CI/webhook triggers (GitHub Actions, Jira transitions) + service accounts at scale | Pending |
| E2-02 | Slack/Teams notifications and gate approvals | Pending |
| E2-03 | DSSE-signed approvals + WORM audit copy | Pending |
| E2-04 | `full-lifecycle` pipeline + Triage/Release/Deploy/Verify/Closeout agents | Pending |
| E2-05 | Learning loop phase 2: guardrail/policy proposals (review checklist and pre-pass rules, shell deny rules) via policy-repo PRs; QA-miss signal from post-merge defects; Zyroflo `claims.py` checks | Pending |
| E2-06 | ACP backend (Gemini CLI, OpenCode, Goose, Copilot CLI) | Pending |
| E2-07 | Policy-merge author verification in control plane | Pending |
| E2-08 | Per-team trace capture policy with admin force | Pending |
| E2-09 | TokenLean P1: Langfuse stream fix, remaining-budget API, kill-switch deny rules, matched-rule persistence | Pending |
| E2-10 | Multi-repo runs (one change across several repos; Zyroflo workspace model) | Pending |
| E2-11 | Alerts to Slack/Teams/PagerDuty via webhook seam (budget, auto-trip, zombie runs) | Pending |
| E3-01 | Web UI (runs, handoffs, gates, RBAC admin, learning proposals) | Pending |
| E3-02 | VS Code extension (thin TS client) | Pending |
| E3-03 | OIDC/SSO + IdP group → role sync | Pending |
| E3-04 | Remote backends (Devin etc.) | Pending |
| E3-05 | Mem0 learned memory on Qdrant | Pending |
| E3-06 | DBOS durable execution | Pending |
| E3-07 | Cedar policy engine (if needed) | Pending |
| E3-08 | Approval delegation and vacation rules (needs web UI) | Pending |
| E4-01 | Signed evidence packs, legal hold | Pending |
| E4-02 | Per-user erasure, fixed retention schedules | Pending |

---

## File structure (v1 target)

```
agentic-sdlc/
├── pyproject.toml                  # uv workspace root (members below), ruff, pytest config
├── core/      pyproject.toml, src/agentic/{definitions,policy,engine,agents,transport,backends,
│              sandbox,handoffs,codeintel,skills,memory,integrations,security,telemetry,auth,learning}/,
│              tests/<same areas>/
├── commons/   pyproject.toml, src/sdlc_commons/{ledger,events,guards,worktree,connectors,graph,lifecycle}/, tests/
├── cli/       pyproject.toml, src/agentic_cli/, tests/
├── control-plane/ pyproject.toml, src/agentic_cp/{api,policy_sync,rbac,ingest,audit,killswitch,notify,learning}/,
│              migrations/, dashboards/, tests/
├── deploy/    helm/agentic-platform/, compose/, runbooks/
├── library/   agents/, pipelines/, handoff-templates/, skills/, eval/
├── templates/policy-repo/
├── spikes/    (throwaway)
└── docs/      superpowers/{specs,plans}/, spikes/, de-scope-register.md
```

Import names: `agentic` (core), `sdlc_commons`, `agentic_cli`, `agentic_cp`. CLI entry point: `agentic`.

---

## Sub-plan SP1 (detailed): bootstrap, definitions and policy model — rows P0-01, M1-01…M1-05

Produces: a tested library that loads agent, pipeline, policy and handoff files and resolves an agent against org policy — the base every other sub-plan builds on.

### Task 1 (P0-01): Repository bootstrap

**Files:** Create `pyproject.toml` (workspace root), `core/pyproject.toml`, `commons/pyproject.toml`, `cli/pyproject.toml`, `control-plane/pyproject.toml`, `core/src/agentic/__init__.py`, `core/tests/test_smoke.py`, `.gitignore`, `.gitattributes`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `LICENSE` (MIT), `docs/superpowers/plans/2026-10-17-agentic-sdlc-master-plan.md` (copy of this file), `docs/superpowers/plans/TRACKER.md`, `docs/de-scope-register.md`. Move existing `docs/superpowers/specs/`, `reports/`, `research_notes/` under version control as-is.

**Interfaces:** Produces: `agentic.__version__: str`.

- [ ] **Step 1:** `git init`; write `.gitignore` (Python, `.venv/`, `.agentic/local/`, `spikes/**/out/`) and `.gitattributes` (`* text=auto eol=lf`).
- [ ] **Step 2: Write the failing test** `core/tests/test_smoke.py`:
  ```python
  import agentic
  def test_version_exposed():
      assert agentic.__version__ == "0.1.0"
  ```
- [ ] **Step 3:** Run `uv run pytest core/tests/test_smoke.py -v` → FAIL (package missing).
- [ ] **Step 4:** Create the uv workspace (`requires-python = ">=3.12,<3.13"`), member packages with src layout, `agentic.__version__ = "0.1.0"`; dev deps: pytest, ruff, pre-commit, pydantic, python-frontmatter, ruamel.yaml.
- [ ] **Step 5:** Run `uv run pytest -v` → PASS; `uv run ruff check .` → clean.
- [ ] **Step 6:** CI workflow: matrix `windows-latest, macos-latest, ubuntu-latest`, Python 3.12, `uv sync --locked`, `ruff check`, `pytest`.
- [ ] **Step 7:** Copy plan + tracker into `docs/superpowers/plans/`; mark P0-01 `Done` in TRACKER.md.
- [ ] **Step 8: Commit** `git add -A && git commit -m "chore: bootstrap agentic-sdlc workspace, spec, plan and tracker"`

### Task 2 (M1-01): AgentDefinition model and loader

**Files:** Create `core/src/agentic/definitions/{__init__,errors,agent}.py`, `core/tests/definitions/test_agent.py`, `core/tests/fixtures/agents/developer.md` (the spec §5.1 example).

**Interfaces:**
- Produces: `Limits(budget_usd: float|None=None, max_turns: int|None=None, max_minutes: int|None=None)`; `ChildLimits(max_total: int=0, max_concurrent: int=1, max_depth: int=1, per_child: Limits=Limits())`; `AgentDefinition` (Pydantic, `extra="forbid"`) with fields `name, version, owner=None, status: Literal["active","deprecated"]="active", description, tier: str|None="balanced", model: str|None=None, effort: Literal["low","medium","high"]|None=None, temperature: float|None (0.0–2.0), backend: str="native" (regex ^(native|claude-code|codex|acp:.+|remote:.+)$), tools: list[str]=[], shell: list[str]=[], skills: list[str]=[], consumes: list[str]=[], produces: str|None=None, limits: Limits=Limits(), children: ChildLimits=ChildLimits(), deny: DenyRules=DenyRules(), instructions: str` (Markdown body, non-empty); `DenyRules(shell: list[str]=[], paths: list[str]=[])` (`extra="forbid"`); a shell entry that appears in both `shell` and `deny.shell` is rejected as contradictory; `load_agent(path: Path) -> AgentDefinition`; `DefinitionError(path: Path, errors: list[str])`.

- [ ] **Step 1: Write failing tests:**
  ```python
  def test_loads_spec_example(fixtures):
      a = load_agent(fixtures / "agents/developer.md")
      assert (a.name, a.tier, a.backend, a.produces) == ("developer", "balanced", "native", "dev-report")
      assert a.children.per_child.max_turns == 30
      assert a.instructions.startswith("You are the Developer.")
  def test_unknown_field_rejected(tmp_path):  # front matter with "colour: red"
      with pytest.raises(DefinitionError) as e: load_agent(p)
      assert "colour" in str(e.value)
  def test_empty_body_rejected(tmp_path): ...  # DefinitionError mentions "instructions"
  def test_bad_backend_rejected(tmp_path): ...  # backend: "ftp" → DefinitionError
  def test_minimal_agent_defaults(tmp_path):  # only name, version, description + body
      a = load_agent(p); assert a.tier == "balanced" and a.tools == [] and a.limits.budget_usd is None
  ```
- [ ] **Step 2:** Run `uv run pytest core/tests/definitions/test_agent.py -v` → FAIL.
- [ ] **Step 3:** Implement with `frontmatter.load`; body → `instructions`; Pydantic `ValidationError` → `DefinitionError` listing field paths.
- [ ] **Step 4:** Run tests → PASS.
- [ ] **Step 5:** Mark M1-01 `Done`; commit `feat(core): agent definition model and loader`.

### Task 3 (M1-02): PipelineDefinition model and loader

**Files:** Create `core/src/agentic/definitions/pipeline.py`, `core/tests/definitions/test_pipeline.py`, `core/tests/fixtures/pipelines/feature-delivery.yaml` (spec §6 example with r2 `until`).

**Interfaces:**
- Produces: `AgentStep(id: str, agent: str, when: str|None=None)`; `GateStep(gate: str, approver_role: str, four_eyes: bool=False)`; `LoopSpec(id: str, max_iterations: int (≥1), until: str, on_exhausted: Literal["gate","fail"]="gate", steps: list[AgentStep|ActionStep])`; `LoopStep(loop: LoopSpec)`; `ActionStep(id: str, action: str, when: str|None=None)`; `Step = AgentStep|GateStep|LoopStep|ActionStep` (discriminated by key); `PipelineDefinition(name, version, regime: Literal["solo","team","regulated"]="team", inputs: dict[str, dict]={}, steps: list[Step])`; `load_pipeline(path: Path) -> PipelineDefinition`. Validation: step/gate/loop ids unique across the pipeline, at least one step, loops not nested.

- [ ] **Step 1: Write failing tests:** `test_loads_feature_delivery` (5 top-level entries; loop `build-review` has `max_iterations == 3`, `until == "review.open_blocking == 0"`), `test_duplicate_ids_rejected`, `test_nested_loop_rejected`, `test_zero_iterations_rejected`, `test_unknown_step_shape_rejected` (DefinitionError names the step index).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement with ruamel.yaml (safe) + Pydantic discriminated union; uniqueness/nesting checks in a model validator.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Mark M1-02 `Done`; commit `feat(core): pipeline definition model and loader`.

### Task 4 (M1-03): OrgPolicy model and loader

**Files:** Create `core/src/agentic/policy/{__init__,model,load}.py`, `core/tests/policy/test_policy_load.py`, `core/tests/fixtures/policy/policy/*.yaml` (tiers, providers, tools, limits, classifications, retention, licences, `teams/payments.yaml`).

**Interfaces:**
- Produces: `TierSpec(model: str, effort: str|None=None, temperature: float|None=None)`; `ClassificationRule(allowed_tiers: list[str], allowed_models: list[str]|None=None, allowed_backends: list[str], trace_capture: bool=False)`; `RoleLimits(max: Limits, children_max: ChildLimits)`; `TeamPolicy(allowed_tiers: list[str]|None=None, monthly_budget_usd: float|None=None, limits_max: dict[str, RoleLimits]={})`; `OrgPolicy(tiers: dict[str, TierSpec], team_overrides: dict[str, dict[str, TierSpec]]={}, classifications: dict[str, ClassificationRule], default_classification: str, tools_catalog: list[str], forbidden_tools: list[str]=[], role_limits: dict[str, RoleLimits], default_role_limits: RoleLimits, concurrency: dict[str,int]={}, blocked_licences: list[str]=[], retention_days: dict[str,int]={}, teams: dict[str, TeamPolicy]={}, allowed_repo_patterns: list[str]=[] (glob over remote URLs; empty = allow all, standalone only), tool_mirror_url: str|None=None, licence_scan: bool=False, allow_repo_pipelines: bool=False, version: str)`; `load_policy(root: Path) -> OrgPolicy` (version = `git rev-parse HEAD` of `root` if a git repo, else `"sha256:" + digest of the sorted policy files`); `PolicyError`.

- [ ] **Step 1: Write failing tests:** `test_loads_fixture_policy` (tiers has the three spec tiers; `team_overrides["payments"]["deep-reasoning"].model == "openai/gpt-5"`), `test_version_is_content_hash_outside_git` (starts with `sha256:` and changes when a file changes), `test_team_cannot_name_unknown_tier` (PolicyError), `test_default_classification_must_exist` (PolicyError), `test_repo_patterns_and_mirror_loaded` (`allowed_repo_patterns == ["github.com/acme/*"]`, `tool_mirror_url` set), `test_allow_repo_pipelines_defaults_false` (D33).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement loader merging the policy files into one model (these files are org-only; no layering here).
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Mark M1-03 `Done`; commit `feat(core): org policy model and loader`.

### Task 5 (M1-04): Layered discovery and agent resolution

**Files:** Create `core/src/agentic/definitions/discovery.py`, `core/src/agentic/policy/resolve.py`, tests `core/tests/definitions/test_discovery.py`, `core/tests/policy/test_resolve.py`.

**Interfaces:**
- Consumes: `load_agent`, `load_pipeline`, `OrgPolicy`.
- Produces: `DefinitionSet.discover(org: Path|None, team: Path|None, repo: Path|None) -> DefinitionSet` with `agent(name) -> AgentDefinition`, `pipeline(name) -> PipelineDefinition`, `source_of(name) -> Literal["org","team","repo"]` (nearest layer wins the whole definition; repo > team > org); `ResolveContext(team: str, role: str, repo_classification: str|None)`; `Violation(field: str, requested: object, allowed: object, reason: str)`; `ResolvedAgent(definition: AgentDefinition, tier: str|None, model: str, effort: str|None, temperature: float|None, backend: str, tools: list[str], shell: list[str], skills: list[str], limits: Limits, children: ChildLimits)`; `resolve_agent(defn, policy, ctx) -> tuple[ResolvedAgent|None, list[Violation]]`.
- Rules (spec §5.2/§5.3/§12.2): model = team override of tier → tier → explicit `model` only if allowed by classification; tier must be allowed by classification **and** team; backend must be allowed by classification; tools ⊆ `tools_catalog` minus `forbidden_tools`; limits/children = min(requested, role max, team max) with `None` requested → role max; any violation → `ResolvedAgent` is `None`; `ResolvedAgent` also carries `deny_shell` and `deny_paths` = union of org, team and agent deny lists, and any allowed shell prefix that a deny entry covers exactly is dropped from `shell`.

- [ ] **Step 1: Write failing tests:** `test_repo_pipelines_ignored_unless_allowed` (D33: repo pipeline not returned and a warning recorded when `allow_repo_pipelines` is false; returned when true), `test_repo_definition_overrides_org` (same name in both dirs → repo version, `source_of == "repo"`), `test_team_override_maps_tier` (payments + deep-reasoning → `openai/gpt-5`), `test_team_cannot_loosen` (team allows only `balanced, fast`; agent requests `deep-reasoning` → violation on `tier`), `test_restricted_classification_blocks_backend` (`restricted` allows `native` only; `claude-code` → violation), `test_forbidden_tool_violation`, `test_limits_clamped_to_role_max` (requested budget 50, role max 5 → 5.0), `test_missing_limit_takes_role_max`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement discovery (glob `agents/*.md`, `pipelines/*.yaml` per layer) and resolution as specified.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Mark M1-04 `Done`; commit `feat(core): layered discovery and agent resolution against policy`.

### Task 6 (M1-05): Handoffs — envelope, kinds, parser, compatibility

**Files:** Create `core/src/agentic/handoffs/{__init__,model,kinds,parse,compat}.py`, tests `core/tests/handoffs/test_handoffs.py`, fixtures `core/tests/fixtures/handoffs/design.md` (spec §7.1 example).

**Interfaces:**
- Produces: `HandoffEnvelope(kind: str, schema_version: int, run_id: str, step: str, agent: str, produced_at: datetime, status: Literal["complete","partial","blocked"])`; kind models `RequirementsData, DesignData, DevReportData, ReviewData, TestReportData` with the spec §7.2 fields (`ReviewData.open_blocking: int`, `findings[].severity`, `score: float|None`); `HANDOFF_KINDS: dict[str, type[BaseModel]]`; `Handoff(envelope, data: BaseModel, body: str)`; `parse_handoff(text: str) -> Handoff`; `HandoffError(errors: list[str])`; `check_compatibility(pipeline: PipelineDefinition, agents: Mapping[str, AgentDefinition]) -> list[Violation]` (each agent step's `consumes` must be produced by an earlier step or be a pipeline input; loop bodies may consume outputs of later steps in the same loop from the previous iteration).

- [ ] **Step 1: Write failing tests:** `test_parses_design_example` (`data.decisions[0].rationale` non-empty, `data.impact.confidence == "candidate"`), `test_missing_rationale_rejected`, `test_unknown_kind_rejected`, `test_broken_front_matter_reports_line` (Review Focus 5: unclosed YAML → `HandoffError` whose message contains the line number; input string unchanged), `test_compat_flags_unproduced_input` (QA consumes `design` but no step produces it → one Violation), `test_compat_allows_loop_feedback` (developer consumes `review` inside build-review loop → no violation).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement: python-frontmatter for split; YAML errors mapped with line numbers; envelope + kind model validation; compatibility walk over flattened steps.
- [ ] **Step 4:** Run → PASS; run full suite `uv run pytest -v` → all PASS on CI matrix.
- [ ] **Step 5:** Mark M1-05 `Done`; commit `feat(core): handoff envelope, kinds, parser and compatibility check`.

---

## Sub-plan outlines (detailed TDD plans written just before each starts)

| Sub-plan | Milestone(s) | Delivers | Key reuse |
|---|---|---|---|
| SP1 Definitions & policy | M1 | agent/pipeline/policy models, resolution, handoffs | python-frontmatter, ruamel.yaml, Pydantic |
| SP2 Commons | M1, M2 | `sdlc_commons` ledger, events, guards, worktree, lifecycle (M1); connectors, graph, Zyroflo switchover (M2) | Zyroflo `approve.py` (`entry_hash` L135, `verify_chain`, `sign_entry`), `events.py` (L48–83), `state.py` (`digest_of`, `append_record`), `policy.py` L3215–4105, `worktree.py`, `connect.py`, `tools/graph/*`, `sdlc/stages/*`, `validate.py` |
| SP3 Transport | M1 | headers, cost capture, budget ledger, corporate network | openai/anthropic SDK `http_client`, httpx event hooks |
| SP4 Agent runtime | M1 | AgentRunner, tools, child spawner, stuck detector | Pydantic AI v2 (`UsageLimits`, deferred tools), OpenHands `StuckDetector` |
| SP5 Handoff templates | M1 | per-kind templates, validation retry | spec-kit templates, Zyroflo `review-rubric.md` |
| SP6 Engine | M1, M2 | state machine, gates, loops, expressions, preflight, timeouts (M1); kill switch, overlays, diff budget, process cleanup (M2) | commons ledger/guards; S13 evaluator |
| SP7 Backends | M1, M2 | native, claude-code (M1); codex, conformance (M2) | Claude Agent SDK, `codex exec`, Zyroflo `RUNNER_CONTRACT.md` + `provider_smoke.py` |
| SP8 Sandbox | M1, M2, M3 | worktree per step, shell policy, srt, Windows (M1); egress (M2); resource limits (M3) | commons worktree/guards, `srt`, Codex sandbox |
| SP9 Codeintel | M1, M2 | interface, symbol adapter, index layout (M1); domain-graph adapter, nightly workflow (M2) | S5 winner, commons graph, Zyroflo `snapshot-publish.yml` |
| SP10 Skills | M1, M2 | catalog, loader, admission, defaults (M1); personal skills, authoring/promotion, superpowers opt-in (M2) | Agent Skills spec, `skills-ref`, superpowers subset, addyosmani, mattpocock |
| SP11 Security | M1, M2 | secret scan, untrusted wrapping (M1); pre-pass + SCA, handoff PII scan (M2) | gitleaks/detect-secrets, Zyroflo `prepass.py`/`sca.py`, OSV-Scanner, ScanCode |
| SP12 Telemetry | M1, M2 | OTel, local sink, trailers, AGENTS.md, logs (M1); control-plane sink + offline queue (M2) | OpenTelemetry SDK, commons events |
| SP13 Identity | M1 | credential provider | keyring |
| SP14 CLI | M1, M2, M3 | core commands (M1); admin + audit commands (M2); gc, doctor, support bundle (M3) | Typer/Cyclopts, Rich, questionary, Zyroflo `third_party.py` |
| SP15 Control plane | M2, M3 | policy sync, RBAC, ingest, audit, kill switch, pause, auto-trip, notify, provisioning (M2); retention, heartbeat (M3) | FastAPI, SQLAlchemy/Alembic, TokenLean `audit/enforcement.py` pattern |
| SP16 Dashboards & deploy | M2, M3 | SQL views, 7 dashboards, compose (M2); ROI, Helm, environments, day-0 (M3) | Zyroflo `metrics-columns.md` + `grafana/dashboard.json`, TokenLean dashboards |
| SP17 Integrations | M1, M2 | GitHub (M1); Jira (M2) | commons connectors, `gh`, Zyroflo `release.py` flow |
| SP18 Library & agent quality | M1, M2 | golden handoffs, eval corpus, built-in agents, pipelines, eval harness, quality gate (M1); policy-repo template (M2) | Zyroflo SOPs P0–P6, P10, `corpus_harvest`, `corpus_replay`, judging protocol |
| SP19 TokenLean | M1, M2, M3 | ST-3, ST-6, ST-8 local (M1); ST-1, ST-2, ST-4, ST-5, ST-9, ST-10, ST-11, ST-6 final (M2); ST-8 cloud, ST-7 (M3) | `research_notes/.../tokenlean_observability_audit.md`, `auth/api_key_manager.py`, `scripts/issue-key.sh`, `dashboard/` |
| SP20 Acceptance & rollout | M1, M2, M3 | acceptance scenarios, CI, docs, release engineering, load test, pilot kit | Zyroflo `docs/pilot-plan.md` |
| SP21 Learning loop | M3 | feedback capture, attribution, clustering, proposals, explicit approval, apply/measure/undo | Zyroflo `feedback.py`, `learning.py`; GitHub review API via SP17 |

**Sequencing inside milestones.** M1: P0-04 → SP1 → SP2 (ledger/guards/worktree) ∥ SP3 ∥ SP19 (ST-3/ST-6/ST-8 local) → SP4 ∥ SP5 ∥ SP13 → SP6 ∥ SP7 ∥ SP8 → SP9 ∥ SP10 ∥ SP11 ∥ SP12 → SP14 ∥ SP17 → SP18 (agents, harness, quality gate) → acceptance. M2: SP15 core ∥ SP19 (ST-1/2/4/5) → RBAC/ingest/audit ∥ SP2 (graph, connectors) → kill switch/pause/auto-trip ∥ SP7 codex ∥ SP10 personal/authoring → SP16 dashboards ∥ SP17 Jira → acceptance. M3: SP16 Helm/environments ∥ SP21 ∥ SP20 release/load → ROI ∥ docs/pilot kit → staging acceptance.

## Verification

- **Per task:** the task's pytest command passes; full suite `uv run pytest -v` passes; `uv run ruff check .` clean; CI green on Windows, macOS, Linux.
- **Per sub-plan:** its tracker rows are `Done` with commit SHAs; the sub-plan's own integration test passes.
- **Phase 0 exit (P0-04):** all 14 spike records exist with decisions; spec r3 committed; M1 re-estimated; owner sign-off.
- **M1 exit:** acceptance scenarios 1, 2 and 4 pass in standalone mode on CI (recorded responses) and once live against local TokenLean LITE; agent quality gate report meets targets or targets are consciously reset at the retro; CI matrix green; demo given.
- **M2 exit:** `docker compose up` brings the org stack; scenario 3 (admin override) passes; kill switch stops a running pipeline within 30 s / one turn leaving no orphan processes; `agentic audit verify` and `audit export` detect a tampered entry; dashboards show cost per team/pipeline/agent; demo given.
- **M3 exit:** Helm install and day-0 bootstrap succeed on a fresh staging cluster from the runbooks alone; full §1.3 suite passes on staging with real models; load test at 1000 concurrent runs meets targets; learning loop E2E (3 corrections → proposal → approval → PR → revert) passes; ROI dashboard populated; 5 pilot developers onboard in ≤ 15 min median; pilot go/no-go recorded.
