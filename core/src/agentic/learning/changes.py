"""L4: a proposal becomes a concrete, reviewable change (M3-14, spec §24 "Propose"; D29).

`draft` turns a proposal (L3) into a `Draft`: one target file's text before and after, the
unified diff between them, the corrections that triggered it (`Evidence`) and a regression
eval case (M1-63 format, always attached). Nothing is written here: a draft is applied
only after its owner approves it (L5, L6).

**The narrowest sensible target wins.** A lesson only one person's corrections support is
**personal** (their `~/.agentic`); one several people taught in one repository is **repo**;
one taught across a team's repositories is **team** (the policy repo). Org targets are
phase 2 (E2-05) and never chosen. Each scope has its change kinds, the first the default:

    personal  personal-skill  ~/.agentic/skills/learned-<agent>/SKILL.md
              personal-note   ~/.agentic/memory/learned-<agent>.md
    repo      repo-memory     AGENTS.md (created when the repo has none, DF-181)
              repo-skill      .agentic/skills/learned-<agent>/SKILL.md
              agent-instruction .agentic/agents/<agent>.md (only when the repo defines it)
    team      team-skill      teams/<team>/skills/internal/learned-<agent>/SKILL.md

A person may retarget a draft to another kind of the same or a wider scope, never a
narrower one (`edit`).

**Instruction-only.** A change adds one marked block of text under the "Learned from
corrections" heading, and nothing else: an agent definition's front matter stays
byte-identical, a skill's front matter holds only the personal-skill keys (never tools,
models or limits), the added text passes the admission scan (no injection phrase, no
download-and-run line, no hidden characters), and a personal skill must load as one
(`load_personal`). `strip_change` takes the block out again - the content of a revert.

**Line endings.** The text is changed on its LF view and written back in the file's own
majority convention, so a CRLF file on Windows stays CRLF; a file without a final newline
keeps its last line and stays without one. A change that removes any line is refused.
"""

import difflib
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints

from agentic.learning.clustering import Proposal
from agentic.learning.records import AGENT_NAME
from agentic.learning.regression import case_path, regression_case
from agentic.skills.admission import scan_text
from agentic.skills.personal import ALLOWED_FRONT_MATTER, load_personal
from agentic.yamlio import YamlFileError, load_yaml, split_front_matter

Scope = Literal["personal", "repo", "team", "org"]
ChangeKind = Literal[
    "personal-skill",
    "personal-note",
    "repo-memory",
    "repo-skill",
    "agent-instruction",
    "team-skill",
]
Root = Literal["home", "repo", "policy"]
CHANGE_KINDS: tuple[str, ...] = get_args(ChangeKind)
#: Each scope's change kinds, the default first; scopes from narrowest to widest.
KINDS: dict[str, tuple[str, ...]] = {
    "personal": ("personal-skill", "personal-note"),
    "repo": ("repo-memory", "repo-skill", "agent-instruction"),
    "team": ("team-skill",),
    "org": (),
}
SCOPE_ORDER = ("personal", "repo", "team", "org")
SCOPE_OF = {kind: scope for scope, kinds in KINDS.items() for kind in kinds}
SKILLS = frozenset({"personal-skill", "repo-skill", "team-skill"})
HEADING = "## Learned from corrections"
_START = "<!-- agentic-learning {id} -->"
_END = "<!-- /agentic-learning {id} -->"


class DraftError(Exception):
    """The proposal cannot be drafted as asked (no such target, a narrower kind)."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Evidence(_Frozen):
    """One correction that triggered a proposal, as its reviewer sees it."""

    feedback_id: str
    signal: str
    actor: str
    given_at: AwareDatetime
    text: str = ""
    run_id: str
    repo: str | None = None


class Draft(_Frozen):
    """The change a proposal would make, ready for its owner to review."""

    proposal_id: str
    agent: Annotated[str, StringConstraints(pattern=AGENT_NAME)]
    lesson: str
    scope: Scope
    owner: str  # the user (personal), "repo:<key>" or "team:<team>"
    kind: ChangeKind
    root: Root
    path: str  # POSIX, relative to the root
    before: str | None  # None: the file does not exist yet
    after: str
    diff: str
    evidence: tuple[Evidence, ...]
    eval_case: dict
    eval_path: str
    problems: tuple[str, ...] = ()  # why it is not instruction-only (empty: it is)
    drafted_at: AwareDatetime


def narrowest_scope(evidence: Sequence[Evidence], cluster_scope: str) -> Scope:
    """personal when one person gave every correction, else the cluster's own scope (the
    repo, or the team when the corrections span its repos)."""
    if len({e.actor.strip().casefold() for e in evidence}) == 1:
        return "personal"
    return "team" if cluster_scope.startswith("team:") else "repo"


def target(kind: str, agent: str, team: str | None) -> tuple[Root, str]:
    """Where a change of `kind` for `agent` lands: (root, POSIX path under it)."""
    learned = f"learned-{agent}"
    if kind == "personal-skill":
        return "home", f"skills/{learned}/SKILL.md"
    if kind == "personal-note":
        return "home", f"memory/{learned}.md"
    if kind == "repo-memory":
        return "repo", "AGENTS.md"
    if kind == "repo-skill":
        return "repo", f".agentic/skills/{learned}/SKILL.md"
    if kind == "agent-instruction":
        return "repo", f".agentic/agents/{agent}.md"
    if kind == "team-skill":
        if not team:
            raise DraftError("a team skill needs the team")
        return "policy", f"teams/{team}/skills/internal/{learned}/SKILL.md"
    raise DraftError(f"unknown change kind {kind!r} (one of {', '.join(CHANGE_KINDS)})")


# ── the text ────────────────────────────────────────────────────────────────


def _crlf(text: str) -> bool:
    crlf = text.count("\r\n")
    return crlf > text.count("\n") - crlf


def _lines(text: str) -> list[str]:
    """The LF view's lines, losing nothing: a last line without a final newline is kept
    (review F1: the files people edit often end without one)."""
    lines = text.replace("\r\n", "\n").split("\n")
    return lines[:-1] if lines[-1] == "" else lines


def _final_newline(text: str | None) -> bool:
    return not text or text.endswith("\n")


def _text(lines: list[str], crlf: bool, final_newline: bool = True) -> str:
    joined = "\n".join(lines) + ("\n" if lines and final_newline else "")
    return joined.replace("\n", "\r\n") if crlf else joined


def _block(kind: str, lesson: str, agent: str, proposal_id: str) -> list[str]:
    entry = f"- ({agent}) {lesson}" if kind == "repo-memory" else f"- {lesson}"
    return [_START.format(id=proposal_id), entry, _END.format(id=proposal_id)]


def _new_file(kind: str, agent: str) -> list[str]:
    if kind in SKILLS:
        description = f"Lessons people taught the {agent} agent by correcting its work."
        return [
            "---",
            f"name: learned-{agent}",
            f"description: {description}",
            "metadata:",
            f"  agents: [{agent}]",
            "---",
            "",
            f"# Learned: {agent}",
        ]
    if kind == "repo-memory":
        return ["# AGENTS.md"]
    if kind == "personal-note":
        return [f"# Learned: {agent}"]
    raise DraftError(f"there is no definition of agent {agent!r} to add an instruction to")


def render_change(kind: str, before: str | None, lesson: str, agent: str, proposal_id: str) -> str:
    """`before` with the lesson's marked block added under `HEADING` (a new file of `kind`
    when `before` is None), in `before`'s own line endings."""
    lines = _lines(before) if before is not None else _new_file(kind, agent)
    block = _block(kind, " ".join(lesson.split()), agent, proposal_id)
    if HEADING in lines:
        start = lines.index(HEADING) + 1
        end = next((i for i in range(start, len(lines)) if lines[i].startswith("#")), len(lines))
        while end > start and not lines[end - 1].strip():
            end -= 1
        lines[end:end] = block if end > start + 1 else ["", *block]
    else:
        lines += ["", HEADING, "", *block]
    return _text(lines, _crlf(before or ""), _final_newline(before))


def strip_change(text: str, proposal_id: str) -> str:
    """`text` without the proposal's block (and without the heading when nothing else is
    left under it): the content a revert restores."""
    lines = _lines(text)
    start, end = _START.format(id=proposal_id), _END.format(id=proposal_id)
    if start not in lines or end not in lines:
        return text
    first, last = lines.index(start), lines.index(end)
    del lines[first : last + 1]
    if first > 0 and not lines[first - 1].strip() and lines[first - 2 : first - 1] == [HEADING]:
        del lines[first - 1]  # the blank line render put after the heading
    if HEADING in lines:
        at = lines.index(HEADING)
        rest = lines[at + 1 :]
        stop = next((i for i, line in enumerate(rest) if line.startswith("#")), len(rest))
        if not any(line.strip() for line in rest[:stop]):
            gap = 1 if at > 0 and not lines[at - 1].strip() else 0
            del lines[at - gap : at + 1 + stop]
    return _text(lines, _crlf(text), _final_newline(text))


# ── instruction-only ────────────────────────────────────────────────────────


def _front(text: str | None) -> tuple[str | None, dict]:
    if text is None:
        return None, {}
    try:
        header, _ = split_front_matter(text.replace("\r\n", "\n"))
        data = load_yaml(header, first_line=2)[0] or {}
    except YamlFileError:
        return None, {}
    return header, data if isinstance(data, dict) else {}


def instruction_only_problems(kind: str, before: str | None, after: str) -> list[str]:
    """Why `after` is more than an instruction added to `before` ([]: it is not)."""
    problems: list[str] = []
    if kind == "agent-instruction" or kind in SKILLS:
        header_before, _ = _front(before)
        header_after, data = _front(after)
        if before is not None and header_before != header_after:
            problems.append("the front matter changed: a learning adds instructions only")
        if kind in SKILLS:
            if header_after is None:
                problems.append("a skill needs its front matter (name, description)")
            if extra := sorted(set(data) - ALLOWED_FRONT_MATTER):
                problems.append(
                    f"front matter {extra} is not allowed: a learning never widens tools, "
                    "models or limits"
                )
    compared = list(difflib.ndiff(_lines(before or ""), _lines(after)))
    if removed := sum(line.startswith("- ") for line in compared):
        problems.append(
            f"the change removes {removed} line(s) of the file: a learning adds instructions only"
        )
    added = [line[2:] for line in compared if line.startswith("+ ")]
    problems += [f"line {n} of the addition: {why}" for n, why in scan_text("\n".join(added))]
    if kind == "personal-skill" and not problems:
        problems += _personal_skill_problems(after)
    return problems


def _personal_skill_problems(text: str) -> list[str]:
    """What `load_personal` refuses about the skill, written into a scratch home."""
    _, data = _front(text)
    name = str(data.get("name") or "skill")
    with tempfile.TemporaryDirectory(prefix="agentic-learning-") as scratch:
        folder = Path(scratch) / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(text, encoding="utf-8", newline="")
        found = load_personal(Path(scratch))
    return [f"{r.path}: {r.reason}" for r in found.rejected]


# ── the draft ───────────────────────────────────────────────────────────────


def _owner(scope: str, evidence: Sequence[Evidence], proposal: Proposal, team: str | None) -> str:
    if scope == "personal":
        return evidence[0].actor if evidence else proposal.actors[0]
    if scope == "team":
        return proposal.scope if proposal.scope.startswith("team:") else f"team:{team}"
    return proposal.scope if proposal.scope.startswith("repo:") else "repo:local"


def _diff(path: str, before: str | None, after: str) -> str:
    lines = difflib.unified_diff(
        _lines(before or ""),
        _lines(after),
        fromfile="/dev/null" if before is None else f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
    )
    return "".join(f"{line}\n" for line in lines)


def draft(
    proposal: Proposal,
    evidence: Sequence[Evidence],
    read: Callable[[str, str], str | None],
    *,
    kind: str | None = None,
    lesson: str | None = None,
    team: str | None = None,
    at: datetime | None = None,
) -> Draft:
    """The change `proposal` would make. `read(root, path)` gives a target file's current
    text (None: no such file); `kind` and `lesson` are a person's edits."""
    narrowest = narrowest_scope(evidence, proposal.scope) if evidence else "repo"
    kind = kind or KINDS[narrowest][0]
    scope = SCOPE_OF.get(kind)
    if scope is None:
        raise DraftError(f"unknown change kind {kind!r} (one of {', '.join(CHANGE_KINDS)})")
    if SCOPE_ORDER.index(scope) < SCOPE_ORDER.index(narrowest):
        raise DraftError(
            f"{kind} is not a {narrowest} change: the corrections come from more than one "
            f"person, so the change is {narrowest}-wide or wider ({', '.join(KINDS[narrowest])})"
        )
    root, path = target(kind, proposal.agent, team)
    before = read(root, path)
    if kind == "agent-instruction" and before is None:
        raise DraftError(
            f"the repository has no definition of agent {proposal.agent!r} "
            f"({path}) to add an instruction to"
        )
    text = " ".join((lesson or proposal.lesson).split())
    after = render_change(kind, before, text, proposal.agent, proposal.id)
    case = regression_case(proposal.id, proposal.agent, text, evidence)
    return Draft(
        proposal_id=proposal.id,
        agent=proposal.agent,
        lesson=text,
        scope=scope,
        owner=_owner(scope, evidence, proposal, team),
        kind=kind,
        root=root,
        path=path,
        before=before,
        after=after,
        diff=_diff(path, before, after),
        evidence=tuple(evidence),
        eval_case=case.model_dump(mode="json", exclude_defaults=True),
        eval_path=case_path(root, proposal.id),
        problems=tuple(instruction_only_problems(kind, before, after)),
        drafted_at=at or datetime.now(UTC),
    )


__all__ = [
    "CHANGE_KINDS",
    "HEADING",
    "KINDS",
    "SCOPE_OF",
    "ChangeKind",
    "Draft",
    "DraftError",
    "Evidence",
    "Root",
    "Scope",
    "draft",
    "instruction_only_problems",
    "narrowest_scope",
    "render_change",
    "strip_change",
    "target",
]
