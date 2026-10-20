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
import random
import secrets
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import fmean

from agentic.agents.metering import CostLog, MeteringTransport
from agentic.agents.models import GatewaySettings, build_model_for, metered_model
from agentic.auth.credentials import TOKENLEAN_KEY, CredentialError, Credentials
from agentic.definitions import DefinitionError, load_agent
from agentic.eval import stats
from agentic.eval.blind import LeakCheck, Scrubber, assign_labels
from agentic.eval.corpus import CorpusError, load_corpus
from agentic.eval.judge import (
    JUDGE_TIER,
    JudgeError,
    RubricError,
    RubricJudge,
    load_rubric,
    resolve_judge,
)
from agentic.eval.metrics import ObjectiveMetrics, RubricMetric
from agentic.eval.records import RunRequest
from agentic.eval.replay import CassetteServer, Upstream, http_upstream
from agentic.eval.report import EvalReport, JudgeInfo, build_report, write_report
from agentic.eval.runner import AgentCaseRunner, run_eval
from agentic.handoffs import BUILTIN_CATALOG
from agentic.policy import PolicyError, ResolveContext, load_policy, resolve_agent
from agentic.transport import BudgetLedger, CallIdentity, NetworkSettings, make_network_transport
from agentic.yamlio import YamlFileError, dump_yaml, load_yaml, read_text
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
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


@dataclass(frozen=True)
class RunResult:
    report: EvalReport
    folder: Path
    paths: dict[str, Path]


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
    policy_dir = _folder(options.policy, fixtures / "policy", "--policy", "the policy folder")
    agents_dir = _folder(options.agents, fixtures / "agents", "--agents", "agent definitions")
    policy = _load(lambda: load_policy(policy_dir))
    definition = _agent(agents_dir, options.agent)
    resolved, violations = resolve_agent(definition, policy, ResolveContext())
    if resolved is None:
        reasons = "; ".join(f"{v.field}: {v.reason}" for v in violations)
        raise CliError(f"policy refuses agent {options.agent}: {reasons}", ExitCode.REFUSED)
    kind = resolved.produces
    if kind is None:
        raise CliError(
            f"agent {options.agent} produces no handoff: nothing to score", ExitCode.INVALID
        )
    split = None if options.split == "all" else options.split
    cases = corpus.select(agent=options.agent, split=split)  # type: ignore[arg-type]
    if not cases:
        raise CliError(
            f"the corpus has no {options.split} cases for agent {options.agent}",
            ExitCode.INVALID,
            hint=f"cases name their agent in 'agent:'; corpus: {root}",
        )
    repeats = options.repeat or policy.eval.repeats
    budget = options.budget_usd or policy.eval.budget_for(options.agent)
    agent_id = f"{definition.name}@{definition.version}"

    rubric_ids = sorted({c.rubric for c in cases if c.rubric})
    rubrics = {
        r: _load(lambda r=r: load_rubric(root / "rubrics" / f"{r}.yaml")) for r in rubric_ids
    }
    choice = None
    if rubrics:
        try:
            choice = resolve_judge(policy, [resolved.model])
        except JudgeError as error:
            raise CliError(
                str(error), ExitCode.REFUSED, hint="ask an admin for an eval-judge tier"
            ) from error

    created = datetime.now(UTC)
    report_id = f"eval-{options.agent}-{created:%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"
    folder = options.out or Path(".agentic") / "local" / "eval" / report_id
    ledger = EvalLedger(Path.cwd(), report_id, policy.version, options.agent)
    ledger("eval.run_started")

    server = None
    if options.mode != "live":
        cassettes = options.cassettes or fixtures / "cassettes"
        if options.mode == "replay":
            server = CassetteServer(cassettes, "replay")
        else:
            server = CassetteServer(
                cassettes, "record", upstream or http_upstream(options.gateway or "")
            )
        server.start()
    api_key = _api_key(options, upstream)
    network = NetworkSettings(trust_env=False) if options.mode == "replay" else None

    def gateway_for(key: str) -> GatewaySettings:
        base = server.base_url(key) if server is not None else (options.gateway or "")
        return GatewaySettings(base_url=base, api_key=api_key, network=network)

    user = TEAM if options.mode == "replay" else getpass.getuser()

    def identity(request: RunRequest, agent: str, tier: str) -> CallIdentity:
        return CallIdentity(
            team=TEAM,
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

    def judge_for(request: RunRequest) -> RubricJudge | None:
        if choice is None or judge_slice is None or request.case.rubric not in rubrics:
            return None
        gateway = gateway_for(f"{request.cassette_key}/judge")
        meter = MeteringTransport(
            make_network_transport(gateway.network),
            budget=judge_slice,
            price_for=policy.price_for,
            fallback_estimate_usd=FALLBACK_ESTIMATE_USD,
            log=judge_costs,
        )
        who = identity(request, f"{JUDGE_TIER}@1", JUDGE_TIER)
        model = build_model_for(choice.model, who, gateway, meter)
        return RubricJudge(model, rubrics[request.case.rubric], model_id=choice.model, leaks=leaks)

    def reference(case) -> str | None:
        if not case.expected.golden:
            return None
        return read_text(corpus.path(case.expected.golden)).replace("\r\n", "\n")

    runner = AgentCaseRunner(
        resolved,
        model_for,
        schema=BUILTIN_CATALOG.schema(kind),
        state_root=folder / "state",
        budget=money,
        clock=None if options.mode == "live" else (lambda: FIXTURE_TIME),
    )
    evaluators = [
        ObjectiveMetrics(),
        RubricMetric(judge_for=judge_for, scrubber=scrubber, reference=reference),
    ]
    try:
        if server is not None:
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
    problems = server.finish() if server is not None else []
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
        labels=labels.reveal(),
        judge=judge_info,
        leaks=leaks.leaks,
        stale_cassettes=server.stale if server is not None else 0,
        problems=problems,
        judge_cost_usd=_reported_total(judge_costs),
    )
    paths = write_report(report, folder, runset.runs)
    for rubric_id in rubric_ids:  # calibration shows people the rubric the judge used
        target = folder / "rubrics" / f"{rubric_id}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / "rubrics" / f"{rubric_id}.yaml", target)
    digest = "sha256:" + hashlib.sha256(paths["json"].read_bytes()).hexdigest()
    ledger("eval.report_written", report=digest, verdict=report.verdict)
    return RunResult(report, folder, paths)


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
    }


def failure(result: RunResult) -> CliError | None:
    """The error to exit with, when the report says the evaluation did not measure."""
    report = result.report
    if report.problems:
        return CliError(
            f"the replay failed: {report.problems[0]}",
            ExitCode.ERROR,
            hint="re-record the cassettes with --mode record (see library/eval/README.md)",
        )
    if report.leaks:
        return CliError(
            f"blinding leak: {', '.join(report.leaks)} reached the judge; the run is void",
            ExitCode.ERROR,
        )
    if report.verdict == "UNMEASURABLE":
        return CliError(
            "no run could be scored", ExitCode.ERROR, hint=f"see {result.paths['markdown']}"
        )
    if report.verdict == "INCOMPLETE":
        return CliError(
            "the budget cap stopped the evaluation", ExitCode.STOPPED, hint="raise --budget-usd"
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
            else:
                raise ValueError(f"unknown eval event {event!r}")
        except LedgerError as error:
            raise CliError(f"the eval ledger refused {event}: {error}", ExitCode.ERROR) from error


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
        items.append({"id": opaque, "rubric": run.judge.rubric_key, "text": text})  # type: ignore[union-attr]
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
            "anchors. Score on your own; do not discuss items with the other rater. Copy "
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
    _write(
        folder / "key.json",
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
    key_path = report_dir / "calibration" / "key.json"
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
    raters = [_rater(path, set(key)) for path in score_files]
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
    result = {
        "verdict": verdict,
        "items": len(items),
        "raters": [name for name, _ in raters],
        "kappa_judge": round(kappa, 4),
        "within_1": round(within, 4),
        "kappa_raters": round(inter, 4),
        "judge_models": sorted({key[i]["judge_model"] for i in items}),
        "rubrics": sorted({key[i]["rubric"] for i in items}),
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


def _folder(given: Path | None, default: Path, flag: str, what: str) -> Path:
    if given is not None:
        return given
    if default.is_dir():
        return default
    raise CliError(f"no {what} given", ExitCode.USAGE, hint=f"pass {flag} <folder>")


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
        if not isinstance(value, int) or not 0 <= value <= 10:
            raise CliError(
                f"{path}: {item} must be a whole number 0-10, not {value!r}", ExitCode.INVALID
            )
        scores[item] = value
    return name, scores


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
