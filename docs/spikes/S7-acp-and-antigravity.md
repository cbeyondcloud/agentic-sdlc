# S7 — ACP adapter stability and the Antigravity CLI check

**Date:** 2026-10-20 · **Code (throwaway):** `spikes/s7_acp/` (`Dockerfile`, `stub.py`, `probe.py`, `agy_headless.py`, `show.py`; raw results in the git-ignored `out/`) · **Platform:** a Linux x86 container run with `--network none`; Windows 11 for the signature checks only.

**Versions:** each is the newest release at least two weeks old on 2026-10-20, except OpenHands, whose newest release is 1.16.0.

| Component | Version | Released |
|---|---|---|
| `agent-client-protocol` (Python ACP SDK) | 0.12.1 | 2026-08-16 |
| `@google/gemini-cli` | 0.63.0 | 2026-10-06 |
| `opencode-ai` | 1.18.35 | 2026-10-06 |
| goose | v1.53.0 | 2026-10-02 |
| `@github/copilot` | 1.0.92 | 2026-10-05 |
| Antigravity CLI (`agy`) | 1.3.0 | 2026-10-06 |
| Antigravity ACP server | 1.3.0 | entered the ACP registry 2026-10-02 |
| OpenHands CLI | 1.16.0 | 2026-05-08 |

## Question

Tracker row S7 asks two things. Both feed phase 2 (E2-06: an ACP backend for Gemini CLI, OpenCode, Goose and Copilot CLI) and D32.

1. **Is ACP stable enough to be our one adapter for third-party agent CLIs?** For each agent we care about:
   - Can every model call be forced through TokenLean, with our identity and the model policy chose?
   - Can tools, permissions and settings be locked down the way S2 did for Claude Code?
   - What about structured output, telemetry, licence and terms, and Windows (signed binaries) and macOS support?
2. **Antigravity (D32): which of three is it?**
   - usable as `acp:antigravity`;
   - needs a dedicated adapter;
   - not viable.

The coordinator added OpenHands (`openhands acp`), which S4 had left as an optional `acp:openhands` backend.

## Method

- **Desk research**, from official sources where they exist; every source is listed at the end, accessed 2026-10-20:
  - the ACP site, schema and announcements;
  - the ACP registry JSON;
  - PyPI, npm and GitHub release metadata, for versions and dates;
  - each vendor's documentation and terms.
- **Hands-on probe** (in the S2/S3 style), all inside one `--network none` container; nothing was installed on the host, no real key was used and no real provider was called.
  - **Model stub:** a local stand-in for TokenLean that speaks three wire formats:
    - OpenAI chat-completions;
    - OpenAI Responses;
    - the Gemini API (`/v1beta/models/{model}:streamGenerateContent`).

    It logs every request (path, headers, model, tools offered, and whether a planted `AGENTS.md`/`GEMINI.md` marker reached the prompt). On the first turn it answers with a shell tool call: `touch s7-tool-ran.txt`, or `echo s7-tool-ran` for the auto-approve check.
  - **Refusing proxy:** set as `HTTPS_PROXY`, it logs every other host an agent tries to reach.
  - **ACP client:** written with the Python SDK. It:
    - spawns each agent over stdio;
    - runs `initialize`, `authenticate` where required, `session/new` and one `session/prompt`;
    - records every `session/update`;
    - answers every `session/request_permission` with a **reject** option.

    So the tool's side effect (the file) appears only if the agent ran the tool without asking.
  - **Each agent gets:**
    - a fresh `HOME` and XDG folders;
    - its own launch profile (command, environment variables, files);
    - a working folder containing a planted `AGENTS.md`, `GEMINI.md` and `.goosehints`.
  - **Follow-up runs:**
    - a path prefix on the base URL;
    - model switching via `session/set_config_option`;
    - a hostile project `opencode.json`;
    - Gemini's admin policy;
    - Antigravity's headless CLI;
    - OpenHands.
- **Windows signatures:** the Windows executables were extracted in a container into a scratch folder, checked with `Get-AuthenticodeSignature` without being run, then deleted.

## Results

### ACP itself

**Protocol.** ACP is at **v1**; the SDK's `PROTOCOL_VERSION` is the integer 1, and the v1 schema is at 1.23.0. New features are added as optional, capability-gated additions, each stabilized through an RFD:

| Date | Feature stabilized or released |
|---|---|
| 2026-02-04 | Session config options |
| 2026-03-09 | ACP Registry |
| 2026-04-22 | Session resume |
| 2026-06-05 | Session usage updates and message IDs |
| 2026-06-24 | Model config category |
| 2026-06-29 | Request cancellation |
| 2026-07-22 | Elicitation |
| 2026-09-17 | Tool call names |

- **Reference SDKs:** the Rust and TypeScript SDKs reached 1.0 on 2026-06-25; the TypeScript SDK is now 2.0.0 (2026-10-19).
- **v2:** "ACP v2 available in Draft" was announced 2026-07-20 with no stabilization date. The announcement says "Don't ship it by default in production until we are closer to stabilization", and that "adding v2 support should not mean dropping v1".
- **Our use:** we negotiate v1 only.

**Python SDK `agent-client-protocol`.**

| Aspect | Detail |
|---|---|
| Licence | **Apache-2.0** (the LICENSE file in the wheel; the PyPI licence field is empty) |
| Python | ≥3.10, <3.15 |
| Core dependency | only `pydantic>=2.7` (the `http` extra adds httpx and websockets) |
| Release cadence | about monthly: 0.7.0 (2025-12-04) through 0.12.1 (2026-08-16) |

1.0 is in release candidates:
- **1.0.0rc1** (2026-09-11): removes the schema-helper API.
- **1.0.0rc2** (2026-09-21): adds v2-draft support; **moves from httpx to httpx2**, which matches D36.
- **1.0.0rc3** (2026-10-12).

Every agent probed worked through 0.12.1's `spawn_agent_process` client with no protocol errors.

**What ACP does not standardize** (seen in the probe):
1. **Pointing an agent at a proxy, and forcing its model.**
   - The "Configurable LLM Providers" RFD (`providers/list|set|disable`, with `baseUrl` and `headers`) is still a draft (2026-02-02, revised 2026-04-19). **No agent probed advertises it.**
   - Gemini CLI offers a non-standard `gateway` auth method instead.
   - Routing is per-agent environment variables and config files.
2. **A structured, schema-checked final result.**
   - ACP returns streamed message chunks and a `stopReason`.
   - We must ask for JSON in the final message and validate it, or use the agent's own headless mode.
3. **Cost.**
   - `usage_update` (context used and size) came from Goose, Copilot and Antigravity only.
   - Gemini puts token counts in a non-standard `_meta.quota` on the prompt response.
   - OpenCode reported nothing.
4. **Tool restriction.** It is per-agent configuration. The ACP permission callback sees only the calls the agent chooses to ask about (see the Gemini row below).

### Per-agent probe (ACP v1, against the stub)

| | Gemini CLI 0.63.0 | OpenCode 1.18.35 | Goose 1.53.0 | Copilot CLI 1.0.92 | Antigravity ACP server 1.3.0 |
|---|---|---|---|---|---|
| ACP entry and maturity | `gemini --acp` (`--experimental-acp` deprecated); in the ACP registry | `opencode acp --pure`; docs: some slash commands unsupported | `goose acp` | `copilot --acp --stdio`, **public preview** | Separate Google binary `agy_acp_server`; registry id `antigravity-acp`, author Google LLC. **The `agy` CLI has no ACP mode.** |
| Auth over ACP | Must call `authenticate("gemini-api-key")` | none needed | none needed | none (BYOK needs no GitHub login) | Must call `authenticate("gemini-api-key")` |
| Model calls reach TokenLean | **Yes.** `GOOGLE_GEMINI_BASE_URL`, Gemini API wire format | **Yes.** `OPENCODE_CONFIG_CONTENT` custom provider (`@ai-sdk/openai-compatible`, `options.baseURL`) with `enabled_providers`; chat-completions | **Yes.** `GOOSE_PROVIDER=openai`, `OPENAI_HOST` + `OPENAI_BASE_PATH`; chat-completions; also `GET /v1/models` | **Yes.** `COPILOT_PROVIDER_BASE_URL` / `_TYPE` / `_API_KEY`; chat-completions (Responses optional) | **Yes.** `GOOGLE_GEMINI_BASE_URL` + `GEMINI_API_KEY`; Gemini API wire format |
| Our `X-Agentic-*` headers | **Yes**, via `GEMINI_CLI_CUSTOM_HEADERS` | **Yes**, via provider `options.headers` | **Yes**, via `OPENAI_CUSTOM_HEADERS` | **Yes**, via `COPILOT_PROVIDER_HEADERS` | **No documented way.** The binary contains undocumented `AGY_LLM_GATEWAY_URL` / `_HEADERS` / `_MODELS` strings; guessed formats did not work. |
| Path prefix on the base URL survives | yes | (not run) | yes | yes | **yes**: `/agentic/<run>/<step>/v1beta/...` arrived intact |
| Model forced | Yes: any name (`GEMINI_MODEL` / system settings) | Yes: the only model offered; set `small_model` too, because the first call is a title call | Yes; the model picker also lists the provider's other models | Yes (`COPILOT_MODEL`) | **Only Google's list** (`gemini-3.8-flash-high`, …, `gemini-3.1-pro-low`), chosen via `session/set_config_option`, which works; mapped to wire names such as `gemini-3.1-pro-preview` |
| Asked our client before `touch` | yes; reject held, no side effect | yes (with `"*": "ask"`); reject held | yes (`GOOSE_MODE=approve`); reject held | yes; reject held | yes; reject held |
| Asked before `echo` | **No: ran it without asking**, even with an Admin-tier `ask_user` rule for `run_shell_command`. The docs say the shell defaults to `ask_user`. | yes | yes | yes | yes |
| Planted `AGENTS.md` reached the prompt | yes, when the folder is trusted (folder trust off) | yes | yes (`.goosehints` / `AGENTS.md`) | yes | **no** |
| Other hosts tried (refused by the proxy) | none with system settings `privacy.usageStatisticsEnabled=false`. **3× `play.googleapis.com`** when those settings were skipped, which happens when their parent folder is world-writable. | 2× `registry.npmjs.org`, even with `--pure` and `OPENCODE_DISABLE_*`; the run still worked | 1× `models.dev` | none (`COPILOT_OFFLINE=true`) | none seen through the proxy |
| Session controls exposed | modes: default / autoEdit / yolo / plan | config options: model, mode; sessions: list / fork / resume / close | modes: auto / approve / smart_approve / chat; config options: provider, model, thinking effort | modes: agent / plan / autopilot; option `allow_all` | modes: default / auto_edit / yolo; config options: model, mode |
| Tools offered (count) | 15, including `google_web_search`, `web_fetch`, `invoke_agent` | 10 | 17, including `extensionmanager__manage_extensions` (the agent can enable extensions) | 14 | 14, including `search_web`, `generate_image`, `schedule`, `define_subagent` |

**Hostile project config (OpenCode).** We planted an `opencode.json` that sets every permission to `allow`, points at another model and base URL, and adds an MCP server and `instructions`.
- Our inline config kept the model, provider and the permission keys it sets.
- **The project file still merged in its MCP server (a local command) and its `instructions`.**
- Precedence documented on opencode.ai/docs/config: inline config sits above the project config but below managed config, and `.opencode/` plugin folders are loaded too.

**Antigravity headless CLI (`agy -p --output-format json [--json-schema]`, 1.3.0).**
- Routing works through `GOOGLE_GEMINI_BASE_URL`.
- It returns a JSON envelope with `conversation_id`, `status`, `response` and `usage`.
- A tool call with no approval flag was refused.
- It also tried to reach `antigravity-unleash.goog` (feature flags), `play.googleapis.com` (telemetry) and an `antigravity-cli-auto-updater` host on every run.
- Guessed `telemetryEnabled` / `autoUpdate` settings did not stop that, and no documented switch exists. The changelog only says telemetry shows as off for Business accounts and under ADC sign-in (1.4.1).
- It releases almost daily: 71 GitHub releases between 2026-06-09 and 2026-10-20.

**OpenHands CLI 1.16.0.**
- **Maintenance:** the README says "This project is no longer actively maintained" and points to Agent Canvas, which is an ACP *client* that drives other agents. The last release was 2026-05-08.
- **Binaries and Windows:** binaries ship for Linux and macOS only; the docs say the CLI "only runs on Linux, macOS, or Windows with WSL".
- **Stale dependencies:** it pins `agent-client-protocol <0.9` (Feb 2026 schema) and `openhands-sdk==1.21.0` (the SDK is now 1.56.0).
- **Not in the ACP registry.**
- **Probe failure:**
  - `openhands acp --override-with-envs` with `LLM_BASE_URL`/`LLM_MODEL`/`LLM_API_KEY` failed `session/new` with "Authentication required". Its source shows why: the ACP path loads agent specs without the environment override, so it needs a stored `~/.openhands/agent_settings.json`.
  - That file holds the API key in plain text (it is saved with `expose_secrets`).
- **The coordinator's questions:**
  - Custom headers through LiteLLM on streaming or Responses calls could not be tested, because no session started. The CLI has no environment variable for extra headers.
  - `--headless` always auto-approves, as S4 found.
  - The plain `httpx` dependency would not clash with our httpx2, because it runs as a separate process. That is moot now.

### Platforms, signing and terms

| Agent | Licence and terms | Windows native | Windows binary signature (`Get-AuthenticodeSignature`) | macOS | Release cadence |
|---|---|---|---|---|---|
| Gemini CLI | Apache-2.0. Since 2026-06-18 it has been replaced by Antigravity for unpaid and Google One users. It is still released weekly for API-key and enterprise users, and is "not optimized for Gemini 4 Argon". | Yes: Windows 11 24H2+, PowerShell, Node 20+ | No own binary (JavaScript on Node). Native npm add-ons not checked. | Yes | weekly |
| OpenCode | MIT | Yes (Git Bash path via `OPENCODE_GIT_BASH_PATH`) | **Valid**: Anomaly Innovations, Inc. | Yes | several per week |
| Goose | Apache-2.0 (Linux Foundation AAIF) | Yes | **Standalone CLI zip: NotSigned.** The desktop bundle's `goose.exe`: **Valid**, LF Open Source, LLC | Yes | about weekly |
| Copilot CLI | Proprietary "GitHub Copilot CLI License": redistribution only unmodified, inside a larger app; use is subject to GitHub's and Copilot's terms | Yes | **Valid**: GitHub, Inc. | Yes | near-daily |
| Antigravity CLI and ACP server | **Proprietary** (registry `license: proprietary`, antigravity.google/terms). The terms collect "Interactions" data, with an opt-out, and prohibit "using third party software, tools, or services to access the Service". For enterprise, the admin's terms apply. | Yes: x64 and arm64, CLI and ACP server | **Valid**: Google LLC (CLI `antigravity.exe` and `agy_acp_server.exe`) | Yes | CLI near-daily; ACP server about every 2 weeks (registry 1.1.1 on 09-03 to 1.4.1 on 10-17) |
| OpenHands CLI | MIT | **No** (WSL only) | no Windows binary | Yes | unmaintained |

Also noted, from npm and the registry:
- **Claude adapter:** it has moved to `@agentclientprotocol/claude-agent-acp`, now 0.94.0 and listed as **proprietary**. The old `@zed-industries` 0.23.1 package was Apache-2.0.
- **Codex adapter:** `codex-acp` is now 2.2.2, Apache-2.0.

We need neither: native `claude-code` and `codex` backends exist.

## ACP adapter verdict

**Stable enough. Adopt it as the single phase-2 adapter, with conditions.**

1. **Pinning.**
   - ACP v1 is stable and changes only by optional, negotiated additions; v2 is a draft we do not enable.
   - Every agent probed implements v1 the same way for what we need:
     - initialize, session/new, prompt;
     - streamed tool calls;
     - permission requests;
     - modes and config options.
   - Pin the Python SDK at **0.12.1** (Apache-2.0, pydantic only). Move to 1.x, which uses httpx2, once 1.0 is out and at least two weeks old.
2. **The rest stays per-agent data.** One `AcpBackend`, plus one **profile per agent**. A profile holds:
   - the launch command and arguments;
   - the environment variables;
   - the files to write into a per-step home;
   - the auth method id;
   - the mode to set;
   - the egress switches;
   - the mapping from our tools to the agent's own policy format;
   - the result extraction.

   Vendor names live only in profiles (provider-agnostic rule).
3. **The ACP permission callback is a second gate, never the only one.**
   - Gemini ran `echo` without asking.
   - Each profile must also compile our shell policy into the agent's own policy.
   - The OS sandbox (`srt` / container, D38) stays the real boundary.
4. **Results and cost.**
   - **Results** come from the final message: JSON requested, validated, and retried through the existing retry path.
   - **Cost** comes from TokenLean's usage rows (as for claude-code in S2), not from ACP usage events.
5. **Identity.**
   - Use headers where the agent supports them (Gemini, OpenCode, Goose, Copilot).
   - Otherwise use a **base-URL path prefix**, which every agent tested preserved. That needs a TokenLean route that reads run and step tags from the path.

**Per-agent readiness for E2-06:**

| Agent | Readiness |
|---|---|
| Goose | Ready. Use the signed desktop-bundle `goose.exe` on Windows. Restrict its extension manager. |
| Copilot CLI | Ready technically (BYOK plus offline mode is the cleanest lock-down). Terms need checking. |
| Gemini CLI | Ready, with the auto-approve gap. It needs a TokenLean inbound Gemini API route. |
| OpenCode | Ready; see M2-57 below. |
| Antigravity | Conditional (next section). |
| OpenHands | Not viable (below). |

## Antigravity verdict

**Usable as `acp:antigravity` through Google's official ACP server, not through the `agy` CLI. No dedicated adapter. Lowest priority, and conditional.**

Why it is usable:
- It speaks ACP v1, published by Google LLC in the ACP registry.
- It is signed on Windows and built for macOS and Linux.
- It accepts API-key auth with `GOOGLE_GEMINI_BASE_URL`, and every model call reached the stub.
- It asks our client before running tools, and a reject holds.
- It made no other egress attempts through the proxy.

Conditions:
1. **TokenLean must accept the Gemini API wire format** (`/v1beta/models/*:streamGenerateContent`). The same route serves Gemini CLI.
2. **No custom headers.** Run and step identity must come from a base-URL path prefix, which survived, through a TokenLean route that reads it.
3. **Models are limited to Google's Gemini list**, set per session. Policy must treat this backend as Gemini-family only. Making TokenLean map the names to another provider would be a hack, and we do not plan it.
4. **Terms.** The software is proprietary, collects Interactions data (with an opt-out), and its terms prohibit accessing "the Service" with third-party software. Using Google's own ACP server, with an API key, against our gateway is the officially published path. Even so, legal review is needed before any org enables it.
5. **The ACP server is undocumented** on antigravity.google: the registry entry is the only source. A pinned version and contract tests are mandatory.

Why not a dedicated headless adapter:
- `agy -p --output-format json --json-schema` works.
- But it calls Google feature-flag, telemetry and updater hosts that no documented switch turns off.
- It ships almost daily.

What Antigravity cannot do for us:
- non-Gemini models;
- our identity headers;
- project `AGENTS.md` context (not loaded in ACP mode);
- an admin lock-down we can verify. Its permission and sandbox settings are its own, and largely undocumented for the ACP server.

## OpenHands verdict

**Not viable now. This withdraws S4's "optional `acp:openhands` backend".**
- The CLI is unmaintained, and Windows needs WSL.
- In ACP mode it ignores environment-only LLM settings and needs a stored file holding the API key in plain text.
- Revisit only if OpenHands ships a maintained ACP agent.
- S4's other OpenHands conclusions are unaffected: do not adopt the product; borrow risk-rule ideas only.

## Implications

**M2-57 (`opencode` backend).**
- **Use the ACP client, not `opencode run --format json`.** Build the generic `AcpBackend` with OpenCode as its first profile. Why:
  - OpenCode has no JSON-schema output in either mode, so ACP loses nothing.
  - ACP gives permission callbacks: with them the backend can pause for a person (`approvals` becomes possible) instead of auto-approving or failing.
  - E2-06 then shrinks to adding profiles.
- D32's "dedicated adapter" becomes "a dedicated profile plus contract tests on the shared ACP backend". If ACP misbehaves for OpenCode, swap the transport to `opencode run` inside the same module.
- **Lock-down the probe proved:**
  - `OPENCODE_CONFIG_CONTENT` with a custom provider, `baseURL` and `headers`;
  - `enabled_providers` set to that provider only;
  - `model` and `small_model` set to the resolved model;
  - `"*": "ask"` plus explicit keys;
  - `--pure`;
  - `OPENCODE_DISABLE_AUTOUPDATE`, `_MODELS_FETCH`, `_LSP_DOWNLOAD`, `_DEFAULT_PLUGINS` and `_CLAUDE_CODE`;
  - a per-step `HOME`/XDG.
- **The project-config merge must be closed:**
  - set every permission key explicitly;
  - neutralize project `mcp`, `instructions` and `.opencode/` plugins (pre-scan the worktree, or managed config);
  - block egress (`registry.npmjs.org` was tried) with the sandbox.
- **Windows:** `opencode.exe` is signed.

**E2-06 (ACP backend).** Profiles for:
- Goose;
- Copilot CLI (if terms allow);
- Gemini CLI;
- Antigravity (if its conditions are met).

Contract tests pinned per agent version must cover:
- routing, headers or path prefix;
- the forced model;
- permission rejects for a plain command and a "read-only" one;
- a hostile project config;
- the egress list.

## Decision

1. **Adopt** ACP v1 with `agent-client-protocol` 0.12.1 (Apache-2.0) as the shared adapter for third-party agent CLIs. It is an optional extra, not a core dependency.
2. **Build M2-57 on it**, with OpenCode as the first profile.
3. **Antigravity** is `acp:antigravity` through Google's official ACP server. It is conditional on:
   - a TokenLean Gemini route;
   - path-prefix identity;
   - legal review of the terms.

   It is the lowest-priority profile in E2-06. There is no dedicated adapter.
4. **OpenHands** is not viable as a backend; S4's recommendation is withdrawn.
5. **The client permission callback is defence in depth.** Agent-native policy and the OS sandbox stay mandatory for every ACP profile.

## Not tested

- **No run on the Windows host.** Nothing could be installed there. In particular, ACP over stdio with Windows' asyncio subprocess loop is untested.
- **No real model and no real TokenLean.** The stub's text answers never satisfied a JSON schema, so Antigravity's `--json-schema` path ended `SUCCESS` without `structured_output`: inconclusive.
- **Egress that ignores `HTTPS_PROXY`.** It was blocked by `--network none` but not logged.
- **`opencode run` non-interactive behaviour on `ask`.**
- **Goose's `GOOSE_ALLOWLIST`.**
- **Copilot's `--available-tools` under ACP.**
- **Gemini's undocumented read-only auto-approve:** the setting that turns it off was not found.
- **macOS.**

## Cost if wrong

- **The adapter:** ACP sits behind the `Backend` protocol. If a profile misbehaves, that agent falls back to its own headless JSON mode in the same module; nothing in the engine changes.
- **The SDK:** pinned, and its core needs only pydantic, so replacing it means touching one module. Each profile is data plus pinned contract tests, so an agent release that changes a flag fails before reaching developers.
- **Antigravity:** if its terms or the missing headers block it, dropping it costs nothing, because nothing was built for it alone.

## Sources (accessed 2026-10-20)

- ACP:
  - updates and announcements: https://agentclientprotocol.com/updates.md
  - v2 draft: https://agentclientprotocol.com/announcements/acp-v2-draft.md
  - LLM providers RFD: https://agentclientprotocol.com/rfds/custom-llm-endpoint.md
  - agents: https://agentclientprotocol.com/get-started/agents.md
  - registry: https://agentclientprotocol.com/get-started/registry.md and https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json
  - Antigravity entry history: https://github.com/agentclientprotocol/registry (commits to `antigravity-acp/agent.json`)
- Python SDK:
  - https://github.com/agentclientprotocol/python-sdk
  - releases: https://github.com/agentclientprotocol/python-sdk/releases
  - https://pypi.org/project/agent-client-protocol/
- Gemini CLI:
  - https://geminicli.com/docs/cli/cli-reference/
  - https://geminicli.com/docs/cli/acp-mode/
  - https://geminicli.com/docs/cli/enterprise/
  - https://geminicli.com/docs/reference/policy-engine/
  - https://geminicli.com/docs/get-started/installation/
  - https://geminicli.com/docs/cli/telemetry/
  - npm `@google/gemini-cli`
- OpenCode:
  - https://opencode.ai/docs/acp/
  - https://opencode.ai/docs/providers/
  - https://opencode.ai/docs/permissions/
  - https://opencode.ai/docs/config/
  - https://opencode.ai/docs/cli/
  - releases: https://github.com/anomalyco/opencode/releases
- Goose:
  - https://goose-docs.ai/docs/guides/environment-variables
  - https://goose-docs.ai/docs/gdk/acp/
  - https://goose-docs.ai/docs/guides/acp-providers
  - releases: https://github.com/aaif-goose/goose/releases
- Copilot CLI:
  - https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server
  - https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-byok-models
  - `copilot help environment` (1.0.92)
  - the package's `LICENSE.md`
- Antigravity:
  - https://antigravity.google/docs/install/
  - https://antigravity.google/docs/ide/extensions
  - https://antigravity.google/terms
  - https://github.com/google-antigravity/antigravity-cli (README, CHANGELOG, releases)
  - forum question on ACP and terms (reply not verifiably from Google): https://discuss.ai.google.dev/t/clarification-request-is-local-ipc-pty-wrapping-of-agy-cli-for-open-standard-editor-bridges-e-g-acp-tos-compliant/175905
- OpenHands:
  - https://github.com/OpenHands/OpenHands-CLI (README, releases, `openhands_cli/stores/agent_store.py`, `openhands_cli/acp_impl/agent/local_agent.py`)
  - https://docs.openhands.dev/openhands/usage/run-openhands/acp
  - https://docs.openhands.dev/openhands/usage/cli/command-reference.md
  - https://pypi.org/project/openhands/
