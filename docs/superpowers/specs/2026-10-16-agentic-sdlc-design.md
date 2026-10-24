# Agentic SDLC Framework — Design Spec

| | |
|---|---|
| Status | Draft for review |
| Date | 2026-10-16 |
| Owner | Sumit Gupta |
| Supporting research | [OSS reuse report](../../../reports/Agentic%20SDLC%20OSS%20reuse.md) · [research notes](../../../research_notes/Agentic%20SDLC%20OSS%20reuse/) · [Zyroflo comparison](../../../research_notes/Agentic%20SDLC%20OSS%20reuse/sdlc_harness_comparison.md) |
| Revision | r3 (2026-10-18): decisions D26-D35 folded in; new §23 Users and roles, §24 Developer onboarding (init, skills, custom skills, learning loop), §25 Deployment landscape (incl. TokenLean LITE), §26 Executive cost breakdown, §27 Enterprise coverage; §5 agent types and deny lists; §19 milestones. Since r3: D36 (httpx2), D37 (handoff templates inherit a base, extend-only layering, §7.4), D38 (sandbox: `srt` on every OS, Windows via WSL2, spike S6), D39 (code-intelligence index built locally; opt-in export outside `main`, spike S9); S4 re-confirms D1/D17 (build, not adopt); S7: ACP v1 is the single phase-2 adapter, opencode moves onto it (D32 revised), Antigravity via its official ACP server, OpenHands dropped; D40 (engine-neutral containers, S12), D41 (eval method, S10), D42 (Responses translation and codex lock-down, S3), D43 (supply-chain checks, P0-03) - confirmed by the owner 2026-10-21 with the D32 revision; D44 (pycasbin RBAC) and D45 (multi-tenant control plane) - owner decisions 2026-10-22 |
| Earlier | r2 (2026-10-17): Zyroflo (sdlc-harness) artefacts folded in — §1.4, D16, D25, §6, §7, §9.2, §10, §14.5, §16, §18–§21 |

---

## 1. Purpose

Build a provider-agnostic framework that lets any developer in a ~1000-person organisation run software-delivery work through a **pipeline of specialised AI agents** (Business Analyst, Design, Developer, Code Review, QA, and custom agents), on **any existing or new git repository**, while **org admins keep central control** of models, tools, spend and data, and every action is **observable and auditable**.

### 1.1 Goals

1. A user defines an agent in **plain English** using a fixed template (instructions + model tier/model, effort, temperature, tools, skills, limits, input/output handoffs) — or picks one from a built-in library.
2. A user chains agents into a **pipeline** (steps, human gates, loops, conditional branches) and triggers it.
3. Agents can **spawn child agents**, within admin-set limits on count, tokens/cost, loops and time.
4. Work passes between agents, and to humans, as **schema-validated handoff documents**.
5. **Provider agnostic**: every model call goes through the **TokenLean** proxy; no provider SDK lock-in.
6. **Enterprise governance**: layered definitions (org > team/user > repo), admin override and veto, role-based access, kill switch, budgets.
7. **Observability and audit**: who/which team/which agent did what, why, at what cost; tamper-evident audit of code and configuration changes; dashboards.
8. **Reuse OSS** wherever possible (standing instruction); custom code only for glue and gaps.

### 1.2 Non-goals

- Building a new LLM gateway/proxy (TokenLean is the gateway).
- Pipeline-level parallel branches (parallelism happens inside a step via child agents).
- A general policy engine in v1 (policy files + typed checks; Cedar later if needed).
- Building our own code-graph engine (reuse OSS indexers).
- Output-compression skills such as Caveman (rejected on evidence, §11.4).

### 1.3 v1 success test (acceptance)

All four must pass:

1. **End-to-end pipeline on an existing repo**: `feature-delivery` (BA → Design → Developer → Code Review → QA) runs from the CLI with a human gate after Design and after Review and a working review→developer loop; output is a reviewed, tested change in a worktree plus a GitHub PR.
2. **Custom agent**: a user defines a new agent (e.g. "Docs Writer") in plain English and adds it to the pipeline without writing code.
3. **Admin override**: an admin changes a tier→model mapping or a tool restriction in the policy repo; the next run honours it (and the run record shows the policy version used).
4. **New empty repo**: the same pipeline works on a freshly initialised empty repository.

### 1.4 Relationship to Zyroflo (sdlc-harness)

Zyroflo (`cbeyondcloud/sdlc-harness`, MIT, same owner) is a zero-dependency governance harness installed into a repo: the developer's own AI coding tool orchestrates, and Zyroflo's SOPs and CLIs police it (hash-chained gates, guards, domain graph). It covers ~25–30% of this spec and its runtime model is inverted relative to D1, so this framework is **built fresh**, but **reuses Zyroflo artefacts as much as possible**:

| Zyroflo artefact | Used here as |
|---|---|
| Lifecycle SOPs `sdlc/stages/P0–P11`, `LIFECYCLE.md`, `STEP_SPEC.md` | Library content: stage instructions for built-in agents and a `full-lifecycle` pipeline (§5.4, §6.2) |
| Review exit rule (loop until no blocking-severity finding; score reported, never the gate) and deterministic pre-pass | Code Review agent behaviour and review loop condition (§6) |
| Gate regimes (solo / team / regulated), org-required phases/gates, gate re-anchoring, diff-size budget per review batch | Pipeline gate model (§6.1) |
| Hash-chained `approvals.jsonl` / `events.jsonl`, artefact digest pinning, CI ledger merge check, optional DSSE signing | Run ledger and tamper-evident audit **from v1** (§7.3, §14.5) |
| `guard.py`, `deny_commands` matcher, protected paths, `worktree.py` | Shell-policy convenience layer and worktree manager (§9.2) |
| `tools/graph` domain graph (services, endpoints, Kafka, tables, Helm/compose, config, tests; impact with provenance; MCP server; shard snapshots + freshness) | Second codeintel adapter `domain-graph` and the design of the committed index (§10) |
| `RUNNER_CONTRACT.md`, conformance tiers, `provider_smoke` | Backend contract + conformance tests (§9.1, §20) |
| JSON-path connectors (Jira, GitHub issue, alert) | Integration adapters' mapping format (§16) |
| Metrics columns doctrine, Grafana dashboard, nightly collector | Seed for metrics schema and dashboards (§14) |
| Corpus replay, pre-registered judging protocol, de-scope register | Method for spikes S5/S10 and agent-quality evals (§20, §21) |
| Learning loop (corrections → gated proposals with undo), `feedback.py`, `learning.py` | v1, milestone M3 (D29, §24); `claims.py` in phase 2 |

**Positioning**: Zyroflo stays the zero-dependency drop-in product; this framework is the enterprise platform. **How it is reused (D34, spike S14):** Zyroflo is a pinned git dependency used unchanged; `sdlc_commons` is the only package that imports it (thin adapters); gaps are closed by small backward-compatible Zyroflo PRs — Z1 event extension fields, Z2 lifecycle content shipped as package data, Z3 configurable run-folder root, Z4 a `zyroflo` import alias. Zyroflo code is never copied into this repo. Record: `docs/spikes/S14-zyroflo-reuse.md`.

---

## 2. Decisions log

Decisions agreed during requirements clarification (2026-10-16).

| # | Area | Decision |
|---|---|---|
| D1 | Runtime | Standalone Python runtime with **pluggable backends**: our own agent loop, plus CLI agents (Claude Code, Codex) and later remote agents (Devin etc.). Re-confirmed by spike S4 (2026-10-20): no OSS project is adopted wholesale (OpenHands' budgets, RBAC and audit are in its commercial Enterprise edition; Archon has no policy layer or budget enforcement) (`docs/spikes/S4-build-vs-adopt.md`); S7 then found the OpenHands CLI unmaintained, so it is not an `acp:` backend either |
| D2 | Gateway | **TokenLean** is the only model gateway (OpenAI/Anthropic/Gemini-compatible). It owns provider keys, routing and admin model overrides, spend caps, per-call usage. Enhancements required (§17) |
| D3 | Pipeline control flow | Linear steps + **human gates** + **loops** (bounded) + **conditional branches**; no pipeline-level parallelism |
| D4 | Handoffs | Markdown documents with **schema-validated YAML front matter**; each agent declares what it consumes and produces |
| D5 | Run state | Local git-ignored run folder by default; **mandatory central reporting in org mode** (D14) |
| D6 | Agent powers | Per-agent **tool allow-list**; code-editing agents run in a **sandboxed git worktree** with an allow-listed shell |
| D7 | Definition layers | **org > team/user > repo**; org policy always wins on restricted fields (model/tier, provider, backend, tools, skills, limits, spend) |
| D8 | Interface | **CLI in v1**; design includes CI/webhook triggers, web UI and VS Code extension as later phases |
| D9 | Language | **Python 3.12**; thin TypeScript client for VS Code later |
| D10 | Code understanding | On-demand exploration by default **plus** a dependency-graph index; graph tool behind an interface (code-review-graph primary, codebase-memory-mcp fallback, Graphify optional) |
| D11 | Limits | Loop limits + per-run/per-agent budgets + **stuck-loop detector** in the runner; TokenLean monthly caps as backstop |
| D12 | Identity | Issued **proxy keys** in v1 with a pluggable credential; SSO/OIDC later; **service keys** for CI |
| D13 | Memory | Repo knowledge file (**AGENTS.md**) in v1; learned memory store (Mem0 on Qdrant) later |
| D14 | Central reporting | Org mode: **metrics and audit always central**; full prompt/response traces **optional per team**, admins can force them |
| D15 | TokenLean editions | Core works with **OSS TokenLean**; commercial TokenLean features (portal, signed attestation, FinOps) used as optional extras |
| D16 | Audit strength | **Tamper-evident from v1** (r2): Zyroflo's hash-chained run ledger + digest-pinned gates + CI ledger merge check; control plane mirrors and verifies the chain. Optional WORM copy and DSSE signing in phase 2 |
| D17 | Architecture | **One core, two modes**: standalone (CLI only) and org (CLI + small control plane) |
| D18 | Access control | **Simple RBAC**: roles = named permission sets; roles assigned to individuals (optionally scoped to a team) |
| D19 | Admin config home | **Git policy repo** for org policy and shared agents/pipelines; **control plane** for roles and assignments |
| D20 | Child-agent controls | Admin limits on number of children, per-child token/cost budget, loops/turns and wall-clock time |
| D21 | Init | `agentic init` per developer per repo (prereqs, skills, policy); `agentic init --repo` once per repo (shared files) |
| D22 | Shared index | Dependency graph **committed to `main`**, refreshed **nightly** from `main` by a CI bot |
| D23 | Integrations | **GitHub and Jira first**; platforms behind adapters |
| D24 | Enterprise defaults | The 13 enterprise defaults (data protection, repo classification, kill switch, rollout, agent quality metrics, dependency scanning, catalog, min CLI version, corporate network, control-plane availability, retention, notifications, concurrency) accepted, including the **admin kill switch** |
| D25 | Zyroflo reuse | Build fresh; reuse Zyroflo artefacts as much as possible (§1.4); Zyroflo remains the zero-dependency product |
| D26 | TokenLean LITE | A deployment profile of the same TokenLean codebase (no fork): gateway features on, behaviour-changing optimisations off for agent traffic, Qdrant and Langfuse optional; own `deploy/lite/` folder for local and cloud (§25) |
| D27 | LITE editions | LITE runs on the OSS TokenLean image by default; the commercial image is optional. In OSS LITE the admin/usage portal is the control plane + Grafana (§25) |
| D28 | Superpowers | Selected discipline skills by default; the full superpowers plugin only as a team opt-in for Claude Code / Codex steps (§24) |
| D29 | Learning loop | Human-approved learning loop for every functional agent in v1: capture corrections → attribute → repeat threshold → propose → explicit approval by the scope owner → apply/measure/undo; guardrail proposals in phase 2 (§24) |
| D30 | Ownership | Shared (org/team/repo) skills, memory and agents are read-only for users and changed only by the scope owner (org admin / team lead / repo maintainer via CODEOWNERS); personal skills and memory belong to each user; promotion on request (§23, §24) |
| D31 | Deny lists | Org, team and agent files may declare `deny` (shell prefixes, path globs); merged as a union, deny beats allow, children inherit (§5.6) |
| D32 | Backends | `opencode` gets a dedicated profile on the shared ACP backend (M2-57; revised after spike S7, was a dedicated adapter). Antigravity: S7 found it ACP-capable via Google's official ACP server and routable via `GOOGLE_GEMINI_BASE_URL`, so it becomes `acp:antigravity` in phase 2, conditional on a TokenLean Gemini route, path-prefix identity and a terms review; no dedicated adapter |
| D33 | Pipelines | Only org admins and team leads change pipelines (policy repo + CODEOWNERS); repo pipelines off by default in org mode; runs use the approved, hash-matched version (§6) |
| D34 | Zyroflo dependency | Zyroflo (`sdlc-harness`) is a pinned git dependency used unchanged behind thin adapters in `sdlc_commons`; gaps closed by small backward-compatible Zyroflo PRs (§1.4, spike S14) |
| D35 | Agent types | Every agent declares an org-defined type; the type (not the name) sets caps, ceilings and defaults; agents inherit and may only narrow; teams lower caps (§5.5) |
| D36 | HTTP client | `agentic.transport` is built on **httpx2** (the Pydantic team's maintained successor to httpx, BSD-3): the Anthropic SDK requires it and Pydantic AI drops plain httpx in v3; cost capture is a wrapping transport under the identity client (spike S1) |
| D37 | Handoff templates | Every handoff kind inherits one **generic base template** (fields and body sections). Org, team and repo layers may **add** fields and sections and **tighten** existing ones (optional → required, fewer choices), never remove, rename, loosen or retype them; new kinds (inheriting the base) only at org and team level; repo extensions are changed by PR approved by the repo maintainers (§7.4) |
| D38 | Sandbox | Anthropic `srt` (sandbox-runtime, Apache-2.0, pinned) on every OS behind one `agentic` sandbox interface: macOS Seatbelt, Linux bubblewrap, **Windows = `srt` Linux mode inside WSL2**. No unsigned binary is installed on developer machines, so native `srt-win` is excluded unless the org builds and signs it; a container backend is the later Windows alternative. With no sandbox, org policy decides per regime (default: team and regulated refuse; solo runs on policy guards with a warning - owner decision 2026-10-20, DF-72). Engine, gates, ledger, model calls, commits and pushes stay outside the sandbox (spike S6) |
| D39 | Code-intelligence index | The symbol index (code-review-graph, spike S5) is **built and updated locally** in `.agentic/local/index/`; nothing is committed to `main`. Repos whose full local build is too slow opt in to a nightly CI job that publishes a deterministic sharded text export outside `main` (artifact or `agentic-index` ref) for developers to import. A generated index is never merged (spike S9, §10.3) |
| D40 | Container runtime | The framework is **engine-neutral**: containers only through the Docker CLI surface and the Compose Specification, engine chosen by `CONTAINER_ENGINE` (docker or podman) or found on PATH; Docker Desktop is never required (it needs paid seats at 1000 people). Recommended org standard: Rancher Desktop (Apache-2.0); Podman Desktop supported (spike S12; purchase decision DF-102) |
| D41 | Agent evaluation | The eval harness (M1-63) uses pydantic-evals under our own blinding, judging and statistics layer: objective metrics first, a pointwise rubric judged by a **different model family** (admin-mapped tier `eval-judge`), k = 3 repeats, a paired sign-flip test decides; eval cases from internal repos live with the adopter (policy repo), never in this repo (spike S10) |
| D42 | Codex and Responses | TokenLean's `/v1/responses` (ST-4) **translates to chat by default** so every guardrail applies; passthrough is a later per-provider opt-in once guardrails cover Responses items. The codex backend runs only with `srt` as the boundary, its worktree marked untrusted, and reports its missing shell default-deny to policy (spike S3) |
| D43 | Supply chain | Local CI enforces import contracts (provider SDKs only in `agentic.transport`, Zyroflo only via `sdlc_commons`, optional extras confined), a licence allow-list over `uv.lock`, pip-audit and gitleaks; a fix for a critical known vulnerability may skip the two-week release-age rule, recorded each time (P0-03) |
| D44 | RBAC engine | Roles and permissions are checked with **pycasbin** (Apache-2.0), RBAC-with-domains (domain = tenant, team or repo within a tenant), behind one `authorize`/`require` interface; roles and assignments live in our own tenant-scoped tables (owner decision 77; supersedes the §1.2 non-goal of no general policy engine in v1) |
| D45 | Tenancy | The control plane is **multi-tenant from the start**: every tenant-owned row carries `tenant_id`, Postgres row-level security (forced, runtime role without BYPASSRLS) isolates tenants, the tenant comes only from the credential, another tenant's object answers 404; one TokenLean tenant per control-plane tenant (owner decision 76; supersedes the single-tenant assumption in §25) |

**Confirmed assumptions**: agents request a **capability tier** by default (exact model optional, admins can override); **preflight** validates every step before a run starts; **child agents inherit and can only narrow** their parent's tools, budget and limits.

---

## 3. Architecture overview

```
              ┌──────────────── Developer laptop / CI runner ─────────────────┐
              │  agentic CLI ──► core (pipeline engine, agents, policy checks, │
              │                  sandbox, backends, codeintel, telemetry)      │
              └───────┬────────────────────────────────┬──────────────────────┘
   every model call   │                                │  run/step/gate/audit events,
   (X-Agentic-* hdrs, │                                │  policy + kill-switch fetch
    traceparent)      ▼                                ▼
            ┌────────────────────┐           ┌─────────────────────────┐
            │     TokenLean      │           │      Control plane      │
            │ (the ONLY gateway) │           │  policy sync (git repo) │
            │ routing/overrides, │           │  RBAC + assignments     │
            │ keys, optimisation,│           │  event ingest + audit   │
            │ spend caps,        │           │  kill switch            │
            │ usage_events       │           │  later: webhooks, UI,   │
            └─────────┬──────────┘           │  VS Code API            │
                      ▼                      └────────────┬────────────┘
               LLM providers                              │
                      └──────────────┬────────────────────┘
                                     ▼
                 Shared Postgres + Grafana + OpenTelemetry/Langfuse
                (joined on request_id / trace_id → one dashboard)

  Git policy repo (org) ──► control plane ──► CLIs + TokenLean routing rules
```

### 3.1 Deployment modes

| | Standalone mode | Org mode |
|---|---|---|
| Who | Solo developer, evaluation, new repos | Organisation (~1000 users) |
| Components | CLI + core | CLI + core + control plane + shared Postgres/Grafana |
| Model calls | Through TokenLean (local or hosted) | Through the org's TokenLean |
| Policy | Local policy file (`~/.agentic/policy.yaml`) | Git policy repo, served by control plane |
| RBAC | None (single user has all permissions) | Enforced |
| Telemetry / audit | Run folder on disk, same event format | Mandatory central ingest (metrics + audit); traces per D14 |

The core library is identical in both modes; only the **sinks** (local vs control plane) and **policy source** differ.

### 3.2 Responsibility split

| Concern | TokenLean | Control plane | Core / CLI |
|---|---|---|---|
| Provider keys, routing, model override enforcement | ✅ | pushes routing derived from policy | — |
| Per-call tokens, cost, latency, prompts (Langfuse) | ✅ | joins for dashboards | reads cost headers into budget ledger |
| Pipeline execution, gates, loops, children | — | records events | ✅ |
| Policy definition | — | validates + serves | preflight checks |
| RBAC | — | ✅ | enforces locally too (fail fast) |
| Audit (pipeline + config) | security events only | ✅ store + chain | emits events, git trailers |
| Kill switch | enforces deny (enhancement) | ✅ source of truth | checks before every step / turn |

---

## 4. Repository layout

### 4.1 Framework monorepo `agentic-sdlc`

```
agentic-sdlc/
├── core/                  # Python library — all logic, no UI
│   ├── definitions/       # load + merge agent/pipeline files (org > team > repo), schemas
│   ├── policy/            # OrgPolicy model, tier resolution, RBAC check, preflight
│   ├── engine/            # pipeline state machine, run folder, checkpoint/resume,
│   │                      #   gates, loops, branches, limits, stuck detector, kill-switch checks
│   ├── agents/            # AgentRunner interface (Pydantic AI inside), child-agent spawner,
│   │                      #   budget ledger
│   ├── transport/         # shared HTTP client: X-Agentic-* headers, traceparent, cost parsing
│   ├── backends/          # native | claude-agent-sdk | codex | acp ; later remote (Devin…)
│   ├── sandbox/           # worktree manager, srt / Codex sandbox wrappers, shell-policy compiler
│   ├── handoffs/          # front-matter parse, schema validation, step compatibility check
│   ├── codeintel/         # interface + adapters (code-review-graph, codebase-memory-mcp, Graphify)
│   ├── skills/            # catalog client, installer, loader (Agent Skills / SKILL.md)
│   ├── memory/            # AGENTS.md read / propose-update ; Mem0 adapter later
│   ├── integrations/      # github, jira adapters (others later)
│   ├── security/          # secret scan before egress, untrusted-content wrapping
│   ├── telemetry/         # OTel spans, audit event model (hash-chain fields), sinks
│   └── auth/              # credential provider: env → OS keyring ; OIDC later
├── cli/                   # `agentic` command
├── control-plane/         # FastAPI + Postgres service
│   ├── policy_sync/       # pull/validate policy repo, serve versions, push routing to TokenLean
│   ├── rbac/              # roles, permissions, assignments
│   ├── ingest/            # event API (runs, steps, gates, limits, audit), offline-queue replay
│   ├── audit/             # append-only store, hash chain
│   ├── killswitch/        # scoped stop switches
│   ├── notify/            # email in v1 ; Slack/Teams later
│   └── dashboards/        # Grafana dashboard JSON + SQL views
├── library/               # shipped content (not code)
│   ├── agents/            # ba, design, developer, code-review, qa
│   ├── pipelines/         # feature-delivery, bugfix, …
│   ├── handoff-templates/ # requirements, design (with impact), dev-report, review, test-report
│   └── skills/            # vendored, pinned, approved skills (§11)
└── templates/
    └── policy-repo/       # starter org policy repo
```

### 4.2 Files added to a target repository

```
<target-repo>/
├── AGENTS.md                         # committed — repo knowledge (human-reviewed); init drafts it only when none exists
├── .agentic/
│   ├── config.yaml                   # committed — repo settings, classification, team, pipelines, codeintel config
│   ├── agents/                       # committed — repo-level agents (added later, e.g. `agentic agents new`)
│   ├── pipelines/                    # committed — repo-level pipelines, run only when org policy sets allow_repo_pipelines (D33)
│   └── local/                        # git-ignored (its own .gitignore) — runs/, state/, skills/, index/, audit/, init.json
├── .gitattributes                    # the repo's own file plus an agentic block: LF for definitions, the index marked generated
└── .github/workflows/agentic-index-export.yml   # only when org policy sets index_export_workflow (D39)
```

`agentic init --repo` proposes exactly `.agentic/config.yaml`, `AGENTS.md` (when the repo has none, in any letter case), `.gitattributes` and, when org policy asks for it, the index export workflow - on a new `agentic/init` branch and pull request. It never adds `.agentic/pipelines/`, and nothing under `.agentic/local/` or any index is committed (D39: the index is built locally; the opt-in export is published outside `main`, §10.3).

### 4.3 Org policy repo `agentic-org-policy` (one per org)

```
agentic-org-policy/
├── policy/
│   ├── tiers.yaml            # tier → model/effort/temperature defaults
│   ├── agent-types.yaml      # agent types: caps, ceilings, defaults (D35)
│   ├── tools.yaml            # tool catalog, forbidden tools, org deny list (D31)
│   ├── limits.yaml           # concurrency limits
│   ├── org.yaml              # allowed repo patterns, tool mirror, allow_repo_pipelines (D33),
│   │                         #   index_export_workflow (D39), sandbox, secret scan
│   ├── tool-pins.yaml        # hash-pinned tools init fetches from the org mirror (M1-51)
│   ├── prices.yaml           # model prices for calls the gateway does not price
│   ├── eval.yaml             # agent evaluation defaults (repeats, budgets, targets; M1-63)
│   ├── classifications.yaml  # repo sensitivity classes and what each allows
│   ├── retention.yaml        # retention per data type, trace capture policy per team
│   ├── licences.yaml         # blocked licences for dependency scanning
│   └── teams/<team>.yaml     # team-lead narrowing incl. team deny list (cannot loosen)
├── agents/                   # org-shared agents (versioned)
├── pipelines/                # org-shared pipelines (org admins only, D33)
├── teams/<team>/            # team layer: agents/, pipelines/ (+ overlays/), skills/
├── skills/catalog.yaml       # approved skills, pinned by hash
└── CODEOWNERS                # org admins own policy/; policy/teams/<team>.yaml and teams/<team>/
                              #   also that team's leads (tighten-only narrowing; decision 93)
```

---

## 5. Agent definitions

### 5.1 Template

An agent is one Markdown file with YAML front matter (Claude-Code-compatible base fields, extended). The **body is the plain-English job description**.

```markdown
---
name: developer
type: developer                    # org-defined agent type (D35): caps, ceilings, defaults
version: 1.2.0
owner: platform-team
status: active                     # active | deprecated
description: Implements an approved design as small, tested commits.
tier: balanced                     # deep-reasoning | balanced | fast (admin-mapped)
model: null                        # optional exact model; admin policy may override
effort: medium
temperature: 0.2
backend: native                    # native | claude-code | codex | acp:<agent>
tools: [read_files, search, edit_files, run_shell, codeintel, spawn_agent]
shell: [pytest, "npm test", "mvn -q test", ruff]   # allow-listed command prefixes
skills: [superpowers/test-driven-development, superpowers/systematic-debugging,
         superpowers/verification-before-completion]
consumes: [design]                 # handoff kinds required as input
produces: dev-report               # handoff kind produced
limits:                            # requested; admin policy caps them
  budget_usd: 5
  max_turns: 80
  max_minutes: 60
children:
  max_total: 5
  max_concurrent: 2
  max_depth: 2
  per_child: { budget_usd: 1, max_turns: 30, max_minutes: 15 }
deny:                              # what this agent may never do (D31)
  shell: ["git push", "git commit --amend"]
  paths: [".github/**", "infra/**"]
---
You are the Developer. Read the approved design handoff and implement it in
small commits. Write a failing test first, then the code...
```

### 5.2 Field ownership (layering)

| Field group | Who controls | Rule |
|---|---|---|
| `description`, body | Repo / team / org layer | Nearest layer's definition wins as a whole (repo > team > org) |
| `type` | Required in every agent | Must be an org-defined agent type (§5.5) |
| `tier`, `model`, `effort`, `temperature` | Inherited from the type if unset | Must resolve within org/team policy; an explicit model must be approved for this repo class and team |
| `backend`, `tools`, `shell`, `skills`, `consumes` | Inherited from the type if unset | May only narrow the type's ceiling, and must satisfy org policy, repo class and team |
| `produces` | Inherited from the type | Must equal the type's kind when the type fixes one |
| `limits`, `children` | Inherited from the type's caps if unset | Clamped to min(request, type cap, team cap); caps come from the type, never the name |
| `deny` | Any layer | Merged as a union with org and team denies; deny beats allow (§5.6) |

Restricted fields are **never merged** across layers; the org policy (plus team narrowing) is their only source of truth, and preflight checks each definition's *request* against it. This avoids needing a config library with field locking (none exists — research finding).

### 5.3 Capability tiers

Defined only in `policy/tiers.yaml`, optionally per team:

```yaml
tiers:
  deep-reasoning: { model: anthropic/claude-opus,   effort: high,   temperature: 1.0 }
  balanced:       { model: anthropic/claude-sonnet, effort: medium }
  fast:           { model: google/gemini-flash }
team_overrides:
  payments: { deep-reasoning: { model: openai/gpt-5 } }
```

The CLI resolves tier → model for preflight; the request still carries `X-Agentic-Tier`, and TokenLean routing rules (generated from the same policy) remain the enforcement point.

**Backend capability matrix**: each backend declares which settings it honours (e.g. Claude Code: model and effort yes, temperature no). Policy decides whether an unhonoured setting is `ignore`, `warn` or `block`.

### 5.4 Built-in agents (library)

| Agent | Purpose | Default tier | Backend | Produces |
|---|---|---|---|---|
| Business Analyst | Elaborate requirements, ask clarifying questions (routed to human), acceptance criteria | balanced | native | `requirements` |
| Design | Architecture, cloud-native considerations, dependency impact analysis via codeintel | deep-reasoning | native | `design` |
| Developer | Implement in worktree, TDD | balanced | native or claude-code / codex | `dev-report` |
| Code Review | Review diff vs design + impact list, security, quality | balanced | native | `review` |
| QA | Test strategy, run tests, dependency licence/vulnerability scan, verify acceptance criteria | balanced | native | `test-report` |

Built-in agents take their stage instructions from Zyroflo's SOPs: BA ← P0 intake + P1 requirements (EARS acceptance criteria, options incl. ≥1 OSS option with licence); Design ← P2 scoping + P3 design (ADR triggers); Developer ← P4 implementation; QA ← P5 test (**baseline test run captured at intake; gate on "no new failures"**) and P10 verify; Code Review ← P6 review (deterministic pre-pass with gitleaks / ast-grep / osv-scanner **before** the agent review, then severity-ranked findings). Additional library agents from the remaining stages: Triage (P7), Release (P8: CI watch, changelog, tag), Deploy (P9: fresh cost confirmation each time), Verify (P10: probes, rollback proposal, defect), Closeout (P11: every criterion checked, tech debt fed back to intake).

### 5.5 Agent types (D35)

The org defines agent types in `policy/agent-types.yaml` (e.g. business-analyst, design, developer, code-review, qa, learning-analyst, general). A type is the org-approved template for one kind of agent:

| Type setting | Meaning |
|---|---|
| `tier` | default tier for agents of this type |
| `backends` | allowed backends; the first is the default |
| `tools`, `shell`, `skills`, `consumes` | ceiling and default: agents may use a subset |
| `produces` | the handoff kind every agent of this type writes |
| `limits.max`, `limits.children_max` | caps; **every field required**, so nothing is unlimited by omission |

A default developer agent and a custom one are both of type `developer`, so the same caps apply; naming an agent `developer` earns nothing. Teams lower caps per type in `teams/<team>.yaml` (`limits_max`), never raise them. Custom agents start from their type's template (`agentic agents new <name> --type <type>`).

### 5.6 Deny lists (D31)

Org policy (`tools.yaml`), each team policy and each agent file may declare `deny: {shell: [...], paths: [...]}`. Shell entries are command prefixes (`git push` also blocks `git push --force`); paths are globs the agent may never write. Deny lists are the one setting merged across layers — as a union, because adding a restriction is always safe. Deny beats allow; an agent listing the same command in both is rejected as contradictory; child agents inherit their parent's denies. Enforcement at run time uses Zyroflo's guards (`policy.denied_command`, `protected`, …) via `sdlc_commons`.

### 5.7 Templates

`templates/agent/agent.md` and `templates/pipeline/pipeline.yaml` show every setting with a commented sample, its allowed values and what applies if it is left out. Tests keep both honest: every model field must appear, and every sample must be valid when uncommented.

---

## 6. Pipeline definitions

GitHub-Actions-like YAML, borrowing node semantics from Archon v2 and gate/overlay semantics from spec-kit.

**Who may change pipelines (D33):** only org admins and team leads. In org mode pipelines live in the policy repo — `pipelines/` (org admins) and `teams/<team>/pipelines/` (that team's leads, including per-repo overlays) — and change only through PRs approved via CODEOWNERS with branch protection. Repo-level `.agentic/pipelines/` is ignored unless org policy sets `allow_repo_pipelines: true`. A run uses only the approved, hash-matched version; a locally edited pipeline is refused. Standalone mode keeps local pipelines.

```yaml
name: feature-delivery
version: 1.0.0
inputs:
  request: { from: [text, file, jira] }
steps:
  - id: requirements
    agent: business-analyst
  - id: design
    agent: design
    when: "requirements.size != 'trivial'"
  - gate: design-approval
    approver_role: team-lead          # role required to approve
    four_eyes: false                  # true → approver must differ from run starter
  - loop:
      id: build-review
      max_iterations: 3
      until: "review.open_blocking == 0"   # Zyroflo exit rule; score reported, never the gate
      on_exhausted: gate              # escalate to a human gate
      steps:
        - id: develop
          agent: developer
        - id: review
          agent: code-review
  - gate: review-approval
    approver_role: approver
  - id: qa
    agent: qa
  - id: publish
    action: github.open_pr            # built-in action, not an agent
```

- `when` / `until` are expressions over handoff front-matter fields, evaluated by a **safe expression evaluator** (OSS to be chosen; candidates cel-python, simpleeval — spike S13).
- **Overlays** (M2-23): a team tailors an org pipeline for one repository without forking it, in `teams/<team>/pipelines/overlays/<repo>.yaml` in the policy repo (D33: only the team's leads and org admins change it; the file name is the repository's name, `repo: host/owner/name` binds it to exactly one). An overlay may add steps (`after:`/`before:` a named step), remove steps, gates and loops that are not org-required, turn `four_eyes` on, set `on_reject` or (not for an org-required gate) the approver role, tighten the regime and lower `max_minutes`; anything else is refused with the rule named. The resolved pipeline (base + overlay) is what preflight checks and the run plan shows, and the control plane's approved-hash check covers the base file and the overlay file (a run that leaves out the overlay applying to it is refused).
- Gates: **approve**, **reject with feedback** (routes back per pipeline), or **edit the handoff then approve** (opens `$EDITOR`).
- **Failures route back, never complete (DF-238):** work goes back on a failure through a loop whose `until` reads the judging handoff (e.g. QA inside the build loop, running every round so the loop always ends on a report about the current change, `until: "review.open_blocking == 0 && test_report.verdict == 'pass'"`). An exhausted loop's gate refuses a bare approval while `until` is false: a person edits a pinned handoff so it holds (marking a review finding accepted, or naming a failing test or criterion in the test report's `accepted_failures` - an attributable acceptance that only a person may write, never an agent), rejects with feedback for another pass, or stops the run. Only blocking findings count towards a loop's oscillation check. A run whose newest review or test report still reports a failure at the end pauses `failing-handoff` instead of completing.
- Preflight compiles the pipeline and checks step N `produces` matches step N+1 `consumes`.

### 6.1 Gate model (from Zyroflo)

- **Gate regimes** per pipeline or repo: `solo` (starter may approve), `team` (approver role, optional four-eyes), `regulated` (four-eyes mandatory, signed approvals, required evidence).
- **Org-required phases and gates**: org policy (`org.yaml` `required_steps: {<pipeline>: [<step, gate or loop>]}`) declares steps/gates (e.g. review, security pre-pass, QA) that overlays can never remove or weaken; preflight rejects a resolved pipeline that lacks one, whatever dropped it.
- **Gate re-anchoring**: when an overlay removes a step, its gate re-anchors to the previous remaining step (with none before it, right after the next one) rather than disappearing; a gate whose `on_reject` named the removed step routes to the step now in its place. Each re-anchoring is shown with the run's plan.
- **Diff-size budget per review batch**: the Developer splits work so each gated diff stays under a configurable size, keeping human review readable.
- **Digest pinning**: an approval records the digest of the exact handoff and diff approved; any later change invalidates the approval (§7.3).

### 6.2 Library pipelines

- `feature-delivery` — BA → Design → (Developer ↔ Code Review ↔ QA loop) → PR (the v1 acceptance pipeline; a QA fail goes back to the Developer).
- `full-lifecycle` — Zyroflo P0–P11: intake → requirements → scoping → design → implementation → test → review → triage → release → deploy → verify → closeout, with gates G1–G5; profiles select which stages run.
- `bugfix` — intake (with baseline tests) → (Developer ↔ Review ↔ QA loop) → PR.

---

## 7. Handoffs

### 7.1 Format

Markdown body for humans; YAML front matter for machines. Envelope fields (framework-owned) + kind-specific fields (Pydantic model per kind).

```markdown
---
kind: design
schema_version: 1
run_id: r-2026-10-16-7f3a
step: design
agent: design@1.4.0
produced_at: 2026-10-16T10:42:00Z
status: complete                  # complete | partial | blocked
decisions:
  - id: D1
    choice: "Add async job queue for exports"
    rationale: "Export latency exceeds API timeout"   # the WHY, required
impact:
  modules: [billing/export.py, api/routes/export.py]
  interfaces: [GET /v1/exports]
  tests: [tests/test_export.py]
  confidence: candidate            # graph-derived lists are candidates to verify
risks: [...]
---
## Design: async exports
...
```

### 7.2 Kinds and templates

| Kind | Key structured fields | Body template source |
|---|---|---|
| `requirements` | items[id, statement, acceptance_criteria[]], size, open_questions | spec-kit spec template (adapted) |
| `design` | decisions[choice, rationale], impact{modules, interfaces, tests}, risks | spec-kit plan + MADR (licence to confirm) |
| `dev-report` | commits[], files_changed, tests_run, deviations_from_design | own |
| `review` | findings[severity, file, line, issue, status], open_blocking (count), score (reported only), prepass_results | Zyroflo P6 + `review-rubric.md` |
| `test-report` | verdict, tests{passed, failed}, acceptance_results[], dependency_scan{licences, vulns} | own |

Validation: python-frontmatter + Pydantic; invalid output triggers an automatic retry with the validation errors, then a human gate.

### 7.3 Run ledger (from Zyroflo)

Each run keeps Zyroflo-format, hash-chained `events.jsonl` and `approvals.jsonl` in its run folder. Every handoff and gated diff is recorded by **digest**; an approval entry pins those digests and chains to the previous entry. The engine refuses to advance past a gate without a valid approval entry. In org mode the entries are also sent to the control plane, which verifies the chain on ingest (§14.5). For runs that produce a PR, a compact ledger summary is committed on the branch and the **CI ledger merge check** (Zyroflo `ledger-check.yml`) blocks merging when gates are unapproved or the chain is broken.

### 7.4 Template inheritance and extensions (D37)

Each handoff schema is built from layers, top to bottom; every layer may only add or tighten, so a handoff valid for a lower layer is always valid for every layer above it:

| Layer | Defines | Where | Changed by |
|---|---|---|---|
| Base | Fields every handoff has: `status` (envelope), `summary` (one line, required), `open_questions` (list). Sections: `## Summary` first, `## Open questions` last | framework | framework releases |
| Kind | The five built-in kinds of §7.2, each adding its fields and sections to the base | framework | framework releases |
| Org | Extensions of any kind; new kinds that inherit the base | policy repo `handoffs/<kind>.yaml` | org admins (CODEOWNERS) |
| Team | Extensions of any kind; new kinds that inherit the base | policy repo `teams/<team>/handoffs/<kind>.yaml` | that team's leads |
| Repo | Extensions of existing kinds only | `.agentic/handoffs/<kind>.yaml` | repo maintainers (PR, CODEOWNERS) |

- **Attributes** are front-matter fields (`string`, `integer`, `number`, `boolean`, `list` of strings, `enum`; each with a description and an example) and body sections (a `##` heading with guidance). A section is required: the agent's document must contain its heading.
- **Allowed**: add a field (optional or required) or a section; make an optional field required; narrow an `enum` (or a built-in choice field) to fewer values. **Refused at load**, naming the file and line: removing, renaming, loosening or retyping any field or section from a layer above, and new kinds defined at repo level.
- Merged order: base `## Summary`, the kind's sections, org, team and repo sections, base `## Open questions`. Fields merge by name.
- The template an agent receives (§7.2), the validation of its answer, the retry with errors and a human's edit at a gate all use the same merged schema. A run fixes its merged schemas at start (their digest is recorded in the run), so a policy change mid-run does not change what a step must produce.
- A type's or agent's `produces` / `consumes` must name a kind in the merged catalog; an agent file naming an unknown kind is refused when definitions are discovered, and an agent type naming one is refused in preflight.
- Phase 0–M1 scope: top-level fields only (no nested objects in extensions); tightening of nested built-in fields (e.g. `findings[].file`) is later.

---

## 8. Run lifecycle

### 8.1 Initialisation

**`agentic init --repo`** (repo owner, once; commits files):
creates `.agentic/config.yaml`, `AGENTS.md` (from a scan of the repo), codeintel config, `.gitattributes`, the nightly index workflow, and the first index build.

**`agentic init`** (each developer, once per repo; nothing committed):
1. Check prerequisites: Python, git (worktree support), sandbox availability (`srt` + Node.js on macOS/Linux; on Windows `srt` inside WSL2, D38), installed backend CLIs, network/proxy/TLS settings.
2. Authenticate: validate proxy key with TokenLean; register with the control plane (org mode); check minimum CLI version.
3. Fetch org policy; cache it locally with expiry.
4. Install the approved skills used by the agents in this repo's pipelines into `.agentic/local/skills/` (pinned by hash).
5. Pull the latest committed index; set up the local git-ignored overlay.
6. Print a readiness report (what passed, what to fix).

Exactly what `init` downloads (and what it never installs), the eight repo-root and safety checks it runs before writing anything, and the full first-time setup are in §24.

### 8.2 Preflight (before every run)

Checks, all before any spend: kill switch state · CLI version · RBAC (`run.trigger` on this pipeline/repo) · each step's tier/model resolves and is allowed for this team and repo classification · backend allowed and installed · tools/shell/skills within policy · limits clamped · team budget remaining (TokenLean, when the remaining-budget API exists) · concurrency cap · handoff compatibility · Jira/GitHub credentials if used. Output: pass, or a report with the exact reason and the resolved policy values.

### 8.3 Execution

- The engine is a **state machine** persisted to `.agentic/local/runs/<run-id>/run.json` (atomic writes). Each step is a checkpoint; steps are designed to be idempotent.
- Each code-editing step runs in its **own git worktree** branched from the previous step's result.
- Each step's inputs are the handoffs it consumes plus its instructions and skills — not the previous agents' chat histories.
- Gates **persist and exit**; `agentic approve <run> --gate <id>` (or `reject --feedback`, or `edit`) resumes. `agentic resume <run>` restarts after a crash from the last completed step.

### 8.4 Child agents and limits (admin controls)

- An agent with `spawn_agent` may create children for sub-tasks (e.g. Review spawns security/performance reviewers).
- Children **inherit and can only narrow**: tools ⊆ parent tools; model tier via policy; budget **sliced from the parent's ledger**; same sandbox (or a sub-worktree).
- Admin-controlled limits (per agent type, D35; team-narrowable): `max_total` children per step, `max_concurrent`, `max_depth`, per-child `budget_usd` / tokens, `max_turns`, `max_minutes`; plus per-step and per-run limits.
- On a child limit hit: the child stops and returns a partial result marked `stopped: budget|turns|time|stuck`; the parent decides.
- On a parent/step/run limit hit: the run **pauses for a human** (approve more budget, or stop).
- Every limit hit emits an audit event and a dashboard metric.

### 8.5 Stuck-loop detection

Port of OpenHands `StuckDetector` (MIT, with attribution): repeated identical actions, repeated errors, ping-pong alternation, monologue loops. Plus a pipeline-level check: successive review findings / developer outputs that repeat across loop iterations end the loop early and escalate to a gate.

### 8.6 Kill switch

- **Scopes**: global · team · user · repo · pipeline · agent (name/version) · model or backend.
- **Who**: org admins (global/any scope); team leads (own team).
- **Effect**: the CLI checks the switch before every step and on every agent turn (lightweight poll with short cache, e.g. ≤ 30 s); matching runs stop immediately, children are terminated, worktrees are kept for inspection, and the run is marked `killed` with who/why.
- **Defence in depth**: the control plane also pushes a deny rule to TokenLean (enhancement §17) so in-flight model calls for the killed scope fail even if a client misbehaves.
- **Offline behaviour**: in org mode, if the CLI cannot confirm switch state beyond the policy-cache expiry, it pauses rather than continues.
- Every switch change is audited (who, scope, reason, time).

### 8.7 Failure handling

Model/proxy errors: bounded retries with backoff (TokenLean already fails over between providers). Validation failures: retry with errors, then gate. Backend crash: step marked failed, resumable. Control plane unreachable: events queue locally and replay later (except kill-switch rule above).

---

## 9. Backends and sandboxing

### 9.1 Backends

| Backend | Reuse | Governance through TokenLean |
|---|---|---|
| `native` | Pydantic AI v2 behind our `AgentRunner`; native openai/anthropic SDKs with injected httpx client | Full: model, headers, cost, tools |
| `claude-code` | Claude Agent SDK (Python, MIT) — locked down: `--bare`, `--json-schema`, `--permission-mode dontAsk`, managed `allowedProviders: ["customEndpoint"]` | Model + proxy routing; tools via allowed-tools; no temperature |
| `codex` | `codex exec --json --output-schema` (Apache-2.0) with `[model_providers]` pointing at TokenLean (`wire_api = "responses"`) | Needs TokenLean Responses API endpoint (§17) |
| `opencode` (M2, D32) | ACP profile (`opencode acp --pure`) on the shared ACP backend (S7); lock-down via `OPENCODE_CONFIG_CONTENT` (provider `baseURL` → TokenLean, model forced) and `OPENCODE_PERMISSION` from the shell policy and deny lists; wrapped in `srt`/container (no own sandbox); project `opencode.json` and `.opencode/` merge under the inline config, so neutralise project `mcp`, `instructions` and plugins, set every permission key, `small_model` = the resolved model | Model-agnostic harness fully routed through TokenLean |
| `acp:<agent>` (phase 2) | `agent-client-protocol` Python SDK (Apache-2.0), ACP v1, pinned 0.12.1 (S7); Gemini CLI (`--acp`), Goose (`goose acp`), Copilot CLI (`--acp --stdio`, BYOK + `COPILOT_OFFLINE`), OpenCode (`opencode acp --pure`), Antigravity (official `agy_acp_server`, API key + `GOOGLE_GEMINI_BASE_URL`); the ACP permission callback is a second gate - agent-native policy and `srt` stay the boundary; result from a validated final message; cost from TokenLean | Per-agent profile as data |
| `remote:devin` etc. (phase 3) | submit / poll / collect-PR adapter | Not routable via TokenLean; policy can only allow/deny |

Each backend has **contract tests** pinned to a tool version; flags change fast (research finding).

### 9.2 Sandbox

- **Isolation**: one git worktree per code-editing step; the step's diff is the reviewable artefact; rollback = discard worktree.
- **OS enforcement**: Anthropic `sandbox-runtime` (`srt`, Apache-2.0) on every OS: macOS Seatbelt, Linux bubblewrap, Windows via WSL2 (D38; native `srt-win` only if org-signed, container backend later); CI → Docker/Podman containers; optional self-hosted E2B pool later.
- **Worktree manager**: ported from Zyroflo `worktree.py`.
- **Shell policy**: one rule format (modelled on Codex execpolicy: prefix → allow / prompt / forbid) compiled into each backend's native permission format, **plus Zyroflo `guard.py` walls**: the `deny_commands` destructive-command matcher, protected paths, and diff budget. The OS/container sandbox is the real boundary; prefix rules and guards are a convenience layer.
- Repos where raw git is unsafe can set a commit-command override (Zyroflo feature).
- Network egress from sandboxes limited to TokenLean and allow-listed package registries.

---

## 10. Code intelligence and memory

### 10.1 Interface

`codeintel` exposes tools to agents: `dependents(symbol|file)`, `impact(diff)` (blast radius incl. tests), `tests_for(file)`, `path(a, b)`, `overview()`, `context_pack(task, budget)`. Two graph levels:

- **Symbol level** (functions, classes, calls inside a repo): **code-review-graph** (MIT, primary), **codebase-memory-mcp** (MIT, fallback for large / multi-language / Windows-heavy repos), **Graphify** (Apache-2.0, optional overview).
- **Service / domain level** (services, endpoints, HTTP calls, Kafka topics, DB tables and migrations, Helm/compose deploys, config and env keys, build deps, tests — across repos): **Zyroflo `tools/graph`** (`domain-graph` adapter; direction-aware damped impact with file:line provenance, budgeted context packs, MCP server). Covers Java, Python, JS/TS. Measured recall is still low, so its output is also treated as candidates.

Agents can use both: domain-graph for "which services/topics/tables does this change touch", symbol graph for "which functions/tests". Precise references on demand via **multilspy** (MIT); Python test confirmation via **pytest-testmon** (MIT). A bake-off on real repos picks the default (spike S5).

Graph answers are **candidates**: the Design handoff lists impact as `confidence: candidate`; Review and QA verify.

### 10.2 On-demand exploration

Always available (read/search tools). If no index exists for a language, agents fall back to exploration.

### 10.3 Index: built locally; an opt-in export for large repos (D39, spike S9)

- **Local by default.** `codeintel` builds the symbol index on first use (4–7 s on the S9 repos) into the git-ignored `.agentic/local/index/`. It refreshes with an incremental `update` whenever the index's recorded commit is not the checked-out HEAD (1–3 s after a pull). Nothing is committed to `main`: no bot commits, no branch-protection bypass, no merge driver, no LFS.
- **Never commit the raw index.** code-review-graph's database stores absolute paths (another checkout refuses it), is rewritten whole every night (timestamps), and grows about 2.7 MB per night on a modest Java repo (S9).
- **Opt-in export for slow repos.** When a repo's full local build is too slow (a policy threshold, proposed at 60 s), a nightly CI job (service account) builds once and publishes a **deterministic, path-independent sharded text export** (one sorted JSONL file per source file, `${ROOT}` for the repo root, a manifest with sha256 values and the tool and schema version) **outside `main`**: a CI artifact or an `agentic-index` ref. Developers import it (seconds) and run `update`.
- **A generated index is never merged.** If an org nevertheless commits the export, it is marked `linguist-generated -diff` and written only by the bot; on a conflict either side is taken and the index regenerated. `merge=union` silently corrupts it (S9).

### 10.4 Memory

- **v1**: `AGENTS.md` (Linux Foundation–stewarded convention). Agents read it; agents may **propose** additions in their handoff; humans accept via normal PR review. Where Claude Code needs `CLAUDE.md`, a one-line `@AGENTS.md` file (no symlinks — Windows).
- **Later**: Mem0 (Apache-2.0) on TokenLean's existing Qdrant, behind a `MemoryStore` interface; learnings promoted back into AGENTS.md by review.

---

## 11. Skills

### 11.1 Mechanism

Agent Skills standard (`SKILL.md`, agentskills.io). Supported by Claude Code, Codex, Gemini CLI, Copilot, Cursor, OpenCode, Goose, OpenHands and others; in the native loop the framework injects the skill catalog (name + description) and exposes an `activate_skill` tool restricted to the agent's allowed skills. For weaker/local models, a role's skills are **pre-loaded** instead of discovered.

### 11.2 Default skills per built-in role

| Role | Default skills (all MIT unless noted) |
|---|---|
| Business Analyst | interview / grill-with-docs (questions routed to a human gate) · spec-driven-development / to-spec · spec-kit spec template |
| Design | documentation-and-ADRs · api-and-interface-design · superpowers writing-plans |
| Developer | superpowers test-driven-development · systematic-debugging · verification-before-completion · incremental-implementation |
| Code Review | addyosmani code-review-and-quality · superpowers code-review checklist · security-and-hardening · *optional* Trail of Bits differential-review (CC-BY-SA-4.0, legal review) |
| QA | webapp-testing / browser-testing (anthropics/skills, Apache-2.0 subset) · verification-before-completion · *optional* Trail of Bits property-based / mutation testing |
| Admins | skill-creator · superpowers writing-skills |

Sources: obra/superpowers (selected skills only), addyosmani/agent-skills, mattpocock/skills, anthropics/skills (Apache-2.0 subset only), spec-kit templates. **r3:** the full per-skill catalog (loading mode, licence, selection rule), the superpowers plugin opt-in (D28), personal vs shared skills and promotion (D30), and the human-approved learning loop (D29) are specified in §24.

### 11.3 Governance

- Private, vendored **catalog** in the policy repo; every skill pinned by content hash with source, version and owner.
- Admission checks: licence · `skills-ref validate` · size budget · static + LLM scan of `SKILL.md` and scripts · human review for any skill with scripts · **A/B evaluation before promotion** (benchmarks show many public SWE skills add no value and some hurt).
- Runtime: per-agent skill allow-list; ≤ 3–5 pre-loaded skills per role; skill scripts run only in the sandbox; repo-level skills only for trusted repos; vendor telemetry stripped; every activation logged.

### 11.4 Explicitly excluded

- Superpowers `using-superpowers` bootstrap ("always invoke"), `subagent-driven-development`, worktree/branch workflow skills — duplicate the orchestrator and conflict with role prompts.
- **Caveman** — no meaningful total-cost saving in independent tests (cost is input/cache-dominated), accuracy collapse on small local models, meaning drift harmful to human-reviewed handoffs and audit, telemetry/cloud mode. Inter-agent messages use structured schemas instead.

---

## 12. Governance

### 12.1 Policy-as-code

Org policy lives in the git policy repo (§4.3). Flow: admin edits YAML → PR → review by another admin → merge → control plane pulls, **validates against schema** (rejects invalid), versions it (commit SHA), serves it to CLIs, and generates TokenLean routing rules. Every run records the policy version used. v1: CODEOWNERS restricts merges to org admins; later: control plane verifies the merging user holds `policy.edit.org` and ignores unauthorised commits.

### 12.2 Layering precedence

org policy (restricted fields, authoritative) → team narrowing (can only tighten) → user/repo definitions (instructions, schemas, pipeline shape; restricted fields are requests only).

### 12.3 RBAC

> **r3:** personas, who does what and when, the full default permission matrix (adds Repo maintainer derived from CODEOWNERS and Executive viewer) and the ownership rule (D30) are in §23; §23 wins where this section differs.

Roles are named permission sets defined in the control plane; admins can edit or add roles. Assignments: `user → role [→ team scope]`; a user can hold several roles.

**Permissions** (initial set): `run.trigger`, `run.cancel.own`, `run.cancel.any`, `gate.approve`, `agent.propose.repo`, `agent.approve.repo` (repo maintainers, D30), `agent.publish.team`, `pipeline.publish.team` (team leads; pipelines are admin-controlled, D33), `skills.personal.edit`, `policy.edit.org`, `policy.edit.team`, `budget.raise.team`, `killswitch.team`, `killswitch.global`, `rbac.assign`, `dashboard.view.own|team|org`, `audit.read.team|org`, `trace.view.team|org`, `skills.approve`.

**Default roles**

| Role | Scope | Key permissions |
|---|---|---|
| Org admin | Org | everything incl. `policy.edit.org`, `killswitch.global`, `rbac.assign`, `skills.approve` |
| Team lead | Team | `policy.edit.team` (narrow only), `agent/pipeline.publish.team`, `budget.raise.team`, `killswitch.team`, `gate.approve`, `dashboard.view.team`, `audit.read.team` |
| Developer | Own repos/runs | `run.trigger`, `run.cancel.own`, `agent.propose.repo` (PR; repo maintainer approves), `skills.personal.edit`, `gate.approve` (where gate allows starter), `dashboard.view.own` — cannot change pipelines (D33) |
| Approver | Assigned | `gate.approve`, `dashboard.view.team` |
| Auditor | Org or team, read-only | `audit.read.*`, `dashboard.view.*`, `trace.view.*` |
| Service account | Team | `run.trigger` only (cannot approve gates) |

**Gate approvals**: each gate names the required role; by default the run starter may approve if they hold it; `four_eyes: true` requires a different person.

### 12.4 Repo classification

Each repo has a classification label (e.g. `public`, `internal`, `confidential`, `restricted`) in `.agentic/config.yaml`, validated against `classifications.yaml`. A class restricts allowed tiers/models/providers (e.g. `restricted` → local models only), backends (e.g. no remote agents), trace capture, and skills.

### 12.5 Versioning and rollout

Agents, pipelines and skills are semver-versioned in the policy repo (M2-15). A new version of an agent or pipeline is rolled out team by team in `policy/rollout.yaml` (`<name>: {version, previous, teams: [...]}`), the previous version kept under `<layer>/agents|pipelines/versions/<name>/<version>.*`; a team's leads may pin a version (`pins:` in `policy/teams/<team>.yaml`). Resolution order: the team's pin, else the rollout's `previous` for a team not in its `teams` (org definitions only: a team's own is the team's to pin), else the current file; then the team's overlay for the repository. Deprecated versions (`deprecated:` in `rollout.yaml`, agents, pipelines and skills) warn until `blocked_from`, then block - in the CLI (org and standalone policy) and at run registration by the control plane's clock, which also refuses a version other than the team's pin and a new version run by a team outside its rollout. Repo-layer definitions are never versioned. A pipeline pinning agent versions is not offered yet.

### 12.6 Catalog

The policy repo is the catalog of agents, pipelines and skills (owner, version, status). `agentic agents list|search`, `agentic pipelines list`, `agentic skills list`.

### 12.7 Minimum CLI version

Control plane advertises `min_cli_version`; older CLIs refuse org-mode runs with an upgrade message.

### 12.8 Concurrency

Per-user and per-team concurrent-run caps in `limits.yaml`, checked at preflight and by the control plane.

### 12.9 Budgets hierarchy

Tenant monthly cap (TokenLean) → team monthly cap (TokenLean enhancement + policy) → run budget → step/agent budget → child budget slice. The runner enforces run/step/child budgets from TokenLean cost data; TokenLean enforces tenant/team caps.

---

## 13. Identity and credentials

- v1: admin-issued **TokenLean proxy key** per user (bound to tenant, team, user). Lookup order: environment variable (CI) → OS keychain via `keyring` (MIT).
- Service keys per team for CI/nightly jobs (cannot approve gates).
- Credential provider is pluggable; phase 3 adds **OIDC/SSO** (authlib / msal) and role assignment synced from IdP groups.
- Framework identity sent to TokenLean in **framework-owned headers** (never TokenLean's behaviour headers — see §14.2).

---

## 14. Observability, dashboards and audit

### 14.1 Ownership

| Data | Owner / store |
|---|---|
| Per-call tokens, cost, model, latency | TokenLean `usage_events` (Postgres) + Prometheus |
| Prompts/responses | TokenLean → Langfuse, per-team capture policy (D14) |
| Runs, steps, loops, gates, approvals, children, limit hits, waiting time | Control plane Postgres (from CLI events) |
| Traces | OpenTelemetry (`gen_ai.*` semconv pinned), `traceparent` propagated to TokenLean |
| Code changes | Git commits with trailers (+ signed commits where org requires) |
| Config changes | Policy repo git history + control-plane audit events |

### 14.2 Identity headers on every model call

`X-Agentic-Team`, `X-Agentic-User`, `X-Agentic-Repo`, `X-Agentic-Pipeline`, `X-Agentic-Run-Id`, `X-Agentic-Step`, `X-Agentic-Agent` (name@version), `X-Agentic-Parent-Agent`, `X-Agentic-Tier`, `X-Agentic-Policy-Version`, plus W3C `traceparent`.

**Never** reuse TokenLean's behaviour headers for identity: `X-Step-Name` enables TokenLean's step-level response cache (replays responses if `X-Step-Inputs-Hash` is not unique); `X-Workflow-ID` drives loop control and learned output caps; `X-Session-ID` drives memory. These may be used deliberately, only for the behaviour they trigger.

### 14.3 Metrics

High-cardinality dimensions (run, step, agent) live in **Postgres**, not Prometheus. Prometheus keeps bounded labels (tenant, team, model, pipeline, agent name). Core measures: runs, sessions, turns, tokens (in/out/cache/reasoning), cost, duration, human wait time, loop iterations, gate approve/reject, limit hits, stuck detections, kill-switch stops, QA pass rate, PR merge rate.

### 14.4 Dashboards (Grafana, shipped as JSON)

| Dashboard | Shows |
|---|---|
| Org overview | Cost, runs, active users/repos by team; budget burn vs caps |
| Team view | Team runs, cost by pipeline/agent/user, limit hits |
| Run explorer | Drill-down: run → steps → agents → children → individual TokenLean calls → Langfuse trace |
| Agent quality | Per agent version: gate acceptance/rejection, review loops, QA pass rate, cost per accepted change |
| Limits & safety | Limit hits, stuck detections, kill-switch events, guardrail/PII events (TokenLean) |
| Audit explorer | Who did what and why; filters by actor, team, repo, event type |
| Adoption | Repos initialised, active developers, pipelines used |

The metrics schema starts from Zyroflo's metrics-columns doctrine (`docs/metrics-columns.md`) and its Grafana dashboard / nightly collector, extended with the org dimensions above. TokenLean's existing 10 dashboards remain for proxy-level views. Commercial TokenLean (portal, FinOps) is used when present (D15).

### 14.5 Audit

**Events** (append-only, control plane): run started/finished/killed · step started/finished · gate requested/approved/rejected/edited (with approver and comment) · limit hit · stuck detected · child spawned · kill switch changed · policy version activated/rejected · role assigned/revoked · skill activated · budget raised · CLI blocked (version) · secret scan block.

**Record fields**: `seq`, `tenant`, `team`, `actor` (human or service), `event`, `target`, `before`/`after` (for config), `reason`, `run_id`, `trace_id`, `policy_version`, `ts`, `prev_hash`, `hash`.

**Tamper evidence (v1)**: two chains. (1) Each run's Zyroflo-format ledger (§7.3) is hash-chained and digest-pinned at the source and verified on ingest. (2) The control plane's own audit table (config, RBAC, kill-switch events) is hash-chained per tenant and stored with an insert-only DB role (pattern from TokenLean `audit/enforcement.py`). The CI ledger merge check blocks PRs with broken chains or unapproved gates. **Phase 2**: optional DSSE-signed approvals (Zyroflo supports this) and a WORM copy (S3 Object Lock / GCS retention lock). Commercial TokenLean signed attestation packs as an optional extra.

**Code audit via git trailers** on every agent commit:

```
Agentic-Run: r-2026-10-16-7f3a
Agentic-Step: develop (iteration 2)
Agentic-Agent: developer@1.2.0
Agentic-Triggered-By: asha@example.com
Agentic-Approved-By: ravi@example.com (gate: review-approval)
Agentic-Handoff: .agentic/local/runs/r-2026-10-16-7f3a/design.md
```

The PR body links the run and its handoffs (the "why").

**Config audit**: policy repo history (who, diff, PR reason, approver) + control-plane events for activation and for role changes.

### 14.6 Retention

**Open requirement OR-01 (to be defined later):** retention period and end-of-life action per data type, legal hold, per-user erasure and how deletion is proven. Until defined, nothing is deleted automatically and trace capture stays off by default. Mechanism once defined: per data type in `retention.yaml` (audit longest; traces shortest), applied in control plane and mapped to TokenLean retention settings. Per-user erasure is a later phase (TokenLean GDPR is tenant-level today).

---

## 15. Security and data protection

- **Secret scan before egress**: repo content sent to any model is scanned first (gitleaks or detect-secrets, OSS); hits are blocked or masked per policy and audited. TokenLean PII masking (G29) is the second layer.
- **Untrusted content**: repo files, Jira tickets, web content and tool output are wrapped and labelled as data, never instructions; tool limits and sandbox contain damage; TokenLean injection/context-trust guardrails (G30/G31) as second layer.
- **Supply chain of agent output**: QA/Review run OSV-Scanner (vulnerabilities) and ScanCode (licences) on dependency changes; blocked licences come from `licences.yaml`.
- **Supply chain of the framework**: skills vetted (§11.3); backend tool versions pinned; dependencies pinned with hashes.
- **Corporate network**: honour `HTTPS_PROXY`/`NO_PROXY`, custom CA bundles; air-gapped operation with local models via TokenLean.

---

## 16. Integrations

| Platform | v1 | Later |
|---|---|---|
| GitHub | Open PR with run/handoff links; commit trailers; Actions workflow for nightly index; status checks | Webhook/issue triggers, PR-comment gates, gh-aw as CI target |
| Jira | Read issue as pipeline input (`--from-jira KEY`); post run link + summary comment; optional status transition | Trigger on transition; gate approvals from Jira |
| Email | Gate-waiting and budget notifications | — |
| Slack / Teams | — | Notifications + gate approvals (webhooks phase) |
| IdP (Okta/Entra/Google) | — | OIDC login, group → role sync |
| GitLab, Azure DevOps, Bitbucket, Linear | — | Additional adapters behind the same interfaces |

Ticket/alert intake mapping uses Zyroflo's JSON-path connector format (`connectors/jira.json`, `github-issue.json`, `alert.json`), so a new source is a mapping file rather than code. PR creation and CI watch reuse Zyroflo's `gh`-based flow.

---

## 17. Required TokenLean enhancements

From the code review of TokenLean (2026-10-16; see research notes).

| Pri | Enhancement | Why |
|---|---|---|
| P0 | **Team level under tenant**: key→team binding, `team` column, team spend caps, team config overlays | D-team budgets, per-team routing and reporting |
| P0 | **`tags` JSONB on `usage_events`** from an allow-listed set of `X-Agentic-*` headers | Per-run/step/agent cost attribution and dashboard joins |
| P0 | **Cost on streams** (final SSE event or trailer) and an **actual-cost header** (`x-tokenlean-cost-usd`) | Budget enforcement for streaming calls; today streams carry no cost headers |
| P0 | **OpenAI Responses API endpoint** (`/v1/responses`) | Codex backend routing |
| P0 | **W3C `traceparent` extraction** + `gen_ai.*` span attributes | Join TokenLean spans into framework traces |
| P1 | Langfuse: `finish_trace` on streamed calls (bug); per-team capture policy with admin force; per-team project/ACL | D14 trace policy; streamed usage missing from Langfuse today |
| P1 | **Remaining-budget API** (tenant/team) | Preflight budget check |
| P1 | **Kill-switch deny rules** by tag scope (team/user/run/agent/model) | Defence-in-depth kill switch |
| P1 | Persist matched routing rule on `usage_events` | Audit of admin overrides in effect |
| P2 | Config audit with full diffs + versioning in OSS; human actor on key paths | Complete config audit in OSS edition |
| P2 | OIDC token acceptance | SSO phase |
| P2 | Per-user erasure; per-team prompt retention | Retention/GDPR phase |
| P0 (D27) | **OSS per-user key API** `/admin/keys` with user + team binding over a cloud-agnostic Postgres key store | Provisioning and revocation without the commercial portal |
| P0 (D27) | **OSS routing-rules and team-limits admin endpoint** (validated, versioned, audited) | Control plane applies tier/classification routing |
| P1 (S7) | **Inbound Gemini API route** (`/v1beta/models/*:streamGenerateContent`) | Gemini CLI and Antigravity (`acp:` backends) routed through TokenLean |
| P1 (S7) | **Identity tags from a base-URL path prefix** (`/agentic/<run>/<step>/...`) | Agents that cannot send custom headers (Antigravity) keep per-step attribution |
| P1 (D26) | **LITE profile + `deploy/lite/`** scripts (local compose, cloud Helm) and the LITE dashboard set | One profile for laptops and clusters (§25) |

Until P0 items land, the framework degrades gracefully: it counts tokens from the provider usage chunk on streams, and stores TokenLean's `X-Trace-ID` beside its own span.

---

## 18. OSS reuse map

| Component | Reuse | Licence | Fallback |
|---|---|---|---|
| Agent loop | Pydantic AI v2 (behind `AgentRunner`) | MIT | OpenAI Agents SDK; LangGraph + Deep Agents |
| Model clients | openai / anthropic SDKs + httpx hooks | Apache-2.0 / MIT | — |
| Pipeline engine | Own state machine (Archon/spec-kit semantics) | own | Burr; LangGraph; later DBOS for durability |
| Definitions / handoffs | python-frontmatter, Pydantic, ruamel.yaml, deepmerge, jsonschema | MIT | — |
| Stuck detector | Port of OpenHands `StuckDetector` | MIT | goose RepetitionInspector (Apache-2.0) |
| Claude backend | Claude Agent SDK (Python) | MIT (CLI proprietary) | — |
| Codex backend | `codex exec` | Apache-2.0 | — |
| Other CLI agents | agent-client-protocol Python SDK | Apache-2.0 | — |
| Sandbox | git worktree + Anthropic `srt` / Codex sandbox / Docker | Apache-2.0 | E2B self-hosted |
| Code index | code-review-graph | MIT | codebase-memory-mcp (MIT); Graphify (Apache-2.0) optional |
| Precise refs / tests | multilspy, pytest-testmon | MIT | — |
| Memory | AGENTS.md; later Mem0 | — / Apache-2.0 | — |
| Skills | superpowers (subset), addyosmani/agent-skills, mattpocock/skills, anthropics/skills (Apache subset) | MIT / Apache-2.0 | — |
| Secret scan | gitleaks or detect-secrets | MIT / Apache-2.0 | — |
| Dependency scan | OSV-Scanner, ScanCode | Apache-2.0 | — |
| Policy engine (later) | cedarpy (Cedar) | Apache-2.0 | OPA |
| Observability | OpenTelemetry SDK, Grafana, Langfuse (TokenLean's) | Apache-2.0 / AGPL-3.0 (Grafana, run as a separate service) / MIT (Langfuse core) | OpenLLMetry |
| CLI | Typer or Cyclopts + Rich + questionary | MIT / Apache-2.0 | — |
| Credentials | keyring; later authlib / msal | MIT / BSD-3 | — |
| Control plane | FastAPI + Postgres (TokenLean's stack) | MIT / PostgreSQL | — |
| Lifecycle content, run ledger, CI ledger check, guards, worktree, domain graph, runner contract, connectors, metrics doctrine | **Zyroflo** (`cbeyondcloud/sdlc-harness`) — see §1.4 | MIT (same owner) | — |

**Licence allow-list**: MIT, Apache-2.0, BSD, ISC, PostgreSQL; public-domain-style licences (Unlicense, CC0-1.0) for unmodified third-party dependencies (owner decision 2026-10-20, DF-83; legal to confirm before org-wide rollout).

**Licence red flags (do not use)**: GitNexus (PolyForm NC), Serena (GPL — only as separate process after legal review), CodeQL (OSS-only terms), Daytona (now closed / AGPL), claude-squad / Devon (AGPL), GPT-Pilot (FSL + malware incident), Restate (BSL), Inngest (SSPL), anthropics/skills docx/pdf/pptx/xlsx (source-available), Trail of Bits skills (CC-BY-SA — opt-in after legal review).

---

## 19. Phasing

**r3:** v1 is delivered as three milestones, each ending in a demo and a go/no-go (estimates, team assumption and the tracker are in the master plan):

| Phase | Scope | Exit |
|---|---|---|
| **0 — Spikes** (§21) | Bootstrap, 14 spikes, spec r3 | Spike review, M1 re-estimated |
| **M1 — Standalone core** | Definitions, policy, agent types, handoffs, Zyroflo adapters (ledger, guards, worktree, lifecycle), transport + budget, agent runtime, engine (gates, loops, preflight), native + claude-code backends, sandbox, codeintel, skills, security, CLI, GitHub, built-in agents, eval harness, TokenLean cost on streams + LITE local | Acceptance 1, 2, 4 in standalone mode + **agent quality gate** |
| **M2 — Org governance** | Control plane (policy sync, RBAC, ingest, audit chain, kill switch, pause, auto-trip, provisioning), codex + opencode backends, personal skills and promotion, pre-pass + SCA, Jira, dashboards, TokenLean team level, tags, Responses API, traceparent, key API, routing endpoint | Acceptance 3, kill switch ≤ 30 s, audit verify + export |
| **M3 — Scale and operations** | Helm + environments, day-0 bootstrap, ROI dashboard + cost breakdown (§26), learning loop (§24), retention jobs (after OR-01), heartbeat, release engineering, load test, docs, onboarding kit, pilot runbook | Full §1.3 suite on staging, load test, pilot go/no-go |
| **2 — CI & hardening** | CI/webhook triggers, Slack/Teams gates, DSSE + WORM, `full-lifecycle` pipeline, guardrail learning proposals, ACP backend, multi-repo runs | |
| **3 — UI & enterprise identity** | Web UI, VS Code extension, OIDC/SSO + IdP group sync, remote backends, Mem0, DBOS, Cedar (if needed) | |
| **4 — Regulated** | Signed evidence packs, legal hold, per-user erasure, fixed retention schedules | |

---

## 20. Testing strategy

- **Unit** (TDD): definitions/merge, policy resolution, RBAC, preflight, state machine transitions, budget ledger, limits, stuck detector, handoff validation, expression evaluator.
- **Deterministic pipeline tests**: Pydantic AI `TestModel`/`FunctionModel` and a recorded-response fake TokenLean, so pipelines run without real model calls.
- **Contract tests** per backend (pinned Claude Code / Codex versions), built on Zyroflo's `RUNNER_CONTRACT.md` conformance tiers and `provider_smoke`, and per integration (GitHub, Jira sandboxes).
- **Reused test assets**: Zyroflo's graph fixture estates with answer keys (orderflow, pantry) and its ledger/guard tests are ported with the code they cover.
- **Agent-quality evals**: Zyroflo's corpus replay (harvest real past changes, replay them) and pre-registered judging protocol, with a de-scope register for anything dropped.
- **Integration**: docker-compose with TokenLean + control plane + Postgres + Grafana; verify headers, cost capture, trace joins, audit events, kill switch.
- **Cross-platform**: CI matrix on Windows, macOS, Linux for CLI, sandbox and `init`.
- **Skill A/B evals**: each default skill measured against no-skill baseline on a small task set before promotion.
- **Acceptance**: the four §1.3 scenarios on a sample existing repo (Python + TypeScript) and an empty repo.

---

## 21. Risks and spikes

| ID | Spike / risk | Why |
|---|---|---|
| S1 | TokenLean cost capture via Pydantic AI (stream + non-stream) | No framework exposes response headers; glue must work |
| S2 | Claude Agent SDK end-to-end through TokenLean (managed settings, base URL, headers) | Governability of the Claude backend |
| S3 | Codex `exec` through TokenLean Responses endpoint | Needs TokenLean P0 |
| S4 | Archon / OpenHands quick test behind a proxy | Re-confirm build-vs-adopt decision |
| S5 | Code-graph bake-off (code-review-graph vs codebase-memory-mcp vs Graphify, plus Zyroflo domain-graph at service level) on Python/TS/Java repos, Windows + Linux, run with Zyroflo's corpus replay + judging protocol | Accuracy unknown; no independent benchmark |
| S6 | Windows sandbox strength (`srt` alpha vs Codex sandbox vs WSL2) | Most developers may be on Windows |
| S7 | ACP adapter stability + Antigravity CLI check (ACP support, TokenLean routing, licence) (D32) | Phase 2 dependency; decides whether Antigravity is usable |
| S8 | Pydantic AI deferred-tool serialisation for cross-process gates | Resume design |
| S9 | Committed binary index: size, merge behaviour, LFS need | D22 practicality |
| S10 | Skill A/B baseline | Evidence that default skills help |
| S11 | Serena (GPL) as separate process — legal | Only if multilspy insufficient |
| S12 | Docker Desktop licensing at 1000 seats (CI/laptops) | Cost/legal |
| S13 | Safe expression evaluator (cel-python vs simpleeval) | `when`/`until` conditions — OSS-first |
| S14 | **Done (D34):** Zyroflo used as a pinned dependency behind adapters; gaps Z1–Z4 via Zyroflo PRs (`docs/spikes/S14-zyroflo-reuse.md`) | Avoid two diverging copies |

Key risks: fast churn in Pydantic AI, Graphify and backend CLI flags (mitigation: pin versions, wrap behind interfaces, contract tests); dependency on TokenLean enhancements (mitigation: graceful degradation, §17); skills may add cost without benefit (mitigation: A/B gate); graph accuracy (mitigation: candidates + verification by Review/QA).

---

## 22. Open questions for review

1. Email provider/SMTP for v1 notifications — use an existing org relay?
2. Default repo classification for repos that don't declare one (`internal`?).
3. Initial cap defaults per agent type and per team (needed to seed `agent-types.yaml`).
5. **OR-01** data retention policy (see §14.6) — deliberately left open.
4. Whether nightly index refresh should also run on every merge for high-traffic repos.

---

> Sections 23–27 are copied verbatim from the master plan (`docs/superpowers/plans/2026-10-17-agentic-sdlc-master-plan.md`) at r3 so the spec and plan cannot drift; tracker IDs (M1-xx, M2-xx, M3-xx, ST-n) refer to that plan.

## 23. Users and roles — who does what, when

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

## 24. Developer onboarding

### Prerequisites (checked automatically by `agentic init`)

Python 3.12 (or the bundled installer), git ≥ 2.40, uv or pipx; access to the internal package index; GitHub access to the repo; Node.js ≥ 20.11 and `srt` for sandboxing (macOS: + `ripgrep`; Linux: + `bubblewrap`, `socat`, `ripgrep`); on Windows: WSL2 with those Linux packages, the CLI and repos inside WSL (S6, D38); optional: Claude Code / Codex CLI if the team uses those backends.

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
| **Shared** | Team | policy repo `teams/<team>/skills/` (§4.3) | Team lead via policy-repo PR | Admission checks (licence, validate, size, script scan); A/B eval recommended | That team's repos |
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

## 25. Deployment landscape

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

## 26. Executive cost breakdown

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

---

## 27. Enterprise coverage (1000-person rollout)

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
