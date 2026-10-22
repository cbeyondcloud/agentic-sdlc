"""What `agentic eval` does (M1-63, spike S10), apart from its flags and output.

`execute_run` evaluates one agent over a corpus:

  1. load the corpus, the policy and the agent definition, and resolve the agent;
  2. pick the judge for rubric cases from tier `eval-judge` (another model family);
  3. open the eval's ledger (`eval.run_started`), then run every case k times under
     label X with production settings, each run's model calls going to
       * `live`   - TokenLean (`--gateway`), with the user's TokenLean key;
       * `record` - TokenLean through the cassette server, which saves every call;
       * `replay` - the cassette server alone: recorded answers, no model, no network;
  4. score the runs (objective metrics; the blind rubric judge for scoping cases);
  5. write report.json, report.md and what the judge saw, and ledger
     `eval.report_written` with the report's digest.

`export_calibration` and `score_calibration` are the human calibration loop: a blinded
sample of judged outputs goes to two people (opaque ids only); their scores decide
whether the judge can be trusted for this rubric and judge model (weighted kappa).

Core cannot import commons, so the ledger (Zyroflo's, through commons) is wired here.
"""

import asyncio
import getpass
import hashlib
import json
import math
import os
import random
import secrets
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from statistics import fmean

from agentic.agents.metering import CostLog, MeteringTransport
from agentic.agents.models import GatewaySettings, build_model_for, metered_model
from agentic.agents.tools import LocalProcessSandbox, ToolContext, build_tools
from agentic.auth.credentials import TOKENLEAN_KEY, CredentialError, Credentials
from agentic.definitions import DefinitionError, load_agent
from agentic.eval import stats
from agentic.eval.blind import LeakCheck, Scrubber, assign_labels
from agentic.eval.cases import EvalCase
from agentic.eval.corpus import CorpusError, load_corpus, rubric_path
from agentic.eval.devtask import copy_workspace
from agentic.eval.gate import GATE_AGENTS, GateReport, evaluate_gate, render_gate
from agentic.eval.judge import (
    JUDGE_TIER,
    FindingJudge,
    JudgeError,
    RubricError,
    RubricJudge,
    load_rubric,
    resolve_judge,
)
from agentic.eval.metrics import FalseFindingMetric, ObjectiveMetrics, RubricMetric
from agentic.eval.records import RunRequest
from agentic.eval.replay import CassetteServer, Upstream, http_upstream
from agentic.eval.report import EvalReport, JudgeInfo, build_report, write_report
from agentic.eval.runner import (
    BUDGET_STOP,
    AgentCaseRunner,
    PrepareWorkspace,
    Workspace,
    run_eval,
)
from agentic.handoffs import BUILTIN_CATALOG
from agentic.policy import (
    OrgPolicy,
    PolicyError,
    ResolveContext,
    ResolvedAgent,
    load_policy,
    resolve_agent,
)
from agentic.sandbox import SandboxRules, SrtSandbox, expand_home
from agentic.sandbox.readiness import decide_sandbox
from agentic.transport import BudgetLedger, CallIdentity, NetworkSettings, make_network_transport
from agentic.yamlio import YamlFileError, dump_yaml, load_yaml, read_text
from agentic_cli.config import agentic_home, user_config
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.runtime import machine_readiness
from sdlc_commons.guards import Guard
from sdlc_commons.ledger import LedgerError, RunIdentity, RunLedger

MODES = ("live", "record", "replay")
SPLITS = ("holdout", "dev", "all")
#: In record and replay the handoff's time is fixed, so a replay reproduces the recording.
FIXTURE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
FALLBACK_ESTIMATE_USD = Decimal("0.05")  # an unpriced call's hold (and charge if unreported)
JUDGE_SHARE = Decimal("0.3")  # of the budget, held for the judge (S10: ~27 % of BA tokens)
TEAM = "eval"
CALIBRATION_FRACTION = 0.2  # S10: humans score 20 % of gate items...
CALIBRATION_MINIMUM = 10  # ... at least 10, doubled when the judge shares the agents' family
KAPPA_FLOOR = 0.6
WITHIN_ONE_FLOOR = 0.8
HALF_POINTS = [i / 2 for i in range(21)]  # 0, 0.5, ..., 10: a mean of two raters
CALIBRATION_KEY = "calibration-key.json"  # beside the report, never in calibration/


@dataclass(frozen=True)
class RunOptions:
    agent: str
    corpus: Path | None = None
    split: str = "holdout"
    repeat: int | None = None
    mode: str = "live"
    budget_usd: float | None = None
    out: Path | None = None
    policy: Path | None = None
    agents: Path | None = None
    cassettes: Path | None = None
    gateway: str | None = None
    seed: int = 0
    max_concurrency: int = 1
    classification: str | None = None  # for cases that name none (default: the org's)
    tag: str | None = None  # only cases carrying this tag (CI: `fixture`, the cassette subset)


@dataclass(frozen=True)
class RunResult:
    report: EvalReport
    folder: Path
    paths: dict[str, Path]
    replay_problems: list[str] = field(default_factory=list)  # the cassette server's


def execute_run(options: RunOptions, *, upstream: Upstream | None = None) -> RunResult:
    """Evaluate one agent; `upstream` replaces the gateway in record mode (scripts)."""
    if options.mode not in MODES:
        raise CliError(f"--mode must be one of {', '.join(MODES)}", ExitCode.USAGE)
    if options.split not in SPLITS:
        raise CliError(f"--split must be one of {', '.join(SPLITS)}", ExitCode.USAGE)
    if options.mode == "live" or (options.mode == "record" and upstream is None):
        if not options.gateway:
            raise CliError(
                f"--mode {options.mode} calls models through TokenLean",
                ExitCode.USAGE,
                hint="pass --gateway <TokenLean URL>, or use --mode replay for recorded runs",
            )
    root = _corpus_root(options.corpus)
    corpus = _load(lambda: load_corpus(root))
    fixtures = root / "fixtures"
    policy_dir = _folder(options.policy, fixtures / "policy", "--policy", "policy folder")
    agents_dir = _folder(options.agents, fixtures / "agents", "--agents", "agents folder")
    policy = _load(lambda: load_policy(policy_dir))
    definition = _agent(agents_dir, options.agent)
    split = None if options.split == "all" else options.split
    cases = corpus.select(options.agent, split, options.tag)  # type: ignore[arg-type]
    if not cases:
        if options.tag is not None and options.tag not in corpus.tags():
            raise CliError(
                f"no case in the corpus carries the tag {options.tag!r}",
                ExitCode.INVALID,
                hint=f"known tags: {', '.join(corpus.tags()) or 'none'}",
            )
        tagged = f" {options.tag!r}" if options.tag is not None else ""
        raise CliError(
            f"the corpus has no {options.split}{tagged} cases for agent {options.agent}",
            ExitCode.INVALID,
            hint=f"cases name their agent in 'agent:'; corpus: {root}",
        )
    classification = _classification(cases, options.classification, policy.default_classification)
    context = ResolveContext(repo_classification=classification)
    resolved, violations = resolve_agent(definition, policy, context)
    if resolved is None:
        reasons = "; ".join(f"{v.field}: {v.reason}" for v in violations)
        raise CliError(
            f"policy refuses agent {options.agent} for '{classification}' cases: {reasons}",
            ExitCode.REFUSED,
        )
    kind = resolved.produces
    if kind is None:
        raise CliError(
            f"agent {options.agent} produces no handoff: nothing to score", ExitCode.INVALID
        )
    repeats = options.repeat or policy.eval.repeats
    budget = options.budget_usd or policy.eval.budget_for(options.agent)
    agent_id = f"{definition.name}@{definition.version}"

    rubric_ids = sorted({c.rubric for c in cases if c.rubric})
    rubrics = {r: _load(lambda r=r: load_rubric(corpus.path(rubric_path(r)))) for r in rubric_ids}
    choice = None
    notes: list[str] = []
    adjudicate = any(c.expected.seeded_defects for c in cases)  # review: false findings
    if rubrics or adjudicate:
        try:  # the cases' class must allow the judge's tier: restricted data stays put
            choice = resolve_judge(policy, [resolved.model], classification=classification)
        except JudgeError as error:
            if rubrics:  # the primary metric needs the judge
                raise CliError(
                    str(error), ExitCode.REFUSED, hint="ask an admin for an eval-judge tier"
                ) from error
            notes.append(f"false findings are unmeasured: {error}")

    created = datetime.now(UTC)
    report_id = f"eval-{options.agent}-{created:%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"
    folder = options.out or Path(".agentic") / "local" / "eval" / report_id
    api_key = _api_key(options, upstream)  # before anything starts or is ledgered
    server = None
    if options.mode != "live":
        cassettes = options.cassettes or fixtures / "cassettes"
        if options.mode == "replay":
            server = CassetteServer(cassettes, "replay")
        else:
            server = CassetteServer(
                cassettes, "record", upstream or http_upstream(options.gateway or "")
            )
    network = NetworkSettings(trust_env=False) if options.mode == "replay" else None

    def gateway_for(key: str) -> GatewaySettings:
        base = server.base_url(key) if server is not None else (options.gateway or "")
        return GatewaySettings(base_url=base, api_key=api_key, network=network)

    # replay: fixed values, so a replay is the same everywhere; otherwise the person and
    # their team, so TokenLean attributes the spend to them (DF-248)
    user = TEAM if options.mode == "replay" else getpass.getuser()
    team = TEAM if options.mode == "replay" else _team()

    def identity(request: RunRequest, agent: str, tier: str) -> CallIdentity:
        return CallIdentity(
            team=team,
            user=user,
            repo=f"eval:{root.name}",
            pipeline="eval",
            run_id=request.run_id,
            step="eval",
            agent=agent,
            policy_version=policy.version,
            tier=tier,
        )

    money = BudgetLedger(Decimal(str(budget)))

    def model_for(request: RunRequest, costs: CostLog, slice_):
        model, _ = metered_model(
            resolved,
            identity(request, agent_id, resolved.tier),
            gateway_for(f"{request.cassette_key}/agent"),
            budget=slice_,
            price_for=policy.price_for,
            fallback_estimate_usd=FALLBACK_ESTIMATE_USD,
            log=costs,
        )
        return model

    scrubber = Scrubber(names=_model_terms(resolved.model))
    leaks = LeakCheck(_model_terms(resolved.model))
    judge_costs = CostLog()
    # The judge's calls are charged to the same cap, from a share held for them (an open
    # slice holds its limit, so an unlimited one would leave the agent runs nothing).
    judge_share = Decimal(str(budget)) * JUDGE_SHARE
    judge_slice = money.run.open("judge", "step", judge_share) if choice else None

    def judge_model(request: RunRequest):
        """The judge's model for one run: its calls keyed to the run, charged to the
        judge's share of the budget."""
        gateway = gateway_for(f"{request.cassette_key}/judge")
        meter = MeteringTransport(
            make_network_transport(gateway.network),
            budget=judge_slice,
            price_for=policy.price_for,
            fallback_estimate_usd=FALLBACK_ESTIMATE_USD,
            log=judge_costs,
        )
        who = identity(request, f"{JUDGE_TIER}@1", JUDGE_TIER)
        return build_model_for(choice.model, who, gateway, meter)  # type: ignore[union-attr]

    def judge_for(request: RunRequest) -> RubricJudge | None:
        if choice is None or judge_slice is None or request.case.rubric not in rubrics:
            return None
        return RubricJudge(
            judge_model(request),
            rubrics[request.case.rubric],
            model_id=choice.model,
            leaks=leaks,
            settings=choice.settings,
        )

    def finding_judge_for(request: RunRequest) -> FindingJudge | None:
        if choice is None or judge_slice is None or not request.case.expected.seeded_defects:
            return None
        return FindingJudge(
            judge_model(request), model_id=choice.model, leaks=leaks, settings=choice.settings
        )

    def reference(case) -> str | None:
        if not case.expected.golden:
            return None
        return read_text(corpus.path(case.expected.golden)).replace("\r\n", "\n")

    prepare = None
    if any(c.inputs.workspace for c in cases):  # dev tasks: tools on a copy, tests after
        prepare, resolved, workspace_notes = _workspaces(resolved, folder, options.mode, policy)
        notes += workspace_notes
    runner = AgentCaseRunner(
        resolved,
        model_for,
        schema=BUILTIN_CATALOG.schema(kind),
        state_root=folder / "state",
        budget=money,
        clock=None if options.mode == "live" else (lambda: FIXTURE_TIME),
        corpus_root=root,
        prepare=prepare,
    )
    evaluators = [
        ObjectiveMetrics(),
        RubricMetric(judge_for=judge_for, scrubber=scrubber, reference=reference),
        FalseFindingMetric(judge_for=finding_judge_for, scrubber=scrubber),
    ]
    ledger = EvalLedger(Path.cwd(), report_id, policy.version, options.agent)
    ledger("eval.run_started")
    try:  # whatever raises from here on, the ledger run is closed, never left open (DF-260)
        try:
            if server is not None:
                server.start()
                server.expect(f"{c.id}/X/{r}/agent" for c in cases for r in range(repeats))
            runset = asyncio.run(
                run_eval(
                    cases,
                    {"X": runner},
                    kind=kind,
                    repeats=repeats,
                    seed=options.seed,
                    evaluators=evaluators,
                    budget_usd=budget,
                    max_concurrency=options.max_concurrency,
                    name=f"eval-{options.agent}",
                )
            )
        finally:
            if server is not None:
                server.close()
            if judge_slice is not None and not judge_slice.closed:
                judge_slice.close()
        # runs the budget cap skipped or stopped made fewer calls than recorded: that is
        # INCOMPLETE, not a replay to re-record (DF-254)
        capped = [
            f"{r.request.cassette_key}/agent"
            for r in runset.runs
            if r.record.status == "skipped" or r.record.error == BUDGET_STOP
        ]
        problems = server.finish(capped=capped) if server is not None else []
        labels = assign_labels([agent_id], seed=options.seed)
        labels.seal_scores()
        judge_info = None
        if choice is not None:
            judge_info = JudgeInfo(
                tier=choice.tier,
                model=choice.model,
                family=choice.family,
                same_family=choice.same_family,
                rubrics={r.key: r.sha for r in rubrics.values()},
                warnings=list(choice.warnings),
            )
        report = build_report(
            runset,
            report_id=report_id,
            created_at=created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            mode=options.mode,  # type: ignore[arg-type]
            agent=options.agent,
            agent_version=definition.version,
            kind=kind,
            corpus_manifest_sha=corpus.manifest.sha,
            split=options.split,
            tag=options.tag,
            labels=labels.reveal(),
            judge=judge_info,
            leaks=leaks.leaks,
            stale_cassettes=server.stale if server is not None else 0,
            problems=problems,
            judge_cost_usd=_reported_total(judge_costs),
            warnings=notes,
        )
        paths = write_report(report, folder, runset.runs)
        for rubric_id in rubric_ids:  # calibration shows people the rubric the judge used
            target = folder / PurePosixPath(rubric_path(rubric_id))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(corpus.path(rubric_path(rubric_id)), target)
        digest = "sha256:" + hashlib.sha256(paths["json"].read_bytes()).hexdigest()
    except BaseException:
        ledger("eval.failed")
        raise
    ledger("eval.report_written", report=digest, verdict=report.verdict)
    return RunResult(report, folder, paths, replay_problems=problems)


def summary(result: RunResult) -> dict:
    report = result.report
    return {
        "report": str(result.paths["json"]),
        "markdown": str(result.paths["markdown"]),
        "verdict": report.verdict,
        "agent": report.agent,
        "cases": report.n,
        "repeats": report.k,
        "aggregates": {
            label: {name: agg.value for name, agg in metrics.items()}
            for label, metrics in report.aggregates.items()
        },
        "stale_cassettes": report.stale_cassettes,
        "problems": report.problems,
        "leaks": report.leaks,
        "warnings": [
            *(report.judge.warnings if report.judge is not None else []),
            *report.warnings,
        ],
    }


def failure(result: RunResult) -> CliError | None:
    """The error to exit with, when the report says the evaluation did not measure."""
    report = result.report
    # first: in replay the cap leaves later calls unasked, which is INCOMPLETE (exit 5),
    # not a broken cassette (DF-254)
    if report.budget_hit or report.verdict == "INCOMPLETE":
        return CliError(
            "the budget cap stopped the evaluation", ExitCode.STOPPED, hint="raise --budget-usd"
        )
    if result.replay_problems:
        verb = "recording" if report.mode == "record" else "replay"
        return CliError(
            f"the {verb} failed: {result.replay_problems[0]}",
            ExitCode.ERROR,
            hint="re-record the cassettes with --mode record (see library/eval/README.md)",
        )
    if report.leaks:
        return CliError(
            f"blinding leak: {', '.join(report.leaks)} reached the judge; the run is void",
            ExitCode.ERROR,
        )
    if report.problems:  # e.g. the judge failed on every run (live, or a recording)
        return CliError(
            f"the evaluation measured nothing: {report.problems[0]}",
            ExitCode.ERROR,
            hint=f"see the evaluator errors in {result.paths['markdown']}",
        )
    if report.verdict == "UNMEASURABLE":
        return CliError(
            "no run could be scored", ExitCode.ERROR, hint=f"see {result.paths['markdown']}"
        )
    return None


class EvalLedger:
    """An evaluation's record in the run ledger (Zyroflo's hash chain, through commons).

    The ledger speaks Zyroflo's lifecycle events only, so the S10 eval events map onto
    them, each value an id (digests, verdicts) - never free text:

      eval.run_started      -> step_started   (step "eval")
      eval.plan_registered  -> step_finished  (step "plan", artifacts {plan: sha})
      eval.ab_decided       -> step_finished  (step "ab", outcome verdict, artifacts {plan})
      eval.report_written   -> step_finished  (step "eval", outcome verdict,
                               artifacts {report.json: sha}), then run_closed
      eval.failed           -> run_closed     (status error: the run raised)

    It is also the `record(event, **fields)` callback `agentic.eval.abtest` takes.
    """

    def __init__(self, target: Path, run_id: str, policy_version: str, project: str) -> None:
        self.ledger = RunLedger(target, run_id)
        try:
            self.ledger.open(
                identity=RunIdentity(team=TEAM, policy_version=policy_version),
                kind="eval",
                project=project,
                owner=TEAM,
                criticality="low",
            )
        except LedgerError as error:
            raise CliError(
                f"the eval ledger could not be opened: {error}", ExitCode.ERROR
            ) from error

    def __call__(self, event: str, **fields: str) -> None:
        try:
            if event == "eval.run_started":
                self.ledger.record("step_started", phase="eval", actor=TEAM)
            elif event == "eval.plan_registered":
                artifacts = {"plan": fields["plan_sha"]}
                self.ledger.record(
                    "step_finished", phase="plan", outcome="registered", artifacts=artifacts
                )
            elif event == "eval.ab_decided":
                artifacts = {"plan": fields["plan_sha"]}
                outcome = fields["verdict"].lower()
                self.ledger.record(
                    "step_finished", phase="ab", outcome=outcome, artifacts=artifacts
                )
            elif event == "eval.report_written":
                outcome = fields["verdict"].lower()
                artifacts = {"report.json": fields["report"]}
                self.ledger.record(
                    "step_finished", phase="eval", outcome=outcome, artifacts=artifacts
                )
                self.ledger.record("run_closed", status=outcome)
                self.ledger.release()
            elif event == "eval.failed":
                self.ledger.record("run_closed", status="error")
                self.ledger.release()
            else:
                raise ValueError(f"unknown eval event {event!r}")
        except LedgerError as error:
            raise CliError(f"the eval ledger refused {event}: {error}", ExitCode.ERROR) from error


# ── the quality gate (M1-64) ──────────────────────────────────────────────────


def run_gate(
    report_dirs: list[Path],
    *,
    policy: Path | None = None,
    agents: list[str] | None = None,
    out: Path | None = None,
) -> tuple[GateReport, Path]:
    """Compare `eval run` reports with policy `eval.targets` (or the M1-64 defaults) and
    write gate.json and gate.md."""
    reports = [_report(folder) for folder in report_dirs]
    targets = _load(lambda: load_policy(policy)).eval.targets if policy is not None else {}
    gate = evaluate_gate(reports, targets=targets, agents=agents or list(GATE_AGENTS))
    folder = out or Path(".agentic") / "local" / "eval" / f"gate-{datetime.now(UTC):%Y%m%dT%H%M%S}"
    _write(folder / "gate.json", json.dumps(gate.model_dump(mode="json"), indent=2) + "\n")
    _write(folder / "gate.md", render_gate(gate))
    return gate, folder


def gate_failure(gate: GateReport, folder: Path) -> CliError | None:
    if gate.verdict == "NOT_A_GATE":
        return CliError(
            f"these reports cannot decide the gate: {gate.problems[0]}",
            ExitCode.INVALID,
            hint="gate on live `eval run` reports of the whole holdout split, one per agent",
        )
    if gate.verdict == "FAIL":
        missed = [f"{r.agent} {r.metric}" for r in gate.rows if r.verdict == "FAIL"]
        return CliError(
            f"the gate failed: {', '.join(missed)} missed their targets",
            ExitCode.ERROR,
            hint=f"see {folder / 'gate.md'}",
        )
    if gate.verdict == "INCOMPLETE":
        unmeasured = [f"{r.agent} {r.metric}" for r in gate.rows if r.verdict == "NOT_MEASURED"]
        return CliError(
            f"the gate is incomplete: {', '.join(unmeasured)} not measured",
            ExitCode.ERROR,
            hint=f"see {folder / 'gate.md'}",
        )
    return None


# ── human calibration ─────────────────────────────────────────────────────────


def export_calibration(report_dir: Path, out: Path | None = None, *, seed: int = 0) -> dict:
    """A blinded sample of judged outputs for people to score (S10: 20 %, at least 10,
    doubled when the judge shares the agents' family). People see exactly what the
    judge saw, under opaque ids; the key stays with the report."""
    report = _report(report_dir)
    judged = [
        r
        for r in report.runs
        if r.judge is not None and (report_dir / "judged" / f"{r.run_id}.md").is_file()
    ]
    if not judged:
        raise CliError("the report has no judged outputs to calibrate", ExitCode.INVALID)
    wanted = max(CALIBRATION_MINIMUM, math.ceil(CALIBRATION_FRACTION * len(judged)))
    if report.judge is not None and report.judge.same_family:
        wanted *= 2
    sample = sorted(
        random.Random(seed).sample(judged, min(wanted, len(judged))), key=lambda r: r.run_id
    )
    rubric_keys = sorted({r.judge.rubric_key for r in sample if r.judge})
    items, key = [], {}
    for run in sample:
        opaque = (
            "item-" + hashlib.sha256(f"{report.report_id}/{run.run_id}".encode()).hexdigest()[:10]
        )
        text = read_text(report_dir / "judged" / f"{run.run_id}.md")
        item = {"id": opaque, "rubric": run.judge.rubric_key, "text": text}  # type: ignore[union-attr]
        context_path = report_dir / "judged" / f"{run.run_id}.context.json"
        if context_path.is_file():  # what the judge graded against, so raters do too
            context = json.loads(context_path.read_bytes())
            item |= {"reference": context.get("reference"), "key_points": context["key_points"]}
        items.append(item)
        key[opaque] = {
            "run_id": run.run_id,
            "judge_total": run.metrics.get("rubric"),
            "rubric": run.judge.rubric_key,  # type: ignore[union-attr]
            "judge_model": run.judge.model_id,  # type: ignore[union-attr]
        }
    items.sort(key=lambda item: item["id"])
    rubrics = {}
    for path in sorted((report_dir / "rubrics").glob("*.yaml")):
        rubric = load_rubric(path)
        if rubric.key in rubric_keys:
            rubrics[rubric.key] = {
                c.id: {"title": c.title, "anchors": c.anchors} for c in rubric.criteria
            }
    folder = report_dir / "calibration"
    pack_path = out or folder / "pack.yaml"
    pack = {
        "instructions": (
            "Score each item 0-10 with the rubric: sum the criteria, each 0, 1 or 2 by its "
            "anchors, judging the text against the item's reference and key points where "
            "given. Score on your own; do not discuss items with the other rater. Copy "
            "scores-template.yaml, fill in your name and a score per item, and return it."
        ),
        "rubrics": rubrics,
        "items": items,
    }
    _write(pack_path, dump_yaml(pack))
    _write(
        folder / "scores-template.yaml",
        dump_yaml({"rater": "", "scores": {i["id"]: None for i in items}}),
    )
    # the answer key (the judge's totals) stays beside the report, outside the folder that
    # is handed to raters (DF-257)
    old_key = folder / "key.json"
    old_key.unlink(missing_ok=True)
    _write(
        report_dir / CALIBRATION_KEY,
        json.dumps({"report_id": report.report_id, "items": key}, indent=2) + "\n",
    )
    return {
        "pack": str(pack_path),
        "template": str(folder / "scores-template.yaml"),
        "items": len(items),
        "same_family": bool(report.judge and report.judge.same_family),
    }


def score_calibration(report_dir: Path, score_files: list[Path]) -> dict:
    """Judge vs people (S10): accept when the quadratic-weighted kappa between the judge
    and the raters' mean is >= 0.6 and they are within 1 point on >= 80 % of items. If
    the raters disagree with each other (kappa < 0.6) the rubric is ambiguous: fix it
    first."""
    key_path = report_dir / CALIBRATION_KEY
    if not key_path.is_file():
        raise CliError(
            "no calibration export for this report",
            ExitCode.INVALID,
            hint="run `agentic eval calibrate export` first",
        )
    key = json.loads(key_path.read_bytes())["items"]
    if len(score_files) < 2:
        raise CliError(
            "calibration needs two raters' scores", ExitCode.USAGE, hint="pass --scores twice"
        )
    for i, path in enumerate(score_files):
        if any(_same_file(path, other) for other in score_files[:i]):
            raise CliError(
                f"{path} is passed twice: the same file is not a second rater",
                ExitCode.USAGE,
                hint="pass each rater's own scores file once",
            )
    raters = [_rater(path, set(key)) for path in score_files]
    names = [name.strip().casefold() for name, _ in raters]
    if len(set(names)) != len(names):
        raise CliError(
            "calibration needs two different raters, but two files name the same rater "
            f"({', '.join(name for name, _ in raters)})",
            ExitCode.INVALID,
            hint="each rater scores on their own and puts their name in 'rater:'",
        )
    items = sorted(set(key).intersection(*(set(scores) for _, scores in raters)))
    items = [i for i in items if key[i]["judge_total"] is not None]
    if not items:
        raise CliError("no item was scored by every rater", ExitCode.INVALID)
    judge = [float(key[i]["judge_total"]) for i in items]
    human = [fmean(scores[i] for _, scores in raters) for i in items]
    human_half = [round(h * 2) / 2 for h in human]
    first, second = raters[0][1], raters[1][1]
    try:
        inter = stats.weighted_kappa(
            [first[i] for i in items], [second[i] for i in items], categories=range(11)
        )
    except stats.Unmeasurable:
        inter = 1.0  # both raters gave one same score to everything: no disagreement
    try:
        kappa = stats.weighted_kappa(judge, human_half, categories=HALF_POINTS)
    except stats.Unmeasurable:
        kappa = 0.0
    within = sum(abs(j - h) <= 1 for j, h in zip(judge, human, strict=True)) / len(items)
    if inter < KAPPA_FLOOR:
        verdict = "RUBRIC_AMBIGUOUS"
    elif kappa >= KAPPA_FLOOR and within >= WITHIN_ONE_FLOOR:
        verdict = "ACCEPTED"
    else:
        verdict = "REJECTED"
    report = _report(report_dir)
    rubric_keys = sorted({key[i]["rubric"] for i in items})
    known_shas = report.judge.rubrics if report.judge is not None else {}
    result = {
        "verdict": verdict,
        "report_id": report.report_id,
        "items": len(items),
        "raters": [name for name, _ in raters],
        "kappa_judge": round(kappa, 4),
        "within_1": round(within, 4),
        "kappa_raters": round(inter, 4),
        # what was calibrated, so an A/B or gate can match it (abtest.Calibration)
        "judge_models": sorted({key[i]["judge_model"] for i in items}),
        "rubrics": rubric_keys,
        "rubric_shas": sorted({known_shas[k] for k in rubric_keys if k in known_shas}),
    }
    _write(report_dir / "calibration" / "result.json", json.dumps(result, indent=2) + "\n")
    return result


# ── helpers ───────────────────────────────────────────────────────────────────


def _corpus_root(given: Path | None) -> Path:
    root = given or Path(".agentic") / "eval"
    if not (root / "cases").is_dir():
        raise CliError(
            f"no eval corpus at {root} (it needs a cases/ folder)",
            ExitCode.INVALID,
            hint="pass --corpus <folder>, e.g. --corpus library/eval for the fixture corpus",
        )
    return root


def _classification(cases: list[EvalCase], given: str | None, default: str) -> str:
    """The one data class the run's cases belong to. A case names its class in
    `source.classification`; `--classification` (else the org default) covers the cases
    that name none and can never relabel one that does. Policy does not rank classes,
    so cases of two classes cannot be run under either: they are refused."""
    named = {c.id: c.source.classification for c in cases if c.source.classification}
    if given is not None and (other := sorted({v for v in named.values() if v != given})):
        raise CliError(
            f"--classification {given} cannot relabel cases classified "
            f"{', '.join(repr(c) for c in other)}",
            ExitCode.INVALID,
            hint="drop --classification, or run those cases on their own",
        )
    fallback = given or default
    classes = sorted({named.get(c.id, fallback) for c in cases})
    if len(classes) > 1:
        raise CliError(
            f"the selected cases come from repos of different classes "
            f"({', '.join(repr(c) for c in classes)}); each class's tiers differ",
            ExitCode.INVALID,
            hint="run each class's cases from their own corpus, or label them alike",
        )
    return classes[0]


def _folder(given: Path | None, default: Path, flag: str, what: str) -> Path:
    if given is not None:
        if not given.is_dir():
            raise CliError(  # a wrong argument, not a broken policy (DF-259)
                f"{flag} {given} is not a folder", ExitCode.USAGE, hint=f"pass the {what}"
            )
        return given
    if default.is_dir():
        return default
    raise CliError(
        f"no {what} given",
        ExitCode.USAGE,
        hint=f"pass {flag} <folder> (the corpus has no {default.parent.name}/{default.name})",
    )


def _load[T](load: Callable[[], T]) -> T:
    try:
        return load()
    except (CorpusError, PolicyError, RubricError, DefinitionError) as error:
        raise CliError(str(error), ExitCode.INVALID) from error


def _agent(folder: Path, name: str):
    path = folder / f"{name}.md"
    if not path.is_file():
        known = ", ".join(sorted(p.stem for p in folder.glob("*.md"))) or "none"
        raise CliError(
            f"no agent definition {name} in {folder}",
            ExitCode.INVALID,
            hint=f"known agents: {known}",
        )
    return _load(lambda: load_agent(path))


#: The tools an eval can give an agent on a dev task's tree; the others (code
#: intelligence, skills) need a run's repository and installed skills.
EVAL_TOOLS = ("read_files", "search", "edit_files", "run_shell")


def _workspaces(
    resolved: ResolvedAgent, folder: Path, mode: str, policy: OrgPolicy
) -> tuple[PrepareWorkspace, ResolvedAgent, list[str]]:
    """How each dev-task run gets its tree (DF-241): a copy of the workspace under a
    guard confined to it, and the agent's file and shell tools on it, sandboxed as a
    solo run would be - srt when this machine has it ready, else a plain process on
    the guards (org policy decides whether that is allowed). A replay always uses a
    plain process: its tool calls are recorded, and the result must not depend on
    the machine. Returns the agent with the tools the eval can give it, and notes."""
    notes = []
    usable = [t for t in resolved.tools if t in EVAL_TOOLS]
    if dropped := [t for t in resolved.tools if t not in EVAL_TOOLS]:
        notes.append(
            f"dev tasks run {resolved.definition.name} without {', '.join(dropped)}: those "
            "tools need a run's repository; the tests measure the change without them"
        )
    agent = resolved.model_copy(update={"tools": usable})
    use_srt = False
    if mode != "replay":
        decision = decide_sandbox(machine_readiness(), "solo", policy.sandbox)
        if decision.refusal:
            raise CliError(decision.refusal, ExitCode.REFUSED)
        if decision.warning:  # the first line; `agentic doctor` shows the remedy in full
            notes.append(
                f"dev tasks: {decision.warning.splitlines()[0]} (the agent's commands and "
                "the case's tests run as plain processes)"
            )
        use_srt = decision.use_srt

    def prepare(request: RunRequest, source: Path) -> Workspace:
        target = folder / "workspaces" / request.run_id
        target.mkdir(parents=True, exist_ok=True)
        guard = Guard.for_agent(target, request.run_id, agent)
        tree = copy_workspace(source, guard.trees_root / "tree")
        sandbox = LocalProcessSandbox()
        if use_srt:
            rules = SandboxRules(
                write=(tree,),
                deny_read=expand_home(policy.sandbox.effective_deny_read, Path.home()),
                allowed_domains=tuple(policy.sandbox.allowed_domains),
            )
            sandbox = SrtSandbox(rules, folder / "sandbox" / request.run_id)
        context = ToolContext(tree=tree, guard=guard, sandbox=sandbox, allow_unisolated=not use_srt)
        return Workspace(tree, build_tools(agent, context), sandbox if use_srt else None)

    return prepare, agent, notes


def _team() -> str:
    """The person's team as other commands see it: AGENTIC_TEAM, else the user config's
    `team`, else 'eval'."""
    named = os.environ.get("AGENTIC_TEAM", "").strip()
    return named or str(user_config(agentic_home()).get("team") or "").strip() or TEAM


def _api_key(options: RunOptions, upstream: Upstream | None) -> str:
    if options.mode == "replay" or upstream is not None:
        return "not-used-in-replay"
    try:
        return Credentials().get(TOKENLEAN_KEY).value
    except CredentialError as error:
        raise CliError(str(error), ExitCode.REFUSED) from error


def _reported_total(costs: CostLog) -> float | None:
    """The calls' total cost, or None when any call's cost is unknown (never a partial sum)."""
    amounts = [cost.cost_usd for cost, _ in costs.calls]
    if not amounts or any(a is None for a in amounts):
        return None
    return float(sum(a for a in amounts if a is not None))


def _model_terms(model: str) -> list[str]:
    """The candidate model's id and name: the judge must not learn whose output it grades."""
    name = model.split("/", 1)[-1]
    return [t for t in dict.fromkeys([model, name]) if len(t) >= 3]


def _report(folder: Path) -> EvalReport:
    path = folder / "report.json"
    if not path.is_file():
        raise CliError(
            f"no report.json in {folder}", ExitCode.INVALID, hint="pass the folder `eval run` wrote"
        )
    return EvalReport.model_validate_json(path.read_bytes())


def _rater(path: Path, known: set[str]) -> tuple[str, dict[str, int]]:
    try:
        data, _ = load_yaml(read_text(path))
    except (YamlFileError, OSError) as error:
        raise CliError(f"{path}: {error}", ExitCode.INVALID) from error
    if not isinstance(data, dict) or not isinstance(data.get("scores"), dict):
        raise CliError(f"{path}: expected 'rater' and 'scores'", ExitCode.INVALID)
    name = str(data.get("rater") or path.stem)
    scores = {}
    for item, value in data["scores"].items():
        if value is None:
            continue
        if item not in known:
            raise CliError(f"{path}: unknown item {item}", ExitCode.INVALID)
        # YAML reads yes/no/true/false as booleans, which Python counts as 0 and 1
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10:
            raise CliError(
                f"{path}: {item} must be a whole number 0-10, not {value!r}", ExitCode.INVALID
            )
        scores[item] = value
    return name, scores


def _same_file(a: Path, b: Path) -> bool:
    """One file under two spellings (case-insensitive file systems, '..', links)."""
    try:
        return os.path.samefile(a, b)
    except OSError:  # a missing file: compare the spelling; reading it reports the error
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
