# M2-60 Agent-CLI Backends in the CLI Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `agentic run` runs a pipeline step on `claude-code`, `codex` or `opencode` as well as `native` (decision 89). The CLI builds each agent-CLI backend locked down: the guard's shell policy and deny lists, its process wrap (`StepWiring.process_wrap_for`: srt, TokenLean plus the policy's registries as the only hosts, M2-42), the run's identity headers to TokenLean, the kill switch at each turn the backend reports (decision 89: a pause holds at the next step boundary, a kill cancels at once). Preflight refuses, before any spend, a step whose backend cannot run here and says exactly what to install. No traceback. Every agent-CLI step ends with a protected-path check of what it wrote.

**Architecture:**
- **Core (provider-agnostic engine untouched; vendor code stays in its backend module):**
  - `agentic.backends.turns.TurnCheck`: one helper that asks the step's `turn_guard` at a turn boundary. A kill raises `RunKilled` (the engine closes the run `killed`). A pause is noted only: these runners cannot save a conversation, so the engine holds the run at the next step boundary.
  - `ClaudeCodeRunner`, `CodexRunner` and `AcpRunner` call it at every turn boundary their protocol reports: before each prompt or exec, at each new model message (claude-code), at each tool call event (codex `item.completed`, ACP `tool_call`). The ACP check cancels the session task, because a notification callback cannot raise into the runner.
  - `backend_issues(..., honoured=...)` and `preflight(..., honoured=...)`: per backend, the settings the wiring makes good, for example `shell_default_deny` on codex with the guard hook (DF-426).
  - `pass_env` in `ClaudeCodeSettings`, `CodexSettings` and `AcpSettings`. Org policy's `sandbox.pass_env` then reaches the agent CLI and its shell commands (DF-541). Provider-routing variables are always blanked.
  - OpenCode's plugin pre-scan parses JSONC and refuses only a non-empty `plugin`/`plugins` (DF-446). ACP teardown retries removing the step home and warns if that fails; the stderr tail it quotes is redacted (DF-448).
- **Commons:** `StepCheckpoints.changed()`: the paths each run tree changed since its HEAD (tracked and untracked, not ignored), for the post-step check.
- **CLI:**
  - New module `agentic_cli/agent_backends.py`. It holds the install check for each backend (extra, binary on PATH, pinned version, native-Windows refusal), the builders (`build(rt, gateway, wiring)`), the preflight problems, and the post-step `ProtectedWrites` runner wrapper.
  - `runtime.CONFIGURED_BACKENDS` becomes all four backends. `Runtime.engine` adds the agent-CLI backends to `StepRunners`. `Runtime.preflight` adds the agent-backend problems.
  - `StepWiring.process_wrap_for(step, write=..., deny=...)`: extra write denials, for codex's CODEX_HOME config, rules, catalog and schema (DF-425). `StepWiring.denied_writes(step)` runs the post-step check.
  - User config `agent_clis: {codex: <path or argv>, opencode: <path>, claude-code: <path>}` overrides where each agent CLI is found (on Windows, opencode needs the full path to its signed `.exe`: an npm `.cmd` shim is refused).

**Tech Stack:** Python 3.12. No new dependencies: `claude-agent-sdk` (`claude-code` extra), `tomli-w` (`codex` extra) and `agent-client-protocol` (`acp` extra) are already pinned. pytest.

**Spec:** §9.1 (backends), §9.2 (sandbox, egress), §8.6 (kill switch), §8.2 (preflight). Decisions 22 (no unsandboxed codex), 73 (no live model or vendor-CLI calls in tests), 89 (batch-8 defaults: CLI backends hold at the step boundary). Spikes S2, S3, S7.

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| Driving each agent CLI | the existing core backends (Claude Agent SDK, `codex exec`, ACP SDK) | nothing new is needed |
| srt rules and the egress list | `StepWiring.process_wrap_for`, `agentic.sandbox.egress` (M2-42) | — |
| Shell policy per backend | `Guard.claude_permissions`, `Guard.shell_rules`, `sdlc_commons.codex_hook` (Zyroflo through commons) | — |
| Changed paths after a step | `git status --porcelain -z` through `sdlc_commons.worktrees` (Zyroflo's git helper) | a file-system snapshot: slower, and blind to what git commits |
| JSONC for the OpenCode plugin pre-scan | a 30-line comment and trailing-comma stripper | `json5` (Apache-2.0) / `commentjson` (MIT, pulls in `lark`): a new dependency of the `acp` extra for one pre-scan; the stripper is exact for JSONC |
| Fakes for tests | the existing scripted stand-ins: `anthropic_stub.TokenLeanStub` + `RefusingProxy` with the bundled Claude Code binary, `fake_codex.py`, `acp_fake_agent.py` | — |

## Global Constraints

- **Provider-agnostic.** The engine, policy model, protocols and CLI flags name no vendor. Backend names (`claude-code`, `codex`, `opencode`) are data. Vendor-specific code stays in `agentic.backends.<backend>`. The CLI's `agent_backends.py` names backends only to pick their builder and install hint.
- **Sandbox per backend (safe default, listed under needs_decision):**
  - `codex` always runs inside srt (decision 22): preflight refuses a codex step on a machine or regime without it.
  - `claude-code` and `opencode` follow the regime's sandbox decision (D38). With srt, the whole process is wrapped. Where the regime lets steps run on guards only, they run unwrapped under their compiled permission rules, with the same preflight warning a native step gets.
- **Missing extras or binaries** are preflight errors with an exact install command, never a traceback. The check runs only for backends a step names.
- **No live model or vendor-CLI calls** (decision 73). E2E tests run a pipeline step through each backend's scripted fake. The Claude Code binary bundled in the pinned SDK wheel runs only against the local TokenLean stub, with every other host refused.
- **Windows and macOS.** Paths are built with `pathlib`. The fake srt used in tests is a Python script (no `env` binary on Windows). Codex is refused on native Windows (unsigned `rg.exe`); its E2E test patches that refusal to run the fake.
- **Formatting, CI and records:**
  - never add `# fmt: skip` or `# fmt: off`; line length 100;
  - check pytest's exit code directly;
  - `uv run python scripts/check.py --all` must be green on Windows and Linux;
  - TRACKER.md, DEFERRED.md and DECISIONS are updated by the coordinator from this track's output.

## Review Focus

1. **Fail closed.** A codex step never runs without srt. An agent-CLI process under srt reaches only TokenLean and the policy's registries, and writes only its tree, its per-step home and the policy's caches. Codex's own config, rules, catalog and the guard hook spec are not writable by its shell commands. Tests: Task 6 `test_codex_wrap_denies_its_own_config`, `test_each_backend_wrap_allows_only_the_gateway_and_registries`; Task 9 `test_a_live_process_wrap_reaches_only_the_gateway` (Linux container).
2. **Preflight before spend.** A step on a backend whose extra or CLI is missing fails preflight with the install command. A codex step without srt fails too. An opencode step whose repo carries OpenCode plugins fails as well. Nothing is spent and nothing prints a traceback. Tests: Task 7.
3. **Kill switch.** A kill at a turn boundary stops each agent-CLI backend at once and leaves no process behind. A pause lets the step finish and holds the run before the next step. The engine watchdog still kills a step blocked between turns. Tests: Task 1 per backend; Task 8 `test_a_kill_at_a_turn_stops_the_run_killed`, `test_a_pause_holds_at_the_next_step`.
4. **Protected writes.** An agent-CLI step that wrote a protected or denied path (for example `.github/workflows/x.yml`, or `infra/**` from the agent's deny list) fails with the paths named. Its work is never checkpointed. Test: Task 5.
5. **Identity and lock-down reach TokenLean.** Each backend's model calls carry the run's `X-Agentic-*` headers. claude-code gets `Guard.claude_permissions()`, and opencode gets `Guard.shell_rules()` with the framework floor. codex gets the guard's deny rules, including `FRAMEWORK_DENY_SHELL`, plus the hook. Tests: Task 6, Task 8.

---

### Task 1: Turn-level kill-switch hook for the agent-CLI runners (decision 89)

**Files:**
- Create: `core/src/agentic/backends/turns.py`
- Modify: `core/src/agentic/backends/claude_code.py`, `core/src/agentic/backends/codex.py`, `core/src/agentic/backends/acp/runner.py`
- Test: `core/tests/backends/test_turn_kill.py`

**Interfaces:**
- `class TurnCheck(guard: Callable[[], Awaitable[Verdict]] | None)`:
  - `async __call__() -> None`;
  - `held: Verdict | None` is the last pause seen.
- Each runner takes `turn_guard=None`, and each backend passes `step.turn_guard`.

- [ ] **Step 1: Write the failing tests.** Codex (fake): a kill verdict at the first tool call raises `RunKilled` and the fake's child is gone. A pause lets the step complete. ACP (fake agent): the same, at a `tool_call` and before the retry prompt. claude-code (bundled binary + stub; skipped without the extra): a kill at the second model message raises `RunKilled`.
- [ ] **Step 2: Run** `uv run pytest core/tests/backends/test_turn_kill.py`. Expected: FAIL (no `agentic.backends.turns`).
- [ ] **Step 3: Implement** `TurnCheck` and the calls in each runner.
- [ ] **Step 4: Run** the backends tests. Expected: PASS.
- [ ] **Step 5: Commit.**

### Task 2: Wired settings in the capability matrix (DF-426)

**Files:**
- Modify: `core/src/agentic/backends/capabilities.py`, `core/src/agentic/engine/preflight.py`
- Test: `core/tests/backends/test_capabilities.py`

**Interfaces:** `backend_issues(agent, policy, *, available, honoured: Mapping[str, Collection[str]] | None = None)` and `preflight(..., honoured=None)`. `honoured[backend]` names the settings the wiring makes good.

- [ ] Steps: a failing test (codex with `honoured={"codex": {"shell_default_deny"}}` reports no shell issue; without it, it warns), then implement, pass and commit.

### Task 3: `pass_env` reaches the agent CLI (DF-541)

**Files:**
- Modify: `claude_code.py`, `codex.py`, `acp/runner.py`, `acp/profile.py`
- Test: `test_claude_code_options.py`, `test_codex_lockdown.py`, `test_acp_profile.py`

**Interfaces:** each settings dataclass gets `pass_env: tuple[str, ...] = ()`:
- claude-code: a pass_env name is not blanked, unless it is a provider-routing name;
- codex: `safe_env(..., pass_through=pass_env)`;
- ACP: `launch_env(..., pass_env=...)` passes those names whatever their name or value says.

- [ ] Steps: failing tests per backend, then implement, pass and commit.

### Task 4: OpenCode pre-scan as JSONC, ACP teardown (DF-446, DF-448)

**Files:**
- Modify: `core/src/agentic/backends/acp/opencode.py`, `core/src/agentic/backends/acp/runner.py`
- Test: `test_acp_opencode_profile.py`, `test_acp_runner.py`

**Interfaces:**
- `strip_jsonc(text) -> str`.
- `code_in_tree` refuses only a non-empty `plugin`/`plugins` array or string. An unparsable config is refused as "cannot be read as JSONC".
- `AcpRunner` retries removing the step home 3 times, then warns, naming the folder.
- The stderr tail is masked: the profile's files and secret-named environment values become `***`.

- [ ] Steps: failing tests (`"plugin": []`, a commented-out plugin line and a string value containing `"plugin":` pass; `"plugin": ["x"]` is refused, with a message saying "loads OpenCode plugins"; a stderr tail echoing the key is masked; a locked home produces a warning), then implement, pass and commit.

### Task 5: Post-step protected-path check (DF-443 remainder)

**Files:**
- Modify: `commons/src/sdlc_commons/worktrees.py` (`StepCheckpoints.changed`), `cli/src/agentic_cli/wiring.py` (`denied_writes`)
- Create: part of `cli/src/agentic_cli/agent_backends.py` (`ProtectedWrites`)
- Test: `commons/tests/test_step_checkpoints.py`, `cli/tests/test_agent_backends.py`

**Interfaces:**
- `StepCheckpoints.changed() -> list[Path]`.
- `StepWiring.denied_writes(step) -> list[str]` gives the guard's reasons.
- `ProtectedWrites(runner, check)` is an `AgentRunner`. After a `completed` or `invalid` outcome with refused writes, it raises `ProtectedWriteError`. The engine then fails the step, its work is never checkpointed, and a fresh retry discards it.

- [ ] Steps: failing tests (a `.github/workflows/x.yml` and an `infra/main.tf` written under an `infra/**` deny, the paths named; a clean step passes through), then implement, pass and commit.

### Task 6: Build each agent-CLI backend in the CLI (DF-74, DF-212, DF-343, DF-356, DF-424, DF-425, DF-429, DF-436, DF-443, DF-541)

**Files:**
- Create: `cli/src/agentic_cli/agent_backends.py`
- Modify: `cli/src/agentic_cli/wiring.py` (`process_wrap_for(..., deny=())`), `cli/src/agentic_cli/runtime.py`
- Test: `cli/tests/test_agent_backends.py`

**Interfaces:**
- `AGENT_CLIS = ("claude-code", "codex", "opencode")`; `CONFIGURED_BACKENDS = ("native", *AGENT_CLIS)`.
- `build(rt, gateway, wiring) -> dict[str, Backend]`. Each backend is wrapped by `ProtectedWrites`.
- claude-code:
  - `permissions_for = Guard.for_agent(...).claude_permissions()`;
  - `wrap_for` = `process_wrap_for(step, write=(config dir,))` with srt, else `None`.
- codex:
  - `rules_for = ShellRules(**guard.shell_rules() allow/ask/deny)`;
  - `hook_for` writes the guard spec under `state/<run>/hooks/` and checks that the hook interpreter exists (DF-429);
  - `wrap_for = process_wrap_for(step, write=(home,), deny=(config.toml, rules, models.json, schema.json))`.
- opencode:
  - `rules_for = ShellRules(**guard.shell_rules())`;
  - `wrap_for` = `process_wrap_for(step, write=(home,))` with srt, else `None`;
  - `AcpSettings(home_root=state/opencode)`.

- [ ] Steps: failing tests (each builder's settings, the wrap's srt settings file: gateway host and registries only, the codex write denials, the hook spec outside the tree, identity headers from `rt.identity`), then implement, pass and commit.

### Task 7: Preflight: install hints, sandbox needs, the OpenCode tree scan (DF-81, DF-212, DF-446)

**Files:**
- Modify: `cli/src/agentic_cli/agent_backends.py` (`problems(report, decision, repo, settings)`), `cli/src/agentic_cli/runtime.py`, `cli/src/agentic_cli/commands/catalog.py`
- Test: `cli/tests/test_agent_backends.py`, `cli/tests/run/test_agent_backend_runs.py`

**Interfaces:** `missing(backend, settings) -> str | None` says exactly what to install, or None. `problems(...)` gives:
- a missing extra or CLI, as an error with the install command;
- codex without srt, as an error;
- a sandbox wrap on native Windows, as an error;
- OpenCode plugins in the repo, as an error naming the files;
- the codex hook honoured (`honoured`).

- [ ] Steps: failing tests (`agentic run --dry-run` exits 3 and names `agentic-core[claude-code]` when the extra is missing, with no traceback; codex in the `solo` regime without srt is refused; a repo with `.opencode/plugin/x.js` is refused before spend), then implement, pass and commit.

### Task 8: E2E: a pipeline step per backend through `agentic run` (acceptance)

**Files:**
- Test: `cli/tests/run/test_agent_backend_runs.py`
- Helpers: `cli/tests/run/_agent_fakes.py`, which puts `core/tests/backends` on `sys.path` for the existing stand-ins and adds a Python fake `srt`.

- [ ] Steps:
  - claude-code: the bundled binary against `TokenLeanStub` (solo regime, no srt), with the identity headers checked;
  - codex: `fake_codex.py` under the fake srt, with native-Windows refusal patched;
  - opencode: `acp_fake_agent.py`;
  - for each backend, a kill at a turn ends the run `killed`, and a pause holds it before the next step (a scripted kill switch);
  - commit.

### Task 9: Conformance fixes and live egress through the wiring (DF-544, DF-545, DF-550, DF-434)

**Files:**
- Modify: `core/tests/backends/conformance.py`, `core/tests/backends/test_codex_contract.py`
- Create: `cli/tests/test_process_wrap_live.py` (skipped without srt and curl)

- [ ] Steps:
  - the empty-answer case accepts only `UnexpectedModelBehavior`, and only from a backend without `own_process`;
  - a new containment case stops a still-running agent and checks its pid is gone;
  - a live srt test of `StepWiring.process_wrap_for` (gateway reached, other host refused);
  - a codex contract test that loads `a|b` alternatives and ask rules through the real binary (Linux image);
  - commit.

### Task 10: Docs and full CI

- [ ] Update the module docstrings (`runtime.py`, `wiring.py`) and the step-boundary hold note (decision 89).
- [ ] Run `uv run python scripts/check.py --all` to completion. If an acceptance cassette drifted, re-record it.

## Deferred items pulled in (target M2-60)

| DF | Plan |
|---|---|
| DF-74, DF-212, DF-343, DF-356 | Task 6 (claude-code built, srt wrap), Task 7 (Windows wrap refusal) |
| DF-81 | Task 7 |
| DF-424, DF-425, DF-429 | Task 6 |
| DF-426 | Task 2 + Task 7 |
| DF-436, DF-443 | Task 6 + Task 5 (post-step check of unenforced globs); `OPENCODE_GIT_BASH_PATH` and `agent_clis.opencode` documented in `agent_backends.py` |
| DF-446 | Task 4 + Task 7 |
| DF-448 | Task 4 (rmtree retry, redacted tail); descendants after the agent exits: accepted (documented POSIX limit; srt's PID namespace on Linux) |
| DF-541 | Task 3 + Task 6 |
| DF-544, DF-545, DF-550 | Task 9 (macOS half of DF-550 retargeted to M3-17) |
| DF-434 | Task 9 (alternatives and ask rules); symlinked paths retargeted to M3-17 |
| DF-76, DF-80, DF-322 | retargeted: each needs a spike against a live Claude Code under srt (decision 73 forbids live calls here) - M3-25 (real-model acceptance) |
| DF-435 | retargeted: a per-step scoped key needs TokenLean key issuance - M2-34 |
| DF-441 | retargeted: OpenCode's own server auth needs the real binary investigated - E2-06 |
