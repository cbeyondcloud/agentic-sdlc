# Governance, configuration, handoff-validation, observability and CLI building blocks (Python 3.12, as of 16 Oct 2026)

Method note: package versions, release dates, licences and `requires_python` were pulled from the PyPI JSON API on 2026-10-16 (cited as the PyPI project page). Narrative claims come from official docs/repos where possible; secondary/vendor sources are flagged. Nothing below is copyleft. The only "source-available / proprietary" items found are the Logfire platform (SDK is MIT), Cerbos Hub (the PDP is Apache-2.0), and Langfuse's `ee` features (not re-verified this round; see Gaps).

## 1. Layered config merging with per-field precedence and admin-locked fields

### Takeaway
No mainstream Python config library natively implements "org-locked fields cannot be overridden by lower layers". All of them do ordered override and merge; locking has to be our own thin layer (about 100-200 lines) on top of a merger plus Pydantic validation. The cleaner design is to not treat restricted fields as merge semantics at all. Merge only the repo-controllable fields, and enforce restricted fields (model/tier, provider, tools, spend) as policy checks in preflight.

### Cited Findings
| Library | Version (date) | Licence | Py | Layering / merge | Locking / provenance | Fit |
|---|---|---|---|---|---|---|
| Dynaconf | 3.3.5 (2026-08-05) | MIT | >=3.10,<3.15 | Override in load order; `dynaconf_merge` for deep merge; validators | No lock feature found; has `inspect_settings`/`get_history` provenance | Medium. Dict-like, weakly typed |
| Hydra (hydra-core) | 1.3.7 (2026-09-14) | MIT | n/a | Config groups + defaults list, composition | None for locking | Low. Built for ML experiment composition; takes over `main()` |
| OmegaConf | 2.4.0 (2026-10-09) | BSD-3 | >=3.10 | `OmegaConf.merge`, interpolation, custom resolvers | **Read-only flag** (`set_readonly` raises `ReadonlyConfigError` on write). **Struct flag** blocks unknown keys. Flags inherit to child nodes | Best primitive for locking a node, but merge-into-readonly behaviour is undocumented |
| pydantic-settings | 2.16.0 (2026-10-14) | MIT | >=3.11 | Source priority is tuple order in `settings_customise_sources`. Ships YAML/TOML/JSON sources | No locking; no per-field provenance | Good for the typed final model and env/secrets, not for N-layer file merging |
| confz | 2.1.0 (2025-02-07) | MIT | >=3.8 | Pydantic-based multi-source | None found | Stale (no 2026 release) |
| dature | 1.6.1 (2026-10-16) | Apache-2.0 | >=3.12 | Dataclass-first; 4 merge strategies + **per-field merge rules** (`append`, `prepend`, field groups) | `debug=True` shows which source supplied each value; locking not mentioned | Young but closest to our needs (per-field rules + provenance) |
| jsonmerge | 1.9.2 (2023-07-19) | MIT | n/a | JSON-Schema-driven merge strategies (`mergeStrategy` per schema node) | Could express "keep base" per field via strategy | Unmaintained since 2023 |
| deepmerge | 3.0.1 (2026-09-01) | MIT | >=3.8 | Configurable per-type merge strategies | None | Good low-level merger |

Sources:
- Dynaconf override is by loading order; `dynaconf_merge` is the merge mechanism; validators can run at construction — [PyPI dynaconf](https://pypi.org/project/dynaconf/), [Dynaconf 3.x release notes](https://fr.github.com/dynaconf/dynaconf/blob/master/3.x-release-notes.md), [dature "why not dynaconf" (competitor page)](https://dature.readthedocs.io/en/latest/comparison/why-not-dynaconf/)
- No Dynaconf locked/frozen key feature turned up. The "freezable fields: lock specific configuration layers to prevent downstream overrides" pattern exists only in a Rust crate, einstellung — [docs.rs einstellung](https://docs.rs/crate/einstellung/0.1.2)
- OmegaConf: "A read-only configuration cannot be modified" (`ReadonlyConfigError`). Struct mode prevents creation of fields that do not exist. Flags inherit from the parent node. The page does not document merging into a read-only target — [OmegaConf usage docs](https://omegaconf.readthedocs.io/en/2.3_branch/usage.html)
- pydantic-settings: priority is set by the order of the tuple returned from `settings_customise_sources`. `YamlConfigSettingsSource` and `TomlConfigSettingsSource` must be wired in explicitly — [pydantic-settings docs mirror](https://docsearch.algolia.com/mcp/docs/repo/pydantic/pydantic-settings), [commit example](https://github.com/pydantic/pydantic-settings/commit/c2fd92ffa74b14ed38fd8fcb1f77d087ca4a960e)
- dature feature table (per-field merge rules, `debug=True` source tracing, secret masking, typed dataclass result). This is self-published — [dature comparison](https://dature.readthedocs.io/en/latest/comparison/why-not-dynaconf/)
- Versions, licences and dates — PyPI: [dynaconf](https://pypi.org/project/dynaconf/), [hydra-core](https://pypi.org/project/hydra-core/), [omegaconf](https://pypi.org/project/omegaconf/), [pydantic-settings](https://pypi.org/project/pydantic-settings/), [confz](https://pypi.org/project/confz/), [dature](https://pypi.org/project/dature/), [jsonmerge](https://pypi.org/project/jsonmerge/), [deepmerge](https://pypi.org/project/deepmerge/)
- Prior art for layered agent definitions with an org layer on top: Claude Code resolves duplicate subagent names by priority: managed settings (org) > `--agents` CLI flag > `.claude/agents/` (project) > `~/.claude/agents/` (user) > plugin. Note that project beats user there. — [Claude Code subagents docs](https://code.claude.com/docs/en/sub-agents)

### Inferences
- Recommended pattern:
  1. Load each layer (org, team, user, repo) as YAML/front matter with ruamel.yaml (MIT, 0.19.1, [PyPI](https://pypi.org/project/ruamel.yaml/)) so line numbers are kept for error messages.
  2. Deep-merge only the repo-controllable sections (instructions, schemas, pipeline shape) with deepmerge, or with dature if per-field rules are wanted.
  3. Validate the result into a Pydantic v2 model.
  4. Keep restricted fields out of the merge entirely. The org policy file is the only source for the tier-to-model map, provider, tools allow-list and budgets. Agent and pipeline files may only *request* (e.g. `tier: deep-reasoning`, `tools: [...]`), and preflight checks each request against policy.
  This avoids the need for "locking" semantics and makes violations explicit errors rather than silent clamps.
- If true locking is wanted, mark fields in the Pydantic policy model (e.g. `Field(json_schema_extra={"locked": True})`). After merging, compare the value from each lower layer against the org value and raise with provenance. Track provenance as a `dict[path, layer]` during the merge. Dynaconf's `get_history` and dature's `debug` show that provenance is expected UX.
- Hydra is a poor fit: it assumes it owns the app entrypoint and the experiment-sweep model.

### Gaps
- Whether `OmegaConf.merge()` raises when a lower layer tries to change a read-only node was not confirmed in docs. It needs a 5-line spike before relying on it.
- I found no Python library offering "admin-locked keys" as a first-class feature.

## 2. Policy engines for admin rules vs. Pydantic-validated policy files

### Takeaway
For v1 the admin rules are data (tier-to-model maps, per-team model allow-lists, tool allow-lists, budget numbers), not complex logic. Pydantic-validated policy YAML plus about 100 lines of Python checks is enough, and a policy engine would be overkill. If rule logic later grows (conditions on repo, branch, data classification, time), the best embedded upgrade is Cedar via `cedarpy`. Fallbacks are Rego via `regorus` or an OPA sidecar. The model call path is enforced again by the TokenLean proxy, so preflight is a fast-fail UX layer and the proxy is the hard enforcement point.

### Cited Findings
| Engine | Python package (version, date) | Licence | Deployment | Notes |
|---|---|---|---|---|
| Cedar | `cedarpy` 4.12.2 (2026-10-14) | Apache-2.0 (repo) | In-process (Rust wheels; Linux/macOS/Windows; Py 3.10-3.14) | `is_authorized`, `is_authorized_batch`, `is_authorized_partial`, `validate_policies` against a `Schema`, `format_policies`. **Not official**: "not officially supported by AWS or the Cedar Policy team"; single PyPI maintainer (k9security-devops). Major/minor track Cedar engine (v4.12) |
| OPA / Rego | OPA server (Go) + `opa-python-client` 3.0.0 (2026-09-30, MIT) | OPA: Apache-2.0 | Sidecar/daemon or CLI binary | CNCF graduated. In Aug 2025 the OPA creators and many Styra staff joined Apple. OPA blog says no change to governance or licensing. Styra commercial products (DAS) being wound down / open-sourced (secondary reports) |
| Rego (embedded) | `regorus` (Microsoft, Rust, pyo3/maturin bindings) | Not verified this round | In-process | "Mostly compliant" with OPA v1.2.0. Defaults to Rego v1. Not all builtins supported. Custom builtins only in Rust. PyPI JSON for `regorus` not found under that name |
| Casbin | `casbin` (pycasbin) 1.43.0 (2025-05-10) | Apache-2.0 | In-process | Model `.conf` + policy CSV/adapters. RBAC strong; ABAC is "syntax sugar" level. Repo now under `github.com/apache/casbin-pycasbin` |
| Cerbos | `cerbos` SDK 0.16.1 (2026-10-12) | Apache-2.0 (SDK and PDP) | Separate Go PDP service (or Hub-managed WASM ePDP) | Rich ABAC YAML policies. Cerbos Hub is commercial SaaS. Heaviest operationally for a CLI |

Sources:
- cedarpy API, version, platform table, "not officially supported" note — [PyPI cedarpy](https://pypi.org/project/cedarpy/); Apache-2.0 licence — [k9securityio/cedar-py](https://github.com/k9securityio/cedar-py)
- OPA stays CNCF graduated with "no changes to the project governance or licensing" after maintainers joined Apple — [OPA blog note from Teemu, Tim and Torin](https://openpolicyagent.org/blog/note-from-teemu-tim-and-torin-to-the-open-policy-agent-community-2dbbfe494371); Styra DAS sunset framing comes from competitors — [Oso post](https://osohq.com/post/opa-maintainers-join-apple-oss-community-to-maintain-styra-products), [Cloud Native Now](https://cloudnativenow.com/editorial-calendar/best-of-2025/apple-buys-styra-brains-opa-remains-open-2/)
- regorus: Rust Rego interpreter, mostly compliant with OPA v1.2.0, not all builtins, pyo3 bindings — [docs.rs regorus](https://docs.rs/regorus). `regopy`/`regorust` are different projects wrapping Microsoft rego-cpp — [PyPI regopy](https://pypi.org/project/regopy)
- opa-python-client 3.0.0, MIT — [PyPI](https://pypi.org/project/opa-python-client/)
- pycasbin Apache-2.0, ABAC model support, repo under the Apache org — [apache/casbin-pycasbin](https://github.com/apache/casbin-pycasbin), [PyPI casbin](https://pypi.org/project/casbin/)
- Cerbos PDP Apache-2.0. Cerbos Hub is the commercial SaaS, with a Hub-managed WASM ePDP — [cerbos/cerbos](https://github.com/cerbos/cerbos/), [AppSecSanta overview](https://appsecsanta.com/cerbos); Python SDK 0.16.1 Apache-2.0 — [PyPI cerbos](https://pypi.org/project/cerbos/)

### Inferences
- The preflight questions are lookups and arithmetic:
  - Does tier X map to a model?
  - Is that model in team T's allow-list?
  - Is each tool in the allow-list?
  - Is the remaining budget at least the estimate?
  A typed `OrgPolicy` Pydantic model with these fields gives schema validation, good error messages, JSON Schema export for editor autocompletion, and zero extra runtime. Policy-as-code engines pay off when non-developers author conditional rules or when the same rules must run in several services. Neither is a v1 requirement.
- Design the policy check behind an interface such as `PolicyDecider.check(principal, action, resource, context) -> Decision`, so Cedar can be dropped in later. Cedar's principal/action/resource/context model maps cleanly: user/team -> `use_model` / `use_tool` / `spend` -> model / tool / budget.
- Prefer Cedar over OPA for an embedded CLI: in-process wheel, schema-validated policies, no sidecar. The risk is a single-maintainer, unofficial binding. Mitigation: pin the version, and vendor or fork if needed.
- Avoid Cerbos for v1, because it needs a running PDP. It could become reasonable later, when the web UI/server exists.
- Spend limits are best enforced by TokenLean (it sees actual tokens). Preflight should read the remaining budget from TokenLean rather than keep its own ledger.

### Gaps
- regorus Python package name on PyPI and its licence were not confirmed (I believe MIT, unverified).
- Whether TokenLean exposes a budget-remaining API is outside this research.

## 3. Handoff format: markdown + YAML front matter validated against per-agent schemas; reusable templates

### Takeaway
Use `python-frontmatter` to split front matter from body, Pydantic v2 models (or repo-supplied JSON Schema validated with `jsonschema`) per agent for the front matter, and `mdformat` to normalise bodies. For the BA/Design/Review/QA document templates, reuse MIT-licensed GitHub spec-kit templates (spec/plan/tasks), BMAD templates (PRD, architecture, story; MIT code but BMAD trademark) and MADR for design decisions. Checking that step N's output schema matches step N+1's input has no off-the-shelf library. It needs a small custom JSON Schema compatibility check.

### Cited Findings
| Library / asset | Version (date) | Licence | Role |
|---|---|---|---|
| python-frontmatter | 1.3.0 (2026-05-20) | MIT | Parse/write `---` YAML front matter + body |
| pydantic | 2.14.1 (2026-10-11) | MIT | Per-agent handoff models; `model_json_schema()` export |
| jsonschema | 4.26.0 (2026-01-07) | MIT | Validate against repo-authored JSON Schema files |
| jsonschema-rs | 0.58.6 (2026-10-06) | MIT | Fast Rust validator alternative |
| datamodel-code-generator | 0.85.0 (2026-10-16) | MIT | Generate Pydantic models from repo JSON Schema |
| mdformat | 1.0.0 (2025-10-16) | MIT | CommonMark formatter for handoff bodies |
| ruamel.yaml | 0.19.1 (2026-01-02) | MIT | Round-trip YAML with comments and line numbers |
| GitHub spec-kit templates | n/a | MIT (per third-party profile) | `templates/` for constitution, spec, plan, tasks; copied into `.specify/templates/` |
| BMAD-METHOD templates | n/a | MIT code; separate BMad trademark terms | Product brief, PRD, architecture, epics, stories, sprint status |
| MADR | v4.0.0 | Not confirmed | ADR template; front matter `status`, `date`, `decision-makers`, `consulted`, `informed` |

Sources:
- PyPI: [python-frontmatter](https://pypi.org/project/python-frontmatter/), [pydantic](https://pypi.org/project/pydantic/), [jsonschema](https://pypi.org/project/jsonschema/), [jsonschema-rs](https://pypi.org/project/jsonschema-rs/), [datamodel-code-generator](https://pypi.org/project/datamodel-code-generator/), [mdformat](https://pypi.org/project/mdformat/), [ruamel.yaml](https://pypi.org/project/ruamel.yaml/)
- spec-kit artefacts: spec.md holds testable user stories, scenarios, edge cases, requirement IDs and measurable outcomes. plan.md holds technical context checked against the constitution, plus research, data-model, contracts and quickstart. tasks.md is a dependency-ordered phase list. Licence is reported as MIT by a third-party profile (July 2026) — [Plannotator spec-kit profile](https://docs.plannotator.ai/frameworks/github-spec-kit), [Mintlify spec-kit templates](https://www.mintlify.com/github/spec-kit/advanced/templates), [CodeMySpec guide](https://codemyspec.com/blog/github-spec-kit-guide)
- BMAD: MIT repo licence with separate trademark terms. Templates for product brief, PRD, architecture, epics, stories and sprint status. The PM -> Architect -> SM -> Dev agent handoff chain — [Plannotator BMAD profile](https://docs.plannotator.ai/frameworks/bmad-method), [BMAD core architecture doc (npm mirror)](https://cdn.jsdelivr.net/npm/@ptnglobal/bmad-method@4.43.1/docs/core-architecture.md)
- MADR v4.0.0 fields and status values (proposed/accepted/rejected/deprecated/superseded) — [pyvista-wasm MADR adoption](https://pyvista-wasm.readthedocs.io/en/latest/decisions/0000-use-markdown-architectural-decision-records.html), [community ADR skill](https://smithery.ai/skills/claylo/capturing-decisions) (secondary)
- Prompt/markdown front matter with published JSON Schema as precedent: PromptPex publishes a JSON schema for its Prompty-compatible front matter — [PromptPex prompt format](https://microsoft.github.io/promptpex/reference/prompt-format/)

### Inferences
- Handoff doc contract:
  - Front matter has fixed envelope fields owned by the framework (`handoff_version`, `run_id`, `step_id`, `from_agent`, `to_agent`, `status`, `created_at`, `artifacts[]`, `cost`).
  - It also has a `payload` object validated against the producing agent's `output_schema`.
  - The markdown body is human-readable rationale.
  - Pydantic models give both runtime validation and a JSON Schema the VS Code YAML extension can use for autocompletion later.
- Schema compatibility (step N output vs step N+1 input): if both sides are Pydantic, the check is to instantiate the consumer model from the producer's schema-derived example, or to compare required fields and types. A practical v1 rule is "every required property of N+1's input exists in N's output with a compatible JSON type". That is about 50-100 lines over `model_json_schema()`, with no dependency.
- Reuse the spec-kit `spec-template.md` and `plan-template.md` as the BA/Design handoff bodies, a BMAD story template for dev handoff, and MADR for design decisions. Add our front matter envelope on top. Credit sources in NOTICE. Avoid "BMAD" naming in the product, because of the trademark.

### Gaps
- MADR licence was not confirmed. I believe upstream adr/madr is dual MIT/CC0, but this needs checking in the repo LICENSE.
- spec-kit's MIT licence is confirmed only via a third-party profile, not the repo LICENSE file directly.
- I found no maintained Python library for JSON Schema subset/compatibility checking.

## 4. Pipeline and agent definition format prior art: is there an emerging standard?

### Takeaway
There is no single winning "agent-in-a-file" standard as of Oct 2026. Three clusters exist:
1. Markdown + YAML front matter agent files, the de facto format in coding tools. The Claude Code subagent format is the most detailed. Agent Skills (`SKILL.md`) is widely adopted, and AGENTS.md is now stewarded by the Linux Foundation's Agentic AI Foundation.
2. Formal, framework-neutral YAML specs: Oracle Open Agent Spec, and Microsoft Agent Framework declarative agents/workflows.
3. Prompt-asset formats: Prompty v2.

Recommendation: adopt a Claude-Code-compatible markdown front matter shape for agent files (plus our `tier`, `input_schema`, `output_schema` fields), a GitHub-Actions-like YAML for pipelines, and keep Oracle Agent Spec as an optional export target.

### Cited Findings
| Format | Owner / licence | Shape | Status (Oct 2026) | Relevance |
|---|---|---|---|---|
| Claude Code subagents | Anthropic (format, docs) | `.md` with YAML front matter: required `name`, `description`; optional `tools`, `disallowedTools`, `model` (alias, full ID or `inherit`), `permissionMode`, `maxTurns`, `skills`, `mcpServers`, `hooks`, `memory`, `effort`, `isolation`, etc. Body is the system prompt. Unknown fields silently ignored | Production | Closest match to our "markdown agent" idea; precedence managed > CLI > project > user > plugin |
| Agent Skills (`SKILL.md`) | Released by Anthropic 18 Dec 2025 as an open standard | Folder with `SKILL.md` (front matter + body) + scripts/references; metadata loaded at start, body on demand | 27-30+ tools reportedly read it (counts vary; secondary) | Reuse for tool/capability packaging, not pipeline definitions |
| AGENTS.md | Agentic AI Foundation (Linux Foundation), alongside MCP and Goose | Repo-level instructions file | Stewarded by AAIF | Repo-instructions layer input |
| Oracle Open Agent Spec / `pyagentspec` | Oracle; Apache-2.0 OR UPL-1.0; pyagentspec 26.3.1 (2026-09-11) | Framework-agnostic declarative YAML/JSON for Agents and Flows; LLM configs (OpenAI, OCI, Ollama); conformance suite | Active; adapters for LangGraph, AutoGen, CrewAI; WayFlow reference runtime | Most rigorous neutral spec; good export/import target |
| Microsoft Agent Framework declarative | Microsoft; MIT; `agent-framework-declarative` 1.3.0 (2026-10-15) | YAML `kind: Prompt`, `name`, `instructions`, `model` (with `=Env.X` expressions); `AgentFactory.create_agent_from_yaml`, `WorkflowFactory` | Declarative *workflows* stable/1.0; declarative *agent* loading marked experimental | Prior art for workflow YAML; Azure-centric |
| Prompty v2 | Microsoft; MIT; `prompty` 2.0.3 (2026-10-16) | `.prompty` markdown: front matter `model` (id/provider/connection), `template`, `inputs`; Jinja2 role-tagged body | v2 repo says alpha / format may change | Prompt-level asset, not agent/pipeline |
| CrewAI YAML | CrewAI; `crewai` 1.15.28 (2026-10-14); Py <3.14 | `agents.yaml` + `tasks.yaml` | Production | Simple prior art; framework-bound |
| OpenAI Agents SDK | OpenAI; MIT; `openai-agents` 0.24.0 (2026-10-15) | Code-first (no file format found) | Production | Not a file standard |
| AGNTCY OASF | Linux Foundation (moved Jul 2025), part of AAIF since Dec 2025 | Schema records for agent *discovery* (capabilities, dependencies) | Active | Directory/discovery, not execution definitions |

Sources:
- Claude Code front matter table, locations and precedence — [Claude Code subagents](https://code.claude.com/docs/en/sub-agents)
- Agent Skills release date and adoption counts (secondary, conflicting counts) — [Product Builder: Agent Skills](https://productbuilder.net/learn/agent-skills), [ExplainThis: Agent Skills](https://explainthis.io/en/ai/agent-skills); AAIF stewards MCP, AGENTS.md, Goose (secondary) — same sources
- Oracle Agent Spec: dual Apache-2.0/UPL-1.0, Agents and Flows, LangGraph/AutoGen/CrewAI adapters, WayFlow runtime, Python >=3.10 — [oracle/agent-spec](https://github.com/oracle/agent-spec); conformance suite — [arXiv 2510.04173](https://arxiv.org/html/2510.04173v2); version — [PyPI pyagentspec](https://pypi.org/project/pyagentspec/)
- MS Agent Framework declarative YAML shape and `AgentFactory` — [Microsoft Learn: declarative agents](https://learn.microsoft.com/en-gb/agent-framework/agents/declarative); agent loading experimental vs workflows stable — [package page](https://simple-repository.app.cern.ch/project/agent-framework-declarative); Python package 1.0.0 and `WorkflowFactory` — [MS Agent Framework blog, Jul 2026](https://devblogs.microsoft.com/agent-framework/?p=5678); version 1.3.0 — [PyPI](https://pypi.org/project/agent-framework-declarative/)
- Prompty v2 front matter and alpha status — [microsoft/prompty](https://github.com/Microsoft/prompty/), [PyPI prompty](https://pypi.org/project/prompty/)
- AGNTCY to Linux Foundation (Jul 2025), OASF for discovery, AAIF component (Dec 2025) — [Converge Digest](https://convergedigest.com/linux-foundation-launches-agntcy-project-to-standardize-ai-agent-interoperability/), [Outshift AGNTCY](https://outshift.cisco.com/the-internet-of-agents/agntcy), [docs.agntcy.org](https://docs.agntcy.org)
- Versions — [PyPI crewai](https://pypi.org/project/crewai/), [PyPI openai-agents](https://pypi.org/project/openai-agents/)

### Inferences
- Agent file: markdown + front matter using Claude Code field names where they overlap (`name`, `description`, `tools`, `model` -> replace with `tier`, `maxTurns`, `skills`, `mcpServers`). Add `tier`, `input_schema`, `output_schema`, `handoff_template`. Users can then port Claude Code / Copilot-style agents with minimal edits, and editors already highlight the format. Unlike Claude Code, which silently ignores unknown fields, fail on unknown fields in strict mode, because governance needs typo detection.
- Pipeline file: GitHub Actions-like YAML (`steps:` with `id`, `agent`, `needs`, `gate: human`, `with:` inputs). This is familiar to the target users. Argo/Tekton are Kubernetes-CRD-shaped and too heavy for a local CLI (not researched in depth).
- Oracle Agent Spec is the only formal, licence-clean, conformance-tested neutral spec. Keep an optional `export --format agentspec` on the roadmap rather than adopting it as the authoring format. Its YAML is more verbose than markdown-first authoring, and it lacks governance fields such as tiers and budgets.
- Reuse `SKILL.md` folders for packaged tools/capabilities, and read `AGENTS.md` as repo-level instructions input.

### Gaps
- Argo Workflows / Tekton and GitHub Actions schema details were not re-researched; they are common knowledge, but not cited here.
- The GitHub Copilot `.agent.md` custom agent schema was not found in sources.
- The exact AAIF governance of AGENTS.md is only confirmed via secondary sources.

## 5. Observability: which conventions and SDKs to emit so traces join TokenLean's

### Takeaway
Emit plain OpenTelemetry (opentelemetry-sdk 1.45.1) using the OTel GenAI semantic conventions (`gen_ai.*`; all still "Development" status, now in a separate `semantic-conventions-genai` repo, schema `gen-ai/1.42.0`). Add Langfuse-recognised attributes (`session.id`/`langfuse.session.id`, `user.id`) and propagate W3C `traceparent` on every HTTP call to the TokenLean proxy, so proxy spans become children of the step spans. Use structlog for JSON logs carrying `trace_id`/`span_id`. Optionally use OpenLLMetry (Apache-2.0) for auto-instrumentation. The Langfuse Python SDK (MIT, OTel-native) and the Logfire SDK (MIT) are both usable, but the Logfire *platform* is closed source.

### Cited Findings
| Option | Version (date) | Licence | Conventions emitted | Notes |
|---|---|---|---|---|
| OpenTelemetry Python SDK | 1.45.1 (2026-10-06); semconv pkg 0.66b1 | Apache-2.0 | Whatever we set | Baseline; vendor-neutral |
| OTel GenAI semconv | repo `open-telemetry/semantic-conventions-genai`, schema `gen-ai/1.42.0` | Apache-2.0 | `gen_ai.*`: `invoke_agent {agent}`, `create_agent`, `execute_tool` operation | **All Development**; no tagged release of the new repo; `gen_ai.provider.name` replaced `gen_ai.system`; opt-in via `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` |
| OpenLLMetry (`traceloop-sdk`) | 0.62.4 (2026-09-29) | Apache-2.0 | OTel GenAI (1.37+ per Datadog) | Auto-instruments OpenAI/Anthropic/etc. clients; Langfuse accepts it |
| OpenInference (Arize) | semconv 0.1.41 (2026-10-01); openai instr. 0.1.65 | Apache-2.0 | `openinference.span.kind`, `llm.*`, `input.value`/`output.value` | Predates GenAI semconv; Langfuse maps `llm.*`; separate namespace from TokenLean's likely `gen_ai.*` |
| Langfuse Python SDK | 5.0.2 (2026-10-16), Py >=3.11 | MIT | `langfuse.*` + OTel | "thin layer on top of the official OpenTelemetry client"; `propagate_attributes()` |
| Logfire SDK | 5.2.0 (2026-10-13) | MIT (SDK); platform closed source (self-host needs enterprise licence) | OTel | Can export to any OTel backend |
| structlog | 26.1.0 (2026-06-06) | MIT OR Apache-2.0 | n/a | JSON logs; add a processor injecting current OTel trace/span IDs |

Sources:
- GenAI semconv: no `gen_ai.*` attribute Stable as of 20 Jul 2026. All Development ("SHOULD NOT be used in production", "MAY be removed without prior notice"). Moved to `semantic-conventions-genai` with schema `gen-ai/1.42.0` and no tagged releases. Core v1.42.0 tagged 12 Jun 2026. Agent spans `create_agent`/`invoke_agent {gen_ai.agent.name}`. `execute_tool` operation. `gen_ai.provider.name` replaces `gen_ai.system`. Recommends pinning the schema version, setting `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`, and setting sampling-relevant attributes at span start — [ecorpit analysis (secondary, cites spec)](https://ecorpit.com/opentelemetry-genai-conventions-llm-agent-tracing-2026/); corroborated by [DEV: GenAI conventions not stable yet](https://dev.to/azena-ai/opentelemetrys-genai-semantic-conventions-are-not-stable-yet-heres-what-actually-shipped-in-2026-3mke), [Greptime May 2026](https://www.greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions), [Dash0 explainer](https://www.dash0.com/knowledge/opentelemetry-genai-semantic-conventions-explained). Older (Mar 2026) guides describe "experimental" status before the repo move; prefer the newer sources.
- Langfuse OTLP endpoint `/api/public/otel` (HTTP JSON/protobuf only, no gRPC), Basic auth with pk/sk, `x-langfuse-ingestion-version=4` for real-time ingestion. Attribute precedence `langfuse.*` > `gen_ai.*` > OpenInference `llm.*` > MLflow. Unmapped attributes go to metadata. `session.id`/`user.id` should be on every span via Baggage + `BaggageSpanProcessor` or `propagate_attributes()`. Langfuse SDKs are a thin layer over OTel — [Langfuse OTel docs](https://langfuse.com/docs/opentelemetry/introduction)
- OpenLLMetry spans follow OTel GenAI 1.37+ — [Datadog OTel instrumentation docs](https://docs.datadoghq.com/llm_observability/instrumentation/otel_instrumentation); Langfuse names OpenLLMetry and OpenLIT as compatible senders — [Langfuse OTel docs](https://langfuse.com/docs/integrations/opentelemetry); Arize can ingest OpenLLMetry traces — [Arize docs](https://arize.com/docs/ax/integrations/opentelemetry/openllmetry)
- Logfire SDK MIT; platform UI/backend closed source; self-host via enterprise licence; SDK exports to any OTel backend — [Logfire FAQ](https://logfire.pydantic.dev/docs/faq), [Logfire README](https://cdn.jsdelivr.net/gh/pydantic/logfire@main/README.md)
- Versions and licences — PyPI: [opentelemetry-sdk](https://pypi.org/project/opentelemetry-sdk/), [opentelemetry-semantic-conventions](https://pypi.org/project/opentelemetry-semantic-conventions/), [traceloop-sdk](https://pypi.org/project/traceloop-sdk/), [openinference-semantic-conventions](https://pypi.org/project/openinference-semantic-conventions/), [langfuse](https://pypi.org/project/langfuse/), [logfire](https://pypi.org/project/logfire/), [structlog](https://pypi.org/project/structlog/)

### Inferences
- Span model per run:
  - Root `run {pipeline}` span with `session.id = run_id` and `user.id`.
  - Child `invoke_agent {agent}` span per step, carrying the requested tier, resolved model and handoff path.
  - Children of each step span: `chat {model}` (or proxy-produced) spans and `execute_tool {tool}` spans.
  - A `preflight` span with each check as an event.
- Joining with TokenLean: instrument the HTTP client (the opentelemetry httpx/requests instrumentations inject `traceparent` automatically; not re-verified). TokenLean's OTel/Langfuse spans then share trace IDs with ours, provided the proxy extracts incoming context. Confirm on the TokenLean side. If TokenLean already emits `gen_ai.*` spans with token/cost data, our framework should **not** duplicate LLM-call spans (avoid double cost counting). Emit agent/step/tool spans and let the proxy own `chat` spans.
- Prefer `gen_ai.*` over OpenInference, so attribute names match what an OTel-based proxy most likely emits. Langfuse maps both anyway.
- Local run folder: also write a `trace.jsonl` via an OTel file/console exporter or structlog, so offline runs are inspectable without a backend. The optional publisher can then replay to OTLP.
- Keep prompt/completion content capture off by default and record it in the local run folder instead. GenAI semconv puts content in opt-in attributes/events.

### Gaps
- Not verified: whether the TokenLean proxy honours incoming `traceparent` (internal system).
- The exact OTel GenAI attribute names for cost (there is no standard `gen_ai.usage.cost`; Langfuse computes cost itself) were not confirmed.
- Langfuse server `ee/` licensing was not re-verified this round.

## 6. CLI and local UX: Typer vs Click vs Cyclopts; Rich/Textual human gate; editor hand-off

### Takeaway
Typer (MIT, Click-based) is the safe default with the largest ecosystem. Cyclopts (Apache-2.0, v5.2.0) is the stronger pick on typing (Unions, Pydantic/attrs/dataclass parameters, docstring help) and suits a Pydantic-heavy codebase. Either works on 3.12. For the human gate, use Rich for diff/markdown rendering plus a prompt (questionary/prompt-toolkit). Escalate to a Textual TUI only if multi-pane review is needed. Editor hand-off is `click.edit()` (respects `$VISUAL`/`$EDITOR`), or a subprocess on a temp file in the run folder.

### Cited Findings
| Library | Version (date) | Licence | Py | Notes |
|---|---|---|---|---|
| Typer | 0.27.3 (2026-10-06) | MIT | >=3.10 | Type-hint CLI on Click; still 0.x |
| Click | 8.5.0 (2026-08-26) | BSD-3-Clause | >=3.10 | Mature; `click.edit()`, `click.launch()` |
| Cyclopts | 5.2.0 (2026-10-06) | Apache-2.0 | >=3.11 | "What you thought Typer was". Docstring-driven help, Union types, proper validation, Pydantic/dataclass/attrs support; about 1.3k stars |
| Rich | 15.0.0 (2026-04-12) | MIT | >=3.9 | Markdown, syntax-highlighted diffs, tables, live progress |
| Textual | 8.2.8 (2026-06-30) | MIT | >=3.9,<4 | Full TUI framework (same authors as Rich) |
| questionary | 2.1.1 (2025-08-28) | MIT | >=3.9 | Select/confirm prompts (approve / reject / edit / comment) |
| prompt-toolkit | 3.0.53 (2026-07-26) | BSD-3 | >=3.10 | Underlying line editor |
| python-statemachine | 3.2.2 (2026-10-14) | MIT | >=3.10 | Optional: model run/step states (pending, running, awaiting_gate, approved, failed) |

Sources:
- Cyclopts licence, claims vs Typer, Py >=3.11, Pydantic support, stars — [BrianPugh/cyclopts](https://github.com/BrianPugh/cyclopts)
- Versions and licences — PyPI: [typer](https://pypi.org/project/typer/), [click](https://pypi.org/project/click/), [cyclopts](https://pypi.org/project/cyclopts/), [rich](https://pypi.org/project/rich/), [textual](https://pypi.org/project/textual/), [questionary](https://pypi.org/project/questionary/), [prompt-toolkit](https://pypi.org/project/prompt-toolkit/), [python-statemachine](https://pypi.org/project/python-statemachine/)
- `click.edit()` opens the user's editor (uses `$VISUAL`/`$EDITOR`, can edit a file in place via `filename=`) — [Click utilities docs](https://click.palletsprojects.com/en/stable/utils/#launching-editors) (known API, page not re-fetched in this session)

### Inferences
- Choose Typer if contributor familiarity matters most. Choose Cyclopts if the CLI will take Pydantic models and Union-typed options directly, which fits a framework already built on Pydantic. Both are thin, so switching cost is low. Cyclopts requires 3.11+, which is fine with the 3.12 target.
- Human gate flow:
  1. Rich renders the handoff markdown and a front-matter validation summary.
  2. A questionary select offers [approve / edit / reject / comment].
  3. "edit" calls `click.edit(filename=handoff_path)`, then re-validates with Pydantic and loops on errors.
  4. The decision is appended to the run folder as an audit record.
  The same gate abstraction later maps to a web UI, a VS Code extension or a PR comment.
- Textual is worth it only for a later "run browser" TUI (list runs, steps, costs). It is not needed for v1 gating.

### Gaps
- No independent, recent Typer-vs-Cyclopts benchmark or comparison article found (search returned noise). The comparison above relies on the Cyclopts README (self-description).

## 7. Secrets and credentials: proxy keys now, SSO/OIDC later

### Takeaway
Define a small `CredentialProvider` interface with ordered resolvers: env var (CI) -> OS keychain via `keyring` (MIT) -> OIDC token cache. Use `authlib` (BSD-3) for generic OIDC/OAuth2 later, or `msal` (MIT) if the org IdP is Entra ID. Never store keys in layered config files. Config holds only a reference (`credential: tokenlean-default`).

### Cited Findings
| Library | Version (date) | Licence | Role |
|---|---|---|---|
| keyring | 25.7.0 (2025-11-16) | MIT | OS keychain (Windows Credential Manager, macOS Keychain, Secret Service) |
| authlib | 1.9.1 (2026-10-14) | BSD-3-Clause | OAuth2/OIDC client library, JWT/JWK |
| msal | 1.39.0 (2026-09-17) | MIT | Microsoft Entra ID auth (incl. device-code / interactive flows) |
| pydantic-settings | 2.16.0 | MIT | `SecretStr` + env/dotenv/secret-file sources for CI |

Sources:
- Versions and licences — PyPI: [keyring](https://pypi.org/project/keyring/), [authlib](https://pypi.org/project/authlib/), [msal](https://pypi.org/project/msal/), [pydantic-settings](https://pypi.org/project/pydantic-settings/)
- pydantic-settings has a built-in file-secret source among its default sources — [pydantic-settings docs mirror](https://docsearch.algolia.com/mcp/docs/repo/pydantic/pydantic-settings)

### Inferences
- v1: `tokenlean login` stores the proxy key with `keyring.set_password("agentic-sdlc", profile, key)`. In CI, read `TOKENLEAN_API_KEY` from env. The OIDC upgrade is a device-authorization flow that caches refresh tokens in keyring and sends short-lived tokens to the proxy. This needs TokenLean to accept OIDC bearer tokens.
- Headless Linux CI often lacks a keyring backend, so env-var precedence must come first.

### Gaps
- Not verified this round: whether authlib provides a client-side helper for the OAuth 2.0 Device Authorization Grant (RFC 8628). msal's device-code flow is well known but was not re-cited.
- Whether TokenLean supports OIDC tokens is unknown.

## 8. Recommended stack (synthesis)

### Takeaway
A Pydantic-centric, mostly-stdlib-shaped stack with no servers, all MIT/BSD/Apache. Custom code is limited to:
- the layer merger with restricted-field enforcement and provenance (about 200 lines)
- the preflight checks (about 150 lines)
- the schema-compatibility check (about 100 lines)
- the run-folder writer and publisher interface

### Cited Findings
| Layer | Pick (licence) | Alternative / upgrade path |
|---|---|---|
| Config parsing | ruamel.yaml (MIT) + python-frontmatter (MIT) | PyYAML |
| Layer merge | deepmerge (MIT) + custom restricted-field guard + provenance map | dature (Apache-2.0, per-field rules, Py>=3.12); OmegaConf read-only nodes (BSD-3) |
| Typed model / validation | pydantic 2.14 (MIT); pydantic-settings 2.16 (MIT) for env/secrets | — |
| Admin policy | Pydantic `OrgPolicy` YAML + Python checks behind a `PolicyDecider` interface | cedarpy (Apache-2.0, unofficial) -> Cedar; OPA (Apache-2.0) sidecar or regorus; Cerbos (Apache-2.0) once a server exists |
| Handoff validation | python-frontmatter + Pydantic per-agent models; jsonschema 4.26 (MIT) for repo JSON Schemas; mdformat (MIT) | jsonschema-rs (MIT) for speed; datamodel-code-generator (MIT) |
| Handoff templates | spec-kit spec/plan/tasks (MIT), BMAD PRD/architecture/story (MIT, trademark caution), MADR (licence TBC) | — |
| Agent file format | Claude-Code-compatible markdown front matter + `tier`/`input_schema`/`output_schema` | Export to Oracle Agent Spec (Apache-2.0/UPL-1.0) |
| Pipeline format | GitHub-Actions-like YAML (`steps`, `needs`, `gate`) | MS Agent Framework declarative workflows (MIT) as reference |
| Observability | opentelemetry-sdk (Apache-2.0) with `gen_ai.*` pinned to `gen-ai/1.42.0`; W3C `traceparent` to TokenLean; Langfuse `session.id`/`user.id`; structlog (MIT/Apache) | traceloop-sdk/OpenLLMetry (Apache-2.0); langfuse SDK (MIT); avoid Logfire platform (closed) |
| CLI | Typer (MIT) or Cyclopts (Apache-2.0) | Click (BSD-3) |
| Human gate UI | Rich (MIT) + questionary (MIT) + `click.edit()` | Textual (MIT) for a later TUI |
| Credentials | keyring (MIT) + env vars | authlib (BSD-3) / msal (MIT) for OIDC |

Sources: see per-layer sections above; licences from PyPI as cited there.

### Inferences
- Python 3.12 compatibility is fine for every pick. Constraints to watch: Cyclopts and pydantic-settings and Langfuse need >=3.11, dature >=3.12, CrewAI <3.14, Dynaconf <3.15.
- Biggest licence/supply-chain risks:
  - cedarpy is single-maintainer and unofficial.
  - The OTel GenAI conventions are explicitly unstable, so isolate attribute names in one module.
  - BMAD trademark.
  - The Logfire platform is proprietary (the SDK is fine).
- Make the TokenLean proxy the hard enforcement point for model/budget, with preflight as an advisory fast-fail. This avoids building a second spend ledger.

### Gaps
- Licences confirmed only via secondary sources: spec-kit (third-party), MADR (unconfirmed), regorus (unconfirmed).
- TokenLean-side capabilities were not researched: traceparent extraction, budget API, OIDC.
