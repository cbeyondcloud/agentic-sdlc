"""What one eval run is asked to do and what it produced (M1-63).

A run is one agent attempt at one case, under one opaque label (X or Y), for one
repeat. Its record keeps what the metrics need: the outcome status and document, the
cost as TokenLean reported it (None when unmeasured, never 0), the resolved model ids,
and - filled in by the judge evaluator - the rubric result.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from agentic.eval.cases import EvalCase

RunStatus = Literal["completed", "stopped", "paused", "invalid", "error", "skipped"]


@dataclass(frozen=True)
class RunRequest:
    case: EvalCase
    label: str  # X or Y: the variant behind it is sealed until scoring
    repeat: int  # 0-based
    kind: str  # the handoff kind the agent produces
    run_id: str  # unique and stable: "<case>.<label>.<repeat>"

    @property
    def cassette_key(self) -> str:
        """Where a recorded run's model calls live (case, label, repeat)."""
        return f"{self.case.id}/{self.label}/{self.repeat}"


@dataclass
class RunRecord:
    status: RunStatus
    output: str | None = None  # the final document (or the last invalid answer)
    errors: list[str] = field(default_factory=list)  # why an invalid answer was refused
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None  # None: not measured (never 0 for "unknown")
    cost_estimated: bool = False  # some of the cost was estimated, not reported
    model_ids: list[str] = field(default_factory=list)  # resolved model per call, in order
    requests: int = 0
    duration_s: float = 0.0
    error: str | None = None  # an infrastructure failure (the run is unscored)
    tests: dict[str, bool] | None = None  # test id -> passed, when the run executed tests
    judge: Any = None  # the RubricResult, attached by the rubric evaluator
    judged: str | None = None  # the scrubbed text the judge saw (humans see the same)
    judge_stopped: bool = False  # the judge's budget share ran out: unscored, INCOMPLETE
