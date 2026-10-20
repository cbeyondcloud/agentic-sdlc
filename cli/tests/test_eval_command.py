"""`agentic eval` (M1-63): run an agent over a corpus in fixture mode (recorded responses,
no model), and calibrate the judge against people. The fixture-corpus test is the CI job
the tracker asks for: it runs on every `check.py` pass, on Windows and Linux."""

import hashlib
import json
import socket
from pathlib import Path

import pytest

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


def calibrate(*args: str) -> int:
    return main(["--json", "eval", "calibrate", *args])


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

    key = json.loads((out / "calibration" / "key.json").read_text(encoding="utf-8"))
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
    key = json.loads((out / "calibration" / "key.json").read_text(encoding="utf-8"))
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
