"""Turning each human correction into a `FeedbackRecord` (M3-11, spec §24 "Capture").

One builder per signal. They only describe what happened; who learns from it is decided
later (`agentic.learning.attribution`, the Learning Analyst):

  * `gate_rejection` - the person's feedback, about the handoff the rejection sends back
                       (the work of the step that will redo it);
  * `gate_edit`      - the unified diff between the handoff the agent wrote and the one the
                       person approved (`difflib`, LF line endings on both sides);
  * `pr_feedback`    - review comments and "changes requested" on an agent-made pull request;
  * `review_misses`  - those of the pull request's inline comments that sit on lines the
                       Code Review agent passed: no finding of its last review within a few
                       lines of them.
"""

import difflib
import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from agentic.learning.records import FeedbackRecord, feedback_id

#: How many lines either side of a review finding count as the place the reviewer looked.
MISS_WINDOW = 3


@dataclass(frozen=True)
class HandoffRef:
    """A pinned handoff: where it is, its kind, the agent that wrote it, its digest."""

    path: str
    kind: str | None
    agent: str | None  # name@version
    digest: str


def _short(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def gate_rejection(
    *,
    run_id: str,
    gate: str,
    step: str | None,
    approver: str,
    feedback: str,
    handoff: HandoffRef | None,
    pinned: Iterable[HandoffRef],
    at: datetime,
    repo: str | None = None,
) -> FeedbackRecord:
    """The record of a rejection. `handoff` is the handoff the rejection is about (None for
    a gate that pinned none); `pinned` is everything the gate pinned: the same bytes
    rejected twice are one correction."""
    pinned_set = ",".join(sorted(f"{ref.path}={ref.digest}" for ref in pinned))
    source = f"gate:{gate}:reject:{_short(pinned_set)}"
    return FeedbackRecord(
        id=feedback_id(run_id, "gate-rejection", source),
        signal="gate-rejection",
        run_id=run_id,
        actor=approver,
        given_at=at,
        text=feedback.strip(),
        source=source,
        repo=repo,
        gate=gate,
        step=step,
        kind=handoff.kind if handoff else None,
        agent=handoff.agent if handoff else None,
        path=handoff.path if handoff else None,
    )


def unified_diff(path: str, before: str, after: str) -> str:
    lines = difflib.unified_diff(
        _lf(before).splitlines(keepends=True),
        _lf(after).splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


def _first_added(diff: str) -> str:
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++") and line[1:].strip():
            return line[1:].strip()
    return ""


def gate_edit(
    *,
    run_id: str,
    gate: str,
    step: str | None,
    approver: str,
    handoff: HandoffRef,
    before: str,
    after: str,
    notes: str,
    at: datetime,
    repo: str | None = None,
) -> FeedbackRecord:
    """The record of an edit-and-approve: the diff, and the approver's notes (or, without
    notes, the first line the person added)."""
    diff = unified_diff(handoff.path, before, after)
    source = f"gate:{gate}:edit:{handoff.path}:{_short(_lf(after))}"
    return FeedbackRecord(
        id=feedback_id(run_id, "gate-edit", source),
        signal="gate-edit",
        run_id=run_id,
        actor=approver,
        given_at=at,
        text=notes.strip() or _first_added(diff),
        source=source,
        repo=repo,
        gate=gate,
        step=step,
        kind=handoff.kind,
        agent=handoff.agent,
        diff=diff,
        path=handoff.path,
    )


# ── pull request reviews ────────────────────────────────────────────────────


class ReviewLike(Protocol):
    """A pull request review (`agentic.hosting.base.PullRequestReview`)."""

    id: int
    author: str
    state: str  # "changes_requested", "commented", "approved", "dismissed"
    body: str
    submitted_at: datetime | None


class CommentLike(Protocol):
    """An inline review comment (`agentic.hosting.base.ReviewComment`)."""

    id: int
    review_id: int | None
    author: str
    body: str
    path: str
    line: int | None
    created_at: datetime | None
    in_reply_to: int | None


class FindingLike(Protocol):
    """A Code Review finding (`agentic.handoffs.kinds.Finding`)."""

    file: str | None
    line: int | None


def _is_bot(login: str, bots: set[str]) -> bool:
    lowered = login.lower()
    return lowered in bots or lowered.endswith("[bot]")


def pr_feedback(
    *,
    run_id: str,
    repo: str,
    number: int,
    reviews: Sequence[ReviewLike],
    comments: Sequence[CommentLike],
    agent_logins: Iterable[str],
    at: datetime,
    tdd_skipped: bool | None = None,
) -> list[FeedbackRecord]:
    """The corrections in a pull request's reviews: every inline comment that opens a
    thread (a reply is the same conversation), and every review that requested changes or
    commented with a body. Approvals, empty reviews and anything written by the agent's
    own host login (`agent_logins`) or by any app account (a login ending in `[bot]`) are
    not corrections."""
    bots = {login.lower() for login in agent_logins if login}
    states = {review.id: review.state.lower() for review in reviews}
    found: list[FeedbackRecord] = []
    for review in reviews:
        state = review.state.lower()
        if _is_bot(review.author, bots) or not review.body.strip():
            continue
        if state not in ("changes_requested", "commented"):
            continue
        source = f"pr:{repo}#{int(number)}:review:{review.id}"
        found.append(
            FeedbackRecord(
                id=feedback_id(run_id, "pr-review", source),
                signal="pr-review",
                run_id=run_id,
                actor=review.author,
                given_at=review.submitted_at or at,
                text=review.body.strip(),
                source=source,
                repo=repo,
                review_state=state,
                tdd_skipped=tdd_skipped,
            )
        )
    for comment in comments:
        if _is_bot(comment.author, bots) or not comment.body.strip() or comment.in_reply_to:
            continue
        source = f"pr:{repo}#{int(number)}:comment:{comment.id}"
        found.append(
            FeedbackRecord(
                id=feedback_id(run_id, "pr-review", source),
                signal="pr-review",
                run_id=run_id,
                actor=comment.author,
                given_at=comment.created_at or at,
                text=comment.body.strip(),
                source=source,
                repo=repo,
                path=comment.path,
                line=comment.line,
                review_state=states.get(comment.review_id or -1, "commented"),
                tdd_skipped=tdd_skipped,
            )
        )
    return found


def review_misses(
    records: Iterable[FeedbackRecord],
    findings: Sequence[FindingLike],
    *,
    reviewer: str | None,
    window: int = MISS_WINDOW,
) -> list[FeedbackRecord]:
    """A `review-miss` for each inline pull request comment on a line no finding of the
    Code Review agent's last review was within `window` lines of. `reviewer` is that
    agent (name@version); no review in the run means nothing was passed, so no miss."""
    if reviewer is None:
        return []
    near = [(f.file, f.line) for f in findings if f.file]
    misses = []
    for record in records:
        if record.signal != "pr-review" or not record.path or record.line is None:
            continue
        if any(
            file == record.path and (line is None or abs(line - record.line) <= window)
            for file, line in near
        ):
            continue
        misses.append(
            record.model_copy(
                update={
                    "id": feedback_id(record.run_id, "review-miss", record.source),
                    "signal": "review-miss",
                    "kind": "review",
                    "agent": reviewer,
                }
            )
        )
    return misses


__all__ = [
    "MISS_WINDOW",
    "HandoffRef",
    "gate_edit",
    "gate_rejection",
    "pr_feedback",
    "review_misses",
    "unified_diff",
]
