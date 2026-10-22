"""`agentic eval` (M1-63): run an agent over a corpus in fixture mode (recorded responses,
no model), and calibrate the judge against people. The fixture-corpus test is the CI job
the tracker asks for: it runs on every `check.py` pass, on Windows and Linux."""

import hashlib
import json
import shutil
import socket
from pathlib import Path

import pytest

from agentic.eval.corpus import write_manifest
from agentic.eval.report import EvalReport, comparable
from agentic.yamlio import dump_yaml
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main

LIBRARY = Path(__file__).resolve().parents[2] / "library" / "eval"
EXPECTED = LIBRARY / "fixtures" / "expected"
AGENTS = ["business-analyst", "design", "code-review", "qa", "developer"]


def run(tmp_path: Path, agent: str, *extra: str) -> tuple[int, Path]:
    out = tmp_path / agent
    code = main(
        [
            "--json",
            "eval",
            "run",
            "--agent",
            agent,
            "--corpus",
            str(LIBRARY),
            "--split",
            "all",
            "--mode",
            "replay",
            "--out",
            str(out),
            "--tag",
            "fixture",
            *extra,
        ]
    )
    return code, out


@pytest.fixture(autouse=True)
def in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)  # the eval's ledger goes under ./.agentic/local/


@pytest.mark.parametrize("agent", AGENTS)
def test_fixture_corpus_matches_expected_reports(tmp_path: Path, agent: str, capsys):
    code, out = run(tmp_path, agent)
    printed = json.loads(capsys.readouterr().out)
    assert code == ExitCode.OK, printed
    report = EvalReport.model_validate_json((out / "report.json").read_bytes())
    expected = json.loads((EXPECTED / f"{agent}.json").read_text(encoding="utf-8"))
    assert comparable(report) == expected
    assert report.stale_cassettes == 0 and report.verdict == "MEASURED"
    assert (out / "report.md").is_file()
    assert printed["verdict"] == "MEASURED" and printed["report"] == str(out / "report.json")


def test_fixture_developer_runs_execute_their_tests(tmp_path: Path, capsys):
    """DF-241: the Developer edits its workspace copy (recorded tool calls) and the
    case's tests then run there, so tests_pass is measured."""
    code, out = run(tmp_path, "developer")
    assert code == ExitCode.OK, capsys.readouterr().err
    report = EvalReport.model_validate_json((out / "report.json").read_bytes())
    tests_pass = report.aggregates["X"]["tests_pass"]
    assert tests_pass.value == 1.0 and tests_pass.n_scored == 2 and tests_pass.unscored == []
    assert all(r.metrics["tests_pass"] == 1.0 for r in report.runs)
    original = (LIBRARY / "workspaces" / "dev-slugify" / "textutil.py").read_text(encoding="utf-8")
    assert "unicodedata" not in original  # the corpus is never edited


def test_review_runs_measure_false_findings(tmp_path: Path, capsys):
    """DF-244: the judge rules on unmatched findings; its rulings are kept for sampling."""
    code, out = run(tmp_path, "code-review")
    assert code == ExitCode.OK, capsys.readouterr().err
    report = EvalReport.model_validate_json((out / "report.json").read_bytes())
    rate = report.aggregates["X"]["false_finding_rate"]
    assert rate.value is not None and 0 < rate.value < 1
    rulings = json.loads(
        (out / "judged" / "rev-discount-rounding.X.0.findings.json").read_text(encoding="utf-8")
    )
    assert rulings[0]["file"] == "billing/invoices.py" and rulings[0]["real"] is False


def test_fixture_mode_makes_no_network_calls(tmp_path: Path, monkeypatch, capsys):
    """DF-113: pydantic-evals, pydantic-ai and the harness reach nothing but the local
    cassette server - no telemetry, no default model."""
    attempts: list[object] = []
    real_connect = socket.socket.connect

    def guarded_connect(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            attempts.append(address)
            raise OSError("network blocked by the test")
        return real_connect(sock, address)

    real_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host not in ("127.0.0.1", "::1", "localhost", None):
            attempts.append(host)
            raise OSError("name resolution blocked by the test")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    code, _ = run(tmp_path, "business-analyst")  # agent and judge calls
    assert code == ExitCode.OK
    assert attempts == []
    with pytest.raises(OSError):  # the guard itself works
        socket.getaddrinfo("pypi.org", 443)
    assert attempts == ["pypi.org"]


def test_the_run_is_ledgered(tmp_path: Path, capsys):
    code, out = run(tmp_path, "qa")
    assert code == ExitCode.OK
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    log = tmp_path / ".agentic" / "local" / "runs" / report["report_id"] / "events.jsonl"
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [e["event"] for e in events] == [
        "run_opened",
        "step_started",
        "step_finished",
        "run_closed",
    ]
    digest = "sha256:" + hashlib.sha256((out / "report.json").read_bytes()).hexdigest()
    assert events[2]["artifacts"] == {"report.json": digest}
    assert events[2]["outcome"] == "measured" and events[0]["kind"] == "eval"


def test_rubric_runs_record_the_judge(tmp_path: Path, capsys):
    code, out = run(tmp_path, "business-analyst")
    report = EvalReport.model_validate_json((out / "report.json").read_bytes())
    assert report.judge is not None and report.judge.family == "family-b"
    assert not report.judge.same_family
    judged = [r for r in report.runs if r.judge is not None]
    assert judged and all(r.judge.rubric_key == "ba-requirements@1" for r in judged)
    text = (out / "judged" / f"{judged[0].run_id}.md").read_text(encoding="utf-8")
    assert "produced_at" not in text  # the judge never sees the envelope
    retried = next(r for r in report.runs if r.case_id == "ba-bulk-tagging")
    assert retried.requests == 2 and retried.metrics["schema_valid"] == 1.0  # one retry


def test_a_missing_cassette_fails_with_a_hint(tmp_path: Path, capsys):
    empty = tmp_path / "no-cassettes"
    empty.mkdir()
    code, _ = run(tmp_path, "qa", "--cassettes", str(empty))
    assert code == ExitCode.ERROR
    err = capsys.readouterr().err
    assert "no cassette" in err and "--mode record" in err


def test_an_unknown_agent_is_invalid(tmp_path: Path, capsys):
    code, _ = run(tmp_path, "no-such-agent")
    assert code == ExitCode.INVALID
    assert "business-analyst" in capsys.readouterr().err  # the known agents are listed


def test_live_mode_needs_a_gateway(tmp_path: Path, capsys):
    code = main(["eval", "run", "--agent", "qa", "--corpus", str(LIBRARY), "--mode", "live"])
    assert code == ExitCode.USAGE
    assert "--gateway" in capsys.readouterr().err


def corpus_copy(tmp_path: Path) -> Path:
    copy = tmp_path / "corpus"
    shutil.copytree(LIBRARY, copy)
    return copy


def run_corpus(corpus: Path, out: Path, agent: str, *extra: str) -> int:
    return main(
        [
            "--json",
            "eval",
            "run",
            "--agent",
            agent,
            "--corpus",
            str(corpus),
            "--split",
            "all",
            "--mode",
            "replay",
            "--out",
            str(out),
            *extra,
        ]
    )


def test_tag_selects_the_fixture_subset(tmp_path: Path, capsys):
    """CI replays only the cases with cassettes (tag `fixture`); the rest of the corpus is
    for live runs and has none."""
    corpus = corpus_copy(tmp_path)
    seed = corpus / "cases" / "scoping" / "ba-invoice-export.yaml"
    extra = seed.read_bytes().replace(b"id: ba-invoice-export", b"id: ba-untagged-extra")
    (corpus / "cases" / "scoping" / "ba-untagged-extra.yaml").write_bytes(
        extra.replace(b"fixture, ", b"").replace(b", fixture", b"").replace(b"[fixture]", b"[]")
    )
    write_manifest(corpus)
    out = tmp_path / "rep"
    assert run_corpus(corpus, out, "business-analyst", "--tag", "fixture") == ExitCode.OK
    report = EvalReport.model_validate_json((out / "report.json").read_bytes())
    assert "ba-untagged-extra" not in report.cases and report.tag == "fixture"
    capsys.readouterr()
    assert run_corpus(corpus, tmp_path / "x", "business-analyst", "--tag", "nope") == (
        ExitCode.INVALID
    )
    assert "fixture" in capsys.readouterr().err  # the known tags are listed


def gateway_policy(tmp_path: Path) -> Path:
    """The fixture policy with both tiers behind one gateway prefix and no `family:`."""
    policy = tmp_path / "gw-policy"
    shutil.copytree(LIBRARY / "fixtures" / "policy", policy)
    tiers = policy / "tiers.yaml"
    text = tiers.read_text(encoding="utf-8").replace("family-a/", "gateway/")
    tiers.write_text(text.replace("family-b/", "gateway/"), encoding="utf-8")
    return policy


def test_a_shared_gateway_prefix_is_warned_and_recorded(tmp_path: Path, capsys):
    """DF-302: preflight warns when the judge and the agents share a prefix that may be a
    gateway, with no `family:` set; the report keeps the warning."""
    policy = gateway_policy(tmp_path)
    code, out = run(tmp_path, "business-analyst", "--policy", str(policy))
    printed = json.loads(capsys.readouterr().out)
    assert code == ExitCode.OK
    assert any("'gateway/'" in w and "family:" in w for w in printed["warnings"])
    report = EvalReport.model_validate_json((out / "report.json").read_bytes())
    assert report.judge.warnings == printed["warnings"] and report.judge.same_family
    human = main(
        [
            "eval",
            "run",
            "--agent",
            "business-analyst",
            "--corpus",
            str(LIBRARY),
            "--split",
            "all",
            "--mode",
            "replay",
            "--tag",
            "fixture",
            "--policy",
            str(policy),
            "--out",
            str(tmp_path / "human"),
        ]
    )
    assert human == ExitCode.OK
    assert "warning:" in capsys.readouterr().err


def ledger_events(tmp_path: Path) -> list[dict]:
    (log,) = (tmp_path / ".agentic" / "local" / "runs").glob("*/events.jsonl")
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_a_failure_after_the_runs_closes_the_ledger(tmp_path: Path, monkeypatch):
    """DF-260: whatever raises after the eval opened its ledger run, the run is closed."""
    from agentic_cli import evaluation

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(evaluation, "write_report", broken)
    options = evaluation.RunOptions(
        agent="qa", corpus=LIBRARY, split="all", mode="replay", tag="fixture", out=tmp_path / "o"
    )
    with pytest.raises(OSError, match="disk full"):
        evaluation.execute_run(options)
    events = ledger_events(tmp_path)
    assert events[-1]["event"] == "run_closed" and events[-1]["status"] == "error"


def test_the_budget_cap_in_replay_is_incomplete_not_a_broken_replay(tmp_path: Path, capsys):
    """DF-254: runs the cap skipped made no calls; that is INCOMPLETE (exit 5), not a
    replay to re-record (exit 1)."""
    code, out = run(tmp_path, "qa", "--budget-usd", "0.01")
    err = capsys.readouterr().err
    assert code == ExitCode.STOPPED, err
    report = EvalReport.model_validate_json((out / "report.json").read_bytes())
    assert report.verdict == "INCOMPLETE" and report.problems == []
    assert "re-record" not in err


def test_a_judge_that_failed_everywhere_is_not_called_a_replay_failure(tmp_path: Path):
    """DF-254: only cassette problems get the re-record hint."""
    from agentic_cli import evaluation

    _, out = run(tmp_path, "business-analyst")
    report = EvalReport.model_validate_json((out / "report.json").read_bytes())
    judged_out = report.model_copy(
        update={"verdict": "UNMEASURABLE", "problems": ["X: no case got a rubric score"]}
    )
    paths = {"json": out / "report.json", "markdown": out / "report.md"}
    error = evaluation.failure(evaluation.RunResult(judged_out, out, paths))
    assert "replay" not in str(error) and "rubric score" in str(error)
    assert "re-record" not in (error.hint or "") and error.code == ExitCode.ERROR
    replayed = evaluation.RunResult(
        judged_out.model_copy(update={"problems": ["no cassette for x: record it"]}),
        out,
        paths,
        replay_problems=["no cassette for x: record it"],
    )
    assert "re-record" in evaluation.failure(replayed).hint


def test_a_policy_folder_that_does_not_exist_is_named(tmp_path: Path, capsys):
    """DF-259: not 'tiers: Field required'."""
    code, _ = run(tmp_path, "qa", "--policy", str(tmp_path / "nowhere"))
    assert code == ExitCode.USAGE  # a wrong argument (exit-code table), not a bad policy
    err = capsys.readouterr().err
    assert "nowhere" in err and "not a folder" in err and "Field required" not in err


def test_no_policy_given_reads_plainly(tmp_path: Path, capsys):
    corpus = corpus_copy(tmp_path)
    shutil.rmtree(corpus / "fixtures" / "policy")
    assert run_corpus(corpus, tmp_path / "o", "qa", "--tag", "fixture") == ExitCode.USAGE
    err = capsys.readouterr().err
    assert "no policy folder given" in err and "no the" not in err


def test_a_readme_in_the_cases_folder_is_allowed(tmp_path: Path, capsys):
    corpus = corpus_copy(tmp_path)
    (corpus / "cases" / "README.md").write_text("# Cases\n", encoding="utf-8")
    write_manifest(corpus)
    assert run_corpus(corpus, tmp_path / "o", "qa", "--tag", "fixture") == ExitCode.OK


def test_a_single_variant_report_shows_no_variant_label(tmp_path: Path, capsys):
    """DF-259: 'X' means something only in an A/B."""
    out = tmp_path / "human"
    code = main(
        [
            "eval",
            "run",
            "--agent",
            "qa",
            "--corpus",
            str(LIBRARY),
            "--split",
            "all",
            "--mode",
            "replay",
            "--tag",
            "fixture",
            "--out",
            str(out),
        ]
    )
    assert code == ExitCode.OK
    shown = capsys.readouterr().out
    assert "  qa_catch:" in shown and " X " not in shown
    markdown = (out / "report.md").read_text(encoding="utf-8")
    assert "## Metrics\n" in markdown and "## Metrics: X" not in markdown


def test_live_calls_carry_the_users_team(tmp_path: Path, monkeypatch):
    """DF-248: TokenLean attributes eval spend to the person's team, not to 'eval'."""
    from agentic.eval.replay import UpstreamReply
    from agentic_cli import evaluation

    monkeypatch.setenv("AGENTIC_TEAM", "payments")
    seen: list[str] = []
    answer = (LIBRARY / "fixtures" / "answers" / "qa-invoice-export.md").read_text(encoding="utf-8")

    def upstream(request):
        seen.append(request.headers.get("x-agentic-team") or request.headers.get("X-Agentic-Team"))
        body = json.loads(request.body)
        reply = {
            "id": "c",
            "object": "chat.completion",
            "created": 1,
            "model": body["model"],
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": answer},
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        return UpstreamReply(200, json.dumps(reply).encode(), "application/json")

    options = evaluation.RunOptions(
        agent="qa",
        corpus=LIBRARY,
        split="all",
        mode="record",
        tag="fixture",
        out=tmp_path / "o",
        cassettes=tmp_path / "cassettes",
    )
    evaluation.execute_run(options, upstream=upstream)
    assert seen and set(seen) == {"payments"}


def test_eval_gate_compares_reports_with_targets(tmp_path: Path, capsys):
    """DF-243 (gate half): `eval gate` writes gate.json/gate.md; fixture reports (replay, a
    tag subset) are named as not a gate (exit 3), never passed."""
    _, ba = run(tmp_path, "business-analyst")
    _, qa = run(tmp_path, "qa")
    capsys.readouterr()
    out = tmp_path / "gate"
    code = main(
        [
            "--json",
            "eval",
            "gate",
            "--report",
            str(ba),
            "--report",
            str(qa),
            "--agent",
            "business-analyst",
            "--agent",
            "qa",
            "--out",
            str(out),
        ]
    )
    assert code == ExitCode.INVALID
    printed = json.loads(capsys.readouterr().out)
    assert printed["verdict"] == "NOT_A_GATE"
    gate = json.loads((out / "gate.json").read_text(encoding="utf-8"))
    assert {r["agent"] for r in gate["rows"]} == {"business-analyst", "qa"}
    assert any("live" in p for p in gate["problems"])
    assert "Why this is not a gate" in (out / "gate.md").read_text(encoding="utf-8")


def test_eval_gate_needs_a_report(tmp_path: Path, capsys):
    assert main(["eval", "gate", "--report", str(tmp_path / "none")]) == ExitCode.INVALID
    assert "report.json" in capsys.readouterr().err


def test_a_rubric_id_cannot_leave_the_corpus(tmp_path: Path, capsys):
    """F1: `rubric: ../../outside` must neither read nor write outside the corpus."""
    corpus = corpus_copy(tmp_path / "t")
    rubric = (corpus / "rubrics" / "ba-requirements.yaml").read_bytes()
    (tmp_path / "t" / "outside.yaml").write_bytes(rubric)
    case = corpus / "cases" / "scoping" / "ba-bulk-tagging.yaml"
    case.write_bytes(
        case.read_bytes().replace(b"rubric: ba-requirements", b"rubric: ../../outside")
    )
    write_manifest(corpus)
    out = tmp_path / "t" / "out" / "rep"
    code = run_corpus(corpus, out, "business-analyst")
    assert code == ExitCode.INVALID, capsys.readouterr()
    assert "rubric" in capsys.readouterr().err
    assert not (tmp_path / "t" / "out" / "outside.yaml").exists()
    assert not out.exists()


def restricted_policy(tmp_path: Path) -> Path:
    policy = tmp_path / "policy"
    shutil.copytree(LIBRARY / "fixtures" / "policy", policy)
    path = policy / "classifications.yaml"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "  restricted:\n    allowed_tiers: [balanced]\n    allowed_backends: [native]\n",
        encoding="utf-8",
    )
    return policy


def test_a_class_without_the_judge_tier_is_refused_before_any_call(tmp_path: Path, capsys):
    """F6: restricted cases never reach the eval-judge model."""
    policy = restricted_policy(tmp_path)
    out = tmp_path / "rep"
    code = run_corpus(
        LIBRARY, out, "business-analyst", "--policy", str(policy), "--classification", "restricted"
    )
    assert code == ExitCode.REFUSED
    assert "restricted" in capsys.readouterr().err
    assert not out.exists()  # refused before any run or judge call


def classify(corpus: Path, agent: str, classification: str, only: str | None = None) -> None:
    source = b"source: {repo: synthetic, synthetic: true, licence: CC0-1.0}"
    labelled = source[:-1] + b", classification: " + classification.encode() + b"}"
    changed = 0
    for case in sorted((corpus / "cases").rglob("*.yaml")):
        text = case.read_bytes()
        if f"agent: {agent}\n".encode() in text and (only is None or case.stem == only):
            assert source in text, case
            case.write_bytes(text.replace(source, labelled))
            changed += 1
    assert changed
    write_manifest(corpus)


def test_case_classifications_select_the_class(tmp_path: Path, capsys):
    corpus = corpus_copy(tmp_path)
    classify(corpus, "business-analyst", "restricted")
    policy = restricted_policy(tmp_path)
    out = tmp_path / "rep"
    assert run_corpus(corpus, out, "business-analyst", "--policy", str(policy)) == (
        ExitCode.REFUSED
    )
    assert "restricted" in capsys.readouterr().err
    code = run_corpus(
        corpus, out, "business-analyst", "--policy", str(policy), "--classification", "internal"
    )
    assert code == ExitCode.INVALID  # the flag cannot relabel a restricted case
    assert "restricted" in capsys.readouterr().err
    assert not out.exists()


def test_cases_of_different_classes_are_not_run_together(tmp_path: Path, capsys):
    corpus = corpus_copy(tmp_path)
    classify(corpus, "business-analyst", "restricted", only="ba-bulk-tagging")
    out = tmp_path / "rep"
    policy = restricted_policy(tmp_path)
    assert run_corpus(corpus, out, "business-analyst", "--policy", str(policy)) == (
        ExitCode.INVALID
    )
    err = capsys.readouterr().err
    assert "internal" in err and "restricted" in err
    assert not out.exists()


def calibrate(*args: str) -> int:
    return main(["--json", "eval", "calibrate", *args])


def rater_files(tmp_path: Path, out: Path, names=("lead-a", "lead-b")) -> list[Path]:
    key = json.loads((out / "calibration-key.json").read_text(encoding="utf-8"))
    paths = []
    for i, name in enumerate(names):
        path = tmp_path / f"rater-{i}.yaml"
        scores = {item: int(e["judge_total"]) for item, e in key["items"].items()}
        path.write_text(dump_yaml({"rater": name, "scores": scores}), encoding="utf-8")
        paths.append(path)
    return paths


def test_raters_see_what_the_judge_saw_and_never_its_totals(tmp_path: Path, capsys):
    """DF-257: kappa(judge, human) compares like with like - raters get the reference and
    key points the judge got - and the folder handed to raters holds no answer key."""
    from agentic.yamlio import load_yaml

    _, out = run(tmp_path, "business-analyst")
    assert calibrate("export", "--report", str(out)) == ExitCode.OK
    pack = load_yaml((out / "calibration" / "pack.yaml").read_text(encoding="utf-8"))[0]
    with_golden = [i for i in pack["items"] if i.get("reference")]
    assert with_golden and all("## " in i["reference"] for i in with_golden)
    assert all(i["key_points"] for i in pack["items"])
    handed_out = {p.name for p in (out / "calibration").iterdir()}
    assert handed_out == {"pack.yaml", "scores-template.yaml"}
    assert (out / "calibration-key.json").is_file()


def test_calibration_needs_two_distinct_raters(tmp_path: Path, capsys):
    """F5: one person's scores, passed twice or under one name, are not two raters."""
    _, out = run(tmp_path, "business-analyst")
    calibrate("export", "--report", str(out))
    capsys.readouterr()
    one, _ = rater_files(tmp_path, out)
    assert calibrate("score", "--report", str(out), "--scores", str(one), "--scores", str(one)) == (
        ExitCode.USAGE
    )
    assert "same file" in capsys.readouterr().err
    first, second = rater_files(tmp_path, out, names=("one", "One "))
    code = calibrate("score", "--report", str(out), "--scores", str(first), "--scores", str(second))
    assert code == ExitCode.INVALID
    assert "two different raters" in capsys.readouterr().err
    assert not (out / "calibration" / "result.json").exists()


def test_a_yes_no_score_is_not_a_number(tmp_path: Path, capsys):
    _, out = run(tmp_path, "business-analyst")
    calibrate("export", "--report", str(out))
    first, second = rater_files(tmp_path, out)
    data = yaml_scores(first)
    data["scores"][next(iter(data["scores"]))] = True
    first.write_text(dump_yaml(data), encoding="utf-8")
    capsys.readouterr()
    code = calibrate("score", "--report", str(out), "--scores", str(first), "--scores", str(second))
    assert code == ExitCode.INVALID
    assert "whole number" in capsys.readouterr().err


def yaml_scores(path: Path) -> dict:
    from agentic.yamlio import load_yaml

    return load_yaml(path.read_text(encoding="utf-8"))[0]


def test_calibration_result_names_what_it_calibrated(tmp_path: Path, capsys):
    """F5: a gate must be able to match a calibration to a rubric sha and judge model."""
    _, out = run(tmp_path, "business-analyst")
    calibrate("export", "--report", str(out))
    first, second = rater_files(tmp_path, out)
    capsys.readouterr()
    calibrate("score", "--report", str(out), "--scores", str(first), "--scores", str(second))
    result = json.loads((out / "calibration" / "result.json").read_text(encoding="utf-8"))
    report = EvalReport.model_validate_json((out / "report.json").read_bytes())
    assert result["report_id"] == report.report_id
    assert result["rubric_shas"] == sorted(report.judge.rubrics.values())
    assert result["judge_models"] == [report.judge.model]


def test_calibration_export_is_blind_and_scoring_accepts_an_agreeing_judge(tmp_path: Path, capsys):
    _, out = run(tmp_path, "business-analyst")
    capsys.readouterr()
    assert calibrate("export", "--report", str(out)) == ExitCode.OK
    exported = json.loads(capsys.readouterr().out)
    pack = (out / "calibration" / "pack.yaml").read_text(encoding="utf-8")
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    for run_ in report["runs"]:
        assert run_["run_id"] not in pack  # opaque ids only
    assert "family-a" not in pack and "family-b" not in pack and ".X." not in pack
    assert exported["items"] == 8  # all 8 judged runs: fewer than the minimum of 10

    key = json.loads((out / "calibration-key.json").read_text(encoding="utf-8"))
    rater = {item: entry["judge_total"] for item, entry in key["items"].items()}
    files = []
    for name, shift in (("lead-a", 0), ("lead-b", 1)):
        path = tmp_path / f"{name}.yaml"
        scores = {
            item: min(10, int(total) + shift * (i % 2))
            for i, (item, total) in enumerate(rater.items())
        }
        path.write_text(dump_yaml({"rater": name, "scores": scores}), encoding="utf-8")
        files += ["--scores", str(path)]
    assert calibrate("score", "--report", str(out), *files) == ExitCode.OK
    result = json.loads(capsys.readouterr().out)
    assert result["verdict"] == "ACCEPTED" and result["kappa_judge"] >= 0.6


def test_raters_who_disagree_mean_the_rubric_is_ambiguous(tmp_path: Path, capsys):
    _, out = run(tmp_path, "business-analyst")
    calibrate("export", "--report", str(out))
    key = json.loads((out / "calibration-key.json").read_text(encoding="utf-8"))
    items = sorted(key["items"])
    files = []
    for name, scores in (
        ("lead-a", {item: (i * 3) % 11 for i, item in enumerate(items)}),
        ("lead-b", {item: (i * 7 + 5) % 11 for i, item in enumerate(items)}),
    ):
        path = tmp_path / f"{name}.yaml"
        path.write_text(dump_yaml({"rater": name, "scores": scores}), encoding="utf-8")
        files += ["--scores", str(path)]
    capsys.readouterr()
    assert calibrate("score", "--report", str(out), *files) == ExitCode.REFUSED
    assert json.loads(capsys.readouterr().out)["verdict"] == "RUBRIC_AMBIGUOUS"


# --- M1 close-out (DF-254, DF-259, DF-260, DF-302) ---


def test_a_failure_while_writing_the_report_closes_the_ledger_run(
    tmp_path: Path, monkeypatch, capsys
):
    """DF-260: the report step can fail too (a full disk); the ledger run never stays open."""
    from agentic_cli import evaluation

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(evaluation, "write_report", full_disk)
    code, _ = run(tmp_path, "qa")
    assert code == ExitCode.ERROR
    [log] = (tmp_path / ".agentic" / "local" / "runs").glob("eval-qa-*/events.jsonl")
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event"] == "run_closed"


def test_a_missing_policy_folder_is_a_usage_error(tmp_path: Path, capsys):
    """DF-259: not 'tiers: Field required' from loading a folder that is not there."""
    code, _ = run(tmp_path, "qa", "--policy", str(tmp_path / "no-such-policy"))
    assert code == ExitCode.USAGE
    error = json.loads(capsys.readouterr().err)
    assert "--policy" in error["error"] and "no-such-policy" in error["error"]


def test_no_policy_folder_reads_as_a_sentence(tmp_path: Path, capsys):
    """DF-259: the message once said 'no the policy folder given'."""
    corpus = corpus_copy(tmp_path)
    shutil.rmtree(corpus / "fixtures" / "policy")
    assert run_corpus(corpus, tmp_path / "out", "qa") == ExitCode.USAGE
    error = json.loads(capsys.readouterr().err)
    assert "no the" not in error["error"] and "--policy" in error["hint"]


def failed(**report) -> object:
    from types import SimpleNamespace

    fields = {"problems": [], "leaks": [], "verdict": "MEASURED", "mode": "replay"}
    fields["budget_hit"] = False
    return SimpleNamespace(
        report=SimpleNamespace(**{**fields, **report}),
        paths={"markdown": Path("report.md")},
        replay_problems=[],
    )


def test_a_live_problem_is_not_called_a_replay_failure():
    """DF-254: 'the replay failed ... re-record' was said for every problem, live too."""
    from agentic_cli.evaluation import failure

    error = failure(failed(mode="live", problems=["the judge failed on every run"]))
    assert error is not None and "replay" not in str(error)
    assert "record" not in (error.hint or "")


def test_a_budget_stop_is_exit_5_even_with_missing_calls():
    """DF-254: in replay, the cap leaves later calls unasked; that is INCOMPLETE (exit 5),
    not a broken cassette (exit 1)."""
    from agentic_cli.evaluation import failure

    error = failure(failed(budget_hit=True, verdict="INCOMPLETE", problems=["1 call never made"]))
    assert error is not None and error.code == ExitCode.STOPPED
