# S2 — Claude Code through TokenLean, locked down, via the Claude Agent SDK

**Date:** 2026-10-20 · **Code (throwaway):** `spikes/s2_claude_code/` (`stub.py`, `spike.py`, `spike2.py`) · **Versions:** `claude-agent-sdk` 0.2.164 (released 2026-10-06, the newest at least two weeks old), which bundles Claude Code 2.1.292; Windows 11.

## Question

Can the `claude-code` backend (M1-29) run Claude Code so that all of the following hold?
- Every model call goes to TokenLean, with our identity headers and the model policy chose.
- Nothing else leaves the machine.
- The user's own Claude Code settings, hooks, plugins and `CLAUDE.md` are not loaded.
- The agent sees only the tools policy allows, and the shell policy (`Guard.claude_permissions()`, SP8) holds.
- The answer comes back as JSON matching a schema.

## Method

As in S1, TokenLean was stood in for, and no real model was called:
- **TokenLean stub.** A local stub of TokenLean's Anthropic route (`/v1/messages` streamed and non-streamed, and `/v1/messages/count_tokens`). It logs every request (path, headers, model, tools offered, temperature, whether a planted `CLAUDE.md` marker reached the prompt) and answers from a small script, including tool calls.
- **Refusing proxy.** Set as `HTTPS_PROXY`/`HTTP_PROXY`, it refuses everything and logs the host Claude Code tried to reach. With `NO_PROXY` set to 127.0.0.1, any traffic outside TokenLean shows up there.
- **Isolation.** The SDK was installed in a throwaway virtualenv. Each run got a fresh working folder and a fresh `CLAUDE_CONFIG_DIR`, so the user's `~/.claude` was never read or written.

**Trust check.** Both the installed `claude.exe` (2.1.221) and the SDK's bundled one (2.1.292) carry valid Authenticode signatures from **Anthropic, PBC**, so the no-unsigned-binaries rule is met.

## Results

| # | Question | Result |
|---|---|---|
| Q1 | Do all model calls reach TokenLean with our identity? | **Yes.** One `POST /v1/messages?beta=true` per turn to `ANTHROPIC_BASE_URL`, carrying every `X-Agentic-*` header set through `ANTHROPIC_CUSTOM_HEADERS`. Claude Code adds `x-app`, `x-claude-code-session-id`, `user-agent` and `x-stainless-*`. None of them is a header we forbid. |
| Q2 | Is the model forced? | **Yes.** Requests carry exactly `model=` from the options. The small/fast model was never requested in the locked run, and `ANTHROPIC_DEFAULT_HAIKU_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` are pinned to the same model as a guard. An unknown model name only logs `unrecognized_model` (which affects Claude Code's own cost estimate, see Q8). |
| Q3 | Does anything else leave the machine? | **Not when locked:** no proxy attempts with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1`, `DISABLE_ERROR_REPORTING=1` and `DISABLE_AUTOUPDATER=1`. **Negative control:** without those switches Claude Code tried `CONNECT api.anthropic.com:443` twice and made an extra tool-less model call. So the switches are required, and the proxy really catches traffic. |
| Q4 | Are user settings, hooks and `CLAUDE.md` kept out? | **Yes, with `--bare` plus `setting_sources=[]`.** The planted `CLAUDE.md` marker was absent and 4 tools were offered. **Control** (no `--bare`, setting sources left unset): the marker reached the prompt and 23 tools were offered. In SDK 0.2.164 an **unset** `setting_sources` loads every source; only an explicit `[]` sends `--setting-sources=` (none). |
| Q5 | Does the agent see only the allowed tools? | **`allowed_tools` does not restrict them.** With `allowed_tools=["Read"]` the model was still offered Bash, Edit and Read, because `allowed_tools` is an auto-approve list. **`tools=["Read"]`** limits what is offered: only `Read` reached the model. |
| Q6 | Does the shell policy hold? | **Yes.** The settings were passed as `--settings` JSON in `Guard.claude_permissions()` shape (`allow: Bash(echo:*)`, `deny: Bash(git push:*)`, `Edit(infra/**)`), with `permission_mode="dontAsk"`. `git push origin main` was refused by the deny rule, quoted by name; the unlisted `curl` was refused (don't-ask mode); `echo s2-plain-echo` ran. `echo ok > allowed.txt` was refused: Claude Code treats a redirect as a file write that needs Edit permission. That is stricter than our rules, and the backend must allow `Edit` inside the tree for agents with `edit_files`. |
| Q7 | Does structured output work? | **Yes.** `output_format={"type":"json_schema","schema":…}` becomes `--json-schema`. Claude Code offers the model a `StructuredOutput` tool, and the result carries `structured_output` matching the schema. |
| Q8 | Cost and budget | `ResultMessage.total_cost_usd` is **Claude Code's own estimate** from its price table; TokenLean's `x-tokenlean-cost-usd` header is ignored. Usage tokens (input, output, cache) are reported. `max_budget_usd` is enforced against that local estimate. |
| Q9 | Temperature and effort | Claude Code never sends `temperature` (it was absent in every request). The SDK exposes `effort` and `thinking`. |

## Decision

1. **The `claude-code` backend (M1-29) drives Claude Code through `claude-agent-sdk`, pinned at 0.2.164, as an optional extra (`agentic-core[claude-code]`), never a core dependency.**
   - The package declares MIT, but its README says use is governed by Anthropic's Commercial Terms of Service, and it bundles the proprietary, Anthropic-signed Claude Code binary.
   - Core stays permissive-only; an org opts in by enabling the backend.
   - `cli_path` may point at an org-pinned install instead of the bundled binary.
2. **Lock-down configuration.** Every run sets all of these. Each was proven above, and the backend's contract tests pin each one.
   - **Options:**
     - `setting_sources=[]` (explicit)
     - `extra_args={"bare": None, "no-session-persistence": None}`
     - `tools=` the Claude Code tools mapped from the agent's capabilities
     - `allowed_tools=` the same list, so it is auto-approved
     - `permission_mode="dontAsk"`
     - `settings=` the JSON of `Guard.claude_permissions()`, plus `Edit` allowed inside the tree when the agent may edit
     - `model=` the resolved model
     - `max_turns` from limits
     - `max_budget_usd` from the step's budget slice
     - `output_format` from the step's output contract
   - **Environment:**
     - `ANTHROPIC_BASE_URL` = TokenLean
     - `ANTHROPIC_API_KEY` = the user's proxy key
     - `ANTHROPIC_CUSTOM_HEADERS` = every `X-Agentic-*` header and `traceparent`
     - `ANTHROPIC_DEFAULT_HAIKU_MODEL` and `ANTHROPIC_SMALL_FAST_MODEL` = the resolved model
     - `CLAUDE_CONFIG_DIR` = a per-step folder in run state
     - `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1`, `DISABLE_ERROR_REPORTING=1`, `DISABLE_AUTOUPDATER=1`
3. **Sandbox.** On macOS, Linux and WSL2 the whole Claude Code process runs under our `srt` sandbox (D38), with only the TokenLean host allowed on the network (DF-65). Claude Code's own sandbox setting stays off, so srt is never nested.
4. **Cost.** The engine charges the step from `ResultMessage.usage` priced with our `Pricer` (marked `estimated`), never from Claude Code's `total_cost_usd`. TokenLean's usage rows, tagged by run and step (ST-2, M2-31), are the reconciled truth. `max_budget_usd` is a best-effort stop inside the step; TokenLean's spend caps are the hard one.
5. **Capability matrix (M1-27).** For `claude-code`:
   - `temperature` is unsupported: policy decides ignore, warn or block.
   - `effort` is supported.
   - The tools map as follows: `read_files` → Read; `search` → Grep, Glob; `edit_files` → Edit, Write; `run_shell` → Bash.
   - `codeintel`, `activate_skill` and `spawn_agent` are not available until mapped, so an agent that needs them cannot use this backend: preflight refuses it.

## Not tested here (carried to M1-29)

- **Claude Code under srt on Linux.** The M1-29 contract tests run in the Linux check image with the SDK's bundled Linux binary.
- **A real model.** Every answer here came from the stub. M1-29 adds one live smoke run through a real TokenLean (M1-67).
- **The Bash tool on Windows.** It worked here; Claude Code uses Git Bash on Windows, which `init`'s readiness check must report when this backend is enabled.

## Cost if wrong

The backend sits behind the backend protocol (M1-27): replacing the SDK with direct `claude -p --output-format stream-json` driving touches one module. The lock-down checks are contract tests, so a Claude Code release that changes a flag fails the pinned-version tests before it reaches a developer.

## Correction (SP7 final review, 2026-10-20)

Two points in the decision above were wrong, and the claude-code backend does not follow them.

- **`allowed_tools` must stay empty.** A bare tool name there (`Bash`, `Edit`, `Read`) approves every use of that tool. It beats don't-ask mode and the guard's prefix rules, so only explicit deny rules held. Q6 missed this because it tried only a denied and an allowed command, never an unlisted one. The backend now passes no `allowed_tools`: the guard's rules in `--settings` (plus `Edit(./**)` inside the tree) are the only approvals. Contract tests now cover:
  - an unlisted command;
  - a chained command;
  - an ask-listed command;
  - a redirect, edit and read outside the tree.
- **In `--bare` mode Claude Code offers only Read, Edit and Bash.** It offers no Grep, Glob or Write, so `search` has no tool there and preflight blocks it.

The review also found that the user's environment reaches Claude Code: the SDK merges `os.environ` under the options. With `CLAUDE_CODE_USE_BEDROCK=1` set, calls went to AWS instead of TokenLean. The backend now blanks every provider switch, cloud credential and secret-named variable from the user's environment, and a contract test runs with a hostile environment.

