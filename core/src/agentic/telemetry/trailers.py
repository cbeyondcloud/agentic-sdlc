"""Code audit through git trailers, and the PR body (M1-45; spec §14.5, §16).

Every checkpoint commit an agent step makes ends with six trailers:

    Agentic-Run: r-2026-10-16-7f3a
    Agentic-Step: develop (iteration 2)
    Agentic-Agent: developer@1.2.0
    Agentic-Triggered-By: asha@example.com
    Agentic-Approved-By: ravi@example.com (gate: design-approval)
    Agentic-Handoff: .agentic/local/runs/r-2026-10-16-7f3a/handoffs/design-1.md

`Agentic-Approved-By` names the run's latest approval before the commit that still
stands - one a later rejection sent the work back past is superseded and never cited
(DF-173) - and `Agentic-Handoff` is repeated once per handoff the step consumed (its
"why"). Its path is repo-relative when the engine is given the runs folder (the CLI
always is, DF-174), else relative to the run folder. When there is none, the value
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
#: Proposed AGENTS.md additions are agent text: shown as inline code, bounded (DF-177).
MAX_PROPOSALS = 20
MAX_PROPOSAL_CHARS = 500
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
    run_link: str | None,
    handoff_link: Callable[[str], str | None] = lambda rel: rel,
    proposals: Iterable[tuple[str, str]] = (),
    run_folder: str = "",
) -> str:
    """The pull request's description: the run, its handoffs (the why), its approvals.

    With no `run_link` (no control plane to link to), the run id is shown as code; a
    handoff whose `handoff_link` is None is shown as its path, under `run_folder` when
    given (DF-166): a link into the starter's git-ignored run folder would be broken."""
    title = f"[{state.run_id}]({run_link})" if run_link else f"`{_line(state.run_id)}`"

    def shown(label: str, rel: str) -> str:
        link = handoff_link(rel)
        if link:
            return f"[{label}]({link})"
        path = f"{run_folder.rstrip('/')}/{rel}" if run_folder else rel
        return f"{label} `{_line(path)}`"

    lines = [
        f"## Agentic run {title}",
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
        lines.append(f"- {shown(label, record.handoff)} `{record.digest}`")
    if not written:
        lines.append("- none")
    lines += ["", "### Approvals", ""]
    lines += [
        f"- {a.gate}: {_line(a.approver)}"
        + (" (superseded by a later rejection)" if a.superseded else "")
        for a in state.approvals
    ] or ["- none"]
    proposed = list(proposals)
    if proposed:
        lines += ["", "### Proposed AGENTS.md additions (review before accepting)", ""]
        for rel, text in proposed[:MAX_PROPOSALS]:
            lines.append(f"- {_code(_cut(_line(text)))} (from {shown('handoff', rel)})")
        if len(proposed) > MAX_PROPOSALS:
            lines.append(f"- ... {len(proposed) - MAX_PROPOSALS} more in the run's handoffs")
    return "\n".join(lines) + "\n"


def _line(value: object) -> str:
    """One line: every run of whitespace (newlines included) becomes one space."""
    return " ".join(str(value).split()) or NONE


def _cut(text: str) -> str:
    return text if len(text) <= MAX_PROPOSAL_CHARS else text[:MAX_PROPOSAL_CHARS] + " [... cut]"


def _code(text: str) -> str:
    """`text` as one inline code span: a fence longer than any backtick run inside it,
    so agent text cannot end the span and render an image, a link or an @mention."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    return f"{fence} {text} {fence}"
