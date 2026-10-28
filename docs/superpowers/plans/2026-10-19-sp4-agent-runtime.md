# SP4 Agent Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a `ResolvedAgent` plus a task into a running agent loop. It calls models only through TokenLean, carries the run's identity, charges every call to the budget ledger, uses only the tools its policy allows (each guarded), can spawn children under admin limits, stops when it is stuck, and can pause for a human and resume in another process.

**Architecture:** A new `agentic.agents` package in `core`, built on **Pydantic AI** (MIT, pinned 2.54.0). `metering.py` is the cost-capturing, budget-charging transport from spike S1, placed under the identity client. `models.py` builds a Pydantic AI model for a resolved agent, on TokenLean's OpenAI or Anthropic route. `runner.py` defines the `AgentRunner` interface and its Pydantic AI implementation, with turn, time and budget limits and pause/resume (spike S8). `tools/` holds the agent tools, each checked by `sdlc_commons.guards.Guard` and offered only if the agent's policy allows it. `children.py` is the child spawner. `stuck.py` ports OpenHands' StuckDetector. The engine (SP6) drives runners. Handoff validation and retries are SP5.

**Tech Stack:** Python 3.12; pydantic-ai-slim[openai,anthropic] 2.54.0 (MIT); httpx2 2.13.1 (BSD-3, D36); the existing `agentic.transport`, `agentic.policy`, `sdlc_commons` (guards, ledger).

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md` — §8.3–8.5 (execution, child limits, stuck detection), §8.7 (failure handling), §14.2 (identity), §12.9 (budgets), D31 (deny lists), D35 (agent types), D36 (httpx2). Spikes: `docs/spikes/S1-cost-capture-pydantic-ai.md`, `docs/spikes/S8-deferred-tools-resume.md`. Master plan rows M1-15…M1-18.

## Global Constraints

- Every model call goes through TokenLean via `base_url`, using `agentic.transport.make_async_client`; product code makes no direct provider call. The API key is passed to the Pydantic AI **provider** (S1).
- Identity headers and the behaviour-header rules of SP3 hold unchanged (§14.2).
- Unmeasured values are `None`, never 0; money is `Decimal`.
- **A child agent can only narrow:** tools ⊆ the parent's; `deny_shell` / `deny_paths` = the parent's ∪ its own; budget, turns and time sliced from the parent (§8.4, D31).
- **A step's inputs are its handoffs, instructions and skills**, never earlier agents' chat histories (§8.3).
- **No vendor telemetry:** set `PYDANTIC_AI_NO_BANNER=1`; no Logfire.
- Dependencies are pinned to releases at least two weeks old.
- Must run on Windows, macOS and Linux; `scripts/check.py --all` is the gate. Tests make no real network or model calls: they use Pydantic AI `TestModel` / `FunctionModel`, or an `httpx2.MockTransport` standing in for TokenLean.
- Spike code under `spikes/` is never imported.

## Review Focus

1. **A model asks for a tool the agent is not allowed** (hallucinated, or a known tool outside its policy). It must not run, and must not be listed to the model. Test in Task 6.
2. **A tool argument that walks out of the tree** (`../`, an absolute path, a symlink or junction out, a denied path). It is refused with a reason the model sees, and nothing is read or written. Test in Task 6.
3. **A time limit that fires while a model stream is in flight.** The reservation is settled or released, never leaked, and the runner returns `stopped: time` with the partial output. Test in Tasks 2 and 4.
4. **Resuming from a state file that was edited, or belongs to another agent version.** Refused before any model call. Test in Task 5.
5. **Children spawned concurrently that together try to exceed `max_total`, `max_concurrent` or `max_depth`.** The limits hold under concurrency. Test in Task 7.

---

### Task 1 (D36): move `agentic.transport` to httpx2

**Files:** Modify `core/pyproject.toml` (replace `httpx` with `httpx2>=2.13.1,<2.14`), `core/src/agentic/transport/{client.py,network.py}` (`import httpx2`), all `core/tests/transport/*.py`, `core/tests/transport/conftest.py`.

**Interfaces:** Produces `make_client` → `httpx2.Client` and `make_async_client` → `httpx2.AsyncClient`; signatures are otherwise unchanged. `_transport` is typed with httpx2 transports.

- [ ] **Step 1:** Change the imports and the dependency (`uv add --package agentic-core "httpx2>=2.13.1,<2.14"`, `uv remove --package agentic-core httpx`).
- [ ] **Step 2:** Add `test_clients_are_httpx2` in `test_client.py`: `assert isinstance(make_client(...), httpx2.Client)` and the same for the async client.
- [ ] **Step 3:** Run `scripts/check.py --all`. Expected: every transport test passes unchanged (S1 showed all 109 pass on httpx2).
- [ ] **Step 4:** Commit `refactor(core): transport on httpx2 (D36)`.

### Task 2 (M1-15, part 1): the metering transport — cost and budget on every call

**Files:** Create `core/src/agentic/agents/__init__.py`, `core/src/agentic/agents/metering.py`; Test `core/tests/agents/test_metering.py` (reuse `core/tests/fixtures/transport/*.json`).

**Interfaces:**
- Consumes: `cost_from_response`, `StreamUsage`, `CallCost`, `BudgetSlice`, `Reservation`, `Pricer`, `estimate_usd` (SP3).
- Produces:
  - `MeteringTransport(inner: httpx2.AsyncBaseTransport, *, budget: BudgetSlice, price_for: Pricer, fallback_estimate_usd: Decimal, on_cost: Callable[[CallCost], None] | None = None)`.
  - `handle_async_request`, in order:
    1. Reserve an estimate. `estimate_request_usd(body, price_for, fallback)` is `estimate_usd(price, tokens_in=len(body_text)//4, tokens_out=max_tokens or 4096)` when the model has a price, otherwise `fallback_estimate_usd`, and never zero. A `LimitHit` here means nothing is sent.
    2. Send through `inner`.
    3. Non-stream: read the body, `cost_from_response` → `reservation.settle(cost)` → return an identical response.
    4. Stream (`content-type: text/event-stream`): return a response whose byte stream tees SSE `data:` events into `StreamUsage` and settles in `aclose()`. A stream closed before it finished settles an `unavailable` cost, which is charged the reservation.
    5. A transport error, or a non-2xx response with no usage, calls `reservation.release()` and re-raises or returns.
  - `CostLog`: the list of `CallCost`, with `total_usd()`, `by_source()`, and `requests` (a count). `on_cost` appends to it. The runner exposes it per run.
  - `LimitHit` raised from `settle` after an overshoot propagates (Pydantic AI surfaces it as `LimitHit`, S1 Q5).

- [ ] **Step 1: Write the failing tests:**
  - `test_a_normal_response_is_settled_at_tokenlean_cost` (openai_nonstream fixture → step spent $0.004321)
  - `test_a_stream_passes_through_unchanged_and_settles_on_close` (bytes out == bytes in; spent == the estimate from the fixture usage)
  - `test_a_stream_closed_early_charges_the_reservation`
  - `test_a_refused_reservation_sends_nothing` (MockTransport records 0 requests)
  - `test_a_transport_error_releases_the_reservation` (handler raises `httpx2.ConnectError`; available unchanged)
  - `test_the_estimate_uses_max_tokens_and_the_price`
  - `test_an_unpriced_model_reserves_the_fallback`
  - `test_overshoot_on_settle_raises_limit_hit_after_recording`
- [ ] **Step 2:** Run. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement `metering.py`.
- [ ] **Step 4:** Run the tests and `check.py --all`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(core): metering transport charges every model call to the budget (M1-15 part 1)`.

### Task 3 (M1-15, part 2): a Pydantic AI model for a resolved agent

**Files:** Modify `core/pyproject.toml` (add `pydantic-ai-slim[openai,anthropic]==2.54.0`); Create `core/src/agentic/agents/models.py`; Test `core/tests/agents/test_models.py`.

**Interfaces:**
- Produces:
  - `GatewaySettings(base_url: str, api_key: str, network: NetworkSettings | None = None)`, where `base_url` is TokenLean's root, e.g. `https://tokenlean.acme.internal`.
  - `build_model(agent: ResolvedAgent, identity: CallIdentity, gateway: GatewaySettings, transport: httpx2.AsyncBaseTransport | None = None) -> pydantic_ai.models.Model`. The model string `"<provider>/<name>"` decides the route: `anthropic/…` → `AnthropicModel(name, provider=AnthropicProvider(base_url=gateway.base_url, api_key=…, http_client=client))`; anything else → `OpenAIChatModel(name, provider=OpenAIProvider(base_url=gateway.base_url + "/v1", api_key=…, http_client=client))`, where `name` keeps TokenLean's routed name (the part after the first `/`). `client` is `make_async_client(...)` over the given transport (the metering transport in production).
  - `model_settings(agent: ResolvedAgent) -> ModelSettings`: `temperature` if set; `effort` → the provider's reasoning-effort setting when the backend honours it (otherwise ignored here; the capability matrix is M1-27).

- [ ] **Step 1: Write the failing tests:**
  - `test_anthropic_models_use_the_messages_route` and `test_other_models_use_the_openai_route`, both with an `httpx2.MockTransport` recording the request path and headers: one `Agent(model).run("hi")` each against the fixture shapes from S1, asserting `/v1/messages` vs `/v1/chat/completions`, `X-Agentic-Run-Id` present, and `Authorization` equal to the gateway key.
  - `test_temperature_is_passed_through`
  - `test_no_banner_and_no_other_host` (a run makes requests only to the gateway host)
- [ ] **Step 2–4:** Run (FAIL), implement, run with `check.py --all` (PASS).
- [ ] **Step 5:** Commit `feat(core): Pydantic AI models routed through TokenLean (M1-15 part 2)`.

### Task 4 (M1-15, part 3): `AgentRunner` and limits

**Files:** Create `core/src/agentic/agents/runner.py`; Test `core/tests/agents/test_runner.py`.

**Interfaces:**
- Produces:
  - `AgentTask(instructions: str, inputs: dict[str, str], prompt: str)`: the agent definition body, the consumed handoffs by kind, and the step's request.
  - `AgentOutcome(status: Literal["completed", "stopped", "paused"], output: str | None, stopped: Literal["budget", "turns", "time", "stuck"] | None, usage: RunUsage, costs: CostLog, pending: list[str], state_path: Path | None)`.
  - `class AgentRunner(Protocol)`: `async def run(self, task: AgentTask) -> AgentOutcome` and `async def resume(self, state_path: Path, decisions: dict[str, bool | str]) -> AgentOutcome`.
  - `PydanticAIRunner(agent: ResolvedAgent, model: Model, *, budget: BudgetSlice, tools: Sequence[Tool] = (), state_dir: Path, clock: Callable[[], float] = time.monotonic, is_child: bool = False)`:
    - **Instructions** = `task.instructions` + each input wrapped as data (`<handoff kind="…">…</handoff>`; full untrusted-content labelling is M1-42).
    - **Turns:** `UsageLimits(request_limit=agent.limits.max_turns)`; `UsageLimitExceeded` → `stopped: turns`.
    - **Time:** `asyncio.wait_for(…, agent.limits.max_minutes * 60)`; a timeout → `stopped: time`, with the output so far when streaming.
    - **Budget:** `LimitHit` with `scope == "child"` on a child runner → `stopped: budget`; on a step or run scope → re-raised for the engine to pause (§8.4).
    - **Stuck:** after each model response, the `StuckDetector` (Task 8) is consulted; stuck → `stopped: stuck`.
    - A deferred tool request → `status: paused` (Task 5).

- [ ] **Step 1: Write the failing tests** (FunctionModel/TestModel, no HTTP):
  - `test_a_run_completes_and_records_usage`
  - `test_handoffs_reach_the_model_as_data_not_chat_history`
  - `test_the_turn_limit_stops_with_turns`
  - `test_the_time_limit_stops_with_time_and_keeps_partial_output` (a fake clock and a slow FunctionModel)
  - `test_a_child_budget_hit_stops_with_budget`
  - `test_a_step_budget_hit_is_raised_for_the_engine`
  - `test_a_runner_satisfies_the_protocol`
- [ ] **Step 2–4:** Run (FAIL), implement, run (PASS).
- [ ] **Step 5:** Commit `feat(core): AgentRunner with turn, time and budget limits (M1-15 part 3)`.

### Task 5 (M1-15, part 4): pause for a human, resume in another process

**Files:** Modify `runner.py`; Test `core/tests/agents/test_resume.py`.

**Interfaces:**
- Produces: on a deferred tool request, the runner writes `state.json` into `state_dir` holding `{agent, agent_version, messages (ModelMessagesTypeAdapter JSON), pending: [tool_call_id], usage, digest}` (written with LF line endings), and returns `status="paused"` with `pending` and `state_path`. `digest` is the sha256 of the canonical JSON of the other fields.
  - `resume(state_path, decisions)` refuses (`ResumeError`), before any model call, when:
    - the digest does not match;
    - the agent name or version differs from this runner's agent;
    - a decision names an unknown id, or a pending id has no decision.

    `True` approves; a string denies with that reason (`ToolDenied`). The saved usage is passed back (`run(usage=…)`), so totals carry across the pause (S8).
  - The saved history is sensitive. It lives only in the git-ignored run folder; the engine records its digest in the ledger, never its content.

- [ ] **Step 1: Write the failing tests:**
  - `test_pause_writes_state_and_runs_nothing` (the tool marked `requires_approval` is not executed)
  - `test_resume_in_a_new_process_approves_and_runs_the_tool` (a `subprocess` running a helper module under `core/tests/agents/`, as in S8)
  - `test_a_denial_reaches_the_model_with_its_reason`
  - `test_usage_carries_across_the_pause`
  - `test_an_edited_state_file_is_refused`
  - `test_another_agent_version_cannot_resume`
  - `test_decisions_must_match_the_pending_calls`
- [ ] **Step 2–4:** Run (FAIL), implement, run (PASS).
- [ ] **Step 5:** Commit `feat(core): pause at a deferred tool and resume in another process (M1-15 part 4)`, mark M1-15 Done.

### Task 6 (M1-16): agent tools

**Files:** Create `core/src/agentic/agents/tools/{__init__,files,shell,codeintel,skills}.py`; Test `core/tests/agents/test_tools.py`.

**Interfaces:**
- Consumes: `sdlc_commons.guards.Guard` (`writes`, `command`), `RunWorktrees` trees.
- Produces:
  - `ToolContext(tree: Path, guard: Guard, sandbox: Sandbox, codeintel: CodeIntel | None, skills: SkillCatalog | None)`.
  - `build_tools(agent: ResolvedAgent, ctx: ToolContext) -> list[pydantic_ai.Tool]`: **only the tools named in `agent.tools`** are built. An unknown name in `agent.tools` raises `ToolConfigError` (policy should already prevent it).
  - **File tools** (`files.py`):
    - `read_file(path)` and `list_dir(path)`: inside `ctx.tree` only; refused for `deny_paths`, framework-protected and Zyroflo-protected paths (reading a secret leaks it to the model). Output is capped at 200 KB with a truncation note.
    - `search(pattern, glob="**/*")`: a Python `re` over files in the tree, skipping denied paths, binaries and `.git`, capped at 200 matches.
    - `edit_file(path, old, new)` (exact single replacement) and `write_file(path, text)`: every path checked by `guard.writes`. A refusal is returned to the model as `ModelRetry` with the guard's reason; nothing is written.
  - **Shell** (`shell.py`):
    - `run_shell(command)`: refused by `guard.command` blocks. Otherwise run through `ctx.sandbox.run(command, cwd=ctx.tree, timeout_s)`. Returns exit code, stdout and stderr (each capped at 50 KB).
    - `class Sandbox(Protocol)`: `isolated: bool`, `run(command, cwd, timeout_s) -> ShellResult`. SP4 ships `LocalProcessSandbox` (`isolated = False`: a subprocess with no OS isolation, for tests and development). The real sandboxes are M1-32/M1-33.
    - `build_tools` refuses `run_shell` with a non-isolated sandbox unless `ToolContext.allow_unisolated=True` (default False), so production cannot fall back to no isolation by accident.
  - **Codeintel** (`codeintel.py`): `class CodeIntel(Protocol)` with `dependents`, `impact`, `tests_for`. Tools wrap it; the real adapter is M1-34; tests use a fake.
  - **Skills** (`skills.py`): `class SkillCatalog(Protocol)` with `get(name) -> str`. `activate_skill(name)` returns the skill text only if `name in agent.skills`. The real catalog is M1-37/M1-38.

- [ ] **Step 1: Write the failing tests:**
  - `test_only_allowed_tools_are_offered_to_the_model` (TestModel's `info.function_tools` names == `agent.tools`)
  - `test_a_call_to_a_tool_not_offered_is_not_run` (FunctionModel calls `run_shell` when it is not allowed → retry prompt, nothing run)
  - `test_read_refuses_paths_outside_the_tree`, parametrized over `../x`, an absolute path, a junction or symlink out (skipped where the OS cannot create one), and a denied glob
  - `test_read_refuses_secrets` (`.env`)
  - `test_search_skips_denied_paths_and_binaries`
  - `test_edit_replaces_exactly_once_or_refuses`
  - `test_write_refused_by_the_guard_writes_nothing`
  - `test_shell_refused_by_the_guard_never_runs` (`git push origin main`)
  - `test_shell_runs_in_the_tree_with_a_timeout`
  - `test_shell_needs_an_isolated_sandbox_unless_allowed`
  - `test_codeintel_tools_call_the_adapter`
  - `test_activate_skill_only_for_the_agents_skills`
- [ ] **Step 2–4:** Run (FAIL), implement, run (PASS on Windows and Linux).
- [ ] **Step 5:** Commit `feat(core): guarded agent tools, offered only when allowed (M1-16)`, mark M1-16 Done.

### Task 7 (M1-17): child spawner

**Files:** Create `core/src/agentic/agents/children.py`; Test `core/tests/agents/test_children.py`.

**Interfaces:**
- Produces:
  - `ChildSpec(name: str, instructions: str, prompt: str, tools: list[str] | None, limits: Limits | None)`.
  - `ChildSpawner(parent: ResolvedAgent, parent_budget: BudgetSlice, runner_factory: Callable[[ResolvedAgent, BudgetSlice, int], AgentRunner], depth: int = 0)` with `async spawn(spec) -> ChildResult`.
  - `ChildResult(output: str | None, stopped: Literal["budget", "turns", "time", "stuck", "refused"] | None, cost_usd: Decimal)`.
  - Limits come from the parent's resolved `children` (`max_total`, `max_concurrent`, `max_depth`, `per_child`):
    - a count, concurrency or depth over its limit → `stopped: refused` with the reason (not an exception, so the parent decides);
    - concurrency uses an `asyncio.Semaphore`;
    - the count is checked and incremented under a lock.
  - **Narrowing:**
    - The child's tools are `spec.tools` ⊆ the parent's tools; a wider request → `refused`.
    - The child's deny lists are the parent's ∪ its own.
    - The child's limits are min(spec, `per_child`).
    - The budget is `parent_budget.open(name, "child", per_child.budget_usd)`, closed after the child ends.
  - The child's partial result carries `stopped` (§8.4).
  - `spawn_agent` tool: offered only if `spawn_agent` is in the parent's tools and `max_total > 0`.

- [ ] **Step 1: Write the failing tests:**
  - one per limit: `test_max_total_is_enforced`, `test_max_concurrent_is_enforced` (8 concurrent spawns with `max_concurrent=2` never run more than 2 at once), `test_max_depth_is_enforced`
  - `test_per_child_budget_turns_and_time_slice_the_parent`
  - `test_a_child_cannot_widen_its_tools`
  - `test_child_deny_is_the_union_of_parent_and_own`
  - `test_a_child_limit_hit_returns_a_partial_result_marked_stopped`
  - `test_concurrent_spawns_cannot_exceed_max_total` (Review Focus 5)
  - `test_spawn_agent_offered_only_when_allowed`
- [ ] **Step 2–4:** Run (FAIL), implement, run (PASS).
- [ ] **Step 5:** Commit `feat(core): child spawner under admin limits, narrowing only (M1-17)`, mark M1-17 Done.

### Task 8 (M1-18): stuck detector

**Files:** Create `core/src/agentic/agents/stuck.py` (header: "Port of OpenHands software-agent-sdk `openhands/sdk/conversation/stuck_detector.py` @ c8a65f0 (MIT, © OpenHands contributors)"); add the MIT notice to `THIRD_PARTY_NOTICES.md` (create it); Test `core/tests/agents/test_stuck.py`.

**Interfaces:**
- Produces:
  - `Event`: one of `Action(tool, args_digest)`, `Observation(tool, content_digest, error: bool)`, `Message(text_digest)`.
  - `events_from_messages(messages: list[ModelMessage]) -> list[Event]`: `ToolCallPart` → Action; `ToolReturnPart` → Observation; `RetryPromptPart` → Observation(error=True); `TextPart` in a response without tool calls → Message.
  - `StuckThresholds(action_observation: int = 4, action_error: int = 3, monologue: int = 3, alternating: int = 6)`, the OpenHands defaults.
  - `StuckDetector(thresholds)` with `check(events) -> StuckVerdict | None`. A verdict carries `pattern: Literal["repeat", "error_loop", "monologue", "ping_pong"]` and `detail`. Only events since the last human/user input are scanned, at most 20.
  - `oscillation(previous: list[str], current: str, window: int = 2) -> bool`: the pipeline-level check that a loop's review findings or developer outputs repeat across iterations (the same normalised digest seen within `window` iterations), used by the engine's loop in M1-24.

- [ ] **Step 1: Write the failing tests:** one per pattern (`test_repeated_identical_actions_are_stuck`, `test_repeated_errors_are_stuck`, `test_ping_pong_is_stuck`, `test_monologue_is_stuck`, `test_oscillation_across_loop_iterations`), plus `test_progress_is_not_stuck` and `test_a_user_message_resets_the_window`.
- [ ] **Step 2–4:** Run (FAIL), implement, run (PASS).
- [ ] **Step 5:** Commit `feat(core): stuck detector (OpenHands port) and loop oscillation check (M1-18)`, mark M1-18 Done.

### Task 9: SP4 review, call flow and tracker

- [ ] **Step 1:** Run the Sonnet 5.5 high-effort review of SP4 in its own worktree, with this plan, the spikes and the Review Focus. The reviewer probes only with Python scripts, and keeps test commands and shell strings as data, never run through a shell.
- [ ] **Step 2:** Fix Critical/Important findings with tests first; ledger the rulings and deferred minors.
- [ ] **Step 3:** Update `docs/architecture/call-flow.md` (the agent loop, tools, children and stuck boxes) and the tracker.
