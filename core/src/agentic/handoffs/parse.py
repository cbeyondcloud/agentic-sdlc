"""Parsing a handoff document: split front matter from body, validate both.

Humans may edit a handoff at a gate. If they break the front matter, the error
must say exactly where (line number in the document) so they can fix it; the
text itself is never changed by parsing.
"""

import yaml
from pydantic import ValidationError

from agentic.definitions import describe_validation_error
from agentic.handoffs.kinds import HANDOFF_KINDS
from agentic.handoffs.model import Handoff, HandoffEnvelope, HandoffError

ENVELOPE_FIELDS = set(HandoffEnvelope.model_fields)


def parse_handoff(text: str) -> Handoff:
    header, body = _split(text)
    meta = _load_header(header)

    kind = meta.get("kind")
    data_model = HANDOFF_KINDS.get(kind)  # type: ignore[arg-type]
    if data_model is None:
        known = ", ".join(HANDOFF_KINDS)
        raise HandoffError([f"kind: '{kind}' is not a known handoff kind (known: {known})"])

    envelope_part = {key: value for key, value in meta.items() if key in ENVELOPE_FIELDS}
    data_part = {key: value for key, value in meta.items() if key not in ENVELOPE_FIELDS}

    problems: list[str] = []
    envelope = data = None
    try:
        envelope = HandoffEnvelope.model_validate(envelope_part)
    except ValidationError as error:
        problems += describe_validation_error(error, "handoff")
    try:
        data = data_model.model_validate(data_part)
    except ValidationError as error:
        problems += describe_validation_error(error, kind)

    supported = data_model.SCHEMA_VERSION  # type: ignore[attr-defined]
    if envelope is not None and envelope.schema_version != supported:
        problems.append(
            f"schema_version: {envelope.schema_version} is not supported for kind "
            f"'{kind}' (supported: {supported})"
        )
    if problems:
        raise HandoffError(problems)
    return Handoff(envelope=envelope, data=data, body=body)  # type: ignore[arg-type]


def _split(text: str) -> tuple[str, str]:
    """Return (YAML front matter, Markdown body)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise HandoffError(["missing front matter: a handoff must start with a '---' line"])
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :]).strip()
    raise HandoffError(["front matter is not closed: add a '---' line after the header"])


def _load_header(header: str) -> dict:
    try:
        meta = yaml.safe_load(header) or {}
    except yaml.YAMLError as error:
        # Prefer where the broken construct starts; fall back to where parsing gave up.
        mark = getattr(error, "context_mark", None) or getattr(error, "problem_mark", None)
        where = f" at line {mark.line + 2}" if mark else ""  # +2: '---' line, 0-based index
        problem = getattr(error, "problem", None) or str(error)
        raise HandoffError([f"front matter is not valid YAML{where}: {problem}"]) from error
    if not isinstance(meta, dict):
        raise HandoffError(["front matter must contain key: value fields"])
    return meta
