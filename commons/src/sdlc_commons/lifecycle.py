"""Lifecycle content from Zyroflo's package (M1-10; D34: reuse, never rewrite).

Zyroflo ships its lifecycle as content (Z2): one SOP per stage (`sdlc/stages/`,
front matter + instructions) and the review rubric (`sdlc_templates/`). This
module reads them from the installed package, through Zyroflo's own accessor
(`tools.sdlc.content`) and stage parser (`stage.parse_step_spec`):

  * stages()       - the 12 stages in order, each with its front matter and its
                     instructions (what the built-in agents start from, M1-60);
                     an SOP that disagrees with Zyroflo's catalog is refused;
  * review_rubric() - the review categories and weights; refused unless the
                     weights sum to 10, as the rubric itself promises;
  * review_deductions() - points off per finding by severity, and the cap a
                     critical finding puts on the final score;
  * RUNS_ROOT      - where the framework keeps run folders (git-ignored); set for
                     Zyroflo when `sdlc_commons` is imported (Z3).
"""

import re
from dataclasses import dataclass
from pathlib import Path

from tools.sdlc import content
from tools.sdlc import stage as zyroflo_stage

#: Run folders live here, relative to the repository - git-ignored, never committed.
RUNS_ROOT = ".agentic/local/runs"

_RUBRIC = Path("sdlc_templates") / "review-rubric.md"
_FRONT_MATTER_FIELDS = (
    "phase",
    "name",
    "produces",
    "conditional",
    "gate",
    "needs",
    "evidence",
    "after_gate",
)
_CATEGORY_ROW = re.compile(
    r"^\|\s*\d+\s*\|\s*\*\*(?P<name>[^*]+)\*\*\s*\|\s*(?P<weight>\d+(?:\.\d+)?)\s*\|"
    r"\s*(?P<checks>.+?)\s*\|\s*$"
)


#: `| 🟠 High | −0.5 | ... |` and `| 🔴 Critical | caps final score at **8.0** | ... |`
_DEDUCTION_ROW = re.compile(
    r"^\s*\|[^|]*?\b(?P<severity>Critical|High|Medium|Low)\s*\|\s*(?P<value>[^|]+?)\s*\|"
)


class LifecycleError(Exception):
    """Zyroflo's lifecycle content is missing or inconsistent."""


@dataclass(frozen=True)
class Stage:
    """One lifecycle stage: its contract (front matter) and its instructions."""

    phase: str  # "P0" … "P11"
    name: str  # "intake", "requirements", …
    gate: str  # the gate at the END of the stage ("G1"…"G5"), or ""
    produces: tuple[str, ...]  # artifacts the stage owes
    conditional: tuple[str, ...]  # owed only when their trigger fires (e.g. an ADR)
    evidence: tuple[str, ...]  # written, never owed
    after_gate: tuple[str, ...]  # written after the stage's own gate
    needs: str
    sop: Path  # the SOP file it was read from
    instructions: str  # the SOP body (Markdown), without front matter


@dataclass(frozen=True)
class RubricCategory:
    name: str
    weight: float
    checks: str  # what a reviewer checks under this category


def stages() -> list[Stage]:
    """All lifecycle stages in order, read from Zyroflo's packaged SOPs."""
    return [_load(entry) for entry in zyroflo_stage.STAGES]


def stage(phase: str) -> Stage:
    """One stage by phase id ("P6")."""
    for entry in zyroflo_stage.STAGES:
        if entry.phase == phase:
            return _load(entry)
    known = ", ".join(s.phase for s in zyroflo_stage.STAGES)
    raise LifecycleError(f"no lifecycle stage {phase!r} - one of {known}")


def review_rubric() -> list[RubricCategory]:
    """The review rubric's categories (Zyroflo's `review-rubric.md`)."""
    path = content.root() / _RUBRIC
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise LifecycleError(f"review rubric not readable at {path}: {error}") from error
    section = _section(lines, "## Categories")
    rubric = [
        RubricCategory(m["name"].strip(), float(m["weight"]), m["checks"])
        for m in map(_CATEGORY_ROW.match, section)
        if m
    ]
    total = sum(c.weight for c in rubric)
    if not rubric or abs(total - 10) > 1e-9:
        raise LifecycleError(f"review rubric at {path}: category weights sum to {total:g}, not 10")
    return rubric


def review_deductions() -> dict[str, float]:
    """The rubric's severity deductions (`high`, `medium`, `low`: points off per finding)
    and `critical_cap` (the highest final score with a critical finding), from Zyroflo's
    scoring procedure; refused when one is missing (DF-235)."""
    path = content.root() / _RUBRIC
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise LifecycleError(f"review rubric not readable at {path}: {error}") from error
    found: dict[str, float] = {}
    for match in map(_DEDUCTION_ROW.match, _section(lines, "## Scoring procedure")):
        if match and (number := re.search(r"\d+(?:\.\d+)?", match["value"])):
            severity = match["severity"].lower()
            found["critical_cap" if severity == "critical" else severity] = float(number[0])
    missing = {"critical_cap", "high", "medium", "low"} - set(found)
    if missing:
        raise LifecycleError(f"review rubric at {path}: no deduction for {sorted(missing)}")
    return found


def _load(entry) -> Stage:
    path = zyroflo_stage.sop_path(entry)
    try:
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError as error:
        raise LifecycleError(f"{entry.phase}: SOP not readable at {path}: {error}") from error
    try:
        declared = zyroflo_stage.parse_step_spec(text)
    except zyroflo_stage.StageError as error:
        raise LifecycleError(f"{entry.phase}: SOP {path.name} is not valid: {error}") from error
    differs = [f for f in _FRONT_MATTER_FIELDS if getattr(declared, f) != getattr(entry, f)]
    if differs:
        raise LifecycleError(
            f"{entry.phase}: SOP {path.name} disagrees with Zyroflo's catalog "
            f"on {', '.join(differs)}"
        )
    return Stage(
        phase=entry.phase,
        name=entry.name,
        gate=entry.gate,
        produces=entry.produces,
        conditional=entry.conditional,
        evidence=entry.evidence,
        after_gate=entry.after_gate,
        needs=entry.needs,
        sop=path,
        instructions=_body(text),
    )


def _body(text: str) -> str:
    """The SOP without its `---` front matter."""
    if text.startswith("---\n") and (end := text.find("\n---\n", 4)) != -1:
        return text[end + len("\n---\n") :].lstrip("\n")
    return text


def _section(lines: list[str], heading: str) -> list[str]:
    """The lines under `heading`, up to the next heading of the same level."""
    out, inside = [], False
    for line in lines:
        if line.startswith("## "):
            inside = line.startswith(heading)
            continue
        if inside:
            out.append(line)
    return out
