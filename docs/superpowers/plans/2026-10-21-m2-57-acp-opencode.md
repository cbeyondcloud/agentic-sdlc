# M2-57 ACP backend with OpenCode as its first profile - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An agent step can run on the `opencode` backend. One generic `AcpBackend` drives any Agent Client Protocol (ACP v1) agent CLI over stdio; everything agent-specific is a profile held as data. OpenCode 1.18.35 is the first profile (`opencode acp --pure`). Every model call goes to TokenLean with our identity headers on the resolved model. The agent sees only the tools mapped from the agent's capabilities, and our shell policy is compiled into OpenCode's own permission rules. A hostile repository cannot add MCP servers, instructions or plugins. The ACP permission callback is a second gate that refuses whatever OpenCode asks about. The answer is checked against the step's contract (one retry). The step is charged at policy prices. Contract tests run the real pinned binary in the Linux check container against an OpenAI-compatible stub.

**Rows:** M2-57, and the deferred items DF-124 (close the project-config merge) and DF-128 (contract tests per pinned agent version).

**Architecture:**
- **New package `agentic.backends.acp`** (core). It imports neither `sdlc_commons` nor Zyroflo, and imports the `acp` SDK only when a run starts:
  - `profile.py`: `ShellRules` (our shell policy, provider-neutral), `StepSetup` (what a profile compiles for one step), `Launch` (its output: environment and files), `AcpProfile` (the profile as data), `home_env(home)`.
  - `opencode.py`: the `OPENCODE` profile, plus `opencode_permissions(tools, rules)` and `opencode_config(setup)`. Vendor names live only here and in the capability row.
  - `runner.py`: `AcpSettings`, `AcpRunner` (an `AgentRunner`), `AcpBackend` (a `Backend`), and `_Client`. `_Client` is the ACP client: it collects the turn's text, tool calls and usage, and refuses every permission request.
- **`agentic.backends.processes`**: `kill_tree(pid)` and `session_leader(argv)` (POSIX setsid launcher), so the time limit kills the agent and everything it started.
- **Capability row** `opencode` in `capabilities.CAPABILITIES`, and `OPTIONAL["opencode"] = "acp"`.
- **Commons:** `Guard.shell_rules()` returns `ShellRules` with the framework floor (`FRAMEWORK_DENY_SHELL`, protected paths) included, ready for CLI wiring.
- **Linux check image:** `opencode-ai@1.18.35` installed with npm, beside `srt`.

**Tech Stack:** Python 3.12, `agent-client-protocol==0.12.1` (Apache-2.0, pure Python, depends only on pydantic, released 2026-08-16) as the new optional extra `acp` of agentic-core, installed in the dev group. pytest.

**Spec:** §9.1 (`opencode` and `acp:<agent>` rows), D32 (revised), §9.2 (srt is the boundary). Spike record: `docs/spikes/S7-acp-and-antigravity.md` (decisions 25-29).

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| ACP client, stdio transport, schema | `agent-client-protocol` 0.12.1 (`spawn_agent_process`, `schema`) | 1.0.0rc*: not GA (DF-122) |
| The agent | OpenCode 1.18.35 (MIT), run as a subprocess, never imported | `opencode run --format json`: no permission callbacks (S7) |
| Model stub for contract tests | grown from `spikes/s7_acp/stub.py` (our own throwaway code) | — |
| Fake ACP agent for unit tests | the SDK's own agent side (`acp.run_agent`) | — |
| Kill-the-tree, session leader | the same approach as `claude_code.py` (moved into a small shared module for ACP; `claude_code.py` is left alone to avoid churn with parallel tracks, see deferred) | — |

## What the OpenCode 1.18.35 source shows (read at tag v1.18.35, commit 53d1eab)

- **Permission keys:** `read`, `edit`, `glob`, `grep`, `list`, `bash`, `task`, `external_directory`, `todowrite`, `question`, `webfetch`, `websearch`, `lsp`, `doom_loop`, `skill`. Built-in agents also ask for `plan_enter` and `plan_exit`. The key is `bash` at this version; current docs say `shell`.
- **Rule order:** the last matching rule wins. User rules come after the built-in agents' defaults. A tool whose last matching rule is `"*": "deny"` is hidden from the model.
- **Shell rules:** they match each parsed subcommand. The pattern `x *` matches both `x` and `x ...`.
- **Edit and read rules:** they match the path relative to the worktree, and `*` crosses `/`.
- **`OPENCODE_DISABLE_PROJECT_CONFIG=1`** skips three things:
  - project `opencode.json[c]`;
  - project `.opencode/` folders (agents, commands, plugins);
  - project instruction files.

  This closes DF-124's merge without touching the worktree.
- **`OPENCODE_PERMISSION`** is deep-merged last, above managed config.
- **On rejection:** a rejected ask stops the agent loop unless `experimental.continue_loop_on_deny` is true. A rule `deny` never stops it.
- **`registry.npmjs.org` egress:** OpenCode always installs `@opencode-ai/plugin` into its global config folder. It cannot be switched off, so the sandbox blocks it.
- **Usage reporting:**
  - The ACP `PromptResponse.usage` covers only the turn's last model call.
  - `usage_update.cost` is the session's summed cost at the model's configured prices. It is sent only when the model has a `limit.context`.

## Found by the contract tests (the real 1.18.35 binary), and closed

- **Plugins still load.** OpenCode's newer internal plugin loader (`packages/core/src/config.ts` and `config/plugin/external.ts`) ignores both `OPENCODE_DISABLE_PROJECT_CONFIG` and `--pure`. It still imports `.opencode/{plugin,plugins}/*.{js,ts}`, and it installs and imports `plugin(s)` npm packages named in a project `opencode.json[c]`, from the working folder up to the repository root. A planted plugin wrote its file in the first contract run.
  - **Closed** with the pre-scan S7 named, as a generic profile hook. `AcpProfile.refuse_tree` stops the agent from ever starting in such a tree (fail closed, with a message naming the files). OpenCode's edit rules also deny `.opencode/*` and `opencode.json[c]`, so an agent cannot plant code for the next step.
  - The rest of a hostile config stays inert: MCP server, instructions, `AGENTS.md`, agents, permissions, provider and model. A mutation run that dropped `OPENCODE_DISABLE_PROJECT_CONFIG` failed the hostile-config test, as it should.
- **srt broke OpenCode's self-calls.** In ACP mode OpenCode calls its own HTTP server on loopback. Under srt with `proxy_everything` (NO_PROXY cleared), those calls went to srt's proxy and were refused ("OpenCode service failure").
  - **Closed** with the generic `AcpProfile.own_server` = `("--port", "127.0.0.1")`. The runner gives the agent a free port and exempts exactly `127.0.0.1:<port>` from proxies.
  - An experiment in the check image showed that Bun honours host:port `NO_PROXY` entries. A gateway on another loopback port therefore still goes through the sandbox proxy, so TokenLean LITE on a laptop keeps working.
  - The profile also pins `--hostname 127.0.0.1 --mdns=false`, so no config can bind the server to every interface.

## Final review fixes (F1-F4)

- **F1, the key file stayed readable.** Any allowed command that reads files (`cat $HOME/tokenlean.key`) returned the key. The new generic hook `AcpProfile.secret_files` fixes this: the runner deletes those files as soon as `session/new` returns, because OpenCode reads `{file:}` once, while it loads its config. A file that cannot be deleted stops the step, and the files are deleted again before the home is removed. Contract test: `cat` finds no file, and every model call still carries the key.
- **F2, OpenCode matches case-sensitively except on Windows.** On a case-blind host (a non-Windows tree whose file system folds case, which is macOS by default), every edit/read deny and every bash ask/deny is also written lower-case, upper-case and capitalised, per path segment or word, in every combination for up to three parts. Flags keep their case. Allows are never widened. The matcher has no character classes, so spellings like `gIt` stay open. This narrows the gap but is not a boundary: on macOS srt is the boundary, and checking it is deferred to M2-41.
- **F3, the agent could be launched from the current folder.** `resolve_command` now searches only the absolute PATH folders, skipping the current folder. It returns an absolute path, and refuses a relative path or a program inside the step tree.
- **F4, writes by allowed commands.** The live srt test now shows that srt, given `deny_write` from `Guard.write_denials(tree)`, blocks `printf >> .git/x` while writes elsewhere in the tree still work. The M2-41 wiring must pass those denials.

## Global Constraints

- **Provider-agnostic engine.** `runner.py` and `profile.py` carry no vendor names, flags or types. A new ACP agent is a new `AcpProfile` value plus its contract tests (E2-06).
- **Lock-down set on every OpenCode run:**
  - **Provider and model:**
    - `OPENCODE_CONFIG_CONTENT` with a single provider `tokenlean` (`@ai-sdk/openai-compatible`), whose `baseURL` is `{gateway}/v1`, with our API key and our identity headers;
    - `enabled_providers = ["tokenlean"]`;
    - `model` and `small_model` both set to `tokenlean/<resolved model name>`.
  - **Permissions:** every permission key is set, with `"*": "deny"` first. The same object also goes in `OPENCODE_PERMISSION`.
  - **Project config:** `OPENCODE_DISABLE_PROJECT_CONFIG=1`, plus `plugin: []`, `instructions: []`, `mcp: {}`, `formatter: false`, `lsp: false`, `snapshot: false`, `share: "disabled"` and `autoupdate: false`.
  - **Other agents:** subagents (`general`, `explore`) and `plan` are disabled.
  - **Egress and update switches:** `--pure`, `OPENCODE_DISABLE_AUTOUPDATE`, `_MODELS_FETCH`, `_LSP_DOWNLOAD`, `_DEFAULT_PLUGINS`, `_CLAUDE_CODE`, `_EXTERNAL_SKILLS` and `_SHARE`.
  - **Per-step home:** `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA` and the XDG folders all point inside it.
- **Environment.** The ACP SDK passes on only a short list of variables: PATH, HOME, user and system basics. Nothing secret-named is inherited. The profile's `inherit` list names the few extra variables passed through: corporate CA bundle and Git Bash path, each dropped if it carries a credential.
- **Shell policy compiled into OpenCode's format:**
  - `bash` starts with `"*": "deny"`;
  - then each allowed prefix `"<p> *": "allow"`;
  - then each ask prefix `"<p> *": "ask"`;
  - then each deny prefix `"<p> *": "deny"`, last so that it wins;
  - `a|b` alternatives in a rule are spelled out as separate rules.

  Edit: `"*": "allow"` when the agent may edit, then each denied path as `deny`. A `**/x` glob also denies `x` at the top, and `**` becomes `*`. Without `edit_files`, edit is `"deny"`. Reads of `.env` files are denied.
- **Second gate.** `_Client.request_permission` never allows. It picks the agent's `reject_once` option (or a cancelled outcome when none is offered), records the refusal, and never selects an `allow_always` option. `approvals=False` in the capability row: ask-listed commands are refused, and preflight warns.
- **srt stays the boundary.** `wrap_for(step)` gives a command prefix (`SrtSandbox.command_prefix(proxy_everything=True)`), whose rules allow only the TokenLean host. Wrapping on native Windows raises (D38: WSL2).
- **Result:** the final message is the agent text after the turn's last tool call. With a contract it is checked; errors go back once in the same session (`OUTPUT_RETRIES = 1`), then the step ends `invalid`.
- **Cost (all `estimated`, at policy prices), first that applies:**
  1. the profile writes our prices into the agent's model config (`session_cost=True`), and the agent reports a cumulative session cost: the step is charged the difference since the last turn;
  2. the turn reports token usage: those tokens times the price;
  3. a price but no usage: estimated client-side from text length (4 characters a token);
  4. no price: `fallback_estimate_usd` per model call, where calls = 1 + the turn's tool calls.

  A charge past the slice raises `LimitHit`, after the call is recorded.
- **Formatting, CI and records:**
  - never add `# fmt: skip` or `# fmt: off`; line length 100;
  - check pytest's exit code directly;
  - `uv run python scripts/check.py --all` must be green;
  - TRACKER.md, DEFERRED.md and DECISIONS-* are updated by the coordinator from this track's output.

## Review Focus

1. **A hostile repository.** A planted `opencode.json` sets every permission to allow, adds an MCP server and instructions, and points at another model and base URL; a planted `.opencode/plugins/x.js` writes a file when loaded. None of it takes effect: the model sees only our tools, the marker never reaches the prompt, the plugin's file never appears, and the model and URL stay ours. Test: Task 7 `test_a_hostile_project_config_changes_nothing`.
2. **A command that is not allowed never runs.** That covers an unlisted plain command (`touch`) and an unlisted "read-only" one (`echo`), both denied by OpenCode's rule, as well as ask-listed ones, which OpenCode asks about and our callback refuses. Test: Task 7 `test_unlisted_commands_are_denied_by_the_compiled_policy`, `test_ask_listed_commands_are_asked_and_refused`; Task 5 `test_every_permission_request_is_rejected_once`.
3. **Every model call reaches TokenLean** with all identity headers on the resolved model, the title call included. Nothing else leaves except the known `registry.npmjs.org` attempt, which the sandbox blocks. Test: Task 7 `test_every_call_reaches_tokenlean_with_identity`, `test_the_egress_list_is_exactly_the_profile_s`; Task 8 live srt test.
4. **The time limit kills the agent and everything it started**, on Windows and POSIX. Test: Task 5 conformance `the_time_limit_stops_the_run`; Task 7 `test_the_time_limit_stops_the_process`.
5. **The budget means the same on every backend.** Usage is priced by policy. An over-slice charge raises `LimitHit`, and the call stays in the cost log. Test: Task 5 cost tests; Task 7 `test_usage_is_charged_at_policy_prices`.

---

### Task 1: The `acp` extra, its import contract, the capability row

**Files:**
- Modify: `core/pyproject.toml` (extra `acp`), `pyproject.toml` (dev group extras; the import-linter contract "The ACP SDK only in the ACP backend runner"), `uv.lock`, `core/src/agentic/backends/capabilities.py`, `core/src/agentic/backends/__init__.py`
- Test: `core/tests/backends/test_capabilities.py`

**Interfaces:** the `CAPABILITIES["opencode"]` row has these fields:
- tools: `read_files` -> `read`, `list`; `search` -> `glob`, `grep`; `edit_files` -> `edit`; `run_shell` -> `bash`;
- `temperature=True` (set on OpenCode's agent);
- `effort=False`, `approvals=False`, `children=False`, `stuck_check=False`, `secret_scan=False`.

Also `OPTIONAL["opencode"] = "acp"`.

- [ ] Test first: `opencode` offers no `spawn_agent` or `codeintel`; temperature on it is honoured, effort is not, and secret_scan is reported. Run and see it fail (KeyError). Then add the row and see it pass.
- [ ] `uv run lint-imports` is green with the new contract.
- [ ] Commit.

### Task 2: Shell rules and the OpenCode policy compile (pure data)

**Files:**
- Create: `core/src/agentic/backends/acp/__init__.py`, `profile.py` (`ShellRules`), `opencode.py` (`opencode_permissions`, `opencode_config`)
- Test: `core/tests/backends/test_acp_opencode_profile.py`

**Interfaces:**
- `ShellRules(allow, ask, deny, deny_paths)`, with `ShellRules.from_agent(agent)`.
- `opencode_permissions(tools: Sequence[str], rules: ShellRules) -> dict` (ordered).
- `opencode_config(setup: StepSetup) -> dict`.

- [ ] Tests first:
  - every permission key is present and `"*"` is the first key;
  - tools outside the capabilities are denied;
  - the `bash` order is deny-all, allow, ask, deny;
  - `a|b` alternatives are spelled out;
  - denied paths become edit denies, with `**/x` also denying `x`;
  - edit is `"deny"` without `edit_files`;
  - the provider points at `{gateway}/v1`, with headers and key;
  - `model` and `small_model` equal `tokenlean/<name>`;
  - `enabled_providers` names only ours;
  - `plugin`, `instructions` and `mcp` are empty, and formatter, lsp and snapshot are off;
  - `continue_loop_on_deny` is true;
  - `max_turns` maps to `agent.build.steps`, and temperature to `agent.build.temperature`;
  - policy prices are written as the model's `cost` (absent when unpriced);
  - the profile's environment sets `OPENCODE_DISABLE_PROJECT_CONFIG`, `OPENCODE_PERMISSION` and each quiet switch.
- [ ] Implement; tests pass.
- [ ] Commit.

### Task 3: The profile model and the step's launch environment

**Files:** `profile.py` (`AcpProfile`, `StepSetup`, `Launch`, `home_env`, `launch_env`), test `core/tests/backends/test_acp_profile.py`

- [ ] Tests first:
  - the home variables (HOME, USERPROFILE, APPDATA, LOCALAPPDATA, XDG_*) all point inside the per-step home;
  - inherited names pass through only when set and carrying no credential;
  - the profile's quiet switches and launch environment win over inherited values;
  - the profile files are written under the home (and refused outside it).
- [ ] Implement; tests pass.
- [ ] Commit.

### Task 4: Process helpers

**Files:** `core/src/agentic/backends/processes.py`, test `core/tests/backends/test_processes.py`

- [ ] Test first: `kill_tree` kills a child Python process and its grandchild, on Windows and POSIX. `session_leader(argv)` is identity on Windows and a setsid launcher on POSIX.
- [ ] Implement; tests pass.
- [ ] Commit.

### Task 5: `AcpRunner` / `AcpBackend` against a fake ACP agent (runs on every OS)

**Files:**
- Create: `core/src/agentic/backends/acp/runner.py`, `core/tests/backends/acp_fake_agent.py` (a scripted ACP agent built on the SDK's agent side; it logs what it received)
- Test: `core/tests/backends/test_acp_runner.py`

- [ ] Tests first:
  - all six backend conformance cases (`conformance.CASES`);
  - the first prompt carries the instructions, the data-fenced inputs and the contract;
  - the final message is the text after the last tool call;
  - `test_every_permission_request_is_rejected_once` (reject_once is chosen, never allow_always; with no reject option, cancelled; refusals land in the outcome `detail`);
  - auth is called when the profile names a method, and the mode is set when the profile names one;
  - the env and cwd the agent saw are the per-step home and the tree;
  - the four cost paths and `LimitHit` (the call stays in the cost log);
  - a `max_turn_requests` stop ends `stopped/turns`, and a `refusal` stop is an error;
  - wrapping on Windows raises;
  - an npm `.cmd` shim is refused with a hint;
  - resume is refused.
- [ ] Implement; tests pass on Windows.
- [ ] Commit.

### Task 6: `Guard.shell_rules()` (commons)

**Files:** `commons/src/sdlc_commons/guards.py`, test `commons/tests/test_guards*.py`

- [ ] Test first: the rules include `FRAMEWORK_DENY_SHELL`, the agent's deny list, and protected paths with the deny paths. Implement; test passes.
- [ ] Commit.

### Task 7: OpenCode contract tests (real pinned binary, Linux check container only)

**Files:**
- Modify: `scripts/linux-check.Dockerfile` (npm `opencode-ai@1.18.35`; `AGENTIC_CHECK_IMAGE=1`)
- Create: `core/tests/backends/openai_stub.py` (from `spikes/s7_acp/stub.py`: scripted chat-completions, streamed and not, `GET /models`; requests with no tools, such as the title call, get a short answer without consuming the queue; the RefusingProxy is reused from `anthropic_stub`), `core/tests/backends/test_opencode_contract.py`
- Skip rules: off Linux, or with no `opencode` on PATH, the tests are skipped with the reason "the pinned binary runs only in the Linux check container". A version mismatch fails inside the check image and skips elsewhere.

- [ ] Tests:
  - conformance cases;
  - `test_every_call_reaches_tokenlean_with_identity` (path `/v1/chat/completions`, all identity headers, the forced model on every call including the title call, the temperature honoured);
  - `test_the_egress_list_is_exactly_the_profile_s`;
  - `test_unlisted_commands_are_denied_by_the_compiled_policy` (plain and read-only);
  - `test_ask_listed_commands_are_asked_and_refused` (plain and read-only, no side effect);
  - `test_an_allowed_command_runs`;
  - `test_a_hostile_project_config_changes_nothing`;
  - `test_only_mapped_tools_are_offered`;
  - `test_usage_is_charged_at_policy_prices`;
  - `test_the_time_limit_stops_the_process`.
- [ ] Run in the container (`scripts/check.py --linux`). Each lock-down test must also have been seen failing once against a weakened config: drop `OPENCODE_DISABLE_PROJECT_CONFIG` and see the hostile test fail.
- [ ] Commit.

### Task 8: OpenCode under srt (live, Linux container)

**Files:** `core/tests/backends/test_opencode_live_srt.py`

- [ ] Test: OpenCode under `srt` (rules: write only the tree and the per-step home; allowed domains only the stub) still reaches the stub, and an allowed command writing outside the tree is blocked.
- [ ] Commit.

### Task 9: Definition of done

- [ ] `uv run python scripts/check.py --all` green (Windows native + Linux container). Grep the touched files for `fmt: skip` / `fmt: off`.
- [ ] Return tracker notes, deferred items and needs-decision in the structured output.

## Deferred from this slice (proposed)

- **CLI wiring** of `AcpBackend("opencode")` into `agentic_cli.runtime`, together with the claude-code wiring (DF-212 pattern): `rules_for = Guard.shell_rules()`, `wrap_for = srt prefix` with only the TokenLean host allowed, `opencode` added to `CONFIGURED_BACKENDS`. Target M2-41.
- **Approval pauses over ACP permission callbacks** (`approvals=True`). They need a held-open session or session resume. Target E2-06.
- **Reconcile ACP step cost against TokenLean usage rows** once TokenLean exposes a per-run usage query. Target M2 TokenLean usage rows / M2-41.
- **`claude_code.py` to use `agentic.backends.processes`** (dedupe kill-tree). Target M2-41.
- **Native Windows/macOS run of the real OpenCode over ACP** (DF-127 stays open for E2-06). The Python fake agent covers the transport on Windows.
