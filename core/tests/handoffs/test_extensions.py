"""Tests for handoff extensions: layers add and tighten, never remove (M1-79, D37, spec §7.4)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentic.definitions import DefinitionError
from agentic.handoffs import parse_handoff
from agentic.handoffs.extensions import extend_catalog, load_extension
from agentic.handoffs.finalise import EnvelopeStamp, finalise_handoff
from agentic.handoffs.schema import BUILTIN_CATALOG, HandoffBase, render_template

STAMP = EnvelopeStamp("r-sp5-1", "review", "review@1.0.0", datetime(2026, 10, 19, tzinfo=UTC))
JIRA_AND_ROLLBACK = """\
fields:
  jira_ticket:
    type: string
    required: true
    description: The Jira issue this change implements
    example: PAY-123
sections:
  - heading: Rollback plan
    guidance: How to undo this change if it fails in production.
"""
ITEMS = [{"id": "REQ-1", "statement": "s", "acceptance_criteria": ["WHEN a THE system SHALL b"]}]


def ext(folder: Path, kind: str, text: str, layer: str = "team"):
    path = folder / "handoffs" / f"{kind}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return load_extension(path, layer)


def test_a_team_adds_a_required_field_and_a_section(tmp_path):
    catalog = extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "review", JIRA_AND_ROLLBACK)])
    review = catalog.schema("review")

    assert review.model.model_fields["jira_ticket"].is_required()
    assert [s.heading for s in review.sections][-2:] == ["Rollback plan", "Open questions"]
    assert "jira_ticket: PAY-123" in render_template(review)


def test_the_extended_template_still_renders_valid(tmp_path):
    catalog = extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "review", JIRA_AND_ROLLBACK)])
    review = catalog.schema("review")

    document, _ = finalise_handoff(render_template(review), schema=review, stamp=STAMP)
    assert parse_handoff(document, catalog).data.jira_ticket == "PAY-123"
    with pytest.raises(Exception, match="Rollback plan"):
        parse_handoff(document.replace("## Rollback plan\n", ""), catalog)


def test_an_optional_field_can_be_made_required(tmp_path):
    text = "fields:\n  score:\n    required: true\n"
    catalog = extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "review", text)])

    assert catalog.schema("review").model.model_fields["score"].is_required()


def test_choices_can_be_narrowed_not_widened(tmp_path):
    narrow = "fields:\n  size:\n    values: [small, medium]\n"
    catalog = extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "requirements", narrow)])
    model = catalog.schema("requirements").model

    model.model_validate({"summary": "s", "size": "small", "items": ITEMS})
    with pytest.raises(ValidationError):
        model.model_validate({"summary": "s", "size": "large", "items": ITEMS})
    wider = "fields:\n  size:\n    values: [small, huge]\n"
    with pytest.raises(DefinitionError, match="cannot add choices"):
        extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "requirements", wider)])


def test_an_added_enum_can_be_narrowed_below(tmp_path):
    org = ext(tmp_path / "org", "review", RISK_ENUM, layer="org")
    repo = ext(tmp_path / "repo", "review", "fields:\n  risk:\n    values: [low]\n", layer="repo")

    model = extend_catalog(BUILTIN_CATALOG, [org, repo]).schema("review").model
    with pytest.raises(ValidationError):
        model.model_validate({"summary": "s", "open_blocking": 0, "risk": "high"})


RISK_ENUM = (
    "fields:\n  risk:\n    type: enum\n    values: [low, high]\n"
    "    description: Risk of the change\n    example: low\n"
)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("fields:\n  summary:\n    required: false\n", "cannot loosen"),
        ("fields:\n  score:\n    type: string\n", "cannot change type"),
        (
            "fields:\n  run_id:\n    type: string\n    description: d\n    example: x\n",
            "framework",
        ),
        (
            "sections:\n  - heading: Findings\n    guidance: again\n",
            "section already defined by kind",
        ),
        ("fields:\n  ticket:\n    type: string\n    description: d\n", "example"),
        (
            "fields:\n  risk:\n    type: enum\n    values: [low]\n    description: d\n"
            "    example: high\n",
            "example",
        ),
        (
            "fields:\n  count:\n    type: integer\n    description: d\n    example: many\n",
            "example",
        ),
        ("fields:\n  Bad-Name:\n    type: string\n    description: d\n    example: x\n", "name"),
        ("remove: [score]\n", "unexpected"),
    ],
)
def test_refused_changes_name_the_file_and_line(tmp_path, text, reason):
    with pytest.raises(DefinitionError, match=reason) as caught:
        extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "review", text)])
    assert "review.yaml" in str(caught.value) and "line" in str(caught.value)


def test_a_lower_layer_cannot_loosen_what_a_higher_layer_tightened(tmp_path):
    org = ext(tmp_path / "org", "review", "fields:\n  score:\n    required: true\n", layer="org")
    repo = ext(
        tmp_path / "repo", "review", "fields:\n  score:\n    required: false\n", layer="repo"
    )

    with pytest.raises(DefinitionError, match="cannot loosen"):
        extend_catalog(BUILTIN_CATALOG, [org, repo])


def test_a_lower_layer_cannot_repeat_a_higher_layers_section(tmp_path):
    org = ext(tmp_path / "org", "review", JIRA_AND_ROLLBACK, layer="org")
    again = "sections:\n  - heading: Rollback plan\n    guidance: mine\n"
    repo = ext(tmp_path / "repo", "review", again, layer="repo")

    with pytest.raises(DefinitionError, match="section already defined by org"):
        extend_catalog(BUILTIN_CATALOG, [org, repo])


def test_new_kinds_inherit_the_base_at_org_and_team_only(tmp_path):
    text = "new_kind: true\ntitle: Threat model\nsections:\n  - heading: Threats\n    guidance: g\n"
    catalog = extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "threat-model", text)])
    schema = catalog.schema("threat-model")

    assert issubclass(schema.model, HandoffBase) and schema.title == "Threat model"
    assert [s.heading for s in schema.sections] == ["Summary", "Threats", "Open questions"]
    with pytest.raises(DefinitionError, match="policy repo"):
        extend_catalog(BUILTIN_CATALOG, [ext(tmp_path / "r", "threat-model", text, layer="repo")])


def test_a_new_kind_cannot_replace_a_builtin(tmp_path):
    text = "new_kind: true\ntitle: Review\n"
    with pytest.raises(DefinitionError, match="already exists"):
        extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "review", text)])


def test_extending_an_unknown_kind_is_refused(tmp_path):
    text = "sections:\n  - heading: Verse\n    guidance: g\n"
    with pytest.raises(DefinitionError, match="unknown kind"):
        extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "poem", text)])


def test_the_digest_changes_only_for_the_extended_kind(tmp_path):
    catalog = extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "review", JIRA_AND_ROLLBACK)])

    assert catalog.digest != BUILTIN_CATALOG.digest
    assert catalog.schema("design").digest == BUILTIN_CATALOG.schema("design").digest
    assert BUILTIN_CATALOG.schema("review").model.model_fields.get("jira_ticket") is None


# ── final review fixes (I1, M5) ───────────────────────────────────────────────


def test_tightening_keeps_the_fields_constraints(tmp_path):  # review I1
    text = "fields:\n  score:\n    required: true\n"
    model = extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "review", text)]).schema("review").model

    with pytest.raises(ValidationError):  # score is 0..10 in the built-in model
        model.model_validate({"summary": "s", "open_blocking": 0, "score": 11})


def test_narrowing_keeps_an_optional_fields_constraints(tmp_path):  # review I1
    org = ext(tmp_path / "org", "review", RISK_ENUM, layer="org")
    team = ext(tmp_path / "team", "review", "fields:\n  risk:\n    required: true\n")

    model = extend_catalog(BUILTIN_CATALOG, [org, team]).schema("review").model
    with pytest.raises(ValidationError):
        model.model_validate({"summary": "s", "open_blocking": 0})


def test_values_only_apply_to_an_enum(tmp_path):  # review M5
    text = (
        "fields:\n  code:\n    type: string\n    values: [a, b]\n"
        "    description: d\n    example: a\n"
    )

    with pytest.raises(DefinitionError, match="enum"):
        extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "review", text)])


# ── M1-81 hardening (DF-31, DF-32, DF-33) ────────────────────────────────────


def _new_field(name: str, *, kind: str = "string", required: bool = False) -> str:
    return (
        f"fields:\n  {name}:\n    type: {kind}\n    required: {str(required).lower()}\n"
        f"    description: d\n    example: x\n"
    )


@pytest.mark.parametrize("name", ["json", "copy", "model_dump", "model_config", "schema"])
def test_a_field_name_the_model_uses_is_refused_clearly(tmp_path, name, recwarn):
    """`json` or `model_config` would shadow Pydantic's own attributes: a raw error or a
    warning for an admin. Refused with a message instead (DF-31)."""
    with pytest.raises(DefinitionError) as error:
        extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "review", _new_field(name))])

    assert f"{name}: clashes with a name the handoff model uses" in str(error.value)
    assert not [w for w in recwarn if "shadows" in str(w.message)]


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("requirements", "failure"),
        ("review", "failure"),
        ("review", "summary_not_blank"),
        ("test-report", "a_pass_has_no_new_failures"),
    ],
)
def test_a_field_name_a_handoff_method_uses_is_refused(tmp_path, kind, name, recwarn):
    """A field named after a method of the kind's model (`failure`, a validator) would
    replace the method: the engine's `handoff.data.failure()` at the end of the run then
    crashes with "'str' object is not callable" after every step was paid for."""
    with pytest.raises(DefinitionError) as error:
        extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, kind, _new_field(name))])

    assert f"{name}: clashes with a name the handoff model uses" in str(error.value)
    assert not [w for w in recwarn if "shadows" in str(w.message)]


def test_a_new_kind_cannot_define_a_field_named_failure(tmp_path):
    """A new kind starts from the base template, which has `failure()` too."""
    text = "new_kind: true\ntitle: Threat model\n" + _new_field("failure")

    with pytest.raises(DefinitionError, match="failure: clashes with a name"):
        extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "threat-model", text, layer="org")])


def test_a_section_heading_with_a_line_break_is_refused(tmp_path):
    """A heading with a line break can never be matched in a body line, so every answer
    of the kind would be refused (DF-32)."""
    text = 'sections:\n  - heading: "Rollback\\nplan"\n    guidance: How to undo it.\n'

    with pytest.raises(DefinitionError, match="heading"):
        ext(tmp_path, "review", text)


@pytest.mark.parametrize("guidance", ["see --> here", "nested <!-- comment"])
def test_guidance_that_would_break_the_rendered_comment_is_refused(tmp_path, guidance):
    """Guidance is rendered inside <!-- ... -->: a `-->` in it ends the comment early and
    shows the rest to the agent as text (DF-32)."""
    text = f"sections:\n  - heading: Rollback plan\n    guidance: '{guidance}'\n"

    with pytest.raises(DefinitionError, match="guidance"):
        ext(tmp_path, "review", text)


@pytest.mark.parametrize("blank", ["''", "'   '"])
def test_a_required_string_field_refuses_a_blank_value(tmp_path, blank):
    """A required field left blank says nothing: refused like a missing one (DF-33)."""
    catalog = extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "review", JIRA_AND_ROLLBACK)])
    review = catalog.schema("review")
    document, _ = finalise_handoff(render_template(review), schema=review, stamp=STAMP)

    with pytest.raises(Exception, match="jira_ticket: .*blank"):
        parse_handoff(document.replace("jira_ticket: PAY-123", f"jira_ticket: {blank}"), catalog)


def test_blank_check_does_not_change_the_digest(tmp_path):
    """The blank check is a validator, not a schema change: a run paused before this
    release still finds the catalog it was started with."""
    catalog = extend_catalog(BUILTIN_CATALOG, [ext(tmp_path, "review", JIRA_AND_ROLLBACK)])
    schema = catalog.schema("review").model.model_json_schema()

    assert schema["properties"]["jira_ticket"] == {
        "title": "Jira Ticket",
        "type": "string",
    }
