# SP7 Backends Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An agent step runs on the backend its policy chose: `native` (Pydantic AI) or `claude-code` (Claude Code through the Claude Agent SDK, locked down per spike S2). A capability matrix decides, per backend and model, which resolved settings are honoured; org policy says whether an unhonoured one is ignored, warned about, or blocks the run.

**Architecture:**
- **New package `agentic.backends`:**
  - `capabilities.py`: what each backend honours, and the policy check;
  - `base.py`: the `Backend` protocol;
  - `native.py`: today's Pydantic AI runner wiring, moved out of the engine factory;
  - `claude_code.py`: the Claude Code runner. It imports `claude_agent_sdk` lazily, so core works without the optional extra.
- **Engine factory.** It becomes `StepRunners`, which dispatches each step to the backend named on its resolved agent.
- **Preflight.** It reports capability problems before any spend.

**Tech Stack:** Python 3.12, Pydantic AI 2.54.0 (`ModelSettings.thinking`, `TestModel`), `claude-agent-sdk` 0.2.164 (optional extra; bundles the Anthropic-signed Claude Code 2.1.292), pytest.

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md` (§9.1 Backends, D2 TokenLean-only, D38 sandbox), spike records `docs/spikes/S2-claude-code-through-tokenlean.md` and `S1-cost-capture-pydantic-ai.md`, master plan rows M1-27..M1-29.

## Global Constraints

- **TokenLean only.** Every model call goes through TokenLean (D2): the claude-code backend sets `ANTHROPIC_BASE_URL` to TokenLean and nothing else.
- **Identity headers.** `X-Agentic-*` plus `traceparent`, never `X-Step-Name` / `X-Workflow-ID` / `X-Session-ID`; claude-code carries them via `ANTHROPIC_CUSTOM_HEADERS`.
- **The SDK is an optional extra.** `claude-agent-sdk==0.2.164` is the extra `claude-code` of `agentic-core`, never a core dependency: its use is under Anthropic's Commercial Terms and it bundles a proprietary binary (S2). The dev group installs it so the tests run.
- **The claude-code lock-down from S2 is mandatory and fully tested.** Every one of these on every run:
  - **Options:**
    - `setting_sources=[]` (explicit)
    - `extra_args={"bare": None, "no-session-persistence": None}`
    - `tools=` the mapped tool names, and `allowed_tools=` the same list
    - `permission_mode="dontAsk"`
    - `settings=` the JSON of the guard's Claude Code permissions
    - `model=` the resolved model
  - **Environment:**
    - `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY` and `ANTHROPIC_CUSTOM_HEADERS`
    - `ANTHROPIC_DEFAULT_HAIKU_MODEL` and `ANTHROPIC_SMALL_FAST_MODEL` set to the resolved model
    - `CLAUDE_CONFIG_DIR` set to a per-step folder
    - `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1`, `DISABLE_ERROR_REPORTING=1`, `DISABLE_AUTOUPDATER=1`
- **Cost.** Claude Code's `total_cost_usd` is never charged: the step is charged from `ResultMessage.usage` priced by our `Pricer`, `source="estimated"`.
- **Package boundaries.** `core` never imports `sdlc_commons`. The guard's permissions arrive as a plain `dict` (wiring passes `Guard.claude_permissions()`).
- **Formatting, CI and records** as before:
  - never add `# fmt: skip`; line length 100;
  - `uv run python scripts/check.py --all` must be green on Windows and Linux;
  - deferred items go into DEFERRED.md and TRACKER.md in the completing commit.

## Review Focus

1. **A Claude Code run whose user has their own `~/.claude` settings, hooks or MCP servers.** None of them may load. Test: Task 4, `test_user_settings_never_load` (a hook and a `CLAUDE.md` planted in the step's config/work folders must not reach the stub).
2. **An answer that is not a valid handoff.** Errors go back once in the same session; a second failure ends `invalid` with the text kept, exactly like native. Test: Task 4, `test_an_invalid_answer_is_retried_once_then_invalid`.
3. **Usage that pushes the step over budget.** A `LimitHit` must reach the engine (it pauses the run), never be swallowed. Test: Task 4, `test_usage_over_the_slice_raises_limit_hit`.
4. **An agent whose tools or settings a backend cannot honour** (`spawn_agent`/`codeintel` on claude-code; temperature on a reasoning model). Preflight names the setting and the backend before any spend. Tests: Task 1, `test_capability_problems_reach_preflight`.
5. **A hung Claude Code process at the step's time limit.** The run stops `time` with the partial text, and the process is gone. Test: Task 4, `test_the_time_limit_stops_the_process`.

---

### Task 1: Capability matrix and the unhonoured-settings policy (M1-27, DF-01, DF-25)

**Files:**
- Create: `core/src/agentic/backends/__init__.py`, `core/src/agentic/backends/capabilities.py`
- Modify: `core/src/agentic/policy/model.py` (`OrgPolicy.unhonoured`), `core/src/agentic/policy/load.py` (`org.yaml` key `unhonoured`)
- Modify: `core/src/agentic/engine/preflight.py` (use the matrix; drop the hard-coded `AVAILABLE_BACKENDS` message)
- Test: `core/tests/backends/test_capabilities.py`; additions to `core/tests/engine/test_preflight.py` and `core/tests/policy/test_resolve.py`

**Interfaces:**
- Produces:
  - `Action = Literal["ignore", "warn", "block"]`.
  - `OrgPolicy.unhonoured: dict[Literal["temperature", "effort"], Action] = {"temperature": "warn", "effort": "warn"}`.
  - `@dataclass(frozen=True) BackendCapabilities`:
    - `name: str`
    - `tools: Mapping[str, tuple[str, ...]]`: our capability → that backend's tool names; a capability missing from the map is unavailable there
    - `temperature: bool`
    - `effort: bool`
    - `approvals: bool` (can pause for a person's tool approval)
    - `children: bool`
    - `stuck_check: bool`
  - `CAPABILITIES: dict[str, BackendCapabilities]`:
    - **native:** every tool capability (identity map, `spawn_agent` included); temperature, effort, approvals, children and stuck_check are all true.
    - **claude-code:** tools `{"read_files": ("Read",), "search": ("Grep", "Glob"), "edit_files": ("Edit", "Write"), "run_shell": ("Bash",)}`; temperature false; effort true; approvals false; children false; stuck_check false.
  - `REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")`: model names Pydantic AI sends no temperature to (DF-25).
  - `@dataclass(frozen=True) Issue(setting: str, action: Action | Literal["block"], detail: str)`.
  - `backend_issues(agent: ResolvedAgent, policy: OrgPolicy, *, available: Collection[str]) -> list[Issue]`:
    - a backend not in `available` is a block (`setting="backend"`);
    - each tool capability the backend lacks is a block (`setting="tools"`);
    - `ask_shell` entries on a backend without approvals give `setting="ask"`, action `warn`, detail "ask-listed commands are refused (no approval pause on <backend>)";
    - temperature set, but the backend lacks it or the model's name (after any `provider/`) starts with a reasoning prefix, gives `policy.unhonoured["temperature"]`;
    - effort set on a backend without it gives `policy.unhonoured["effort"]`;
    - `ignore` produces no issue.
- Preflight turns a block into an error `Problem(step, "backend", detail, policy_value)` and a warn into a warning Problem.

- [ ] **Step 1: Write the failing tests.**

```python
def test_temperature_on_claude_code_is_blocked_when_policy_says_block(resolved, policy):
    policy.unhonoured["temperature"] = "block"
    agent = resolved.model_copy(update={"backend": "claude-code", "temperature": 0.2,
                                        "tools": ["read_files"]})
    assert [(i.setting, i.action) for i in backend_issues(agent, policy, available={"native", "claude-code"})] == [("temperature", "block")]

def test_temperature_on_claude_code_warns_by_default(...): ...            # action "warn"
def test_ignore_reports_nothing(...): ...
def test_temperature_on_a_reasoning_model_is_unhonoured_on_native(...):   # DF-25
    # model "openai/gpt-5-mini", temperature 0.2 -> ("temperature", "warn")
def test_a_tool_the_backend_lacks_blocks(...):                            # spawn_agent on claude-code
def test_an_unavailable_backend_blocks(...):                              # claude-code not in available
def test_ask_rules_warn_on_a_backend_without_approvals(...):
def test_capability_problems_reach_preflight(check, ...):                 # Review Focus 4: error + warning
def test_an_explicit_model_does_not_inherit_tier_effort_or_temperature(policy, developer):  # DF-01
    # tier spec carries effort "high" and temperature 0.3; agent names a different allowed
    # model explicitly -> resolved.effort is None and resolved.temperature is None
def test_unhonoured_policy_loads_from_org_yaml(...): ...                  # org.yaml unhonoured: {effort: ignore}
```

- [ ] **Step 2: Run** `uv run pytest core/tests/backends core/tests/engine/test_preflight.py core/tests/policy -q`. Expected: the new tests FAIL (no module `agentic.backends`).

- [ ] **Step 3: Implement** as specified.
  - Preflight takes `available_backends: Collection[str] | None = None`, defaulting to `agentic.backends.available()`.
  - `available()` is `{"native"}`, plus `"claude-code"` when `importlib.util.find_spec("claude_agent_sdk")` finds it.

- [ ] **Step 4: Run** the same command. Expected: PASS. Then run `uv run pytest -q`: all PASS.

- [ ] **Step 5: Commit.** `feat(core): backend capability matrix and unhonoured-settings policy (M1-27, DF-01, DF-25)`. Mark DF-01 and DF-25 Done, and M1-27 Done.

### Task 2: Backend protocol and the native backend (M1-28)

**Files:**
- Create: `core/src/agentic/backends/base.py`, `core/src/agentic/backends/native.py`
- Modify: `core/src/agentic/engine/factory.py` (`PydanticStepRunners` → `StepRunners`), `core/src/agentic/agents/models.py` (`model_settings` maps effort)
- Modify: `core/tests/engine/test_factory.py` (new names)
- Test: `core/tests/backends/conformance.py` (a shared suite), `core/tests/backends/test_native.py`

**Interfaces:**
- Produces:
  - `class Backend(Protocol)`:
    - `name: str`
    - `capabilities: BackendCapabilities`
    - `def runner(self, step: StepContext, *, tool_context: ToolContext | None, state_dir: Path, spawner: ChildSpawner | None) -> AgentRunner`
  - `class NativeBackend`: holds what `PydanticStepRunners` held (`identity_for`, `gateway`, `prices`, `fallback_estimate_usd`, `model_for`, `policy_version`). `runner(...)` builds today's `PydanticAIRunner`, with the same metering, tools and contract.
  - `class StepRunners(backends: Mapping[str, Backend], *, tools_for, state_dir_for)`:
    - `__call__(step) -> AgentRunner` dispatches on `step.agent.backend`;
    - `root_spawner` and `child_runner` work as today, only when the backend has children;
    - an unknown backend raises `EngineError` naming it.
  - `model_settings(agent)` also sets `settings["thinking"] = agent.effort` when the effort is set.
  - `conformance.py`: `def conformance_cases(make_runner) -> list[Callable]`, asserting:
    - a valid handoff completes;
    - a twice-invalid answer ends `invalid`;
    - a budget `LimitHit` propagates;
    - a time limit gives `stopped` with `time`;
    - usage is reported.
  - Each backend's test module runs every case.

- [ ] **Step 1: Write the failing tests.** `test_native.py` runs every conformance case against `NativeBackend` with a `TestModel` / `FunctionModel`. Also:
  - `test_step_runners_dispatch_on_the_agents_backend` (a fake backend records the call);
  - `test_an_unknown_backend_is_an_engine_error`;
  - `test_effort_reaches_model_settings_as_thinking`.
- [ ] **Step 2: Run** `uv run pytest core/tests/backends core/tests/engine/test_factory.py -q`. Expected: FAIL (no `agentic.backends.native`).
- [ ] **Step 3: Implement.** Move the code; behaviour unchanged except the dispatch and the effort mapping.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all PASS.
- [ ] **Step 5: Commit.** `feat(core): backend protocol, native backend and step-runner dispatch (M1-28)`. Mark M1-28 Done.

### Task 3: Claude Code options builder and test stub (M1-29, part 1)

**Files:**
- Modify: `core/pyproject.toml` (`[project.optional-dependencies] claude-code = ["claude-agent-sdk==0.2.164"]`), root `pyproject.toml` (dev group gains `agentic-core[claude-code]`); then `uv lock`
- Create: `core/src/agentic/backends/claude_code.py` (`lock_down`), `core/tests/backends/anthropic_stub.py` (the S2 stub and refusing proxy, cleaned up as a test helper)
- Test: `core/tests/backends/test_claude_code_options.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) ClaudeCodeSettings`:
    - `gateway_url: str`
    - `api_key: str`
    - `config_root: Path` (per-step `CLAUDE_CONFIG_DIR` folders live here)
    - `cli_path: Path | None = None` (None: the SDK's bundled binary)
  - `lock_down(step: StepContext, *, settings: ClaudeCodeSettings, identity: CallIdentity, permissions: dict, cwd: Path, edit_tree: bool) -> dict`. It returns the `ClaudeAgentOptions` keyword arguments, exactly the Global Constraints list:
    - `tools` and `allowed_tools` from `CAPABILITIES["claude-code"].tools` for the agent's capabilities;
    - `settings = json.dumps(permissions)`, with `"Edit(./**)"` added to `permissions.allow` when `edit_tree`;
    - `max_turns` from `agent.limits.max_turns`;
    - `effort` from `agent.effort`;
    - `max_budget_usd` = `float(step.budget.available())` when the slice has a limit.
  - It must not import `claude_agent_sdk`: it returns plain data.

- [ ] **Step 1: Write the failing tests.** Pure data, so no CLI is needed.

```python
def test_every_lock_down_setting_is_present(...):
    kw = lock_down(step, settings=S, identity=ID, permissions=PERMS, cwd=tree, edit_tree=True)
    assert kw["setting_sources"] == []
    assert kw["extra_args"] == {"bare": None, "no-session-persistence": None}
    assert kw["permission_mode"] == "dontAsk"
    assert kw["tools"] == kw["allowed_tools"] == ["Read", "Grep", "Glob", "Edit", "Write", "Bash"]
    env = kw["env"]
    assert env["ANTHROPIC_BASE_URL"] == "https://tokenlean.acme.internal"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == env["ANTHROPIC_SMALL_FAST_MODEL"] == kw["model"]
    assert {env[k] for k in ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_TELEMETRY",
            "DISABLE_ERROR_REPORTING", "DISABLE_AUTOUPDATER")} == {"1"}
    assert "X-Agentic-Run-Id: r-1" in env["ANTHROPIC_CUSTOM_HEADERS"].splitlines()
    assert "Edit(./**)" in json.loads(kw["settings"])["permissions"]["allow"]

def test_tools_follow_the_agents_capabilities(...): ...   # read_files only -> ["Read"]
def test_forbidden_identity_headers_never_appear(...): ... # none of X-Step-Name/X-Workflow-ID/X-Session-ID
def test_each_step_gets_its_own_config_dir(...): ...
def test_budget_slice_becomes_max_budget_usd(...): ...
```

- [ ] **Step 2: Run** `uv run pytest core/tests/backends/test_claude_code_options.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `lock_down`. Move the stub into `anthropic_stub.py` (scripted answers, request log, refusing proxy).
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all PASS.
- [ ] **Step 5: Commit.** `feat(core): claude-code lock-down options and the TokenLean test stub (M1-29)`.

### Task 4: Claude Code runner and contract tests (M1-29, part 2, DF-65)

**Files:**
- Modify: `core/src/agentic/backends/claude_code.py` (`ClaudeCodeRunner`, `ClaudeCodeBackend`)
- Modify: `core/src/agentic/sandbox/srt.py` (`SrtSandbox.command_prefix() -> list[str]`, used to wrap the Claude Code process)
- Test: `core/tests/backends/test_claude_code_contract.py` (real bundled Claude Code against the stub, on Windows and Linux); `core/tests/backends/test_claude_code_live_srt.py` (Linux with srt only)

**Interfaces:**
- Produces:
  - `class ClaudeCodeRunner` (an `AgentRunner`):
    - `__init__(agent, *, options: dict, budget: BudgetSlice, price_for: Pricer, contract: OutputContract | None, time_limit_s: float | None, wrap: Sequence[str] | None = None)`.
    - **`run(task)`:**
      - opens one `ClaudeSDKClient` session;
      - sends the same instructions/inputs/prompt text `PydanticAIRunner` builds (reuse `_instructions`);
      - reads until the `ResultMessage` and takes `result.result` as the answer;
      - with a contract, a failed `check` sends `retry_message(errors)` once in the same session, and a second failure returns `invalid` with the text and errors;
      - charges `budget.reserve(estimate).settle(CallCost(source="estimated", ...))` from the result's usage priced by `price_for`, after each result;
      - a `LimitHit` propagates;
      - `asyncio.wait_for` enforces `time_limit_s`; on timeout it disconnects (the process ends) and returns `stopped`/`time` with the last text.
    - **`resume(...)`** raises `ResumeError("claude-code steps never pause for approval")`.
    - **`wrap`:** a command prefix (srt). The runner writes an executable wrapper script in the step's config folder that runs `<wrap...> <claude> "$@"` and passes it as `cli_path`. This is POSIX only: on Windows `wrap` must be None.
  - `class ClaudeCodeBackend(settings: ClaudeCodeSettings, *, identity_for, prices, permissions_for: Callable[[StepContext], dict], wrap_for: Callable[[StepContext], Sequence[str] | None])`: its `runner(...)` builds `lock_down(...)` and a `ClaudeCodeRunner`.
  - `SrtSandbox.command_prefix() -> list[str]`: the settings file (written) plus `["srt", "--settings", <file>, "--", "env", TMPDIR..., ]`, without `bash -c`.

- [ ] **Step 1: Write the failing contract tests.** Every test drives the pinned bundled CLI against `anthropic_stub` with the refusing proxy set.

```python
def test_every_call_reaches_tokenlean_with_identity(...)          # all requests at the stub; headers; model forced
def test_nothing_else_leaves_the_machine(...)                     # proxy.attempts == []
def test_user_settings_never_load(...)                            # Review Focus 1: planted CLAUDE.md marker absent;
                                                                  # offered tools == mapped tools exactly
def test_a_denied_command_is_refused_and_an_allowed_one_runs(...) # Guard permissions dict; git push refused
def test_a_valid_handoff_completes(...)                           # stub answers a valid design handoff
def test_an_invalid_answer_is_retried_once_then_invalid(...)      # Review Focus 2: 2 answers; errors in 2nd prompt
def test_usage_is_charged_from_usage_not_claude_codes_cost(...)   # slice spent == Pricer(usage), source estimated
def test_usage_over_the_slice_raises_limit_hit(...)               # Review Focus 3
def test_the_time_limit_stops_the_process(...)                    # Review Focus 5: stub delays; stopped/time
def test_temperature_is_never_sent(...)
def test_resume_is_refused(...)
```

`test_claude_code_live_srt.py` (skipped unless Linux with srt): the same valid-handoff run with `wrap=SrtSandbox(...).command_prefix()`, where the rules allow only the stub host. It completes, and a Bash call writing outside the tree is blocked (DF-65).

- [ ] **Step 2: Run** `uv run pytest core/tests/backends -q`. Expected: the contract tests FAIL (no `ClaudeCodeRunner`).
- [ ] **Step 3: Implement** as specified.
- [ ] **Step 4: Run** `uv run python scripts/check.py --all`. Expected: green. On Linux the contract tests and the live srt test run, not skip.
- [ ] **Step 5: Commit.** `feat(core): claude-code backend - locked-down Claude Code through TokenLean (M1-29, DF-65)`. Mark DF-65 Done and M1-29 Done. Add DEFERRED rows:
  - `init`'s readiness reports Git Bash for claude-code on Windows → M1-53;
  - wiring builds `ClaudeCodeBackend` with `Guard.claude_permissions()` and the srt prefix → M1-54 (with DF-47);
  - one live smoke run through a real TokenLean → M1-68.
