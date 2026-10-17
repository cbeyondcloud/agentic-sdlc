# sdlc-harness (Zyroflo) vs the Agentic SDLC design spec

| | |
|---|---|
| Date | 2026-10-16 |
| Compared | `cbeyondcloud/sdlc-harness` @ `0625e39` (main, 1,224 commits, 2026-07-13 → 2026-10-16, single author) vs `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md` |
| Method | Full read of the spec; read of README, CLAUDE.md, RUNNER_CONTRACT, LIFECYCLE, STEP_SPEC, stage SOPs (P2/P4/P6), templates (policy/profile/providers/schemas/agents/SKILL/hooks/connectors/grafana/collector/snapshot-publish), graph design doc + `tools/graph` code, milestone gate records M0–M7, backlog status, pyproject, CI, manifest. "Inferred" marks anything not read directly in code. |

## 1. What sdlc-harness is

sdlc-harness ("Zyroflo", MIT) is a **governance harness that you install into a repo**. It is **not an agent runtime**. `sdlc-init.py` (5,272 lines, stdlib only) adds a `/sdlc` skill (`.claude/skills/sdlc/SKILL.md`), plus the same instructions in the native format of each detected tool (Cursor, Windsurf, Copilot, Gemini, Aider, Codex/AGENTS.md). It also writes `.sdlc/profile.yml`, `.sdlc/policy.yml`, `providers.toml`, a rubric, a plan template, and a CI ledger check.

**Runtime model:** by default the **AI coding tool you already have open is the orchestrator**. It reads the skill and the per-phase SOPs (`sdlc/stages/P0..P11`) and calls the harness's Python CLIs (`tools/sdlc/*.py`: `stage`, `state`, `approve`, `worktree`, `guard`, `drive`, `release`, …). Gates are enforced because those CLIs **refuse to advance without a matching hash-chained ledger entry**. The runner is never trusted: the CLIs check artifacts.

There are two optional execution modes:
- **Declared runner:** any CLI agent that meets `RUNNER_CONTRACT.md` (goose and codex are the examples). Conformance is checked by `tools/provider_smoke.py`.
- **Model endpoint:** `http_runner.py` talks openai-chat or anthropic-messages over stdlib urllib to a `providers.toml` entry, with keys read from `.env`. It is chat-only, so it cannot edit files or run commands.

`drive.py` walks the steps between gates unattended. There is **no agent loop of its own, no LLM gateway, no control plane, no RBAC and no budgets**.

The pipeline is a **fixed 12-phase superset (P0–P11) with gates G1–G5**:
- A profile chooses which phases run. Order is fixed.
- intake, implementation and review are locked.
- `extra_gates` can add more gates.
- Gate regimes are solo, team or regulated.

**Tier 2** is a home-grown cross-service **"domain graph"**: tree-sitter extractors, JSONL with SQLite/Neo4j stores, an MCP server, and committed per-component snapshots.

**Maturity:**
- Tier 0/1 are declared complete (M9 closed 2026-09-23).
- The backlog lists 216 Done, 57 Waived, 25 Deferred and 4 Open rows. "1.0 is done when they close."
- About 57k lines of production Python and about 107k lines of tests (5,748 test functions).
- Real-world evidence is thin:
  - The M4 kill gate met 8 of 10 conditions.
  - The paid-LLM end-to-end runs (T369/T370) were waived.
  - The pilot baseline was 9 runs opened, 1 closed (`docs/pilot-plan.md`).
  - October's 225 commits are mostly graph review-fix batches.

## 2. Section-by-section comparison

Coverage: **Full** / **Partial** / **None** / **Diff** (different approach).

| Spec § | sdlc-harness coverage | Evidence | Notes |
|---|---|---|---|
| 1 Goals / acceptance | **Partial** | README; `sdlc/LIFECYCLE.md` | The feature pipeline on an existing or empty repo with gates, a review loop and a PR exists (P0–P8). There is no plain-English custom *agent* (goal 1), no child agents (3), no gateway (5) and no org admin layer (6). Acceptance tests 2 (custom agent) and 3 (admin tier→model override) **cannot pass**. Test 1 is close. Test 4 is plausible (stack fallback is generic). |
| 3 Architecture & modes | **Diff** | CLAUDE.md "three tiers"; README "Two tiers" | Tier 0 (single repo, zero dependencies) and Tier 1/2 (multi-repo estate with a graph) are *capability* tiers, not standalone vs org. No control plane. The harness "holds no database connection string" and only writes files. |
| 4 Repo layout | **Diff** | README "What gets installed" | `.sdlc/` (profile, policy, providers, pipeline.md, plans/, runs/, metrics/) vs our `.agentic/`. The run folder is **committed** on the branch. |
| 5 Agent definitions & tiers | **None / Diff** | `sdlc_templates/agents/sdlc-reviewer.md`, `sdlc-security.md`; `tools/sdlc/models.py` | Agents are *phases*, not definitions. There are only 2 optional Claude subagent personas. No tier abstraction: `models:` maps step to `provider/model` and is off by default. No effort/temperature, no per-agent tool or skill allow-lists, no limits. |
| 6 Pipeline / control flow | **Partial (Diff)** | `LIFECYCLE.md` "Customising the pipeline"; `STEP_SPEC.md`; `tools/sdlc/stage.py` (2,903 lines) | Fixed order, selectable phases, locked steps, gate re-anchoring/merge rules, extra hard gates, review loop (`max_iterations`, `block_on` exit), triage loops back to P3–P6. **No user-defined steps** (the parser exists, the loader "does not ship"), **no `when` branches**, no overlays, no generic loops. Gate edit-then-approve exists (edits are recorded in `feedback.jsonl`). |
| 7 Handoffs | **Partial** | `LIFECYCLE.md` "Artifacts"; `stage.py --verify`; `state.py --record` | Markdown artifacts with YAML frontmatter (`00-intake.md`…`B0-closure.md`), `schema_version`, digests pinned at gate time. The phase contract is "owes these files" (existence plus recorded digest). Per-kind schema-validated front matter like our Pydantic kinds was not found (inferred). |
| 8.1 Init | **Partial** | `sdlc-init.py`; `docs/bootstrap.md`; `sdlc-project.toml` | One repo-level install (detects stack, wires tools, `--workspace` for multi-repo, `--claude-extras`). No per-developer init (prerequisites, auth, policy fetch, skill install). Good pieces: the readiness/refusal messaging and the monorepo unit detection. |
| 8.2 Preflight | **Partial** | `sdlc config --check`; `policy.py --validate`; `drive.py --plan/--spend` | Profile vs policy floors, model routing and missing keys are refused before spending. Runner conformance is optional (`require_conformance`). No RBAC, budget, concurrency or kill-switch checks. |
| 8.3 Execution / state / resume | **Full (Diff)** | `tools/sdlc/state.py`; `drive.py`; SessionStart hook | `state.json` plus a plan/tracker markdown, resumable across sessions and tools; hard gates persist and exit. This is the closest match to our engine semantics. |
| 8.4 Child agents & limits | **None** | SKILL.md capability table | Only prompt guidance ("≤3 subagents"). No spawning API, ledger or limits. |
| 8.5 Stuck detection | **None** | — | Only the review `max_iterations` cap. |
| 8.6 Kill switch | **None** | — | — |
| 8.7 Failure handling | **Partial** | `tools/cli.py` (`run_bounded`, exit-code doctrine) | A strict "could not look ≠ clean" doctrine and bounded subprocesses. No model-retry layer (the runner owns that). |
| 9 Backends | **Partial (Diff)** | `RUNNER_CONTRACT.md`; `provider_smoke.py`; `http_runner.py`; `docs/m6/c2-goose.md` | A generic CLI-runner contract with conformance tiers (null/contract/agent). goose was proven end-to-end (M6-C2); codex is listed but not exercised. No Claude Agent SDK or ACP backend. |
| 9 Sandbox | **Partial** | `tools/sdlc/worktree.py` (962 lines); `guard.py`; `policy.yml` `deny_git`/`deny_commands`/`protected_paths`/`worktrees_root`/diff budget | One worktree per run and component, with recorded base commits. Five "walls" checked by `guard.py` and logged to `guardrail.jsonl`. A token-based destructive-command matcher sees through `&&`, pipes, interpreters and `find -exec`. **No OS sandbox** (no srt, Codex sandbox or containers) and no network egress control. |
| 10 Code intelligence & index | **Diff (strong)** | `tools/graph/`; `docs/sdlc-graph-design.md`; `snapshot.py`; `freshness.py`; `sdlc_templates/snapshot-publish.yml` | Own engine (see §3). Committed shards plus a scheduled bot workflow are very close to D22 (weekly cron, not nightly). |
| 10.4 Memory | **Partial (beyond v1)** | `tools/sdlc/feedback.py`, `learning.py`; P11 | Reads AGENTS.md/CLAUDE.md. Human corrections → `feedback.jsonl` → learning proposals that must pass G3, with `--undo` (D-06: auto-apply never). |
| 11 Skills | **Diff** | `sdlc_templates/SKILL.md`, `CONFIG-SKILL.md`, `GRAPH-SKILL.md` | The harness *is* a skill fanned out to 7 tools. No catalog, pinning, admission checks or per-agent allow-list. |
| 12 Governance / policy-as-code | **Partial (Diff)** | `policy.yml` + `schemas/policy.schema.json`; `policy.py` (4,274 lines); `LIFECYCLE.md` | Two-layer, tighten-only: repo `policy.yml` (floors: gate regime, `block_on`, signing, coverage enforcement, `self_approval`, `allowed_runners`, `required_phases`/`required_gates`) over `profile.yml`. Changes to policy go through a gated run. **No org policy repo, no team layer, no versioned rollout, no catalog.** |
| 12.3 RBAC | **None** | `self_approval: allow\|forbid` | Only a four-eyes equivalent. Any git identity can approve. |
| 13 Identity | **None** | `.env` keys; approver = git identity; `identity: hash` pseudonymisation in exports | — |
| 14 Observability / dashboards | **Partial** | `tools/sdlc/events.py`; `schemas/events.schema.json`; `metrics.py` (export ndjson/csv/sqlite, dashboard.html, REPORT.md, `--merge` org roll-up); `sdlc_templates/grafana/` (6-panel SQLite dashboard); `collector/nightly-sdlc-metrics.yml`; `docs/metrics-columns.md` | Run, step, gate, wait-time, loop-back, tokens and cost events. Tokens "unavailable", never 0; cost only from declared prices. **OTLP emitter deferred (T451).** No traces, no `X-Agentic-*` headers, no central ingest. |
| 14.5 Audit | **Full for pipeline audit; ahead of our v1 in places** | `approve.py`; `.sdlc/ledger_check.py`; `ledger-check.yml`; `status.py --verify` | Hash-chained `approvals.jsonl` and `events.jsonl` are already active. The CI check requires an append-only diff vs the PR base and refuses a merge whose hard gate is missing, rejected or approved over moved bytes. DSSE signing is optional (in-toto). No config-audit events (policy changes are audited via git and gates instead). No git trailers. |
| 15 Security | **Partial** | `tools/review/prepass.py` (gitleaks + ast-grep); `review/sca.py` (osv-scanner); `intake.py` (spec "fenced as data"); `third_party/manifest.json` (sha256-pinned binaries per platform); CI gitleaks over full history | A secret scan **on the diff at review**, not before egress to a model. No licence scan (ScanCode). M4 red-team (a)–(e), including prompt injection, was recorded as a condition. |
| 16 Integrations | **Partial** | `tools/sdlc/connect.py` + `connectors/{jira,github-issue,alert}.json`; `release.py` (gh: push, PR, CI watch, version/changelog/tag; never merges) | Vendor-neutral JSON-path mapping for intake. No Jira comment or transition write-back. No email notifications. |
| 17 TokenLean | **None (compatible)** | `providers.toml` `base_url` | `http_runner` could point at TokenLean's OpenAI- or Anthropic-compatible endpoint. Identity headers and cost-header parsing would need adding. |
| 19 Phasing | **Diff** | `docs/m0..m7`; `SDLC-harness-backlog.md` | Milestone kill gates (M0–M9) with pre-registered criteria and off-ramps. A rigorous process worth copying for our spikes. |
| 20 Testing | **Full (different emphasis)** | 226 test files; `SDLC_FORBID_SKIPS`; process-level CLI tests; fixture estates; `test_zero_dependency_parity.py`; `test_no_stale_shas.py` | Very strong deterministic and contract testing. Linux CI plus a Windows-first developer box. No fake-model pipeline tests, because the harness calls no model. CI is manual-dispatch only (Actions budget). |

## 3. Graph tooling vs code-review-graph / codebase-memory-mcp / Graphify

| Aspect | sdlc-harness `tools/graph` | Our plan (code-review-graph primary) |
|---|---|---|
| Granularity | **Domain/architecture graph** ("DG" namespace). Nodes: `svc`, `ep` (endpoint), `topic`, `table`, `cfg`, `envkey`, `mod`, `deploy`, `ui`, `test`. Edges: `DG_CALLS`, `DG_EXPOSES`, `DG_PRODUCES`/`DG_CONSUMES`, `DG_OWNS_TABLE`, `DG_DEPENDS_ON`, `DG_BUILT_FROM`, `DG_DEPLOYS`, `DG_CONFIGURES`, `DG_SPECIFIES`, `DG_TESTS`, `DG_ALIAS_OF`, `DG_IN_REPO`. **No function/class symbols, no intra-code call graph.** `core/model.py` explicitly leaves a "symbol layer (e.g. CodeGraphContext)" to another tool. | Symbol-level: functions, classes, callers, dependents, tests (code-review-graph). Multi-language symbol index (codebase-memory-mcp). Overview (Graphify). |
| Languages / artefacts | Java, Python, JavaScript, TypeScript (individually pinned tree-sitter grammars, Windows wheels). Extractors: spring, kafka, kafka_python, http_routes, http_calls, openapi, helm, compose, maven, gradle, npm, properties, env_keys, sqlmigrations, frontend, tests. No Go, C#, Rust or protobuf despite the July design doc. | Python/TS/Java symbols, plus more languages in codebase-memory-mcp. |
| Cross-repo | **Yes, its core strength**: workspace of N components, canonical/alias identity, name-join and spec-match linkers. Finds service→topic→service and env→compose→URL chains. | Mostly single-repo. |
| Impact | `impact.py` (930 lines): seed file or diff hunks → bounded (4 hops), direction-aware, confidence-damped traversal (`rules.py`, overridable per workspace). Ranked files with hop depth and best-path parent. Every edge carries file:line plus EXTRACTED/INFERRED/AMBIGUOUS provenance. `pack.py` builds budgeted context packs. | `impact(diff)` incl. tests; `dependents`; `tests_for`. |
| Measured quality | M1 blind A/B vs grep: beat grep on 2 of 3 commits, but **absolute recall@20 was only 0.07–0.09** ("wins by being nonzero, not by being good", `docs/m1/gate-record.md`). M2: 3 non-obvious review findings vs a ≥2 threshold. `corpus_replay.py` re-scores nightly. | No independent benchmark (spike S5). |
| Freshness / snapshot | Per-component committed shards (`<component>/.sdlc/graph/shard.jsonl` + `snapshot.json` with sha256 and parser-version stamps). `--check-fresh` / `--incremental` / `--from-snapshot`. `-text` gitattributes. Determinism: rebuild == snapshot. `snapshot-publish.yml` bot commits **weekly** (cron Monday). | D22: committed to main, nightly bot, `merge=ours`, local overlay. |
| MCP | `mcp_server.py` (fastmcp, optional extra), 8 tools: `graph_status`, `impact`, `impact_diff`, `context_pack`, `search_nodes`, `node_neighbors`, `validate_graph`, `check_fresh`. Reloads when the graph on disk changes. stdout is shielded. | code-review-graph / codebase-memory-mcp expose MCP too. |
| Stores | JSONL canonical; SQLite and Neo4j behind `GraphStore` (contract suite runs on both). | SQLite (code-review-graph). |
| Tests | 113 files, about 1,915 test functions in `tools/graph/tests`. Two fixture estates (orderflow: Java/Spring/Kafka/Helm/TS; pantry: Python/compose) with pre-registered answer keys. A core-genericity test. A zero-silent-drop doctrine. | Unknown. |

**Verdict:** it is **not a replacement** for our symbol-level codeintel. It cannot answer `dependents(symbol)`, `tests_for(file)` at function level, or `path(a,b)` across code. It is a **better fit than anything in our OSS shortlist for cross-service, config, deploy and data impact in microservice estates** (Spring/Kafka/Helm/compose/OpenAPI). It also carries better provenance discipline.

Recommendation:
- Add it as a **second codeintel adapter (`domain-graph`)** beside code-review-graph. The two are complementary: symbol blast radius plus service blast radius.
- Include it in spike S5.
- Adopt its **shard + digest + parser-stamp freshness design** for D22.
- Our non-goal ("don't build our own graph engine") is not violated: it already exists and the user owns it.

## 4. Conflicts with our decisions, and what it has that we lack

### 4.1 Conflicts (D1–D24)

| D# | Conflict |
|---|---|
| D1 Runtime | **Inverted.** The harness has no agent loop. The driving AI tool orchestrates and the harness CLIs police it. Our engine orchestrates and agents are workers. Its runner contract ≈ our CLI backends only. |
| D2 Gateway | Direct provider keys in `.env` via `providers.toml`. No gateway concept or identity headers. Cost comes from declared prices. (Pointing `base_url` at TokenLean would technically work.) |
| D3 Control flow | Fixed 12-phase order. No `when` branches, no generic bounded loops, no custom steps (the loader is unbuilt). |
| D4 Handoffs | Artifacts are files owed per phase. We want per-kind schema-validated front matter with consumes/produces matching. |
| D5 Run state | The run folder is **committed on the branch** (the CI merge check reads the ledger from the branch). Ours is git-ignored locally plus central in org mode. |
| D6 Agent powers | No per-agent tool allow-list. Guard walls only. No OS sandbox. |
| D7 / D19 Layers | repo `policy.yml` > `profile.yml` only. No org policy repo, team narrowing or control plane. |
| D10 | Own graph engine at a different granularity (see §3). |
| D11 / D20 | No budgets, child limits or stuck detector. Only `max_iterations`. |
| D12 / D18 | No identity, keys or RBAC. Approver = git identity. |
| D14 / D17 | Explicitly no central sink ("writes files; loading them is the org's job"). No org mode. |
| D21 | Single repo-level installer. No per-developer init. |
| D22 | Weekly, not nightly. Per-component shard paths, not `.agentic/index/`. |
| D24 | No kill switch. |
| Dependency philosophy | **Zero-dependency, stdlib-only shipped code**: its own YAML-subset reader, no pydantic in shipped code. This conflicts with our OSS-reuse stance (Pydantic AI, ruamel, python-frontmatter, Typer, keyring…). |

### 4.2 Things it has that our spec is missing (worth adopting)

1. **Full lifecycle beyond "deliver a PR"**:
   - P0 baseline test run recorded, then P5 **baseline-relative "no new failures"**, plus changed-file coverage that fails loudly when it cannot be measured.
   - P1 **EARS acceptance criteria** and 2–4 options, at least one free/OSS, each with SPDX licence and performance impact.
   - P3 **ADR triggers** (dependency, boundary or data-model change).
   - P7 triage (findings become tracker rows, looping back).
   - P8 release (CI watch, version, changelog, tag; humans merge).
   - P9 deploy with a fresh cost confirmation each time.
   - P10 verify probes → rollback proposal plus automatic defect intake.
   - P11 closeout verifying **every** criterion, a retro, and **tech debt fed back into intake**.

   Our §5.4/§6 stop at QA → PR.
2. **Categorical review exit**: loop until no `block_on` severity is open. The rubric score is reported, never the gate. Its rationale (scores are not reproducible across providers, and models drift toward the threshold) should replace our `until: review.verdict == 'approve'`.
3. **Deterministic pre-pass before agent review** (gitleaks + ast-grep + osv-scanner), with "could not run = exit 3, never clean".
4. **Gate regimes** (solo/team/regulated) and **gate re-anchoring** when a step is removed. Locked steps. Org `required_phases`/`required_gates`.
5. **Gates pin artifact digests**: an edit after approval voids the gate. A **CI merge check** verifies the ledger is append-only vs base and that every hard gate is approved. This makes the audit enforceable at merge, not just recorded.
6. **Diff budget per review batch** (`max_files_per_batch`, `max_lines_per_batch`) so human gates stay readable.
7. **feedback.jsonl → learning proposals** that pass a gate, with `--undo`. **claims.py** checks numbers stated in documents against measured values.
8. **Runner conformance smoke** with honesty tiers (null < contract < agent). A ready pattern for our backend contract tests (§9.1).
9. **Corpus harvest + nightly replay** (precision@k regression on the team's own merge history), plus a **pre-registered judging protocol**. Directly usable for spike S5 and the skill A/B evals (S10).
10. **De-scope register** with re-add triggers. **Milestone kill gates** with off-ramps.
11. A **token-based destructive-command matcher** (`deny_commands`). A good concrete input for our shell-policy compiler.
12. `vcs.deny_raw_git` / custom commit commands for unusual repos (open-core splits, wrapper scripts).
13. Multi-repo **workspace** (`components[]`, per-component worktrees, dependent build closure, "solo refused when components > 1").
14. Metrics doctrine: tokens "unavailable", never 0. Cost only from a declared price. Pseudonymised identity in exports. An idempotent org roll-up keyed by event hash.
15. Fan-out of one lifecycle to 7 AI-tool instruction formats. Useful if we want a "lite" in-IDE mode.

## 5. Closeness and recommendation

**Overall closeness: about 25–30% of our spec.**
- **About 50–60%** of the "pipeline, gates, run-state, audit, lifecycle content" half: §6–8.3, §14.5, §16 partly.
- **About 0–10%** of the "platform" half: TokenLean gateway, agent definitions and tiers, child agents, budgets, stuck detection, kill switch, RBAC, org policy repo, control plane, identity, OTel, skills governance, OS sandbox.
- Its graph is strong but covers a different granularity.

**Recommendation: (c), using sdlc-harness as a component and content source inside our framework, built fresh, i.e. (a) and (c) together. Not (b).**

Why not (b), evolving sdlc-harness:
- **The runtime model is inverted.** Its core assumption is that the user's AI tool orchestrates and the harness only polices artifacts. D1/D2/D11/D20 need an engine that owns the agent loop, every model call through TokenLean, budgets, children and kill checks. That is a new core, not an extension.
- **The zero-dependency, stdlib-only constraint** runs through the codebase: its own YAML subset, no pydantic, single-file installer. Lifting it means rewriting the readers and validators we would build on.
- **The fixed 12-phase superset** has no custom-step loader, no `when` branches and no agent definitions. Our user-defined agents and pipelines (goals 1–2, acceptance tests 2–3) are its biggest gap.
- **The code is large and idiosyncratic.** `policy.py` is 4,274 lines and `stage.py` 2,903, with dense defect-history docstrings. Field validation with real LLMs is thin (T369/T370 waived, pilot 1 of 9 runs closed). Its value is design and doctrine more than battle-tested runtime code.

What to take, concretely (same owner, MIT, so no licence friction):

| Take | Into our component |
|---|---|
| P0–P11 SOPs, EARS, baseline-relative test, `block_on` exit, rubric, ADR triggers, triage, release/CI watch, verify, closeout | `library/agents` + `library/pipelines` (a `full-lifecycle` pipeline beside `feature-delivery`), handoff templates |
| Hash-chained `approvals.jsonl` / `events.jsonl` formats, digest pinning, `ledger_check.py` CI merge gate | `telemetry/` audit model (activates D16 in v1 rather than phase 2), `integrations/github` status check |
| `guard.py` walls + `deny_commands` tokenizer + protected paths + diff budget | `sandbox/` shell-policy convenience layer (the OS sandbox stays the real boundary) |
| `worktree.py` (base-commit recording, all-or-nothing creation, deny_raw_git) | `sandbox/` worktree manager (port the logic) |
| `tools/graph` as a `domain-graph` adapter, its shard/freshness/snapshot design, MCP server | `codeintel/` second adapter, plus the D22 nightly workflow design |
| `RUNNER_CONTRACT.md` + `provider_smoke.py` tiers | Backend contract tests (§9.1, §20) |
| `connect.py` JSON-path connectors | `integrations/jira` intake mapping |
| `metrics.py` exports and doctrine, events schema fields | Control-plane ingest schema, Grafana views |
| Judging protocol, corpus replay, de-scope register, kill-gate milestones | Spike S5/S10 method, §19 phasing |

Keep Zyroflo as the **zero-dependency drop-in product** for solo developers and in-IDE use. Make the new framework the **enterprise platform**. Share the lifecycle content, the ledger and event formats, and the graph between the two, so a repo can graduate from one to the other without changing its audit trail.
