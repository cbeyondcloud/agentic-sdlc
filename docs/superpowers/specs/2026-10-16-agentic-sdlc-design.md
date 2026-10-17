# Agentic SDLC Framework — Design Spec

| | |
|---|---|
| Status | Draft for review |
| Date | 2026-10-16 |
| Owner | Sumit Gupta |
| Supporting research | [OSS reuse report](../../../reports/Agentic%20SDLC%20OSS%20reuse.md) · [research notes](../../../research_notes/Agentic%20SDLC%20OSS%20reuse/) · [Zyroflo comparison](../../../research_notes/Agentic%20SDLC%20OSS%20reuse/sdlc_harness_comparison.md) |
| Revision | r2 (2026-10-17): Zyroflo (sdlc-harness) artefacts folded in — §1.4, D16, D25, §6, §7, §9.2, §10, §14.5, §16, §18–§21 |

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
| Learning loop (corrections → gated proposals with undo), `claims.py` | Phase 2 item (§19) |

**Positioning**: Zyroflo stays the zero-dependency drop-in product; this framework is the enterprise platform. Shared artefacts (lifecycle content, ledger format, graph) are extracted into a common package consumed by both (spike S14 decides the packaging).

---

## 2. Decisions log

Decisions agreed during requirements clarification (2026-10-16).

| # | Area | Decision |
|---|---|---|
| D1 | Runtime | Standalone Python runtime with **pluggable backends**: our own agent loop, plus CLI agents (Claude Code, Codex) and later remote agents (Devin etc.) |
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
├── AGENTS.md                         # committed — repo knowledge (human-reviewed)
├── .agentic/
│   ├── config.yaml                   # committed — repo settings, classification, codeintel config
│   ├── agents/                       # committed — repo-level agents
│   ├── pipelines/                    # committed — repo-level pipelines
│   ├── index/                        # committed — dependency graph (nightly bot only, §10.3)
│   └── local/                        # git-ignored — runs/, skills/, index-overlay/, cache/
├── .gitattributes                    # marks index files binary, merge strategy "ours"
└── .github/workflows/agentic-index-nightly.yml
```

### 4.3 Org policy repo `agentic-org-policy` (one per org)

```
agentic-org-policy/
├── policy/
│   ├── tiers.yaml            # tier → model/effort/temperature defaults
│   ├── providers.yaml        # allowed providers/models, per repo classification
│   ├── tools.yaml            # tool catalog, forbidden tools, shell policy
│   ├── limits.yaml           # budgets, child-agent limits, concurrency, per role/team
│   ├── classifications.yaml  # repo sensitivity classes and what each allows
│   ├── retention.yaml        # retention per data type, trace capture policy per team
│   ├── licences.yaml         # blocked licences for dependency scanning
│   └── teams/<team>.yaml     # team-lead narrowing (cannot loosen org policy)
├── agents/                   # org-shared agents (versioned)
├── pipelines/                # org-shared pipelines
├── skills/catalog.yaml       # approved skills, pinned by hash
└── CODEOWNERS                # only org admins may merge policy/
```

---

## 5. Agent definitions

### 5.1 Template

An agent is one Markdown file with YAML front matter (Claude-Code-compatible base fields, extended). The **body is the plain-English job description**.

```markdown
---
name: developer
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
---
You are the Developer. Read the approved design handoff and implement it in
small commits. Write a failing test first, then the code...
```

### 5.2 Field ownership (layering)

| Field group | Who controls | Rule |
|---|---|---|
| `description`, body, `consumes`/`produces`, handoff schema choice | Repo / team / user | Lower layer overrides higher (repo wins for its own agents) |
| `tier`, `model`, `effort`, `temperature` | Requested by definition | Must resolve within org/team policy; admin mapping and overrides win |
| `backend`, `tools`, `shell`, `skills` | Requested by definition | Must be a subset of what policy allows for this repo classification/team |
| `limits`, `children` | Requested by definition | Clamped to policy maxima; team leads may set lower maxima |

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

---

## 6. Pipeline definitions

GitHub-Actions-like YAML, borrowing node semantics from Archon v2 and gate/overlay semantics from spec-kit.

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
- **Overlays**: a repo can overlay an org pipeline (add/replace a step, change a gate's approver) without forking it.
- Gates: **approve**, **reject with feedback** (routes back per pipeline), or **edit the handoff then approve** (opens `$EDITOR`).
- Preflight compiles the pipeline and checks step N `produces` matches step N+1 `consumes`.

### 6.1 Gate model (from Zyroflo)

- **Gate regimes** per pipeline or repo: `solo` (starter may approve), `team` (approver role, optional four-eyes), `regulated` (four-eyes mandatory, signed approvals, required evidence).
- **Org-required phases and gates**: org policy can declare that certain steps/gates (e.g. review, security pre-pass, QA) cannot be removed by repo overlays; preflight rejects pipelines that drop them.
- **Gate re-anchoring**: when an overlay removes a step, its gate re-anchors to the nearest remaining step rather than disappearing.
- **Diff-size budget per review batch**: the Developer splits work so each gated diff stays under a configurable size, keeping human review readable.
- **Digest pinning**: an approval records the digest of the exact handoff and diff approved; any later change invalidates the approval (§7.3).

### 6.2 Library pipelines

- `feature-delivery` — BA → Design → (Developer ↔ Code Review loop) → QA → PR (the v1 acceptance pipeline).
- `full-lifecycle` — Zyroflo P0–P11: intake → requirements → scoping → design → implementation → test → review → triage → release → deploy → verify → closeout, with gates G1–G5; profiles select which stages run.
- `bugfix` — intake (with baseline tests) → Developer ↔ Review → QA → PR.

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

---

## 8. Run lifecycle

### 8.1 Initialisation

**`agentic init --repo`** (repo owner, once; commits files):
creates `.agentic/config.yaml`, `AGENTS.md` (from a scan of the repo), codeintel config, `.gitattributes`, the nightly index workflow, and the first index build.

**`agentic init`** (each developer, once per repo; nothing committed):
1. Check prerequisites: Python, git (worktree support), sandbox availability (Linux/macOS: `srt`; Windows: Codex sandbox or WSL2), installed backend CLIs, network/proxy/TLS settings.
2. Authenticate: validate proxy key with TokenLean; register with the control plane (org mode); check minimum CLI version.
3. Fetch org policy; cache it locally with expiry.
4. Install the approved skills for this developer's roles into `.agentic/local/skills/` (pinned by hash).
5. Pull the latest committed index; set up the local git-ignored overlay.
6. Print a readiness report (what passed, what to fix).

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
- Admin-controlled limits (policy, per role, team-narrowable): `max_total` children per step, `max_concurrent`, `max_depth`, per-child `budget_usd` / tokens, `max_turns`, `max_minutes`; plus per-step and per-run limits.
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
| `acp:<agent>` (phase 2) | `agent-client-protocol` Python SDK (Apache-2.0) for Gemini CLI, OpenCode, Goose, Copilot CLI, … | Per-agent config profile |
| `remote:devin` etc. (phase 3) | submit / poll / collect-PR adapter | Not routable via TokenLean; policy can only allow/deny |

Each backend has **contract tests** pinned to a tool version; flags change fast (research finding).

### 9.2 Sandbox

- **Isolation**: one git worktree per code-editing step; the step's diff is the reviewable artefact; rollback = discard worktree.
- **OS enforcement**: Linux/macOS → Anthropic `sandbox-runtime` (`srt`, Apache-2.0); Windows → Codex native sandbox, or WSL2; CI → Docker/Podman containers; optional self-hosted E2B pool later.
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

### 10.3 Shared index committed to `main`

- Path `.agentic/index/`; **only the nightly bot commits it** (developers' incremental updates go to the git-ignored overlay `.agentic/local/index-overlay/`).
- Format and freshness follow Zyroflo's snapshot design: per-component **shards** stamped with sha256 and parser version, `--check-fresh` to detect stale shards and `--incremental` rebuilds, so nightly commits only touch changed shards. Zyroflo's `snapshot-publish.yml` is the starting point for the nightly workflow (schedule changed from weekly to nightly).
- `.gitattributes`: index files `binary`, `merge=ours`; bot commits use `[skip ci]`.
- **Nightly refresh**: GitHub Actions scheduled workflow (service account) rebuilds from latest `main` and commits; optional on-merge refresh per repo.
- Git LFS optional per repo if the index grows large.

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

Sources: obra/superpowers (selected skills only), addyosmani/agent-skills, mattpocock/skills, anthropics/skills (Apache-2.0 subset only), spec-kit templates.

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

Roles are named permission sets defined in the control plane; admins can edit or add roles. Assignments: `user → role [→ team scope]`; a user can hold several roles.

**Permissions** (initial set): `run.trigger`, `run.cancel.own`, `run.cancel.any`, `gate.approve`, `agent.edit.repo`, `agent.publish.team`, `pipeline.edit.repo`, `pipeline.publish.team`, `policy.edit.org`, `policy.edit.team`, `budget.raise.team`, `killswitch.team`, `killswitch.global`, `rbac.assign`, `dashboard.view.own|team|org`, `audit.read.team|org`, `trace.view.team|org`, `skills.approve`.

**Default roles**

| Role | Scope | Key permissions |
|---|---|---|
| Org admin | Org | everything incl. `policy.edit.org`, `killswitch.global`, `rbac.assign`, `skills.approve` |
| Team lead | Team | `policy.edit.team` (narrow only), `agent/pipeline.publish.team`, `budget.raise.team`, `killswitch.team`, `gate.approve`, `dashboard.view.team`, `audit.read.team` |
| Developer | Own repos/runs | `run.trigger`, `run.cancel.own`, `agent/pipeline.edit.repo`, `gate.approve` (where gate allows starter), `dashboard.view.own` |
| Approver | Assigned | `gate.approve`, `dashboard.view.team` |
| Auditor | Org or team, read-only | `audit.read.*`, `dashboard.view.*`, `trace.view.*` |
| Service account | Team | `run.trigger` only (cannot approve gates) |

**Gate approvals**: each gate names the required role; by default the run starter may approve if they hold it; `four_eyes: true` requires a different person.

### 12.4 Repo classification

Each repo has a classification label (e.g. `public`, `internal`, `confidential`, `restricted`) in `.agentic/config.yaml`, validated against `classifications.yaml`. A class restricts allowed tiers/models/providers (e.g. `restricted` → local models only), backends (e.g. no remote agents), trace capture, and skills.

### 12.5 Versioning and rollout

Agents, pipelines and skills are semver-versioned in the policy repo. A new version can be rolled out team by team (`rollout: [team-a, team-b]`) and a pipeline can pin agent versions. Deprecated versions warn, then block after a date.

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

Per data type in `retention.yaml` (audit longest; traces shortest), applied in control plane and mapped to TokenLean retention settings. Per-user erasure is a later phase (TokenLean GDPR is tenant-level today).

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

**Licence red flags (do not use)**: GitNexus (PolyForm NC), Serena (GPL — only as separate process after legal review), CodeQL (OSS-only terms), Daytona (now closed / AGPL), claude-squad / Devon (AGPL), GPT-Pilot (FSL + malware incident), Restate (BSL), Inngest (SSPL), anthropics/skills docx/pdf/pptx/xlsx (source-available), Trail of Bits skills (CC-BY-SA — opt-in after legal review).

---

## 19. Phasing

| Phase | Scope |
|---|---|
| **0 — Spikes** (§21) | Prove the risky integrations before committing code structure |
| **1 — v1** | Core; CLI; standalone + org modes; Zyroflo shared package extracted (S14); hash-chained run ledger + CI ledger merge check; minimal control plane (policy sync, RBAC via CLI/API, event ingest with chain verification, hash-chained audit table, kill switch, email notify, Grafana dashboards); backends `native` + `claude-code` (+ `codex` if TokenLean Responses endpoint ready); worktree sandbox (`srt`, Codex sandbox on Windows); codeintel (symbol adapter + Zyroflo domain-graph adapter) + nightly index workflow; skills catalog + default skills; built-in agents (Zyroflo SOP-based) + `feature-delivery` and `bugfix` pipelines; GitHub + Jira adapters; `init` / `init --repo`; TokenLean P0 enhancements. **Exit = §1.3 acceptance test** |
| **2 — CI & hardening** | CI/webhook triggers (GitHub Actions, Jira transitions), service accounts at scale, Slack/Teams gates, DSSE-signed approvals + WORM copy, `full-lifecycle` pipeline (release/deploy/verify/closeout agents), Zyroflo learning loop (corrections → gated proposals with undo) and `claims.py` checks, ACP backend, policy-merge author verification, per-team trace policy, TokenLean P1 |
| **3 — UI & enterprise identity** | Web UI (runs, handoffs, gates, admin for RBAC), VS Code extension (thin TS client over control-plane API), OIDC/SSO + IdP group sync, remote backends (Devin), Mem0 memory, DBOS durable execution, Cedar policy (if needed) |
| **4 — Regulated** | Signed evidence packs, legal hold, per-user erasure, fixed retention schedules |

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
| S7 | ACP adapter stability | Phase 2 dependency |
| S8 | Pydantic AI deferred-tool serialisation for cross-process gates | Resume design |
| S9 | Committed binary index: size, merge behaviour, LFS need | D22 practicality |
| S10 | Skill A/B baseline | Evidence that default skills help |
| S11 | Serena (GPL) as separate process — legal | Only if multilspy insufficient |
| S12 | Docker Desktop licensing at 1000 seats (CI/laptops) | Cost/legal |
| S13 | Safe expression evaluator (cel-python vs simpleeval) | `when`/`until` conditions — OSS-first |
| S14 | Zyroflo reuse packaging: extract ledger, guards, worktree, graph and SOP content into a shared package used by both products (keeping Zyroflo stdlib-only) vs vendoring copies | Avoid two diverging copies; `policy.py` (4k lines) and large modules need careful slicing |

Key risks: fast churn in Pydantic AI, Graphify and backend CLI flags (mitigation: pin versions, wrap behind interfaces, contract tests); dependency on TokenLean enhancements (mitigation: graceful degradation, §17); skills may add cost without benefit (mitigation: A/B gate); graph accuracy (mitigation: candidates + verification by Review/QA).

---

## 22. Open questions for review

1. Email provider/SMTP for v1 notifications — use an existing org relay?
2. Default repo classification for repos that don't declare one (`internal`?).
3. Initial budget defaults per role and per team (needed to seed `limits.yaml`).
4. Whether nightly index refresh should also run on every merge for high-traffic repos.
