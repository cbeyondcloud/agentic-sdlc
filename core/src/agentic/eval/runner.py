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

A case with a workspace (a dev task) runs in a fresh copy of it (DF-241): `prepare`
copies the workspace and gives the agent its tools on that tree - the CLI builds them
with the run's guard and sandbox - and once the agent has answered, the case's
fail-to-pass and pass-to-pass tests run on the tree (through the workspace's sandbox),
so `tests_pass` is measured. The corpus itself is never changed.
"""

import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath

from pydantic_ai import Tool
from pydantic_ai.models import Model
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator

from agentic.agents.metering import CostLog
from agentic.agents.runner import AgentTask, PydanticAIRunner
from agentic.agents.tools.context import Sandbox
from agentic.eval.cases import EvalCase
from agentic.eval.devtask import copy_workspace, run_tests
from agentic.eval.records import RunRecord, RunRequest
from agentic.handoffs import HandoffContract, HandoffSchema
from agentic.policy import ResolvedAgent
from agentic.security.untrusted import untrusted
from agentic.transport import BudgetLedger, BudgetSlice, LimitHit

RunCase = Callable[[RunRequest], Awaitable[RunRecord]]
ModelFor = Callable[[RunRequest, CostLog, BudgetSlice], Model]


@dataclass(frozen=True)
class Workspace:
    """Where one dev-task run works: a fresh tree, the agent's tools on it, and the
    sandbox its tests run in (None: a plain process with the safe environment)."""

    tree: Path
    tools: Sequence[Tool] = ()
    sandbox: Sandbox | None = None


PrepareWorkspace = Callable[[RunRequest, Path], Workspace]  # (request, workspace source)
SCORED = ("completed", "invalid", "stopped")  # the agent answered or failed on its own


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
    judge_stopped = any(r.record.judge_stopped for r in runs)
    return RunSet(
        runs, repeats, seed, budget_usd, bool(spent["hit"]) or judge_stopped, spent["usd"]
    )


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
        clock: Callable[[], datetime] | None = None,  # replay: a fixed time, so output is stable
        corpus_root: Path | None = None,  # where cases' workspaces are (dev tasks)
        prepare: PrepareWorkspace | None = None,  # None: a copy with no tools
    ) -> None:
        self.resolved, self.model_for, self.schema = resolved, model_for, schema
        self.state_root, self.budget, self.step = Path(state_root), budget, step
        self.clock = clock
        self.corpus_root = None if corpus_root is None else Path(corpus_root)
        self.prepare = prepare or self._copy_only

    def _copy_only(self, request: RunRequest, source: Path) -> Workspace:
        return Workspace(copy_workspace(source, self.state_root / request.run_id / "tree"))

    async def __call__(self, request: RunRequest) -> RunRecord:
        definition = self.resolved.definition
        agent = f"{definition.name}@{definition.version}"
        own = self.resolved.limits.budget_usd
        limit = None if own is None else Decimal(str(own))
        try:
            slice_ = self.budget.run.open(request.run_id, "step", limit)
        except LimitHit:
            return RunRecord(status="skipped", error=BUDGET_STOP)
        granted = slice_.limit_usd
        costs = CostLog()
        started = time.perf_counter()
        workspace = None
        try:
            if request.case.inputs.workspace is not None and self.corpus_root is not None:
                source = self.corpus_root / PurePosixPath(request.case.inputs.workspace)
                workspace = self.prepare(request, source)
            runner = PydanticAIRunner(
                self.resolved,
                self.model_for(request, costs, slice_),
                budget=slice_,
                state_dir=self.state_root / request.run_id,
                tools=workspace.tools if workspace is not None else (),
                costs=costs,
                run_id=request.run_id,
                contract=self._contract(request.run_id, agent),
            )
            task = task_for(request.case, definition.instructions, workspace=workspace is not None)
            outcome = await runner.run(task)
        except LimitHit as hit:
            if hit_eval_cap(hit, granted=granted, own=limit):
                record = RunRecord(status="stopped", error=BUDGET_STOP)
            else:  # the agent spent its own per-run budget: its failure, and scored
                record = RunRecord(status="stopped", errors=[str(hit)])
            return await self._tested(_record(record, costs, started), request, workspace)
        except Exception as error:  # noqa: BLE001 - an infrastructure failure, its spend kept
            record = RunRecord(status="error", error=f"{type(error).__name__}: {error}")
            return _record(record, costs, started)
        finally:
            slice_.close()
        record = RunRecord(status=outcome.status, output=outcome.output, errors=outcome.errors)
        return await self._tested(_record(record, costs, started), request, workspace)

    async def _tested(
        self, record: RunRecord, request: RunRequest, workspace: Workspace | None
    ) -> RunRecord:
        """The record with the case's tests run on the agent's tree (a dev task the agent
        answered or failed on its own). A test that cannot run leaves tests_pass
        unmeasured, never failed by the harness."""
        tests = request.case.expected.tests
        unscored = record.status not in SCORED or record.error is not None  # e.g. the cap
        if workspace is None or tests is None or unscored:
            return record
        try:
            record.tests = await run_tests(
                workspace.tree,
                [*tests.fail_to_pass, *tests.pass_to_pass],
                sandbox=workspace.sandbox,
            )
        except (OSError, ValueError) as error:
            record.errors.append(f"the case's tests could not run: {error}")
        return record

    def _contract(self, run_id: str, agent: str) -> HandoffContract:
        if self.clock is None:
            return HandoffContract(self.schema, run_id, self.step, agent)
        return HandoffContract(self.schema, run_id, self.step, agent, clock=self.clock)


def hit_eval_cap(hit: LimitHit, *, granted: Decimal | None, own: Decimal | None) -> bool:
    """Whether a budget stop was the evaluation's cap (the run is unscored, the eval
    INCOMPLETE) rather than the agent spending its own per-run limit (an agent failure,
    scored). A run slice smaller than the agent's limit was cut short by the cap."""
    if hit.scope == "run":
        return True
    return granted is not None and (own is None or granted < own)


WORKSPACE_NOTE = (
    "The files of this task are in your working tree. Read, change and test them with "
    "your tools, then report what you did."
)


def task_for(case: EvalCase, instructions: str, *, workspace: bool = False) -> AgentTask:
    """What the agent is given: its instructions, the case's handoffs (fenced as data by
    the runner), and the prompt with any diff fenced as untrusted data; for a dev task
    run in its workspace, where its files are."""
    prompt = case.inputs.prompt.strip()
    if case.inputs.diff:
        prompt += "\n\n" + untrusted(case.inputs.diff, source="diff")
    if workspace:
        prompt += "\n\n" + WORKSPACE_NOTE
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
