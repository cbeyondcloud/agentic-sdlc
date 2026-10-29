"""The regression eval case every learning carries, and its run before and after the change
(M3-14, M3-16; DF-247).

`regression_case` writes the corrected example as an eval case in the M1-63 format
(`agentic.eval.cases.EvalCase`): class `scoping`, split `dev`, the corrections as the
prompt's context and the lesson as the key point the agent's answer must now cover, tagged
`learning` and `regression`. It lands next to the change, in the target's eval corpus
(`case_path`): `~/.agentic/eval/` for a personal learning, the repo's `.agentic/eval/`, the
policy repo's `eval/`. Its source is the adopter's (`synthetic: false`), so it never belongs
in `library/eval/` (decision 17).

`measure` runs the case through the eval harness (`run_eval`, pydantic-evals) under two
labels, `before` (the agent's instructions as they are) and `after` (with the change), and
scores each answer with `LessonCovered`: the share of each key point's content words the
answer holds, averaged (deterministic, no judge model). The per-case regression rule is
`Measurement.regressed`: the answer with the change covers the lesson less than the answer
without it. The runners are the caller's: tests and fixtures pass scripted ones; nothing
here calls a model.

pydantic-evals is the `eval` extra of agentic-core, so it is imported only when a case is
measured.
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Protocol

from agentic.eval.cases import EvalCase
from agentic.learning.lessons import tokens

if TYPE_CHECKING:
    from agentic.eval.runner import RunCase

#: The rubric a scoping case names, per agent (the corpus has these two, M1-59); every other
#: agent's case names the requirements rubric until its class has its own truth (deferred).
RUBRICS = {"design": "design"}
DEFAULT_RUBRIC = "ba-requirements"
MAX_CONTEXT = 2000


class _Correction(Protocol):
    signal: str
    text: str


def case_path(root: str, proposal_id: str) -> str:
    """Where the regression case of a learning lands, under its target's root."""
    name = f"eval/cases/scoping/learned-{proposal_id}.yaml"
    return f".agentic/{name}" if root == "repo" else name


def regression_case(
    proposal_id: str, agent: str, lesson: str, evidence: Sequence[_Correction]
) -> EvalCase:
    """The corrected example as an M1-63 eval case for `agent`."""
    corrections = "\n".join(f"- ({e.signal}) {' '.join(e.text.split())}" for e in evidence)
    prompt = (
        f"Do the task again, as people corrected the {agent} agent's earlier work:\n"
        f"{corrections[:MAX_CONTEXT] or '- (no text recorded)'}\n"
        "Your answer must follow what they asked for."
    )
    return EvalCase.model_validate(
        {
            "id": f"learned-{proposal_id}",
            "agent": agent,
            "case_class": "scoping",
            "split": "dev",
            "source": {"repo": "learning", "synthetic": False, "licence": "LicenseRef-adopter"},
            "inputs": {"prompt": prompt},
            "expected": {"key_points": [lesson]},
            "rubric": RUBRICS.get(agent, DEFAULT_RUBRIC),
            "tags": ["learning", "regression", proposal_id],
        }
    )


def lesson_coverage(key_points: Sequence[str], output: str | None) -> float:
    """The mean share of each key point's content words that `output` holds (0..1)."""
    if not output or not key_points:
        return 0.0
    held = tokens(output)
    shares = [len(tokens(kp) & held) / len(tokens(kp)) for kp in key_points if tokens(kp)]
    return round(sum(shares) / len(shares), 4) if shares else 0.0


@dataclass(frozen=True)
class Measurement:
    case: str
    before: float
    after: float

    @property
    def regressed(self) -> bool:
        """The per-case regression rule: the change makes the lesson's coverage worse."""
        return self.after < self.before

    def as_dict(self) -> dict:
        return {**asdict(self), "regressed": self.regressed}


async def measure(case: EvalCase, *, before: "RunCase", after: "RunCase", kind: str) -> Measurement:
    """Run `case` once without the change (`before`) and once with it (`after`)."""
    from pydantic_evals.evaluators import Evaluator, EvaluatorContext

    from agentic.eval.runner import run_eval

    class LessonCovered(Evaluator):
        def evaluate(self, ctx: EvaluatorContext) -> dict[str, float]:
            record = ctx.output
            done = record is not None and record.status == "completed"
            output = record.output if done else None
            return {"lesson_covered": lesson_coverage(ctx.inputs.case.expected.key_points, output)}

    found = await run_eval(
        [case],
        {"before": before, "after": after},
        kind=kind,
        repeats=1,
        seed=0,
        evaluators=[LessonCovered()],
        name=f"learning-{case.id}",
    )
    score = {r.request.label: r.metrics.get("lesson_covered", 0.0) for r in found.runs}
    return Measurement(case=case.id, before=score["before"], after=score["after"])


__all__ = [
    "DEFAULT_RUBRIC",
    "RUBRICS",
    "Measurement",
    "case_path",
    "lesson_coverage",
    "measure",
    "regression_case",
]
