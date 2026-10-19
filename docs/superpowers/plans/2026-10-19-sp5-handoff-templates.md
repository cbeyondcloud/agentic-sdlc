# SP5 Handoff Templates and Validation Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every agent that produces a handoff is told exactly what document to write (a per-kind template), and what it writes is checked: the framework fills in the fields it owns, a broken document goes back to the agent once with the errors, and a second failure ends the run as `invalid` for the engine to put in front of a human.

**Architecture:** Three additions to `agentic.handoffs` and one to the runner. `finalise.py` turns an agent's answer into the final handoff document. It strips one outer code fence and normalises line endings, then stamps the framework-owned envelope (kind, schema_version, run_id, step, agent, produced_at). It also computes the review's `open_blocking` from the findings and the run's `block_on` severities, and validates the result. Errors carry line numbers in the agent's text. `templates/<kind>.md` are the five body templates, adapted from spec-kit, MADR and Zyroflo P6, plus `templates.py` to load them and build the output instructions. `contract.py` combines both into a `HandoffContract`. `PydanticAIRunner` accepts any `OutputContract`: it appends the contract's instructions and registers a Pydantic AI **output validator**, which raises `ModelRetry` with the errors under `retries={"output": 1}`. Pydantic AI does the feeding back; no retry loop of our own. When the retry budget runs out, the runner returns `status="invalid"` with the errors and the last text. One deviation from the master plan: the templates live in `core/src/agentic/handoffs/templates/`, not `library/handoff-templates/`, so an installed `agentic-core` wheel finds them. `library/` holds content that orgs edit, and these templates are part of the contract the framework validates against.

**Tech Stack:** Python 3.12; pydantic-ai-slim 2.54.0 (output validators, `ModelRetry`, `retries={"tools": n, "output": n}`); ruamel.yaml (existing `agentic.yamlio`); Pydantic v2. Template sources: github/spec-kit `templates/spec-template.md` and `templates/plan-template.md` (MIT, Copyright GitHub, Inc.); adr/madr `template/adr-template.md` (MIT OR CC0-1.0; the licence the spec left to confirm is confirmed); Zyroflo `sdlc/stages/P6-review.md` and `sdlc_templates/review-rubric.md` (structure only; the rubric reaches the review agent in M1-60, never copied here, D34).

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md` §7.1 (format: front matter for machines, Markdown body for people; envelope fields framework-owned), §7.2 (kinds, fields, body template sources; "invalid output triggers an automatic retry with the validation errors, then a human gate"), §8.7 (validation failures: retry with errors, then gate), §6 (review loop `until: review.open_blocking == 0`, Zyroflo exit rule). Master plan rows M1-19, M1-20.

## Global Constraints

- Handoff = YAML front matter + Markdown body (§7.1); validation is python-frontmatter-style splitting + Pydantic (`agentic.yamlio.split_front_matter`, `load_yaml`, existing kind models).
- Envelope fields are **framework-owned** (§7.1): `kind`, `schema_version`, `run_id`, `step`, `agent`, `produced_at`. The agent writes `status` and the kind's fields.
- **The review exit is categorical** (Zyroflo P6): the loop ends when no finding at a `block_on` severity is open; the score is reported, never the gate. Default `block_on` = `critical, high` (Zyroflo `profile.yml` and `policy.yml` floor); the regulated regime adds `medium`. The engine (SP6) passes the regime's set.
- "Errors fed back **once**, then gate": exactly one output retry per agent run (`OUTPUT_RETRIES = 1`).
- Documents written by the framework use LF line endings and UTF-8 (Review Focus 1 of the master plan: digests stable across OSes).
- Never copy Zyroflo code or text into this repo (D34). Adapted third-party templates get an entry in `THIRD_PARTY_NOTICES.md`.
- Core does not import `sdlc_commons` or Zyroflo; templates ship inside the `agentic` package so an installed wheel finds them.
- Tests make no network or model calls (`FunctionModel`/`TestModel`); `uv run python scripts/check.py --all` (Windows + Linux) is the gate.
- Keep `ruff format` output as is; never add `# fmt: skip`.

## Review Focus

1. **The model wraps its answer in a ```` ```markdown ```` code fence**, as models often do. Accept it: one outer fence is stripped. Test in Task 1.
2. **A review claims `open_blocking: 0` while it lists an open critical finding**, which would end the review loop early. The framework recomputes the count from the findings and the agent's number is ignored. Test in Task 1.
3. **The agent writes envelope fields itself**: another run id, another agent's name, or the wrong kind. The framework's values win, and a different `kind` is an error the agent sees. Test in Task 1.
4. **An answer with CRLF line endings.** The final document is LF, so its digest is the same on every OS. Test in Task 1.
5. **A model that answers with plain prose, twice.** The run ends `status="invalid"` with the errors and the last text kept for the human. It never raises, and nothing is retried a third time. Test in Task 3.

---

### Task 1 (M1-19, part 1): finalising an agent's answer into a handoff

**Files:**
- Create: `core/src/agentic/handoffs/finalise.py`
- Modify: `core/src/agentic/handoffs/kinds.py` (TestReportData consistency validator; `Severity` values exported as `SEVERITIES`), `core/src/agentic/handoffs/__init__.py` (exports)
- Test: `core/tests/handoffs/test_finalise.py`

**Interfaces:**
- Consumes: `parse_handoff`, `HandoffError`, `Handoff`, `HandoffEnvelope`, `HANDOFF_KINDS` (existing); `agentic.yamlio.split_front_matter`, `load_yaml`, `line_of`; `agentic.definitions.describe_validation_error`.
- Produces:
  - `EnvelopeStamp(run_id: str, step: str, agent: str, produced_at: datetime)`: a frozen dataclass. `agent` is `name@version`, and `produced_at` must be timezone-aware (otherwise ValueError).
  - `BLOCK_ON_DEFAULT: frozenset[str] = frozenset({"critical", "high"})`
  - `FRAMEWORK_FIELDS: tuple[str, ...] = ("kind", "schema_version", "run_id", "step", "agent", "produced_at")`
  - `finalise_handoff(text: str, *, kind: str, stamp: EnvelopeStamp, block_on: frozenset[str] = BLOCK_ON_DEFAULT) -> tuple[str, Handoff]`: returns the final document text and its parsed `Handoff`, or raises `HandoffError`. A `block_on` with a value outside `SEVERITIES`, or a `kind` not in `HANDOFF_KINDS`, raises ValueError (a caller bug, not agent output).

Rules `finalise_handoff` applies, in order:
1. Normalise CRLF to LF, then strip a BOM and surrounding whitespace.
2. If the whole text is one fenced block (first line starts with ```` ``` ````, last line is ```` ``` ````), drop those two lines. Error line numbers then count from the line after the fence.
3. No front matter gives exactly this error: `the answer must be one handoff document: a line '---', the YAML fields, a line '---', then the Markdown body`.
4. A `kind` present in the agent's fields and different from `kind` gives the error `kind: this step produces '<kind>', not '<given>' (line N)`. Other framework fields the agent wrote are replaced silently.
5. A blank body (whitespace only) gives the error `body: the Markdown document after the front matter is empty`.
6. For `review`, `open_blocking` is set to the number of findings with `status == "open"` and `severity in block_on`, whatever the agent wrote.
7. Validate the kind's fields with its model. Errors use `describe_validation_error(..., lines=<agent's line map>)`, so lines point into the agent's text.
8. Render the final document: `---`, then the envelope fields in `FRAMEWORK_FIELDS` order with `status` after them, then the kind's fields in the agent's order, then `---`, then the body unchanged (LF, ending in one newline). `produced_at` is written as `YYYY-MM-DDTHH:MM:SSZ` in UTC.
9. `parse_handoff(document)` must succeed. It is the final guarantee, and its result is the returned `Handoff`.

- [ ] **Step 1: Write the failing tests** in `core/tests/handoffs/test_finalise.py` (one helper `answer(fields: str, body: str = "## Design\nText.\n") -> str` that wraps fields in `---` lines; `STAMP = EnvelopeStamp("r-sp5-1", "design", "design@1.4.0", datetime(2026, 10, 19, 10, 42, tzinfo=UTC))`):

```python
def test_the_framework_stamps_the_envelope():
    document, handoff = finalise_handoff(answer(DESIGN_FIELDS), kind="design", stamp=STAMP)
    assert handoff.envelope.run_id == "r-sp5-1" and handoff.envelope.agent == "design@1.4.0"
    assert document.startswith("---\nkind: design\nschema_version: 1\nrun_id: r-sp5-1\n")
    assert "produced_at: 2026-10-19T10:42:00Z\nstatus: complete\n" in document

def test_envelope_fields_the_agent_wrote_are_replaced():  # Review Focus 3
    fields = "run_id: someone-else\nagent: admin@9\n" + DESIGN_FIELDS
    _, handoff = finalise_handoff(answer(fields), kind="design", stamp=STAMP)
    assert (handoff.envelope.run_id, handoff.envelope.agent) == ("r-sp5-1", "design@1.4.0")

def test_a_different_kind_is_an_error():  # Review Focus 3
    with pytest.raises(HandoffError, match=r"this step produces 'design', not 'review' \(line 2\)"):
        finalise_handoff(answer("kind: review\n" + DESIGN_FIELDS), kind="design", stamp=STAMP)

def test_one_outer_code_fence_is_stripped():  # Review Focus 1
    fenced = "```markdown\n" + answer(DESIGN_FIELDS) + "```\n"
    _, handoff = finalise_handoff(fenced, kind="design", stamp=STAMP)
    assert handoff.data.decisions[0].id == "D1"

def test_crlf_answers_become_lf_documents():  # Review Focus 4
    crlf = answer(DESIGN_FIELDS).replace("\n", "\r\n")
    document, _ = finalise_handoff(crlf, kind="design", stamp=STAMP)
    lf, _ = finalise_handoff(answer(DESIGN_FIELDS), kind="design", stamp=STAMP)
    assert "\r" not in document and document == lf

def test_prose_without_front_matter_is_an_error():
    with pytest.raises(HandoffError, match="must be one handoff document"):
        finalise_handoff("Here is my design: use a queue.", kind="design", stamp=STAMP)

def test_an_empty_body_is_an_error():
    with pytest.raises(HandoffError, match="body: the Markdown document"):
        finalise_handoff(answer(DESIGN_FIELDS, body="  \n"), kind="design", stamp=STAMP)

def test_field_errors_point_at_the_agents_lines():
    bad = "status: complete\ndecisions:\n  - id: D1\n    choice: queue\n    rationale: ''\n"
    with pytest.raises(HandoffError) as caught:
        finalise_handoff(answer(bad), kind="design", stamp=STAMP)
    assert "rationale" in str(caught.value) and "line 6" in str(caught.value)

def test_open_blocking_is_counted_by_the_framework():  # Review Focus 2
    fields = ("status: complete\nopen_blocking: 0\nfindings:\n"
              "  - {severity: critical, issue: SQL injection, status: open}\n"
              "  - {severity: high, issue: no timeout, status: resolved}\n"
              "  - {severity: medium, issue: weak test, status: open}\n")
    _, default = finalise_handoff(answer(fields), kind="review", stamp=STAMP)
    _, regulated = finalise_handoff(answer(fields), kind="review", stamp=STAMP,
                                    block_on=frozenset({"critical", "high", "medium"}))
    assert (default.data.open_blocking, regulated.data.open_blocking) == (1, 2)

def test_a_passing_test_report_with_failures_is_an_error():
    fields = "status: complete\nverdict: pass\ntests: {passed: 9, failed: 1}\n"
    with pytest.raises(HandoffError, match="verdict"):
        finalise_handoff(answer(fields), kind="test-report", stamp=STAMP)

def test_the_final_document_parses_back_to_the_same_handoff():
    document, handoff = finalise_handoff(answer(DESIGN_FIELDS), kind="design", stamp=STAMP)
    assert parse_handoff(document) == handoff

def test_a_naive_timestamp_or_unknown_severity_is_a_caller_error():
    with pytest.raises(ValueError):
        EnvelopeStamp("r", "s", "a@1", datetime(2026, 10, 19))
    with pytest.raises(ValueError):
        finalise_handoff(answer(DESIGN_FIELDS), kind="design", stamp=STAMP, block_on=frozenset({"urgent"}))
```

`DESIGN_FIELDS` is `"status: complete\ndecisions:\n  - id: D1\n    choice: queue\n    rationale: exports time out\n"`. In `test_field_errors_point_at_the_agents_lines`, line 6 counts the opening `---` as line 1.

- [ ] **Step 2: Run the tests to verify they fail.** Run `uv run pytest core/tests/handoffs/test_finalise.py -q`. Expected: collection error, `cannot import name 'finalise_handoff'`.

- [ ] **Step 3: Add to `kinds.py`.**
  - `SEVERITIES: tuple[str, ...] = typing.get_args(Severity)`.
  - A `model_validator(mode="after")` on `TestReportData`. When `verdict == "pass"` and either `tests.failed > 0` or any `acceptance_results[i].passed` is False, it raises `verdict: 'pass' but <n> test(s) failed` or `verdict: 'pass' but acceptance criterion <id> failed`.

- [ ] **Step 4: Implement `finalise.py`** per the rules above, reusing `split_front_matter`/`load_yaml`/`line_of`/`describe_validation_error`. Render the YAML with ruamel safe dump (block style, `sort_keys` off, `allow_unicode=True`, width large enough not to fold lines).

- [ ] **Step 5: Run the tests to verify they pass, then the existing handoff tests.** Run `uv run pytest core/tests/handoffs -q`. Expected: all pass.

- [ ] **Step 6: Commit.**

```bash
git add core/src/agentic/handoffs core/tests/handoffs/test_finalise.py
git commit -m "feat(core): finalise agent answers into handoffs - framework envelope, open_blocking, line-numbered errors (M1-19)"
```

---

### Task 2 (M1-19, part 2): the five body templates and the output instructions

**Files:**
- Create: `core/src/agentic/handoffs/templates/{requirements,design,dev-report,review,test-report}.md`, `core/src/agentic/handoffs/templates.py`
- Modify: `core/src/agentic/handoffs/__init__.py`, `THIRD_PARTY_NOTICES.md` (spec-kit MIT with its copyright line and licence text; MADR `MIT OR CC0-1.0` used under CC0-1.0 with attribution), `docs/superpowers/plans/TRACKER.md` (M1-19 Done)
- Test: `core/tests/handoffs/test_templates.py`

**Interfaces:**
- Consumes: `finalise_handoff`, `EnvelopeStamp`, `FRAMEWORK_FIELDS` (Task 1); `HANDOFF_KINDS`.
- Produces:
  - `load_template(kind: str) -> str`: the template text, read with `importlib.resources.files("agentic.handoffs") / "templates" / f"{kind}.md"` as UTF-8. An unknown kind raises ValueError naming the known kinds.
  - `output_instructions(kind: str) -> str`: the exact text below with the template inserted.

`output_instructions` text (exact copy; `{kind}` and `{template}` substituted):

```
<output-format kind="{kind}">
Finish by answering with one handoff document and nothing else: a line '---', YAML fields, a line '---', then the Markdown body. The framework fills in kind, schema_version, run_id, step, agent and produced_at. You write status (complete, partial or blocked) and the fields shown below. Replace every example value and every <!-- guidance --> comment with your own content; keep the section headings.

{template}
</output-format>
```

Each template is a complete answer: front matter with **no framework fields**, `status: complete`, and example values that are valid for the kind's model, followed by the body. Body sections, with one `<!-- guidance -->` comment under each heading:

| Kind | Source (adapted) | Body sections (in order) |
|---|---|---|
| requirements | spec-kit spec template | `# Requirements: <feature>` · `## User scenarios` (stories with priority P1/P2/P3, each with EARS acceptance criteria matching `items`) · `## Edge cases` · `## Functional requirements` (one bullet per item id) · `## Success criteria` (measurable) · `## Assumptions` · `## Open questions` (mirrors `open_questions`; mark guesses `[NEEDS CLARIFICATION: ...]`) |
| design | spec-kit plan template + MADR | `# Design: <title>` · `## Summary` · `## Context and problem statement` · `## Decision drivers` · `## Considered options` · `## Decision outcome` (one `### D<n>: <choice>` per decision with its rationale and consequences) · `## Impact` (modules, interfaces, tests; graph-derived lists are candidates) · `## Risks` · `## Confirmation` (how the decision will be verified) |
| dev-report | own | `# Development report: <title>` · `## What changed` · `## Tests run` · `## Deviations from the design` (each with its reason) · `## Notes for review` |
| review | Zyroflo P6 + review rubric (structure only) | `# Review: <title>` · `## Summary` · `## Findings` (one line each: severity — file:line — issue → fix) · `## Category breakdown` (rubric categories and score; reported only) · `## Pre-pass results` · `## Verdict` (open blocking findings; the score never decides) |
| test-report | own | `# Test report: <title>` · `## Verdict` · `## Test results` · `## Acceptance criteria` (each criterion id: pass/fail with evidence) · `## Dependency scan` |

The review template's front matter omits `open_blocking`, because the framework computes it (Task 1).

- [ ] **Step 1: Write the failing tests** in `core/tests/handoffs/test_templates.py`:

```python
@pytest.mark.parametrize("kind", list(HANDOFF_KINDS))
def test_every_template_renders_and_parses_back_valid(kind):
    document, handoff = finalise_handoff(load_template(kind), kind=kind, stamp=STAMP)
    assert parse_handoff(document) == handoff and handoff.envelope.kind == kind

@pytest.mark.parametrize("kind", list(HANDOFF_KINDS))
def test_templates_leave_the_envelope_to_the_framework(kind):
    header, _ = split_front_matter(load_template(kind))
    fields, _ = load_yaml(header)
    assert not set(fields) & set(FRAMEWORK_FIELDS) and fields["status"] == "complete"

@pytest.mark.parametrize("kind", list(HANDOFF_KINDS))
def test_every_section_has_guidance(kind):
    body = split_front_matter(load_template(kind))[1]
    headings = [line for line in body.splitlines() if line.startswith("## ")]
    assert headings and body.count("<!--") >= len(headings)

def test_the_design_template_follows_madr():
    body = load_template("design")
    for heading in ("## Context and problem statement", "## Considered options",
                    "## Decision outcome", "## Confirmation"):
        assert heading in body

def test_output_instructions_wrap_the_template():
    text = output_instructions("review")
    assert text.startswith('<output-format kind="review">\n') and text.endswith("</output-format>")
    assert load_template("review") in text and "produced_at" in text

def test_an_unknown_kind_names_the_known_ones():
    with pytest.raises(ValueError, match="requirements"):
        load_template("poem")
```

- [ ] **Step 2: Run the tests to verify they fail.** Run `uv run pytest core/tests/handoffs/test_templates.py -q`. Expected: collection error, `No module named 'agentic.handoffs.templates'`.

- [ ] **Step 3: Write the five templates and `templates.py`** per the table and the interface above.

- [ ] **Step 4: Run the tests to verify they pass.** Run `uv run pytest core/tests/handoffs -q`. Expected: all pass.

- [ ] **Step 5: Verify the templates ship in the wheel.** Run `uv build --package agentic-core --wheel -o <scratchpad>/dist`, then list the wheel with `python -m zipfile -l`. Expected: the five `agentic/handoffs/templates/*.md` files are present. If they are missing, add them to the build-backend's data/include settings in `core/pyproject.toml` and repeat.

- [ ] **Step 6: Update `THIRD_PARTY_NOTICES.md` and the tracker (M1-19 Done, with the commit SHA and check count), run `uv run python scripts/check.py --all`, then commit.**

```bash
git add core/src/agentic/handoffs core/tests/handoffs/test_templates.py THIRD_PARTY_NOTICES.md docs/superpowers/plans/TRACKER.md
git commit -m "feat(core): handoff body templates for the five kinds (spec-kit, MADR, Zyroflo P6) (M1-19)"
```

---

### Task 3 (M1-20): the validation retry, through the runner

**Files:**
- Create: `core/src/agentic/handoffs/contract.py`
- Modify: `core/src/agentic/agents/runner.py`, `core/src/agentic/handoffs/__init__.py`, `docs/architecture/call-flow.md` (line "invalid handoff → retry once" ✅ and the step-table row), `docs/superpowers/plans/TRACKER.md` (M1-20 Done)
- Test: `core/tests/agents/test_contract.py`

**Interfaces:**
- Consumes: `finalise_handoff`, `EnvelopeStamp`, `BLOCK_ON_DEFAULT` (Task 1); `output_instructions` (Task 2); `PydanticAIRunner`, `AgentOutcome`, `AgentTask` (SP4).
- Produces:
  - In `runner.py`:
    - `class OutputContract(Protocol)`, with `instructions: str` (a property or attribute) and `def check(self, text: str) -> tuple[str | None, list[str]]` (the final document and no errors, or None and the errors).
    - `OUTPUT_RETRIES = 1`.
    - `retry_message(errors: list[str]) -> str`, with the exact text below.
    - A new keyword `PydanticAIRunner(..., contract: OutputContract | None = None)`.
  - `AgentOutcome` gains `status` value `"invalid"` and `errors: list[str] = field(default_factory=list)`. On `invalid`, `output` is the agent's last answer, unchanged; on `completed` with a contract, `output` is the final document.
  - In `contract.py`: `HandoffContract(kind: str, run_id: str, step: str, agent: str, block_on: frozenset[str] = BLOCK_ON_DEFAULT, clock: Callable[[], datetime] = <UTC now>)`, a frozen dataclass. Its `instructions` property returns `output_instructions(kind)`. `check(text)` builds an `EnvelopeStamp` with `produced_at=clock()`, calls `finalise_handoff`, and maps `HandoffError.errors` to the second element.

`retry_message` text (exact copy; errors as `- ` bullets):

```
Your handoff document is not valid:
{bullets}
Answer again with the whole document, corrected: a line '---', the YAML fields, a line '---', then the Markdown body.
```

Runner behaviour:
- `_instructions` appends `contract.instructions` after the input handoffs, separated by a blank line.
- The Pydantic AI agent is built with `retries={"tools": 1, "output": OUTPUT_RETRIES}`.
- An output validator runs only when `contract` is set and the output is a `str` (a `DeferredToolRequests` pause is never validated). On errors it records `(text, errors)` for this drive and raises `ModelRetry(retry_message(errors))`; otherwise it returns the final document.
- When Pydantic AI raises `UnexpectedModelBehavior` and this drive recorded a failed check, return `AgentOutcome(status="invalid", output=<last text>, errors=<last errors>, stopped=None, usage=..., costs=...)`. Any other `UnexpectedModelBehavior` is re-raised.
- `resume` validates the same way, because the saved instructions already include the output format.
- Child agents get no contract: their output goes back to the parent, not into the pipeline.
- The engine (SP6, M1-22) turns `status="invalid"` into a human gate, where the person can edit the kept text. SP5 stops at the outcome.

- [ ] **Step 1: Write the failing tests** in `core/tests/agents/test_contract.py`, using `FunctionModel` and a `HandoffContract("design", "r-sp5-1", "design", "design@1.4.0", clock=lambda: FIXED)`. `VALID` is a valid design answer and `PROSE = "I think we should add a queue."`:

```python
def test_a_valid_answer_completes_with_the_final_document(resolved, tmp_path):
    outcome = run(runner(resolved, answers(VALID), tmp_path, contract=CONTRACT))
    assert outcome.status == "completed" and outcome.output.startswith("---\nkind: design\n")

def test_an_invalid_answer_is_retried_once_with_its_errors(resolved, tmp_path):
    seen = []
    outcome = run(runner(resolved, answers(PROSE, VALID, seen=seen), tmp_path, contract=CONTRACT))
    assert outcome.status == "completed" and len(seen) == 2
    retry = seen[1][-1].parts[-1]  # the RetryPromptPart the model received
    assert "must be one handoff document" in retry.model_response()

def test_two_invalid_answers_end_invalid_and_keep_the_text(resolved, tmp_path):  # Review Focus 5
    seen = []
    outcome = run(runner(resolved, answers(PROSE, PROSE, PROSE, seen=seen), tmp_path, contract=CONTRACT))
    assert (outcome.status, outcome.output) == ("invalid", PROSE)
    assert outcome.errors and len(seen) == 2  # never a third try

def test_the_model_is_told_the_output_format(resolved, tmp_path):
    seen = []
    run(runner(resolved, answers(VALID, seen=seen), tmp_path, contract=CONTRACT))
    assert '<output-format kind="design">' in seen[0][0].instructions

def test_a_pause_for_approval_is_not_validated(resolved, tmp_path):
    calls = FunctionModel(lambda m, i: ModelResponse(parts=[ToolCallPart("write_file", {"path": "out.txt"})]))
    r = runner(resolved, calls, tmp_path, contract=CONTRACT, tools=[write_tool(tmp_path)])
    outcome = run(r)
    assert outcome.status == "paused" and outcome.state_digest

def test_without_a_contract_any_text_completes(resolved, tmp_path):
    assert run(runner(resolved, answers(PROSE), tmp_path)).output == PROSE
```

`answers(*texts, seen=None)` is a `FunctionModel` that returns the next text in turn and appends each message list to `seen`. `write_tool` is imported from `_resume_helper`; it needs a human's approval, so the run pauses.

- [ ] **Step 2: Run the tests to verify they fail.** Run `uv run pytest core/tests/agents/test_contract.py -q`. Expected: collection error, `cannot import name 'HandoffContract'`.

- [ ] **Step 3: Implement `contract.py` and the runner changes** per the interfaces above.

- [ ] **Step 4: Run the tests to verify they pass, then all agent and handoff tests.** Run `uv run pytest core/tests/agents core/tests/handoffs -q`. Expected: all pass.

- [ ] **Step 5: Update the call-flow doc and the tracker (M1-20 Done), run `uv run python scripts/check.py --all` (expected: green on Windows and Linux), then commit.**

```bash
git add core/src/agentic core/tests/agents/test_contract.py docs/architecture/call-flow.md docs/superpowers/plans/TRACKER.md
git commit -m "feat(core): handoff validation retry - errors fed back once, then invalid for a human gate (M1-20)"
```
