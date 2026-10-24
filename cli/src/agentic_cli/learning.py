"""The learning loop in a workspace (M3-11..M3-13): capture, analyse, deliver, propose.

  * `FeedbackCapture` - the engine's feedback port, and what `agentic learn capture-pr`
    feeds: each record is attributed by the rules, given a heuristic lesson, screened by
    the publish scan (org mode) and chained into the run's feedback journal
    (`sdlc_commons.feedback`, beside the run ledger); then the journal is delivered;
  * `deliver` - the journal goes to the control plane through `FeedbackSink` with
    `agentic.telemetry.sinks.forward` and its cursor (`sinks/control-plane-feedback.json`
    in the run folder): at least once, in order, never twice. A failure is a warning and
    the journal stays queued for the next delivery (or `agentic learn sync`);
  * `analyse` - the Learning Analyst (the library agent, through the native backend and
    the gateway) for records it has not seen: its lesson, and its attribution where the
    rules did not decide;
  * `refresh_proposals` - standalone only: the repository's runs' lessons are clustered and
    new proposals kept in `.agentic/local/learning/` (in org mode the control plane
    clusters the team's records on ingest).

Nothing here changes a skill, a memory file or an agent: proposals are created only (D29).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from agentic.handoffs import parse_handoff
from agentic.handoffs.kinds import DevReportData, ReviewData
from agentic.learning.analyst import AnalystContract, LearningAnalyst, analyse
from agentic.learning.attribution import RunAgents, rules
from agentic.learning.clustering import Lesson, Proposal, propose
from agentic.learning.lessons import heuristic_lesson
from agentic.learning.records import (
    ANALYSED,
    CAPTURED,
    CORRECTED,
    Attribution,
    FeedbackRecord,
    FeedbackState,
    Learner,
    analysed_fields,
    captured_fields,
    corrected_fields,
    fold,
)
from agentic.learning.store import LocalLearningStore
from agentic.policy import resolve_agent
from agentic.telemetry.sinks import forward
from agentic.transport import BudgetLedger
from agentic_cli import control_plane
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from sdlc_commons.feedback import FeedbackJournal
from sdlc_commons.ledger import run_ids

if TYPE_CHECKING:
    from agentic.engine.engine import StepContext
    from agentic_cli.runtime import Runtime

_LOG = logging.getLogger("agentic.learning")
ANALYST = "learning-analyst"
LEARNING = Path(".agentic") / "local" / "learning"
ANALYST_HINT = (
    "add the library's agents/learning-analyst.md and the `learning-analyst` agent type "
    "(templates/policy-repo/policy/agent-types.yaml) to the policy repo"
)


def _now() -> datetime:
    return datetime.now(UTC)


def journal(rt: Runtime, run_id: str) -> FeedbackJournal:
    return FeedbackJournal(rt.ws.repo, run_id)


def states(rt: Runtime, run_id: str) -> dict[str, FeedbackState]:
    return fold(journal(rt, run_id).entries())


def all_states(rt: Runtime) -> dict[str, FeedbackState]:
    found: dict[str, FeedbackState] = {}
    for run_id in run_ids(rt.ws.repo):
        found.update(states(rt, run_id))
    return found


def run_agents(rt: Runtime, run_id: str) -> tuple[RunAgents, bool | None, list, str | None]:
    """(the run's agent per handoff kind, whether its developer reported no tests, the
    findings of its newest review, the reviewer as name@version)."""
    try:
        state = rt.store.load(run_id)
    except Exception:  # an unknown or unreadable run: the library's names
        return RunAgents(), None, [], None
    by_kind: dict[str, str] = {}
    tdd_skipped, findings, reviewer = None, [], None
    for kind, rel in state.latest.items():
        try:
            text = (rt.store.runs_root / run_id / rel).read_text(encoding="utf-8")
            handoff = parse_handoff(text.replace("\r\n", "\n"), rt.ws.definitions().handoffs)
        except Exception:  # a handoff that no longer parses names no agent
            continue
        by_kind[kind] = handoff.envelope.agent.split("@", 1)[0]
        data = handoff.data
        if isinstance(data, DevReportData):
            counts = data.tests_run
            tdd_skipped = counts is None or counts.passed + counts.failed == 0
        if isinstance(data, ReviewData):
            findings, reviewer = list(data.findings), handoff.envelope.agent
    return RunAgents(by_kind), tdd_skipped, findings, reviewer


def feedback_sink(rt: Runtime) -> control_plane.FeedbackSink | None:
    """The control plane's feedback sink, in org mode with one signed in."""
    for sink in rt.sinks():
        if isinstance(sink, control_plane.ControlPlaneSink):
            return control_plane.FeedbackSink(sink.client, guard=sink.guard)
    return None


class FeedbackCapture:
    """Records corrections into the run's journal (the engine's `feedback` port)."""

    def __init__(
        self, rt: Runtime, warn: Callable[[str], None] | None = None, *, deferred: bool = False
    ) -> None:
        """`deferred` (the engine's port): the journal is delivered when the command ends,
        after the run's own events (`Runtime.deliver_feedback`), never inside a decision."""
        self.rt, self.deferred = rt, deferred
        self.warn = warn or (lambda message: _LOG.warning("%s", message))

    def __call__(self, record: FeedbackRecord) -> None:
        self.capture(record.run_id, [record])

    def _screen(self, event: str, fields: dict) -> dict:
        if not self.rt.ws.settings.get("org"):
            return fields
        return control_plane.publish_guard(self.rt.ws.policy).screen(event, fields)

    def capture(
        self, run_id: str, records: Iterable[FeedbackRecord], agents: RunAgents | None = None
    ) -> list[FeedbackRecord]:
        """Chain each new record; returns the ones recorded (a repeat is not)."""
        if agents is None:
            agents = run_agents(self.rt, run_id)[0]
        log, landed = journal(self.rt, run_id), []
        for record in records:
            fields = captured_fields(record, rules(record, agents), heuristic_lesson(record))
            if log.append(CAPTURED, self._screen(CAPTURED, fields), row_id=record.id, clock=_now):
                landed.append(record)
        self.after(run_id)
        return landed

    def after(self, run_id: str) -> None:
        if self.deferred:
            self.rt.feedback_runs.add(run_id)
        else:
            deliver(self.rt, run_id, self.warn)
        try:
            refresh_proposals(self.rt)
        except Exception as error:  # proposals are recomputed next time
            self.warn(f"learning proposals were not updated: {error}")

    def correct(self, feedback_id: str, agents: list[str], reason: str, by: str) -> str:
        """A person's correction, standalone: appended to the record's run journal."""
        for run_id in run_ids(self.rt.ws.repo):
            if feedback_id in states(self.rt, run_id):
                attribution = Attribution(
                    learners=tuple(Learner(agent=a, confidence=1.0) for a in agents),
                    method="human",
                    reason=reason,
                )
                fields = corrected_fields(feedback_id, attribution, by)
                row = f"{feedback_id}:corrected:{len(journal(self.rt, run_id).entries())}"
                journal(self.rt, run_id).append(
                    CORRECTED, self._screen(CORRECTED, fields), row_id=row, clock=_now
                )
                self.after(run_id)
                return run_id
        raise CliError(
            f"there is no feedback record {feedback_id} in this repository's runs",
            ExitCode.INVALID,
            hint="`agentic learn feedback` lists them",
        )


def deliver(rt: Runtime, run_id: str, warn: Callable[[str], None]) -> int:
    """Send the run's journal entries the control plane has not had; returns how many."""
    sink = feedback_sink(rt)
    if sink is None:
        return 0
    log = journal(rt, run_id)
    try:
        return forward(run_id, log.entries(), sink, log.run_dir / "sinks" / f"{sink.name}.json")
    except Exception as error:  # queued in the journal; the next delivery sends it
        warn(f"feedback for run {run_id} was not sent to the control plane yet: {error}")
        return 0


def lessons_of(found: dict[str, FeedbackState], team: str | None) -> list[Lesson]:
    return [
        Lesson(
            feedback_id=fid,
            agent=agent,
            text=state.lesson or heuristic_lesson(state.record),
            at=state.record.given_at,
            source=state.record.source,
            actor=state.record.actor,
            team=team,
            repo=state.record.repo,
        )
        for fid, state in found.items()
        if state.attribution is not None
        for agent in state.attribution.agents
    ]


def refresh_proposals(rt: Runtime, now: datetime | None = None) -> list[Proposal]:
    """Standalone: cluster the repository's lessons; new and grown proposals are kept."""
    if rt.ws.settings.get("org"):
        return []
    team = rt.ws.team or "standalone"
    store = LocalLearningStore(rt.ws.repo / LEARNING)
    settings = rt.ws.policy().learning_settings(team)
    changed = propose(
        lessons_of(all_states(rt), team), settings, now or _now(), existing=store.proposals()
    )
    store.save(changed)
    return changed


# ── the Learning Analyst ────────────────────────────────────────────────────


def make_backend(rt: Runtime):
    """The native backend the analyst runs on (the gateway; tests replace the transport)."""
    from agentic_cli.runtime import _native_backend

    return _native_backend(rt, rt.gateway())


def learning_analyst(rt: Runtime, run_id: str) -> LearningAnalyst:
    from agentic.engine.engine import StepContext

    try:
        definition = rt.ws.definitions().agent(ANALYST)
    except Exception:
        raise CliError(
            "this workspace has no Learning Analyst agent", ExitCode.REFUSED, hint=ANALYST_HINT
        ) from None
    resolved, violations = resolve_agent(definition, rt.ws.policy(), rt.ws.context())
    if resolved is None:
        why = "; ".join(f"{v.field}: {v.reason}" for v in violations)
        raise CliError(f"the Learning Analyst is not allowed here: {why}", ExitCode.REFUSED)
    backend = make_backend(rt)
    budget_usd = Decimal(str(resolved.limits.budget_usd or 1))
    folder = rt.store.runs_root / run_id / "agents" / ANALYST

    def runner_for(contract: AnalystContract):
        budget = BudgetLedger(budget_usd).run.open(ANALYST, ANALYST, budget_usd)
        step: StepContext = StepContext(
            run_id=run_id,
            step=ANALYST,
            iteration=1,
            agent=resolved,
            budget=budget,
            contract=contract,
        )
        return backend.runner(step, tool_context=None, state_dir=folder, spawner=None)

    return LearningAnalyst(runner_for, definition.instructions)


def analyse_run(rt: Runtime, run_id: str, capture: FeedbackCapture) -> list[str]:
    """Run the analyst on the run's records it has not seen; returns their ids."""
    found = [s for s in states(rt, run_id).values() if not s.analysed and s.corrected_by is None]
    if not found:
        return []
    analyst = learning_analyst(rt, run_id)
    agents = run_agents(rt, run_id)[0]
    log, done = journal(rt, run_id), []
    for state in found:
        attribution, lesson = asyncio.run(analyse(state.record, agents, analyst))
        decided = attribution if state.attribution is None else None
        fields = analysed_fields(state.record.id, decided, lesson)
        row = f"{state.record.id}:analysed"
        if log.append(ANALYSED, capture._screen(ANALYSED, fields), row_id=row, clock=_now):
            done.append(state.record.id)
    capture.after(run_id)
    return done


__all__ = [
    "FeedbackCapture",
    "all_states",
    "analyse_run",
    "deliver",
    "feedback_sink",
    "journal",
    "learning_analyst",
    "lessons_of",
    "refresh_proposals",
    "run_agents",
    "states",
]
