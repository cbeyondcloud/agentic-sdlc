"""Feedback records: one human correction of an agent's work (M3-11, spec §24 "Capture").

A `FeedbackRecord` is what a person changed or said, where, and about whose output:

  * `gate-rejection` - a gate sent the work back with feedback (`text`);
  * `gate-edit`      - a person edited a pinned handoff before approving it (`diff`);
  * `pr-review`      - a review comment, or "changes requested", on an agent-made pull
                       request (`path`/`line` for an inline comment);
  * `review-miss`    - a person asked for a change on lines the Code Review agent passed
                       without a finding.

Its id is derived from the run, the signal and the correction's `source` (the gate and the
bytes it pinned, the pull request comment), so capturing the same correction twice records
it once. A record names people (`actor`): it lives in the run's feedback journal and the
control plane's feedback table, never in the run ledger, whose events carry ids only.

The journal carries three events, folded by `fold` into each record's current state:
`learning.feedback_captured` (the record, the rules' attribution if they decided, and a
lesson), `learning.feedback_analysed` (the Learning Analyst's attribution and lesson) and
`learning.attribution_corrected` (a person's correction, which analysis never overrides).
"""

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Annotated, Literal, get_args

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

Signal = Literal["gate-rejection", "gate-edit", "pr-review", "review-miss"]
SIGNALS: tuple[str, ...] = get_args(Signal)
#: The longest text, diff and lesson a record keeps (longer ones are cut with a marker).
MAX_TEXT, MAX_DIFF, MAX_LESSON = 4000, 32000, 200
#: The journal's events (`sdlc_commons.feedback.FeedbackJournal`).
CAPTURED = "learning.feedback_captured"
ANALYSED = "learning.feedback_analysed"
CORRECTED = "learning.attribution_corrected"
EVENTS = (CAPTURED, ANALYSED, CORRECTED)
#: An agent's name, as agent files name it (`developer`, `payments-reviewer`).
AGENT_NAME = r"^[a-z][a-z0-9-]{0,63}$"
FEEDBACK_ID = r"^fb-[0-9a-f]{20}$"


def cap(text: str, limit: int) -> str:
    """`text`, cut to at most `limit` characters with a marker saying how much went."""
    if len(text) <= limit:
        return text
    marker = f"\n... [cut: {len(text)} characters]"
    return text[: max(limit - len(marker), 0)] + marker


def _capped(limit: int):
    return AfterValidator(lambda value: None if value is None else cap(value, limit))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FeedbackRecord(_Strict):
    id: Annotated[str, StringConstraints(pattern=FEEDBACK_ID)]
    signal: Signal
    run_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    actor: Annotated[str, StringConstraints(min_length=1, max_length=320)]  # who gave it
    given_at: AwareDatetime
    text: Annotated[str, _capped(MAX_TEXT)] = ""
    source: Annotated[str, StringConstraints(min_length=1, max_length=400)]
    repo: str | None = None  # host/owner/name
    gate: str | None = None
    step: str | None = None
    kind: str | None = None  # the handoff kind the correction is about
    agent: str | None = None  # name@version of the agent whose output was corrected
    diff: Annotated[str | None, _capped(MAX_DIFF)] = None
    path: str | None = None  # an inline review comment's file
    line: int | None = Field(default=None, ge=1)
    review_state: str | None = None  # "changes_requested", "commented"
    tdd_skipped: bool | None = None  # the run's developer reported no tests (None: unknown)

    @property
    def agent_name(self) -> str | None:
        return self.agent.split("@", 1)[0] if self.agent else None


def feedback_id(run_id: str, signal: str, source: str) -> str:
    material = "\x00".join((run_id, signal, source)).encode("utf-8")
    return "fb-" + hashlib.sha256(material).hexdigest()[:20]


class Learner(_Strict):
    agent: Annotated[str, StringConstraints(pattern=AGENT_NAME)]
    confidence: float = Field(ge=0, le=1)


class Attribution(_Strict):
    """Which agents learn from a record, how that was decided, and why."""

    learners: tuple[Learner, ...] = Field(min_length=1, max_length=8)
    method: Literal["rules", "analyst", "human"]
    reason: Annotated[str, StringConstraints(max_length=500)] = ""

    @property
    def agents(self) -> list[str]:
        return [learner.agent for learner in self.learners]


# ── the journal's events ────────────────────────────────────────────────────


def _attribution(value: Attribution | None) -> dict | None:
    return value.model_dump(mode="json") if value is not None else None


def captured_fields(record: FeedbackRecord, attribution: Attribution | None, lesson: str) -> dict:
    """The fields of a `learning.feedback_captured` row (its row id is the record's id)."""
    data = record.model_dump(mode="json", exclude={"id"})
    return {
        **data,
        "feedback": record.id,
        "attribution": _attribution(attribution),
        "lesson": cap(lesson, MAX_LESSON),
    }


def analysed_fields(feedback: str, attribution: Attribution | None, lesson: str) -> dict:
    return {
        "feedback": feedback,
        "attribution": _attribution(attribution),
        "lesson": cap(lesson, MAX_LESSON),
    }


def corrected_fields(feedback: str, attribution: Attribution, by: str) -> dict:
    """A person's correction; `by` is an identity field (`actor`), as the publish scan
    expects of the people an audit trail is about."""
    return {"feedback": feedback, "attribution": _attribution(attribution), "actor": by}


RECORD_FIELDS = frozenset(FeedbackRecord.model_fields) - {"id"}


def record_from(fields: Mapping[str, object]) -> FeedbackRecord:
    """The record a `learning.feedback_captured` row carries."""
    data = {name: fields[name] for name in RECORD_FIELDS if name in fields}
    return FeedbackRecord(id=fields["feedback"], **data)


@dataclass
class FeedbackState:
    """A record as it stands after every event about it."""

    record: FeedbackRecord
    attribution: Attribution | None
    lesson: str
    analysed: bool = False
    corrected_by: str | None = None


def fold(entries: Iterable[Mapping[str, object]]) -> dict[str, FeedbackState]:
    """Each record's current state, in capture order. An event about a record not captured
    (yet) is ignored; analysis never replaces a person's correction."""
    states: dict[str, FeedbackState] = {}
    for entry in entries:
        event, feedback = entry.get("event"), str(entry.get("feedback", ""))
        attribution = entry.get("attribution")
        given = Attribution.model_validate(attribution) if attribution else None
        if event == CAPTURED:
            if feedback not in states:
                states[feedback] = FeedbackState(
                    record_from(entry), given, str(entry.get("lesson") or "")
                )
            continue
        state = states.get(feedback)
        if state is None:
            continue
        if event == ANALYSED:
            state.analysed = True
            state.lesson = str(entry.get("lesson") or state.lesson)
            if given is not None and state.corrected_by is None:
                state.attribution = given
        elif event == CORRECTED and given is not None:
            state.attribution = given
            state.corrected_by = str(entry.get("actor") or "")
    return states


__all__ = [
    "ANALYSED",
    "CAPTURED",
    "CORRECTED",
    "EVENTS",
    "MAX_DIFF",
    "MAX_LESSON",
    "MAX_TEXT",
    "SIGNALS",
    "Attribution",
    "FeedbackRecord",
    "FeedbackState",
    "Learner",
    "Signal",
    "analysed_fields",
    "cap",
    "captured_fields",
    "corrected_fields",
    "feedback_id",
    "fold",
    "record_from",
]
