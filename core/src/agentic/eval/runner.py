"""Running an evaluation (M1-63, spike S10 "Repeats and nondeterminism").

`run_eval` runs every case under every label (X, or X and Y) k times, with production
settings - we measure the system people will use, not a temperature-0 artefact. Runs
are interleaved per case, in an order drawn from the recorded seed, so provider drift
during a run hits both variants alike. pydantic-evals is the engine: each run is one
`Case`, the metrics are its `Evaluator`s, and it bounds the concurrency.

Every run is kept. A run that raises is recorded as `error` (unscored, named in the
report), never dropped. Once the spend reaches the budget cap, the remaining runs are
`skipped` and the evaluation is INCOMPLETE - the cap also bounds a runaway agent loop.

`AgentCaseRunner` is how a real agent runs one case: the resolved agent on
`PydanticAIRunner` with its kind's handoff contract, its model built per run (so the
model's calls can be routed to the run's cassette and metered into the run's cost log).
"""

import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from pydantic_ai.models import Model
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator

from agentic.agents.metering import CostLog
from agentic.agents.runner import AgentTask, PydanticAIRunner
from agentic.eval.cases import EvalCase
from agentic.eval.records import RunRecord, RunRequest
from agentic.handoffs import HandoffContract, HandoffSchema
from agentic.policy import ResolvedAgent
from agentic.security.untrusted import untrusted
from agentic.transport import BudgetLedger, BudgetSlice, LimitHit

RunCase = Callable[[RunRequest], Awaitable[RunRecord]]
ModelFor = Callable[[RunRequest, CostLog, BudgetSlice], Model]


@dataclass
class EvalRunResult:
    request: RunRequest
    record: RunRecord
    metrics: dict[str, float]
    evaluator_errors: list[str] = field(default_factory=list)


@dataclass
class RunSet:
    runs: list[EvalRunResult]
    repeats: int
    seed: int
    budget_usd: float | None
    budget_hit: bool
    spent_usd: float


def plan_runs(
    cases: Sequence[EvalCase], labels: Sequence[str], *, repeats: int, seed: int, kind: str
) -> list[RunRequest]:
    """Every (case, repeat, label), cases in id order, labels shuffled per (case, repeat)."""
    rng = random.Random(seed)
    order = []
    for case in sorted(cases, key=lambda c: c.id):
        for repeat in range(repeats):
            shuffled = list(labels)
            rng.shuffle(shuffled)
            order += [
                RunRequest(case, label, repeat, kind, f"{case.id}.{label}.{repeat}")
                for label in shuffled
            ]
    return order


async def run_eval(
    cases: Sequence[EvalCase],
    runners: Mapping[str, RunCase],
    *,
    kind: str,
    repeats: int,
    seed: int,
    evaluators: Sequence[Evaluator] = (),
    budget_usd: float | None = None,
    max_concurrency: int = 1,
    name: str = "eval",
) -> RunSet:
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    requests = plan_runs(cases, list(runners), repeats=repeats, seed=seed, kind=kind)
    spent = {"usd": 0.0, "hit": False}

    async def task(request: RunRequest) -> RunRecord:
        if budget_usd is not None and spent["usd"] >= budget_usd:
            spent["hit"] = True
            return RunRecord(status="skipped", error="the evaluation's budget cap was reached")
        started = time.perf_counter()
        try:
            record = await runners[request.label](request)
        except Exception as error:  # noqa: BLE001 - recorded, never lost
            record = RunRecord(status="error", error=f"{type(error).__name__}: {error}")
            record.duration_s = time.perf_counter() - started
        spent["usd"] += record.cost_usd or 0.0
        if record.error == BUDGET_STOP:
            spent["hit"] = True
        return record

    dataset = Dataset[RunRequest, RunRecord, None](
        name=name,
        cases=[Case(name=r.run_id, inputs=r) for r in requests],
        evaluators=list(evaluators),
    )
    report = await dataset.evaluate(
        task, name=name, max_concurrency=max_concurrency, progress=False
    )
    by_id = {c.name: c for c in report.cases}
    runs = []
    for request in requests:
        done = by_id[request.run_id]
        metrics = {k: float(v.value) for k, v in done.scores.items()}
        errors = [f"{f.name}: {f.error_message}" for f in done.evaluator_failures]
        runs.append(EvalRunResult(request, done.output, metrics, errors))
    return RunSet(runs, repeats, seed, budget_usd, bool(spent["hit"]), spent["usd"])


BUDGET_STOP = "stopped: the evaluation's budget cap was reached"


class AgentCaseRunner:
    """Runs one case with a resolved agent; a `RunCase` for `run_eval`."""

    def __init__(
        self,
        resolved: ResolvedAgent,
        model_for: ModelFor,
        *,
        schema: HandoffSchema,
        state_root: Path,
        budget: BudgetLedger,
        step: str = "eval",
    ) -> None:
        self.resolved, self.model_for, self.schema = resolved, model_for, schema
        self.state_root, self.budget, self.step = Path(state_root), budget, step

    async def __call__(self, request: RunRequest) -> RunRecord:
        definition = self.resolved.definition
        agent = f"{definition.name}@{definition.version}"
        limit = Decimal(str(self.resolved.limits.budget_usd))
        try:
            slice_ = self.budget.run.open(request.run_id, "step", limit)
        except LimitHit:
            return RunRecord(status="skipped", error=BUDGET_STOP)
        costs = CostLog()
        started = time.perf_counter()
        try:
            runner = PydanticAIRunner(
                self.resolved,
                self.model_for(request, costs, slice_),
                budget=slice_,
                state_dir=self.state_root / request.run_id,
                costs=costs,
                run_id=request.run_id,
                contract=HandoffContract(self.schema, request.run_id, self.step, agent),
            )
            outcome = await runner.run(task_for(request.case, definition.instructions))
        except LimitHit:
            return _record(RunRecord(status="stopped", error=BUDGET_STOP), costs, started)
        finally:
            slice_.close()
        record = RunRecord(status=outcome.status, output=outcome.output, errors=outcome.errors)
        if outcome.stopped == "budget":
            record.error = BUDGET_STOP
        return _record(record, costs, started)


def task_for(case: EvalCase, instructions: str) -> AgentTask:
    """What the agent is given: its instructions, the case's handoffs (fenced as data by
    the runner), and the prompt with any diff fenced as untrusted data."""
    prompt = case.inputs.prompt.strip()
    if case.inputs.diff:
        prompt += "\n\n" + untrusted(case.inputs.diff, source="diff")
    return AgentTask(instructions=instructions, inputs=dict(case.inputs.handoffs), prompt=prompt)


def _record(record: RunRecord, costs: CostLog, started: float) -> RunRecord:
    calls = [cost for cost, _ in costs.calls]
    record.requests = len(calls)
    record.model_ids = [c.model or "unknown" for c in calls]
    if calls and all(c.cost_usd is not None for c in calls):
        record.cost_usd = float(sum(c.cost_usd for c in calls))  # type: ignore[misc]
    record.cost_estimated = any(c.source == "estimated" for c in calls)
    if calls and all(c.tokens_in is not None and c.tokens_out is not None for c in calls):
        record.tokens_in = sum(c.tokens_in for c in calls)  # type: ignore[misc]
        record.tokens_out = sum(c.tokens_out for c in calls)  # type: ignore[misc]
    record.duration_s = time.perf_counter() - started
    return record
