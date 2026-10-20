"""The engine for one workspace (M1-54, DF-47): what `run`, `resume` and the gate verbs
drive, wired from core and commons.

  * run state     - `RunStore` under `.agentic/local/runs`, next to Zyroflo's ledger;
  * run ledger    - `RunLedger`, opened for a new run and resumed for an existing one,
                    behind a `ForwardingRecorder` (no sinks in M1; M1-44, DF-165);
  * gates         - `ZyrofloGates`, blocking severities by regime (`regime_block_on`);
  * steps         - `StepRunners` over the native backend: every model call through the
                    configured gateway (TokenLean), metered against the step's budget,
                    secret-scanned under org policy; tools from `StepWiring` (run trees,
                    guard, sandbox, installed skills, code intelligence);
  * step logs     - `StepLogs` through the commons redactor, the gateway key included;
  * traces        - the local JSONL span exporter into the run folder, when the
                    telemetry extra is installed (`AGENTIC_TRACES=off` turns it off).

The claude-code backend is not built yet (DF-74): preflight is given the configured
backends, so a step naming another backend is refused before any spend.
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
from agentic.engine.preflight import PreflightReport, Problem, preflight
from agentic.engine.state import RunStore
from agentic.policy import ResolvedAgent
from agentic.sandbox.readiness import Readiness, SandboxDecision, decide_sandbox, readiness
from agentic.security import SecretScanner
from agentic.skills.catalog import CatalogError, load_catalog
from agentic.skills.install import sync_skills
from agentic.skills.loader import InstalledSkills
from agentic.skills.select import pipeline_skills
from agentic.telemetry.logs import StepLogs
from agentic.telemetry.sinks import ForwardingRecorder
from agentic.transport import CallIdentity
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.wiring import StepWiring
from agentic_cli.workspace import Workspace
from sdlc_commons.gates import ZyrofloGates, regime_block_on
from sdlc_commons.ledger import RunIdentity, RunLedger
from sdlc_commons.redaction import redactor

#: The backends this CLI builds. The claude-code backend is not wired yet (DF-74), so
#: preflight refuses a step that names it instead of failing mid-run.
CONFIGURED_BACKENDS: tuple[str, ...] = ("native",)
#: Built-in actions (`github.open_pr` arrives with M1-58).
ACTIONS: dict = {}
SKILLS = Path(".agentic") / "local" / "skills"
STATE = Path(".agentic") / "local" / "state"
#: Charged for a call whose cost the gateway did not report and whose model has no price.
FALLBACK_ESTIMATE_USD = Decimal("0.05")
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

    @property
    def user(self) -> str:
        return self.ws.user()

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
        report = preflight(
            pipeline,
            self.ws.definitions(),
            self.ws.policy(),
            self.ws.context(),
            actions=ACTIONS,
            sandbox=self.sandbox(pipeline),
            available_backends=CONFIGURED_BACKENDS,
            skills=self._skill_check(name, live=live),
        )
        if not codeintel_index.available():
            for agent_name, agent in sorted(report.resolved.items()):
                if "codeintel" in agent.tools:
                    detail = (
                        f"agent {agent_name} may use codeintel, but the code intelligence "
                        "extra is not installed (agentic-core[codeintel])"
                    )
                    report.problems.append(Problem(None, "codeintel", detail))
        return report

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
            if pipeline.regime == "solo":
                who = f"you (solo regime), or anyone holding {step.approver_role}"
            else:
                who = f"a person holding {step.approver_role}"
            if four_eyes:
                who += ", not the person who started the run (four-eyes)"
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
        return {
            "pipeline": pipeline.name,
            "version": pipeline.version,
            "layer": self.ws.definitions().pipeline_source(pipeline.name),
            "regime": pipeline.regime,
            "max_minutes": pipeline.max_minutes,
            "policy_version": self.ws.policy().version,
            "team": self.ws.team,
            "sandbox": decision.label,
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
        return redactor(extra=[gateway.api_key] if gateway is not None else [])

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
        if gateway is None:
            runners = _no_model_calls
        else:
            runners = StepRunners(
                {"native": _native_backend(self, gateway)},
                tools_for=wiring.tools_for,
                state_dir_for=lambda step: (
                    self.ws.repo / STATE / step.run_id / "agents" / f"{step.step}-{step.iteration}"
                ),
            )
        return Engine(
            definitions=self.ws.definitions(),
            policy=policy,
            context=self.ws.context(),
            store=self.store,
            recorder_for=lambda run_id: ForwardingRecorder(self.recorder(run_id), []),
            gates_for=self.gates,
            runner_factory=runners,
            block_on_for=regime_block_on,
            clock=SystemClock(),
            actions=ACTIONS,
            run_budget_usd=run_budget_usd,
            sandbox_label=decision.label,
            trees_for=wiring.trees_for,
            step_logs=StepLogs(self.redact(gateway)),
        )

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
        )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _wiring(self, decision: SandboxDecision) -> StepWiring:
        codeintel_for = None
        if codeintel_index.available():
            from agentic.codeintel.crg import CodeReviewGraph

            def codeintel_for(tree: Path, data: Path):
                index = codeintel_index.LocalIndex(tree, data, home_dir=data / "home")
                return CodeReviewGraph(index)

        library = self._installed()
        return StepWiring(
            self.ws.repo,
            self.ws.policy().sandbox,
            decision,
            self.ws.repo / STATE,
            skills=library if isinstance(library, InstalledSkills) else None,
            codeintel_for=codeintel_for,
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
                self._library = InstalledSkills(catalog, installed_skills_root(self.ws))
        return self._library  # type: ignore[return-value]

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
            sync_skills(library.catalog, self._catalog_root(), library.root, known)
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
        scanner_for=lambda step: SecretScanner.from_policy(policy.secret_scan),
    )


def tracing_wanted() -> bool:
    if os.environ.get("AGENTIC_TRACES", "").strip().lower() in ("0", "off", "false", "no"):
        return False
    return importlib.util.find_spec("opentelemetry.sdk") is not None


def _slug(text: str) -> str:
    return _UNSAFE.sub("-", text).strip("-._")
