"""Turning an agent's answer into the final handoff document (spec §7.1, §7.4).

The agent writes `status`, its kind's fields and the Markdown body. The
framework owns the envelope - kind, schema_version, run_id, step, agent,
produced_at - and writes it, whatever the agent put there. It also counts a
review's `open_blocking` itself, from the findings and the run's `block_on`
severities: the review loop ends on that number, so the model never decides it.

Errors carry line numbers in the agent's own text, so the retry message (M1-20)
can point at the exact line to fix.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError

from agentic.definitions import describe_validation_error
from agentic.handoffs.kinds import SEVERITIES, count_open_blocking
from agentic.handoffs.model import Handoff, HandoffEnvelope, HandoffError
from agentic.handoffs.parse import missing_sections, parse_handoff
from agentic.handoffs.schema import HandoffCatalog, HandoffSchema
from agentic.yamlio import YamlFileError, dump_yaml, line_of, load_yaml, split_front_matter

BLOCK_ON_DEFAULT = frozenset({"critical", "high"})  # Zyroflo's default and floor
FRAMEWORK_FIELDS = ("kind", "schema_version", "run_id", "step", "agent", "produced_at")
NO_DOCUMENT = (
    "the answer must be one handoff document: a line '---', the YAML fields, "
    "a line '---', then the Markdown body"
)


@dataclass(frozen=True)
class EnvelopeStamp:
    """What the framework writes into every handoff's envelope."""

    run_id: str
    step: str
    agent: str  # name@version
    produced_at: datetime

    def __post_init__(self) -> None:
        if self.produced_at.tzinfo is None or self.produced_at.utcoffset() is None:
            raise ValueError("produced_at must be timezone-aware")


def finalise_handoff(
    text: str,
    *,
    schema: HandoffSchema,
    stamp: EnvelopeStamp,
    block_on: frozenset[str] = BLOCK_ON_DEFAULT,
    person: bool = False,
) -> tuple[str, Handoff]:
    """The final document and its parsed handoff, or HandoffError with every problem.
    `person`: the text is a person's (an edit at a gate), who may accept a finding at
    a blocking severity or a failing test (`accepted_failures`); an agent may not
    (DF-41, owner 2026-10-20; review F3)."""
    check_block_on(block_on)

    text, above = _unwrapped(text)
    try:
        header, body = split_front_matter(text)
    except YamlFileError as error:
        raise HandoffError([NO_DOCUMENT]) from error
    try:
        meta, lines = load_yaml(header, first_line=2 + above)
    except YamlFileError as error:
        raise HandoffError([f"front matter: {error}"]) from error
    meta = {} if meta is None else meta
    if not isinstance(meta, dict):
        raise HandoffError(["front matter must contain key: value fields"])

    problems: list[str] = []
    given = meta.get("kind")
    if given is not None and given != schema.kind:
        problems.append(
            f"kind: this step produces '{schema.kind}', not '{given}' "
            f"(line {line_of(lines, ('kind',))})"
        )
    data = {k: v for k, v in meta.items() if k not in FRAMEWORK_FIELDS and k != "status"}
    if schema.kind == "review":  # by kind: another kind's own open_blocking field is its own
        data["open_blocking"] = count_open_blocking(data.get("findings"), block_on)
        if not person:
            problems += _accepted_by_agent(data.get("findings"), block_on, lines)
    if schema.kind == "test-report" and not person and data.get("accepted_failures"):
        problems.append(  # QA accepting its own failures would end the loop (review F3)
            "accepted_failures: only a person can accept a failure, at a gate "
            f"(line {line_of(lines, ('accepted_failures',))})"
        )
    envelope = {
        "kind": schema.kind,
        "schema_version": schema.schema_version,
        "run_id": stamp.run_id,
        "step": stamp.step,
        "agent": stamp.agent,
    }

    try:
        HandoffEnvelope.model_validate(
            {**envelope, "produced_at": stamp.produced_at, "status": meta.get("status")}
        )
    except ValidationError as error:
        problems += describe_validation_error(error, "handoff", lines=lines)
    try:
        schema.model.model_validate(data)
    except ValidationError as error:
        problems += describe_validation_error(error, schema.kind, lines=lines)
    if not body.strip():
        problems.append("body: the Markdown document after the front matter is empty")
    else:
        problems += missing_sections(body, schema)
    if problems:
        raise HandoffError(problems)

    produced = stamp.produced_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    document = (
        f"---\n{dump_yaml(envelope)}produced_at: {produced}\n"
        f"{dump_yaml({'status': meta['status'], **data})}---\n{body}\n"
    )
    return document, parse_handoff(document, HandoffCatalog({schema.kind: schema}))


def check_block_on(block_on: frozenset[str]) -> None:
    """A caller's block_on must name known severities and keep the floor (critical, high)."""
    if unknown := sorted(set(block_on) - set(SEVERITIES)):
        raise ValueError(f"unknown severities in block_on: {unknown} (known: {SEVERITIES})")
    if missing := sorted(BLOCK_ON_DEFAULT - set(block_on)):
        raise ValueError(
            f"block_on must keep the floor {sorted(BLOCK_ON_DEFAULT)}; missing {missing}"
        )


def _accepted_by_agent(findings: object, block_on: frozenset[str], lines) -> list[str]:
    """An agent marking a blocking finding `accepted` would remove it from the review
    loop's exit count: only a person (at a gate) may do that."""
    if not isinstance(findings, list):
        return []
    return [
        f"findings[{index}]: only a person can accept a finding at a blocking severity "
        f"({finding.get('severity')}) (line {line_of(lines, ('findings', index))})"
        for index, finding in enumerate(findings)
        if isinstance(finding, dict)
        and finding.get("status") == "accepted"
        and finding.get("severity") in block_on
    ]


def _unwrapped(text: str) -> tuple[str, int]:
    """LF line endings, no byte-order mark, and one outer code fence (three or more
    backticks or tildes, DF-35) removed; with the number of lines removed above the
    document, so errors name lines in the agent's own text."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").removeprefix("\ufeff")
    above = _leading_lines(text)
    text = text.strip()
    lines = text.split("\n")
    fence = _FENCE.match(lines[0])
    if len(lines) >= 2 and fence and _closes(lines[-1].strip(), fence.group(1)):
        inner = "\n".join(lines[1:-1])
        above += 1 + _leading_lines(inner)
        text = inner.strip()
    return text, above


_FENCE = re.compile(r"(`{3,}|~{3,})[^`~]*$")


def _closes(line: str, opening: str) -> bool:
    return len(line) >= 3 and set(line) == {opening[0]}


def _leading_lines(text: str) -> int:
    """How many whole lines `strip()` removes from the start of `text`."""
    return text[: len(text) - len(text.lstrip())].count("\n")
