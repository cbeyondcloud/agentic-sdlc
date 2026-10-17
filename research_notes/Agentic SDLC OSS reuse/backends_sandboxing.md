# External coding agents as pluggable backends, and sandboxing for code-editing agents (research as of 16 Oct 2026)

Scope: what a Python 3.12 agentic SDLC framework can reuse to (1) drive external coding agents (Claude Code, Codex CLI, Gemini CLI, Aider, OpenCode, Goose, Copilot CLI, remote Devin/Copilot coding agent/Jules) as execution backends routed through the TokenLean proxy, and (2) sandbox code-editing steps (worktree/container, allow-listed shell, reviewable diff, rollback) on Windows, Linux, macOS and CI.

Note on sources: most flag-level facts come from official docs fetched on 2026-10-16. Where only third-party sources were available, this is stated.

---

## 1. Standard protocols for driving agents (ACP, A2A, MCP, AG-UI): could ACP give one adapter for many CLI agents?

### Takeaway
ACP (Agent Client Protocol, from Zed) is the only protocol built for exactly this job: a client spawns a local coding agent over stdio JSON-RPC and drives sessions with it. As of Oct 2026 about 43 agents speak it, natively or through adapters, including Claude (via `claude-agent-acp`), Codex (via `codex-acp`), Gemini CLI, Copilot CLI (preview), Goose, OpenCode and OpenHands, and there is an Apache-2.0 Python SDK. That makes ACP a good *common* adapter for streaming and session control. It does not replace per-agent configuration for forcing the model, routing through the proxy, or restricting tools, which stays agent-specific (env vars and config files). A2A suits remote agent-to-agent calls, AG-UI suits the agent-to-UI front end, and MCP is the tool layer, not a way to drive agents.

### Cited Findings
**ACP: which agents implement it**
- The ACP agents page lists 43 agents, including AgentPool, Augment Code, Blackbox AI, Claude Agent (via Zed's SDK adapter `claude-agent-acp`), Cline, Codex CLI (via ACP's `codex-acp` adapter), Cursor, Docker's cagent, fast-agent, Factory Droid, Gemini CLI, GitHub Copilot (public preview), Goose, Junie (JetBrains), Kimi CLI, Kiro CLI, Mistral Vibe, OpenCode, OpenHands, Pi (via `pi-acp`), Qwen Code and others — [ACP agents](https://agentclientprotocol.com/overview/agents)
- ACP clients include VS Code extensions (ACP Client, ACP Patchbay, ACP Pro Extension, Multicoder, Poolside Assistant, Exo), JetBrains, Neovim (CodeCompanion, agentic.nvim, avante.nvim, hermes.nvim), Emacs (agent-shell.el), Zed, Visual Studio (via Poolside) and others — [ACP clients](https://agentclientprotocol.com/overview/clients)
- Orchestrators and programmatic ACP clients already exist: **Jockey** ("multi-agent orchestrator that coordinates Claude Code, Gemini CLI, and Codex CLI via ACP"), **Raven** (splits a task into a DAG and runs each node on an ACP agent it launches), CompozyOS, Kronos, and the CLI clients `acpx` and Hash. Frameworks with ACP support include AgentPool, Koog, Mastra and LangChain/LangGraph (Deep Agents) — [ACP clients](https://agentclientprotocol.com/overview/clients)
- Python SDK: `pip install agent-client-protocol` (repo `agentclientprotocol/python-sdk`). It provides Pydantic models mirroring the ACP schema, async base classes and JSON-RPC plumbing for both agents and clients, and its `examples/` include clients and Gemini CLI bridges — [ACP Python library](https://agentclientprotocol.com/libraries/python)
- The Python SDK is licensed Apache-2.0 and had about 338 stars. Its releases "track the upstream ACP schema", but the README states no protocol version number — [GitHub python-sdk](https://github.com/agentclientprotocol/python-sdk)
- Per-agent ACP entry points: Gemini CLI `--experimental-acp` (still marked experimental) — [Gemini CLI reference](https://geminicli.com/docs/cli/cli-reference/); `opencode acp` ("communicates over stdin/stdout using nd-JSON", with `--cwd`) — [OpenCode CLI](https://opencode.ai/docs/cli/); Goose's repo includes an ACP test client and lets users connect existing subscriptions through ACP — [goose GitHub](https://github.com/block/goose); the Copilot CLI docs mention an "ACP server mode" (in which `--fleet` is not supported) but don't document the flag that starts it — [Copilot CLI programmatic reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference)

**A2A**
- A2A reached v1.0 on 12 March 2026. It is governed under the Linux Foundation by a TSC of AWS, Cisco, Google, IBM Research, Microsoft, Salesforce, SAP and ServiceNow (secondary sources) — [chatforest A2A v1](https://chatforest.com/guides/a2a-protocol-v1-production-ready/), [rywalker A2A](https://rywalker.com/research/google-a2a)
- Python SDK `a2a-sdk` implements spec 1.0 (with a 0.3 compatibility mode) over JSON-RPC, HTTP+JSON/REST and gRPC, for both client and server. Version 1.1.2 appears on PyPI — [PyPI a2a-sdk](https://www.pypi.org/project/a2a-sdk)

**AG-UI**
- AG-UI is CopilotKit's open, event-based protocol between front-end apps and agent backends, with roughly 16 standard event types and TypeScript and Python SDKs (secondary source) — [skills.sh AG-UI](https://www.skills.sh/akillness/oh-my-gods/ag-ui)
- Pydantic AI ships an AG-UI integration that depends on the `ag-ui-protocol` Python package — [Pydantic AI AG-UI](https://ai.pydantic.dev/ag-ui)
- Claimed adopters (Google, Microsoft, Amazon, Oracle, LangChain, Mastra, PydanticAI) come from a directory-style page and are unverified — [yespress CopilotKit](https://yespress.io/copilotkit.md)

### Inferences
- **ACP as the "one adapter"**: one `AcpBackend` class built on `agent-client-protocol` could start any ACP agent subprocess (`gemini --experimental-acp`, `opencode acp`, `npx claude-agent-acp`, `codex-acp`, `goose acp`) and normalize prompts, streamed updates, tool-call events, permission requests and cancellation. The client side of ACP also answers the agent's permission requests, which gives a second, uniform enforcement point for tool allow-lists on top of each agent's own config.
- ACP is not enough on its own. It doesn't standardize model selection, the proxy base URL, the final structured result (JSON schema) or cost reporting. Each backend still needs an env/config "profile" and a result-extraction step. For CI batch steps, each agent's native headless JSON mode (section 2) is simpler and better documented than ACP, so a dual-mode adapter makes sense: `exec` mode (native headless JSON) and `acp` mode (interactive and streaming, also usable from a VS Code front end).
- Use A2A to expose *your* pipeline agents to other orgs and systems, or to call remote agent services. It is not a fit for driving local CLIs. AG-UI fits the planned VS Code or web front end that streams pipeline state to users.
- Use MCP as the tool layer: give external agents your tools (repo context, ticket lookup, artifact store) through `--mcp-config` (Claude), `mcp_servers` (Agent SDK), or the equivalent options in other agents.
- ACP's ecosystem is broad but young. Several agents are ACP-capable only through third-party adapters (Claude, Codex, Pi), and Gemini's flag is still "experimental", so pin adapter versions.

### Gaps
- I found no official statement of ACP's current protocol version or stability status on the pages fetched.
- I didn't verify the licences and maintenance of `claude-agent-acp` and `codex-acp`. Check their repos.
- I didn't confirm the exact flag that starts Copilot CLI's ACP server mode.
- I found no primary-source AG-UI version or adoption data.

---

## 2. Per-agent headless control: model forcing, proxy routing, tool restriction, JSON output, licences

### Takeaway
Claude Code and Codex CLI are the most governable backends: both have documented headless JSON output with JSON Schema-constrained final results, both can be pinned to a custom endpoint (Claude with `ANTHROPIC_BASE_URL` plus a managed `allowedProviders: ["customEndpoint"]` lock; Codex with a `[model_providers.X]` `base_url`), and both have fine-grained tool and command policy. Gemini CLI is technically capable (JSON output, `GOOGLE_GEMINI_BASE_URL`, Policy Engine) but carries strategic risk, because Google replaced it with the closed-source Antigravity CLI for unpaid and Google One users on 18 June 2026. OpenCode (MIT), Goose (Apache-2.0, now under the Linux Foundation's AAIF) and Aider (Apache-2.0) are good OSS backends. Aider's Python API is officially unsupported. Each agent needs a specific wire format from TokenLean: Anthropic Messages for Claude, OpenAI Responses for Codex, Gemini API for Gemini CLI, and OpenAI chat-completions for Goose, OpenCode and Aider.

### Cited Findings
**Claude Code (`claude -p`) and the Claude Agent SDK (Python)**
- Headless: `claude -p "<task>"` with `--output-format text|json|stream-json`. `--json-schema '<schema>'` together with `--output-format json` returns the schema-conforming result in `structured_output`. JSON output includes `result`, `session_id`, `total_cost_usd` and a per-model cost breakdown. The process exits 0 on success and non-zero on failure — [Claude Code headless](https://code.claude.com/docs/en/headless)
- `--bare` skips auto-discovery of hooks, skills, plugins, MCP servers, auto memory and CLAUDE.md. It is "the recommended mode for scripted and SDK calls, and will become the default for `-p`". In bare mode you load context explicitly with `--append-system-prompt[-file]`, `--settings`, `--mcp-config`, `--agents` and `--plugin-dir`. Without `--bare`, `-p` runs a repo's `.claude/settings.json` hooks and `.mcp.json` servers "even in a folder you've never trusted" — [Claude Code headless](https://code.claude.com/docs/en/headless)
- Tools: `--allowedTools "Read,Edit,Bash(git diff *),..."` uses permission-rule syntax with prefix matching (`Bash(git diff *)`). `--permission-mode dontAsk` "denies every call that would otherwise prompt", which the docs recommend for locked-down CI. `--permission-prompts none` (v2.1.259+) denies anything that would prompt; denials appear in `permission_denials` — [Claude Code headless](https://code.claude.com/docs/en/headless)
- Other run controls: sessions resume with `--resume <session_id>`, `--max-budget-usd` caps spend, SIGTERM exits with code 143, and stream-json emits `system/init` (model, tools, MCP status) and `system/api_retry` events — [Claude Code headless](https://code.claude.com/docs/en/headless)
- Proxy: `ANTHROPIC_BASE_URL` points Claude Code at a gateway. Any gateway exposing a supported API format works, but Anthropic "doesn't support routing Claude Code to non-Claude models through any gateway". Managed setting `allowedProviders: ["customEndpoint"]`, with `ANTHROPIC_BASE_URL` in the managed `env` block, makes the gateway the only allowed destination (v2.1.285+). Setting only the base URL without a gateway credential leaves the claude.ai subscription as the active credential — [Claude Code LLM gateway](https://code.claude.com/docs/en/llm-gateway)
- Agent SDK (Python) `claude-agent-sdk`: MIT-licensed SDK that bundles the Claude Code CLI. `ClaudeAgentOptions` has `cwd`, `allowed_tools` (auto-approve only, "does not remove tools"), `disallowed_tools` (block), `permission_mode`, `cli_path`, `hooks` (e.g. a `PreToolUse` Python callback on `Bash`), `system_prompt` and `mcp_servers`. It needs Python 3.10+, and wheels are built for macOS, Linux and Windows. Use is also governed by Anthropic's Commercial Terms — [claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python)
- Licence position: the SDK wrapper is MIT, but the Claude Code CLI it drives is a proprietary Anthropic product (fine as a backend; not an OSS reuse candidate) — [claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python)

**OpenAI Codex CLI (`codex exec`)**
- Licence Apache-2.0 (about 129.5k stars), with a PowerShell installer for Windows — [openai/codex](https://github.com/openai/codex)
- `codex exec`: `--json` turns stdout into a JSONL event stream (`thread.started`, `turn.started`, `turn.completed` with token usage, `turn.failed`, `item.*`, `error`; item types include command executions, file changes and MCP tool calls). `--output-schema <file>` constrains the final response to a JSON Schema, and `-o/--output-last-message <path>` writes the final message to a file. `--sandbox` defaults to read-only, with `workspace-write` and `danger-full-access` as the other options. **`--full-auto` "is no longer supported"**; use `--sandbox workspace-write` or `--approve-for-me` instead. Other flags: `--ephemeral`, `--skip-git-repo-check`, `--ignore-user-config` (skip `$CODEX_HOME/config.toml`), `--ignore-rules` (skip execpolicy `.rules`), and `codex exec resume <SESSION_ID>`. The `CODEX_API_KEY` env var works for `exec`. The docs warn not to expose keys at job level where repo code runs — [Codex non-interactive](https://learn.chatgpt.com/docs/non-interactive-mode)
- Proxy: in `config.toml`, set `model_provider = "litellm"` with a `[model_providers.litellm]` block (`base_url = "http://localhost:4000/v1"`, `env_key = "LITELLM_API_KEY"`, `wire_api = "responses"`). Codex uses the OpenAI **Responses** API (`/v1/responses`) — [LiteLLM Codex docs](https://docs.litellm.ai/docs/proxy/client_setup/codex_cli.md). One proxy guide says newer Codex releases dropped `wire_api = "chat"` (third-party) — [ALCF proxy Codex](https://alcf-proxy.readthedocs.io/en/latest/clients/codex/)
- Command policy: execpolicy `.rules` files are written in Starlark with `prefix_rule(pattern=[...], decision="allow"|"prompt"|"forbidden", justification=..., match=[...], not_match=[...])`. The strictest matching rule wins, and the `match`/`not_match` examples are validated at load time. These details come from secondary sources and mirrors, and one mirror notes the API is "in preview" — [Daniel Vaughan execpolicy](https://codex.danielvaughan.com/2026/05/21/codex-cli-execution-policy-rules-starlark-command-governance-enterprise-allowlists/)

**Gemini CLI**
- Headless runs when stdin is not a TTY or with `-p/--prompt`. `--output-format` takes `text|json|stream-json`. JSON output has `response`, `stats` and an optional `error`. Stream events are `init`, `message`, `tool_use`, `tool_result`, `error` and `result`. Exit codes: 0 success, 1 error, 42 input error, 53 turn limit exceeded — [Gemini CLI headless](https://geminicli.com/docs/cli/headless/)
- Flags: `-m/--model`, `--approval-mode default|auto_edit|yolo|plan` (`--yolo` is deprecated), `-s/--sandbox`, `--allowed-mcp-server-names` and `--experimental-acp`. **`--allowed-tools` is deprecated in favour of the Policy Engine** — [Gemini CLI reference](https://geminicli.com/docs/cli/cli-reference/)
- Proxy: `GOOGLE_GEMINI_BASE_URL` routes to a custom endpoint. Gemini CLI uses the Gemini API protocol, not OpenAI's — [LiteLLM Gemini CLI tutorial](https://docs.litellm.ai/docs/tutorials/litellm_gemini_cli). A community article reports that Vertex/Google-auth credentials bypass `GOOGLE_GEMINI_BASE_URL` — [DEV Community](https://dev.to/polar3130/using-gemini-cli-through-litellm-proxy-1627)
- Strategic risk: the official docs say Gemini CLI "was replaced by Antigravity CLI on June 18th, 2026" for unpaid-tier and Google One users, and that it "has not been optimized for Gemini 4 Argon" — [Gemini CLI headless](https://geminicli.com/docs/cli/headless/). Press reports describe Antigravity CLI as a closed-source Go binary (`agy`). Gemini CLI code stays Apache-2.0, and paid API-key and enterprise users keep access — [The New Stack](https://thenewstack.io/google-antigravity-cli/), [FOSS Force](https://fossforce.com/2026/05/gemini-clis-short-life-and-googles-antigravity-bait-and-switch/), [codenote](https://codenote.net/en/posts/gemini-cli-oss-continuity-license-risk-antigravity/)

**OpenCode**
- MIT licence, about 214k stars. The repo is now under the `anomalyco` org. Windows installs via scoop, choco or npm, and a Windows desktop app is in beta — [anomalyco/opencode](https://github.com/anomalyco/opencode)
- `opencode run`: `--format json` (raw JSON events), `-m provider/model`, `--agent`, `-f` (attach files), `--dir`, `--attach <serve URL>` and `--auto` (auto-approve permissions not explicitly denied). `opencode serve` runs a headless HTTP server with basic auth via `OPENCODE_SERVER_PASSWORD`. Other entry points: `opencode acp`, inline permission JSON via `OPENCODE_PERMISSION`, and `OPENCODE_GIT_BASH_PATH` on Windows — [OpenCode CLI](https://opencode.ai/docs/cli/)
- Inline config via `OPENCODE_CONFIG_CONTENT` has the highest precedence — [OpenCode config](https://docs.opencode.ai/docs/config). Per-provider `options.baseURL` for proxies is documented only on a community page — [learnopencode](https://learnopencode.com/appendix/config-ref)

**Goose**
- Apache-2.0, about 55k stars. The repo is now `aaif-goose/goose`, part of the "Agentic AI Foundation (AAIF) at the Linux Foundation". It has 15+ providers, ACP support, and Windows desktop and PowerShell installers — [goose GitHub](https://github.com/block/goose)
- Headless: `goose run` with `-t/--text` or `--instructions <file|->`, `--recipe <name|path>` with `--params KEY=VALUE`, `--no-session`, and `--output-format json` for CI — [goose headless](https://goose-docs.ai/docs/tutorials/headless-goose), [goose-run man page](https://www.mankier.com/1/goose-run)
- Proxy: `GOOSE_PROVIDER=openai`, `GOOSE_MODEL`, `OPENAI_HOST` (scheme and host only) plus `OPENAI_BASE_PATH`. `OPENAI_API_KEY` must be non-empty (gateway docs) — [agentgateway Goose](https://agentgateway.dev/docs/integrations/web-uis/goose)

**Aider**
- Apache-2.0, about 49.6k stars — [Aider GitHub](https://github.com/Aider-AI/aider)
- Scripting: `--message/-m` (one instruction, apply edits, exit), `--message-file/-f`, `--yes` and `--no-auto-commits`. The Python API `Coder.create(...)`/`coder.run(...)` "is not officially supported or documented, and could change" — [Aider scripting](https://aider.chat/docs/scripting.html)

**GitHub Copilot CLI (proprietary)**
- `-p PROMPT`; `--allow-tool='write, shell'` with filters such as `shell(git:*)`; `--deny-tool` (takes precedence over the allow flags); `--allow-all-tools`; `--model` / `COPILOT_MODEL`; `--output-format json` (JSONL); `-s/--silent`; `--no-ask-user`. Auth through `COPILOT_GITHUB_TOKEN`/`GH_TOKEN` — [Copilot CLI programmatic reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference)
- BYOK (e.g. `COPILOT_PROVIDER_TYPE=anthropic` with a custom base URL) is documented only by a third party — [OrcaRouter Copilot CLI](https://docs.orcarouter.ai/integrations/copilot-cli)

**Summary table: local CLI backends**

| Agent | Licence | Headless + structured result | Model forced / proxy | Tool restriction | Windows native | ACP |
|---|---|---|---|---|---|---|
| Claude Code / Agent SDK | CLI proprietary; Python SDK MIT | `-p --output-format json --json-schema` → `structured_output`; SDK message objects | `ANTHROPIC_BASE_URL` + gateway credential; managed `allowedProviders:["customEndpoint"]` locks it; Anthropic Messages format only; non-Claude models unsupported | `--allowedTools`/`--disallowedTools` rule syntax, `--permission-mode dontAsk`, `--permission-prompts none`, SDK `PreToolUse` hooks; `--bare` | Yes (SDK wheels for Windows; bash sandbox needs WSL2, see section 4) | Via `claude-agent-acp` adapter |
| Codex CLI | Apache-2.0 | `exec --json` JSONL + `--output-schema` + `-o` | `[model_providers.X] base_url, env_key, wire_api="responses"`; `--ignore-user-config` | Sandbox modes + Starlark execpolicy `.rules`; `requirements.toml` admin constraints | Yes, native sandbox (MXC/elevated/unelevated) | Via `codex-acp` |
| Gemini CLI | Apache-2.0 (successor Antigravity CLI is closed) | `-p --output-format json` → `response`, `stats` | `GOOGLE_GEMINI_BASE_URL` (Gemini wire protocol) | Policy Engine; `--approval-mode`; `--allowed-tools` deprecated | Not verified | `--experimental-acp` |
| OpenCode | MIT | `run --format json`; `serve` HTTP API + SDK | provider `options.baseURL` (community doc), `-m provider/model`, `OPENCODE_CONFIG_CONTENT` | `OPENCODE_PERMISSION` JSON; `--auto` | Yes (scoop/choco; Git Bash) | `opencode acp` |
| Goose | Apache-2.0 (Linux Foundation AAIF) | `goose run --output-format json`, recipes | `GOOSE_PROVIDER`, `OPENAI_HOST`/`OPENAI_BASE_PATH` | Recipes / extensions (not researched in depth) | Yes | Yes |
| Aider | Apache-2.0 | `--message`, exit; no JSON result documented | OpenAI-compatible base (flag not verified this session) | No tool layer: edits files and auto-commits | Not verified | No |
| Copilot CLI | Proprietary | `-p --output-format json` (JSONL) | `--model`; BYOK only third-party documented | `--allow-tool`/`--deny-tool` with `shell(...)` filters | Not verified | ACP server mode (flag not confirmed) |

### Inferences
- TokenLean must expose several wire formats to front all these agents: Anthropic Messages (Claude Code), OpenAI Responses `/v1/responses` (Codex), Gemini `generateContent` (Gemini CLI), and OpenAI chat-completions (Goose, OpenCode, Aider and most others). If TokenLean speaks only OpenAI chat-completions, Codex and Claude Code need a translation layer (e.g. LiteLLM in front of or inside TokenLean). This is the biggest integration requirement uncovered.
- Claude Code can be routed through TokenLean only to Claude models (Anthropic doesn't support non-Claude routing). Use Codex, OpenCode or Goose when a step must run on non-Anthropic models.
- For governance at 1,000-developer scale, prefer backends with *admin-enforced* config: Claude managed settings (`allowedProviders`, permissions, sandbox) and Codex `requirements.toml`. Always run Claude with `--bare` plus explicit `--settings`/`--mcp-config`, and Codex with `--ignore-user-config` plus an explicit `-c`/profile, so a developer's personal config or a repo's config cannot change model, proxy or tools.
- Prefer the Claude Agent SDK over shelling out to `claude -p` for Claude steps. It runs in-process, exposes typed messages, and gives programmatic `PreToolUse` hooks for shell-command policy. For Codex, `codex exec --json --output-schema` is the most stable contract.
- Treat Gemini CLI as a lower-priority, replaceable backend because of the Antigravity transition. Don't build features around it.
- Aider fits "edit these files" steps but lacks tool governance and a structured result. Drive it through the CLI, not its unsupported Python API.

### Gaps
- I didn't verify the Gemini CLI Policy Engine format or a native-Windows statement for Gemini CLI.
- I didn't fetch official OpenCode docs for the provider `baseURL` and permission schema, nor Aider's `--openai-api-base`/`--model` flags.
- I didn't verify Codex `-m`/`-c` override syntax or the official `config.toml` provider doc on OpenAI's site (the base_url details come from LiteLLM's docs).
- Official docs for Copilot CLI BYOK/custom endpoint were not found.
- Goose tool-restriction mechanisms (extension allow-lists, permission modes) were not researched.

---

## 3. Remote agent APIs (Devin, GitHub Copilot coding agent, Jules): adapter shape and limits

### Takeaway
All three are asynchronous, PR-producing services. A remote adapter would *submit a task, poll or stream status, collect the PR/diff and structured output*. None of them can be routed through TokenLean or held to your tool allow-list; governance is limited to repo scoping, vendor-side settings and reviewing the resulting PR. Devin has the richest API (structured-output schema, playbooks, ACU budget, RBAC service users). Jules is alpha and GitHub-only. The Copilot coding agent is driven by issue assignment (GraphQL or `gh`), not a task API.

### Cited Findings
- **Devin v3**: `POST /v3/organizations/{org_id}/sessions` with a Bearer service-user token (`cog_…`) or personal access token, which needs the `UseDevinSessions` permission. Body fields include `prompt` (required), `playbook_id`, `structured_output_schema` (JSON Schema ≤64KB), `structured_output_required`, `repos[]`, `tags`, `max_acu_limit`, `devin_mode`, `bypass_approval`, `knowledge_ids`, `secret_ids`, `session_secrets` and `security_profile`. The response includes `session_id`, `url`, `status`, `acus_consumed`, `pull_requests` and `structured_output` — [Devin API create session](https://docs.devin.ai/api-reference/v3/sessions/post-organizations-sessions.md)
- Devin flow: create a session, then poll the messages endpoint — [Devin common flows](https://docs.devin.ai/api-reference/common-flows)
- **Jules REST API (v1alpha, experimental)**: base `https://jules.googleapis.com`, `POST /v1alpha/sessions`, auth with `X-Goog-Api-Key` (maximum 3 keys per account). Sources such as GitHub repos require the Jules GitHub app to be installed first — [Jules API reference](https://developers.google.com/jules/api/reference/rest)
- A Jules Session has `prompt`, `sourceContext` (`source`, `githubRepoContext.startingBranch`), `requirePlanApproval`, `automationMode` (`AUTO_CREATE_PR`), `state` (QUEUED, PLANNING, AWAITING_PLAN_APPROVAL, AWAITING_USER_FEEDBACK, IN_PROGRESS, PAUSED, FAILED, COMPLETED) and `outputs[].pullRequest{url,title,description}`. Methods: `create`, `get`, `list`, `approvePlan`, `sendMessage`; activities are available as a sub-resource — [Jules sessions resource](https://developers.google.com/jules/api/reference/rest/v1alpha/sessions)
- **GitHub Copilot coding agent**: assign work programmatically through the GraphQL API (check that Copilot is a suggested actor, then `createIssue` with Copilot's bot ID) or `gh issue edit` to add Copilot as assignee — [Microsoft Learn module](https://learn.microsoft.com/en-us/training/modules/github-copilot-code-agent/3-assign-track-troubleshoot-copilot-code-agent-tasks)
- Copilot coding agent can be assigned Jira issues (public preview, March 2026; needs Jira Cloud with Rovo) — [GitHub changelog](https://github.blog/changelog/2026-03-06-github-copilot-coding-agent-for-jira-is-now-in-public-preview/)
- Third-party report: Claude and Codex are available as coding agents inside GitHub's "Agent HQ" — [Daniel Vaughan](https://codex.danielvaughan.com/2026/03/28/codex-github-copilot-coding-agent-issue-assignment/)

| Service | Licence | Submit | Result | Governance available | Limits |
|---|---|---|---|---|---|
| Devin | Proprietary SaaS | REST v3 create session | Poll; `structured_output`, `pull_requests` | RBAC service users, `max_acu_limit`, playbooks, secrets, `security_profile` | No proxy or model control; vendor-hosted VM |
| Jules | Proprietary (Google) | REST v1alpha sessions | Poll state/activities; PR in `outputs` | Plan approval gate, branch choice | Alpha API; GitHub only; 3 API keys |
| Copilot coding agent | Proprietary (GitHub) | Assign issue (GraphQL/`gh`) | PR on GitHub; track via PR/timeline | Repo-level Copilot policies | No documented task-submission REST API found; GitHub-hosted only |

### Inferences
- Model a `RemoteAgentBackend` interface separately from local backends: `submit(task, repo, branch, schema) -> handle`, `status(handle)`, `result(handle) -> {pr_url, diff, structured_output, cost}`, `message(handle, text)`, `cancel`. The framework's "reviewable diff" for these is the PR diff. Rollback means closing the PR or deleting the branch.
- Because these services bypass TokenLean and your sandbox, gate them by policy: an allow-list of repos and data classification, and a mandatory Review agent on their PRs.

### Gaps
- I found no official REST endpoint for creating Copilot coding agent tasks; check GitHub's REST/GraphQL docs for any newer "agent tasks" API.
- I didn't research Devin and Jules rate limits, pricing, or data-residency terms.

---

## 4. Sandboxing options (worktrees, containers, microVMs, OS-level sandboxes): Windows support, licences, self-hosting

### Takeaway
No single OSS sandbox covers all three OSes natively with strong isolation. A practical layered design: **(a) a git worktree per step** (cheap, cross-platform, gives the diff and rollback); **(b) an OS-level process sandbox** for the shell: Anthropic `sandbox-runtime` (Apache-2.0; macOS and Linux supported, Windows alpha) or Codex's built-in sandbox (native Windows support via MXC, elevated or unelevated); **(c) optionally a container** (Docker/Podman, or Dagger `container-use`) for stronger isolation in CI and on Linux. MicroVM platforms are for server-side or CI pools, not laptops: E2B (Apache-2.0, self-hostable via Terraform, GCP most mature) and microsandbox (Apache-2.0, pre-1.0, Windows preview). Daytona went closed-source in June 2026; its last open release is AGPL-3.0, so avoid it as a reuse candidate.

### Cited Findings
**Anthropic sandbox-runtime (`srt`)**
- Apache-2.0, a "Beta Research Preview". Mechanisms: macOS `sandbox-exec` (Seatbelt), Linux `bubblewrap` plus network namespace, Windows **alpha** (dedicated `srt-sandbox` user, Windows Filtering Platform egress fence, NTFS ACEs; needs one-time elevated `windows-install`). Network goes through host HTTP/SOCKS5 proxies with a domain allow-list (default deny) and DNS-rebinding protection. Filesystem reads are deny-then-allow; writes are allow-only. Ships as an npm CLI (`srt --settings file.json <cmd>`) and a TS library (`SandboxManager.wrapWithSandbox`). Linux needs `bubblewrap`, `socat` and `ripgrep` — [sandbox-runtime GitHub](https://github.com/anthropic-experimental/sandbox-runtime) (Claude Code docs link it as `github.com/anthropics/sandbox-runtime`)
- Claude Code's bash sandbox is built on `@anthropic-ai/sandbox-runtime`. It "runs on macOS, Linux, and WSL2. On native Windows, Claude Code runs commands unsandboxed." `sandbox.failIfUnavailable: true` makes startup fail instead of silently running unsandboxed. Disabling the unsandboxed-retry makes the sandbox admin-required. `excludedCommands` and `network.allowedDomains` are configurable. In a linked git worktree, the sandbox allows writes to the main repo's `.git` but still denies `hooks/` and `config` — [Claude Code sandboxing](https://code.claude.com/docs/en/sandboxing)

**Codex built-in sandbox**
- Windows modes: `mxc` (recommended; native process isolation with no admin setup), `elevated` (dedicated low-privilege users, ACLs, firewall rules; needs admin setup) and `unelevated` (restricted token plus ACLs; weaker network isolation; no deny-read). Admins can constrain modes via `requirements.toml` (`allow_mxc`, `allowed_sandbox_implementations`). Windows 11 is recommended; Windows 10 1809+ is best effort — [Codex Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox)
- `codex exec` defaults to a read-only sandbox, with `workspace-write` and `danger-full-access` as alternatives — [Codex non-interactive](https://learn.chatgpt.com/docs/non-interactive-mode)

**Dagger container-use**
- Apache-2.0. An MCP server (`container-use stdio`, CLI `cu`) that gives each agent "a fresh container in its own git branch". Works with Claude Code, Cursor, Goose, VS Code and other MCP agents. Status is "early development", marked experimental. About 4.1k stars. Windows support is not mentioned — [dagger/container-use](https://github.com/dagger/container-use)

**E2B**
- E2B says its SDKs, CLI, runtime, control plane and Firecracker microVM layer are public under Apache-2.0, with self-hosting via Terraform from `e2b-dev/infra` — [E2B open source](https://e2b.dev/open-source)
- Self-hosting maturity conflicts between sources: GCP is the most mature; AWS is beta or in progress; Azure and generic Linux are not supported — [AgenticWire E2B self-hosted](https://www.agenticwire.news/article/e2b-self-hosted-guide), [dev.co E2B](https://dev.co/ai/frameworks/e2b). One comparison lists E2B cloud/BYOC as proprietary — [morphllm E2B vs Daytona](https://www.morphllm.com/comparisons/e2b-vs-daytona)

**Daytona**
- Moved production development to a closed codebase in June 2026; the public repo is frozen. Last open release v0.190.0, AGPL-3.0 (sources: a community fork README and third-party blogs; no official announcement found) — [helix.ml self-hosted sandboxes](https://helix.ml/blog/self-hosted-agent-sandboxes), [Nightona README](https://cdn.jsdelivr.net/gh/nightona-co/nightona@main/README.md), [Agent37](https://www.agent37.com/blog/daytona-alternative)

**microsandbox**
- Apache-2.0, built on libkrun microVMs, pre-1.0. On Windows, the Rust SDK README says it runs on the Windows Hypervisor Platform in preview, while a June 2026 review says it runs via WSL2 (conflicting). Versions 0.6.x/0.7.x — [microsandbox GitHub](https://github.com/superradcompany/microsandbox), [Rust SDK README](https://cdn.jsdelivr.net/gh/superradcompany/microsandbox@main/sdk/rust/README.md), [rywalker microsandbox](https://rywalker.com/research/microsandbox)

**OpenHands**
- Core (SDK, CLI, GUI, server) is MIT; the `enterprise/` directory is source-available and needs a commercial licence (secondary source) — [dsebastien OpenHands](https://www.dsebastien.net/openhands/)
- The Software Agent SDK offers Python and REST APIs and a server that runs agents in Docker or Kubernetes — [OpenHands SDK docs](https://docs.openhands.dev/sdk)
- Runtime backends: Docker by default (cap-drop ALL, no-new-privileges), plus Local, Kubernetes, Modal and Remote (secondary summary in a GitHub issue) — [hermes-agent issue](https://github.com/NousResearch/hermes-agent/issues/477)

**Worktree managers**
- Worktrunk (`max-sixty/worktrunk`): a Rust CLI for git worktree management "designed for parallel AI agent workflows". Commands `wt switch/list/merge/remove`, hooks on create and merge, code-signed Windows binaries, about 4.9k stars — [worktrunk README](https://cdn.jsdelivr.net/gh/max-sixty/worktrunk@main/README.md), [gittrend](https://gittrend.io/repo/max-sixty/worktrunk)
- gwtm: a small Python CLI that stores worktrees in `~/.worktrees/{repo}/{name}` — [PyPI gwtm](https://pypi.org/project/gwtm/)

| Option | Licence | Isolation | Windows | macOS/Linux | Self-host | Fit |
|---|---|---|---|---|---|---|
| git worktree (own code, or Worktrunk) | git GPL tool used as-is; Worktrunk licence not verified | Filesystem working copy only (no security) | Yes | Yes | n/a | Always: diff and rollback unit |
| Anthropic `srt` | Apache-2.0 | OS process sandbox: FS + domain-allow-list network | Alpha (admin setup) | Yes | n/a (local) | Wrap any agent's shell; reuse as library (npm) or CLI |
| Codex built-in sandbox | Apache-2.0 (part of Codex) | OS sandbox + execpolicy | Yes (MXC/elevated/unelevated) | Yes | n/a | Only when the backend is Codex |
| Claude Code sandbox | Proprietary CLI on Apache-2.0 srt | srt | **No** (WSL2 only) | Yes | n/a | Claude backend on mac/Linux/WSL2 |
| Dagger container-use | Apache-2.0 | Container + branch per agent (MCP) | Not stated (needs a container engine) | Yes | Local | Experimental; borrow ideas |
| Docker/Podman (DIY) | Apache-2.0 engines (Docker Desktop is commercial for large orgs; not verified this session) | Container | Via Docker Desktop/WSL2 | Yes | Yes | CI default; laptop optional |
| E2B | Apache-2.0 | Firecracker microVM | Client only | Server on Linux/KVM | Terraform; GCP mature, AWS beta | Central sandbox pool for CI and remote runs |
| microsandbox | Apache-2.0 | libkrun microVM | Preview (conflicting reports) | Yes | Yes | Watch; pre-1.0 |
| OpenHands runtime | MIT core (enterprise dir source-available) | Docker/K8s/remote | Via Docker | Yes | Yes | Heavy; reuse only if adopting OpenHands |
| Daytona | AGPL-3.0 frozen; now closed | Container sandboxes | n/a | n/a | Frozen fork only | Avoid (copyleft, unmaintained upstream) |

### Inferences
- **Laptop baseline (all OSes):** a worktree per step, plus the backend's own OS sandbox: Codex on Windows natively; Claude with `sandbox.enabled` and `failIfUnavailable` on mac/Linux/WSL2. Windows developers who use Claude backends must run them under WSL2 to get sandboxing, or accept `srt` Windows alpha. For agents without a sandbox (Aider, OpenCode, Goose), wrap the agent process in `srt` (mac/Linux) or run it in a container.
- **CI baseline:** ephemeral container per step (Docker/Podman, rootless where possible) with the worktree bind-mounted, network egress limited to the TokenLean proxy and package mirrors, and `srt` inside for per-command domain policy if needed. Stronger tenancy at 1,000 developers: self-hosted E2B (Firecracker) on GCP or AWS.
- The diff and rollback contract should be framework-owned and independent of the sandbox: create a worktree on a step branch, run the agent, `git add -A && git diff --binary base..HEAD` produces the artifact, then merge, cherry-pick or discard by removing the worktree. Worktrunk's hooks model is a useful reference, but plain `git worktree` driven from Python (e.g. subprocess or GitPython) removes a dependency.
- Enterprise self-hosting matters: E2B cloud, Daytona and Devin are vendor-hosted. For code confidentiality, only OSS self-hostable layers (srt, containers, E2B infra, microsandbox) satisfy "runs on our infra".

### Gaps
- I didn't research Firecracker and gVisor directly (both Linux-only to my knowledge; not verified this session).
- Worktrunk's licence, container-use Windows support, and E2B infra's current AWS status were not verified from primary sources.
- I didn't verify the Docker Desktop licensing terms for a 1,000-developer org this session.
- I found no direct comparison of srt Windows alpha vs Codex MXC isolation strength.

---

## 5. Shell command allow-listing and policy: reusable OSS

### Takeaway
There is no standalone, agent-agnostic "shell policy engine" library with wide adoption. The reusable pieces are: Codex's Starlark **execpolicy** (prefix rules allow/prompt/forbidden with self-testing examples; Apache-2.0 Rust, preview API); Claude Code's **permission-rule syntax** (`Bash(git diff *)`) plus SDK `PreToolUse` hooks; Copilot CLI's `shell(git:*)` filters; and `srt` for OS-level enforcement of filesystem and network. Recommended approach: define one framework-level policy schema (a prefix-rule model inspired by execpolicy) and compile it into each backend's native format, backed by OS or container enforcement.

### Cited Findings
- Codex execpolicy: Starlark `prefix_rule` with `pattern` token lists (list elements are alternatives), `decision` allow/prompt/forbidden, `justification`, and `match`/`not_match` tests validated at load. The strictest decision wins (forbidden > prompt > allow). Preview API (secondary sources) — [Daniel Vaughan execpolicy](https://codex.danielvaughan.com/2026/05/21/codex-cli-execution-policy-rules-starlark-command-governance-enterprise-allowlists/), [Daniel Vaughan execpolicy (Apr)](https://codex.danielvaughan.com/2026/04/16/codex-cli-execution-policy-rules-starlark-command-governance/)
- `codex exec --ignore-rules` skips user and project `.rules`, so for enforcement, policies should be admin-scoped and the flag must not be passed by the framework — [Codex non-interactive](https://learn.chatgpt.com/docs/non-interactive-mode)
- Claude Code `--allowedTools` uses permission-rule syntax. `Bash(git diff *)` is a prefix match, and the space before `*` matters (`Bash(git diff*)` would also match `git diff-index`) — [Claude Code headless](https://code.claude.com/docs/en/headless)
- Claude Agent SDK `hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[check_bash_command])]}` runs a Python callback before each Bash call — [claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python)
- Copilot CLI `--allow-tool`/`--deny-tool` with filters like `shell(git:*)`; deny takes precedence — [Copilot CLI programmatic reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference)
- `srt` enforces a filesystem and network-domain policy on any command and its children at the OS level — [sandbox-runtime GitHub](https://github.com/anthropic-experimental/sandbox-runtime)
- Gemini CLI has moved tool control to a "Policy Engine" (`--allowed-tools` deprecated) — [Gemini CLI reference](https://geminicli.com/docs/cli/cli-reference/)

### Inferences
- Prefix or token rules are bypassable (`bash -c`, `python -c`, `env`, pipes, PowerShell aliases). Command allow-listing is advisory UX. Treat OS or container isolation (srt, Codex sandbox, container, microVM) as the security boundary, and the allow-list as defence in depth plus audit.
- For your own-loop agents (the ones calling TokenLean directly), implement the shell tool yourself: parse with `shlex` (POSIX) or a PowerShell AST on Windows, match against the framework policy, and execute inside `srt` or the container. That gives one policy for in-house and external agents.

### Gaps
- I found no maintained Python library for shell-command policy matching. Codex execpolicy is Rust, and whether it can be called standalone (e.g. a `codex execpolicy check` subcommand) was not verified this session.
- Gemini Policy Engine syntax was not retrieved.

---

## 6. Existing multi-agent orchestrators that wrap several CLI agents: what to borrow

### Takeaway
The category ships plenty of worktree-per-task UIs, but most are desktop or TUI apps with licence or maintenance problems for reuse. Vibe Kanban (Apache-2.0, ~28k stars, 10+ agent "executors") is the best *code* reference but is sunsetting. Claude Squad is AGPL-3.0 (copyleft; tmux-based, so no native Windows). Conductor is proprietary and macOS-only. ACP-native orchestrators (Jockey, Raven) show the protocol route. Borrow patterns (worktree lifecycle, per-agent executor profiles, diff review UI) rather than code.

### Cited Findings
- **Vibe Kanban**: Apache-2.0, about 28.4k stars. Supports Claude Code, Codex, Gemini CLI, GitHub Copilot, Amp, Cursor, OpenCode, Droid, CCR and Qwen Code; each workspace gets its own branch, terminal and dev server (worktree cleanup setting). Rust backend plus web frontend (inferred). README says "Vibe Kanban is sunsetting" — [BloopAI/vibe-kanban](https://github.com/BloopAI/vibe-kanban). Company bloop shut down April 2026; community-maintained since — [ai.engineer Vibe Kanban](https://www.ai.engineer/orgs/vibe-kanban), [crystl.dev comparison](https://crystl.dev/blog/best-ai-agent-orchestration-tools/)
- **Claude Squad**: AGPL-3.0, about 8.6k stars. Supports Claude Code, Codex, Gemini, Aider, OpenCode and Amp. Needs tmux and gh. "Each task gets its own isolated git workspace". Windows is not mentioned — [smtg-ai/claude-squad](https://github.com/smtg-ai/claude-squad)
- **Conductor**: macOS app running Claude Code, Codex or Cursor agents in worktree-backed workspaces. Fixed agent list; paid tiers from $50/month (vendor-authored comparison) — [crystl.dev comparison](https://crystl.dev/blog/best-ai-agent-orchestration-tools/)
- **ACP orchestrators**: Jockey (Claude Code, Gemini CLI and Codex CLI via ACP), Raven (task DAG over ACP agents), CompozyOS, Kronos — [ACP clients](https://agentclientprotocol.com/overview/clients)
- **container-use**: container plus branch per agent via MCP (Apache-2.0, experimental) — [dagger/container-use](https://github.com/dagger/container-use)

| Project | Licence | Agents wrapped | Isolation | Windows | Status | What to borrow |
|---|---|---|---|---|---|---|
| Vibe Kanban | Apache-2.0 | 10+ (Claude, Codex, Gemini, Copilot, Amp, Cursor, OpenCode, Droid, Qwen…) | Worktree per task | Unclear | Sunsetting / community | Executor definitions per agent (CLI args, output parsing), worktree lifecycle, review UI |
| Claude Squad | **AGPL-3.0** (copyleft) | Claude, Codex, Gemini, Aider, OpenCode, Amp | Worktree + tmux | Unlikely (tmux) | Active | Ideas only; don't copy code |
| Conductor | Proprietary | Claude, Codex, Cursor | Worktree | No (macOS) | Commercial | UX ideas only |
| Jockey / Raven (ACP) | Not verified | Any ACP agent | Not verified | Not verified | Not verified | ACP client patterns for multi-agent DAGs |
| container-use | Apache-2.0 | Any MCP agent | Container + branch | Not stated | Experimental | Container-per-step + branch pattern |
| Worktrunk | Not verified | Agent-agnostic | Worktree | Yes (signed binaries) | Active | Worktree create/merge/remove hooks |

### Inferences
- The orchestration layer (pipeline of BA → Design → Dev → Review → QA) is your differentiator; none of these tools provide SDLC-role pipelines, proxy governance or policy compilation. Reuse is limited to (a) executor command and parse knowledge (Vibe Kanban, Apache-2.0, safe to study or port with attribution), and (b) ACP client code (Python SDK).
- Avoid AGPL code (Claude Squad, Daytona) in a framework distributed across 1,000 developers unless legal approves.

### Gaps
- I couldn't locate "Crystal" (stravu), "uzi" or "agent-farm" in searches; their licences and status are unverified. The only "crystl" result is a different, vendor macOS product.
- I didn't verify the licences of Jockey, Raven and Worktrunk.

---

## 7. Recommendation: what to reuse

### Takeaway
Build a thin, framework-owned **Backend** abstraction with three implementations: (1) **native-headless adapters** for Claude (Agent SDK) and Codex (`codex exec --json --output-schema`) as the governed, first-class external backends; (2) a generic **ACP adapter** (Apache-2.0 `agent-client-protocol` Python SDK) for the long tail (Gemini CLI, OpenCode, Goose, Copilot CLI, Claude/Codex via adapters) and for the future VS Code front end; (3) a **remote adapter** (submit/poll/PR) for Devin and Jules, with Copilot coding agent through issue assignment. For sandboxing, own the **git-worktree-per-step** lifecycle in Python, enforce with **Anthropic `srt`** (mac/Linux; Windows alpha) or **Codex's native sandbox** (Windows), use **containers** in CI, and consider **self-hosted E2B** for a central microVM pool. Compile one framework **policy schema** (prefix rules, after Codex execpolicy) into each agent's native permission format.

### Cited Findings
- Claude: `--bare`, `--json-schema`→`structured_output`, `--permission-mode dontAsk`, and managed `allowedProviders:["customEndpoint"]` give model, proxy and tool lockdown — [Claude Code headless](https://code.claude.com/docs/en/headless), [Claude Code LLM gateway](https://code.claude.com/docs/en/llm-gateway)
- Codex: `exec --json`, `--output-schema`, sandbox modes, `--ignore-user-config`, and `[model_providers]` `base_url`/`wire_api="responses"` — [Codex non-interactive](https://learn.chatgpt.com/docs/non-interactive-mode), [LiteLLM Codex docs](https://docs.litellm.ai/docs/proxy/client_setup/codex_cli.md)
- The ACP Python SDK is Apache-2.0 and covers many agents — [python-sdk](https://github.com/agentclientprotocol/python-sdk), [ACP agents](https://agentclientprotocol.com/overview/agents)
- `srt`: Apache-2.0, mac/Linux supported, Windows alpha — [sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime). Claude Code's sandbox doesn't run on native Windows — [Claude Code sandboxing](https://code.claude.com/docs/en/sandboxing). Codex has native Windows sandbox modes — [Codex Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox)
- E2B infra is Apache-2.0 and self-hostable via Terraform — [E2B open source](https://e2b.dev/open-source)

**Reuse shortlist**

| Need | Reuse | Licence | Notes |
|---|---|---|---|
| Claude backend | `claude-agent-sdk` (Python) | MIT SDK / proprietary CLI | Use `cli_path` to pin the CLI version; `PreToolUse` hook for policy; run bare via `--settings` |
| Codex backend | `codex exec` subprocess | Apache-2.0 | JSONL parse + `--output-schema`; ship a framework-owned `config.toml`/`-c` with the TokenLean provider |
| Many CLI agents, IDE front end | `agent-client-protocol` Python SDK | Apache-2.0 | Per-agent launch profile (cmd, env, config) + ACP session |
| A2A exposure / remote agents | `a2a-sdk` | Not verified (A2A project under Linux Foundation) | Later phase |
| UI streaming | `ag-ui-protocol` | Not verified | Later phase (VS Code/web) |
| Shell/FS/network sandbox | `@anthropic-ai/sandbox-runtime` (CLI wrapper from Python) | Apache-2.0 | Beta; Windows alpha; Node dependency |
| Windows-native sandbox | Codex sandbox (when backend = Codex) | Apache-2.0 | MXC preferred |
| CI isolation | Docker/Podman; optionally E2B self-hosted | Apache-2.0 | GCP most mature for E2B |
| Worktree mgmt | Plain `git worktree` from Python (reference: Worktrunk, Vibe Kanban) | — | Avoid AGPL Claude Squad code |
| Command policy | Own schema modelled on Codex execpolicy, compiled to Claude rules / Codex `.rules` / Copilot `--allow-tool` | — | OS sandbox is the real boundary |

### Inferences
- Phase 1: Claude SDK + Codex exec adapters, worktree manager, srt/Codex sandbox, container in CI, and a TokenLean multi-format check (Anthropic Messages + OpenAI Responses). Phase 2: ACP adapter (Gemini, OpenCode, Goose, Copilot CLI) and VS Code front end over ACP/AG-UI. Phase 3: remote adapters (Devin first, given its structured-output schema; then Jules once out of alpha; Copilot via issue assignment).
- Biggest risks: TokenLean wire-format coverage; Windows sandbox parity for non-Codex agents (srt Windows alpha, Claude sandbox needs WSL2); Gemini CLI's product future; adapter churn (`--full-auto` removal and `--allowed-tools` deprecation show flags change quickly), so pin agent versions and add contract tests per backend.

### Gaps
- No hands-on validation was performed. All flags come from docs as of 2026-10-16 and should be checked in a spike against pinned versions.
- Licences for `a2a-sdk`, `ag-ui-protocol`, Worktrunk, `claude-agent-acp` and `codex-acp` are not verified.
