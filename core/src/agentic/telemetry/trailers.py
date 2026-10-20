"""Code audit through git trailers, and the PR body (M1-45; spec §14.5, §16).

Every checkpoint commit an agent step makes ends with six trailers:

    Agentic-Run: r-2026-10-16-7f3a
    Agentic-Step: develop (iteration 2)
    Agentic-Agent: developer@1.2.0
    Agentic-Triggered-By: asha@example.com
    Agentic-Approved-By: ravi@example.com (gate: design-approval)
    Agentic-Handoff: handoffs/design-1.md

`Agentic-Approved-By` names the run's latest approval before the commit, and
`Agentic-Handoff` is repeated once per handoff the step consumed (its "why"; paths are
relative to the run folder, `.agentic/local/runs/<run>/`). When there is none, the value
is `none`, so every commit carries all six and a reader never guesses.

Values come partly from people (the starter, an approver), so each is made one line:
a newline in a value cannot add a trailer of its own.

The PR body links the run and every handoff it wrote (with its digest), lists the
approvals, and shows the AGENTS.md additions agents proposed for review (M1-46).
"""

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from agentic.engine.state import RunState

TRAILER_KEYS = (
    "Agentic-Run",
    "Agentic-Step",
    "Agentic-Agent",
    "Agentic-Triggered-By",
    "Agentic-Approved-By",
    "Agentic-Handoff",
)
NONE = "none"
_TRAILER = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]*): (.*)")


@dataclass(frozen=True)
class CommitAudit:
    run_id: str
    step: str
    iteration: int
    agent: str  # name@version
    triggered_by: str  # the run's starter
    approved_by: str | None  # the latest approval's approver, if any
    approval_gate: str | None
    handoffs: Sequence[str]  # run-folder relative paths the step consumed


def commit_message(subject: str, audit: CommitAudit) -> str:
    """`subject`, a blank line, then the six trailers."""
    approved = NONE
    if audit.approved_by:
        approved = f"{_line(audit.approved_by)} (gate: {_line(audit.approval_gate or NONE)})"
    trailers = [
        ("Agentic-Run", _line(audit.run_id)),
        ("Agentic-Step", f"{_line(audit.step)} (iteration {int(audit.iteration)})"),
        ("Agentic-Agent", _line(audit.agent)),
        ("Agentic-Triggered-By", _line(audit.triggered_by)),
        ("Agentic-Approved-By", approved),
    ]
    trailers += [("Agentic-Handoff", _line(rel)) for rel in audit.handoffs] or [
        ("Agentic-Handoff", NONE)
    ]
    body = "\n".join(f"{key}: {value}" for key, value in trailers)
    return f"{_line(subject)}\n\n{body}\n"


def parse_trailers(message: str) -> dict[str, list[str]]:
    """The trailers of a commit message's last paragraph, by key, in order."""
    paragraphs = [p for p in message.replace("\r\n", "\n").strip().split("\n\n") if p.strip()]
    found: dict[str, list[str]] = {}
    if len(paragraphs) < 2:
        return found
    for line in paragraphs[-1].split("\n"):
        match = _TRAILER.fullmatch(line.strip())
        if match is None:
            return {}  # not a trailer block (git's rule: every line is a trailer)
        found.setdefault(match.group(1), []).append(match.group(2))
    return found


def pr_body(
    state: RunState,
    *,
    run_link: str,
    handoff_link: Callable[[str], str] = lambda rel: rel,
    proposals: Iterable[tuple[str, str]] = (),
) -> str:
    """The pull request's description: the run, its handoffs (the why), its approvals."""
    lines = [
        f"## Agentic run [{state.run_id}]({run_link})",
        "",
        f"- Pipeline: `{state.pipeline}` {state.pipeline_version} ({state.regime})",
        f"- Started by: {_line(state.starter)}",
        f"- Policy version: `{state.policy_version}`",
        f"- Status: {state.status}",
        "",
        "### Handoffs",
        "",
    ]
    written = [r for r in state.history if r.handoff and not r.handoff.endswith(".invalid.md")]
    for record in written:
        label = record.step if record.iteration <= 1 else f"{record.step} #{record.iteration}"
        lines.append(f"- [{label}]({handoff_link(record.handoff)}) `{record.digest}`")
    if not written:
        lines.append("- none")
    lines += ["", "### Approvals", ""]
    lines += [f"- {a.gate}: {_line(a.approver)}" for a in state.approvals] or ["- none"]
    proposed = list(proposals)
    if proposed:
        lines += ["", "### Proposed AGENTS.md additions (review before accepting)", ""]
        lines += [f"- {_line(text)} (from {handoff_link(rel)})" for rel, text in proposed]
    return "\n".join(lines) + "\n"


def _line(value: object) -> str:
    """One line: every run of whitespace (newlines included) becomes one space."""
    return " ".join(str(value).split()) or NONE
