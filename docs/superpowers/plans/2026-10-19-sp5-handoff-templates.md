# SP5 Handoff Templates, Extensions and Validation Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every handoff kind inherits one generic base template. Org, team and repo layers can add and tighten fields and sections but never remove them (D37). Every agent is told exactly what document to write and has its answer checked: the framework fills in the fields it owns, a broken document goes back to the agent once with the errors, and a second failure ends the run as `invalid` for the engine to put in front of a human.

**Architecture:** `agentic.handoffs` grows four modules:
- `schema.py` defines the base (`HandoffBase` model with its sections) and the five built-in kinds, which inherit it. Their sections and example values are read from `templates/*.md`. It also holds `HandoffSchema` (one kind's merged fields and sections) and `HandoffCatalog` (all kinds visible to a run), and renders a schema into the template an agent sees.
- `extensions.py` loads `<layer>/handoffs/<kind>.yaml` files and merges them onto the catalog under the add-or-tighten rules. It compiles merged fields into Pydantic models with `pydantic.create_model`, so no new dependency is needed.
- `finalise.py` turns an agent's answer into the final document against a schema.
- `contract.py` packages a schema for the runner.

`parse_handoff` validates against a catalog, so a human's edit at a gate is checked by the same merged schema. Discovery picks up the extension files layer by layer and refuses agents that name unknown kinds. The runner accepts any `OutputContract`: it appends the contract's instructions and registers a Pydantic AI **output validator**, which raises `ModelRetry` under `retries={"output": 1}`. Pydantic AI feeds the errors back; there's no retry loop of our own. When the budget runs out, the runner returns `status="invalid"`.

The templates live in `core/src/agentic/handoffs/templates/`, not the master plan's `library/handoff-templates/`, so an installed wheel finds them; they are part of the contract the framework validates against.

**Tech Stack:** Python 3.12; Pydantic v2 (`create_model`, validators); pydantic-ai-slim 2.54.0 (output validators, `ModelRetry`, `retries={"tools": n, "output": n}`); ruamel.yaml via `agentic.yamlio`. Template sources:
- github/spec-kit `templates/spec-template.md` and `templates/plan-template.md` (MIT, Copyright GitHub, Inc.).
- adr/madr `template/adr-template.md` (MIT OR CC0-1.0; this confirms the licence the spec left open).
- Zyroflo `sdlc/stages/P6-review.md` and `sdlc_templates/review-rubric.md`, structure only. The rubric reaches the review agent in M1-60 and is never copied here (D34).

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md`:
- §7.1 format: front matter plus body; envelope fields are framework-owned.
- §7.2 kinds, fields and template sources; invalid output gets one retry with the errors, then a human gate.
- §7.4 / D37 template inheritance and extensions.
- §8.7 failure handling.
- §6 review loop, `until: review.open_blocking == 0`.

Master plan rows: M1-19, M1-20, and the new row M1-79 (D37 extensions).

## Global Constraints

- Envelope fields are **framework-owned**: `kind`, `schema_version`, `run_id`, `step`, `agent`, `produced_at`. The agent writes `status` and the kind's fields (§7.1).
- **Base** (§7.4): field `summary` (string, required, one line) and `open_questions` (list of strings, default empty); sections `## Summary` (first) and `## Open questions` (last). Every kind inherits them.
- **Extensions add or tighten only** (D37):
  - Allowed: add a field (optional or required) or a section; make an optional field required; narrow an `enum` or built-in choice field to fewer values.
  - Refused at load, naming file and line: removing, renaming, loosening or retyping; a section heading already defined; a new kind at repo level.
- Extension field types: `string`, `integer`, `number`, `boolean`, `list` (of strings), `enum`. Each added field has a `description` and an `example`. Top-level fields only.
- Merged section order: base `## Summary`, the kind's sections, then org, team and repo sections in that order, then base `## Open questions`. Every section is required: its `## <heading>` line must appear in the body.
- **The review exit is categorical** (Zyroflo P6): `open_blocking` counts open findings at a `block_on` severity, and the framework computes it. The default `block_on` is `critical, high`; regulated adds `medium` (the engine passes the set).
- One output retry per agent run (`OUTPUT_RETRIES = 1`), then `status="invalid"`; the human gate itself is M1-22.
- Documents the framework writes use LF and UTF-8.
- No Zyroflo code or text is copied into this repo (D34). Adapted third-party templates get an entry in `THIRD_PARTY_NOTICES.md`.
- Core does not import `sdlc_commons` or Zyroflo.
- Tests make no network or model calls.
- `uv run python scripts/check.py --all` (Windows + Linux) is the gate.
- Never add `# fmt: skip`.

## Review Focus

1. **The model wraps its answer in a ```` ```markdown ```` code fence.** It is accepted, and one outer fence is stripped. Test in Task 2.
2. **A review claims `open_blocking: 0` while listing an open critical finding.** The framework recomputes the count. Test in Task 2.
3. **The agent writes envelope fields itself** (another run id, another agent, the wrong kind). The framework's values win, and a different kind is an error. Test in Task 2.
4. **An answer with CRLF line endings.** The final document is LF and identical to the LF answer's. Test in Task 2.
5. **A model that answers with plain prose twice.** The outcome is `status="invalid"`, keeping the errors and the last text. Nothing raises, and there is no third try. Test in Task 5.

---

### Task 1 (M1-19): the base, the built-in kinds and their templates

**Files:**
- Create:
  - `core/src/agentic/handoffs/schema.py`
  - `core/src/agentic/handoffs/templates/{base,requirements,design,dev-report,review,test-report}.md`
- Modify:
  - `core/src/agentic/handoffs/kinds.py`: kind models inherit `HandoffBase`; `RequirementsData.open_questions` moves to the base; add a `TestReportData` consistency validator; export `SEVERITIES = typing.get_args(Severity)`.
  - `core/src/agentic/handoffs/__init__.py`
  - `core/tests/fixtures/handoffs/design.md`: add `summary` and the design sections.
  - `THIRD_PARTY_NOTICES.md`: spec-kit (MIT, licence text) and MADR (MIT OR CC0-1.0, used under CC0-1.0 with attribution).
- Test: `core/tests/handoffs/test_schema.py`

**Interfaces:**
- Produces:
  - `HandoffBase(BaseModel)` with `extra="forbid"`, `summary: str` (non-blank) and `open_questions: list[str] = []`.
  - `Section(heading: str, guidance: str, layer: Layer)` (frozen).
  - `FieldSpec(name, type: Literal["string","integer","number","boolean","list","enum"] | None, required: bool, values: tuple[str, ...] | None, description: str, example: object, layer: Layer)`: `type` is None for built-in fields typed by their model.
  - `Layer = Literal["base", "kind", "org", "team", "repo"]`.
  - `HandoffSchema(kind: str, title: str, model: type[BaseModel], sections: tuple[Section, ...], examples: dict[str, object], schema_version: int)` (frozen), with property `digest: str` (`"sha256:"` plus sha256 of canonical JSON of kind, model JSON schema, section headings and guidance).
  - `HandoffCatalog(schemas: Mapping[str, HandoffSchema])` with `schema(kind) -> HandoffSchema` (ValueError naming the known kinds), `kinds() -> list[str]` and `digest: str` (over all schema digests, sorted by kind).
  - `BUILTIN_CATALOG: HandoffCatalog`.
  - `render_template(schema: HandoffSchema) -> str`: front matter `status: complete`, then `examples` in field order; a blank line; `# <title>: <short title>`; then each section as `## <heading>` followed by `<!-- <guidance> -->` and a blank line.

How the built-ins are read: `templates/base.md` and each `templates/<kind>.md` are Markdown files.
- Front matter holds the **example values** of the fields that file introduces. `base.md` holds `summary` and `open_questions`; the kind files do not repeat them.
- The body holds one `# <title>` line, then the sections that file introduces, each `## heading` followed by one `<!-- guidance -->` comment.
- `base.md`'s body has `## Summary` and `## Open questions`, which are placed first and last.

Kind sections (adapted, in order):

| Kind | Title | Source | Sections |
|---|---|---|---|
| requirements | Requirements | spec-kit spec | User scenarios (P1/P2/P3 stories, EARS acceptance criteria matching `items`) · Edge cases · Functional requirements · Success criteria · Assumptions |
| design | Design | spec-kit plan + MADR | Context and problem statement · Decision drivers · Considered options · Decision outcome (`### D<n>: <choice>` with rationale and consequences) · Impact (graph-derived lists are candidates) · Risks · Confirmation |
| dev-report | Development report | own | What changed · Tests run · Deviations from the design · Notes for review |
| review | Review | Zyroflo P6 + rubric (structure only) | Findings (severity — file:line — issue → fix) · Category breakdown (reported only) · Pre-pass results · Verdict (open blocking findings decide; the score never does) |
| test-report | Test report | own | Verdict · Test results · Acceptance criteria (id: pass/fail with evidence) · Dependency scan |

The review examples omit `open_blocking` (the framework computes it, Task 2).

- [ ] **Step 1: Write the failing tests** in `core/tests/handoffs/test_schema.py`:

```python
@pytest.mark.parametrize("kind", list(HANDOFF_KINDS))
def test_every_kind_inherits_the_base(kind):
    schema = BUILTIN_CATALOG.schema(kind)
    assert issubclass(schema.model, HandoffBase)
    assert schema.sections[0].heading == "Summary" and schema.sections[-1].heading == "Open questions"

@pytest.mark.parametrize("kind", list(HANDOFF_KINDS))
def test_every_builtin_template_is_valid_for_its_model(kind):
    schema = BUILTIN_CATALOG.schema(kind)
    header, body = split_front_matter(render_template(schema))
    fields, _ = load_yaml(header)
    schema.model.model_validate({k: v for k, v in fields.items() if k != "status"} | extra_for(kind))
    assert [f"## {s.heading}" for s in schema.sections] == [l for l in body.splitlines() if l.startswith("## ")]

def test_kind_files_do_not_repeat_the_base():
    for kind in HANDOFF_KINDS:
        assert "## Summary" not in load_kind_file(kind) and "summary:" not in load_kind_file(kind)

def test_the_design_sections_follow_madr():
    headings = [s.heading for s in BUILTIN_CATALOG.schema("design").sections]
    assert headings[1:5] == ["Context and problem statement", "Decision drivers",
                             "Considered options", "Decision outcome"]

def test_a_passing_test_report_with_failures_is_invalid():
    with pytest.raises(ValidationError, match="verdict"):
        TestReportData.model_validate({"summary": "s", "verdict": "pass", "tests": {"passed": 1, "failed": 1}})

def test_the_catalog_digest_changes_with_any_schema():
    assert BUILTIN_CATALOG.digest.startswith("sha256:")
    smaller = HandoffCatalog({k: s for k, s in BUILTIN_CATALOG.schemas.items() if k != "review"})
    assert smaller.digest != BUILTIN_CATALOG.digest

def test_an_unknown_kind_names_the_known_ones():
    with pytest.raises(ValueError, match="requirements"):
        BUILTIN_CATALOG.schema("poem")
```

`extra_for("review")` returns `{"open_blocking": 0}` and `{}` for the other kinds. `load_kind_file(kind)` reads the raw template file through `importlib.resources`.

- [ ] **Step 2: Run the tests to verify they fail.** Run `uv run pytest core/tests/handoffs/test_schema.py -q`. Expected: collection error, `No module named 'agentic.handoffs.schema'`.
- [ ] **Step 3: Implement** `schema.py`, the six template files and the `kinds.py` changes. The `TestReportData` validator raises `verdict: 'pass' but <n> test(s) failed` or `verdict: 'pass' but acceptance criterion <id> failed`. Update the `design.md` fixture.
- [ ] **Step 4: Run** `uv run pytest core/tests/handoffs -q`. Expected: all pass.
- [ ] **Step 5: Verify the templates ship in the wheel.** Run `uv build --package agentic-core --wheel -o <scratchpad>/dist`, then `python -m zipfile -l <wheel>`. Expected: the six `agentic/handoffs/templates/*.md` files are present. If they are missing, include them in `[tool.uv.build-backend]` and repeat.
- [ ] **Step 6: Commit:** `feat(core): handoff base template and built-in kinds inheriting it (M1-19, D37)`.

---

### Task 2 (M1-19): parsing against a catalog, and finalising an agent's answer

**Files:**
- Create: `core/src/agentic/handoffs/finalise.py`
- Modify: `core/src/agentic/handoffs/parse.py`, `core/src/agentic/handoffs/__init__.py`
- Test: `core/tests/handoffs/test_finalise.py`; existing `test_handoffs.py` (call sites unchanged by the new default)

**Interfaces:**
- Consumes: `HandoffCatalog`, `HandoffSchema`, `BUILTIN_CATALOG` (Task 1).
- Produces:
  - `parse_handoff(text: str, catalog: HandoffCatalog = BUILTIN_CATALOG) -> Handoff`: as today, validating fields against `catalog.schema(kind).model`. It adds one error per missing section: `body: section '## <heading>' is missing`.
  - `EnvelopeStamp(run_id: str, step: str, agent: str, produced_at: datetime)`, frozen. `agent` is `name@version`; a naive `produced_at` raises ValueError.
  - `BLOCK_ON_DEFAULT = frozenset({"critical", "high"})`.
  - `FRAMEWORK_FIELDS = ("kind", "schema_version", "run_id", "step", "agent", "produced_at")`.
  - `finalise_handoff(text: str, *, schema: HandoffSchema, stamp: EnvelopeStamp, block_on: frozenset[str] = BLOCK_ON_DEFAULT) -> tuple[str, Handoff]`: returns the final document and its `Handoff`, or raises `HandoffError`. A `block_on` value outside `SEVERITIES` raises ValueError.

`finalise_handoff` rules, in order:
1. CRLF becomes LF; strip the BOM and surrounding whitespace.
2. If the whole text is one fenced block (first line starts with ```` ``` ````, last line is ```` ``` ````), drop those two lines. Line numbers then count from the line after the fence.
3. With no front matter, the error is exactly: `the answer must be one handoff document: a line '---', the YAML fields, a line '---', then the Markdown body`.
4. A `kind` that differs from `schema.kind` is the error `kind: this step produces '<kind>', not '<given>' (line N)`. Other framework fields the agent wrote are replaced silently.
5. A blank body is the error `body: the Markdown document after the front matter is empty`. Missing sections are errors as in `parse_handoff`.
6. For `review`, set `open_blocking` to the number of findings with `status == "open"` and `severity in block_on`.
7. Validate the fields with `schema.model`. Errors use `describe_validation_error(..., lines=<the agent's line map>)`.
8. Render: `---`; the envelope in `FRAMEWORK_FIELDS` order, then `status`; the kind's fields in the agent's order; `---`; the body unchanged, ending in one LF. `produced_at` is written as `YYYY-MM-DDTHH:MM:SSZ` (UTC), and `schema_version` comes from the schema.
9. `parse_handoff(document, <a catalog holding schema>)` must succeed; its result is returned.

- [ ] **Step 1: Write the failing tests** in `core/tests/handoffs/test_finalise.py`.
  - Helpers:
    - `answer(fields, body=DESIGN_BODY)` wraps the fields in `---` lines.
    - `DESIGN = BUILTIN_CATALOG.schema("design")`.
    - `DESIGN_FIELDS = "status: complete\nsummary: queue exports\ndecisions:\n  - id: D1\n    choice: queue\n    rationale: exports time out\n"`.
    - `DESIGN_BODY` is the rendered design body (`split_front_matter(render_template(DESIGN))[1]`).
    - `STAMP = EnvelopeStamp("r-sp5-1", "design", "design@1.4.0", datetime(2026, 10, 19, 10, 42, tzinfo=UTC))`.

```python
def test_the_framework_stamps_the_envelope():
    document, handoff = finalise_handoff(answer(DESIGN_FIELDS), schema=DESIGN, stamp=STAMP)
    assert document.startswith("---\nkind: design\nschema_version: 1\nrun_id: r-sp5-1\n")
    assert "produced_at: 2026-10-19T10:42:00Z\nstatus: complete\n" in document

def test_envelope_fields_the_agent_wrote_are_replaced():  # Review Focus 3
    _, h = finalise_handoff(answer("run_id: x\nagent: admin@9\n" + DESIGN_FIELDS), schema=DESIGN, stamp=STAMP)
    assert (h.envelope.run_id, h.envelope.agent) == ("r-sp5-1", "design@1.4.0")

def test_a_different_kind_is_an_error():  # Review Focus 3
    with pytest.raises(HandoffError, match=r"this step produces 'design', not 'review' \(line 2\)"):
        finalise_handoff(answer("kind: review\n" + DESIGN_FIELDS), schema=DESIGN, stamp=STAMP)

def test_one_outer_code_fence_is_stripped():  # Review Focus 1
    _, h = finalise_handoff("```markdown\n" + answer(DESIGN_FIELDS) + "```\n", schema=DESIGN, stamp=STAMP)
    assert h.data.decisions[0].id == "D1"

def test_crlf_answers_become_the_same_lf_document():  # Review Focus 4
    crlf, _ = finalise_handoff(answer(DESIGN_FIELDS).replace("\n", "\r\n"), schema=DESIGN, stamp=STAMP)
    lf, _ = finalise_handoff(answer(DESIGN_FIELDS), schema=DESIGN, stamp=STAMP)
    assert crlf == lf and "\r" not in crlf

def test_prose_and_empty_bodies_are_errors():
    with pytest.raises(HandoffError, match="must be one handoff document"):
        finalise_handoff("Use a queue.", schema=DESIGN, stamp=STAMP)
    with pytest.raises(HandoffError, match="body: the Markdown document"):
        finalise_handoff(answer(DESIGN_FIELDS, body=" \n"), schema=DESIGN, stamp=STAMP)

def test_a_missing_section_is_an_error_on_parse_too():
    body = DESIGN_BODY.replace("## Risks\n", "")
    with pytest.raises(HandoffError, match="section '## Risks' is missing"):
        finalise_handoff(answer(DESIGN_FIELDS, body=body), schema=DESIGN, stamp=STAMP)

def test_field_errors_point_at_the_agents_lines():
    bad = "status: complete\nsummary: s\ndecisions:\n  - id: D1\n    choice: q\n    rationale: ''\n"
    with pytest.raises(HandoffError, match=r"rationale.*line 7"):
        finalise_handoff(answer(bad), schema=DESIGN, stamp=STAMP)

def test_open_blocking_is_counted_by_the_framework():  # Review Focus 2
    fields = ("status: complete\nsummary: s\nopen_blocking: 0\nfindings:\n"
              "  - {severity: critical, issue: injection, status: open}\n"
              "  - {severity: high, issue: timeout, status: resolved}\n"
              "  - {severity: medium, issue: weak test, status: open}\n")
    review, body = BUILTIN_CATALOG.schema("review"), split_front_matter(render_template(BUILTIN_CATALOG.schema("review")))[1]
    _, d = finalise_handoff(answer(fields, body), schema=review, stamp=STAMP)
    _, r = finalise_handoff(answer(fields, body), schema=review, stamp=STAMP,
                            block_on=frozenset({"critical", "high", "medium"}))
    assert (d.data.open_blocking, r.data.open_blocking) == (1, 2)

def test_the_final_document_parses_back_to_the_same_handoff():
    document, handoff = finalise_handoff(answer(DESIGN_FIELDS), schema=DESIGN, stamp=STAMP)
    assert parse_handoff(document) == handoff

def test_caller_errors_are_value_errors():
    with pytest.raises(ValueError):
        EnvelopeStamp("r", "s", "a@1", datetime(2026, 10, 19))
    with pytest.raises(ValueError):
        finalise_handoff(answer(DESIGN_FIELDS), schema=DESIGN, stamp=STAMP, block_on=frozenset({"urgent"}))
```

- [ ] **Step 2: Run the tests to verify they fail.** Run `uv run pytest core/tests/handoffs/test_finalise.py -q`. Expected: collection error, `cannot import name 'finalise_handoff'`.
- [ ] **Step 3: Implement** `finalise.py` per the rules, and the catalog parameter plus section check in `parse.py`. Reuse `split_front_matter`, `load_yaml`, `line_of` and `describe_validation_error`. Render with ruamel safe dump: block style, no sorting, `allow_unicode`, wide lines.
- [ ] **Step 4: Run** `uv run pytest core/tests/handoffs -q`. Expected: all pass.
- [ ] **Step 5: Mark M1-19 Done in the tracker. Run `uv run python scripts/check.py --all` (expected: green), then commit:** `feat(core): finalise agent answers into handoffs - framework envelope, open_blocking, line-numbered errors (M1-19)`.

---

### Task 3 (M1-79, D37): extensions - add and tighten, never remove

**Files:**
- Create: `core/src/agentic/handoffs/extensions.py`
- Modify: `core/src/agentic/handoffs/__init__.py`
- Test: `core/tests/handoffs/test_extensions.py`

**Interfaces:**
- Consumes: `HandoffCatalog`, `HandoffSchema`, `FieldSpec`, `Section`, `Layer`, `BUILTIN_CATALOG`, `HandoffBase` (Task 1).
- Produces:
  - `HandoffExtension(kind: str, layer: Layer, path: Path, new_kind: bool, title: str | None, fields: tuple[FieldSpec, ...], sections: tuple[Section, ...])`.
  - `load_extension(path: Path, layer: Literal["org","team","repo"]) -> HandoffExtension`: the kind is the file stem. It raises `DefinitionError(path, errors)` with line numbers for a bad shape.
  - `extend_catalog(catalog: HandoffCatalog, extensions: Sequence[HandoffExtension]) -> HandoffCatalog`: extensions are applied in the order given (org, then team, then repo). It raises `DefinitionError` naming the extension's file and line for any refused change.

Extension file format (exact keys; unknown keys refused):

```yaml
# <layer>/handoffs/<kind>.yaml
new_kind: false            # true defines a new kind inheriting the base (org/team only)
title: Threat model        # required when new_kind is true; refused otherwise
fields:
  jira_ticket:             # a new field
    type: string           # string | integer | number | boolean | list | enum
    required: true         # default false
    description: The Jira issue this change implements
    example: PAY-123
  risk_level:
    type: enum
    values: [low, medium, high]
    description: Risk of the change
    example: low
  score:                   # an existing field: only `required: true` and/or a narrower `values:`
    required: true
sections:
  - heading: Rollback plan
    guidance: How to undo this change if it fails in production.
```

Merge rules (each refusal is one `DefinitionError` line, `<file>: <field or heading>: <reason> (line N)`):
- Names: fields match `^[a-z][a-z0-9_]{0,39}$`; kind names `^[a-z][a-z0-9-]{0,39}$`; headings are 1–80 characters with no `#`. A field named like an envelope field or `status` is refused.
- A new field needs `type`, `description` and `example`. The `example` must validate against its type, and `enum` needs non-empty `values` containing the example.
- An existing field may set `required: true` (from optional) and/or `values` that are a subset of its current choices (an `enum` or a built-in `Literal`). Refused: `required: false` on a required field (`cannot loosen`), values not a subset (`cannot add choices`), and a `type` that differs (`cannot change type`).
- A heading already present in the schema is refused (`section already defined by <layer>`).
- `new_kind: true`: refused at repo level (`new kinds are defined in the policy repo`); refused if the kind already exists. The new schema starts from `HandoffBase` with base sections only and `schema_version` 1.
- Extending a kind that does not exist (without `new_kind`) is refused (`unknown kind`).
- The merged model is built with `pydantic.create_model(f"{kind}_{layer}", __base__=<previous model>, **new_or_tightened_fields)`, with `extra="forbid"` kept. Optional new fields default to `None`, or `[]` for `list`.
- Sections are inserted before base `Open questions`, in extension order. `examples` gain each new field's example.

- [ ] **Step 1: Write the failing tests** in `core/tests/handoffs/test_extensions.py`. A helper `ext(tmp_path, kind, text, layer="team")` writes and loads a file.

```python
def test_a_team_adds_a_required_field_and_a_section(tmp_path):
    catalog = extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "review", JIRA_AND_ROLLBACK)])
    review = catalog.schema("review")
    assert review.model.model_fields["jira_ticket"].is_required()
    assert [s.heading for s in review.sections][-2:] == ["Rollback plan", "Open questions"]
    assert "jira_ticket: PAY-123" in render_template(review)

def test_the_extended_template_still_renders_valid(tmp_path):
    catalog = extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "review", JIRA_AND_ROLLBACK)])
    document, _ = finalise_handoff(render_template(catalog.schema("review")), schema=catalog.schema("review"), stamp=STAMP)
    assert parse_handoff(document, catalog).data.jira_ticket == "PAY-123"

def test_an_optional_field_can_be_made_required(tmp_path):
    catalog = extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "review", "fields:\n  score:\n    required: true\n")])
    assert catalog.schema("review").model.model_fields["score"].is_required()

def test_choices_can_be_narrowed_not_widened(tmp_path):
    narrowed = extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "requirements", "fields:\n  size:\n    values: [small, medium]\n")])
    with pytest.raises(ValidationError):
        narrowed.schema("requirements").model.model_validate({"summary": "s", "size": "large", "items": ITEMS})
    with pytest.raises(DefinitionError, match="cannot add choices"):
        extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "requirements", "fields:\n  size:\n    values: [small, huge]\n")])

@pytest.mark.parametrize(("text", "reason"), [
    ("fields:\n  summary:\n    required: false\n", "cannot loosen"),
    ("fields:\n  score:\n    type: string\n", "cannot change type"),
    ("fields:\n  run_id:\n    type: string\n    description: d\n    example: x\n", "framework"),
    ("sections:\n  - heading: Findings\n    guidance: again\n", "section already defined by kind"),
    ("fields:\n  ticket:\n    type: string\n    description: d\n", "example"),
    ("fields:\n  risk:\n    type: enum\n    values: [low]\n    description: d\n    example: high\n", "example"),
    ("remove: [score]\n", "unexpected"),
])
def test_refused_changes_name_the_file_and_line(tmp_path, text, reason):
    with pytest.raises(DefinitionError, match=reason) as caught:
        extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "review", text)])
    assert "review.yaml" in str(caught.value) and "line" in str(caught.value)

def test_a_lower_layer_cannot_loosen_what_a_higher_layer_tightened(tmp_path):
    org = ext(tmp_path / "org", "review", "fields:\n  score:\n    required: true\n", layer="org")
    repo = ext(tmp_path / "repo", "review", "fields:\n  score:\n    required: false\n", layer="repo")
    with pytest.raises(DefinitionError, match="cannot loosen"):
        extend_catalog(BUILTIN_CATALOG, [org, repo])

def test_new_kinds_inherit_the_base_at_org_and_team_only(tmp_path):
    text = "new_kind: true\ntitle: Threat model\nsections:\n  - heading: Threats\n    guidance: g\n"
    catalog = extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "threat-model", text, layer="team")])
    schema = catalog.schema("threat-model")
    assert issubclass(schema.model, HandoffBase)
    assert [s.heading for s in schema.sections] == ["Summary", "Threats", "Open questions"]
    with pytest.raises(DefinitionError, match="policy repo"):
        extend_catalog(BUILTIN_CATALOG, [ext(tmp_path / "r", "threat-model", text, layer="repo")])

def test_extending_an_unknown_kind_is_refused(tmp_path):
    with pytest.raises(DefinitionError, match="unknown kind"):
        extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "poem", "sections:\n  - heading: Verse\n    guidance: g\n")])

def test_the_digest_changes_when_a_layer_adds_something(tmp_path):
    catalog = extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "review", JIRA_AND_ROLLBACK)])
    assert catalog.digest != BUILTIN_CATALOG.digest
    assert catalog.schema("design").digest == BUILTIN_CATALOG.schema("design").digest
```

`JIRA_AND_ROLLBACK` is the `jira_ticket` field and `Rollback plan` section from the format above. `ITEMS` is one valid requirement item.

- [ ] **Step 2: Run the tests to verify they fail.** Run `uv run pytest core/tests/handoffs/test_extensions.py -q`. Expected: collection error, `No module named 'agentic.handoffs.extensions'`.
- [ ] **Step 3: Implement `extensions.py`** per the format and merge rules, reading files with `agentic.yamlio` (duplicate keys refused, line map kept for errors).
- [ ] **Step 4: Run** `uv run pytest core/tests/handoffs -q`. Expected: all pass.
- [ ] **Step 5: Commit:** `feat(core): handoff extensions - layers add and tighten fields and sections, never remove (M1-79, D37)`.

---

### Task 4 (M1-79, D37): extensions discovered per layer; unknown kinds refused

**Files:**
- Modify:
  - `core/src/agentic/definitions/discovery.py`
  - `core/src/agentic/policy/resolve.py`: a new function only; `resolve_agent` is unchanged.
  - `docs/superpowers/plans/TRACKER.md`: add row M1-79 after M1-20 and mark it Done.
- Test: `core/tests/definitions/test_discovery.py`, `core/tests/policy/test_resolve.py`

**Interfaces:**
- Consumes: `load_extension`, `extend_catalog` (Task 3); `BUILTIN_CATALOG`.
- Produces:
  - `DefinitionSet.handoffs: HandoffCatalog`: built during `discover` from `<layer>/handoffs/*.yaml`, org then team then repo. The folder accepts only `*.yaml` files (others refused, as for agents).
  - `discover` raises `DefinitionError(<agent file>, ["produces: unknown handoff kind '<k>' (known: ...)"])` (and the same for `consumes`) when an agent definition names a kind missing from the merged catalog.
  - `kind_violations(agent: ResolvedAgent, catalog: HandoffCatalog) -> list[Violation]` in `agentic.policy.resolve`: one `Violation("produces" | "consumes", kind, catalog.kinds(), "unknown handoff kind")` per unknown kind. This covers kinds inherited from an agent type; preflight (M1-25) calls it.

- [ ] **Step 1: Write the failing tests:**

```python
def test_handoff_extensions_stack_across_layers(tmp_path):  # test_discovery.py
    org, team, repo = (make_layer(tmp_path / n) for n in ("org", "team", "repo"))
    write(org / "handoffs" / "review.yaml", JIRA_FIELD)
    write(repo / "handoffs" / "review.yaml", ROLLBACK_SECTION)
    found = DefinitionSet.discover(org=org, team=team, repo=repo)
    review = found.handoffs.schema("review")
    assert "jira_ticket" in review.model.model_fields and review.sections[-2].heading == "Rollback plan"

def test_a_repo_cannot_define_a_new_kind(tmp_path):
    repo = make_layer(tmp_path / "repo")
    write(repo / "handoffs" / "threat-model.yaml", "new_kind: true\ntitle: T\n")
    with pytest.raises(DefinitionError, match="policy repo"):
        DefinitionSet.discover(org=None, team=None, repo=repo)

def test_an_agent_producing_an_unknown_kind_is_refused(tmp_path):
    repo = make_layer(tmp_path / "repo", agent_produces="threat-model")
    with pytest.raises(DefinitionError, match="unknown handoff kind 'threat-model'"):
        DefinitionSet.discover(org=None, team=None, repo=repo)

def test_a_kind_defined_by_the_team_can_be_produced(tmp_path):
    team = make_layer(tmp_path / "team")
    write(team / "handoffs" / "threat-model.yaml", "new_kind: true\ntitle: Threat model\n")
    repo = make_layer(tmp_path / "repo", agent_produces="threat-model")
    assert DefinitionSet.discover(org=None, team=team, repo=repo).handoffs.schema("threat-model")

def test_kind_violations_cover_kinds_from_the_type(resolved):  # test_resolve.py
    agent = resolved.model_copy(update={"produces": "threat-model", "consumes": ["design", "poem"]})
    assert [(v.field, v.requested) for v in kind_violations(agent, BUILTIN_CATALOG)] == [
        ("produces", "threat-model"), ("consumes", "poem")]
```

`make_layer(path, agent_produces=None)` is the existing helper, extended to write one agent file whose `produces` is set when given. `write` creates parent folders.

- [ ] **Step 2: Run the tests to verify they fail.** Run `uv run pytest core/tests/definitions/test_discovery.py core/tests/policy/test_resolve.py -q`. Expected: the new tests FAIL (`DefinitionSet` has no `handoffs`; no `kind_violations`).
- [ ] **Step 3: Implement** both changes.
- [ ] **Step 4: Run** `uv run pytest core/tests -q`. Expected: all pass.
- [ ] **Step 5: Add tracker row M1-79** as Done, with the commit SHA and check count. Its text: `**Handoff template inheritance (D37)**: base template; org/team/repo extensions add or tighten fields and sections, never remove; new kinds at org/team; unknown kinds refused`, `Depends on M1-19`, `Done when: refusal per rule with file and line; extensions stack across layers; unknown kinds refused`, `PW 1`. Run `uv run python scripts/check.py --all` (expected: green), then commit: `feat(core): handoff extensions discovered per layer; unknown kinds refused (M1-79, D37)`.

---

### Task 5 (M1-20): the validation retry, through the runner

**Files:**
- Create: `core/src/agentic/handoffs/contract.py`
- Modify:
  - `core/src/agentic/agents/runner.py`
  - `core/src/agentic/handoffs/__init__.py`
  - `docs/architecture/call-flow.md`: "invalid handoff → retry once" marked ✅; step-table row 8; template inheritance noted.
  - `docs/superpowers/plans/TRACKER.md`: M1-20 Done.
- Test: `core/tests/agents/test_contract.py`

**Interfaces:**
- Consumes: `HandoffSchema`, `render_template` (Task 1); `finalise_handoff`, `EnvelopeStamp`, `BLOCK_ON_DEFAULT` (Task 2); `PydanticAIRunner`, `AgentOutcome` (SP4).
- Produces:
  - In `runner.py`:
    - `class OutputContract(Protocol)` with `instructions: str` and `def check(self, text: str) -> tuple[str | None, list[str]]`.
    - `OUTPUT_RETRIES = 1`.
    - `retry_message(errors: list[str]) -> str`.
    - A keyword `PydanticAIRunner(..., contract: OutputContract | None = None)`.
    - `AgentOutcome.status` gains `"invalid"`, and a new field `errors: list[str] = field(default_factory=list)`.
  - In `contract.py`: `HandoffContract(schema: HandoffSchema, run_id: str, step: str, agent: str, block_on: frozenset[str] = BLOCK_ON_DEFAULT, clock: Callable[[], datetime] = <UTC now>)`, frozen.
    - `instructions` returns the text below with `render_template(schema)` inserted.
    - `check(text)` calls `finalise_handoff` with `EnvelopeStamp(run_id, step, agent, clock())` and maps `HandoffError.errors` to the second element.

`instructions` text (exact copy):

```
<output-format kind="{kind}">
Finish by answering with one handoff document and nothing else: a line '---', YAML fields, a line '---', then the Markdown body. The framework fills in kind, schema_version, run_id, step, agent and produced_at. You write status (complete, partial or blocked) and the fields shown below. Keep every section heading; replace every example value and every <!-- guidance --> comment with your own content.

{template}
</output-format>
```

`retry_message` text (exact copy):

```
Your handoff document is not valid:
{one "- <error>" line per error}
Answer again with the whole document, corrected: a line '---', the YAML fields, a line '---', then the Markdown body.
```

Runner behaviour:
- `_instructions` appends `contract.instructions` after the input handoffs.
- The agent is built with `retries={"tools": 1, "output": OUTPUT_RETRIES}`.
- The output validator runs only with a contract and a `str` output; a `DeferredToolRequests` pause is never validated. On errors it records `(text, errors)` for this drive and raises `ModelRetry(retry_message(errors))`. Otherwise it returns the final document, so `outcome.output` is the document.
- If `UnexpectedModelBehavior` is raised after a failed check, the result is `AgentOutcome(status="invalid", output=<last text>, errors=<last errors>, stopped=None, ...)`. Any other `UnexpectedModelBehavior` is re-raised.
- `resume` validates the same way, because the saved instructions include the output format.
- Children get no contract.
- The engine turns `invalid` into a human gate (M1-22) and builds the contract from `DefinitionSet.handoffs` with the run's `block_on`. It records `catalog.digest` in the run (M1-21).

- [ ] **Step 1: Write the failing tests** in `core/tests/agents/test_contract.py`.
  - `CONTRACT = HandoffContract(BUILTIN_CATALOG.schema("design"), "r-sp5-1", "design", "design@1.4.0", clock=lambda: FIXED)`.
  - `VALID` is a valid design answer and `PROSE = "I think we should add a queue."`.
  - `answers(*texts, seen=None)` is a `FunctionModel` returning the texts in turn and appending each message list to `seen`.
  - `write_tool` comes from `_resume_helper`.

```python
def test_a_valid_answer_completes_with_the_final_document(resolved, tmp_path):
    outcome = run(runner(resolved, answers(VALID), tmp_path, contract=CONTRACT))
    assert outcome.status == "completed" and outcome.output.startswith("---\nkind: design\n")

def test_an_invalid_answer_is_retried_once_with_its_errors(resolved, tmp_path):
    seen = []
    outcome = run(runner(resolved, answers(PROSE, VALID, seen=seen), tmp_path, contract=CONTRACT))
    assert outcome.status == "completed" and len(seen) == 2
    assert "must be one handoff document" in seen[1][-1].parts[-1].model_response()

def test_two_invalid_answers_end_invalid_and_keep_the_text(resolved, tmp_path):  # Review Focus 5
    seen = []
    outcome = run(runner(resolved, answers(PROSE, PROSE, PROSE, seen=seen), tmp_path, contract=CONTRACT))
    assert (outcome.status, outcome.output) == ("invalid", PROSE)
    assert outcome.errors and len(seen) == 2  # never a third try

def test_the_model_is_told_the_output_format(resolved, tmp_path):
    seen = []
    run(runner(resolved, answers(VALID, seen=seen), tmp_path, contract=CONTRACT))
    assert '<output-format kind="design">' in seen[0][0].instructions and "## Summary" in seen[0][0].instructions

def test_a_pause_for_approval_is_not_validated(resolved, tmp_path):
    calls = FunctionModel(lambda m, i: ModelResponse(parts=[ToolCallPart("write_file", {"path": "out.txt"})]))
    outcome = run(runner(resolved, calls, tmp_path, contract=CONTRACT, tools=[write_tool(tmp_path)]))
    assert outcome.status == "paused" and outcome.state_digest

def test_without_a_contract_any_text_completes(resolved, tmp_path):
    assert run(runner(resolved, answers(PROSE), tmp_path)).output == PROSE
```

- [ ] **Step 2: Run the tests to verify they fail.** Run `uv run pytest core/tests/agents/test_contract.py -q`. Expected: collection error, `cannot import name 'HandoffContract'`.
- [ ] **Step 3: Implement** `contract.py` and the runner changes.
- [ ] **Step 4: Run** `uv run pytest core/tests -q`. Expected: all pass.
- [ ] **Step 5: Update call-flow and the tracker (M1-20 Done). Run `uv run python scripts/check.py --all` (expected: green on Windows and Linux), then commit:** `feat(core): handoff validation retry - errors fed back once, then invalid for a human gate (M1-20)`.
