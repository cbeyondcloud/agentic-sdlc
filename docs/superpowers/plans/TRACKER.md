# Tracker — Agentic SDLC framework

Source of truth for execution status. Update the row's Status to Done and put the commit SHA in Notes in the same commit that completes it. Full context: [master plan](2026-10-17-agentic-sdlc-master-plan.md).


Status for every row: **Pending** at plan approval. Columns: `PW` = estimated person-weeks (±50 %); `Done when` is the acceptance check for the row; `Notes` records the completing commit SHA.

### Phase 0 — Bootstrap and spikes (~15 PW, ~4 weeks)

| ID | Sub-plan | Item | Depends on | Done when | PW | Status | Notes |
|---|---|---|---|---|---|---|---|
| P0-01 | SP0 | Repo bootstrap: git init, uv workspace (`core`, `commons`, `cli`, `control-plane`), ruff, pytest, pre-commit, CI matrix (Windows/macOS/Linux), MIT LICENSE, `.gitattributes eol=lf`; copy spec, research, this plan and `TRACKER.md` into the repo | — | `uv run pytest` and `uv run ruff check .` green locally and in CI on all 3 OSes; tracker committed | 1 | Done | f6dede2..4e124c9 — CI green on ubuntu/macos/windows (run 39027665054); repo cbeyondcloud/agentic-sdlc |
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
| M1-01 | SP1 | `AgentDefinition` model + Markdown loader, incl. optional `deny:` block (`shell` command prefixes, `paths` globs) — D31 | P0-01 | `core/tests/definitions/test_agent.py` passes (5 tests in detailed plan) | 0.5 | Done | a4ef92b + deny block (D31) commit — 10 tests, check.py --all green (Windows + Linux); + templates/agent/agent.md (all fields with commented samples, 3 tests) |
| M1-02 | SP1 | `PipelineDefinition` model + YAML loader | P0-01 | `test_pipeline.py` passes (5 tests) | 0.5 | Done | feat(core): pipeline definition — 8 tests, check.py --all green (Windows + Linux); + templates/pipeline/pipeline.yaml (every field and step type with commented examples, 3 tests) |
| M1-03 | SP1 | `OrgPolicy` model + policy-dir loader + policy version (incl. `allowed_repo_patterns`, `tool_mirror_url`, `licence_scan`, `allow_repo_pipelines` (D33, default false)) | P0-01 | `test_policy_load.py` passes (6 tests) | 1 | Done | feat(core): org policy model and loader — 11 tests, check.py --all green (Windows + Linux) |
| M1-04 | SP1 | Layered discovery + `resolve_agent` (tiers, team narrowing, clamping, violations; deny lists = union of org + team + agent, deny beats allow; repo-level pipelines ignored with a warning unless `allow_repo_pipelines` — D33) | M1-01, M1-03 | `test_resolve.py` + `test_discovery.py` pass (8 tests) | 1 | Done | feat(core): layered discovery + resolve_agent — 21 tests (4 discovery, 17 resolve), check.py --all green (Windows + Linux) |
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
