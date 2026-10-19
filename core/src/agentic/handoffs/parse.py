"""Parsing a handoff document: split front matter from body, validate both.

Humans may edit a handoff at a gate. If they break it, every error must say
exactly where (the line number in the document) so they can fix it; the text
itself is never changed by parsing.

A document is checked against a catalog of kinds (D37): the built-in one by
default, or the run's merged catalog with org, team and repo extensions. Its
fields must match the kind's merged model and its body must contain every
section heading of the kind's template.
"""

from pydantic import ValidationError

from agentic.definitions import describe_validation_error
from agentic.handoffs.model import Handoff, HandoffEnvelope, HandoffError
from agentic.handoffs.schema import BUILTIN_CATALOG, HandoffCatalog, HandoffSchema
from agentic.yamlio import YamlFileError, line_of, load_yaml, split_front_matter

ENVELOPE_FIELDS = set(HandoffEnvelope.model_fields)


def parse_handoff(text: str, catalog: HandoffCatalog = BUILTIN_CATALOG) -> Handoff:
    try:
        header, body = split_front_matter(text.removeprefix("\ufeff"))
        meta, lines = load_yaml(header, first_line=2)  # line 1 is the opening '---'
    except YamlFileError as error:
        raise HandoffError([f"front matter: {error}"]) from error
    if not isinstance(meta, dict):
        raise HandoffError(["front matter must contain key: value fields"])

    kind = meta.get("kind")
    if not isinstance(kind, str) or kind not in catalog.schemas:
        line = line_of(lines, ("kind",))
        where = f" (line {line})" if line else ""
        known = ", ".join(catalog.kinds())
        raise HandoffError([f"kind: {kind!r} is not a known handoff kind (known: {known}){where}"])
    schema = catalog.schema(kind)

    envelope_part = {key: value for key, value in meta.items() if key in ENVELOPE_FIELDS}
    data_part = {key: value for key, value in meta.items() if key not in ENVELOPE_FIELDS}

    problems: list[str] = []
    envelope = data = None
    try:
        envelope = HandoffEnvelope.model_validate(envelope_part)
    except ValidationError as error:
        problems += describe_validation_error(error, "handoff", lines=lines)
    try:
        data = schema.model.model_validate(data_part)
    except ValidationError as error:
        problems += describe_validation_error(error, kind, lines=lines)

    if envelope is not None and envelope.schema_version != schema.schema_version:
        line = line_of(lines, ("schema_version",))
        problems.append(
            f"schema_version: {envelope.schema_version} is not supported for kind "
            f"'{kind}' (supported: {schema.schema_version}) (line {line})"
        )
    problems += missing_sections(body, schema)
    if problems:
        raise HandoffError(problems)
    return Handoff(envelope=envelope, data=data, body=body)  # type: ignore[arg-type]


def missing_sections(body: str, schema: HandoffSchema) -> list[str]:
    """One error per section heading of the kind's template that the body lacks."""
    present = {line.rstrip() for line in body.split("\n")}
    return [
        f"body: section '## {section.heading}' is missing"
        for section in schema.sections
        if f"## {section.heading}" not in present
    ]
