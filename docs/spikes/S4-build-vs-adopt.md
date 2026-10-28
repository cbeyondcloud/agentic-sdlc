# S4 — Archon / OpenHands behind a proxy: re-confirm build-vs-adopt

**Date:** 2026-10-20 · **Method:** desk research from official sources (repos, release notes, docs, build workflows, PyPI and GitHub API metadata), all accessed 2026-10-20. No hands-on run: see "Not verified". **Code:** none.

## Question

Spec D1 builds a standalone Python runtime with pluggable backends. D17 runs one core in two modes, standalone and org with a small control plane. The OSS reuse report (2026-10-16) found no project covering about 70% of the brief and recommended copying Archon's node vocabulary instead of adopting it. Four days and roughly 36 completed M1 rows later, should anything be adopted wholesale, or a component reused, instead of building? The test is whether every model call can be routed through TokenLean, plus governance, Windows and macOS, licence, and UX.

## What agentic-sdlc already has and still needs

- **Built (TRACKER Done, M1-01 to M1-36, plus review rows):**
  - Definitions and handoffs: agent and pipeline definitions, layered org > team/user > repo policy with locked fields and deny lists, agent types with caps (D35), handoff envelope and templates with inheritance (D37).
  - Zyroflo adapters: hash-chained ledger, guards, worktrees.
  - Model calls: an httpx2 transport with `X-Agentic-*` and `traceparent`, TokenLean cost capture, and a run → step → child budget ledger. The corporate proxy and CA are honoured.
  - Agent runtime: a Pydantic AI runner, the agent tools, a child spawner, and a stuck detector (a port of OpenHands).
  - Engine: state machine with resume, gate regimes with digest-pinned approvals, the `when`/`until` evaluator, loops, preflight and timeouts.
  - Backends `native` and `claude-code`, with a capability matrix.
  - Sandbox and code intelligence: a shell-policy compiler, the `srt` sandbox (WSL2 on Windows, D38), and codeintel with a local index (D39).
- **Still to build:**
  - M1: skills, secret scan, OTel, sinks, the CLI and gate UX, GitHub, the built-in agents, and evals.
  - M2: the control plane (policy sync, RBAC, ingest, kill switch) and the `codex` and `opencode` backends.
  - Phase 2: ACP backends.

## Candidates: current facts (2026-10-20)

| | OpenHands Agent Canvas | OpenHands software-agent-sdk | Archon | Microsoft Agent Governance Toolkit |
|---|---|---|---|---|
| Repo | [OpenHands/OpenHands](https://github.com/OpenHands/OpenHands) | [OpenHands/software-agent-sdk](https://github.com/OpenHands/software-agent-sdk) | [coleam00/Archon](https://github.com/coleam00/Archon) (the spec's "Archon v2"; the Python v1 is archived on a branch) | [microsoft/agent-governance-toolkit](https://github.com/microsoft/agent-governance-toolkit) |
| Licence | MIT. The repo root no longer has an `enterprise/` directory. Enterprise is a separate commercial product ([enterprise vs OSS](https://docs.openhands.dev/enterprise/enterprise-vs-oss.md)) | MIT | MIT | MIT |
| Language | TypeScript (Node 24) plus the Python agent server | Python ≥ 3.12 | TypeScript/Bun, shipped as compiled binaries | Python, plus TS, .NET, Rust and Go SDKs |
| Latest | v1.28.1, 2026-10-19 | v1.56.0, 2026-10-19 (PyPI `openhands-sdk`) | v0.12.1, 2026-10-17 | v4.1.0, 2026-06-09, "Public Preview … May have breaking changes before GA" |
| Pace | release-please, multiple per week | about one minor release a day | a security fix release (see below) | preview |

## How the main candidates compare on our requirements

● strong · ◐ partial · ○ none. The OpenHands column describes the **OSS** edition.

| Requirement | agentic-sdlc (built / planned) | OpenHands (SDK + Canvas, OSS) | Archon v0.12.1 |
|---|---|---|---|
| Python core | ● | ● SDK. Canvas UI is TS | ○ TS/Bun |
| Every call goes through TokenLean with **per-call** identity headers and TokenLean cost harvested | ● Done: httpx2 transport (M1-11/12), D36 | ◐ `LLM.base_url` plus `extra_headers` ("Optional HTTP headers to forward to LiteLLM requests", [llm.py](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/llm/llm.py)). Headers are fixed per LLM instance. Calls go through **LiteLLM** (`litellm>=1.93,<1.106`), which is a second router. Cost comes from LiteLLM, not TokenLean headers, and our transport, budget ledger and CA handling are bypassed | ◐ Model calls go through the harnesses (Claude Code, Codex, Pi). A custom OpenAI-compatible endpoint works only through Pi's `models.json`. OpenCode `baseUrl` is "not supported" ([docs](https://archon.diy/llms-small.txt)) |
| Provider-agnostic | ● | ● (any LiteLLM model) | ◐ (harness-bound) |
| Org > team > repo policy with locked fields | ● Done (M1-03/04) | ○ OSS. Org settings, "Enforce default LLMs" and RBAC are Cloud/Enterprise only | ○ global + repo + user only |
| Agent types with caps, per-agent tool allow-lists | ● Done (M1-75) | ◐ Per-agent `tools` and `max_iteration_per_run` in file agents, but no admin-locked caps | ◐ `allowed_tools`/`denied_tools`, enforced only for some providers |
| Steps, gates, bounded loops, branches | ● Done (M1-21–24) | ○ No declarative pipeline. Automations are trigger-based | ● `wait` approval, `loop`, `when`, attention queue |
| Digest-pinned approvals plus a hash-chained ledger | ● Done (Zyroflo, M1-06/22) | ○ Event log only | ○ Run metadata and logs only |
| Budgets enforced | ● Done (M1-13), TokenLean backstop | ○ OSS. "LLM gateway & budgeting" is Enterprise-only. The SDK tracks `accumulated_cost` but enforces no cap ([metrics](https://docs.openhands.dev/sdk/guides/metrics.md)) | ○ `maxBudgetUsd` is "tracked … not enforced" for Pi/OpenCode |
| Human approval on actions | ● gates plus prompt rules | ◐ `AlwaysConfirm`/`ConfirmRisky`. But headless CLI "always runs in `always-approve` mode", and `execute_tool()` "bypasses the analyzer and confirmation policy" ([security](https://docs.openhands.dev/sdk/guides/security.md), [headless](https://docs.openhands.dev/openhands/usage/cli/headless.md)) | ● approval node |
| Kill switch, central audit, RBAC (org mode, D17) | ◐ Planned: M2-01–06, M2-19 | ○ OSS. "Centralized audit logs", RBAC (Keycloak) and multi-user are commercial | ○ |
| Sandbox on Windows and Mac without Docker Desktop | ● `srt`: Seatbelt on macOS, WSL2 on Windows (D38) | ○ Docker is "recommended". The process sandbox is "(unsafe, but fast)", "No container isolation" ([sandboxes](https://docs.openhands.dev/openhands/usage/sandboxes/overview.md)). Per-conversation containers on Windows need WSL 2 plus Docker Desktop, and "the native Windows host is not supported by this runtime" ([README.windows](https://github.com/OpenHands/OpenHands/blob/main/README.windows.md)) | ◐ Worktrees are "not an execution or security sandbox". `--container` needs Docker |
| No unsigned binaries on Windows | ● pip/uv wheels only | ○ Desktop: "The installer is not code-signed (no signing certs exist for any platform)". macOS is "only ad-hoc signed" ([desktop-windows.yml](https://github.com/OpenHands/OpenHands/blob/main/.github/workflows/desktop-windows.yml), [desktop-macos.yml](https://github.com/OpenHands/OpenHands/blob/main/.github/workflows/desktop-macos.yml)). The SDK and CLI are Python, so this applies only to the desktop app | ○ About 90 MB Bun-compiled `archon-*.exe` per component, installed by `irm … \| iex`. The release workflow has no signing step, only `checksums.txt` ([release.yml](https://github.com/coleam00/Archon/blob/main/.github/workflows/release.yml)) |
| External CLI agents as backends | ● native and claude-code. codex/opencode in M2, ACP in phase 2 | ● Canvas runs Claude Code, Codex, Gemini, OpenCode and "any ACP-compatible agent" | ● Claude, Codex, Pi. OpenCode and Copilot are "deprecated community" providers |
| Skills (agentskills.io) | ◐ Planned M1-37–40 | ● | ● |
| Code intelligence | ● Done (M1-34–36) | ○ | ○ |
| Telemetry default | none | opt-in (OTel/Laminar env vars) | **on** (PostHog), opt-out |
| Security and churn signals | — | about one minor release a day | v0.12.0 binaries loaded the target repo's `.env`, so "a project's `ANTHROPIC_API_KEY` could reach Claude", fixed in v0.12.1 ([release notes](https://github.com/coleam00/Archon/releases/tag/v0.12.1)) |

**Docker Desktop at 1,000 seats.** It is free only for "fewer than 250 employees AND less than $10 million in annual revenue" ([Docker docs](https://docs.docker.com/subscription/desktop-license/)). The list prices are Team $15 and Business $24 per user per month on annual billing ([pricing](https://www.docker.com/pricing/)). For 1,000 seats that is about **$180k–$288k a year**. An OpenHands-based laptop story would pay this, or would have to be validated on Podman Desktop (Apache-2.0), which was not checked. S12 still owns the general question.

## Verdict per candidate

| Candidate | Verdict | Reasons |
|---|---|---|
| **OpenHands, whole product** (Agent Canvas + SDK + agent server) | **Not viable to adopt** | The governance we need is the part held back for the commercial Enterprise edition: budgets, RBAC, multi-user, audit and admin-enforced LLMs. The OSS loop routes through LiteLLM, which would bypass our TokenLean transport and cost ledger. Safe isolation means Docker, which on Windows means WSL2 plus Docker Desktop at paid seats. The desktop app is unsigned. There is no pipeline, gate or ledger model. |
| **OpenHands SDK as the agent loop** (replacing Pydantic AI behind `AgentRunner`) | **Not now** | Technically possible: Python and MIT, with `base_url` and `extra_headers`. It would add LiteLLM, plain `httpx`, fastmcp, fakeredis and lmnr to the core, and duplicate cost tracking outside the transport. Pydantic AI is already integrated (M1-15) and passed S1/S8. Revisit only if the native loop fails the M1 agent-quality gate (M1-64). |
| **OpenHands as a backend** (`openhands acp`, or `--headless --json` with `LLM_BASE_URL` and `--override-with-envs`, [CLI reference](https://docs.openhands.dev/openhands/usage/cli/command-reference.md)) | **Reuse as an optional backend (phase 2)** | Fits the existing `acp:<agent>` row with no new design. It can be routed to TokenLean by `LLM_BASE_URL`, wrapped in `srt`, and governed through ACP permission callbacks. Use ACP mode, not headless, because headless always auto-approves. |
| **OpenHands components** | **Reused already / borrow ideas** | `StuckDetector` is already ported (M1-18). The file-agent front matter is already borrowed. Two ideas are worth a later look: the deterministic `PatternSecurityAnalyzer` / `PolicyRailSecurityAnalyzer` rule sets for the native shell tool, and PreToolUse hooks with "exit 2 blocks", which match Claude Code. The `openhands-workspace` Docker/K8s workspaces are **not** reused: they host OpenHands' own agent server, not a generic sandbox. |
| **Archon** | **Not viable to adopt or fork. Semantics already copied** | Its YAML node vocabulary is already in §6. It is TS/Bun and ships unsigned compiled binaries. Calls are mediated by harnesses, with custom endpoints only via Pi. There is no org layer, budgets are unenforced, and telemetry is on by default. It churns: providers were dropped and a key-leak bug shipped and was fixed within one release cycle. One **UX idea is worth copying**: the attention queue (`archon workflow attention`), one list of every gate waiting for the user across runs. |
| **Microsoft Agent Governance Toolkit** (new since the report) | **Not now. Watch for the phase 3 policy engine** | MIT, with YAML/OPA/Cedar policies, `require_approval`, tamper-evident audit and a kill switch. But it is "Public Preview" with breaking changes before GA, enforces "at the application middleware layer, not at the OS kernel level", has no budget model and lists no Pydantic AI adapter. It overlaps work already done: the Zyroflo ledger, preflight and guards. It is a candidate behind `PolicyDecider` when Cedar is considered (§19 phase 3). |
| Others in the report (spec-kit v1.2.2, MAF python-1.22.0, crewAI, ChatDev 2.0, Open SWE) | **No change** | Their dispositions in the report still hold: borrow semantics, or keep as a fallback runtime. Nothing material changed. |

## Components worth reusing (net new from S4)

1. **`acp:openhands` backend** in phase 2. Add it to the S7 ACP matrix.
2. **Attention-queue UX** from Archon: `agentic runs --waiting`, or a "waiting for you" section in `status`, listing gates across runs that this user's role can approve (M1-54/M1-55).
3. **Deterministic risk rules** from OpenHands `PatternSecurityAnalyzer`/`PolicyRailSecurityAnalyzer` (MIT) as an input to the shell-policy deny defaults. Idea and rule content only, with attribution.

## Decision on D1 / D17

**Both confirmed, with no change to their substance.**
- **D1:** no candidate gives a Python runtime where every call passes through TokenLean under our transport, budget ledger and policy. The pluggable-backend design is also the right way to *use* OpenHands: as one more backend.
- **D17:** the org-mode features (multi-user, RBAC, central audit, budgets, admin-enforced models) are exactly what OpenHands reserves for its commercial edition and what Archon lacks. That leaves the white space found on 2026-10-16 unchanged.

## Proposed spec amendment (small, for the coordinator to apply)

1. §2, D1 row, append: "Re-confirmed by spike S4 (2026-10-20): no OSS project is adopted wholesale; OpenHands is usable only as an optional `acp:` backend (`docs/spikes/S4-build-vs-adopt.md`)."
2. §9.1, `acp:<agent>` row, Reuse column: change "for Gemini CLI, Goose, Copilot CLI, …" to "for Gemini CLI, Goose, Copilot CLI, OpenHands (`openhands acp`, routed by `LLM_BASE_URL` with `--override-with-envs`; never `--headless`, which always auto-approves), …".
3. §18, licence red-flags row "Mastra, LiteLLM, OpenHands, Langfuse": add "OpenHands' governance features (budgets, RBAC, audit, multi-user) are in the commercial Enterprise edition, not the MIT repo (S4)".

## Not verified

- **No hands-on run behind TokenLean.** The tracker's "quick test" was not done. The verdict rests on structural disqualifiers (licensing of governance features, Docker on Windows, unsigned binaries, TS runtime, no org layer, LiteLLM routing), which a run would not change. The end-to-end routing check belongs with the `acp:openhands` work in S7.
- **Signing.** Archon's signing status is inferred from its release workflow, not from the binary: the download was not permitted in this environment. OpenHands' signing status is quoted from its workflow comments.
- **Untested items:**
  - Podman Desktop compatibility for OpenHands.
  - Whether `extra_headers` survives LiteLLM on streaming and Responses calls.
  - Whether `openhands-sdk`'s plain `httpx` dependency would conflict with our httpx2 stack (D36).
- Docker prices are list prices. Enterprise discounts and the eligibility FAQ were not checked (S12).

## Cost if wrong

If OpenHands turned out to be the better agent loop, it would be swapped in behind `AgentRunner` (one module plus contract tests), because the engine, policy, ledger and gates do not depend on Pydantic AI. Adding it as an ACP backend is additive. If Archon's UX were wanted later, it is a UI reference, not a dependency. The irreversible mistake would have been adopting either one, which would have moved budgets, policy and audit outside the framework. This record avoids that.

## Correction (spike S7, 2026-10-20)

S7 tested `openhands acp` hands-on and withdrew the optional `acp:openhands` backend recommended above: the OpenHands CLI README says it is no longer actively maintained (last release 1.16.0, 2026-05-08), it runs on Windows only under WSL, it pins an ACP SDK below 0.9, and its ACP mode ignores `--override-with-envs` and needs `~/.openhands/agent_settings.json`, which stores the API key in plain text. See `docs/spikes/S7-acp-and-antigravity.md`. The build-not-adopt verdict on D1/D17 stands.
