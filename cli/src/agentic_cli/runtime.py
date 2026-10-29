"""The engine for one workspace (M1-54, DF-47): what `run`, `resume` and the gate verbs
drive, wired from core and commons.

  * run state     - `RunStore` under `.agentic/local/runs`, next to Zyroflo's ledger;
  * run ledger    - `RunLedger`, opened for a new run and resumed for an existing one,
                    behind a `ForwardingRecorder`; in org mode with a control plane its
                    events are also delivered there by `ControlPlaneSink`, on a background
                    thread (M2-20, DF-304), the ledger itself being the offline queue;
  * gates         - `ZyrofloGates`, blocking severities by regime (`regime_block_on`);
  * steps         - `StepRunners` over the native backend: every model call through the
                    configured gateway (TokenLean), metered against the step's budget,
                    secret-scanned under org policy; tools from `StepWiring` (run trees,
                    guard, sandbox, installed skills, code intelligence); and over the
                    agent-CLI backends - claude-code, codex, opencode - each locked down
                    by `agent_backends` (M2-60);
  * step logs     - `StepLogs` through the commons redactor, the gateway key included;
                    the same redactor masks the pause reasons and step details the
                    engine stores in run.json (a provider's error can echo the key);
  * traces        - the local JSONL span exporter into the run folder, when the
                    telemetry extra is installed (`AGENTIC_TRACES=off` turns it off).

Preflight is given every configured backend; a step on an agent-CLI backend this machine
cannot run (a missing extra or CLI, codex without srt) is refused before any spend, with
what to install (`agent_backends.problems`).
"""

import importlib.util
import os
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx2

from agentic.agents.models import GatewaySettings
from agentic.auth.credentials import TOKENLEAN_KEY, CredentialError, Credentials
from agentic.codeintel import index as codeintel_index
from agentic.definitions import PipelineDefinition, UnknownDefinitionError
from agentic.definitions.pipeline import ActionStep, AgentStep, GateStep, LoopStep
from agentic.engine.engine import Engine, EngineError, StepContext
from agentic.engine.factory import StepRunners
from agentic.engine.gates import Authorizer, StandaloneAuthorizer
from agentic.engine.preflight import PreflightReport, Problem, preflight
from agentic.engine.state import RunState, RunStore
from agentic.killswitch import KillSwitch, NoKillSwitch
from agentic.orgapi import Me
from agentic.orgapi.rbac import OrgAuthorizer
from agentic.policy import ResolvedAgent
from agentic.sandbox.readiness import Readiness, SandboxDecision, decide_sandbox, readiness
from agentic.skills.catalog import CatalogError, load_catalog
from agentic.skills.install import log_record, sync_skills
from agentic.skills.loader import InstalledSkills
from agentic.skills.personal import MAX_PERSONAL_SKILLS, PersonalContext, load_personal
from agentic.skills.select import pipeline_skills
from agentic.telemetry.logs import StepLogs
from agentic.telemetry.sinks import EventSink, ForwardingRecorder
from agentic.transport import CallIdentity
from agentic_cli import agent_backends, control_plane
from agentic_cli import killswitch as killswitch_port
from agentic_cli.codeintel import codeintel_for as codeintel_factory
from agentic_cli.codeintel import codeintel_settings, codeintel_warning
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.heartbeat import Heartbeat
from agentic_cli.learning import FeedbackCapture
from agentic_cli.skill_activity import SkillActivity, StampingRecorder
from agentic_cli.standalone import PLACEHOLDER
from agentic_cli.step_extras import StepExtras
from agentic_cli.wiring import StepWiring, production_scanner
from agentic_cli.workspace import RUNS, Workspace
from sdlc_commons.gates import ZyrofloGates, regime_block_on
from sdlc_commons.ledger import LedgerError, RunIdentity, RunLedger
from sdlc_commons.redaction import redactor

#: Seconds a command waits, after the engine stops, for run events still being delivered to
#: the control plane; what is left stays queued in the ledger for `agentic sync` (R20).
SINK_GRACE_S = 5.0

#: The backends this CLI builds: native and the agent CLIs (M2-60, decision 89).
CONFIGURED_BACKENDS: tuple[str, ...] = agent_backends.CONFIGURED_BACKENDS


def _publish(kind: str | None) -> Callable[["Runtime"], Callable]:
    def make(rt: "Runtime") -> Callable:
        from agentic_cli.publishing import publish_action  # it imports this module's peers

        return publish_action(rt.ws, rt.store.runs_root, kind)

    return make


#: The publishing actions (M1-58), by name -> the provider kind they insist on: push the
#: run's branch and open its pull request - on a GitHub host (`github.open_pr`, the
#: spec's name), or on whatever host `origin` is (`open_pr`). Preflight checks their host
#: and token before a run starts.
PUBLISH_ACTIONS: dict[str, str | None] = {"github.open_pr": "github", "open_pr": None}
#: Built-in actions, by the name a pipeline step gives: name -> (Runtime -> action).
ACTIONS: dict[str, Callable[["Runtime"], Callable]] = {
    name: _publish(kind) for name, kind in PUBLISH_ACTIONS.items()
}
SKILLS = Path(".agentic") / "local" / "skills"
STATE = Path(".agentic") / "local" / "state"
#: Charged for a call whose cost the gateway did not report and whose model has no price.
FALLBACK_ESTIMATE_USD = Decimal("0.05")
#: Why four-eyes is a record and not yet a control in standalone mode (review F7).
UNVERIFIED = (
    "approvals are not authenticated before org sign-in (M2-03): the approver's name "
    "comes from AGENTIC_USER or git user.email and roles are not checked, so four-eyes "
    "compares names only"
)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def installed_skills_root(ws: Workspace) -> Path:
    """Where `agentic init` installs the repo's skills (git-ignored)."""
    return ws.repo / SKILLS


def network_transport() -> httpx2.AsyncBaseTransport | None:
    """The innermost transport for model calls; None = the network (tests replace it)."""
    return None


def machine_readiness() -> Readiness:
    """This machine's sandbox prerequisites (tests replace it)."""
    return readiness()


def new_run_id(pipeline: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"{_slug(pipeline)[:60]}-{stamp}-{secrets.token_hex(2)}"


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class _Unavailable:
    """Skills when the policy repo has no catalog: every skill is missing."""

    def __init__(self, why: str) -> None:
        self.why = why

    def unavailable(self, name: str) -> str | None:
        return self.why

    def preloads_all(self, agent: ResolvedAgent) -> bool:
        return False


class _Catalogued:
    """Skills for a dry run: what the catalog has would be installed by the run."""

    def __init__(self, library: InstalledSkills) -> None:
        self.library = library

    def unavailable(self, name: str) -> str | None:
        return None if name in self.library.catalog.skills else "not in the skills catalog"

    def preloads_all(self, agent: ResolvedAgent) -> bool:
        return self.library.preloads_all(agent)


class Runtime:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws
        self.store = RunStore(ws.runs_root)
        self._ledgers: dict[str, RunLedger] = {}
        self._gates: dict[str, ZyrofloGates] = {}
        self._pipelines: dict[str, str] = {}
        self._library: InstalledSkills | None | bool = False  # False: not looked up yet
        self._gateway_key: str | None = None  # the key `redact` masks once it is known
        self._restored: list[str] = []  # skills the last sync restored (a local edit)
        self._recorders: dict[str, ForwardingRecorder] = {}
        self._stamped: dict[str, StampingRecorder] = {}
        self._next_ids: dict[str, dict[str, str]] = {}  # run -> ids for its next event
        # skill and memory activations, stamped on each step_finished in the ledger (M2-45)
        self.activity = SkillActivity()
        self._personal: PersonalContext | None = None
        self._sinks: list[EventSink] | None = None
        self.sink_problem: str | None = None  # why runs are not reported (a warning)
        self._killswitch: KillSwitch | None = None
        self._me: Me | None | bool = False  # False: not asked yet
        # runs whose feedback journal grew during this command (M3-11): delivered at its end
        self.feedback_runs: set[str] = set()

    @property
    def user(self) -> str:
        """The person acting. In org mode with a control plane, the signed-in person
        (`/v1/me`): the starter, the approver and whoever skips or stops a step are then one
        identity, which four-eyes compares (review F1). Otherwise AGENTIC_USER or git
        user.email. CliError (exit 2) when the control plane cannot say who you are."""
        me = self.me()
        return me.user.email if me is not None else self.ws.user()

    def known_user(self) -> str:
        """`user`, or - when the control plane cannot answer - the signed-in person last
        verified, else the workspace's person: for records that need no check (`stop`)."""
        try:
            return self.user
        except CliError:
            record = control_plane.login_record(self.ws.home) or {}
            return str(record.get("user") or "") or self.ws.user()

    def me(self, why: str = "in org mode the control plane says who you are") -> Me | None:
        """Who the control plane says you are (fetched once), in org mode with one; None
        otherwise. CliError (exit 2, starting with `why`) when it is configured but cannot
        answer."""
        if self._me is False:
            try:
                client = self.control_plane()
                if client is None:
                    self._me = None
                else:
                    with client:
                        self._me = client.me()
            except control_plane.ControlPlaneError as error:
                raise CliError(
                    f"{why}: {error}",
                    ExitCode.REFUSED,
                    hint=error.hint,
                ) from None
        return self._me  # type: ignore[return-value]

    # ── ledgers ──────────────────────────────────────────────────────────────

    def recorder(self, run_id: str) -> RunLedger:
        """The run's ledger: resumed when it exists, else opened under the run's identity."""
        if run_id not in self._ledgers:
            ledger = RunLedger(self.ws.repo, run_id)
            if (ledger.folder / "events.jsonl").is_file():
                ledger.resume()
            else:
                team = self.ws.team or "standalone"
                ledger.open(
                    identity=RunIdentity(team=team, policy_version=self.ws.policy().version),
                    kind="feature",
                    project=_slug(self.ws.repo.name) or "repo",
                    owner=team,
                    criticality=self.ws.config.get("criticality", "low"),
                )
            self._ledgers[run_id] = ledger
        return self._ledgers[run_id]

    def sinks(self) -> list[EventSink]:
        """Where runs are reported besides the run folder: the control plane, in org mode
        with one configured and the user signed in (M2-20). Not signed in, runs still run
        (the events stay queued in the ledger) and `sink_problem` says why."""
        if self._sinks is None:
            self._sinks = []
            if self.ws.settings.get("org"):
                try:
                    client = control_plane.client_for(self.ws.env, self.ws.settings, self.ws.home)
                except control_plane.ControlPlaneError as error:
                    client = None
                    hint = f" ({error.hint})" if error.hint else ""
                    self.sink_problem = f"runs are not reported to the control plane: {error}{hint}"
                if client is not None:
                    guard = control_plane.publish_guard(self.ws.policy)
                    self._sinks = [control_plane.ControlPlaneSink(client, guard=guard)]
        return self._sinks

    def forwarding(self, run_id: str) -> StampingRecorder:
        """The run's recorder: the ledger first, then the sinks on a background thread, so
        a slow control plane never holds up the run (DF-304). In org mode each event is
        screened by the publish scan before it is chained (M2-49) - signed in or not, as
        a run recorded offline is published by `agentic sync` later. Each step_finished
        carries the skills and notes the step used (M2-45)."""
        if run_id not in self._recorders:
            screen = None
            if self.ws.settings.get("org"):
                screen = control_plane.publish_guard(self.ws.policy).screen
            self._recorders[run_id] = ForwardingRecorder(
                self.recorder(run_id), self.sinks(), background=True, screen=screen
            )
            self._stamped[run_id] = StampingRecorder(self._recorders[run_id], self.activity)
            if ids := self._next_ids.pop(run_id, None):  # a policy change on resume (DF-697)
                self._stamped[run_id].stamp_next(ids)
        return self._stamped[run_id]

    def heartbeat(self, run_id: str) -> Heartbeat:
        """The run's heartbeat around one engine call (M3-07): into the run folder always,
        and to the control plane in org mode with one and the user signed in, on a client of
        its own (the sink's client belongs to the delivery thread). Not signed in, only the
        folder beats: `sinks()` already says why runs are not reported."""
        poster = None
        if self.ws.settings.get("org"):
            try:
                poster = control_plane.client_for(self.ws.env, self.ws.settings, self.ws.home)
            except control_plane.ControlPlaneError:
                poster = None
        return Heartbeat(
            self.store.path(run_id).parent,
            run_id,
            interval_s=self.ws.policy().run_health.heartbeat_seconds,
            poster=poster,
            status=lambda: self.store.load(run_id).status,  # a refused engine call (F1)
        )

    def close_sinks(self, timeout: float = SINK_GRACE_S) -> dict[str, int]:
        """Wait (bounded) for the runs' pending deliveries; returns run -> events still
        queued (`agentic sync` sends them later)."""
        queued = {}
        for run_id, recorder in self._recorders.items():
            left = sum(recorder.close(timeout).values())
            if left:
                queued[run_id] = left
        return queued

    def deliver_feedback(self, warn: Callable[[str], None]) -> None:
        """Send the feedback journals that grew during this command (after `close_sinks`,
        so the control plane holds each run's events first); a failure stays queued."""
        from agentic_cli.learning import deliver

        for run_id in sorted(self.feedback_runs):
            deliver(self, run_id, warn)
        self.feedback_runs.clear()

    def gates(self, run_id: str) -> ZyrofloGates:
        if run_id not in self._gates:
            self._gates[run_id] = ZyrofloGates(self.recorder(run_id))
        return self._gates[run_id]

    # ── preflight and plan ───────────────────────────────────────────────────

    def pipeline(self, name: str) -> PipelineDefinition:
        try:
            return self.ws.definitions().pipeline(name)
        except UnknownDefinitionError as error:
            raise CliError(str(error), ExitCode.INVALID) from error

    def sandbox(self, pipeline: PipelineDefinition) -> SandboxDecision:
        return decide_sandbox(machine_readiness(), pipeline.regime, self.ws.policy().sandbox)

    def preflight(self, name: str, *, live: bool) -> PreflightReport:
        """Every check before any spend. `live` installs the pipeline's skills first (a
        dry run writes nothing and checks them against the catalog instead)."""
        pipeline = self.pipeline(name)
        decision = self.sandbox(pipeline)
        report = preflight(
            pipeline,
            self.ws.definitions(),
            self.ws.policy(),
            self.ws.context(),
            actions=ACTIONS,
            sandbox=decision,
            available_backends=CONFIGURED_BACKENDS,
            skills=self._skill_check(name, live=live),
            honoured=agent_backends.HONOURED,  # the codex guard hook (DF-426)
            skill_versions=self._skill_versions(),  # deprecations (M2-15)
        )
        report.problems += agent_backends.problems(
            report, decision, repo=self.ws.repo, settings=self.ws.settings
        )
        if pipeline.regime == "regulated" or any(
            isinstance(step, GateStep) and step.four_eyes for step in pipeline.steps
        ):
            report.problems.append(Problem(None, "identity", UNVERIFIED, severity="warning"))
        tiers = (self.ws.policy_repo or Path("<policy repo>")) / "policy" / "tiers.yaml"
        try:
            missing = self._codeintel_missing()
        except CliError as error:  # DF-467: listed with the other problems, not raised alone
            missing = None
            detail = f"{error}" + (f" ({error.hint})" if error.hint else "")
            report.problems.append(Problem(None, "codeintel", detail))
        for agent_name, agent in sorted(report.resolved.items()):
            if PLACEHOLDER in agent.model:  # the gateway would refuse it on the first call
                detail = (
                    f"agent {agent_name} would call '{agent.model}' (tier {agent.tier}), the "
                    f"template's placeholder: set a model your gateway serves in {tiers}"
                )
                report.problems.append(Problem(None, "model", detail))  # DF-225
            if "codeintel" in agent.tools and missing:
                # DF-226: a missing optional extra (or domain graph) must not refuse the run
                detail = f"agent {agent_name} may use codeintel, but {missing}"
                report.problems.append(Problem(None, "codeintel", detail, severity="warning"))
        for skill in self._restored:  # DF-186: a local edit was put back before the run
            detail = f"skill {skill} had been changed locally; it was restored as pinned"
            report.problems.append(Problem(None, "skills", detail, severity="warning"))
        report.problems += self._publish_problems(pipeline)
        report.problems += self._personal_problems(report)
        return report

    def _codeintel_missing(self) -> str | None:
        """What the repo's code intelligence lacks on this machine (the extra, or a built
        domain graph), or None (`codeintel.adapter: none` lacks nothing)."""
        settings = codeintel_settings(self.ws.config)
        available = codeintel_index.available()
        return codeintel_warning(self.ws.repo, settings, crg_available=available)

    def _publish_problems(self, pipeline: PipelineDefinition) -> list[Problem]:
        """A publishing step whose host or token is missing fails here, before any spend
        (an error; a warning when the step has a `when` and may not run)."""
        from agentic_cli.publishing import publish_check  # it imports this module's peers

        steps = [
            inner
            for step in pipeline.steps
            for inner in (step.loop.steps if isinstance(step, LoopStep) else [step])
            if isinstance(inner, ActionStep) and inner.action in PUBLISH_ACTIONS
        ]
        problems, checked = [], {}
        for step in steps:
            kind = PUBLISH_ACTIONS[step.action]
            if kind not in checked:
                checked[kind] = publish_check(self.ws, kind)
            if checked[kind]:
                severity = "warning" if step.when else "error"
                problems.append(Problem(step.id, "publish", checked[kind], severity=severity))
        return problems

    def unenforced(self, name: str, report: PreflightReport) -> dict[str, list[str]]:
        """Per agent, its deny globs the OS sandbox cannot enforce (the file tools still
        do), when the run would use the sandbox (DF-64)."""
        decision = self.sandbox(self.pipeline(name))
        if not decision.use_srt:
            return {}
        wiring = self._wiring(decision)
        found = {a: wiring.unenforced_preview(agent) for a, agent in report.resolved.items()}
        return {agent: globs for agent, globs in sorted(found.items()) if globs}

    def plan(self, name: str, report: PreflightReport) -> dict:
        """What a run of `name` would do, step by step (`run --dry-run`, spec §24 step 6)."""
        pipeline = self.pipeline(name)
        decision = self.sandbox(pipeline)
        wiring = self._wiring(decision)

        def agent_step(step: AgentStep) -> dict:
            row: dict = {"kind": "agent", "id": step.id, "agent": step.agent, "when": step.when}
            agent = report.resolved.get(step.agent)
            if agent is None:
                return {**row, "resolved": False}
            limits, children = agent.limits, agent.children
            return {
                **row,
                "resolved": True,
                "version": agent.definition.version,
                "layer": self.ws.definitions().agent_source(step.agent),
                "type": agent.type,
                "tier": agent.tier,
                "model": agent.model,
                "backend": agent.backend,
                "limits": {
                    "budget_usd": limits.budget_usd,
                    "max_turns": limits.max_turns,
                    "max_minutes": limits.max_minutes,
                    "children": children.max_total or 0,
                },
                "tools": agent.tools,
                "skills": agent.skills,
                "consumes": agent.consumes,
                "produces": agent.produces,
                "unenforced_deny_paths": (
                    wiring.unenforced_preview(agent) if decision.use_srt else []
                ),
            }

        def gate_step(step: GateStep) -> dict:
            four_eyes = step.four_eyes or pipeline.regime == "regulated"
            # standalone (M1): no sign-in and no RBAC, so the plan must not promise them
            who = (
                f"role {step.approver_role} - not checked before org sign-in (M2-03): "
                "anyone using agentic in this repository can decide"
            )
            if pipeline.regime == "solo":
                who = f"you (solo regime); {who}"
            if four_eyes:
                who += (
                    "; four-eyes: a different name from the person who started the run "
                    "(names come from AGENTIC_USER or git user.email and are not authenticated)"
                )
            return {
                "kind": "gate",
                "id": step.gate,
                "approver_role": step.approver_role,
                "four_eyes": four_eyes,
                "who_approves": who,
                "on_reject": step.on_reject,
            }

        steps: list[dict] = []
        for step in pipeline.steps:
            if isinstance(step, AgentStep):
                steps.append(agent_step(step))
            elif isinstance(step, GateStep):
                steps.append(gate_step(step))
            elif isinstance(step, LoopStep):
                loop = step.loop
                inner = [
                    agent_step(s)
                    if isinstance(s, AgentStep)
                    else {"kind": "action", "id": s.id, "action": s.action, "when": s.when}
                    for s in loop.steps
                ]
                steps.append(
                    {
                        "kind": "loop",
                        "id": loop.id,
                        "max_iterations": loop.max_iterations,
                        "until": loop.until,
                        "on_exhausted": loop.on_exhausted,
                        "steps": inner,
                    }
                )
            elif isinstance(step, ActionStep):
                steps.append(
                    {"kind": "action", "id": step.id, "action": step.action, "when": step.when}
                )
        definitions = self.ws.definitions()
        applied = definitions.overlays.get(pipeline.name)
        return {
            "pipeline": pipeline.name,
            "version": pipeline.version,
            "layer": definitions.pipeline_source(pipeline.name),
            # the team's overlay for this repository (M2-23) and every version that is
            # not simply the current one, or is deprecated (M2-15)
            "overlay": (
                {"file": self.ws.policy_path(applied.file), "changes": list(applied.notes)}
                if applied is not None
                else None
            ),
            "versions": [choice.describe() for _, choice in sorted(definitions.versions.items())],
            "org": self.ws.settings.get("org") or None,  # None: a standalone policy
            "regime": pipeline.regime,
            "max_minutes": pipeline.max_minutes,
            "policy_version": self.ws.policy().version,
            "team": self.ws.team,
            "sandbox": decision.label,
            "approver_identity": "unverified",  # standalone: names and roles are not checked
            "steps": steps,
            "preflight": {
                "passed": report.passed,
                "problems": [
                    {
                        "step": p.step,
                        "check": p.check,
                        "detail": p.detail,
                        "policy": p.policy_value,
                        "severity": p.severity,
                    }
                    for p in report.problems
                ],
            },
        }

    # ── the engine ───────────────────────────────────────────────────────────

    def gateway(self) -> GatewaySettings:
        url = os.environ.get("AGENTIC_GATEWAY_URL", "").strip() or str(
            self.ws.settings.get("gateway_url") or ""
        )
        if not url:
            raise CliError(
                "no model gateway is configured",
                ExitCode.REFUSED,
                hint="set gateway_url in ~/.agentic/config.yaml (or AGENTIC_GATEWAY_URL)",
            )
        try:
            key = Credentials().get(TOKENLEAN_KEY).value
        except CredentialError as error:
            raise CliError(str(error), ExitCode.REFUSED, hint="run `agentic login`") from error
        return GatewaySettings(base_url=url, api_key=key)

    def redact(self, gateway: GatewaySettings | None = None) -> Callable[[str], str]:
        """Masks known secrets: the environment's, secret shapes, and the gateway key
        when given (the last one asked for is remembered, for later calls without it)."""
        if gateway is not None:
            self._gateway_key = gateway.api_key
        return redactor(extra=[self._gateway_key] if self._gateway_key else [])

    def engine(
        self,
        *,
        run_budget_usd: Decimal | None = None,
        regime: str = "team",
        offline: bool = False,
    ) -> Engine:
        """The engine for this workspace; the run's `regime` picks the sandbox decision.
        `offline` builds one that makes no model calls (stopping a run needs no gateway)."""
        gateway = None if offline else self.gateway()
        policy = self.ws.policy()
        decision = decide_sandbox(machine_readiness(), regime, policy.sandbox)
        wiring = self._wiring(decision)
        # the one model host an agent CLI process may reach under srt (M2-42)
        wiring.gateway_url = gateway.base_url if gateway is not None else None
        if gateway is None:
            runners = _no_model_calls
        else:
            agent_clis = agent_backends.build(
                repo=self.ws.repo,
                policy=policy,
                settings=self.ws.settings,
                gateway=gateway,
                wiring=wiring,
                identity_for=self.identity,
                fallback_estimate_usd=FALLBACK_ESTIMATE_USD,
            )
            runners = StepRunners(
                {"native": _native_backend(self, gateway), **agent_clis},
                tools_for=wiring.tools_for,
                state_dir_for=lambda step: (
                    self.ws.repo / STATE / step.run_id / "agents" / f"{step.step}-{step.iteration}"
                ),
                prompt_extras=StepExtras.for_runtime(self),  # pre-pass, personal skills
            )
        return Engine(
            killswitch=None if offline else self.killswitch(),  # stopping needs no switch
            kill_poll_s=killswitch_port.POLL_S,
            repo=self.repo_key(),
            definitions=self.ws.definitions(),
            policy=policy,
            context=self.ws.context(),
            store=self.store,
            recorder_for=self.forwarding,
            gates_for=self.gates,
            runner_factory=runners,
            block_on_for=regime_block_on,
            clock=SystemClock(),
            actions={name: make(self) for name, make in ACTIONS.items()},
            run_budget_usd=run_budget_usd,
            sandbox_label=decision.label,
            trees_for=wiring.trees_for,
            batch_budget_for=wiring.batch_budget_for,  # M2-24: one review batch per step
            step_logs=StepLogs(self.redact(gateway)),
            redact=self.redact(gateway),  # pause reasons and step details in run.json
            handoff_prefix=RUNS.as_posix(),  # repo-relative Agentic-Handoff trailers (DF-174)
            # gate corrections -> the run's feedback journal, sent when the command ends (M3-11)
            feedback=FeedbackCapture(self, deferred=True),
        )

    def repo_key(self) -> str | None:
        """This repository as `host/owner/name` (its origin), or None without one."""
        return self.ws.repo_key

    def control_plane(self) -> control_plane.ControlPlaneClient | None:
        """The org's control plane client: None outside org mode or without a configured
        control plane; `ControlPlaneRefused` when one is configured but not signed in."""
        if not self.ws.settings.get("org"):
            return None
        return control_plane.client_for(self.ws.env, self.ws.settings, self.ws.home)

    def authorizer(self) -> tuple[Authorizer, str | None]:
        """Who may decide at a gate, and the deciding person: in org mode with a control
        plane, the roles `/v1/me` reports (`OrgAuthorizer`, so an org admin standing in for
        the named role is recorded as such, DF-409) and the signed-in person; otherwise the
        standalone authorizer (no RBAC) and None (the workspace's person decides)."""
        me = self.me(why="gate decisions in org mode are checked by the control plane")
        if me is None:
            return StandaloneAuthorizer(), None
        return OrgAuthorizer(me, self.ws.team), me.user.email

    def check_same_policy(self, state: RunState) -> tuple[str, str] | None:
        """Before a paused run continues (resume, a gate decision): a run keeps the policy it
        started on, so its record names the policy every step used (spec §1.3, §12.1). With
        the control plane's policy, any `agentic run` in this home follows the tenant's active
        version (M2-53), so a run paused on an older one is refused here (exit 2) rather than
        carried on under the new tiers and tools with the old version on its record (review
        F3). A git-policy org's cache moves on with its 24 h refresh, so it is refused the
        same way (DF-697). `agentic stop` still ends it.

        A standalone or `--policy` repo is the person's own: a run paused on one version and
        resumed after its owner edited the policy continues (owner decision 105), and its
        ledger says so - the next event the run records carries the version in force before
        (`policy_from`: the last change its ledger recorded, else the version it started
        on) and the new one (`policy_to`), and `run.policy_changed` is in the audit log.
        Returns that (before, after) pair for the command to show, or None."""
        current = self.ws.policy().version
        if self.ws.org_policy_cache():
            if state.policy_version == current:
                return None
            raise CliError(
                f"run {state.run_id} started on org policy {state.policy_version}, and the org "
                f"policy is now {current}: a run continues only on the policy it started on",
                ExitCode.REFUSED,
                hint=f"agentic stop {state.run_id} --reason 'org policy changed', then start it "
                f"again: agentic run {state.pipeline}",
            )
        before = self._policy_in_force(state)
        if before == current:
            return None
        ids = {"policy_from": before, "policy_to": current}
        if state.run_id in self._stamped:
            self._stamped[state.run_id].stamp_next(ids)
        else:  # stamped once `forwarding` builds the run's recorder
            self._next_ids[state.run_id] = ids
        step = state.pending.step if state.pending is not None else None
        log_record("run.policy_changed", {"run": state.run_id, "step": step, **ids})
        return before, current

    def _policy_in_force(self, state: RunState) -> str:
        """The policy version a paused standalone run last ran under: the last change its
        ledger recorded, else the version it started on."""
        try:
            entries = RunLedger(self.ws.repo, state.run_id).events()
        except LedgerError:  # the resume itself reports a broken ledger
            return state.policy_version
        found = state.policy_version
        for entry in entries:
            block = (entry.get("ext") or {}).get("agentic") or {}
            found = block.get("policy_to") or found
        return found

    def killswitch(self) -> KillSwitch:
        """The engine's kill-switch port (M2-19): the control plane's switches in org mode
        with one; nothing to stop runs otherwise (standalone, or org mode without a control
        plane, which preflight warns about). Configured but not signed in, the state can
        never be confirmed, so every check holds the run (ruling R2)."""
        if self._killswitch is None:
            try:
                client = self.control_plane()
            except control_plane.ControlPlaneError as error:
                self._killswitch = killswitch_port.Unconfirmable(str(error))
            else:
                if client is None:
                    self._killswitch = NoKillSwitch()
                else:
                    user = (control_plane.login_record(self.ws.home) or {}).get("user")
                    self._killswitch = killswitch_port.ControlPlaneKillSwitch(
                        client, user=user, team=self.ws.team, repo=self.repo_key()
                    )
        return self._killswitch

    def identity(self, step: StepContext) -> CallIdentity:
        if step.run_id not in self._pipelines:
            self._pipelines[step.run_id] = self.store.load(step.run_id).pipeline
        agent = step.agent
        return CallIdentity(
            team=self.ws.team or "standalone",
            user=self.user,
            repo=self.ws.repo.name,
            pipeline=self._pipelines[step.run_id],
            run_id=step.run_id,
            step=step.step,
            agent=f"{agent.definition.name}@{agent.definition.version}",
            policy_version=self.ws.policy().version,
            tier=agent.tier,
            classification=agent.classification,  # the gateway's class rules (M2-09)
        )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _wiring(self, decision: SandboxDecision) -> StepWiring:
        # code-review-graph and/or the domain graph, as the repo chose (M2-43); None: agents
        # get no codeintel tools (wiring.NO_CODEINTEL)
        codeintel_for = codeintel_factory(
            self.ws.repo,
            codeintel_settings(self.ws.config),
            crg_available=codeintel_index.available(),
        )
        library = self._installed()
        return StepWiring(
            self.ws.repo,
            self.ws.policy().sandbox,
            decision,
            self.ws.repo / STATE,
            skills=library if isinstance(library, InstalledSkills) else None,
            codeintel_for=codeintel_for,
            scanner=production_scanner(self.ws.policy()),  # tool output, as model calls
            batch=self.ws.policy().review_batch,
            checkpoint_exclude=tuple(self.ws.policy().checkpoint_exclude),
        )

    def _catalog_root(self) -> Path | None:
        root = (self.ws.policy_repo or Path()) / "skills"
        return root if (root / "catalog.yaml").is_file() else None

    def _installed(self) -> InstalledSkills | None:
        if self._library is False:
            root = self._catalog_root()
            if root is None:
                self._library = None
            else:
                try:
                    catalog = load_catalog(root)
                except CatalogError as error:
                    raise CliError(str(error), ExitCode.INVALID) from error
                self._library = InstalledSkills(
                    catalog, installed_skills_root(self.ws), record=self.activity.record
                )
        return self._library  # type: ignore[return-value]

    def skill_library(self) -> InstalledSkills | None:
        """The repo's shared skills (None without a catalog in the policy repo)."""
        return self._installed()

    def personal_context(self) -> PersonalContext:
        """This user's personal skills and notes (M2-45), loaded once per command. Each
        refused one is named in the audit log here and as a preflight warning."""
        if self._personal is None:
            found = load_personal(self.ws.home)
            for refused in found.rejected:
                fields = {"path": refused.path, "reason": refused.reason}
                log_record("skills.personal_rejected", fields)
            library = self.skill_library()
            self._personal = PersonalContext(
                found,
                self.ws.policy().classifications,
                shared=list(library.catalog.skills) if library is not None else [],
                record=self.activity.record,
            )
        return self._personal

    def _personal_problems(self, report: PreflightReport) -> list[Problem]:
        """A personal skill or note that will not be used, said before the run (warnings):
        the user's only other trace is the audit log."""
        personal = self.personal_context()
        problems = [
            Problem(
                None,
                "personal-skills",
                f"~/.agentic/{refused.path} is not used: {refused.reason}",
                severity="warning",
            )
            for refused in personal.personal.rejected
        ]
        for agent_name, agent in sorted(report.resolved.items()):
            if dropped := personal.over_cap(agent):
                names = ", ".join(skill.name for skill in dropped)
                detail = (
                    f"agent {agent_name} gets at most {MAX_PERSONAL_SKILLS} personal skills; "
                    f"not used: {names}"
                )
                problems.append(Problem(None, "personal-skills", detail, severity="warning"))
        return problems

    def _skill_versions(self) -> dict[str, str] | None:
        """Each catalogued skill's version (None without a catalog)."""
        library = self._installed()
        if library is None:
            return None
        return {name: entry.version for name, entry in library.catalog.skills.items()}

    def _skill_check(self, pipeline: str, *, live: bool):
        library = self._installed()
        if library is None:
            return _Unavailable("no skills catalog in the policy repo (skills/catalog.yaml)")
        if not live:
            return _Catalogued(library)
        wanted = pipeline_skills(
            self.ws.definitions(), [pipeline], self.ws.policy(), self.ws.context()
        )
        known = [name for name in wanted if name in library.catalog.skills]
        if known:  # sync before every run: restores a locally edited skill (M1-37)

            def record(event: str, fields: dict[str, object]) -> None:
                log_record(event, fields)  # the audit log, as before
                if event == "skills.tamper_restored":  # and preflight shows it (DF-186)
                    self._restored.append(str(fields.get("skill")))

            sync_skills(library.catalog, self._catalog_root(), library.root, known, record=record)
        return library


def _no_model_calls(step: StepContext):
    raise EngineError("this command makes no model calls")


def _native_backend(rt: Runtime, gateway: GatewaySettings):
    from agentic.backends.native import NativeBackend

    policy = rt.ws.policy()
    return NativeBackend(
        identity_for=rt.identity,
        gateway=gateway,
        prices=policy.price_for,
        fallback_estimate_usd=FALLBACK_ESTIMATE_USD,
        policy_version=policy.version,
        network_transport=network_transport(),
        scanner_for=lambda step: production_scanner(policy),
    )


def tracing_wanted() -> bool:
    if os.environ.get("AGENTIC_TRACES", "").strip().lower() in ("0", "off", "false", "no"):
        return False
    return importlib.util.find_spec("opentelemetry.sdk") is not None


def _slug(text: str) -> str:
    return _UNSAFE.sub("-", text).strip("-._")
