# End-to-end "agentic SDLC" / multi-agent software-team frameworks: adopt, fork, borrow or ignore

Research date: 2026-10-16. Unless another source is given, star counts, licence SPDX IDs, primary language, last-push dates and archived flags were pulled from the GitHub REST API (`gh api repos/<owner>/<repo>` and `/releases/latest`) on 2026-10-16. Each figure is cited to the repo URL. A licence that GitHub reports as "NOASSERTION" was checked by reading the LICENSE file itself.

Requirements we scored against ("REQ"):
1. Plain-English agent template (instructions, model or tier, effort, temperature, tool allow-list, I/O handoff schemas).
2. Pipeline with linear steps, human gates, loops and conditional branches; child agents instead of pipeline-level parallelism.
3. Markdown handoffs with schema-validated YAML front matter.
4. Works on any existing or new git repo.
5. Provider-agnostic through a custom base_url proxy.
6. Layered definitions (org > team/user > repo) with admin override of model, provider, tools and spend.
7. Sandboxed git worktrees.
8. CLI first, then CI/webhooks, web UI and VS Code.
9. Scales to an org of about 1000 developers.

---

## Q1. Which OSS projects already implement an SDLC agent team or role-based pipeline, and what are their basic facts (licence, language, stars, maintenance)?

### Takeaway
The field has split into four groups:
- **First-generation "virtual software company" research systems.** MetaGPT, ChatDev 1.0, GPT-Pilot, Devika and Devon are now stale, pivoted or compromised.
- **SWE-bench-style single-agent issue fixers.** SWE-agent, mini-swe-agent and Agentless solve one issue at a time and have no role pipeline.
- **Prompt-and-artifact "methodologies" that run inside an existing coding harness.** BMAD, spec-kit, OpenSpec, GSD, Superpowers and subagent collections are very active and very popular, but they do not orchestrate pipelines themselves (spec-kit's new workflow engine is a partial exception).
- **A newer 2026 wave of workflow engines and orchestrators for coding agents.** Archon v2, GitHub Agentic Workflows (gh-aw), OpenHands Agent Canvas/SDK, Open SWE, Orchestrator.inc, ChatDev 2.0 and goose recipes.

Archon v2 is structurally the closest match to our pipeline requirements. It is TypeScript/Bun, not Python.

### Cited Findings

**Master comparison table (facts as of 2026-10-16)**

| Project | Licence (exact) | Copyleft / non-OSI flag | Lang | Stars | Last push / latest release | Health |
|---|---|---|---|---|---|---|
| MetaGPT (FoundationAgents/MetaGPT) | MIT | – | Python | 70.9k | push 2026-01-21; release v0.8.1 2024-04-22 | Stale (few commits since mid-2025) |
| ChatDev (OpenBMB/ChatDev), now "ChatDev 2.0 / DevAll" | Apache-2.0 | – | Python (+Vue UI) | 34.5k | push 2026-10-16; v2.2.0 2026-03-23 | Active, but pivoted to general-purpose |
| BMAD-METHOD | MIT + trademark notice | Trademark restricts the name, not the code | Python/Markdown (npm installer) | 54.2k | push 2026-10-16; v6.12.4 2026-10-14; v7 alpha | Very active |
| GPT-Pilot (Pythagora) | **FSL-1.1-MIT (Functional Source License)** | **Non-OSI** (source-available; converts to MIT later) | Python | 33.7k | push 2026-06-18 (security clean-up only) | **Abandoned; malware incident** |
| GitHub spec-kit | MIT | – | Python | 141.5k | push 2026-10-16; v1.2.0 2026-10-13 | Very active |
| OpenHands (OpenHands/OpenHands, now "Agent Canvas") | MIT | – | TypeScript (frontend) + Python SDK | 91.1k | push 2026-10-16; v1.27.1 2026-10-12 | Very active |
| OpenHands software-agent-sdk | MIT | – | Python | 1.3k | push 2026-10-16 | Very active |
| SWE-agent | MIT | – | Python | 20.6k | push 2026-10-12; v1.1.0 2025-05-22 | Superseded by mini-swe-agent |
| mini-swe-agent | MIT | – | Python | 8.6k | push 2026-10-12 | Active |
| Agentless | MIT | – | Python | 2.1k | push 2024-12-22 | Stale (research artefact) |
| Devika (stitionai) | MIT | – | Python | 19.6k | push 2025-09-25 | Stale / experimental |
| Devon (entropy-research) | **AGPL-3.0** | **Copyleft** | Python | 3.4k | push 2025-05-26 | Stale |
| crewAI | MIT | – | Python | 59.8k | push 2026-10-16; 1.15.28 2026-10-14 | Very active |
| crewAI-examples | No licence reported | – | Jupyter | 6.2k | **archived** | Archived |
| AutoGen (microsoft) | Code MIT (LICENSE-CODE); docs CC-BY-4.0 (GitHub API reports CC-BY-4.0) | – | Python | 61.4k | push 2026-04-15; python-v0.7.5 2025-09-30 | **Maintenance mode** |
| Microsoft Agent Framework (AutoGen successor) | MIT | – | Python/.NET | 14.2k | push 2026-10-16 | Very active |
| AG2 (ag2ai/ag2) | Apache-2.0 | – | Python | 5.0k | push 2026-10-15; v1.2.0 2026-10-11 | Active |
| goose (aaif-goose/goose, formerly block/goose) | Apache-2.0 | – | Rust | 55.3k | push 2026-10-16; v1.54.0 2026-10-08 | Very active (Linux Foundation AAIF) |
| Aider | Apache-2.0 | – | Python | 49.6k | push 2026-05-22; v0.86.0 2025-08-09 | Slowing |
| wshobson/agents | MIT | – | Markdown/Python | 40.9k | push 2026-10-15 | Very active |
| VoltAgent/awesome-claude-code-subagents | MIT | – | Shell/Markdown | 25.8k | push 2026-10-11 | Active |
| Archon v2 (coleam00/Archon) | MIT | – | TypeScript (Bun) | 23.7k | push 2026-10-16; v0.11.1 2026-09-25 | Very active |
| GitHub Agentic Workflows (github/gh-aw) | MIT | – | Go | 5.4k | push 2026-10-16 | Very active |
| Open SWE (langchain-ai/open-swe) | MIT (but see Agent Server licence-key note) | Production deploy needs a LangGraph Agent Server **licence key** | Python | 11.0k | push 2026-10-16 | Active, "breaking changes", no external contributions |
| Orchestrator.inc (OrchestratorInc/agent-orchestrator, formerly ComposioHQ) | Apache-2.0 | – | Go | 13.2k | push 2026-10-16 | Very active |
| OpenSpec (Fission-AI) | MIT | – | TypeScript | 72.1k | push 2026-10-16; v1.14.2 2026-10-15 | Very active |
| Superpowers (obra) | MIT | – | Shell/Markdown | 300.5k | push 2026-10-10 | Very active |
| Get Shit Done (gsd-build) | MIT | – | JavaScript | 64.3k | push 2026-05-31 | Slowing (no push in about 4.5 months) |
| Spec Kitty | MIT | – | Python | 1.7k | push 2026-10-16 | Active |
| Ruflo (formerly claude-flow, ruvnet) | MIT | – | TypeScript | 75.5k | push 2026-10-16 | Very active |
| SuperClaude Framework | MIT | – | Python | 23.9k | push 2026-09-27 | Active |
| claude-task-master | **MIT + Commons Clause** | **Non-OSI** (no "Sell" right) | JavaScript | 28.2k | push 2026-04-28 | Slowing |
| HumanLayer | Apache-2.0 (GitHub reports NOASSERTION) | – | TypeScript | 11.7k | push 2026-10-15 | Active |
| claude-squad | **AGPL-3.0** | **Copyleft** | Go | 8.6k | push 2026-08-20 | Active |
| container-use (dagger) | Apache-2.0 | – | Go | 4.1k | push 2026-09-21 | Active |
| vibe-kanban (BloopAI) | Apache-2.0 | – | Rust | 28.4k | push 2026-09-19 | Active |
| PraisonAI | MIT | – | Python | 9.2k | push 2026-10-16 | Active |
| deepagents (langchain) | MIT | – | Python | 30.3k | push 2026-10-16 | Very active |
| Small 2026 "agentic-SDLC" repos: smart-sdlc, agentic-os, oliveira-sp/agentic-sdlc, nexus-sdlc, ai-team-sdlc | MIT / Apache-2.0 | – | various | 0–8 each | 2026 | Toy-scale; ignore except for ideas |

Sources for table rows (GitHub API, 2026-10-16):
- [MetaGPT](https://github.com/FoundationAgents/MetaGPT) · [ChatDev](https://github.com/OpenBMB/ChatDev) · [BMAD-METHOD](https://github.com/bmad-code-org/BMAD-METHOD) · [GPT-Pilot](https://github.com/Pythagora-io/gpt-pilot) · [spec-kit](https://github.com/github/spec-kit)
- [OpenHands](https://github.com/OpenHands/OpenHands) · [software-agent-sdk](https://github.com/OpenHands/software-agent-sdk) · [SWE-agent](https://github.com/SWE-agent/SWE-agent) · [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) · [Agentless](https://github.com/OpenAutoCoder/Agentless)
- [Devika](https://github.com/stitionai/devika) · [Devon](https://github.com/entropy-research/Devon) · [crewAI](https://github.com/crewAIInc/crewAI) · [crewAI-examples](https://github.com/crewAIInc/crewAI-examples) · [AutoGen](https://github.com/microsoft/autogen) · [Agent Framework](https://github.com/microsoft/agent-framework) · [AG2](https://github.com/ag2ai/ag2)
- [goose](https://github.com/aaif-goose/goose) · [Aider](https://github.com/Aider-AI/aider) · [wshobson/agents](https://github.com/wshobson/agents) · [VoltAgent subagents](https://github.com/VoltAgent/awesome-claude-code-subagents) · [Archon](https://github.com/coleam00/Archon) · [gh-aw](https://github.com/github/gh-aw)
- [Open SWE](https://github.com/langchain-ai/open-swe) · [Orchestrator.inc](https://github.com/OrchestratorInc/agent-orchestrator) · [OpenSpec](https://github.com/Fission-AI/OpenSpec) · [Superpowers](https://github.com/obra/superpowers) · [GSD](https://github.com/gsd-build/get-shit-done) · [Spec Kitty](https://github.com/spec-kitty/spec-kitty)
- [Ruflo](https://github.com/ruvnet/ruflo) · [SuperClaude](https://github.com/SuperClaude-Org/SuperClaude_Framework) · [task-master](https://github.com/eyaltoledano/claude-task-master) · [HumanLayer](https://github.com/humanlayer/humanlayer) · [claude-squad](https://github.com/smtg-ai/claude-squad) · [container-use](https://github.com/dagger/container-use)
- [vibe-kanban](https://github.com/BloopAI/vibe-kanban) · [PraisonAI](https://github.com/MervinPraison/PraisonAI) · [deepagents](https://github.com/langchain-ai/deepagents)
- [smart-sdlc](https://github.com/supermldev/smart-sdlc) · [agentic-os](https://github.com/kinnister/agentic-os) · [oliveira-sp/agentic-sdlc](https://github.com/oliveira-sp/agentic-sdlc) · [nexus-sdlc](https://github.com/zer0dayf/nexus-sdlc) · [ai-team-sdlc](https://github.com/CalinL/ai-team-sdlc)

**Licence verification details**
- GPT-Pilot's LICENSE is "Functional Source License, Version 1.1, MIT Future License (FSL-1.1-MIT)". FSL is not OSI-approved. — [GPT-Pilot LICENSE](https://github.com/Pythagora-io/gpt-pilot/blob/main/LICENSE)
- claude-task-master is "MIT License" plus a "Commons Clause" License Condition v1.0 that removes "the right to Sell the Software", including hosting and consulting fees. This makes it non-OSI. — [task-master LICENSE](https://github.com/eyaltoledano/claude-task-master/blob/main/LICENSE)
- BMAD's LICENSE is MIT plus a trademark notice: "BMad™, BMad Method™, and BMad Core™ are trademarks of BMad Code, LLC ... covering all casings and variations (including BMAD, bmad, BMadMethod, BMAD-METHOD...)". A fork would have to be renamed. — [BMAD LICENSE](https://github.com/bmad-code-org/BMAD-METHOD/blob/main/LICENSE)
- HumanLayer's LICENSE text is Apache License 2.0, even though GitHub reports NOASSERTION. — [HumanLayer LICENSE](https://github.com/humanlayer/humanlayer/blob/main/LICENSE)
- AutoGen has `LICENSE` (CC-BY-4.0, for docs) and `LICENSE-CODE` (MIT). — [AutoGen repo](https://github.com/microsoft/autogen)
- Copyleft projects: Devon (AGPL-3.0) and claude-squad (AGPL-3.0). — [Devon](https://github.com/entropy-research/Devon), [claude-squad](https://github.com/smtg-ai/claude-squad)
- Open SWE's README says "Production standalone Agent Server deployments require a license key". The MIT code depends on a commercially licensed LangGraph server for production. — [Open SWE README](https://github.com/langchain-ai/open-swe)

**Maintenance and pivot facts**
- GPT-Pilot's README carries a CAUTION banner: "Malicious code was found in this repository ... A supply-chain worm (a credential stealer) was hidden in `core/telemetry/` from August 2025 until 11 June 2026". The same README says "This repository is no longer actively maintained". — [GPT-Pilot README](https://github.com/Pythagora-io/gpt-pilot)
- AutoGen README: "AutoGen is now in maintenance mode. It will not receive new features ... New users should start with Microsoft Agent Framework". — [AutoGen README](https://github.com/microsoft/autogen)
- SWE-agent README: "mini-swe-agent ... has superseded SWE-agent ... Our general recommendation is to use mini-SWE-agent instead". — [SWE-agent README](https://github.com/SWE-agent/SWE-agent)
- MetaGPT's last five commits run from 2025-06-30 to 2026-01-21 and are minor (Windows terminal support, a README update). Its most recent README news item is from March 2025 and promotes the commercial MGX product. — [MetaGPT commits](https://github.com/FoundationAgents/MetaGPT/commits/main), [MetaGPT README](https://github.com/FoundationAgents/MetaGPT)
- ChatDev 2.0 (DevAll) was released 2026-01-07 as "a Zero-Code Multi-Agent Platform for 'Developing Everything'". The classic software-company ChatDev moved to the `chatdev1.0` branch. — [ChatDev README](https://github.com/OpenBMB/ChatDev)
- Devika's README calls itself "very early development/experimental stage ... a lot of unimplemented/broken features" and points to a successor, "Opcode". — [Devika README](https://github.com/stitionai/devika)
- crewAI-examples is archived on GitHub. — [crewAI-examples](https://github.com/crewAIInc/crewAI-examples)
- Archon v2 is a full rewrite: "Looking for the original Python-based Archon (task management + RAG)? It's fully preserved on the `archive/v1-task-management-rag` branch". — [Archon README](https://github.com/coleam00/Archon)
- goose moved from block/goose to aaif-goose/goose. Its README says "goose is part of the Agentic AI Foundation (AAIF) at the Linux Foundation". — [goose README](https://github.com/aaif-goose/goose)
- Roo-Code is archived (2026-05) and microsoft/vscode-copilot-chat is archived. Neither can be adopted. — [Roo-Code](https://github.com/RooCodeInc/Roo-Code), [vscode-copilot-chat](https://github.com/microsoft/vscode-copilot-chat)

**Academic comparison**
- An arXiv paper from June 2026, "From Prompt to Process: a Process Taxonomy and Comparative Assessment of Frameworks Supporting AI Software Development Agents" (S. O. de Macedo), compares GitHub Spec Kit, OpenSpec, BMAD Method, GSD, Spec Kitty and Reversa on six dimensions: specification, context, roles, execution, validation and portability.
  - Finding: no framework strongly covers all six dimensions, and there is "a structural trade-off between process depth and portability across agents".
  - Finding: the frameworks converge on "persistent artifacts, work contracts, traceability and human review".
  - Risks named: spec/code drift, over-trust in generated artefacts, fragile community extensions, platform dependence, and the lack of whole-process benchmarks.
  - Source: [arXiv 2606.04967](https://arxiv.org/abs/2606.04967)

### Inferences
- Of the original "classic" SDLC agent-team projects named in the brief, none is a viable base:
  - MetaGPT is effectively dormant.
  - ChatDev 1.0 is legacy.
  - GPT-Pilot is abandoned, non-OSI and was compromised.
  - Devika and Devon are stale, and Devon is AGPL.
- The real 2026 activity is in workflow engines on top of coding harnesses (Archon, gh-aw, spec-kit workflows, OpenHands, Orchestrator.inc) and in methodology packs (BMAD, spec-kit, OpenSpec, Superpowers).
- Star counts for prompt-pack repos (e.g. Superpowers at 300k) say little about fit for an org platform.

### Gaps
- I did not verify the per-framework scores in the arXiv paper because only the abstract was read.
- Download and usage figures (PyPI/npm) were not collected.
- The licences of the small 2026 SDLC repos come from GitHub metadata only. Their contents were not reviewed.

---

## Q2. Per-project capability evaluation and verdicts (declarative agents, pipeline model, HITL, base_url, existing repos, sandboxing, governance)

### Takeaway
On pipeline mechanics, only a handful of projects implement our model: declarative steps, human gates, loops and conditionals.
- **Archon v2:** YAML DAG with prompt, bash, loop, approval and `when:` nodes, per-node model/effort/tools, and worktree isolation.
- **spec-kit's workflow engine:** gate, if/switch, while/do-while and overlays.
- **ChatDev 2.0:** YAML DAG with `human` nodes and conditional edges, but no git/repo focus.
- **Microsoft Agent Framework and crewAI Flows:** code-first graphs with human-in-the-loop.

On agent definitions, the best plain-English formats are OpenHands file-based sub-agents, BMAD's SKILL.md personas with layered TOML customisation, and goose recipes. None of the projects offers org-level admin override of model, provider, tools and spend.

### Cited Findings

#### Archon v2 (coleam00/Archon): closest structural match
- It calls itself "a workflow engine for AI coding agents. Define your development processes as YAML workflows - planning, implementation, validation, code review, PR creation". — [Archon README](https://github.com/coleam00/Archon)
- Pipeline features:
  - Node types: `command:`, `prompt:`, `bash:`, `loop:`, `approval:`, `cancel:`.
  - Branching through `when:` and `trigger_rule`.
  - `output_format` is "enforced" (grammar-constrained) for Claude, Codex and OpenCode, and "best-effort" for Pi and Copilot (up to 3 re-asks).
  - Source: [Archon docs llms-small.txt](https://archon.diy/llms-small.txt)
- The README example shows the patterns we need: a `loop` with `until: ALL_TASKS_COMPLETE` and `fresh_context: true`, and a `loop_group` with `max_iterations: 5` wrapping a `wait:` approval form, so a reviewer rejection loops back to revise. — [Archon README](https://github.com/coleam00/Archon)
- Per-node settings: `model:`, `provider:`, `effort:` (from `minimal` up to `ultra`/`persistent`), `allowed_tools`/`denied_tools`, inline `agents:`, skills and `systemPrompt:`. — [Archon docs](https://archon.diy/llms-small.txt)
- Model tiers `small`/`medium`/`large` and aliases (e.g. `@fast`), set with commands like `archon ai tier set large claude opus`. — [Archon docs](https://archon.diy/llms-small.txt)
- Config layering is install-wide `~/.archon/config.yaml` plus per-repo `.archon/config.yaml`. Same-named repo workflows override bundled ones. Per-user credentials and "Just me" preferences take precedence. The docs describe **no org/admin policy layer**. — [Archon docs](https://archon.diy/llms-small.txt)
- Spend: `maxBudgetUsd` is "tracked but not enforced for Pi or OpenCode", and no org-level budgets are documented. — [Archon docs](https://archon.diy/llms-small.txt)
- Providers are agent harnesses, not raw model APIs: Claude Code, Codex, OpenCode, GitHub Copilot and Pi. A custom OpenAI-compatible endpoint is documented only through Pi's `~/.pi/agent/models.json`, and an OpenCode external `baseUrl` is "not supported". — [Archon docs](https://archon.diy/llms-small.txt)
- Sandboxing: git worktrees by default, plus an optional `--container` / `container.enabled` overlay-isolated Docker container. — [Archon docs](https://archon.diy/llms-small.txt)
- Interfaces: CLI, Web UI (runs, run detail with "governance actions", settings with model tiers, an experimental visual workflow builder), Slack, Telegram, Discord and GitHub webhooks. Storage is SQLite or PostgreSQL with users, identities and per-user provider keys. — [Archon README](https://github.com/coleam00/Archon)
- It ships an `sdlc` workflow pack: `archon-ship`, `archon-triage`, `archon-investigate`, `archon-plan`, `archon-implement`, `archon-pr`, `archon-deliver`, `archon-review` ("parallel specialist lenses"), `archon-validate` and `archon-upkeep`. — [Archon README](https://github.com/coleam00/Archon)
- Telemetry (PostHog) is on by default and opt-out (`ARCHON_TELEMETRY_DISABLED=1`, `DO_NOT_TRACK=1`). It is auto-disabled in CI. — [Archon README](https://github.com/coleam00/Archon)
- The legacy `archon-*` workflows were removed between versions ("no longer ship"), which signals churn. — [Archon README](https://github.com/coleam00/Archon)
- **Verdict: borrow heavily; possibly fork.** The YAML node model is almost exactly our pipeline spec. Blockers: TypeScript/Bun rather than Python, harness-mediated providers instead of a direct base_url proxy, no org policy layer, no enforced spend caps, and fast-moving breaking changes.

#### GitHub spec-kit: best artefact chain, plus a new workflow engine
- The SDD flow is "Constitution once per project; specify → plan → tasks → implement → converge per feature". It runs as `/speckit-*` skills inside the coding agent. — [spec-kit README](https://github.com/github/spec-kit)
- Workflows are YAML with step types `command`, `prompt`, `shell`, `init`, `slot`, `gate` (human approval with `on_reject: abort|retry`), `if`/`switch`, `while`/`do-while` and `fan-out`/`fan-in`. Runs pause and resume with `specify workflow resume <run_id>`. — [spec-kit workflows reference](https://github.github.io/spec-kit/reference/workflows.html)
- Workflow overlays (`extends`, `priority`, `insert_after`/`replace`/`remove`) modify an installed workflow without forking it. Catalog resolution order: env var > project `.specify/workflow-catalogs.yml` > user `~/.specify/...` > built-in. `shell` steps "run with your privileges and has no sandbox". — [spec-kit workflows reference](https://github.github.io/spec-kit/reference/workflows.html)
- Capabilities table: "Enforce organizational or regulatory standards" through Presets, "Host a private catalog of approved components", "Work offline or behind a firewall", "Inspect which layer supplies a command, template, or script". It also ships bug-fix (assess → fix → test) and idea-assessment extensions. — [spec-kit README](https://github.com/github/spec-kit)
- Existing repos are supported (it has an "existing-project guide"). It is agent-agnostic through "integrations" (Copilot default). — [spec-kit README](https://github.com/github/spec-kit)
- **Verdict: borrow the artefact chain and the overlay/preset/catalog layering ideas.** It is MIT, Python and hugely popular. It does not call models itself (it relies on the host coding agent), so it has no base_url, sandbox or spend control of its own.

#### BMAD-METHOD: richest role personas and a team/user customisation layer
- Agents ship as SKILL.md files: `bmad-agent-analyst`, `bmad-agent-architect`, `bmad-agent-dev`, `bmad-agent-pm` and `bmad-agent-ux-designer` (in the v6.12 tree). — [BMAD V6.12 tree](https://github.com/bmad-code-org/BMAD-METHOD/tree/V6.12/src/bmm-skills/agents)
- Customisation layering: each architect persona ("Winston") resolves its `agent` block from `{skill-root}/customize.toml` (defaults) → `_bmad/custom/{skill-name}.toml` (team overrides) → `_bmad/custom/{skill-name}.user.toml` (personal overrides).
  - Merge rules: "Scalars override, tables deep-merge, arrays of tables keyed by `code` or `id` replace matching entries ... all other arrays append".
  - Customisable fields include `role`, `identity`, `communication_style`, `principles`, `persistent_facts` and `activation_steps_prepend`.
  - Source: [BMAD architect SKILL.md](https://github.com/bmad-code-org/BMAD-METHOD/blob/V6.12/src/bmm-skills/agents/bmad-agent-architect/SKILL.md)
- Positioning: "Right-sized process ... New or existing code ... Durable context". Modules include Test Architect (enterprise testing) and "BMad Loop" ("Builds, verifies, and retros a whole epic unattended"). It installs into Claude Code or Codex as plugins/skills. — [BMAD README](https://github.com/bmad-code-org/BMAD-METHOD)
- Stability: "The stable release is v6.12 ... frozen aside from bug fixes. V7 is in preview alpha ... Expect breaking changes". — [BMAD README](https://github.com/bmad-code-org/BMAD-METHOD)
- **Verdict: borrow the persona content and the base → team → user TOML merge semantics.** Do not adopt it as the runtime: it is a prompt methodology executed by a host agent, there is no programmatic pipeline engine, the trademark means a fork must be renamed, and v7 is churning.

#### OpenHands (Agent Canvas + software-agent-sdk)
- OpenHands has pivoted into "Agent Canvas ... self-hosted developer control center for coding agents and automations". It can run OpenHands, Claude Code, Codex, Gemini, Pi, OpenCode "or any ACP-compatible agent". It supports "Bring your own model ... Use with any LLM" and automations on a schedule or webhooks (Slack, GitHub, Linear). — [OpenHands README](https://github.com/OpenHands/OpenHands)
- Sandboxing: "Option 3: With Multiple Docker Sandboxes" runs each conversation in its own container. The README warns that "conversations using the same host workspace still share those files; choose separate directories or worktrees". — [OpenHands README](https://github.com/OpenHands/OpenHands)
- The Python SDK owns "agents, tools, conversations, workspaces, events, the REST/WebSocket API". Workspaces can be local or ephemeral Docker/Kubernetes. — [software-agent-sdk README](https://github.com/OpenHands/software-agent-sdk)
- File-based sub-agents are Markdown with YAML front matter. Keys: `name`, `description`, `tools`, `model` (default `"inherit"` or a named LLM profile), `skills`, `max_iteration_per_run`, `mcp_servers`, `hooks`, `permission_mode` (`always_confirm` / `never_confirm` / `confirm_risky`).
  - Precedence: programmatic > plugin > project `.agents/agents/` > user `~/.agents/agents/`.
  - Delegation goes through `TaskToolSet` with `subagent_type`.
  - Source: [OpenHands file-based agents](https://docs.openhands.dev/sdk/guides/agent-file-based)
- The SDK paper (MLSys 2026) cites a "security analyzer for agent actions, flexible lifecycle control (pause/resume, sub-agent delegation, history restore)". — [MLSys 2026 abstract](https://proceedings.mlsys.org/paper_files/paper/2026/hash/8ae9cf363ea625161f885b798c1f1f78-Abstract-Conference.html)
- **Verdict: strong candidate as the agent runtime underneath our pipeline layer (borrow or depend).** It is Python, MIT, has Docker/K8s workspaces, a sub-agent file format very close to ours, and permission modes. It does not provide a declarative role pipeline with gates or loops (Automations are trigger-based), and it has no org > team > repo admin override layer.

#### ChatDev 2.0 (DevAll)
- YAML DAG workflows with node types `model`, `python`, `agent`, `human`, `subgraph`, `passthrough`, `literal`, `loop_counter`, `loop_timer`. Edges carry conditions (e.g. the `keyword` condition "keeps looping unless the reviewer types `ACCEPT`"). Provider and model are set per node. — [ChatDev workflow authoring](https://github.com/OpenBMB/ChatDev/blob/main/docs/user_guide/en/workflow_authoring.md)
- `API_KEY` and `BASE_URL` are configured in `.env`. It has a Vue web console with a visual canvas, a Python SDK (`run_workflow`), `make validate-yamls` schema validation and Docker Compose. The bundled workflows are general-purpose (data viz, 3D, deep research, game dev, `ChatDev_v1.yaml`). — [ChatDev README](https://github.com/OpenBMB/ChatDev)
- Incremental development on existing code (`--config "incremental" --path`) and Git mode exist from the v1 era (2023). — [ChatDev README](https://github.com/OpenBMB/ChatDev)
- **Verdict: borrow ideas.** The YAML DAG schema, human nodes, keyword-conditioned loop edges and loop guard nodes are all useful. It is not git-repo/PR-centric, has no worktree sandboxing or governance, and is aimed at "developing everything", not enterprise SDLC.

#### MetaGPT
- "Code = SOP(Team)". It takes a one-line requirement and produces PRDs, designs and code through PM, Architect, Project Manager and Engineer roles. Configuration is `~/.metagpt/config2.yaml` with `api_type`, `model` and `base_url`. Python is limited to ">= 3.9 but < 3.12". — [MetaGPT README](https://github.com/FoundationAgents/MetaGPT)
- **Verdict: ignore as a base; cite for prior art.** It has the right roles and supports base_url, but it is greenfield-oriented ("create a repo in ./workspace"), has roles defined in Python code, is dormant, and has no last release since 2024.

#### GPT-Pilot / Pythagora
- See the malware banner, the "no longer maintained" statement and the FSL licence above. — [GPT-Pilot README](https://github.com/Pythagora-io/gpt-pilot)
- **Verdict: ignore. It is a security liability and non-OSI.**

#### SWE-agent / mini-swe-agent / Agentless
- SWE-agent is "Governed by a single `yaml` file" and lets "your language model of choice" fix GitHub issues. Its focus is research and benchmarks. — [SWE-agent README](https://github.com/SWE-agent/SWE-agent)
- Agentless uses a fixed three-phase process (localisation → repair → patch validation) with no agents. Its last update was December 2024. — [Agentless README](https://github.com/OpenAutoCoder/Agentless)
- **Verdict: ignore as a framework.** mini-swe-agent could serve as a minimal Developer-agent executor, and the Agentless localise/repair/validate pattern could inform a "bug-fix" pipeline template.

#### Devika / Devon
- Devika supports Claude, GPT-4, Gemini, Mistral, Groq and Ollama, and is self-described as experimental. — [Devika README](https://github.com/stitionai/devika)
- Devon is an AGPL-3.0 "pair programmer" whose last push was May 2025. — [Devon](https://github.com/entropy-research/Devon)
- **Verdict: ignore. Both are stale, and Devon is copyleft.**

#### crewAI
- Agents and tasks can be declared in `config/agents.yaml` and `config/tasks.yaml`, with a newer JSON-first `crew.jsonc` and `agents/*.jsonc` layout. — [crewAI Flows docs](https://docs.crewai.com/en/concepts/flows)
- Flows provide `@start`, `@listen` and `@router` (conditional routing), `or_`/`and_` triggers, `@human_feedback` (crewAI ≥ 1.8.0, with LLM-mapped outcomes that pick the next listener), Pydantic state and `@persist` (SQLite) with resume and fork. — [crewAI Flows docs](https://docs.crewai.com/en/concepts/flows)
- The official examples repo is archived. — [crewAI-examples](https://github.com/crewAIInc/crewAI-examples)
- **Verdict: a candidate library, not an SDLC product.** It could implement our pipeline engine in Python (routers give branches and loops; human feedback gives gates), but it has no git/worktree, sandbox or governance features. Community "SDLC crew" templates are mostly demos.

#### AutoGen / AG2 / Microsoft Agent Framework
- AutoGen is in maintenance mode (see above). — [AutoGen README](https://github.com/microsoft/autogen)
- Microsoft Agent Framework offers "graph-based workflows supporting sequential, concurrent, handoff, and group collaboration patterns; includes checkpointing, streaming, human-in-the-loop, and time-travel", "Declarative Agents: Define agents using YAML", "Multiple Agent Provider Support", and a DevUI. It is Python and .NET, MIT. — [Agent Framework README](https://github.com/microsoft/agent-framework)
- AG2 is the community AutoGen fork (Apache-2.0, v1.2.0, October 2026), with sample apps in build-with-ag2. — [AG2](https://github.com/ag2ai/ag2), [build-with-ag2](https://github.com/ag2ai/build-with-ag2)
- **Verdict:** MAF is a viable Python orchestration substrate (declarative YAML agents, graph workflows with HITL and checkpoints), but it is not SDLC-specific and carries an Azure bias. Ignore AutoGen for new work.

#### goose (AAIF / Linux Foundation)
- It works with "15+ providers — Anthropic, OpenAI, Google, Ollama, OpenRouter, Azure, Bedrock, and more" and offers 70+ MCP extensions, a desktop app, a CLI and an API. "Custom Distributions" ship "preconfigured providers, extensions, and branding". — [goose README](https://github.com/aaif-goose/goose)
- Recipe fields (YAML):
  - Core: `instructions`, `prompt`, `parameters`, `extensions` (with `available_tools` allow-lists), `settings` (`goose_provider`, `goose_model`, `temperature`, `max_turns`).
  - Output and retries: `response.json_schema` (validated structured output), `retry` (`checks` shell commands, `max_retries`, `on_failure`).
  - Composition: `sub_recipes`, with subagent delegation through the `summon` extension.
  - The recipe reference documents no approval gates.
  - Source: [goose recipe reference](https://goose-docs.ai/docs/guides/recipes/recipe-reference)
- **Verdict: borrow the recipe schema.** It maps almost one-to-one onto our agent template (instructions, provider/model, temperature, tool allow-list, output JSON schema, retry checks). The custom-distro idea fits org-level defaults. It is Rust and has no pipeline gates or loops across recipes.

#### Aider-based workflows
- Aider: Apache-2.0, last release v0.86.0 on 2025-08-09, last push 2026-05-22. — [Aider](https://github.com/Aider-AI/aider)
- **Verdict: ignore as a framework.** Its development is slowing, and it is a single-agent pair programmer with no role pipeline (it is listed as a worker harness inside Orchestrator.inc).

#### Claude Code / Codex sub-agent collections (wshobson/agents, VoltAgent, SuperClaude, Ruflo)
- wshobson/agents has "94 plugins ... 202 agents, 185 skills, and 105 commands" from "a Markdown source", adapted for Claude Code, Codex CLI, Cursor, OpenCode, Antigravity, Copilot and Pi, with a capability matrix for "model mappings, tool permissions". — [wshobson/agents README](https://github.com/wshobson/agents)
- VoltAgent offers "161+ Claude Code subagents across 10 categories", installed to `~/.claude/agents/` or `.claude/agents/`. — [VoltAgent README](https://github.com/VoltAgent/awesome-claude-code-subagents)
- Ruflo (formerly claude-flow) is an "agent meta-harness for Claude Code and Codex" with "100+ specialized agents, coordinated swarms, self-learning memory ... enterprise security guardrails". Its marketing-heavy README mentions 314 MCP tools. — [Ruflo README](https://github.com/ruvnet/ruflo)
- **Verdict: borrow content (role prompts for reviewer, QA, security and so on) as seed templates.** These are prompt libraries bound to specific harnesses, not orchestration frameworks.

#### Superpowers / OpenSpec / GSD / Spec Kitty (methodology packs)
- Superpowers runs brainstorm → spec sign-off → implementation plan → "subagent-driven-development" with review, and enforces TDD. It is installed per harness (Claude Code, Codex, Cursor, Gemini CLI, Copilot CLI and others). — [Superpowers README](https://github.com/obra/superpowers)
- OpenSpec: `/opsx:propose` creates `openspec/changes/<id>/` with `proposal.md`, `specs/`, `design.md` and `tasks.md`; `/opsx:apply` implements; `/opsx:archive` merges spec deltas. Specs use "ADDED Requirements" with SHALL statements and Scenarios. It is "built for brownfield not just greenfield". — [OpenSpec README](https://github.com/Fission-AI/OpenSpec)
- **Verdict: borrow formats.** OpenSpec's change-folder plus spec-delta model fits handoffs on existing repos well. These are not runtimes.

#### GitHub Agentic Workflows (gh-aw): CI/webhook phase
- Workflows are defined "in Markdown with YAML frontmatter" and compiled by `gh aw compile` into `.lock.yml` GitHub Actions. Engines: Copilot, Claude Code, Codex, Gemini, Pi. "Agent jobs are read-only and sandboxed by default", and writes go through validated "safe-outputs" jobs with scoped permissions. — [gh-aw README](https://github.com/github/gh-aw)
- A security advisory retired releases `>= 0.83.3, < 0.85.4`. — [gh-aw README](https://github.com/github/gh-aw)
- **Verdict: borrow the Markdown + YAML front-matter agent format and the safe-outputs pattern; consider it the CI integration target.** It is GitHub-only and is not a multi-step role pipeline.

#### Open SWE (LangChain)
- Billed as "An open-source software factory": investigate → implement in an isolated sandbox → validate → PR, then review, CI babysitting and learning review preferences. It is customisable for "models, reasoning effort, instructions, skills, integrations, and sandbox providers" and triggers from the dashboard, GitHub, Slack or Linear. Human review happens in Slack. MCP connections layer "instance-wide, workspace-specific, and personal tools". — [Open SWE README](https://github.com/langchain-ai/open-swe)
- Caveats: "not accepting issues or external contributions"; "correctness, stability, and compatibility are not guaranteed"; LangSmith is the default sandbox and tracing provider; production needs an Agent Server licence key. — [Open SWE README](https://github.com/langchain-ai/open-swe)
- **Verdict: borrow ideas (layered MCP scoping, Slack approval, CI babysit).** Avoid adopting it because of the vendor coupling and the no-contributions policy.

#### Orchestrator.inc (agent-orchestrator)
- A desktop app plus local daemon. Each worker gets "its own branch and worktree". A project orchestrator agent decomposes plans and spawns workers. A Kanban derives state from PR, CI and review facts. It supports 30+ harnesses (Claude Code, Codex, Aider, Goose, OpenHands, Cline and others). Telemetry is opt-out. — [Orchestrator.inc README](https://github.com/OrchestratorInc/agent-orchestrator)
- **Verdict: borrow UX ideas (Kanban state derived from PR/CI).** It is a supervision UI for parallel workers, not a declarative role pipeline. It is Go, desktop-first, with no org governance.

#### Small 2026 "agentic SDLC" repos
- smart-sdlc defines eight personas (Scout, Product/BA, Architect, Developer, Team Lead, QA, Release, Modernization Lead) as `.agent.md` files for the Copilot agent picker. ai-team-sdlc is a Copilot plugin with "quality gates and one human sign-off". agentic-os is a "governed, portable engineering platform" with a governance layer and SDLC pipeline. Each has 0–8 stars. — [smart-sdlc](https://github.com/supermldev/smart-sdlc), [ai-team-sdlc](https://github.com/CalinL/ai-team-sdlc), [agentic-os](https://github.com/kinnister/agentic-os)
- **Verdict: ignore (immature).** agentic-os's "governance layer" concept is worth a quick read.

### Inferences

**Requirement coverage matrix** (my judgement from the cited findings; ● strong, ◐ partial, ○ none):

| REQ | Archon v2 | spec-kit | BMAD | OpenHands SDK/Canvas | ChatDev 2.0 | goose | MAF | crewAI | gh-aw | Open SWE |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 Plain-English agent template w/ model, effort, temp, tools, I/O schema | ● (per-node model, effort, tools, output_format; temperature not seen) | ◐ (commands and templates) | ◐ (personas, no model or tools) | ● (MD+YAML: model, tools, skills, permission_mode) | ◐ (per-node provider and model) | ● (recipe: model, temp, tools, json_schema) | ◐ (YAML agents) | ◐ (YAML agents) | ◐ (MD+YAML engine, tools) | ◐ |
| 2 Linear + gates + loops + branches | ● | ● (gate, if/switch, while) | ○ (conversational) | ○ | ● (human node, conditional edges, loop guards) | ◐ (sub_recipes, retry) | ● (graph, HITL) | ● (router, human_feedback) | ○ | ◐ |
| 3 MD handoffs with schema-validated front matter | ◐ (artifacts; structured output) | ◐ (MD artefacts, no schema) | ◐ (MD artefacts) | ○ | ○ | ◐ (JSON schema output) | ○ | ◐ (Pydantic outputs) | ○ | ○ |
| 4 Existing repos | ● | ● | ● | ● | ◐ | ● | n/a | n/a | ● | ● |
| 5 base_url proxy, provider-agnostic | ◐ (via harness; Pi custom endpoints) | n/a (host agent) | n/a | ● (any LLM, LLM profiles) | ● (BASE_URL) | ● (15+ providers) | ● | ● | ◐ (engines) | ◐ |
| 6 Org > team > repo layering + admin override | ◐ (global + repo + user; no org policy) | ◐ (presets, overlays, catalogs) | ◐ (base → team → user TOML) | ◐ (project/user precedence) | ○ | ◐ (custom distros) | ○ | ○ | ○ | ◐ (MCP scope layers) |
| 7 Sandboxed worktrees | ● (worktree + optional container) | ○ | ○ | ● (Docker/K8s; worktree advised) | ◐ (Docker) | ○ | ○ | ○ | ● (Actions sandbox) | ● (cloud sandbox) |
| 8 CLI → CI/webhooks → web UI → IDE | ● CLI, web, GitHub/Slack (no VS Code ext) | ◐ CLI and agents | ◐ | ● web, automations, CLI | ◐ web, SDK | ● desktop, CLI, API | ◐ | ◐ | ● CI | ● dashboard, GitHub, Slack |
| 9 1000-dev org (multi-user, governance, spend) | ◐ (Postgres, users; budget not enforced) | ○ | ○ | ◐ (self-host, enterprise tier) | ○ | ○ | ◐ | ○ | ◐ (GitHub perms) | ◐ |
| Language fit (Python) | ✗ TS/Bun | ✓ | ~ | ✓ | ✓ | ✗ Rust | ✓ | ✓ | ✗ Go | ✓ |

- The two strongest "pipeline-engine" matches (Archon, spec-kit workflows) delegate model calls to an external coding harness. That conflicts with our requirement that every model call goes through our proxy with org-enforced model, provider and spend rules. A harness can sometimes be pointed at a proxy via its own env vars (Claude Code supports a custom Anthropic base URL; Codex supports custom model providers), but enforcement would live outside the framework. This is my inference from general knowledge of those harnesses and was not verified in this session.
- Org-level admin override (model, provider, tools, spend) is the clearest **white space**. No candidate offers it.

### Gaps
- I did not confirm whether Archon nodes accept `temperature`, or how its `approval:` node interacts with the CLI in CI.
- I did not verify OpenHands Enterprise governance features (RBAC, budgets) because they are commercial and not in the OSS README.
- MAF's declarative-workflow YAML depth (gates and loops in YAML rather than code) was not checked.
- No source was found on any project's performance at 1000-developer scale.
- Community crewAI/AG2 "software team" templates were not individually evaluated; the official crewAI examples repo is archived.

---

## Q3. Does any project cover most requirements well enough that building new would be wasteful?

### Takeaway
No. No single OSS project covers even about 70% of the requirements. The independent arXiv assessment also found that no framework strongly covers all process dimensions. Archon v2 comes closest on pipeline semantics plus worktrees plus UI, but it is TypeScript, harness-bound, lacks org governance and changes fast. The pragmatic route is **build a thin Python orchestration and governance layer and reuse heavily**: OpenHands SDK or a direct proxy client as the agent runtime, plus artefact and agent formats borrowed from spec-kit, OpenSpec, BMAD, goose and OpenHands.

### Cited Findings
- "No framework strongly covers all six dimensions ... a structural trade-off between process depth and portability across agents." — [arXiv 2606.04967](https://arxiv.org/abs/2606.04967)
- Archon has no org/admin policy layer and no enforced org budgets; base_url support is harness-dependent. — [Archon docs](https://archon.diy/llms-small.txt)
- spec-kit's `shell` steps have "no sandbox". It relies on the host coding agent. — [spec-kit workflows](https://github.github.io/spec-kit/reference/workflows.html)
- OpenHands provides Docker/K8s workspaces, file-based sub-agents with per-agent `model` and `permission_mode`, and any-LLM support, but it is a control center plus SDK, not a role pipeline. — [OpenHands README](https://github.com/OpenHands/OpenHands), [file-based agents](https://docs.openhands.dev/sdk/guides/agent-file-based)
- Open SWE's production path depends on a licence-keyed LangGraph Agent Server and LangSmith defaults. — [Open SWE README](https://github.com/langchain-ai/open-swe)

### Inferences
**Recommended disposition**

| Disposition | Projects |
|---|---|
| **Fork / deep-study candidate** | Archon v2 (only if the team accepts TypeScript; otherwise reimplement its node model in Python) |
| **Depend on as a component** | OpenHands software-agent-sdk (agent runtime, sandboxed workspaces, sub-agent delegation); optionally crewAI Flows or Microsoft Agent Framework as the graph/HITL engine instead of a custom one; gh-aw as the CI execution target; container-use (Apache-2.0) for container sandboxes |
| **Borrow ideas / formats** | spec-kit, OpenSpec, BMAD, goose recipes, ChatDev 2.0 YAML DAG, OpenHands agent MD format, gh-aw safe-outputs, Open SWE layered MCP scope, Orchestrator.inc Kanban, wshobson/VoltAgent role prompts, Superpowers TDD/review discipline |
| **Ignore** | MetaGPT (dormant), ChatDev 1.0 (legacy), GPT-Pilot (malware, FSL), Devika, Devon (AGPL, stale), SWE-agent (superseded), Agentless (stale), AutoGen (maintenance), Aider (slowing, single agent), claude-task-master (Commons Clause), claude-squad (AGPL), Ruflo (hype-heavy, harness-bound), small 2026 SDLC repos |

- Building new is justified mainly by:
  - Python plus a single proxy-enforced model gateway.
  - Org > team > repo policy with admin override of model, provider, tools and spend.
  - Schema-validated Markdown + YAML handoffs.
  - Enterprise multi-tenancy for about 1000 developers.

  None of these exists in the candidates. The pipeline DSL itself does exist (Archon, spec-kit), so we should copy its semantics rather than invent new ones.

### Gaps
- No hands-on test was done. Archon and OpenHands should be prototyped against a sample repo with an OpenAI-compatible proxy to confirm base_url routing and the worktree/sandbox behaviour.

---

## Q4. Which ideas and formats are worth borrowing?

### Takeaway
Borrow these:
- spec-kit's constitution → specify → plan → tasks → implement → converge artefact chain, and its workflow overlay/preset/catalog layering.
- OpenSpec's change-folder with spec deltas (ADDED/MODIFIED requirements with SHALL plus scenarios).
- BMAD's persona SKILL files and its base → team → user TOML deep-merge rules.
- The OpenHands and gh-aw "Markdown body + YAML front matter" agent definition, including `model: inherit`, tool lists and `permission_mode`.
- goose recipe fields (provider, model, temperature, available_tools, `response.json_schema`, `retry.checks`).
- Archon's node vocabulary (prompt, bash, loop with `until` and `fresh_context`, `loop_group` + approval, `when`, `output_format`, model tiers and aliases).
- ChatDev's `human` node, keyword-conditioned loop edges and `loop_counter` guards.
- gh-aw's "safe-outputs" (read-only agent; writes applied by a separate validated job).

### Cited Findings
- spec-kit: "Constitution once per project; specify → plan → tasks → implement → converge per feature". Workflow `gate` with `on_reject: abort|retry`. Overlays with `extends`/`priority`/`insert_after`/`replace`. Presets to "Enforce organizational or regulatory standards". Private catalogs. "Artifact inventory" showing "which layer supplies a command, template, or script". — [spec-kit README](https://github.com/github/spec-kit), [workflows reference](https://github.github.io/spec-kit/reference/workflows.html)
- OpenSpec: `openspec/changes/<id>/{proposal.md, specs/, design.md, tasks.md}`, archived after apply. Requirements use "SHALL" plus "Scenario" sections. — [OpenSpec README](https://github.com/Fission-AI/OpenSpec)
- BMAD: three-file customisation merge (defaults → team → user) with precise merge semantics. Persona fields `role`, `identity`, `communication_style`, `principles`, `persistent_facts`. — [BMAD architect SKILL.md](https://github.com/bmad-code-org/BMAD-METHOD/blob/V6.12/src/bmm-skills/agents/bmad-agent-architect/SKILL.md)
- OpenHands file-based agents: front-matter keys `name`, `description`, `tools`, `model`, `skills`, `max_iteration_per_run`, `mcp_servers`, `hooks`, `permission_mode`. Project > user precedence. — [OpenHands docs](https://docs.openhands.dev/sdk/guides/agent-file-based)
- goose recipes: `settings.goose_provider`/`goose_model`/`temperature`/`max_turns`, `extensions[].available_tools`, `response.json_schema`, `retry.checks`/`on_failure`, `sub_recipes`, and parameters with `requirement: user_prompt`. — [goose recipe reference](https://goose-docs.ai/docs/guides/recipes/recipe-reference)
- Archon: node types, `loop.until`, `fresh_context`, `loop_group.max_iterations`, `wait.form: approval`, `when:`/`trigger_rule`, `output_format` enforcement, model tiers `small`/`medium`/`large`, `@alias`, and workflows committed in `.archon/workflows/` that override bundled defaults. — [Archon README](https://github.com/coleam00/Archon), [Archon docs](https://archon.diy/llms-small.txt)
- ChatDev 2.0: `human` nodes, edge `condition: {type: keyword}`, `loop_counter.max_iterations`, `subgraph`, and `make validate-yamls`. — [ChatDev workflow authoring](https://github.com/OpenBMB/ChatDev/blob/main/docs/user_guide/en/workflow_authoring.md)
- gh-aw: "YAML frontmatter configures triggers, permissions, tools, and the AI engine; the Markdown body tells the AI agent what to accomplish". It compiles to a lock file, and writes go through "safe-outputs" jobs. — [gh-aw README](https://github.com/github/gh-aw)
- Archon's telemetry design (categorical-only events, run cost totals, `DO_NOT_TRACK`) is a usable template for our own usage and spend reporting. — [Archon README](https://github.com/coleam00/Archon)

### Inferences
- Our agent template should probably be a **superset of the OpenHands agent front matter and the goose recipe settings**:
  - `name`, `description`, `model` or `tier`, `effort`, `temperature`, `tools` allow-list, `permission_mode`, `skills`, `mcp_servers`, `max_turns`.
  - `input_schema` and `output_schema` (a JSON Schema for the handoff front matter).
  - A plain-English Markdown body.

  This keeps it close to formats users already know from Claude Code, Codex and Copilot.
- Our pipeline DSL can mirror Archon's node vocabulary, restricted to sequential execution: `prompt|agent`, `bash`, `gate`, `loop` with `until`/`max_iterations`, and `when`. Add spec-kit-style overlays so a repo can modify an org pipeline without forking it.
- For the layering model, combine BMAD's merge semantics (scalars override, tables deep-merge, keyed arrays replace) with spec-kit's catalog precedence. Add a "locked" flag for org-admin fields, which no candidate has.
- Shipping default BA/Architect/Dev/QA personas adapted from BMAD (MIT), with credit and without the BMad trademark, would save content effort.

### Gaps
- I did not inspect the actual front-matter JSON Schemas of spec-kit or OpenSpec artefacts, if any exist, to confirm whether they validate metadata.
- The Microsoft Agent Framework declarative YAML schema was not compared.
