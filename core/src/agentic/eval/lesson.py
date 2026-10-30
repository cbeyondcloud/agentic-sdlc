"""The learning loop's regression metric (M3-16, DF-247): how much of a learned lesson an
answer covers.

`lesson_coverage` is the mean share of each key point's content words (stemmed, the
learning loop's own tokens) that the answer holds: deterministic, no judge model.
`LessonCovered` is the pydantic-evals evaluator `agentic.learning.regression.measure` runs
it with; it lives here because pydantic-evals is the eval harness's optional extra."""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from agentic.eval.records import RunRecord, RunRequest
from agentic.learning.lessons import tokens


def lesson_coverage(key_points: Sequence[str], output: str | None) -> float:
    """The mean share of each key point's content words that `output` holds (0..1)."""
    if not output or not key_points:
        return 0.0
    held = tokens(output)
    shares = [len(tokens(kp) & held) / len(tokens(kp)) for kp in key_points if tokens(kp)]
    return round(sum(shares) / len(shares), 4) if shares else 0.0


@dataclass
class LessonCovered(Evaluator[RunRequest, RunRecord, None]):
    """`lesson_covered`: the case's key points covered by a completed answer (0 otherwise)."""

    def evaluate(self, ctx: EvaluatorContext[RunRequest, RunRecord, None]) -> dict[str, float]:
        record = ctx.output
        done = record is not None and record.status == "completed"
        output = record.output if done else None
        return {"lesson_covered": lesson_coverage(ctx.inputs.case.expected.key_points, output)}


__all__ = ["LessonCovered", "lesson_coverage"]
