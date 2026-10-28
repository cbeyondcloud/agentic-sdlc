# S3 — Codex `exec` through a TokenLean Responses endpoint

**Date:** 2026-10-20 · **Code (throwaway):** `spikes/s3_codex/` (`stub.py` Responses stub and refusing proxy, `probe.py` main runs, `probe2.py` repo-config and sandbox runs, `probe3.py` apply_patch shapes, `Dockerfile`; raw traffic in the git-ignored `out/`) · **Versions:** Codex CLI `rust-v0.160.1` (released 2026-10-05, the newest release at least two weeks old; Apache-2.0), Linux x86_64 musl binary checked against the release sha256; LiteLLM 1.95.1 (TokenLean's floor) for the bridge check · **Platform:** Debian bookworm container run with `--network none`. Nothing was installed on the Windows host.

## Question

What must TokenLean build (ST-4, M2-32) so that the `codex` backend (M2-40) can run `codex exec --json --output-schema` through TokenLean? And can Codex be locked down the way S2 locked down Claude Code? That means:
- every model call goes to TokenLean with our identity headers;
- nothing else leaves the machine;
- the user's and the repo's own Codex config do not load;
- the shell policy holds;
- the answer comes back as schema-shaped JSON.

## Method

- **Stub.** A stdlib stand-in for a TokenLean `POST /v1/responses` route. It logs every request (path, headers, body) and answers from a script with a streamed Responses API reply. The reply is either a reasoning item carrying `encrypted_content` plus a function call to the shell tool Codex offered, or a final message: JSON when `text.format` is `json_schema`, plain text otherwise. Event sets are switchable: the full set, a minimal set, no `response.completed`, or no `usage`. Any other path answers 404 and is logged.
- **Refusing proxy.** Set as `HTTPS_PROXY`/`HTTP_PROXY` with `NO_PROXY=127.0.0.1`, as in S2. The container has no network at all, so nothing could have left it anyway.
- **Isolation and planted markers.** Each run got a fresh `CODEX_HOME`, a fresh git work folder and a fresh stub. Markers were planted in four places: the work folder's `AGENTS.md`, the work folder's `.codex/config.toml` (as `developer_instructions`, plus a hostile provider), and the real `~/.codex/config.toml` and `~/.codex/AGENTS.md`.
- **Runs.** 30 runs (A–O, P1–S2, T) with the identity headers set through `env_http_headers`, and `codex execpolicy check` against a compiled rules file.
- **Source reading** at the pinned tag: `model-provider-info` (wire API), `codex-api/src/sse/responses.rs` (which events are handled), the config schema, the config-layer loader and the compaction gates.

**Trust check.** The Windows binary `codex-x86_64-pc-windows-msvc.exe` carries an Authenticode signature from **OpenAI OpCo, LLC**, issued by Microsoft ID Verified CS EOC CA 04 (read with `osslsigncode` in a container). The npm package `codex-npm-win32-x64` bundles an **unsigned `rg.exe`**. The Linux binaries ship sigstore bundles.

## Results

| # | Question | Result |
|---|---|---|
| Q1 | Which wire API? | **Responses only.** `wire_api = "chat"` is rejected at config load ("no longer supported", run H). Chat Completions is not an option, so ST-4 is a hard prerequisite for M2-40. |
| Q2 | Which endpoints does Codex call? | **Only `POST {base_url}/responses`**, one per model turn. There is no `GET /models` and no WebSocket: `supports_websockets` defaults to false for custom providers. There is no `/responses/compact` either: remote compaction is enabled only for the built-in OpenAI and Bedrock providers, so a custom provider compacts through ordinary `/responses` calls. |
| Q3 | Identity and model? | **Yes.** Every `X-Agentic-*` header plus `traceparent` arrived through `env_http_headers`; a header whose variable is empty is omitted. Static headers go through `http_headers`. The key arrived as `Authorization: Bearer <$TOKENLEAN_API_KEY>` (from `env_key`). `model` is exactly the configured one. A hostile `OPENAI_API_KEY`, `OPENAI_BASE_URL` or `CODEX_API_KEY` changed nothing (run G); the backend still blanks them, as S2 decided. |
| Q4 | Does anything else leave the machine? | **Not when locked.** Negative control F (provider only) tried `CONNECT github.com`, `chatgpt.com` and `ab.chatgpt.com`. The cause is the plugin manager's featured-plugins fetch (`chatgpt.com/backend-api/plugins/featured`), the marketplace sync, and analytics. With `[features] plugins = false` and `[analytics] enabled = false` (plus the other switches below) there were no attempts in any locked run. |
| Q5 | Do user and repo config stay out? | **User config:** yes. A per-step `CODEX_HOME` means `~/.codex` is never read, and that marker never appeared. **`AGENTS.md`:** off with `project_doc_max_bytes = 0` (control F: the marker reached the prompt). **Repo `.codex/config.toml`:** **no, not by default.** Codex's layer order is managed > `-c` session flags > project `.codex/` > user (`CODEX_HOME`). Only the provider keys are refused from a project layer. A hostile repo's `developer_instructions` reached the model and its `[mcp_servers]` entry **started a process** (P1), even when the whole lock-down was also passed as `-c` (P3), because those keys add to the config rather than override ours. **Only `[projects."<worktree>"] trust_level = "untrusted"` in our config kept the project layer out**: no marker, no MCP server, and the repo's `.codex/rules` ignored (P2, R). Run A (read-only sandbox) happened not to load the project layer; the cause was not traced, and it is not a defence. |
| Q6 | Does the shell policy hold? | **Partly.** `codex execpolicy` (Starlark `prefix_rule`) works: a `forbidden` rule rejected `git push origin main` with our justification quoted (I), also with Codex's sandbox off (N) and also against a hostile repo `allow` rule (strictest wins, P4). A plain `&&` chain is split and checked per part (O). With `approval_policy = "never"` a `prompt` rule is **rejected**, matching the claude-code backend's "ask means refused" (run S1). `approval_policy = "untrusted"` was removed in this version; only `never`, `on-request` and `granular` remain (run S2). **Gaps:** (a) **there is no default-deny**: an unlisted command runs, sandboxed (J); (b) **an `allow` rule runs the command outside Codex's sandbox**: `touch` outside the workspace succeeded when allow-listed and failed in the sandbox when unlisted (Q1/Q2, R2); (c) **a redirect or substitution stops the script from being split**, so `echo ok > f && git push origin main` was not matched and the push ran (O). |
| Q7 | Structured output? | **Yes.** `--output-schema` arrives as `text.format = {type: "json_schema", strict: true, name: "codex_output_schema", schema}`. The final `item.completed` `agent_message` carries the JSON, and `-o` writes it to a file. Without `--output-schema`, `text` is absent for fallback-metadata models (L); a catalog model still sends `text.verbosity`. |
| Q8 | What does Codex need from the stream? | Items are built **only from `response.output_item.done`**. The turn ends **only on `response.completed`** (or `response.incomplete` / `response.failed`), whose `response.id` is required. The minimal set of `output_item.done` + `completed` worked end to end with a tool round trip (C). `output_text.delta` feeds live text. `function_call_arguments.*`, `content_part.*`, `in_progress` and `output_text.done` are ignored. **No `response.completed`:** Codex reports "stream closed before response.completed" and **retries the whole call** with the partial text appended (E: two upstream calls, then `turn.failed`). **No `usage`:** the turn completes with all token counts at 0 (D). |
| Q9 | Is conversation state stateless? | **Yes.** `store: false`, never `previous_response_id`, and the **whole history is resent each turn**. That includes the reasoning item with `encrypted_content` (`include: ["reasoning.encrypted_content"]` is always requested), the `function_call`, and a `function_call_output` whose output is Codex's exec text. Input items carry Codex-generated `id`s. The first request was about 35 KB, mostly the 17k-character `instructions`. `prompt_cache_key` is the thread id. |
| Q10 | Tools offered | With a non-catalog model (fallback metadata, e.g. a Claude model routed by TokenLean): `exec_command`, `write_stdin`, `request_user_input`, `view_image`, and **no `apply_patch`**. Unlocked, it also offers a `namespace` tool (`multi_agent_v1`), goal tools and a hosted `web_search`. With a catalog slug (`gpt-5.5`, M) it also offers **`apply_patch` as a `custom` tool** (`format: {type: grammar, syntax: lark, …}`) and sends `reasoning.effort` and `text.verbosity`. **Codex accepts an `apply_patch` call only as a `custom_tool_call` item** (`input` = the patch). The same call as a `function_call` with `{"input"}` or `{"content"}` came back `aborted` (T). Codex never sends `temperature`. |
| Q11 | Cost | Codex's JSONL `turn.completed.usage` carries `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens` and `reasoning_output_tokens`, summed over the turn's calls. It reports **no cost**, and it ignores TokenLean's `x-tokenlean-cost-usd` and every other response header. |

### Captured request (locked run, catalog model, turn 2)

| Field | Value Codex sent | ST-4 handling |
|---|---|---|
| path, method | `POST /v1/responses` | new ingress route |
| `Authorization` | `Bearer <TOKENLEAN_API_KEY>` | existing proxy-key auth |
| `X-Agentic-*`, `traceparent` | all ten plus `traceparent`, from `env_http_headers` | existing routing/tag headers (ST-2) |
| `originator`, `user-agent` | `codex_exec`, `codex_exec/0.160.1 (…)` | accept; never forward upstream |
| `session-id`, `thread-id`, `x-client-request-id`, `x-codex-window-id`, `x-codex-turn-metadata` (installation, session, turn ids), `x-codex-beta-features: remote_compaction_v2` | per thread or turn | accept; never forward (privacy) |
| `model` | the configured model | routed as today |
| `instructions` | about 17k characters | system message |
| `input[]` | `message` (developer/user/assistant; `input_text`/`output_text`), `reasoning` (`summary`, `encrypted_content`), `function_call`, `function_call_output`, `custom_tool_call`(`_output`) | translate (below) |
| `tools[]` | `function` (JSON schema), `custom` (lark grammar); unlocked also `namespace` and `web_search` | translate, or refuse naming the field |
| `tool_choice`, `parallel_tool_calls` | `auto`, `true` | pass |
| `text` | `format: json_schema` (strict, name, schema); `verbosity` on catalog models | `response_format` json_schema; verbosity where supported |
| `reasoning` | `{effort}` (catalog) or `{summary: auto}` (fallback) | `reasoning_effort` |
| `store`, `include` | `false`, `["reasoning.encrypted_content"]` | stateless; `include` matters only on passthrough |
| `prompt_cache_key` | the thread id | provider cache key / G21 hint |
| `client_metadata` | session, thread, turn, window and installation ids | drop; never forward |
| `stream` | `true` | always stream |

**Events Codex handles:** `response.created` (optional), `response.output_item.added` (optional), `response.output_text.delta`, `response.reasoning_summary_text.delta`/`.done`, `response.reasoning_text.delta`, `response.custom_tool_call_input.delta`, **`response.output_item.done` (required for every item)**, and **`response.completed` (required)**, or `response.incomplete` / `response.failed` / `error`.

### Bridge check (OSS first)

LiteLLM is already TokenLean's upstream client (MIT, `litellm>=1.95.1,<2`). At 1.95.1 it ships two relevant pieces:
- `litellm.aresponses()`, a native Responses passthrough;
- a Responses-to-chat bridge, `LiteLLMCompletionResponsesConfig`.

Fed the captured turn-2 body, the bridge produced:
- the system and developer/user messages, the assistant `tool_calls` and a `tool` message;
- `response_format` json_schema, `reasoning_effort`, `tool_choice` and `parallel_tool_calls`;
- the custom `apply_patch` as a function with one `content` parameter.

It dropped the encrypted reasoning, `prompt_cache_key` and `verbosity`. Its non-streaming reverse transform rebuilds `custom_tool_call` items, but a source search of its streaming iterator module finds no `custom_tool_call` handling. Codex always streams.

## Decision

1. **ST-4 (M2-32) is a Responses ingress adapter in TokenLean's existing adapter pattern.** It is a new `protocols/` adapter next to the Anthropic and Gemini ones, with its own `parse_request` and `StreamTranslator`, so every upstream provider and every G-group works unchanged. A config-driven native passthrough for Responses-capable upstreams is the second step (NEEDS OWNER, below). The full requirement list follows.
2. **The `codex` backend (M2-40) drives the pinned `codex exec` CLI with this lock-down.** Each item was proven above, and contract tests pin each one.
   - **Command:** `codex exec --json --ephemeral --strict-config -C <worktree> -m <model> --output-schema <file> -o <file> <-c overrides…> -`, with the prompt on stdin. `--strict-config` fails fast if a pinned key is unknown.
   - **Config:** a per-step `CODEX_HOME` holding the config below. **The same keys are also passed as `-c`**, so they beat a project layer. **`[projects."<worktree>"] trust_level = "untrusted"` is mandatory**: it is the only switch that keeps the repo's `.codex/` (instructions, MCP servers, rules) out.

     Abridged below, with several keys per line. The real file has one key per line; see `locked_config` in `spikes/s3_codex/probe.py`, which passed `--strict-config`.

     ```toml
     model = "<resolved>"
     model_provider = "tokenlean"
     approval_policy = "never"
     sandbox_mode = "danger-full-access"      # inside srt; see 3
     check_for_update_on_startup = false
     project_doc_max_bytes = 0
     web_search = "disabled"
     [history]   persistence = "none"
     [features]  plugins = false  apps = false  remote_models = false  memories = false  multi_agent = false  goals = false
     [analytics] enabled = false
     [feedback]  enabled = false
     [otel]      exporter = "none"  metrics_exporter = "none"  trace_exporter = "none"
     [shell_environment_policy] inherit = "core"
     [projects."<worktree>"] trust_level = "untrusted"
     [model_providers.tokenlean]
     name = "TokenLean"  base_url = "<tokenlean>/v1"  wire_api = "responses"  env_key = "TOKENLEAN_API_KEY"
     request_max_retries = <limits>  stream_max_retries = <limits>  stream_idle_timeout_ms = <limits>
     env_http_headers = { "X-Agentic-Run-Id" = "AGENTIC_RUN_ID", … one per identity header, "traceparent" = … }
     ```

   - **Environment:** `CODEX_HOME`, `TOKENLEAN_API_KEY`, the `AGENTIC_*` header variables, and the S2 blanking of provider switches, cloud credentials and secret-named variables (`OPENAI_*`, `CODEX_API_KEY` …).
   - **Rules:** `CODEX_HOME/rules/agentic.rules`, compiled from the guard:
     - deny → `forbidden` (with justification);
     - ask → `prompt`, which is refused under `never`;
     - allow → `allow`, but **only when srt is the boundary**, because `allow` bypasses Codex's sandbox.

     Each rule carries `match` examples, which Codex validates at load.
3. **Sandbox.** On macOS, Linux and WSL2 the whole Codex process runs under srt (D38), with only the TokenLean host reachable (DF-65), as for claude-code. Codex's own sandbox stays off (`danger-full-access`), so sandboxes are not nested. There is a second reason: Codex's Linux bwrap sandbox cannot create namespaces in an unprivileged container (every sandboxed command failed here), so it would break CI images. Forbidden and prompt rules still apply with the sandbox off (runs N).
4. **Windows.** Per D38 the CLI runs inside WSL2, using the Linux binary verified against the release sha256. Native Windows is not a target. If it ever is, use the signed standalone `codex-x86_64-pc-windows-msvc.exe`, **never the npm package**: it bundles an unsigned `rg.exe`, which breaks the no-unsigned-binaries rule.
5. **Cost.** As S2: the engine charges the step from `turn.completed.usage` priced by our `Pricer` (marked `estimated`). TokenLean's usage rows, tagged by run and step, are the reconciled truth.
6. **Capability matrix (M1-27), `codex`:**
   - `temperature` is unsupported (never sent).
   - `effort` is supported (`model_reasoning_effort` → `reasoning.effort`).
   - Tools: `run_shell`, `read_files` and `search` → `exec_command`; `edit_files` → `apply_patch` (catalog models) or `exec_command`.
   - `approvals`, `children` and `stuck_check` are false; `multi_agent` is off.
   - A new flag records that **the shell policy has no default-deny** and that prefix rules miss scripts with redirects. Policy then decides: warn, or block agents whose shell policy relies on "unlisted means refused".

## ST-4 requirements (for M2-32)

All generic and config-driven. Existing routes are byte-for-byte unchanged.

1. **Route and adapter.** `POST /v1/responses` as a new ingress protocol (`protocols/…responses…`, registered in `_BY_NAME`). Bearer proxy-key auth. Errors before the stream starts use an OpenAI error envelope with `Retry-After` kept. Other `/v1/responses/*` paths answer a clear 404.
2. **Request translation to the internal chat shape:**
   - `instructions` → system message;
   - `message` items (developer/user/assistant; `input_text`/`output_text`/`input_image`) → messages;
   - `function_call` → assistant `tool_calls`; `function_call_output` → `tool` message;
   - `custom_tool_call` / `custom_tool_call_output` → the same, with the string input wrapped;
   - `reasoning` items → dropped for chat upstreams (kept on passthrough);
   - `function` tools → function tools;
   - **`custom` tools → a function with one string parameter**, with the tool name remembered as custom for the reply;
   - `namespace` and hosted tools (`web_search` …) → 400 naming the field (the lock-down never sends them);
   - `text.format` json_schema → `response_format`; `text.verbosity` → `verbosity` where supported;
   - `reasoning.effort` → `reasoning_effort` (G12/G25 read it);
   - `tool_choice`, `parallel_tool_calls` → pass;
   - `prompt_cache_key` → the provider cache key and a G21 hint;
   - `store: false` honoured;
   - `client_metadata` and unknown fields dropped through the existing allow-list pattern (`ingress.extra_allowed_params`).
3. **Stream translation (`StreamTranslator`):** SSE with `event:` and `data:`:
   - at least `response.created`, `response.output_text.delta`, **one `response.output_item.done` per item** (message / `function_call` / **`custom_tool_call` for tools the request declared custom**: required, since Codex aborts a `function_call` for them), and a terminal **`response.completed` with `response.id` and `usage`**;
   - upstream failure mid-stream → `response.failed` with `error{code,message}`, never a silently closed stream (that makes Codex retry the whole call);
   - the non-stream path builds the same `response` object.
4. **Usage mapping.**

   | Chat field | Responses field |
   |---|---|
   | `prompt_tokens` | `input_tokens` |
   | cached tokens | `input_tokens_details.cached_tokens` |
   | cache-write tokens | `input_tokens_details.cache_write_tokens` |
   | `completion_tokens` | `output_tokens` |
   | reasoning tokens | `output_tokens_details.reasoning_tokens` |
   | — | `total_tokens` |

5. **Usage rows and cost:**
   - one `usage_events` row per upstream call, including retried, failed and truncated calls;
   - tagged with the `X-Agentic-*` identity (ST-2) and the model actually used;
   - `x-tokenlean-*` cost headers set as on the other routes (Codex ignores them; harmless).
6. **Stateless.** No response storage, `previous_response_id` or `GET /responses/{id}` is needed for Codex. A `previous_response_id` arriving gets a clear 400. Because Codex resends the whole history every turn, prefix caching (G21, provider caching) is where the savings are.
7. **Pipeline fit:**
   - G-groups run on the translated chat shape;
   - message-rewriting groups must keep `tool_calls`/`tool` pairs intact;
   - G11 validates json_schema outputs;
   - **G13 batch-defer (202) must never apply on this route**: Codex needs a stream.
8. **Headers.** Accept Codex's headers (`originator`, `session-id`, `thread-id`, `x-client-request-id`, `x-codex-*`) and never forward them or `client_metadata` upstream.
9. **Not needed:** WebSocket transport, `/responses/compact`, `/models`.
10. **Passthrough (second step, config-driven).** For providers flagged Responses-native (OpenAI, Azure OpenAI), relay allow-listed fields to upstream `/v1/responses` through `litellm.aresponses()`. That keeps encrypted reasoning items and grammar-constrained custom tools, and takes usage from upstream `response.completed`.
11. **Tests:**
    - adapter unit tests from Codex-shaped fixtures, regenerated by running this probe against the pinned Codex (`out/` is git-ignored);
    - the stream contract tests, written against LiteLLM's mapping;
    - a Codex smoke (the M2-32 done-when).

## What it cannot do (even when locked)

- **Shell policy:**
  - no default-deny for unlisted commands;
  - prefix rules miss scripts with redirects or substitutions;
  - `allow` escapes Codex's sandbox.

  The srt network and filesystem walls are the real boundary, as spec §shell-policy already says. The Zyroflo guard's `deny_commands` and diff-budget walls do not hook into Codex's exec path; Codex hooks might, but that was not tested.
- **Reasoning continuity is lost** when TokenLean translates to a chat upstream: OpenAI reasoning models then lose reasoning between turns. Passthrough fixes this for OpenAI and Azure only.
- **No `apply_patch` for non-catalog models.** Models outside Codex's bundled catalog, which includes every non-OpenAI model through TokenLean, get fallback metadata: no `apply_patch`, so edits go through shell, and a warning appears on each run. `model_catalog_json` can describe org models; not tested.
- **No temperature, no child agents, no approval pause.**
- **No repo-level Codex customisation** (`AGENTS.md`, `.codex/`). The framework injects instructions itself.
- **Plugins are off.** The full superpowers plugin opt-in (M2-47) needs `features.plugins`, which brings back the `chatgpt.com`/`github.com` fetches.

## Not tested (carried to M2-40 / M2-32)

- A real model, and a real TokenLean.
- macOS, WSL2, and Codex under srt: nesting, and whether Codex's helper-binary setup works there (it warned "could not create PATH aliases" under a temp `CODEX_HOME`).
- Windows signature-chain validation on a Windows host; only the signer was read.
- `model_catalog_json`, Codex hooks as a guard, long sessions with local compaction, images, MCP.
- LiteLLM's streaming bridge at runtime (a source search only).

## Cost if wrong

The backend sits behind the backend protocol. If Codex's CLI flags or config keys change, `--strict-config` and the pinned-version contract tests fail before a developer sees it. The ST-4 adapter is additive: a new route and adapter, no change to existing routes. If translation proves too lossy for OpenAI reasoning models, passthrough is a per-provider config switch, not a redesign.
