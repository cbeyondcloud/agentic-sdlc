"""Parsing a handoff document: split front matter from body, validate both.

Humans may edit a handoff at a gate. If they break it, every error must say
exactly where (the line number in the document) so they can fix it; the text
itself is never changed by parsing.
"""

from pydantic import ValidationError

from agentic.definitions import describe_validation_error
from agentic.handoffs.kinds import HANDOFF_KINDS
from agentic.handoffs.model import Handoff, HandoffEnvelope, HandoffError
from agentic.yamlio import YamlFileError, line_of, load_yaml, split_front_matter

ENVELOPE_FIELDS = set(HandoffEnvelope.model_fields)


def parse_handoff(text: str) -> Handoff:
    try:
        header, body = split_front_matter(text.removeprefix("﻿"))
        meta, lines = load_yaml(header, first_line=2)  # line 1 is the opening '---'
    except YamlFileError as error:
        raise HandoffError([f"front matter: {error}"]) from error
    if not isinstance(meta, dict):
        raise HandoffError(["front matter must contain key: value fields"])

    kind = meta.get("kind")
    if not isinstance(kind, str) or kind not in HANDOFF_KINDS:
        line = line_of(lines, ("kind",))
        where = f" (line {line})" if line else ""
        known = ", ".join(HANDOFF_KINDS)
        raise HandoffError([f"kind: {kind!r} is not a known handoff kind (known: {known}){where}"])
    data_model = HANDOFF_KINDS[kind]

    envelope_part = {key: value for key, value in meta.items() if key in ENVELOPE_FIELDS}
    data_part = {key: value for key, value in meta.items() if key not in ENVELOPE_FIELDS}

    problems: list[str] = []
    envelope = data = None
    try:
        envelope = HandoffEnvelope.model_validate(envelope_part)
    except ValidationError as error:
        problems += describe_validation_error(error, "handoff", lines=lines)
    try:
        data = data_model.model_validate(data_part)
    except ValidationError as error:
        problems += describe_validation_error(error, kind, lines=lines)

    supported = data_model.SCHEMA_VERSION  # type: ignore[attr-defined]
    if envelope is not None and envelope.schema_version != supported:
        line = line_of(lines, ("schema_version",))
        problems.append(
            f"schema_version: {envelope.schema_version} is not supported for kind "
            f"'{kind}' (supported: {supported}) (line {line})"
        )
    if problems:
        raise HandoffError(problems)
    return Handoff(envelope=envelope, data=data, body=body)  # type: ignore[arg-type]
