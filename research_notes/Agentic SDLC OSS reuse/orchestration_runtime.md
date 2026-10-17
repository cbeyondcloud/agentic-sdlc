# Orchestration / Agent Runtime Libraries for the Agentic SDLC Core Engine

Research date: 2026-10-16. Repo metadata (licence, stars, latest release) was pulled live from the GitHub GraphQL API on that date. Capability claims were checked against official docs or source where a link is given. Anything marked "(unverified this session)" comes from prior knowledge and was not re-checked; treat it as a lead to confirm.

## Q1. Candidate libraries: how does each one fit the requirements?

### Takeaway
Most "agent frameworks" now ship the same basics: a tool loop, structured output, some way to pause for HITL, and OTel. They differ on four points that matter here: (a) whether you can pass headers per request **and read response headers**, (b) whether pause/resume works across processes from a plain local folder, (c) how much they lock you to a vendor or pull in heavy dependencies, and (d) how mature their declarative specs are. Pydantic AI v2, the OpenAI Agents SDK, LangGraph and Microsoft Agent Framework (MAF) are the serious options. **No candidate surfaces successful-response HTTP headers through its agent API**, except LangChain's `ChatOpenAI` with `include_response_headers`. Cost-header capture will be glue code whichever library we pick.

### Cited Findings

**Repo metadata (GitHub API, 2026-10-16)**

| Library | Repo | Licence | Stars | Latest release | Notes |
|---|---|---|---|---|---|
| LangGraph | [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) | MIT | 43.4k | langgraph 1.2.15 (2026-10-12) | Very active |
| Pydantic AI (+ pydantic-graph) | [pydantic/pydantic-ai](https://github.com/pydantic/pydantic-ai) | MIT | 20.7k | v2.57.0 (2026-10-16) | v2.0.0 stable on 2026-06-23 ([release](https://github.com/pydantic/pydantic-ai/releases/tag/v2.0.0)); about 57 minor releases since, so a very fast cadence |
| OpenAI Agents SDK | [openai/openai-agents-python](https://github.com/openai/openai-agents-python) | MIT | 30.1k | v0.24.0 (2026-10-15) | Still 0.x |
| Google ADK | [google/adk-python](https://github.com/google/adk-python) | Apache-2.0 | 21.9k | v2.12.0 (2026-10-13) | |
| CrewAI (+ Flows) | [crewAIInc/crewAI](https://github.com/crewAIInc/crewAI) | MIT | 59.8k | 1.15.28 (2026-10-14) | |
| AutoGen | [microsoft/autogen](https://github.com/microsoft/autogen) | MIT code (GitHub reports CC-BY-4.0, the docs licence) | 61.4k | python-v0.7.5 (2025-09-30) | **Maintenance mode**. The README says new users should start with MAF |
| AG2 | [ag2ai/ag2](https://github.com/ag2ai/ag2) | Apache-2.0 | 5.0k | v1.2.0 (2026-10-11) | Community fork of AutoGen 0.2 |
| Microsoft Agent Framework | [microsoft/agent-framework](https://github.com/microsoft/agent-framework) | MIT | 14.2k | python-1.22.0 (2026-10-15) | 1.0 GA, described as "production-ready release: stable APIs, and a commitment to long-term support" ([AutoGen README](https://github.com/microsoft/autogen)) |
| smolagents | [huggingface/smolagents](https://github.com/huggingface/smolagents) | Apache-2.0 | 29.9k | v1.26.0 (2026-05-29) | No release in 4.5 months |
| Strands Agents | [strands-agents/harness-sdk](https://github.com/strands-agents/harness-sdk) (renamed from sdk-python) | Apache-2.0 | 8.8k | python/v1.61.1 (2026-10-15) | Now a monorepo with Python and TS "harness" plus a CLI |
| Agno | [agno-agi/agno](https://github.com/agno-agi/agno) | Apache-2.0 | 42.8k | v3.1.4 (2026-10-16) | |
| Burr | [apache/burr](https://github.com/apache/burr) | Apache-2.0 | 2.6k | v0.43.0-incubating (2026-08-29) | Apache Incubator |
| Haystack | [deepset-ai/haystack](https://github.com/deepset-ai/haystack) | Apache-2.0 | 26.7k | v3.4.0 (2026-10-15) | |
| LlamaIndex Workflows | [run-llama/llama-agents](https://github.com/run-llama/llama-agents) (workflows-py redirects here) | MIT | 0.46k (repo); llama_index 52.5k | llama-index-workflows 2.26.0 (2026-10-11) | Standalone package |
| DSPy | [stanfordnlp/dspy](https://github.com/stanfordnlp/dspy) | MIT | 38.7k | 3.4.1 (2026-10-12) | |
| Letta | [letta-ai/letta](https://github.com/letta-ai/letta) | Apache-2.0 | 25.2k | 0.16.8 (2026-05-14) | Last push 2026-09-10, so activity is slowing |
| Mastra (TS) | [mastra-ai/mastra](https://github.com/mastra-ai/mastra) | Mixed: core Apache-2.0, anything under `ee/` uses a separate enterprise licence (**source-available portions**) | 28.9k | @mastra/core 1.78.0 (2026-10-16) | TypeScript, noted only |
| Deep Agents (new) | [langchain-ai/deepagents](https://github.com/langchain-ai/deepagents) | MIT | 30.3k | 0.7.26 (2026-10-15) | Harness built on LangGraph |
| Claude Agent SDK (Python) | [anthropics/claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python) | MIT (SDK; it drives the Claude Code CLI) | 8.3k | v0.2.168 (2026-10-15) | Candidate external-CLI backend |
| OpenAI Codex | [openai/codex](https://github.com/openai/codex) | Apache-2.0 | 129.5k | rust-v0.163.0 (2026-10-15) | Candidate external-CLI backend |
| OpenHands (agent SDK) | [OpenHands/software-agent-sdk](https://github.com/OpenHands/software-agent-sdk) | MIT | 1.3k (main OpenHands repo 91k) | | Contains a reusable StuckDetector (see Q4) |
| Dapr Agents (new) | [dapr/dapr-agents](https://github.com/dapr/dapr-agents) | Apache-2.0 | 0.76k | v1.0.9 (2026-10-09) | Durable agents on Dapr workflows, needs the Dapr runtime |
| BeeAI Framework | [i-am-bee/beeai-framework](https://github.com/i-am-bee/beeai-framework) | Apache-2.0 | 3.4k | python_v0.1.85 (2026-09-28) | |

All of the above: [GitHub GraphQL API query, 2026-10-16]. Licence detail for Mastra comes from its [LICENSE](https://github.com/mastra-ai/mastra).

**Pydantic AI v2**
- V2 is "harness-first" with **capabilities** as the core primitive: "a single composable unit that bundles an agent's tools, hooks, instructions, and model settings" — [v2.0.0 release](https://github.com/pydantic/pydantic-ai/releases/tag/v2.0.0)
- Declarative **AgentSpec** in YAML or JSON, loaded with `Agent.from_file()` / `Agent.from_spec()`. Fields: `name, description, instructions` (templated), `model, model_settings, capabilities, output_schema` (JSON Schema), `deps_schema, retries, end_strategy, tool_timeout, instrument, metadata`. Custom Python tools are **not** a spec field: capabilities that take callables "can only be used in Python code" — [Agent spec docs](https://pydantic.dev/docs/ai/core-concepts/agent-spec/)
- `ModelSettings.extra_headers: dict[str, str]` is supported by OpenAI, Anthropic, Google, Groq, Bedrock, OpenRouter, Ollama and others — [settings.py](https://github.com/pydantic/pydantic-ai/blob/main/pydantic_ai_slim/pydantic_ai/settings.py)
- Response headers appear in the model adapters only when mapping errors (`headers=dict(e.response.headers)`). Nothing surfaces them on successful responses — [models/openai.py](https://github.com/pydantic/pydantic-ai/blob/main/pydantic_ai_slim/pydantic_ai/models/openai.py) (grep of source; the same pattern is in anthropic.py)
- `UsageLimits` offers `request_limit, tool_calls_limit, output_tokens_limit, per_request_input_tokens_limit, cost_limit`. The docs say "Restricting the number of requests can be useful in preventing infinite loops or excessive tool calling". There is no dedicated repetition detector — [Agent docs](https://pydantic.dev/docs/ai/core-concepts/agent/)
- HITL: a tool marked `requires_approval=True` ends the run with a `DeferredToolRequests` output. The caller answers with `DeferredToolResults` (approve, deny, or override args). The stored conversation "is everything you need to resume". It is serialised with `ConversationTypeAdapter.dump_json`, and the resume can happen in a new process as a new run linked by `conversation_id`. Message history alone is not enough — [Deferred tools docs](https://pydantic.dev/docs/ai/tools-toolsets/deferred-tools/)
- Durable execution integrations: Temporal, DBOS, Prefect, Restate and AWS Lambda (co-maintained), plus Kitaru, Airflow, Absurd and pgtask, and a "durable execution backend builder" for other engines. Each model request and tool call becomes its own durable unit, and only one engine is allowed per agent — [Durable execution overview](https://pydantic.dev/docs/ai/integrations/durable_execution/overview/)
- pydantic-graph is an "async graph and state machine library" that works independently of Pydantic AI. It offers a `GraphBuilder`, `graph.iter()` for manual stepping, and Mermaid rendering. The fetched page did not cover persistence — [Graph docs](https://pydantic.dev/docs/ai/graph/)

**LangGraph**
- `interrupt()` pauses inside a node. You resume with `Command(resume=...)`, which **needs a checkpointer and a `thread_id`**. "In production, this should be a persistent checkpointer (e.g. backed by a database)." The node **re-runs from its start** on resume. `response_schema` validation of resume values arrived in langgraph>=1.2.12. The page shows `InMemorySaver` and `SqliteSaver` — [Interrupts docs](https://docs.langchain.com/oss/python/langgraph/interrupts)
- With `include_response_headers=True`, LangChain `ChatOpenAI` puts HTTP headers in `response_metadata["headers"]`, streaming included. A PR added the same option to `ChatAnthropic` — [LangChain response metadata](https://python.langchain.com/docs/how_to/response_metadata); [ChatAnthropic PR branch](https://codspeed.io/langchain-ai/langchain/branches/ketanbhatt%3Afeature%2Fanthropic-include-response-headers) (taken from test code and commits, not a parameter reference)
- Deep Agents is "an opinionated harness on top of `create_agent`" with **sub-agents that have isolated context windows**, filesystem and skills, all on LangGraph checkpointing. Any `CompiledStateGraph` can be plugged in as a sub-agent — [deepagents README](https://github.com/langchain-ai/deepagents)

**OpenAI Agents SDK**
- HITL: `needs_approval` (a bool or a callable that "fail[s] closed"). Paused runs expose `result.interruptions`. `result.to_state()` returns state you `approve()` or `reject()`, and `Runner.run(agent, state)` resumes it. **`state.to_json()` / `RunState.from_json(agent, stored)` allows resume in another process.** The docs warn: "Only deserialize snapshots from trusted storage" — [HITL docs](https://openai.github.io/openai-agents-python/human_in_the_loop/)
- `ModelSettings.extra_headers` ("Additional headers to provide with the request") and `extra_args` — [model_settings.py](https://github.com/openai/openai-agents-python/blob/main/src/agents/model_settings.py)
- A grep of `openai_responses.py` found no use of `with_raw_response` or response-header surfacing — [openai_responses.py](https://github.com/openai/openai-agents-python/blob/main/src/agents/models/openai_responses.py) (source grep)

**Microsoft Agent Framework (Python)**
- Workflow checkpoints are taken at the end of each superstep and capture executor state, pending messages, pending requests/responses and shared state. Three backends ship: `InMemoryCheckpointStorage`, **`FileCheckpointStorage`** (local disk, "survive process restarts") and `CosmosCheckpointStorage`. A new workflow instance can be rehydrated with `workflow.run(checkpoint_id=..., checkpoint_storage=...)`. It "must preserve the topology and executor identities" — [MAF checkpoints](https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints)
- Checkpoints use **pickle**, with a restricted unpickler and an `allowed_checkpoint_types` allow-list. "Never load checkpoints from untrusted or potentially tampered sources" — [MAF checkpoints](https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints)
- Checkpoints also cover "pending requests and responses", which is the HITL request/response mechanism — [MAF checkpoints](https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints)
- A `declarative` package exists at `python/packages/declarative` (YAML agents/workflows) — [agent-framework repo](https://github.com/microsoft/agent-framework/tree/main/python/packages/declarative) (existence confirmed by code search; contents not read)

**Google ADK**
- Resumability: `ResumabilityConfig(is_resumable=True)` on `App` (Python 1.16.0+). You resume by `invocation_id` through `/run_sse` or `runner.run_async`. Tools run "at least once". "Resume is not supported by default for Custom Agents." You must not modify the workflow before resuming. The page does not say which session services persist this state — [ADK resume](https://adk.dev/runtime/resume/)
- Agent Config (YAML) is **experimental**. It supports `name, model, description, instruction, tools, sub_agents (config_path), agent_class`, and "currently only supports Gemini models" — [ADK Agent Config](https://adk.dev/agents/config/)
- `LoopAgent` has `max_iterations` ([loop_agent.py](https://github.com/google/adk-python/blob/main/src/google/adk/agents/loop_agent.py)). There is a built-in OTel telemetry module ([adk telemetry](https://github.com/google/adk-python/tree/main/src/google/adk/telemetry)), and `lite_llm.py` references `extra_headers` ([lite_llm.py](https://github.com/google/adk-python/blob/main/src/google/adk/models/lite_llm.py)) (code search)

**CrewAI**
- Flows use `@start/@listen/@router` plus `or_`/`and_`. State is a dict or a Pydantic model. `@persist` defaults to `SQLiteFlowPersistence`, and you resume with `kickoff(inputs={"id": ...})`. `@human_feedback` (1.8.0+) can let an LLM collapse the free-text feedback into routed outcomes. **The page does not explain cross-process resume of a paused flow** — [CrewAI Flows](https://docs.crewai.com/en/concepts/flows)
- The core dependency is `openai>=2.30`. LiteLLM and anthropic are optional extras — [crewai pyproject](https://github.com/crewAIInc/crewAI/blob/main/lib/crewai/pyproject.toml)
- Telemetry docs cover the `CREWAI_DISABLE_TELEMETRY` opt-out — [CrewAI telemetry doc](https://github.com/crewAIInc/crewAI/blob/main/docs/v1.14.6/en/telemetry.mdx) (code search hit)

**Strands**
- Interrupts can be raised from hooks or tools, and only `BeforeToolCallEvent` is interruptible. Pending interrupts can be session-managed and answered later "under a new agent session". The `HumanInTheLoop` intervention plus a session manager handles stateless deployments — [Strands interrupts](https://strandsagents.com/docs/user-guide/concepts/interrupts/); [Strands HITL](https://strandsagents.com/docs/user-guide/concepts/agents/interventions/human-in-the-loop/)

**Agno**
- A paused run exposes `needs_confirmation / needs_user_input / needs_external_execution` and is resumed with `continue_run`. "Continuation by run ID requires the same database and session identity" — [Agno HITL](https://docs.agno.com/hitl/overview)
- Observability goes through OpenInference (Opik, MLflow, Logfire examples) — [Agno cookbook](https://github.com/agno-agi/agno/tree/main/cookbook/observability)

**Burr / Haystack / LlamaIndex**
- Burr writes state after every action. Runs are keyed by `app_id` and `partition_key` and resumed with `initialize_from()`. `SQLLitePersister` ships, and custom persisters can be written. You can fork from any `sequence_id` — [Burr persistence](https://burr.apache.org/concepts/state-persistence/)
- Haystack breakpoints work on any component or Agent (by component and visit count). The snapshot can be a JSON file or a callback, can be edited, and is passed back to `run()` to resume. There is no dedicated approval example — [Haystack breakpoints](https://docs.haystack.deepset.ai/docs/pipeline-breakpoints)
- LlamaIndex Workflows: on `InputRequiredEvent`, call `handler.ctx.to_dict()` (JSON-serialisable) and `cancel_run()`, then resume later "in another process". `HumanResponseEvent` and `ctx.wait_for_event` complete the pattern — [LlamaIndex HITL](https://developers.llamaindex.ai/python/framework/understanding/agent/human_in_the_loop/); [llama-agents workflows HITL](https://developers.llamaindex.ai/python/llamaagents/workflows/human_in_the_loop/)

### Comparison matrix (requirements x candidates)

Key: Y = native/documented, P = partial or needs glue, N = no, ? = not verified this session. "(src)" means confirmed by reading source rather than docs.

| Requirement | Pydantic AI v2 | OpenAI Agents SDK | LangGraph (+Deep Agents) | MAF (Python) | Google ADK | CrewAI | Strands | Agno | Burr | Haystack | LlamaIndex WF | smolagents | Letta |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Licence | MIT | MIT | MIT | MIT | Apache-2.0 | MIT | Apache-2.0 | Apache-2.0 | Apache-2.0 | Apache-2.0 | MIT | Apache-2.0 | Apache-2.0 |
| Maturity / API stability | v2 stable (Jun-26), very fast cadence | 0.x | 1.x, most mature | 1.x GA | 2.x | 1.x | 1.x | 3.x | 0.x incubating | 3.x | 2.x | 1.x, slowing | 0.x, slowing |
| Provider-agnostic | Y | P (OpenAI-first) | Y (via LangChain) | Y (Azure-leaning) | P (Gemini-first; LiteLLM wrapper) | Y | Y (Bedrock-first) | Y | N/A (BYO) | Y | Y | Y | Y |
| Custom base_url | Y (provider/client injection) | Y (custom client) ? | Y | Y ? | Y via LiteLLM ? | Y | Y ? | Y ? | BYO | Y ? | Y ? | Y ? | Y ? |
| Per-request headers | Y `extra_headers` (src) | Y `extra_headers` (src) | Y (`default_headers`) ? | ? | P (LiteLLM `extra_headers`, src) | ? | ? | ? | BYO | ? | ? | ? | ? |
| Read response headers | N (errors only, src) | N (src grep) | **Y** `include_response_headers` (OpenAI; Anthropic PR) | ? | ? | ? | ? | ? | BYO | ? | ? | ? | ? |
| Cross-process HITL pause/resume | Y (serialised conversation + DeferredToolResults) | Y (`RunState.to_json`) | Y (checkpointer + thread_id) | Y (FileCheckpointStorage) | P (resumable App, 1.16+; not Web UI/CLI) | P (persist by id; cross-process HITL unclear) | Y (session-managed interrupts) | P (same DB + session) | Y (persister) | Y (snapshot JSON) | Y (`ctx.to_dict`) | N | Y (server-side state) ? |
| Local file/SQLite checkpoint | P (you store the JSON) | P (you store the JSON) | Y SQLite | Y file | ? | Y SQLite | Y file session ? | Y SQLite ? | Y SQLite | Y JSON | P (you store the dict) | N | N (server+DB) |
| Loops / conditional edges | Y (pydantic-graph) | P (code) | Y | Y | Y (LoopAgent, sub-agents) | Y (router) | Y (Graph) ? | Y (workflows) ? | Y | Y | Y | N | N |
| Sub-agent / agent-as-tool | Y (delegation via tools) ? | Y (handoffs, agents-as-tools) ? | Y (Deep Agents sub-agents) | Y | Y (sub_agents, AgentTool) ? | Y | Y (agents-as-tools, Swarm) ? | Y (Teams) | BYO | P | P | Y (managed agents) ? | P |
| Declarative YAML/MD agents | Y AgentSpec (no custom tools in spec) | N ? | N ? | Y declarative pkg | P (experimental, Gemini-only) | Y (YAML agents/tasks) ? | ? | ? | N | Y (pipeline YAML) ? | N | N | P (agent files) ? |
| Structured output validation | Y (Pydantic native) | Y | Y | Y | Y | Y | Y | Y | BYO | Y | Y | P | P |
| OpenTelemetry | Y (Logfire/OTel) ? | P (own tracing; processors) ? | P (LangSmith-first; OTel export) ? | Y ? | Y (src) | P (own telemetry, opt-out) | Y ? | P (OpenInference) | Y ? | Y ? | Y ? | P ? | ? |
| Loop/budget guards | Y UsageLimits incl. `cost_limit`, `request_limit`, `tool_calls_limit` | Y `max_turns` ? | Y `recursion_limit` ? | ? | Y `LoopAgent.max_iterations` (src) | Y `max_iter` ? | ? | Y ? | BYO | Y max agent steps ? | ? | Y `max_steps` ? | ? |
| Stuck/repetition detection | N | N | N | N | N | N | N | N | N | N | N | N | N |
| Lock-in / heavy deps | Low (slim package, optional extras) | Medium (OpenAI tracing defaults) | Medium-high (LangChain ecosystem, LangSmith pull) | Medium (Azure extras, pickle checkpoints) | Medium (Gemini/Vertex pull) | Medium (opinionated, telemetry) | Medium (AWS pull) | Medium (AgentOS platform) | Low | Medium (RAG-centric) | Medium | Low | High (server) |

### Inferences
- The pipeline model (linear steps, gates, bounded review loops, conditional branches, no parallelism) is simple enough that a **graph engine adds little**. The hard, reusable part is the *inner agent loop*: tool calling, provider adapters, structured-output validation and retries, usage limits, HITL deferral and OTel. Pick the agent-loop library on those grounds and write our own pipeline state machine, persisting to the run folder as JSON or YAML.
- **Pydantic AI v2 matches the requirements best.** Handoff front matter is Pydantic, so validation is native. `extra_headers` in ModelSettings covers agent-identity headers, and per-run settings overrides mean the step/pipeline headers can be set per call. UsageLimits (including `cost_limit`) covers budget guards. AgentSpec YAML is a ready-made base for the plain-English agent template. Serialisable deferred-tool conversations give cross-process HITL inside an agent. The durable-execution adapters (DBOS, Temporal, Prefect, Restate) leave a path to org scale. Risks: v2 is only four months old and minor releases come almost daily, so pin versions and isolate it behind our own `AgentRunner` interface. Its `cost_limit` presumably uses its own price tables, while our budgets must come from TokenLean headers, so we need our own budget enforcer.
- **OpenAI Agents SDK is a close second for HITL.** `RunState.to_json` is the cleanest cross-process resume primitive found. It is still 0.x, though, OpenAI-first, and its tracing defaults point at OpenAI's backend.
- **LangGraph is the safe choice if we want an off-the-shelf graph plus checkpointer.** It is the only stack with native response-header surfacing (`include_response_headers`). Deep Agents already implements parent/child sub-agents with isolated context. The cost is LangChain's abstraction weight and the pull towards LangSmith.
- **MAF is the strongest "enterprise" alternative.** `FileCheckpointStorage` maps almost one-to-one onto the "local run folder" requirement, and it ships declarative YAML. Its pickle-based checkpoints and Azure gravity count against it for a portable, proxy-centric tool.
- CrewAI, Agno, Strands and ADK are viable but opinionated (platform, AWS or Gemini pull). ADK's YAML config being Gemini-only rules it out as the declarative layer. smolagents and Letta are weak fits: smolagents has no durability and slowing releases, Letta is server-centric with slowing releases. DSPy is not an orchestrator. It might later help optimise agent instructions. Burr is a credible lightweight state-machine layer if we don't want to hand-roll, but it has a small community (2.6k stars).
- AutoGen should not be used: it is in maintenance mode. AG2 is a smaller community fork.

### Gaps
- I could not verify per-request header support, base_url examples or OTel status for Strands, Agno, Haystack, LlamaIndex, smolagents, Letta or MAF in this session. Matrix cells marked "?" need a quick doc check.
- I did not read the pydantic-graph persistence API (`FileStatePersistence`) in the v2 docs. The fetched page didn't cover it.
- I did not read the contents of MAF's declarative YAML schema, only confirmed that the package exists.
- I don't know whether Pydantic AI `cost_limit` can take an externally supplied cost (for example from response headers) or only uses built-in pricing.
- CrewAI's cross-process resume of `@human_feedback` (an async provider guide exists but was not read).

## Q2. Durable execution engines vs a library's built-in checkpointer: when is each worth it?

### Takeaway
Build v1 on a **plain file-based run-folder checkpoint**: a step-boundary state machine plus serialised agent state. Design each step to be idempotent so a durable engine can be added later without a rewrite. **DBOS** is the best "grow into" engine: MIT, an embedded library, SQLite or Postgres, no separate server, and a first-party Pydantic AI integration. **Temporal** is only worth it at org scale with a platform team. Restate (BSL) and Inngest (SSPL) are **source-available servers** and should be flagged for an OSS-only constraint.

### Cited Findings
- DBOS is "lightweight durable workflows built on top of Postgres". It checkpoints in Postgres and offers queues, notifications and scheduling with "no separate queueing service or message broker — just Postgres" — [dbos-transact-py README](https://github.com/dbos-inc/dbos-transact-py). MIT, 1.6k stars, 3.3.0 (2026-10-15) [GitHub API]
- The DBOS source includes a SQLite system-database implementation (`dbos/_sys_db_sqlite.py`, `_datasource_sqlite.py`) — [dbos _sys_db_sqlite.py](https://github.com/dbos-inc/dbos-transact-py/blob/main/dbos/_sys_db_sqlite.py) (confirmed by code search; the README still talks about Postgres only)
- DBOS describes durable agents as checkpointing every step so "a crashed agent replays its completed steps and resumes", with no orchestrator server — [DBOS blog](https://dbos.dev/blog/durable-execution-crashproof-ai-agents) (vendor source)
- Pydantic AI co-maintains integrations with Temporal, DBOS, Prefect, Restate and AWS Lambda. Durability "keeps one run alive across crashes and restarts, but it does not store chat threads" — [Pydantic AI durable execution](https://pydantic.dev/docs/ai/integrations/durable_execution/overview/)
- Temporal Python SDK: MIT, 1.2k stars, 1.34.0 (2026-09-30) [GitHub API: [temporalio/sdk-python](https://github.com/temporalio/sdk-python)]. A competitor (Diagrid) argues that self-building agent durability on Temporal "requires significant effort to retrofit deterministic workflows for AI's probabilistic nature" — [Diagrid FAQ](https://www.diagrid.io/faq/alternatives-dbos-inngest/what-are-the-underappreciated-integration-costs-when-self-building-a-durable-execution-layer-on-temporal-c) (competitor source, so weigh accordingly)
- Prefect: Apache-2.0, 24.0k stars, 3.8.8 (2026-10-06) — [PrefectHQ/prefect](https://github.com/PrefectHQ/prefect) [GitHub API]
- Restate server: **Business Source License 1.1**. The additional use grant forbids offering a "Public Restate Platform Service". The Python SDK is MIT (v1.0.5) — [restate LICENSE](https://github.com/restatedev/restate); [restatedev/sdk-python](https://github.com/restatedev/sdk-python)
- Inngest server: **Server Side Public License v1.0 with an "Apache 2.0 Future License"** (source-available). The Python SDK is Apache-2.0, and its last release, 0.5.19, was 2026-06-23 — [inngest LICENSE](https://github.com/inngest/inngest); [inngest-py](https://github.com/inngest/inngest-py)
- Hatchet: MIT, 8.1k stars, v0.110.5 (2026-10-06) — [hatchet-dev/hatchet](https://github.com/hatchet-dev/hatchet) [GitHub API]
- Dapr Agents: Apache-2.0, v1.0.9, durable agents built on Dapr workflows — [dapr/dapr-agents](https://github.com/dapr/dapr-agents) [GitHub API]

### Inferences
- Our pause points are coarse: a human gate between steps. The library primitives (Pydantic AI deferred-tool conversations, OpenAI `RunState`, LangGraph checkpointer, MAF `FileCheckpointStorage`) already cover mid-agent tool approvals. Durable engines pay off when a single run needs **crash-recovery mid-step**: long agent runs, many concurrent runs, workers that die, scheduled or webhook triggers with retries and queues. That is the CI/webhook/org phase, not the CLI phase.
- Suggested progression:
  1. CLI v1: run folder with `run.json` (pipeline cursor, loop counters, budget ledger) plus per-step handoff markdown plus serialised agent conversation. Gate = persist and exit. Resume = reload and continue.
  2. Team/CI: put the same step functions under DBOS with SQLite (single node, no infra), then DBOS with Postgres (multi-worker queues for webhooks).
  3. Org platform: Temporal only if the organisation already runs it.
- Hatchet, Inngest and Restate all need a separate server. Inngest and Restate also break a strict OSS-only rule (SSPL and BSL). Prefect is heavier and orchestration-oriented, and fits better if the org already uses it for data pipelines.
- Whatever we pick, steps must be idempotent: ADK warns that tools run "at least once" ([ADK resume](https://adk.dev/runtime/resume/)), and LangGraph nodes re-execute from the start on resume ([LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)). So side-effecting tools (git commits, PR creation) need idempotency keys either way.

### Gaps
- I did not confirm the maturity or official docs status of DBOS's SQLite mode (code exists, README is Postgres-only). Check the DBOS docs before relying on it.
- I did not verify claims about "DBOSify" (a Temporal drop-in) from a vendor-adjacent blog.
- I did not verify Temporal server licence (believed MIT) or Prefect's pause/suspend semantics in this session.

## Q3. Model-client layer: which handles custom base_url + headers + reading response headers best?

### Takeaway
Use the **native `openai` and `anthropic` SDKs**: TokenLean is already OpenAI- and Anthropic-compatible and does the routing. Both SDKs support `base_url`, default and per-request `extra_headers`, `with_options()`, `.with_raw_response` for headers, and an injectable `httpx` client with event hooks. That injectable client is the cleanest **framework-independent** place to stamp identity headers and harvest `x-tokenlean-*` cost headers. LiteLLM duplicates TokenLean's job, and its SDK-side response-header access is documented as OpenAI-only. any-llm and aisuite add nothing here.

### Cited Findings
- openai-python: "The 'raw' Response object can be accessed by prefixing `.with_raw_response.` to any HTTP method call" and `response.headers.get('X-My-Header')`. It supports custom `base_url` or `OPENAI_BASE_URL`, plus `extra_query`, `extra_body` and `extra_headers` request options. `with_options()` customises per request, and the httpx client can be swapped for "proxies, transports, authentication, event hooks" — [openai-python README](https://github.com/openai/openai-python). Apache-2.0, v3.31.0 (2026-10-14) [GitHub API]
- anthropic-sdk-python: MIT, v1.15.0 (2026-10-15) — [anthropics/anthropic-sdk-python](https://github.com/anthropics/anthropic-sdk-python) [GitHub API]. (Its `with_raw_response` / `extra_headers` parity with openai-python was not re-checked this session.)
- LiteLLM: MIT, except the `enterprise/` directory, which has its own licence — [litellm LICENSE](https://github.com/BerriAI/litellm). In the SDK, provider response headers need `litellm.return_response_headers = True` and are read from `response._hidden_params`, which is "currently only supported for openai". The proxy prefixes provider headers with `llm_provider-` — [LiteLLM response headers](https://docs.litellm.ai/docs/proxy/response_headers)
- any-llm (Mozilla AI): Apache-2.0, 1.36.0. It supports "Custom OpenAI-compatible Endpoints" and its own "Otari Gateway". The README does not mention response-header access — [any-llm README](https://github.com/mozilla-ai/any-llm)
- aisuite: MIT, v0.1.3 (2026-07-20), last push 2026-09-18 — [andrewyng/aisuite](https://github.com/andrewyng/aisuite) [GitHub API]. No evidence of header access found.
- Pydantic AI and the OpenAI Agents SDK both expose `extra_headers` but do not surface successful-response headers — [pydantic-ai settings.py](https://github.com/pydantic/pydantic-ai/blob/main/pydantic_ai_slim/pydantic_ai/settings.py); [openai-agents model_settings.py](https://github.com/openai/openai-agents-python/blob/main/src/agents/model_settings.py)
- LangChain `ChatOpenAI(include_response_headers=True)` places headers in `response_metadata["headers"]` — [LangChain response metadata](https://python.langchain.com/docs/how_to/response_metadata)

### Inferences
- Recommended glue, a "TokenLean transport":
  - Build one `httpx.AsyncClient` per run with an `event_hooks={"request": [...], "response": [...]}` pair. The request hook reads a `contextvars.ContextVar` holding `(agent, pipeline, step, team, run_id, parent_agent)` and sets the identity headers. The response hook parses `x-tokenlean-*` and posts the cost to the run's budget ledger, raising or flagging when a budget is exceeded.
  - Pass that client into `AsyncOpenAI(base_url=..., http_client=...)` and `AsyncAnthropic(base_url=..., http_client=...)`, then into Pydantic AI's providers, or whichever framework we choose.
  - This works for every framework that accepts an injected SDK client, so our cost accounting doesn't depend on framework support.
- Streaming caveat: cost headers arrive with the response headers, before the token stream finishes. If TokenLean computes the final cost only after the stream completes, it would need trailers or a follow-up usage call. This is a TokenLean design question, not a client one.
- Avoid LiteLLM as a second router behind TokenLean: it is a double routing layer and a heavy dependency.

### Gaps
- I don't know whether TokenLean sends cost headers on streaming responses (header-time vs. end-of-stream).
- I did not verify Anthropic SDK raw-response and event-hook docs this session (expected parity, since both are Stainless-generated).
- I could not confirm a reported 2026 LiteLLM PyPI supply-chain incident in this session. Check before adopting LiteLLM.

## Q4. Prior art for stuck-loop detection: is there a reusable OSS implementation?

### Takeaway
The only substantial reusable implementation found is **OpenHands' `StuckDetector`** (MIT, Python). It detects five patterns over a window of 20 events: repeated action/observation, repeated action/error, monologue, ping-pong alternation and context-window error loops. **Goose's `RepetitionInspector`** (Apache-2.0, Rust) is a simpler consecutive identical-tool-call limiter. None of the mainstream orchestration frameworks ship repetition detection; they offer only count-based caps (max turns, requests, iterations).

### Cited Findings
- OpenHands `StuckDetector` "detects when an agent is stuck in repetitive or unproductive patterns". It checks: 1) repeating action-observation cycles, 2) repeating action-error cycles, 3) agent monologue, 4) repeating alternating action-observation patterns, 5) context-window errors. It scans at most `MAX_EVENTS_TO_SCAN_FOR_STUCK_DETECTION = 20` events since the last user message. Thresholds are configurable via `StuckDetectionThresholds` — [stuck_detector.py](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/stuck_detector.py) (MIT, [repo](https://github.com/OpenHands/software-agent-sdk))
- Default thresholds: 4+ identical action-observation pairs, 3+ action-error repeats, 3+ monologue messages, 6+ alternating cycles. Comparison is semantic: tool name, action content and thought, "ignoring IDs and metrics". It is on by default (`stuck_detection=True`), with `conversation.stuck_detector.is_stuck()` available. A TypeScript port and tests exist — [OpenHands stuck detector guide](https://github.com/OpenHands/docs/blob/main/sdk/guides/agent-stuck-detector.mdx)
- Goose `RepetitionInspector` tracks `last_call`, `repeat_count` and per-tool `call_counts`, matching on identical tool `name` and `parameters`, with `max_repetitions` — [goose tool_monitor.rs](https://github.com/aaif-goose/goose/blob/main/crates/goose/src/tool_monitor.rs) (Apache-2.0; repo now `aaif-goose/goose`, 55.3k stars)
- Pydantic AI recommends `request_limit` against "infinite loops" and has no dedicated detector — [Pydantic AI agent docs](https://pydantic.dev/docs/ai/core-concepts/agent/). ADK `LoopAgent.max_iterations` — [loop_agent.py](https://github.com/google/adk-python/blob/main/src/google/adk/agents/loop_agent.py)

### Inferences
- Port the OpenHands algorithm (MIT, keep the attribution) into our runner at two levels:
  - (a) **Intra-agent:** a hook or capability on each tool call or model response, keyed on `(tool_name, canonical_json(args))` hashes and on normalised observation hashes, using OpenHands' default thresholds.
  - (b) **Inter-step (pipeline loops):** for review → developer → review cycles, hash or diff successive handoff documents and reviewer findings. If iteration N's review findings match N-2 (oscillation), or the developer's diff is empty or identical, short-circuit before `max_loops` and escalate to a human gate.
- Combine this with hard caps (UsageLimits / `max_turns`, a per-loop `max_iterations`, and TokenLean cost budgets) as the backstop.

### Gaps
- I found no academic or benchmark data on false-positive rates for these heuristics.
- I did not check Cline, Aider or Claude Code for comparable OSS logic.

## Q5. Ranked recommendation: what to build on, and what glue we still write

### Takeaway
**Build our own thin pipeline runtime and use Pydantic AI v2 as the agent-loop engine**, behind an internal `AgentBackend` interface. Add LangGraph-free, file-based checkpoints, a home-grown TokenLean httpx transport, a ported OpenHands stuck detector, and DBOS as the later durability upgrade. Backends for external CLI agents (Claude Agent SDK, Codex) plug into the same interface.

### Cited Findings
- Pydantic AI has: YAML/JSON AgentSpec ([docs](https://pydantic.dev/docs/ai/core-concepts/agent-spec/)), `extra_headers` ([settings.py](https://github.com/pydantic/pydantic-ai/blob/main/pydantic_ai_slim/pydantic_ai/settings.py)), UsageLimits ([docs](https://pydantic.dev/docs/ai/core-concepts/agent/)), cross-process deferred-tool resume ([docs](https://pydantic.dev/docs/ai/tools-toolsets/deferred-tools/)), and Temporal/DBOS/Prefect/Restate adapters ([docs](https://pydantic.dev/docs/ai/integrations/durable_execution/overview/))
- OpenAI Agents SDK `RunState.to_json` for cross-process HITL — [docs](https://openai.github.io/openai-agents-python/human_in_the_loop/)
- LangGraph checkpointer + `interrupt()` — [docs](https://docs.langchain.com/oss/python/langgraph/interrupts); Deep Agents sub-agents — [README](https://github.com/langchain-ai/deepagents)
- MAF `FileCheckpointStorage` (pickle-based) — [docs](https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints)
- Claude Agent SDK (MIT) and Codex (Apache-2.0) exist as actively released external-agent backends — [claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python); [codex](https://github.com/openai/codex) [GitHub API]

### Inferences

**Ranking for the core engine**
1. **Pydantic AI v2 (agent loop) + own pipeline state machine.** This is the best fit on Pydantic-native handoffs, headers, budget guards, declarative specs, OTel and durable-engine options. It has low lock-in. Mitigate the churn risk by pinning versions and wrapping it.
2. **OpenAI Agents SDK.** Excellent HITL serialisation and agents-as-tools, but it is 0.x, OpenAI-centric, and its tracing must be redirected to OTel. It's a good fallback, or a second `AgentBackend`.
3. **LangGraph (+ Deep Agents).** Choose this if we would rather adopt a mature graph and checkpointer than write about 500 lines of state machine. It is the only stack that reads response headers natively. The cost is heavier dependencies and LangChain abstractions.
4. **Microsoft Agent Framework.** The strongest built-in file checkpointing and declarative YAML. Pickle checkpoints and Azure gravity are the drawbacks. Consider it if the target orgs are Microsoft-centric.
5. **Burr** as a minimal state-machine layer (alternative to hand-rolling), paired with any agent loop.
6. Not recommended as the core: CrewAI (opinionated, telemetry), Agno (platform-centric), Strands (AWS-centric), Google ADK (Gemini-only YAML, resumability caveats), Haystack and LlamaIndex (RAG-centric), smolagents (no durability), Letta (server), AutoGen (maintenance), AG2 (small), DSPy (not an orchestrator). Mastra is TypeScript only.

**Glue we would still write, regardless of library**
1. **Pipeline runtime:** a step/gate/loop/branch state machine, loop counters with max N, and a branch-condition evaluator over handoff front matter.
2. **Run-folder checkpointing:** atomic writes of `run.json` and per-step artefacts, gate pause/exit, resume in a new process (`resume <run-id>`), and schema versioning of saved state.
3. **Agent template → AgentSpec compiler:** our plain-English template (tier/model, effort, temperature, tool allow-list, input/output schemas) maps onto Pydantic AI AgentSpec plus Python tool registration, because custom tools can't live in the spec.
4. **Handoff docs:** a markdown + YAML front-matter parser/renderer with Pydantic validation, used as `output_type`, with retries on validation failure.
5. **TokenLean transport** (Q3): identity headers via contextvars and an httpx request hook; cost harvesting via a response hook into a per-run and per-agent budget ledger; enforcement that aborts or pauses a run.
6. **Child-agent spawning:** an agent-as-tool wrapper enforcing depth limits, tool-set narrowing (child tools must be a subset of the parent's) and budget slicing (a child budget drawn from the parent's ledger), with child identity in the headers (`parent_agent`).
7. **Stuck detector** (Q4): an OpenHands port plus inter-iteration oscillation checks.
8. **OTel conventions:** span attributes for run/pipeline/step/agent/team and cost, emitted alongside the library's own instrumentation (GenAI semantic conventions).
9. **External CLI backend adapter:** Claude Agent SDK and Codex behind the same `AgentBackend` protocol, with budget and identity propagated where those CLIs allow a custom base_url.
10. **Later:** a DBOS adapter for durable steps, a webhook/CI trigger service, and a web UI and VS Code extension reading the run folder or an event log.

### Gaps
- I did not benchmark how much code the hand-rolled pipeline state machine needs compared with adopting LangGraph or Burr. The estimate above is a judgement call.
- I did not verify whether Claude Code and Codex CLIs honour a custom base_url and extra headers, which determines whether CLI-backend steps can be metered by TokenLean.
