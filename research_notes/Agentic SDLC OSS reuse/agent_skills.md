# Agent Skills and Prompt-Methodology Repos for an Agentic SDLC Framework (as of 16 Oct 2026)

Repo stats (stars, licence SPDX, last push) were pulled live from the GitHub REST API on 2026-10-16 (`api.github.com/repos/<owner>/<repo>`). "None" means GitHub detected no repo-level licence file; "NOASSERTION" means a licence file exists that GitHub could not classify.

## 1. Agent Skills standard (SKILL.md): spec status, client support, progressive disclosure, use from a non-Claude model in our own loop

### Takeaway
Agent Skills is now an open, Anthropic-originated format, stewarded at agentskills.io (spec repo Apache-2.0, docs CC-BY-4.0). It is supported by essentially every major coding agent: Claude Code, Codex, Gemini CLI, Copilot/VS Code, Cursor, OpenCode, Goose, OpenHands, Junie, Kiro, Amp, Roo, Factory, Spring AI and more. A skill is only a folder with a `SKILL.md` (YAML front matter plus markdown). Any tool-calling model in a Pydantic AI loop can use skills: put a name and description catalog (~50-100 tokens per skill) in the system prompt or in a tool description, then load the body on demand through a file-read tool or an `activate_skill` tool.

### Cited Findings
- Spec format: a directory with required `SKILL.md`, plus optional `scripts/`, `references/` and `assets/`. Front matter fields:
  - `name` (required; ≤64 chars, lowercase/digits/hyphens, must match the directory name)
  - `description` (required; ≤1024 chars, says what the skill does and when to use it)
  - `license`, `compatibility` (≤500 chars), `metadata` (string map) — all optional
  - `allowed-tools` (optional; marked **"Experimental. Support for this field may vary between agent implementations"**)

  [agentskills.io/specification](https://agentskills.io/specification)
- The spec has **no version field**. Its own example puts `version: "1.0"` under `metadata`, which is a convention, not a normative field. Validation uses the `skills-ref validate ./my-skill` reference library — [agentskills.io/specification](https://agentskills.io/specification)
- Progressive disclosure has three tiers — [agentskills.io/specification](https://agentskills.io/specification); [agentskills.io/integrate-skills](https://agentskills.io/integrate-skills):
  1. Catalog: name and description, about 50-100 tokens per skill, loaded at session start.
  2. Instructions: the full `SKILL.md` body, recommended under 5,000 tokens and under 500 lines, loaded on activation.
  3. Resources: scripts and references, loaded only when the instructions point to them.
- The integration guide for building your own agent — [agentskills.io/integrate-skills](https://agentskills.io/integrate-skills):
  - **Catalog:** use a structured catalog (XML `<available_skills>` with name, description and location; JSON and bullet lists also work). Place it in a system-prompt section or in the description of a dedicated activation tool.
  - **Activation:** either "file-read activation" (the model reads `SKILL.md` with its normal file tool) or "dedicated tool activation" (`activate_skill`, with `name` constrained to an enum of valid skills to prevent hallucinated names).
  - **Context management:** wrap activated content in `<skill_content name=…>` tags, protect skill content from context compaction, and deduplicate repeat activations.
  - **Filtering:** hide disabled or permission-denied skills entirely rather than blocking them at activation time.
  - **Trust:** gate project-level skills on a trust check, because "untrusted repositories [could] silently inject instructions".
  - **Conventions:** the cross-client path convention is `.agents/skills/` (project and user), and many clients also scan `.claude/skills/`. On a name collision, project-level skills override user-level ones.
  - **Subagents:** running a skill inside a separate subagent is an optional advanced pattern.
- The client showcase lists (among others) Claude Code, Claude, ChatGPT & Codex, Gemini CLI, GitHub Copilot, VS Code, Cursor, OpenCode, OpenHands, Goose, Junie (JetBrains), Kiro, Amp, Roo Code, Factory, Letta, Mux, pi, Hermes Agent, OpenClaw, Spring AI, fast-agent, Tabnine, Qodo, Snowflake Cortex Code, Databricks Genie Code, Pulumi Neo, Mistral Vibe and TRAE — [agentskills.io/clients](https://agentskills.io/clients)
- The spec repo agentskills/agentskills has 26.2k stars, code under Apache-2.0 and docs under CC-BY-4.0. It says the format "was originally developed by Anthropic, released as an open standard… open to contributions from the broader ecosystem". Last push was 2026-08-09 — [GitHub agentskills/agentskills](https://github.com/agentskills/agentskills)
- openai/skills is now **deprecated** in favour of openai/plugins ("Build plugins" guide). Codex docs still point to the Agent Skills open standard, and Codex skills are invoked as `$skill-name` — [openai/skills README](https://github.com/openai/skills)
- Distribution tooling has converged:
  - Vercel's `npx skills` CLI (vercel-labs/skills, MIT, 35.2k stars) and the skills.sh registry. addyosmani/agent-skills says the CLI "installs into 70+ agents" — [vercel-labs/skills](https://github.com/vercel-labs/skills); [addyosmani/agent-skills README](https://github.com/addyosmani/agent-skills)
  - GitHub CLI has `gh skill install <repo> <skill>` — [wshobson/agents README](https://github.com/wshobson/agents)
  - Plugin marketplaces (Claude Code, Codex, Copilot CLI, Cursor, Gemini extensions) package skills together with agents, commands and hooks — [obra/superpowers README](https://github.com/obra/superpowers)

### Inferences
- For our Pydantic AI loop:
  - Implement a `SkillRegistry` that parses the front matter and builds the tier-1 catalog per agent, already filtered by that agent's allow-list.
  - Register an `activate_skill(name: Enum[...])` tool that returns the body wrapped in `<skill_content>` and lists the resources.
  - Expose a sandboxed `read_skill_resource` tool, and `run_skill_script` only where the skill is approved for scripts.
  - None of this is Claude-specific. It needs only reliable tool calling, so it works through an OpenAI- or Anthropic-compatible proxy for GPT, Gemini and local models. Weaker local models may under-trigger skills (an inference, not measured). Mitigation: let templates declare `skills: [..]` that are **pre-activated** (injected eagerly) rather than relying on model-driven activation.
- When delegating to Claude Code, Codex, Copilot CLI or Gemini CLI, the framework can materialise the same approved skill folders into `.agents/skills/` (plus `.claude/skills/` for Claude Code) in the workspace. One catalog then serves both execution modes.
- Claude Code adds non-standard front-matter extensions (such as `disable-model-invocation`, mentioned in the agentskills.io guide's filtering section). Our parser should be lenient, as the guide recommends, and treat unknown fields as metadata.

### Gaps
- I found no formal standards-body governance (W3C/LF-style) or versioned spec releases. The spec repo's last push was August 2026, and there is no semver for the spec itself.
- I did not verify per-client differences in honouring `allowed-tools`. The spec says only that support "may vary".

## 2. Does attaching skills measurably help? (general evidence base)

### Takeaway
Independent benchmarks show that curated skills help on average but help *software engineering* least. SkillsBench measured +16.2 pp overall but only about +4.5 pp for SWE. SWE-Skills-Bench found 39 of 49 public SWE skills gave zero pass-rate gain (+1.2% average), with token overhead of up to +451%. A few skills help a lot (up to +30%) and some hurt (up to −10%) when their guidance conflicts with project context. Evidence favours a **small, curated, measured** default set over large bundles.

### Cited Findings
- SkillsBench (arXiv 2602.12670) — [arXiv](https://arxiv.org/html/2602.12670v1); domain figure via [O'Reilly Radar](https://oreilly.com/radar/agent-skills-work-but-the-research-shows-most-teams-are-building-them-wrong):
  - Curated skills raised the average pass rate by 16.2 pp across 7 agent-model configurations.
  - The gain ranged from about +4.5 pp in software engineering to about +51.9 pp in healthcare.
  - 16 of 84 tasks had negative deltas.
  - Self-generated skills gave no benefit on average.
  - Focused skills with 2-3 modules beat comprehensive documentation.
- A secondary analysis gives absolute averages of 40.6% (curated), 24.3% (no skills) and 21.0% (self-generated). I could not verify these against the paper — [engineerscodex](https://www.engineerscodex.com/skillsbench-ai-generated-skills)
- SWE-Skills-Bench (arXiv 2603.15401, March 2026) — [arXiv 2603.15401](https://arxiv.org/pdf/2603.15401):
  - Setup: 49 public SWE skills, about 565 task instances.
  - 39 of 49 skills gave zero pass-rate improvement; the average gain was +1.2%.
  - Token overhead ranged "from modest savings to a 451% increase" with flat pass rates.
  - 7 skills gained up to +30%. 3 degraded results by up to −10%, attributed to "version-mismatched guidance conflicting with project context".
- On TDD skills specifically (SWE-Skills-Bench via a survey) — [arXiv 2609.12012 survey](https://arxiv.org/pdf/2609.12012):
  - `tdd-workflow` with Claude Code and Haiku 4.5: pass rate 21.4% → 28.6% on 14 tasks, but average tokens rose from 83K to 148K.
  - `springboot-tdd`: 80% → 70% on 10 tasks, with tokens falling.
  - The survey's verdict: "neither a universal benefit nor a universal cost penalty".

### Inferences
- The framework should ship an **eval harness per skill**: A/B of pass rate, tokens and cost on the org's own tasks. Admins should promote skills into the catalog only on measured benefit. SkillsBench's finding that self-generated skills do not help argues against letting agents author catalog skills without human curation.
- Stack-specific skills can conflict with project versions, as the −10% cases show. Catalog entries should declare `compatibility` (language, framework version) and be matched to repos.

### Gaps
- No independent benchmark isolates the obra/superpowers or addyosmani/mattpocock skill sets end to end. See section 3.

## 3. Candidate-by-candidate evaluation (table + notes)

### Takeaway
Bundle (or vendor and adapt) a small set of permissively licensed, process-discipline skills:
- From **obra/superpowers** (MIT): TDD, systematic debugging, verification-before-completion, writing-plans, code-review request and response.
- From **addyosmani/agent-skills** and **mattpocock/skills** (both MIT): spec, ADR, interview/grill, code review, incremental implementation.
- From **Trail of Bits** (CC-BY-SA-4.0, copyleft, so offer as optional): security review.
- From **anthropics/skills** (Apache-2.0 subset): skill-creator and webapp-testing.

Treat the persona libraries (wshobson/agents, VoltAgent subagents, BMAD) and indexes as **reference material for writing our own agent templates**, not as bundles. Do **not** bundle Caveman's CLI or proxy. At most, adopt a framework-native terse style for machine-to-machine channels.

### Cited Findings (repo facts)
- **obra/superpowers**: 300.5k stars, MIT, last push 2026-10-10 — [GitHub](https://github.com/obra/superpowers)
  - **Skills:** brainstorming, using-git-worktrees, writing-plans, subagent-driven-development, executing-plans, test-driven-development, systematic-debugging, verification-before-completion, requesting-code-review, receiving-code-review, dispatching-parallel-agents, finishing-a-development-branch, diagnosing-superpowers, writing-skills, using-superpowers.
  - **Harnesses:** installs on Claude Code (official marketplace), Codex (official plugin marketplace), Cursor, Gemini CLI, Copilot CLI, OpenCode, Kimi, Qwen, Pi, Hermes, Factory, Devin, Grok, Antigravity.
  - **Contributions:** the maintainers state they "don't generally accept contributions of new skills".
  - **Commercial:** the steward, Prime Radiant, offers commercial support.
  - **Telemetry:** the optional visual-companion logo loads from Prime Radiant's site and sends the Superpowers version. Opt out with `SUPERPOWERS_DISABLE_TELEMETRY`; Claude Code's `DISABLE_TELEMETRY` is also honoured.
- **Caveman** (JuliusBrussee/caveman): 113.0k stars, Apache-2.0, last push 2026-10-15. Now v4.0, a six-part product — [GitHub](https://github.com/JuliusBrussee/caveman)
  - Parts: output "voice" skill, input-compression proxy, "Auto" model router, "Blocks" script reuse, cache-warming, and browse compression, plus a commercial "Caveman Cloud".
  - The CLI sends usage telemetry **by default** (commands, token counts, install ID, OS, IP), with opt-out.
  - "Auto" sends the latest ask plus attachments such as CLAUDE.md to Caveman Cloud.
  - "Caveman" and its logo are trademarks of the author.
- **anthropics/skills**: 181.5k stars, last push 2026-10-09. GitHub detects no single licence — [GitHub](https://github.com/anthropics/skills)
  - Licensing per the README: "Many skills… are open source (Apache 2.0)", but docx/pdf/pptx/xlsx are "**source-available, not open source**".
  - Skills: skill-creator, mcp-builder, webapp-testing, frontend-design, doc-coauthoring, claude-api, brand-guidelines, internal-comms, and others.
  - Labelled "for demonstration and educational purposes only".
- **openai/skills**: 28.1k stars, deprecated. The licence is per skill (`LICENSE.txt` in each folder) and must be checked skill by skill. The successor openai/plugins has 7.5k stars and no repo-level licence detected — [openai/skills](https://github.com/openai/skills); [openai/plugins](https://github.com/openai/plugins)
- **github/awesome-copilot**: 40.1k stars, MIT, last push 2026-10-16. A community collection of Copilot agents, instructions (file-pattern coding standards), skills, hooks and plugins. It carries the caveat: "sourced from third-party developers. Please inspect any agent… before installing" — [GitHub](https://github.com/github/awesome-copilot)
- **wshobson/agents**: 40.9k stars, MIT, last push 2026-10-15 — [GitHub](https://github.com/wshobson/agents)
  - Contents: 94 plugins, 202 agents, 185 skills and 105 commands, across Claude Code, Codex, Cursor, OpenCode, Antigravity, Copilot and Pi.
  - Codex-generated copies split bodies to fit an 8 KB budget.
  - **wshobson/commands** is **archived** (MIT, 2.6k stars) and replaced by the marketplace — [GitHub](https://github.com/wshobson/commands)
- **VoltAgent/awesome-claude-code-subagents**: 25.8k stars, MIT, last push 2026-10-11. 161+ Claude Code *subagent* definitions (personas), not Agent Skills. The README carries sponsored listings — [GitHub](https://github.com/VoltAgent/awesome-claude-code-subagents)
- **BMAD-METHOD**: 54.2k stars, licence shows as NOASSERTION — [GitHub](https://github.com/bmad-code-org/BMAD-METHOD); [LICENSE](https://raw.githubusercontent.com/bmad-code-org/BMAD-METHOD/HEAD/LICENSE)
  - The LICENSE file is MIT **plus a trademark notice**: "BMad™, BMad Method™… trademarks… covering all casings and variations".
  - Stable v6.12. v7 is in "preview alpha… expect breaking changes", with the v7 ETA given as 16 Oct 2026.
  - v7 is distributed as Agent Skills via `npx skills add` and Claude/Codex plugin marketplaces. It needs uv and Python.
- **github/spec-kit**: 141.5k stars, MIT, last push 2026-10-16 — [GitHub](https://github.com/github/spec-kit)
  - Ships `/speckit-*` commands as agent skills: constitution → specify → plan → tasks → implement, plus a bug flow (assess → fix → test).
  - Has presets and templates with project-local overrides and multi-agent integrations.
- **trailofbits/skills**: 7.6k stars, **CC-BY-SA-4.0**, last push 2026-10-15. A security plugin marketplace — [GitHub](https://github.com/trailofbits/skills); [skills-curated](https://github.com/trailofbits/skills-curated)
  - Skills: differential-review, insecure-defaults, sharp-edges, static-analysis (CodeQL/Semgrep/SARIF), supply-chain-risk-auditor, variant-analysis, fp-check, c-review, rust-review, property-based-testing, mutation-testing, spec-to-code-compliance, semgrep-rule-creator, agentic-actions-auditor, among others.
  - Loads in Codex through Claude marketplace compatibility.
  - Companion repo trailofbits/skills-curated is also CC-BY-SA-4.0 (515 stars).
- **addyosmani/agent-skills**: 107.3k stars, MIT, last push 2026-10-13 — [GitHub](https://github.com/addyosmani/agent-skills)
  - 25 skills: spec-driven-development, interview-me, idea-refine, planning-and-task-breakdown, documentation-and-adrs, api-and-interface-design, incremental-implementation, test-driven-development, debugging-and-error-recovery, code-review-and-quality ("five-axis review"), code-simplification, security-and-hardening, performance-optimization, browser-testing-with-devtools, ci-cd-and-automation, observability-and-instrumentation, shipping-and-launch, deprecation-and-migration, context-engineering, and more.
  - 9 lifecycle commands: /spec, /plan, /build, /test, /review, /ship, and others.
  - Known gap: single-skill installs miss the shared `references/` (issue #361).
- **mattpocock/skills**: 293.8k stars, MIT, last push 2026-10-13. Claims to be "small, easy to adapt, and composable. They work with any model" and explicitly positions itself against process-owning frameworks (GSD, BMAD, Spec-Kit) — [GitHub](https://github.com/mattpocock/skills)
  - Engineering skills: grill-with-docs, to-spec, to-tickets, domain-modeling, codebase-design, improve-codebase-architecture, tdd, diagnosing-bugs, code-review, implement, implement-spec, triage, pr, retro, prototype, research.
  - Productivity skills include grill-me.
- **Indexes and registries** — [GitHub API data, links above]:
  - VoltAgent/awesome-agent-skills (MIT, 35.8k stars, "1000+ agent skills")
  - ComposioHQ/awesome-claude-skills (77.2k stars, no licence detected)
  - travisvn/awesome-claude-skills (15.4k stars, no licence detected, last push 2026-04-28, going stale)
  - hesreallyhim/awesome-claude-code (55.8k stars, NOASSERTION)
  - vercel-labs/agent-skills (32.5k stars, no licence detected)
  - getsentry/skills (Apache-2.0, 1.1k stars)
  - alirezarezvani/claude-skills (MIT, 28.3k stars, "380+ skills", last push 2026-08-30)
- **anthropics/claude-plugins-official**: Apache-2.0, 40.1k stars. An Anthropic-managed directory that lists superpowers and mattpocock-skills — [GitHub](https://github.com/anthropics/claude-plugins-official); [superpowers README](https://github.com/obra/superpowers); [mattpocock README](https://github.com/mattpocock/skills)

### Cited Findings (superpowers specifics)
- Wording and size, measured by fetching raw SKILL.md files on 2026-10-16 ([obra/superpowers/skills](https://github.com/obra/superpowers/tree/main/skills)). Token counts are my estimate at about 4 bytes per token.
  - test-driven-development: 9.6 KB (~2.4k tokens). Contains no "Claude" or tool-specific terms.
  - systematic-debugging: 9.5 KB.
  - brainstorming: 10.1 KB.
  - writing-plans: 10.3 KB.
  - verification-before-completion: 3.6 KB.
  - requesting-code-review: 3.0 KB.
  - subagent-driven-development: 32.6 KB (~8k tokens, above the spec's recommended <5,000 tokens). It assumes a subagent-dispatch capability.
- The `using-superpowers` bootstrap is injected at session start and uses coercive wording: "If you think there is even a 1% chance a skill might apply… you ABSOLUTELY MUST invoke the skill… YOU DO NOT HAVE A CHOICE". It tells the agent to invoke skills "BEFORE any response or action — including clarifying questions" — [using-superpowers/SKILL.md](https://github.com/obra/superpowers/blob/main/skills/using-superpowers/SKILL.md)
- Evidence: the project's own evals ("Quorum", prime-radiant-inc/superpowers-evals; 131 stars; no licence detected) are explicitly "not a generic benchmark suite… an eval lab for workflow compliance: skill triggering, worktree behavior, subagent coordination, verification reflexes, review quality". They measure process adherence, not outcome uplift — [superpowers-evals](https://github.com/prime-radiant-inc/superpowers-evals)
- Independent evidence is anecdotal:
  - One blogger's 12-session (6 vs 6) test reported 14% fewer tokens and better code quality "but not for every task" — [mcp.directory](https://mcp.directory/blog/superpowers-skill-worth-it-2026)
  - A Chinese-language write-up reports Execute/Review stages that are very token-heavy in subagent mode, "often falling into endless loops of revision and review" — [80aj.com](https://www.80aj.com/2026/06/20/ai-superpowers-claude-code/)
  - A plugin-cost analysis notes the skill catalog is re-listed each turn — [dev.to](https://dev.to/ohugonnot/claude-code-i-had-10-plugins-active-at-once-heres-what-it-actually-costs-2ckn)
  - A review of 6.0 notes the heavy process "only amortizes on complex work" — [Towards AI](https://pub.towardsai.net/superpowers-6-0-unpacked-faster-ai-code-review-and-fewer-tokens-5ab40a6c564c)

### Summary table

| Repo | Licence | Health (stars / last push) | Useful skills → agent role | Evidence | Verdict |
|---|---|---|---|---|---|
| obra/superpowers | MIT | 300.5k / 2026-10-10; core team only accepts few new skills | test-driven-development, systematic-debugging, verification-before-completion → **Developer**; writing-plans → **Design→Dev handoff**; brainstorming → **BA**; requesting/receiving-code-review → **Code Review / Developer**; writing-skills → **admins** | Compliance evals only (Quorum). Anecdotal: +/− token cost. Generic TDD-skill evidence is mixed (21.4→28.6% pass but tokens ~1.8x) | **Bundle selected skills (vendored, pinned, adapted)**. Do NOT ship the `using-superpowers` bootstrap or subagent-driven-development as-is; our orchestrator owns the workflow |
| JuliusBrussee/caveman | Apache-2.0 (+ trademark) | 113k / 2026-10-15; v4 now CLI+proxy+cloud | Output-terse voice → inter-agent chatter only; caveman-review → none (human review needs prose); caveman-compress → memory files | JetBrains: −8.5% output tokens, no quality loss (p=0.82) but not cheaper overall; LemonCrow: −44% output, +0.06% total cost; own data: only 3% below "Answer concisely." | **Ignore the product; optional framework-native "terse internal" style**. Never bundle the CLI (default telemetry, cloud routing) |
| anthropics/skills | Mixed: Apache-2.0 for most; docx/pdf/pptx/xlsx **source-available, not OSS** | 181.5k / 2026-10-09 | skill-creator → **admins/skill authors**; webapp-testing → **QA**; mcp-builder → custom agents; doc-coauthoring → **BA** | Demo-grade ("educational purposes only") | **Optional (Apache subset only)**; exclude document skills |
| openai/skills → openai/plugins | Per-skill LICENSE.txt / none detected | 28.1k (deprecated) / 7.5k | gh-address-comments, create-plan (experimental) | None found | **Ignore** (check per-skill licence if cherry-picking) |
| github/awesome-copilot | MIT | 40.1k / 2026-10-16 | Language/framework *instructions* (file-glob standards) → **Developer/Code Review** per stack; some testing/review skills | Community, unvetted ("inspect before installing") | **Optional source to cherry-pick** stack instructions |
| wshobson/agents (+commands archived) | MIT | 40.9k / 2026-10-15 | 202 agent personas (python-pro, security-auditor, test-automator, etc.) → templates for **custom agents**; skills like python-testing-patterns → **QA/Dev** | None | **Reference only** for persona templates; cherry-pick individual skills after review |
| VoltAgent/awesome-claude-code-subagents | MIT | 25.8k / 2026-10-11 | 161 subagent personas → inspiration for **custom agents** | None; sponsored listings | **Reference only** |
| bmad-code-org/BMAD-METHOD | MIT + trademark notice (shows NOASSERTION) | 54.2k / 2026-10-16; v7 alpha, breaking changes | Analyst/PM/Architect/Dev/QA personas and PRD/architecture templates → **BA, Design** templates | None independent | **Reference only** (process-owning; can't use the name; churn) |
| github/spec-kit | MIT | 141.5k / 2026-10-16 | constitution/spec/plan/tasks templates → **BA (spec)**, **Design (plan)**; bugfix assess→fix→test → **Developer** | None independent | **Optional: adopt templates as artifact schemas**, not the CLI |
| trailofbits/skills | **CC-BY-SA-4.0 (share-alike copyleft; CC licences are not OSI-approved for software)** | 7.6k / 2026-10-15; reputable security firm | differential-review, insecure-defaults, sharp-edges, supply-chain-risk-auditor, static-analysis → **Code Review (security)**; property-based-testing, mutation-testing → **QA**; spec-to-code-compliance → **QA/Review** | Expert-authored; no published uplift numbers | **Optional (high value), shipped unmodified as a separate catalog pack** so share-alike obligations stay contained; legal review first |
| addyosmani/agent-skills | MIT | 107.3k / 2026-10-13 | spec-driven-development, interview-me, idea-refine → **BA**; documentation-and-adrs, api-and-interface-design → **Design**; incremental-implementation, debugging-and-error-recovery → **Developer**; code-review-and-quality, security-and-hardening → **Code Review**; browser-testing-with-devtools → **QA** | None independent | **Bundle selected skills** (best role coverage, permissive) |
| mattpocock/skills | MIT | 293.8k / 2026-10-13 | grill-with-docs, to-spec, to-tickets, domain-modeling → **BA**; codebase-design, improve-codebase-architecture → **Design**; tdd, diagnosing-bugs → **Developer**; code-review → **Review** | None independent; deliberately small and model-agnostic | **Bundle selected skills** (BA/Design especially) |
| VoltAgent/awesome-agent-skills, ComposioHQ/awesome-claude-skills, travisvn, hesreallyhim, alirezarezvani/claude-skills | MIT / none / none / NOASSERTION / MIT | 35.8k / 77.2k / 15.4k (stale) / 55.8k / 28.3k | Discovery only | n/a | **Ignore as sources to ship**; use for discovery |
| vercel-labs/skills (`npx skills`) | MIT | 35.2k / 2026-10-09 | Installer CLI across 70+ agents | n/a | **Optional tooling**. Our catalog sync can mimic it; don't let devs install from the open registry |
| getsentry/skills | Apache-2.0 | 1.1k / 2026-10-16 | Sentry team dev skills | None | Optional / watch |

### Inferences
- **Provider-agnostic wording:** superpowers' core discipline skills (TDD, debugging, verification) contain no Claude-specific references and are portable as-is. The orchestration skills (using-superpowers, subagent-driven-development, writing-plans, requesting-code-review) assume subagent dispatch and a self-driven workflow. That duplicates and conflicts with *our* orchestrator, which already sequences BA → Design → Dev → Review → QA.
- The coercive "1% chance… ABSOLUTELY MUST" bootstrap is a conflicting-instruction risk alongside our role prompts. It also inflates catalog-triggering on weaker models. Drop it and use explicit per-role pre-activation.
- The "brainstorming" skill expects a live human in the loop for Q&A. In an autonomous pipeline it must be adapted so questions are routed to a human-in-the-loop (HITL) gate or emitted as an "open questions" section.
- addyosmani and mattpocock cover BA and Design roles that superpowers does not cover well (ADRs, API design, domain modelling, specs).
- Trail of Bits is the strongest security content. Its CC-BY-SA-4.0 licence means modified versions must be shared under the same licence, so distribute it unmodified as a separate opt-in pack. This is my licence reading, not legal advice.

### Gaps
- No outcome benchmark exists for superpowers, addyosmani, mattpocock, BMAD or spec-kit as packages. Their stars are a popularity signal, not evidence of benefit.
- I did not read every SKILL.md in addyosmani, mattpocock or Trail of Bits for tool-specific wording or bundled scripts. A per-skill audit is still needed before bundling.
- Per-skill licences inside openai/plugins and vercel-labs/agent-skills were not checked.

## 4. Caveman: real trade-offs, where terse output is safe vs harmful

### Takeaway
Caveman-style output reliably shortens *output*, but in agentic coding the bill is dominated by input and cache re-reads. Independent A/Bs therefore show small or no net cost savings:
- JetBrains: −8.5% output tokens, no measurable quality loss, overall cost not lower.
- LemonCrow: −44% output, but +0.06% total cost.

Academic work (Adobe's CAVEWOMAN) finds terse output does cut cost on short-answer tasks, but:
- small models can lose a lot of accuracy;
- about half of "correct" terse answers no longer semantically match the full answer, which the authors say harms transcripts and audit trails;
- compressing the *input* prompt is "a strict lose-lose".

Terse output is safe for machine-consumed, inter-agent chatter, and harmful for human-reviewed handoffs (specs, ADRs, review comments).

### Cited Findings
- Caveman's own numbers — [Caveman README](https://github.com/JuliusBrussee/caveman); [HONEST-NUMBERS.md](https://github.com/JuliusBrussee/caveman/blob/main/docs/HONEST-NUMBERS.md):
  - On 10 dev questions with claude-opus-5-5, output tokens were 6,983 (no instruction), 4,334 ("Answer concisely."), 4,119 (/caveman) and 2,693 (/ultracave).
  - The project concedes that "'Be concise' is the real baseline". /caveman cuts only 3% more at the median; /ultracave 35%. n=10, single run.
  - "The rules cost about 1,160 tokens of input", and on per-request billing (Copilot) "shorter answers save nothing".
  - It states token counts "do not prove semantic or technical equivalence" and that "no quality claim is published".
- Net-negative cases reported by Caveman itself — [HONEST-NUMBERS.md](https://github.com/JuliusBrussee/caveman/blob/main/docs/HONEST-NUMBERS.md):
  - Terse coding Q&A, where fixed prompt overhead exceeded the saving (issue #145).
  - Per-request billing (#506).
  - A Cursor A/B with 4.3M tokens with caveman vs 1M without, at twice the wall-clock time; not reproducible (#550).
- Outside A/Bs listed by Caveman — [HONEST-NUMBERS.md](https://github.com/JuliusBrussee/caveman/blob/main/docs/HONEST-NUMBERS.md):
  - LemonCrow: 300 runs per arm on claude-opus-4-8. Billed output −44.1%, **total cost +0.06%**, with +1,532 cached context tokens per run.
  - edubraqd: across 662 Claude Code sessions, output was 0.3% of all tokens, and caveman's net bill effect was within ±0.2%.
- JetBrains (July 2026) — [JetBrains blog](https://blog.jetbrains.com/ai/2026/07/speak-to-ai-agents-like-cavemen-tosave-tokens/) (403 to fetcher; details via [TechTimes](https://www.techtimes.com/articles/320756/20260716/jetbrains-tests-caveman-token-skill-86-real-tasks-savings-hit-9-not-65.htm), [InfoWorld](https://www.infoworld.com/article/4193775/talk-like-a-caveman-prompts-save-tokens-but-far-less-than-promised.html)):
  - Setup: 86 real coding tasks (SkillsBench), paired A/B. Per TechTimes, Claude Sonnet 5 at low effort.
  - 8.5% fewer output tokens, described as a ceiling. The advertised figure was 65%.
  - No measurable quality loss (p = 0.82) and no detectable effect on task success, code quality or time.
  - Total benchmark cost was *higher* for the caveman runs; InfoWorld attributes this to one long-context outlier task.
  - The same JetBrains series found rtk (an output-compression tool) at +7.6% cost — [JetBrains rtk post](https://blog.jetbrains.com/ai/2026/07/rtk-claude-code-token-savings/)
- CAVEWOMAN (Adeyemi, Rossi, Dernoncourt; Adobe Research; arXiv 2606.24083, June 2026) — [arXiv abs](https://arxiv.org/abs/2606.24083); [HTML](https://arxiv.org/html/2606.24083):
  - **Cost:** output compression cut cost 1.4-2.4x per API model (up to 3x) and was cheaper in 17 of 20 cells. GPT-5.4 was an exception because its hidden reasoning tokens dominate billing.
  - **Accuracy is model-dependent.** Qwen2.5-VL-7B on GSM8K fell 82.4→72.3. Qwen3.5-9B collapsed (BoolQ 62.0→37.8, GSM8K 21.5→11.4). GPT-4o was stable or better (GSM8K 91.5→95.6).
  - **Divergence:** across non-reasoning models, 51.9% of terse generations were "correct yet no longer entailed the model's own unconstrained reference" (80.4% length-matched). The authors say this does not matter when only the final answer is consumed, but affects "transcripts, audit trails, and reasoning displays".
  - **Input compression** raises cost (about 1.15x on average, up to 2.7x) while accuracy "collapses": "a strict lose-lose".
  - **Scope:** benchmarks were short-answer (GSM8K, BoolQ, ARC-Easy, CSQA, MMLU-STEM). No claims are made about long-form tasks.
- Caveman's own voice rules keep "Security warnings, irreversible actions, and confused users" in full sentences and keep code, paths and errors verbatim. It never rewrites user prompts, citing the Adobe finding — [Caveman README](https://github.com/JuliusBrussee/caveman)
- Caveman's input-side proxy reports 33.2% fewer session input tokens on its own synthetic suite (each file hides one record in 61-72 KB of noise; HTML got 9.9% worse). This is vendor-run — [Caveman README](https://github.com/JuliusBrussee/caveman)

### Inferences
- **Safe zones** (machine-consumed, low audit value):
  - child-agent → orchestrator status chatter
  - progress narration between tool calls
  - intermediate scratch summaries that are regenerated anyway
  - commit-message one-liners (if the org accepts them)
- Even here, **structured outputs** (Pydantic models / JSON schema) are a better, model-agnostic way to get compact, parseable inter-agent messages than a prose "voice". Prefer them.
- **Harmful zones:**
  - Human-reviewed handoffs: BA requirements/spec, Design ADR, Code Review comments to developers, QA test reports and release notes.
  - Audit trails in regulated contexts. This is the 51.9% "correct-but-divergent" problem.
  - Any run on **small local models**: CAVEWOMAN's Qwen3.5-9B collapse is directly relevant because the framework supports local models.
- **Cost lever ranking for a ~1000-dev org:**
  1. Prompt caching, tool-output trimming and context hygiene (input side) — the higher-value levers.
  2. Model and effort routing.
  3. Output-voice rules — marginal. "Be concise" captures most of the gain at near-zero input cost.
- Do not deploy Caveman's CLI or proxy org-wide. Telemetry is on by default, and "Auto" sends prompts and attachments to a third-party cloud. That needs data-protection review, and it overlaps with our own model proxy.

### Gaps
- No independent study measures terse output in *long-form* SDLC artefacts (specs, ADRs) or human reviewer comprehension time. That is a gap in the literature.
- The JetBrains primary page could not be fetched (403), so its details come via TechTimes and InfoWorld and Caveman's summary.

## 5. Org-scale governance of skills (1000 devs, admin-curated catalog)

### Takeaway
Treat skills as **code with full agent privileges**. Documented in-the-wild attacks:
- 157 malicious skills (632 vulnerabilities) out of 98,380 scanned (USENIX Security 2026);
- 341 malicious of 2,857 on ClawHub (about 12%) in the ClawHavoc campaign.

Governance needs: a private, pinned, signed catalog; static and LLM scanning of `SKILL.md` *and* bundled scripts; per-agent allow-lists; no script execution without a sandbox; trust-gating of repo-level skills; and per-skill evals before promotion.

### Cited Findings
- "'Do Not Mention This to the User': Detecting and Understanding Malicious Agent Skills in the Wild" (Liu et al., arXiv 2602.06547, accepted USENIX Security 2026) — [arXiv](https://arxiv.org/abs/2602.06547):
  - 98,380 skills from two registries were scanned. 157 were confirmed malicious, with 632 vulnerabilities across 13 techniques (about 4 per skill).
  - Dominant strategies: credential theft via remote code execution, and agent manipulation through adversarial instructions in documentation.
  - Over half of cases came from one actor using brand impersonation.
  - All 157 were removed after disclosure.
- Snyk (Feb 2026) on the ClawHavoc campaign — [Snyk](https://snyk.io/articles/skill-md-shell-access/):
  - 341 malicious skills out of 2,857 audited on ClawHub.
  - "Prerequisites" sections pointed to trojanized downloads that installed the AMOS infostealer, targeting SSH keys, API keys and `.env` files.
  - Skills have "no cryptographic signing". Hidden payloads sit in `install.sh` or in fenced code blocks.
  - Unpinned `npx` dependencies are supply-chain exposure.
  - Advice: "treat third-party skills as trusted code. Read them before enabling", sandbox execution, and pin versions.
- The OWASP Agentic Skills Top 10 (v0.5, June 2026) frames skill risks, noting skills execute with the host agent's full privileges — [OWASP PDF](https://owasp.org/www-project-agentic-skills-top-10/docs/OWASP-Agentic-Skills-Top10-v0.5.pdf) (seen via search summary; not read in full)
- The spec's `allowed-tools` is experimental, and the spec has no signing or version field — [agentskills.io/specification](https://agentskills.io/specification)
- The integration guide recommends trust-gating project-level skills, hiding disallowed skills from the catalog, enum-constraining activation, and optionally prompting for consent or tracking activations in a dedicated tool — [agentskills.io/integrate-skills](https://agentskills.io/integrate-skills)
- Telemetry inside skill packs is real:
  - Superpowers' visual companion loads a logo carrying the version (opt-out env var) — [superpowers README](https://github.com/obra/superpowers)
  - The Caveman CLI telemetry is on by default — [Caveman README](https://github.com/JuliusBrussee/caveman)
- Version-mismatched skill guidance degraded SWE pass rates by up to −10% — [SWE-Skills-Bench](https://arxiv.org/pdf/2603.15401)

### Inferences
Recommended governance model for the framework:
1. **Private catalog repo.** Every skill is vendored (copied, not fetched live) with `metadata.source_repo`, `source_commit`, `upstream_license`, `catalog_version` (semver), `owner`, `risk_tier`, `approved_roles`, `requires_scripts: bool`.
   - Pin by content hash; the framework refuses a skill whose hash does not match the catalog lockfile.
   - Optionally sign releases with Sigstore/cosign.
2. **Admission pipeline:**
   - licence check (block source-available; flag CC-BY-SA/GPL; require MIT/Apache/BSD for modification);
   - `skills-ref validate`;
   - size budget (body under 5k tokens);
   - static scan of scripts (Semgrep, secret scan, network/exec calls) and of markdown fenced code;
   - an LLM-based injection/"hidden instruction" review (e.g. "do not mention to the user", exfiltration URLs);
   - a human security review for any skill with scripts;
   - an A/B eval on a golden task set (pass rate, tokens, cost) before promotion.
3. **Runtime policy:**
   - Per-agent `skills:` allow-list in the template front matter, as pre-activated or model-activatable.
   - Org deny-list enforced centrally.
   - Repo-level `.agents/skills` honoured only for trusted repos and only after admin approval.
   - Scripts run only in a sandbox (container, no network by default, no credentials mounted).
   - Map `allowed-tools` onto our own tool permission layer rather than trusting it.
   - Strip or disable vendor telemetry.
   - Log every activation (skill, version, agent, run) for audit and cost attribution.
4. **Conflict control:**
   - Limit concurrent pre-activated skills per role to about 3-5.
   - Forbid "always/MUST invoke" bootstraps.
   - Define a precedence order: org policy > role template > skill.
   - Lint skills for contradictory directives against role templates.
5. **Lifecycle:**
   - Quarterly upstream diff review (upstreams churn, e.g. BMAD v7 and Caveman v4 rewrites).
   - Deprecation flags.
   - Per-skill owners.
   - Re-run evals on model upgrades.

### Gaps
- No standard exists yet for skill signing or provenance (no SBOM-equivalent in the spec). Snyk markets "AI-BOM" tooling but I did not evaluate it.
- The OWASP Agentic Skills Top 10 contents were not read directly.

## 6. Recommendation: default skill set per built-in agent role

### Takeaway
Ship a lean, vendored default of about 3-5 skills per role, drawn mainly from obra/superpowers, addyosmani/agent-skills and mattpocock/skills (all MIT). Offer Trail of Bits security and testing skills as an opt-in CC-BY-SA pack and Anthropic's Apache-licensed skill-creator for skill authors. Use spec-kit and BMAD only as templates and inspiration for artefacts and personas. Exclude Caveman except as an internal-channel style rule. Validate each default with an org-run A/B before GA, because public evidence for SWE skills is weak (+1.2% to +4.5 pp average).

### Cited Findings
- Source repos and licences as in section 3 — [obra/superpowers](https://github.com/obra/superpowers); [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills); [mattpocock/skills](https://github.com/mattpocock/skills); [trailofbits/skills](https://github.com/trailofbits/skills); [anthropics/skills](https://github.com/anthropics/skills); [github/spec-kit](https://github.com/github/spec-kit)
- Focused skills with 2-3 modules outperform comprehensive documentation — [SkillsBench](https://arxiv.org/html/2602.12670v1)

### Inferences
Proposed default sets. All are vendored and adapted to remove harness-specific orchestration. Activation: "pre" means injected eagerly; "on-demand" means listed in the catalog.

| Role | Default skills (source) | Optional / opt-in |
|---|---|---|
| **Business Analyst** | `interview-me` or `grill-with-docs` (addyosmani / mattpocock) — adapted so questions route to a HITL gate [pre]; `spec-driven-development` (addyosmani) or `to-spec` (mattpocock) [pre]; `domain-modeling` (mattpocock) [on-demand]; spec-kit `spec`/`constitution` templates as the **output schema** | `brainstorming` (superpowers) for interactive sessions; `to-tickets` (mattpocock); `doc-coauthoring` (anthropics, Apache) |
| **Design / Architect** | `documentation-and-adrs` (addyosmani) [pre]; `api-and-interface-design` (addyosmani) [on-demand]; `writing-plans` (superpowers) for the Dev handoff plan [pre]; `codebase-design` / `improve-codebase-architecture` (mattpocock) [on-demand] | Trail of Bits `sharp-edges` (design footguns) |
| **Developer** | `test-driven-development` (superpowers) [pre]; `systematic-debugging` (superpowers) [on-demand]; `verification-before-completion` (superpowers) [pre]; `incremental-implementation` (addyosmani) [on-demand]; `receiving-code-review` (superpowers) [on-demand] | Stack instructions from awesome-copilot; `code-simplification` (addyosmani) |
| **Code Review** | `code-review-and-quality` (addyosmani, five-axis) [pre]; `requesting-code-review` checklist content (superpowers, minus subagent dispatch) [pre]; `security-and-hardening` (addyosmani) [on-demand] | Trail of Bits pack: `differential-review`, `insecure-defaults`, `supply-chain-risk-auditor`, `static-analysis` (CC-BY-SA, unmodified) |
| **QA** | `test-driven-development` (for test-first acceptance tests) or the TDD testing-anti-patterns reference (superpowers) [on-demand]; `browser-testing-with-devtools` (addyosmani) or `webapp-testing` (anthropics, Apache) [on-demand]; `verification-before-completion` (superpowers) [pre] | Trail of Bits `property-based-testing`, `mutation-testing`, `spec-to-code-compliance` |
| **Custom agents / admins** | `skill-creator` (anthropics, Apache) and `writing-skills` (superpowers — "TDD for skills": baseline-fail, then write, then verify) for authoring; persona inspiration from wshobson/agents and VoltAgent (MIT) | — |
| **Token compression** | No Caveman bundle. Framework rule: inter-agent messages use structured output schemas plus a "be concise" line; human-facing artefacts always full prose | Opt-in terse style for child-agent chatter on frontier models only (never small local models) |

- Do not adopt superpowers wholesale. Its `using-superpowers` bootstrap and `subagent-driven-development` / `executing-plans` / `using-git-worktrees` / `finishing-a-development-branch` encode a full workflow that duplicates the framework orchestrator. They also add coercive, conflicting instructions, and subagent-driven-development alone is ~8k tokens (estimate).
- When delegating to Claude Code or Codex, a team may still opt in to the full superpowers plugin per workspace, which is a reasonable "power user" path.
- Licensing flags:
  - Trail of Bits: CC-BY-SA-4.0 (share-alike; not an OSI software licence).
  - anthropics docx/pdf/pptx/xlsx: source-available, exclude.
  - BMAD and Caveman: trademark notices, so do not use their names in product UI.
  - openai/plugins, vercel-labs/agent-skills, ComposioHQ and travisvn indexes: no repo licence detected, so do not vendor content from them without a per-file licence.

### Gaps
- None of the proposed defaults has independent outcome data in a multi-role pipeline. The recommendation rests on licence, coverage, provider-neutral wording and general skill-benchmark evidence. An internal A/B is required to make "measurably better" a claim rather than a hypothesis.
