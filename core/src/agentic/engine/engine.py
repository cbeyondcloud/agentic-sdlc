"""The pipeline engine (M1-21, M1-24, M1-26; spec §6, §8.3, §8.4, §8.7).

A state machine over a `PipelineDefinition`. `advance` runs from the run's cursor
until something needs a person - a gate, a pause - or the run ends:

  * agent step  - `when` checked over the latest handoffs; the agent resolved
                  against policy; a step budget slice opened from the run's; its
                  answer validated against the kind's merged template; the final
                  handoff written into the run folder and pinned by digest
  * action step - a built-in action by name
  * loop        - its body repeated until `until` holds; an oscillating or
                  exhausted loop escalates to a gate (or fails the run)
  * gate        - the run stops until the gate ledger reports it satisfied:
                  an approval over the exact bytes it pinned (spec §7.3)

Each completed step is a checkpoint: `run.json` is saved before the next step
starts, and the budget snapshot is saved as calls settle, so a crash re-runs only
the step that was in progress and its spend still counts. A limit, a timeout, an
invalid answer, a stuck agent or a condition that cannot be evaluated pauses the
run with the reason; nothing is decided silently.

The engine imports neither `sdlc_commons` nor Zyroflo: the run ledger and the
gate ledger are protocols, wired by the CLI (and the tests).
"""

import hashlib
import json
import logging
import os
from collections.abc import Callable, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from agentic.agents.runner import (
    AgentOutcome,
    AgentRunner,
    AgentTask,
    OutputContract,
    ResumeError,
)
from agentic.agents.stuck import oscillation
from agentic.definitions import DefinitionSet
from agentic.definitions.pipeline import (
    ActionStep,
    AgentStep,
    GateStep,
    LoopSpec,
    LoopStep,
    PipelineDefinition,
)
from agentic.engine.expressions import ExpressionError, compile_condition
from agentic.engine.gates import Approve, Authorizer, Decision, Edit, GateRefused, Reject
from agentic.engine.state import ApprovalMark, Cursor, Pending, RunState, RunStore, StepRecord
from agentic.handoffs import (
    EnvelopeStamp,
    HandoffContract,
    HandoffError,
    finalise_handoff,
    parse_handoff,
)
from agentic.memory.agents_md import agents_md_knowledge, nearest_agents_md
from agentic.policy import OrgPolicy, ResolveContext, ResolvedAgent, resolve_agent
from agentic.telemetry.logs import StepLogs
from agentic.telemetry.spans import finish_step_span, step_span
from agentic.telemetry.trailers import CommitAudit, commit_message
from agentic.transport import BudgetLedger, BudgetSlice, LimitHit
from agentic.transport.budget import BudgetError

_LOG = logging.getLogger("agentic.engine")
PROMPT_HEAD = "Pipeline: {pipeline} · step: {step} · iteration: {iteration}"


class EngineError(Exception):
    """A request the engine refuses (an unknown run, a run waiting at a gate, ...)."""


class RunRecorder(Protocol):
    """The run's hash-chained ledger (`sdlc_commons.ledger.RunLedger`)."""

    @property
    def run_dir(self) -> Path: ...

    def record(self, event: str, phase: str = "", **fields: object) -> object: ...

    def write_artifact(self, rel: str, text: str) -> str: ...

    def digest(self, rel: str) -> str: ...


class GateLedger(Protocol):
    """Approval entries pinned to artefact digests (`sdlc_commons.gates.ZyrofloGates`)."""

    def record(
        self,
        gate: str,
        verdict: Literal["approve", "reject"],
        approver: str,
        artefacts: dict[str, str],
        *,
        step: str,
        notes: str = "",
        extra: dict | None = None,
    ) -> str: ...

    def state(self, gate: str) -> tuple[bool, str]: ...

    def waiting(self, gate: str, step: str) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...  # timezone-aware


@dataclass(frozen=True)
class StepContext:
    run_id: str
    step: str
    iteration: int
    agent: ResolvedAgent
    budget: BudgetSlice
    contract: OutputContract | None


class StepRunnerFactory(Protocol):
    def __call__(self, step: StepContext) -> AgentRunner: ...


class StepTrees(Protocol):
    """The run's git trees, checkpointed per step (`sdlc_commons.worktrees.StepCheckpoints`,
    M1-30): each agent step starts at the previous step's commit and ends in its own
    checkpoint; a rejection rolls the code back to the target step's base."""

    def prepare(self) -> Path: ...

    def begin(self, *, fresh: bool) -> dict[str, str]: ...

    def finish(self, message: str) -> dict[str, str]: ...

    def rollback(self, to: dict[str, str]) -> None: ...


class Engine:
    def __init__(
        self,
        *,
        definitions: DefinitionSet,
        policy: OrgPolicy,
        context: ResolveContext,
        store: RunStore,
        recorder_for: Callable[[str], RunRecorder],
        gates_for: Callable[[str], GateLedger],
        runner_factory: StepRunnerFactory,
        block_on_for: Callable[[str], frozenset[str]],
        clock: Clock,
        actions: Mapping[str, Callable[[RunState], str]] | None = None,
        run_budget_usd: Decimal | None = None,
        sandbox_label: str = "none",
        trees_for: Callable[[str], StepTrees] | None = None,
        step_logs: StepLogs | None = None,
        redact: Callable[[str], str] | None = None,
    ) -> None:
        self.definitions, self.policy, self.context = definitions, policy, context
        self.store, self.recorder_for, self.gates_for = store, recorder_for, gates_for
        self.runner_factory, self.block_on_for, self.clock = runner_factory, block_on_for, clock
        self.actions = dict(actions or {})
        self.run_budget_usd = run_budget_usd
        self.sandbox_label = sandbox_label  # recorded on every run it starts
        self.trees_for = trees_for  # None: steps get no tree (tests, pipelines without code)
        self.step_logs = step_logs  # None: no per-step log files (M1-47)
        # masks secrets in the reasons and details stored in run.json: a provider's error
        # text can echo a key (the CLI passes its redactor; None: stored as it is)
        self.redact: Callable[[str], str] = redact or _as_is

    async def start(
        self, pipeline: str, inputs: dict[str, str], starter: str, run_id: str
    ) -> RunState:
        """Create the run (its ledger, its run.json) and advance it."""
        if self.store.exists(run_id):
            raise EngineError(f"run {run_id} already exists - resume it instead")
        definition = self.definitions.pipeline(pipeline)
        _check_inputs(definition, inputs)
        now = self.clock.now()
        state = RunState(
            run_id=run_id,
            pipeline=pipeline,
            pipeline_version=definition.version,
            pipeline_digest=pipeline_digest(definition),
            policy_version=self.policy.version,
            catalog_digest=self.definitions.handoffs.digest,
            sandbox=self.sandbox_label,
            regime=definition.regime,
            starter=starter,
            status="running",
            cursor=Cursor(index=0),
            inputs=dict(inputs),
            started_at=now,
            updated_at=now,
            max_minutes=definition.max_minutes,
            budget=BudgetLedger(self.run_budget_usd).snapshot(),
        )
        with self._lock(run_id):
            self.recorder_for(run_id)  # opens the run's ledger
            self.store.save(state)
            return await _Run(self, state).go()

    async def advance(self, run_id: str) -> RunState:
        """Run from the cursor until a gate, a pause, the end or a failure."""
        with self._lock(run_id):
            return await _Run(self, self.store.load(run_id)).go()

    async def stop(self, run_id: str, who: str, reason: str) -> RunState:
        """End a run that is not finished, from any pause or gate (spec §8.4: "or stop").
        It works even when the pipeline or templates changed since the run started."""
        with self._lock(run_id):
            state = self.store.load(run_id)
            if state.status in ("completed", "failed", "stopped"):
                raise EngineError(f"run {run_id} is already {state.status}")
            state.status, state.pending = "stopped", None
            state.ended_by, state.end_reason = who, reason
            self.recorder_for(run_id).record("run_closed", "", status="stopped")
            state.updated_at = self.clock.now()
            self.store.save(state)
            return state

    @contextmanager
    def _lock(self, run_id: str):
        """One caller at a time per run: an OS lock on `run.lock` in the run folder,
        released by the OS if the process dies."""
        path = self.store.path(run_id).with_name("run.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+b")
        try:
            try:
                _lock_file(handle)
            except OSError:
                raise EngineError(
                    f"run {run_id} is busy: another process or call is working on it"
                ) from None
            try:
                yield
            finally:
                _unlock_file(handle)
        finally:
            handle.close()

    async def resume(
        self,
        run_id: str,
        *,
        extra_minutes: int = 0,
        extra_budget_usd: Decimal = Decimal(0),
        decisions: dict[str, bool | str] | None = None,
        skip: bool = False,
        who: str | None = None,
        self_approval: Callable[[str, str], str | None] | None = None,
    ) -> RunState:
        """Continue a paused run: grant more time or budget, or answer pending tool
        calls (`decisions`), then advance. A run waiting at a gate is decided instead.
        More budget raises the run's limit only: it is refused for a run without one, and
        for a step that reached its own budget (more run budget would not raise that).
        `skip` routes around a condition that cannot be evaluated: the step it guards is
        skipped, or a loop whose `until` it is ends as it stands (DF-59). A skip names
        the person (`who`), who is recorded; in the regulated regime it may not be the
        person who started the run (`self_approval`, as at a gate; default: same name)."""
        with self._lock(run_id):
            run = _Run(self, self.store.load(run_id))
            if skip:
                return await run.skip(who, self_approval)
            return await run.resume(extra_minutes, extra_budget_usd, decisions)

    async def decide(
        self,
        run_id: str,
        decision: Decision,
        *,
        authorizer: Authorizer,
        self_approval: Callable[[str, str], str | None],
    ) -> RunState:
        """A person's decision at the gate the run waits on, then advance (M1-22).
        `self_approval(approver, starter)` returns why a person may not approve their
        own run, or None (`sdlc_commons.gates.self_approval_refusal`)."""
        with self._lock(run_id):
            run = _Run(self, self.store.load(run_id))
            return await run.decide(decision, authorizer, self_approval)


def _check_inputs(pipeline: PipelineDefinition, inputs: Mapping[str, str]) -> None:
    """A pipeline that declares `inputs:` gets exactly those: none it does not know, and
    every one not marked `required: false` (DF-54). A pipeline declaring none takes any."""
    declared = pipeline.inputs
    if not declared:
        return
    if unknown := sorted(set(inputs) - set(declared)):
        raise EngineError(
            f"unknown input {', '.join(unknown)} for pipeline {pipeline.name} "
            f"(it takes: {', '.join(sorted(declared))})"
        )
    required = {name for name, spec in declared.items() if spec.get("required", True) is not False}
    if missing := sorted(required - set(inputs)):
        raise EngineError(f"missing input {', '.join(missing)} for pipeline {pipeline.name}")


def pipeline_digest(pipeline: PipelineDefinition) -> str:
    canonical = json.dumps(
        pipeline.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _Run:
    """One run being advanced: its state, budget, ledgers and pipeline."""

    def __init__(self, engine: Engine, state: RunState) -> None:
        self.e, self.state = engine, state
        self.pipeline = engine.definitions.pipeline(state.pipeline)
        if pipeline_digest(self.pipeline) != state.pipeline_digest:
            raise EngineError(
                f"pipeline {state.pipeline} changed since run {state.run_id} started "
                "(a run uses only the version it was started with)"
            )
        if engine.definitions.handoffs.digest != state.catalog_digest:
            raise EngineError(
                f"the handoff templates changed since run {state.run_id} started "
                "(a run validates against the templates it was started with)"
            )
        self.recorder = engine.recorder_for(state.run_id)
        self.gates = engine.gates_for(state.run_id)
        self.catalog = engine.definitions.handoffs
        self.budget = (
            BudgetLedger.restore(state.budget)
            if state.budget
            else BudgetLedger(engine.run_budget_usd)
        )
        self.budget.on_change = self._save_budget
        self._tree_handle: StepTrees | None = None
        self._work: Path | None = None  # where the run's agents work (from prepare)
        self._marks: tuple | None = None  # (step, iteration, base, commit) of the attempt
        self._children: dict[str, Decimal] = {}  # the settled step's spend per child

    # ── the walk ─────────────────────────────────────────────────────────────

    async def go(self) -> RunState:
        state = self.state
        if state.status in ("completed", "failed", "stopped") or state.pending is not None:
            return state
        state.status = "running"
        while state.cursor.index < len(self.pipeline.steps):
            if self._timed_out():
                return self._pause("run-timeout", self._timeout_reason())
            step = self.pipeline.steps[state.cursor.index]
            if isinstance(step, GateStep):
                if not self._pass_gate(step):
                    return state
            elif isinstance(step, LoopStep):
                if not await self._loop(step.loop):
                    return state
            else:
                if not await self._step(step, 1):
                    return state
                self._next()
        return self._close("completed")

    async def resume(
        self, extra_minutes: int, extra_budget: Decimal, decisions: dict | None
    ) -> RunState:
        state, pending = self.state, self.state.pending
        if pending is not None and pending.kind == "gate":
            raise EngineError(
                f"run {state.run_id} waits at {pending.id}: approve, reject or edit it"
            )
        if pending is not None and pending.kind == "approval":
            if decisions is None or set(decisions) != set(pending.calls):
                raise EngineError(
                    f"run {state.run_id} waits for a decision on each of these tool calls, "
                    f"and only these: {pending.calls}"
                )
        if extra_budget > 0:
            if pending is not None and pending.id == "step-budget":
                raise EngineError(
                    f"step {pending.step} of run {state.run_id} reached its own budget (the "
                    "agent's limits.budget_usd): more run budget does not raise it"
                )
            if self.budget.run.limit_usd is None:
                raise EngineError(
                    f"run {state.run_id} has no budget limit: there is no run budget to raise"
                )
        state.extra_minutes += extra_minutes
        if extra_budget > 0:
            self.budget.run.raise_limit(self.budget.run.limit_usd + extra_budget)
        if pending is not None and pending.kind == "approval":
            step = self._step_at_cursor()
            iteration = state.cursor.loop_iteration or 1
            state.status = "running"  # the pause stays recorded until the step settles
            resumed = (Path(pending.state_path), decisions, pending.state_digest)
            try:
                finished = await self._step(step, iteration, resume=resumed)
            except ResumeError as error:
                state.status = "paused"
                self.save()
                raise EngineError(f"the paused tool calls cannot be resumed: {error}") from error
            if state.pending is pending:
                self._clear_pending()
            if not finished:
                return state
            self._after_step()
            return await self.go()
        self._clear_pending()
        return await self.go()

    async def skip(self, who: str | None, self_approval) -> RunState:
        state, pending = self.state, self.state.pending
        if pending is None or pending.kind != "pause" or pending.id != "condition":
            raise EngineError(
                f"run {state.run_id}: only a condition pause can be skipped "
                f"(it waits on {pending.id if pending else 'nothing'})"
            )
        person = (who or "").strip()
        if not person:
            raise EngineError("a skip names the person who decided it")
        if state.regime == "regulated":
            if refusal := (self_approval or _same_person)(person, state.starter):
                raise EngineError(
                    f"four-eyes: {refusal} (someone else must skip a step of your own run)"
                )
        top = self.pipeline.steps[state.cursor.index]
        skipped = top.loop.id if isinstance(top, LoopStep) and pending.step == top.loop.id else None
        skipped = skipped or self._step_at_cursor().id
        # the person's decision, in the approval ledger like a gate's (events carry no
        # names): who skipped what, and why the step could not be decided by its condition
        self.gates.record(
            f"skip-{skipped}",
            "approve",
            person,
            {},
            step=skipped,
            notes=f"skipped: its condition could not be evaluated ({pending.reason})",
            extra={"regime": state.regime, "action": "skip"},
        )
        self._clear_pending()
        if isinstance(top, LoopStep) and pending.step == top.loop.id:
            self.recorder.record("step_finished", top.loop.id, outcome="skipped")
            self._next()  # the loop ends as it stands
        else:
            iteration = state.cursor.loop_iteration or 1
            self._history(
                skipped,
                iteration,
                "skipped",
                started=self.e.clock.now(),
                detail=f"skipped by {person}",
            )
            self.recorder.record("step_finished", skipped, outcome="skipped")
            self._after_step()
        return await self.go()

    def _clear_pending(self) -> None:
        """The person answered: the wait ends, and its time is not run time (I6)."""
        pending = self.state.pending
        if pending is not None:
            waited = (self.e.clock.now() - pending.since).total_seconds()
            self.state.waited_seconds += max(waited, 0)
        self.state.pending = None
        if self.state.status in ("paused", "awaiting_gate"):
            self.state.status = "running"

    def _step_at_cursor(self) -> AgentStep | ActionStep:
        step = self.pipeline.steps[self.state.cursor.index]
        if isinstance(step, LoopStep):
            return step.loop.steps[self.state.cursor.loop_index]
        return step

    def _after_step(self) -> None:
        """Move past the step at the cursor (inside a loop: to the next body step)."""
        step = self.pipeline.steps[self.state.cursor.index]
        if isinstance(step, LoopStep):
            self.state.cursor.loop_index += 1
            self.save()
        else:
            self._next()

    def _next(self) -> None:
        self.state.cursor = Cursor(index=self.state.cursor.index + 1)
        self.save()

    # ── gates ────────────────────────────────────────────────────────────────

    def _pass_gate(self, gate: GateStep) -> bool:
        satisfied, why = self.gates.state(gate.gate)
        covers = set(self.state.approved.get(gate.gate, {})) == set(self.state.unpinned)
        if satisfied and covers:
            self.state.unpinned = {}
            self._next()
            return True
        reason = f"waiting for approval by {gate.approver_role}"
        if satisfied and not covers:
            reason += " (new work since its last approval)"
        elif why and "no entry" not in why:
            reason += f" ({why})"
        self._wait(gate.gate, reason, gate.gate, dict(self.state.unpinned))
        return False

    def _wait(self, gate: str, reason: str, step: str, artefacts: dict[str, str]) -> None:
        self.state.pending = Pending(
            kind="gate",
            id=gate,
            reason=self.e.redact(reason),
            artefacts=artefacts,
            since=self.e.clock.now(),
            step=step,
        )
        self.state.status = "awaiting_gate"
        self.gates.waiting(gate, step)
        self.save()

    # ── decisions ────────────────────────────────────────────────────────────

    async def decide(self, decision: Decision, authorizer: Authorizer, self_approval) -> RunState:
        state, pending = self.state, self.state.pending
        if pending is None or pending.kind != "gate":
            raise GateRefused(f"run {state.run_id} is not waiting at a gate")
        if decision.gate != pending.id:
            raise GateRefused(
                f"run {state.run_id} waits at {pending.id}, not {decision.gate} - "
                "a decision names the gate the person saw"
            )
        gate = self._gate_step(pending.id)
        role = gate.approver_role if gate is not None else "approver"
        approver = decision.approver.strip()
        if not approver:
            raise GateRefused("a decision names the person who took it")
        if state.regime != "solo" or approver.lower() != state.starter.lower():
            if not authorizer.holds(approver, role):
                raise GateRefused(
                    f"{approver} does not hold the role {role!r} that {pending.id} needs"
                )
        if isinstance(decision, Reject):  # sending work back is never four-eyes bound
            return await self._reject(decision, gate)
        if state.regime == "regulated" or (gate is not None and gate.four_eyes):
            if refusal := self_approval(approver, state.starter):
                raise GateRefused(f"four-eyes: {refusal} (someone else must approve your own run)")
        edited = self._edit(decision) if isinstance(decision, Edit) else None
        if edited is None and pending.id == "invalid-output":
            raise GateRefused(
                "an invalid answer cannot be approved as it is: edit it into "
                "a valid handoff, or reject it"
            )
        return await self._approve(decision, edited)

    def _gate_step(self, gate_id: str) -> GateStep | None:
        return next(
            (s for s in self.pipeline.steps if isinstance(s, GateStep) and s.gate == gate_id), None
        )

    def _edit(self, decision: Edit) -> tuple[str, str, str]:
        """Validate the edited text, keep the original, write the edit and repin it.
        Returns (path, digest, kind) of the edited file."""
        pending = self.state.pending
        if decision.path not in pending.artefacts:
            raise GateRefused(
                f"{decision.path} is not one of the files this gate pins: "
                f"{', '.join(sorted(pending.artefacts))}"
            )
        text = decision.text.replace("\r\n", "\n").replace("\r", "\n")
        block_on = self.e.block_on_for(self.state.regime)
        try:
            if pending.id == "invalid-output":
                agent = self._resolved(self._step_at_cursor())
                stamp = EnvelopeStamp(
                    self.state.run_id, pending.step, _name(agent), self.e.clock.now()
                )
                text, handoff = finalise_handoff(
                    text,
                    schema=self.catalog.schema(agent.produces),
                    stamp=stamp,
                    block_on=block_on,
                    person=True,
                )
            else:
                handoff = parse_handoff(text, self.catalog, block_on=block_on)
        except HandoffError as error:
            raise GateRefused(
                "the edit is not a valid handoff:\n" + "\n".join(error.errors)
            ) from error
        folder = self.recorder.run_dir / Path(decision.path).parent
        count = len(list(folder.glob(f"{Path(decision.path).name}.edited-*.md"))) + 1
        rel = f"{decision.path}.edited-{count}.md"
        digest = self.recorder.write_artifact(rel, text)
        del pending.artefacts[decision.path]
        pending.artefacts[rel] = digest
        kind = handoff.envelope.kind
        if pending.id == "invalid-output" or self.state.latest.get(kind) == decision.path:
            self.state.latest[kind] = rel
        self.state.unpinned.pop(decision.path, None)
        self.state.unpinned[rel] = digest
        self.state.digests[rel] = digest
        self.save()
        return rel, digest, kind

    async def _approve(self, decision: Approve | Edit, edited: tuple | None) -> RunState:
        state, pending = self.state, self.state.pending
        changed, invalid = [], []
        for rel, digest in list(pending.artefacts.items()):
            try:
                current = self.recorder.digest(rel)
            except OSError:
                current = None
            if current == digest:
                continue
            changed.append(rel)
            problems = self._validate(rel) if current is not None else ["the file is missing"]
            if problems:
                invalid.append(f"{rel}: " + "; ".join(problems))
            else:
                pending.artefacts[rel] = current  # the next decision is about these bytes
                self.state.digests[rel] = current
        if changed:
            self.save()
            message = f"changed since the gate opened: {', '.join(changed)}"
            if invalid:
                message += " - not a valid handoff:\n" + "\n".join(invalid)
            raise GateRefused(message + " - review them and decide again")
        self.gates.record(
            pending.id,
            "approve",
            decision.approver,
            dict(pending.artefacts),
            step=pending.step or pending.id,
            notes=decision.notes,
            extra={"regime": state.regime},
        )
        mark = ApprovalMark(gate=pending.id, approver=decision.approver, at=self.e.clock.now())
        state.approvals.append(mark)
        if self._gate_step(pending.id) is not None:
            state.approved[pending.id] = dict(pending.artefacts)
        self._clear_pending()
        if pending.id == "blocked-output":
            self._after_step()  # the person accepted (or edited) the blocked handoff
        elif pending.id == "invalid-output" and edited is not None:
            rel, digest, _ = edited
            step, iteration = pending.step, state.cursor.loop_iteration or 1
            self._history(
                step,
                iteration,
                "completed",
                started=self.e.clock.now(),
                handoff=rel,
                digest=digest,
                detail="edited by a person",
            )
            self.recorder.record(
                "step_finished", step, outcome="completed", artifacts={rel: digest}
            )
            state.feedback.pop(step, None)
            self._after_step()
        elif pending.id.endswith("-exhausted") and self._gate_step(pending.id) is None:
            self._next()  # a person accepted the loop's result as it stands
        else:
            self.save()
        return await self.go()

    async def _reject(self, decision: Reject, gate: GateStep | None) -> RunState:
        state, pending = self.state, self.state.pending
        if pending.id in ("invalid-output", "blocked-output"):
            target, cursor = pending.step, state.cursor
        elif gate is None:  # an exhausted loop: run it again from its first step
            loop = self.pipeline.steps[state.cursor.index].loop
            target = loop.steps[0].id
            cursor = Cursor(index=state.cursor.index, loop_iteration=1, loop_index=0)
        else:
            target, cursor = self._reject_target(gate)
        self._rollback_for(target, cursor.loop_iteration or 1)  # the code goes back too
        self.gates.record(
            pending.id,
            "reject",
            decision.approver,
            dict(pending.artefacts),
            step=pending.step or pending.id,
            notes=decision.feedback,
            extra={"regime": state.regime},
        )
        if cursor.loop_iteration == 1 and cursor is not state.cursor:
            state.loop_pass_start = len(state.history)  # a new pass of the loop (I9)
        state.feedback[target] = decision.feedback
        self._clear_pending()
        state.cursor = cursor
        self.save()
        return await self.go()

    def _reject_target(self, gate: GateStep) -> tuple[str, Cursor]:
        """Where a rejection sends the work: `on_reject`, else the step before the gate."""
        index = self.pipeline.steps.index(gate)
        if gate.on_reject:
            for position, step in enumerate(self.pipeline.steps):
                if isinstance(step, AgentStep | ActionStep) and step.id == gate.on_reject:
                    return step.id, Cursor(index=position)
                if isinstance(step, LoopStep):
                    for inner, body in enumerate(step.loop.steps):
                        if body.id == gate.on_reject:
                            return body.id, Cursor(
                                index=position, loop_iteration=1, loop_index=inner
                            )
            raise GateRefused(f"{gate.gate}: on_reject names no step: {gate.on_reject}")
        for position in range(index - 1, -1, -1):
            step = self.pipeline.steps[position]
            if isinstance(step, AgentStep | ActionStep):
                return step.id, Cursor(index=position)
            if isinstance(step, LoopStep):
                return step.loop.steps[0].id, Cursor(index=position, loop_iteration=1)
        raise GateRefused(f"{gate.gate}: there is no step before the gate to send the work back to")

    # ── the run's trees (M1-30) ─────────────────────────────────────────────

    def _trees(self) -> StepTrees | None:
        """The run's trees, made (or found) once per process."""
        if self.e.trees_for is None:
            return None
        if self._tree_handle is None:
            handle = self.e.trees_for(self.state.run_id)
            self._work = handle.prepare()
            self._tree_handle = handle
        return self._tree_handle

    def _knowledge(self) -> str:
        """The nearest AGENTS.md of the run's tree, wrapped as data, or "" (M1-46)."""
        if self._work is None:
            return ""
        path = nearest_agents_md(self._work, self._work)
        try:
            return agents_md_knowledge(path, self._work) if path else ""
        except OSError:
            return ""  # unreadable: the step runs without it rather than failing

    def _tree_begin(self, *, fresh: bool) -> dict[str, str] | None:
        trees = self._trees()
        return trees.begin(fresh=fresh) if trees else None

    def _tree_finish(self, message: str) -> dict[str, str] | None:
        trees = self._trees()
        return trees.finish(message) if trees else None

    def _rollback_for(self, step: str, iteration: int) -> None:
        """Roll the trees back to where the newest attempt of `step` at `iteration`
        started; nothing to do when there are no trees or no such attempt."""
        if self.e.trees_for is None:
            return
        record = next(
            (
                r
                for r in reversed(self.state.history)
                if r.step == step and r.iteration == iteration and r.base is not None
            ),
            None,
        )
        if record is None:
            return
        try:
            self._trees().rollback(record.base)
        except Exception as error:
            raise GateRefused(f"the code cannot be rolled back for {step}: {error}") from error

    def _resolved(self, step: AgentStep) -> ResolvedAgent:
        agent, violations = resolve_agent(
            self.e.definitions.agent(step.agent), self.e.policy, self.e.context
        )
        if agent is None:
            raise GateRefused("; ".join(f"{v.field}: {v.reason}" for v in violations))
        return agent

    # ── loops ────────────────────────────────────────────────────────────────

    async def _loop(self, loop: LoopSpec) -> bool:
        cursor = self.state.cursor
        if cursor.loop_iteration == 0:
            cursor.loop_iteration, cursor.loop_index = 1, 0
            self.state.loop_pass_start = len(self.state.history)
        while True:
            while cursor.loop_index < len(loop.steps):
                if self._timed_out():
                    self._pause("run-timeout", self._timeout_reason())
                    return False
                if not await self._step(loop.steps[cursor.loop_index], cursor.loop_iteration):
                    return False
                cursor.loop_index += 1
                self.save()
            try:
                done = compile_condition(loop.until).evaluate(self._context())
            except ExpressionError as error:
                self._pause("condition", str(error), step=loop.id)
                return False
            except _Unusable as problem:
                self._pause(problem.pause_id, problem.reason, step=loop.id)
                return False
            if done:
                self._next()
                return True
            repeating = self._oscillating(loop)
            if repeating or cursor.loop_iteration >= loop.max_iterations:
                reason = (
                    f"the loop is oscillating: '{repeating}' repeats an earlier iteration"
                    if repeating
                    else f"max_iterations {loop.max_iterations} reached and {loop.until!r} "
                    "is still false"
                )
                if loop.on_exhausted == "fail":
                    self._close("failed")
                else:
                    self._wait(f"{loop.id}-exhausted", reason, loop.id, dict(self.state.unpinned))
                return False
            self.recorder.record(
                "loop_back", loop.id, **{"from": loop.steps[-1].id, "to": loop.steps[0].id}
            )
            cursor.loop_iteration, cursor.loop_index = cursor.loop_iteration + 1, 0
            self.save()

    def _oscillating(self, loop: LoopSpec) -> str | None:
        """An open finding of this iteration that repeats one of the last two (§8.5)."""
        body = {step.id for step in loop.steps}
        by_iteration: dict[int, list[str]] = {}
        for record in self.state.history[self.state.loop_pass_start :]:
            if record.step in body and record.outcome == "completed" and record.handoff:
                issues = self._open_issues(record.handoff)
                if issues is not None:
                    by_iteration.setdefault(record.iteration, []).extend(issues)
        current = self.state.cursor.loop_iteration
        earlier = [issue for i in (current - 2, current - 1) for issue in by_iteration.get(i, [])]
        for issue in by_iteration.get(current, []):
            if earlier and oscillation(earlier, issue, window=len(earlier)):
                return issue
        return None

    def _open_issues(self, rel: str) -> list[str] | None:
        try:
            handoff = self._read(rel)
        except (OSError, HandoffError):
            return None
        if handoff.envelope.kind != "review":
            return None
        return [f.issue for f in handoff.data.findings if f.status == "open"]  # type: ignore[attr-defined]

    # ── one step ─────────────────────────────────────────────────────────────

    async def _step(
        self, step: AgentStep | ActionStep, iteration: int, *, resume: tuple | None = None
    ) -> bool:
        """Run one step. True when it completed or was skipped (the caller moves on)."""
        if resume is None and step.when:
            try:
                wanted = compile_condition(step.when).evaluate(self._context())
            except ExpressionError as error:
                self._pause("condition", str(error), step=step.id)
                return False
            except _Unusable as problem:
                self._pause(problem.pause_id, problem.reason, step=step.id)
                return False
            if not wanted:
                now = self.e.clock.now()
                self._history(step.id, iteration, "skipped", started=now)
                self.recorder.record("step_finished", step.id, outcome="skipped")
                return True  # saved with the cursor move
        with self._step_log(step.id, iteration):
            _LOG.info("step started", extra={"fields": self._log_fields(step, iteration)})
            seen = len(self.state.history)
            try:
                if isinstance(step, ActionStep):
                    done = self._action(step, iteration)
                else:
                    done = await self._agent(step, iteration, resume)
            except Exception:
                _LOG.exception("step failed")
                raise
            self._log_end(seen)
            return done

    # ── the step's log (M1-47) ───────────────────────────────────────────────

    def _step_log(self, step: str, iteration: int):
        logs = self.e.step_logs
        if logs is None:
            return nullcontext()
        return logs.step(self.recorder.run_dir, self.state.run_id, step, iteration)

    def _log_fields(self, step: AgentStep | ActionStep, iteration: int) -> dict:
        fields: dict = {"attempt": self._attempt(step.id, iteration)}
        if isinstance(step, ActionStep):
            fields["action"] = step.action
        else:
            try:
                definition = self.e.definitions.agent(step.agent)
                fields["agent"] = f"{definition.name}@{definition.version}"
            except Exception:  # an unknown agent: the step itself reports it
                fields["agent"] = step.agent
        return fields

    def _log_end(self, seen: int) -> None:
        """How the step ended: its newest history record, or the pause it left."""
        records = self.state.history[seen:]
        if records:
            last = records[-1]
            outcome, fields = last.outcome, {"cost_usd": last.cost_usd}
            if last.handoff:
                fields["handoff"] = last.handoff
            if last.detail:
                fields["detail"] = last.detail
        else:
            pending = self.state.pending
            outcome, fields = f"paused-{pending.id}" if pending else "none", {}
            if pending:
                fields["detail"] = pending.reason
        level = {"completed": logging.INFO, "skipped": logging.INFO, "failed": logging.ERROR}
        _LOG.log(
            level.get(outcome, logging.WARNING),
            "step ended",
            extra={"fields": {"outcome": outcome, **fields}},
        )

    def _action(self, step: ActionStep, iteration: int) -> bool:
        started = self.e.clock.now()
        action = self.e.actions.get(step.action)
        if action is None:
            self._history(
                step.id,
                iteration,
                "failed",
                started=started,
                detail=f"unknown action {step.action!r}",
            )
            self._close("failed")
            return False
        self.recorder.record("step_started", step.id, attempt=self._attempt(step.id, iteration))
        try:
            detail = action(self.state)
        except Exception as error:
            reason = f"{type(error).__name__}: {error}"
            self._history(step.id, iteration, "failed", started=started, detail=reason)
            self.recorder.record("step_finished", step.id, outcome="error")
            self._pause("step-error", reason, step=step.id)
            return False
        self._history(step.id, iteration, "completed", started=started, detail=str(detail))
        self.recorder.record("step_finished", step.id, outcome="completed")
        return True  # saved with the cursor move

    async def _agent(self, step: AgentStep, iteration: int, resume: tuple | None) -> bool:
        agent, violations = resolve_agent(
            self.e.definitions.agent(step.agent), self.e.policy, self.e.context
        )
        if agent is None:
            reason = "; ".join(f"{v.field}: {v.reason}" for v in violations)
            self._pause("policy", reason, step=step.id)
            return False
        name = f"{step.id}-{iteration}"
        try:
            base = self._tree_begin(fresh=resume is None)  # a resumed step keeps its work
        except Exception as error:  # a git failure: the step does not run
            self._pause("worktree", f"{type(error).__name__}: {error}", step=step.id)
            return False
        self._marks = (step.id, iteration, base, None)
        try:
            budget = self.budget.open_slice(name) or self.budget.run.open(
                name, "step", _usd(agent.limits.budget_usd)
            )
        except LimitHit as hit:
            self._pause(_budget_pause(hit), str(hit), step=step.id)
            return False
        contract = None
        if agent.produces:
            contract = HandoffContract(
                schema=self.catalog.schema(agent.produces),
                run_id=self.state.run_id,
                step=step.id,
                agent=f"{agent.definition.name}@{agent.definition.version}",
                block_on=self.e.block_on_for(self.state.regime),
                clock=self.e.clock.now,
            )
        started = self.e.clock.now()
        attempt = self._attempt(step.id, iteration)
        if resume is None:
            self.recorder.record("step_started", step.id, attempt=attempt)
        try:
            # inside the try: a runner that cannot be built (no sandbox, no tool service)
            # pauses the run and closes the step's slice, like any other step failure (DF-69)
            runner = self.e.runner_factory(
                StepContext(self.state.run_id, step.id, iteration, agent, budget, contract)
            )
            with step_span(self.state.run_id, step.id, iteration, agent) as span:
                if resume is None:
                    task = AgentTask(
                        instructions=agent.definition.instructions,
                        inputs=self._inputs(agent.consumes),
                        prompt=self._prompt(step.id, iteration),
                        knowledge=self._knowledge(),
                    )
                    outcome = await runner.run(task)
                else:
                    outcome = await runner.resume(*resume)
                finish_step_span(span, outcome)
        except _Unusable as problem:
            self._close_slice(budget)
            self._pause(problem.pause_id, problem.reason, step=step.id)
            return False
        except ResumeError:
            self._close_slice(budget)
            raise
        except LimitHit as hit:
            cost = budget.total_spent()
            self._close_slice(budget)
            self._children = _children(budget)
            self._history(
                step.id,
                iteration,
                "stopped",
                started=started,
                cost=cost,
                children=self._children,
                detail=str(hit),
            )
            self.recorder.record("step_finished", step.id, outcome="budget", cost=self._usd(cost))
            pause = _budget_pause(hit, budget, _usd(agent.limits.budget_usd))
            self._pause(pause, str(hit), step=step.id)
            return False
        except Exception as error:  # a provider, network or tool failure: a person decides
            cost = budget.total_spent()
            self._close_slice(budget)
            self._children = _children(budget)
            reason = f"{type(error).__name__}: {error}"
            self._history(
                step.id,
                iteration,
                "failed",
                started=started,
                cost=cost,
                children=self._children,
                detail=reason,
            )
            self.recorder.record("step_finished", step.id, outcome="error", cost=self._usd(cost))
            self._pause("step-error", reason, step=step.id)
            return False
        cost = budget.total_spent()
        closing = self._close_slice(budget)
        self._children = _children(budget)
        # Only an answer is checkpointed: a stopped attempt's partial work is discarded by
        # the retry's fresh begin, so a later reject returns to the step's real base (I7);
        # a tool-approval pause keeps working in the tree.
        if outcome.status in ("completed", "invalid"):
            message = commit_message(
                f"agentic: {step.id} #{iteration} ({self.state.run_id})",
                self._audit(step.id, iteration, agent),
            )
            try:
                commit = self._tree_finish(message)
            except Exception as error:
                self._pause("worktree", f"{type(error).__name__}: {error}", step=step.id)
                return False
            self._marks = (step.id, iteration, base, commit)
        return self._outcome(step.id, iteration, attempt, outcome, started, cost, closing, agent)

    def _outcome(
        self,
        step: str,
        iteration: int,
        attempt: int,
        outcome: AgentOutcome,
        started: datetime,
        cost: Decimal,
        closing: str,
        agent: ResolvedAgent,
    ) -> bool:
        base = f"handoffs/{step}-{iteration}" + (f"-attempt{attempt}" if attempt > 1 else "")
        usd = self._usd(cost)
        if outcome.status == "completed":
            rel = f"{base}.md"
            digest = self.recorder.write_artifact(rel, outcome.output or "")
            self.state.digests[rel] = digest
            if agent.produces:
                self.state.latest[agent.produces] = rel
            self.state.unpinned[rel] = digest
            self.state.feedback.pop(step, None)
            self._history(
                step,
                iteration,
                "completed",
                started=started,
                handoff=rel,
                digest=digest,
                cost=cost,
                children=self._children,
                detail=closing,
            )
            self.recorder.record(
                "step_finished", step, outcome="completed", artifacts={rel: digest}, cost=usd
            )
            if blocked := self._blocked(rel):
                self._wait(
                    "blocked-output",
                    f"the agent reports it is blocked: {blocked}",
                    step,
                    {rel: digest},
                )
                return False
            return True  # saved with the cursor move
        if outcome.status == "invalid":
            rel = f"{base}.invalid.md"
            digest = self.recorder.write_artifact(rel, outcome.output or "")
            self.state.digests[rel] = digest
            reason = "the answer is not a valid handoff after one retry: " + "; ".join(
                outcome.errors
            )
            self._history(
                step,
                iteration,
                "invalid",
                started=started,
                handoff=rel,
                digest=digest,
                cost=cost,
                children=self._children,
                detail=closing,
            )
            self.recorder.record("step_finished", step, outcome="invalid", cost=usd)
            self._wait("invalid-output", reason, step, {rel: digest})
            return False
        if outcome.status == "paused":
            self._history(
                step, iteration, "paused", started=started, cost=cost, children=self._children
            )
            self.recorder.record("step_finished", step, outcome="paused", cost=usd)
            self.state.pending = Pending(
                kind="approval",
                id="tool-approval",
                step=step,
                since=self.e.clock.now(),
                reason=f"{len(outcome.pending)} tool call(s) need a person's decision",
                state_path=str(outcome.state_path),
                state_digest=outcome.state_digest,
                calls=list(outcome.pending),
            )
            self.state.status = "paused"
            self.save()
            return False
        # stopped: budget, turns, time (the step's time limit), stuck
        reason = outcome.detail or f"the step stopped: {outcome.stopped}"
        self._history(
            step,
            iteration,
            "stopped",
            started=started,
            cost=cost,
            children=self._children,
            detail=reason,
        )
        self.recorder.record("step_finished", step, outcome=f"stopped-{outcome.stopped}", cost=usd)
        self._pause(f"step-{outcome.stopped}", reason, step=step)
        return False

    # ── helpers ──────────────────────────────────────────────────────────────

    def _context(self) -> dict[str, dict]:
        """The front-matter fields of the newest handoff of each kind, for conditions.
        A handoff that changed on disk, or no longer parses, pauses the run (I7)."""
        context = {}
        for kind, rel in self.state.latest.items():
            self._check_unchanged(rel)
            try:
                handoff = self._read(rel)
            except (OSError, HandoffError) as error:
                raise _Unusable("handoff-invalid", f"{rel} cannot be read: {error}") from error
            fields = handoff.data.model_dump(mode="json")
            context[kind] = {**fields, "status": handoff.envelope.status}
        return context

    def _read(self, rel: str):
        text = (self.recorder.run_dir / rel).read_text(encoding="utf-8")
        return parse_handoff(text.replace("\r\n", "\n").replace("\r", "\n"), self.catalog)

    def _check_unchanged(self, rel: str) -> None:
        recorded = self.state.digests.get(rel)
        if recorded is None:
            return
        try:
            current = self.recorder.digest(rel)
        except OSError:
            current = None
        if current != recorded:
            raise _Unusable(
                "handoff-changed",
                f"{rel} changed on disk after it was recorded - restore it, or stop the run",
            )

    def _validate(self, rel: str) -> list[str]:
        """Why the file at `rel` is not a valid handoff for this run, or []."""
        if rel.endswith(".invalid.md"):
            return []  # an invalid answer is settled by an edit, never approved as it is
        try:
            text = (self.recorder.run_dir / rel).read_text(encoding="utf-8")
            parse_handoff(
                text.replace("\r\n", "\n"),
                self.catalog,
                block_on=self.e.block_on_for(self.state.regime),
            )
        except HandoffError as error:
            return list(error.errors)
        except OSError as error:
            return [str(error)]
        return []

    def _blocked(self, rel: str) -> str | None:
        """The summary of a handoff whose envelope says `blocked`, else None."""
        try:
            handoff = self._read(rel)
        except (OSError, HandoffError):
            return None
        if handoff.envelope.status != "blocked":
            return None
        return getattr(handoff.data, "summary", "") or "no summary given"

    def _inputs(self, consumes: list[str]) -> dict[str, str]:
        inputs = {}
        for kind in consumes:
            if kind in self.state.latest:
                rel = self.state.latest[kind]
                self._check_unchanged(rel)
                inputs[kind] = (self.recorder.run_dir / rel).read_text(encoding="utf-8")
        return inputs

    def _prompt(self, step: str, iteration: int) -> str:
        parts = [PROMPT_HEAD.format(pipeline=self.state.pipeline, step=step, iteration=iteration)]
        if self.state.inputs:
            lines = "\n".join(f"{key}: {value}" for key, value in self.state.inputs.items())
            parts.append(f"Request:\n{lines}")
        if feedback := self.state.feedback.get(step):
            parts.append(f"Feedback from the last review of this step:\n{feedback}")
        return "\n\n".join(parts)

    def _audit(self, step: str, iteration: int, agent: ResolvedAgent) -> CommitAudit:
        """The trailers of a step's checkpoint commit: who, what, which approval, and the
        handoffs the step consumed (M1-45)."""
        approval = self.state.approvals[-1] if self.state.approvals else None
        consumed = [self.state.latest[k] for k in agent.consumes if k in self.state.latest]
        return CommitAudit(
            run_id=self.state.run_id,
            step=step,
            iteration=iteration,
            agent=_name(agent),
            triggered_by=self.state.starter,
            approved_by=approval.approver if approval else None,
            approval_gate=approval.gate if approval else None,
            handoffs=tuple(consumed),
        )

    def _attempt(self, step: str, iteration: int) -> int:
        tried = [r for r in self.state.history if r.step == step and r.iteration == iteration]
        return len([r for r in tried if r.outcome != "skipped"]) + 1

    def _history(
        self,
        step: str,
        iteration: int,
        outcome: str,
        *,
        started: datetime,
        handoff: str | None = None,
        digest: str | None = None,
        cost: Decimal = Decimal(0),
        detail: str = "",
        children: dict[str, Decimal] | None = None,
    ) -> None:
        marks = self._marks if self._marks and self._marks[:2] == (step, iteration) else None
        self.state.history.append(
            StepRecord(
                step=step,
                iteration=iteration,
                outcome=outcome,
                handoff=handoff,
                digest=digest,
                base=marks[2] if marks else None,
                commit=marks[3] if marks else None,
                sandbox=self.e.sandbox_label,  # per step: a resume may run elsewhere (I6)
                cost_usd=str(cost),
                children_usd={name: str(usd) for name, usd in (children or {}).items()},
                started_at=started,
                ended_at=self.e.clock.now(),
                detail=self.e.redact(detail),
            )
        )

    def _usd(self, cost: Decimal) -> dict:
        """The `cost` of a step_finished event: the step's spend and, when it spawned
        children, each child's (M1-57)."""
        usd: dict = {"usd": str(cost)}
        if self._children:
            usd["children"] = {name: str(v) for name, v in self._children.items()}
        return usd

    def _close_slice(self, budget: BudgetSlice) -> str:
        try:
            budget.close()
        except BudgetError as error:  # calls still in flight (DF-22): kept open, recorded
            return str(error)
        return ""

    def _timed_out(self) -> bool:
        if self.state.max_minutes is None:
            return False
        limit = timedelta(minutes=self.state.max_minutes + self.state.extra_minutes)
        active = self.e.clock.now() - self.state.started_at
        return active - timedelta(seconds=self.state.waited_seconds) > limit

    def _timeout_reason(self) -> str:
        minutes = self.state.max_minutes + self.state.extra_minutes  # type: ignore[operator]
        return f"run time limit of {minutes} min reached"

    def _pause(self, pause_id: str, reason: str, *, step: str | None = None) -> RunState:
        self.state.pending = Pending(
            kind="pause",
            id=pause_id,
            reason=self.e.redact(reason),
            since=self.e.clock.now(),
            step=step,
        )
        self.state.status = "paused"
        self.save()
        return self.state

    def _close(self, status: Literal["completed", "failed", "stopped"]) -> RunState:
        self.state.status, self.state.pending = status, None
        self.recorder.record("run_closed", "", status=status)
        self.save()
        return self.state

    def _save_budget(self) -> None:
        self.state.budget = self.budget.snapshot()
        self.save()

    def save(self) -> None:
        self.state.updated_at = self.e.clock.now()
        self.e.store.save(self.state)


class _Unusable(Exception):
    """A recorded handoff that cannot be used as it is: the run pauses for a person."""

    def __init__(self, pause_id: str, reason: str) -> None:
        super().__init__(reason)
        self.pause_id, self.reason = pause_id, reason


def _lock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _as_is(text: str) -> str:
    return text


def _same_person(who: str, starter: str) -> str | None:
    return "you started this run" if who.lower() == starter.lower() else None


def _budget_pause(
    hit: LimitHit, budget: BudgetSlice | None = None, own: Decimal | None = None
) -> str:
    """`budget` when the run's limit is what stopped the step (more run budget helps),
    `step-budget` when the agent's own limit is (it does not; spec §8.4). A step's slice
    is carved as the smaller of its agent's limit and what the run has left, so a step
    slice smaller than `own` was capped by the run."""
    if hit.scope == "run":
        return "budget"
    capped = budget is not None and budget.limit_usd is not None
    if capped and (own is None or budget.limit_usd < own):
        return "budget"
    return "step-budget"


def _children(budget: BudgetSlice) -> dict[str, Decimal]:
    return dict(budget.child_spend)


def _name(agent: ResolvedAgent) -> str:
    return f"{agent.definition.name}@{agent.definition.version}"


def _usd(amount: float | None) -> Decimal | None:
    """A policy amount (a float in the definitions) as Decimal, through its text (DF-26)."""
    return None if amount is None else Decimal(str(amount))


__all__ = [
    "Clock",
    "Engine",
    "EngineError",
    "GateLedger",
    "RunRecorder",
    "StepContext",
    "StepRunnerFactory",
    "pipeline_digest",
]
