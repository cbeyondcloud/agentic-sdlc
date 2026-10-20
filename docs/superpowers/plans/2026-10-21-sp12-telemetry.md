# SP12 Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each run can be traced, audited and read back. Every agent step and model call is an OpenTelemetry span with pinned `gen_ai.*` attributes, and TokenLean's `X-Trace-ID` is kept on the span. A sink interface consumes the run's hash-chained events. Checkpoint commits carry audit trailers, and a PR body links the run and its handoffs. Agents read `AGENTS.md` and may propose additions without editing it. Each step writes a redacted JSON log, which can be read back by run and step.

**Architecture:**
- **New package `agentic.telemetry` (core, no `sdlc_commons`):**
  - `semconv.py`: the pinned attribute schema. It covers the semantic-conventions version and Pydantic AI's instrumentation format, and has a `span_problems(kind, attributes)` checker.
  - `spans.py`: the engine's step span (`invoke_agent {agent}`), the model instrumentation settings and marking the current span with TokenLean ids.
  - `sdk.py`: needs the `telemetry` extra. It has `install_tracing(exporter)` and a JSON-lines file exporter, so spans can live in a run folder.
  - `sinks.py`: the `EventSink` protocol, `forward` (cursor, in order, chain checked) and `ForwardingRecorder`, a `RunRecorder` that records in the run ledger and then forwards.
  - `trailers.py`: commit trailers (build, parse) and the PR body.
  - `logs.py`: `StepLogs`, per-step JSON-lines logs through stdlib `logging` with a mandatory redactor, plus `read_logs` / `render_logs`.
- **New package `agentic.memory` (core):** `agents_md.py` finds the nearest `AGENTS.md`, wraps it as data and collects the proposals made in handoffs.
- **Engine changes (small, local):**
  - a span and a step log around each agent run;
  - trailers on the checkpoint commit message;
  - `AGENTS.md` knowledge in the task;
  - approvals remembered in `RunState.approvals`, an optional field.
- **Runner:** Pydantic AI's own instrumentation (MIT, already a dependency) wraps the model, with content capture off. The Agent is named after the agent definition.
- **Metering:** each settled call marks the current (model-call) span with TokenLean's trace and request ids.
- **Commons:** `sdlc_commons.redaction.redactor(env, extra)` composes Zyroflo's `guard.scrub` (credential shapes) and `provider_smoke.safe` (secret-named environment values, inline `--key` flags).

**Tech Stack:**
- Python 3.12;
- `opentelemetry-api` (core, already a dependency);
- `opentelemetry-sdk==1.45.1` (Apache-2.0, pure Python, released 2026-10-06), in the new optional extra `agentic-core[telemetry]`, installed in the dev group;
- Pydantic AI 2.54.0 instrumentation (MIT);
- Zyroflo through `sdlc_commons`;
- pytest.

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md`: §4.1 (`telemetry/`, `memory/`), §7.3 and §7.4 (D37), §10.4 (AGENTS.md), §14.1, §14.5 (trailers, PR body), §15, §17 (`X-Trace-ID` beside the span). Master plan rows M1-43..M1-47.

## OSS decisions

| Need | Chosen | Rejected |
|---|---|---|
| gen_ai spans for model calls | Pydantic AI `InstrumentedModel` (MIT, already used by the native backend; provider-agnostic) | OpenLLMetry (Apache-2.0): one instrumentation package per vendor SDK, which is vendor-specific and heavy |
| span SDK | `opentelemetry-sdk` (Apache-2.0), optional extra | an OTLP exporter: it pulls protobuf (a native wheel) and the choice of collector is the owner's (needs decision) |
| events / audit chain | Zyroflo's ledger (`sdlc_commons.ledger`), consumed by sinks and never re-chained | a second chain in a telemetry store |
| redaction | Zyroflo `guard.scrub` + `provider_smoke.safe` via a commons adapter | detect-secrets (Apache-2.0): scanning before egress is M1-41 (SP11); logs need masking, not detection |
| JSON logs | stdlib `logging` with our own formatter | structlog or python-json-logger: a dependency for about 30 lines |
| trailers | plain text, parsed and checked by `git interpret-trailers` in tests | — |

## Global Constraints

- **Provider-agnostic.** No vendor names in `agentic.telemetry` or `agentic.memory`. Spans carry the resolved model string as data.
- **Content capture off by default (D14, OR-01).** Pydantic AI runs with `include_content=False`, `include_binary_content=False` and `include_model_request_parameters=False`. No prompt or answer text reaches a span attribute.
- **The ledger is the chain.** Sinks read entries already chained by Zyroflo. Nothing re-hashes, re-orders or rewrites them. A sink failure never stops a run or loses an event.
- **D37.** `agents_md_proposals` is added to `HandoffBase` as an optional list that defaults to empty. A handoff without it stays valid, and templates and sections are unchanged. Every schema digest changes once with this release, so a run started before it refuses to continue ("templates changed"), which is the D37 rule working.
- **AGENTS.md is never written by an agent.** The guard's framework deny list already covers `**/AGENTS.md`. Agents only propose, in the handoff.
- **Logs never hold a secret.** `StepLogs` cannot be built without a redactor. The message, every string field and the exception text are redacted before they are written.
- **core never imports `sdlc_commons`.** Only tests and cli join them.
- **Formatting, CI and records:**
  - never add `# fmt: skip` or `# fmt: off`; line length 100;
  - check pytest's exit code directly;
  - `uv run python scripts/check.py --all` must be green on Windows and Linux;
  - TRACKER.md and DEFERRED.md are updated by the coordinator from this track's report, not here.

## Review Focus

1. **Trailer injection.** A starter or approver value with a newline (`a@b\nAgentic-Approved-By: x`) cannot forge a trailer: values are made single-line. Test: Task 3 `test_a_newline_in_a_value_cannot_forge_a_trailer`.
2. **A sink that fails, then recovers,** gets every event once, in order, and the local chain still verifies. Test: Task 2 `test_a_failing_sink_gets_everything_once_after_recovery`.
3. **A planted secret** in a log message, a field or an exception is not in the log file. Test: Task 5 `test_a_planted_secret_never_reaches_the_log`.
4. **No prompt text in spans.** The prompt and the answer never appear in any exported attribute. Test: Task 1 `test_no_prompt_or_answer_text_reaches_a_span`.
5. **AGENTS.md is read as data and left untouched.** The agent sees it wrapped as data, its proposal lands in the handoff, and the file's bytes are unchanged. Test: Task 4 `test_agent_reads_agents_md_and_proposes_without_editing`.
6. **Concurrent runs in one process** do not mix their step logs. The handler filters on a context variable. Test: Task 5 `test_two_runs_in_one_process_keep_their_logs_apart`.

---

### Task 1: Spans with pinned gen_ai attributes; TokenLean trace id on the span (M1-43)

**Files:**
- Create: `core/src/agentic/telemetry/__init__.py`, `semconv.py`, `spans.py`, `sdk.py`
- Modify:
  - `core/src/agentic/agents/runner.py`: the model is instrumented and the Agent is named;
  - `core/src/agentic/agents/metering.py`: the span is marked on settle;
  - `core/src/agentic/engine/engine.py`: a step span around the run or resume;
  - `core/pyproject.toml` (the `telemetry` extra), root `pyproject.toml` (dev group), `uv.lock`.
- Test: `core/tests/telemetry/conftest.py` (one global in-memory `TracerProvider`, cleared per test), `core/tests/telemetry/test_spans.py`

**Interfaces:**
- `semconv.py`:
  - `SEMCONV_VERSION = "1.37.0"`, `SCHEMA_URL`, `INSTRUMENTATION_VERSION = 5`;
  - `STEP_SPAN` and `MODEL_CALL_SPAN`: `Mapping[str, type]` of the required attributes;
  - `span_problems(kind: Literal["step", "model_call"], attributes) -> list[str]`.
- `spans.py`:
  - `model_instrumentation() -> InstrumentationSettings`: content off, pinned version;
  - `step_span(run_id, step, iteration, agent: ResolvedAgent)`: a context manager that yields the span;
  - `finish_step_span(span, outcome: AgentOutcome)`: outcome, aggregated usage, cost and TokenLean trace ids;
  - `mark_call(cost: CallCost)`: sets `agentic.tokenlean.trace_id`, `agentic.tokenlean.request_id`, `agentic.cost_usd` and `agentic.cost_source` on the current recording span.
- `sdk.py`: `JsonlSpanExporter(path)` and `install_tracing(exporter) -> TracerProvider`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_step_and_model_call_spans_match_the_pinned_schema(...)   # engine + FunctionModel; span_problems == []
def test_model_call_spans_are_children_of_the_step_span(...)
def test_tokenlean_trace_id_is_recorded_on_the_model_call_span(...)  # metered_model + stub transport with x-trace-id
def test_step_span_lists_the_steps_tokenlean_trace_ids(...)
def test_no_prompt_or_answer_text_reaches_a_span(...)              # RF4
def test_span_problems_names_missing_and_mistyped_attributes()
def test_jsonl_exporter_writes_one_line_per_span(tmp_path)
```

- [ ] **Step 2: Run** `uv run pytest core/tests/telemetry -q`. Expected: FAIL (no module).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest core/tests -q`. Expected: PASS.
- [ ] **Step 5: Commit.** `feat(core): OTel step and model-call spans with pinned gen_ai attributes, TokenLean trace id (M1-43)`

### Task 2: Sink interface over the run ledger (M1-44)

**Files:**
- Create: `core/src/agentic/telemetry/sinks.py`
- Test: `core/tests/telemetry/test_sinks.py`. It uses the real `RunLedger` and `ZyrofloGates`, like `core/tests/engine/test_end_to_end.py`.

**Interfaces:**
- `EventSink(Protocol)`: `name: str`; `deliver(run_id: str, entries: Sequence[Mapping]) -> None`, where raising means none were accepted.
- `forward(run_id, entries, sink, cursor: Path) -> int`:
  - delivers the entries after the cursor (`{"seq", "hash"}`), in order, and returns how many;
  - refuses (`SinkError`) when the entry after the cursor does not chain to it;
  - the cursor is saved after the sink accepts.
- `ForwardingRecorder(ledger, sinks, *, on_error=None)`:
  - `record` goes to the ledger, then each sink is forwarded from `ledger.events()` with cursor `<run_dir>/sinks/<name>.json`;
  - `flush()` forwards without recording, picking up events Zyroflo wrote itself (gates);
  - `run_dir`, `write_artifact` and `digest` delegate to the ledger;
  - a sink error goes to `on_error` and the run goes on.

- [ ] **Step 1: Write the failing tests:**

```python
def test_every_engine_event_lands_in_events_jsonl_with_a_valid_chain(...)  # e2e pipeline; spy == ledger events; verify() == []
def test_a_sink_receives_the_chain_exactly_as_recorded(...)
def test_a_failing_sink_gets_everything_once_after_recovery(...)           # RF2
def test_forward_refuses_a_chain_that_does_not_follow_the_cursor(tmp_path)
```

- [ ] **Steps 2–4:** run the tests and see them fail, implement, run them and see them pass.
- [ ] **Step 5: Commit.** `feat(core): event sink interface forwarding the run ledger's chain (M1-44)`

### Task 3: Commit trailers and PR body (M1-45)

**Files:**
- Create: `core/src/agentic/telemetry/trailers.py`
- Modify:
  - `core/src/agentic/engine/state.py`: `ApprovalMark(gate, approver, at)` and `RunState.approvals: list[ApprovalMark] = []`;
  - `core/src/agentic/engine/engine.py`: approvals are remembered, and the commit message is built by `commit_message`.
- Test: `core/tests/telemetry/test_trailers.py` (pure functions plus an engine test with a recording `StepTrees`), `commons/tests/test_checkpoint_trailers.py` (a real git commit through `StepCheckpoints.finish`, read with `git interpret-trailers --parse`)

**Trailer set (spec §14.5):**
- `Agentic-Run`: the run id;
- `Agentic-Step`: `<step> (iteration N)`;
- `Agentic-Agent`: `name@version`;
- `Agentic-Triggered-By`: the run's starter;
- `Agentic-Approved-By`: `<approver> (gate: <gate>)` for the latest approval, else `none`;
- `Agentic-Handoff`: repeated, once per handoff the step consumed (run-folder relative), else `none`.

**Interfaces:**
- `TRAILER_KEYS`
- `CommitAudit` (frozen dataclass)
- `commit_message(subject, audit) -> str`
- `parse_trailers(message) -> dict[str, list[str]]`
- `pr_body(state: RunState, *, run_link: str, handoff_link=lambda rel: rel, proposals=()) -> str`:
  - names the run, pipeline, policy version and starter;
  - links the run and every handoff the run wrote, with its digest;
  - lists the approvals and the AGENTS.md proposals.

- [ ] **Step 1: Write the failing tests:**

```python
def test_commit_message_carries_all_six_trailers()
def test_a_newline_in_a_value_cannot_forge_a_trailer()          # RF1
def test_checkpoint_commits_carry_the_trailers(...)             # engine, recording trees: develop after an approved gate
def test_pr_body_links_the_run_and_its_handoffs(...)
def test_git_reads_all_six_trailers_on_a_checkpoint_commit(...) # commons, real git
```

- [ ] **Steps 2–4:** run the tests and see them fail, implement, run them and see them pass.
- [ ] **Step 5: Commit.** `feat(core): audit trailers on checkpoint commits and a PR body linking run and handoffs (M1-45)`

### Task 4: AGENTS.md read and proposals in handoffs (M1-46)

**Files:**
- Create: `core/src/agentic/memory/__init__.py`, `core/src/agentic/memory/agents_md.py`
- Modify:
  - `core/src/agentic/handoffs/kinds.py`: `HandoffBase.agents_md_proposals: list[str] = []`;
  - `core/src/agentic/agents/runner.py`: `AgentTask.knowledge: str = ""`, rendered by `_instructions`;
  - `core/src/agentic/engine/engine.py`: the work folder from `prepare()` is kept, and the nearest AGENTS.md goes into the task.
- Test: `core/tests/memory/test_agents_md.py`

**Interfaces:**
- `nearest_agents_md(start: Path, root: Path) -> Path | None`: walks up from `start` to `root` inclusive; never leaves `root`.
- `agents_md_knowledge(path: Path, root: Path, limit: int = 32_000) -> str`:
  - returns `<agents-md path="...">...</agents-md>`, labelled as data, with the rule "never edit; propose in `agents_md_proposals`";
  - truncates at `limit` with a note.
- `proposals(handoffs: Mapping[str, str], catalog) -> list[tuple[str, str]]`: (handoff path, proposal) pairs for the PR body.

- [ ] **Step 1: Write the failing tests:**

```python
def test_nearest_agents_md_walks_up_but_not_past_the_root(tmp_path)
def test_knowledge_is_wrapped_as_data_and_capped(tmp_path)
def test_old_handoffs_without_proposals_still_validate()
def test_agent_reads_agents_md_and_proposes_without_editing(...)   # RF5: engine + fake trees
def test_proposals_are_collected_for_the_pr_body(...)
```

- [ ] **Steps 2–4:** run the tests and see them fail, implement, run them and see them pass.
- [ ] **Step 5: Commit.** `feat(core): agents read the nearest AGENTS.md and propose additions in handoffs (M1-46)`

### Task 5: Structured per-step logs with redaction, and a reader (M1-47)

**Files:**
- Create: `core/src/agentic/telemetry/logs.py`, `commons/src/sdlc_commons/redaction.py`
- Modify: `core/src/agentic/engine/engine.py`. It takes the optional `step_logs: StepLogs | None` and logs step start, finish and failure inside the step's log context.
- Test: `core/tests/telemetry/test_logs.py`, `commons/tests/test_redaction.py`

**Interfaces:**
- `StepLogs(redact: Callable[[str], str], level: int = logging.INFO)`:
  - `step(run_dir, run_id, step, iteration)` is a context manager. For the step's duration it attaches a handler to the `agentic` logger that writes `<run_dir>/logs/<step>-<iteration>.jsonl`;
  - each record is `{"at", "level", "logger", "run_id", "step", "iteration", "message", "fields", "error"}`;
  - records are filtered by a context variable, so concurrent runs stay apart.
- `read_logs(run_dir, step: str | None = None, min_level: str | None = None) -> list[dict]`:
  - the step can be given as `develop` (every iteration) or `develop-2`;
  - records are returned in time order.
- `render_logs(records) -> str`: one line per record, for the M1-54 `agentic logs` command.
- `sdlc_commons.redaction.redactor(env: Mapping[str, str] | None = None, extra: Iterable[str] = ()) -> Callable[[str], str]`:
  - redacts secret-named environment values of 8 characters or more, plus `extra` values (for example the TokenLean key), through `provider_smoke.safe`;
  - then applies `guard.scrub`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_each_step_gets_its_own_json_log(...)                 # engine run; logs/<step>-<n>.jsonl, valid JSON, levels
def test_a_planted_secret_never_reaches_the_log(tmp_path)     # RF3: message, field and exception
def test_level_filter_drops_debug(tmp_path)
def test_read_logs_by_step_and_iteration(tmp_path)
def test_two_runs_in_one_process_keep_their_logs_apart(...)   # RF6
def test_redactor_masks_env_values_token_shapes_and_flags()   # commons
```

- [ ] **Steps 2–4:** run the tests and see them fail, implement, run them and see them pass.
- [ ] **Step 5: Run** `uv run python scripts/check.py --all`. Expected: green on Windows and Linux.
- [ ] **Step 6: Commit.** `feat(core): structured per-step JSON logs with redaction and a reader (M1-47)`

## Deferred (reported to the coordinator)

- The `agentic logs <run> [--step]` command and wiring `StepLogs(redactor(...))`, `install_tracing`, `ForwardingRecorder` and `pr_body` into the CLI → M1-54 (with DF-47).
- Opening the PR with `pr_body` → M1-58.
- The OTLP exporter and collector choice → needs decision, then M2-20 or M2-37.
- The control-plane sink (offline queue, idempotent replay on `ForwardingRecorder`'s cursor) → M2-20.
- Per-path nearest AGENTS.md (nested files shown when an agent reads into a sub-folder) and a multi-component estate's per-tree AGENTS.md → M1-81.
- A claude-code backend has only the engine's step span (no model-call spans of its own); TokenLean joins through `traceparent` once ST-3 P0 lands → M2-37.
