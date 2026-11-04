"""The onboarding timer (M3-23): the first `agentic login`, `init`, `pipelines list` and
successful `run --dry-run` are kept on this machine only, and `agentic onboarding` prints
the time from the first login to the first successful dry run."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentic_cli import onboarding
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main

T0 = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


@pytest.fixture
def timer_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("AGENTIC_ONBOARDING_TIMER", raising=False)
    return home / ".agentic"


def saved(agentic_home: Path) -> dict:
    return json.loads((agentic_home / onboarding.FILE).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("args", "step"),
    [
        (["login", "--invite", "https://x/invite#a"], "login"),
        (["--json", "login"], "login"),
        (["login", "--for", "github"], None),  # the git host, not the sign-in
        (["init"], "init"),
        (["init", "--install-missing"], "init"),
        (["init", "--repo"], None),  # the team lead's set-up of the repository
        (["pipelines", "list"], "pipelines"),
        (["run", "feature-delivery", "--input", "request=x", "--dry-run"], "dry-run"),
        (["run", "feature-delivery", "--input", "request=x"], None),
        (["init", "--help"], None),
        (["--version"], None),
        ([], None),
        (["onboarding"], None),
    ],
)
def test_which_command_is_a_step(args, step):
    assert onboarding.step_of(args) == step


def test_the_first_try_and_the_first_success_are_kept(timer_home: Path):
    onboarding.note(["login"], 1, at(0), at(0.5), home=timer_home)  # a failed first try
    onboarding.note(["login"], 0, at(2), at(3), home=timer_home)
    onboarding.note(["login"], 0, at(9), at(10), home=timer_home)  # later ones change nothing
    login = saved(timer_home)["steps"]["login"]
    assert login == {"first_try": at(0).isoformat(), "first_ok": at(3).isoformat()}


def test_the_report_times_each_step_from_the_one_before(timer_home: Path):
    for args, start, end in [
        (["login", "--invite", "x"], 0, 1),
        (["init"], 2, 9),
        (["pipelines", "list"], 10, 10.5),
        (["run", "feature-delivery", "--dry-run"], 11, 12.5),
    ]:
        onboarding.note(args, 0, at(start), at(end), home=timer_home)
    report = onboarding.report(home=timer_home)
    assert report["complete"] is True
    assert report["total_seconds"] == 750  # 12.5 min, first login try to the dry run
    assert [(s["step"], s["seconds"]) for s in report["steps"]] == [
        ("login", 60),
        ("init", 480),
        ("pipelines", 90),
        ("dry-run", 120),
    ]


def test_nothing_is_written_once_the_dry_run_has_passed(timer_home: Path):
    onboarding.note(["login"], 0, at(0), at(1), home=timer_home)
    onboarding.note(["run", "p", "--dry-run"], 0, at(2), at(3), home=timer_home)
    before = (timer_home / onboarding.FILE).read_bytes()
    onboarding.note(["init"], 0, at(5), at(6), home=timer_home)
    assert (timer_home / onboarding.FILE).read_bytes() == before


def test_an_unfinished_onboarding_names_the_next_step(timer_home: Path):
    onboarding.note(["login"], 0, at(0), at(1), home=timer_home)
    report = onboarding.report(home=timer_home)
    assert report["complete"] is False and report["total_seconds"] is None
    assert report["next"] == "agentic init"


def test_nothing_is_recorded_in_ci_or_when_turned_off(timer_home: Path):
    onboarding.note(["login"], 0, at(0), at(1), home=timer_home, env={"CI": "true"})
    onboarding.note(["login"], 0, at(0), at(1), home=timer_home, env={"GITHUB_ACTIONS": "1"})
    off = {"AGENTIC_ONBOARDING_TIMER": "off"}
    onboarding.note(["login"], 0, at(0), at(1), home=timer_home, env=off)
    assert not (timer_home / onboarding.FILE).exists()


def test_a_broken_file_never_fails_a_command(timer_home: Path):
    (timer_home / onboarding.FILE).mkdir(parents=True)  # cannot be read or replaced
    onboarding.note(["login"], 0, at(0), at(1), home=timer_home)  # no exception
    assert onboarding.report(home=timer_home)["steps"] == []


def test_a_file_that_is_not_json_is_started_again(timer_home: Path):
    timer_home.mkdir(parents=True, exist_ok=True)
    (timer_home / onboarding.FILE).write_text("{not json", encoding="utf-8")
    onboarding.note(["login"], 0, at(0), at(1), home=timer_home)
    assert saved(timer_home)["steps"]["login"]["first_ok"] == at(1).isoformat()


def test_main_records_the_steps_a_person_runs(timer_home: Path, capsys):
    assert main(["version"]) == ExitCode.OK  # not a step: nothing written
    assert not (timer_home / onboarding.FILE).exists()
    code = main(["pipelines", "list"])
    pipelines = saved(timer_home)["steps"]["pipelines"]
    assert pipelines["first_try"] and bool(pipelines["first_ok"]) == (code == ExitCode.OK)


def test_the_command_prints_the_times_and_says_nothing_is_sent(timer_home: Path, capsys):
    onboarding.note(["login"], 0, at(0), at(1), home=timer_home)
    onboarding.note(["init"], 0, at(1), at(4), home=timer_home)
    onboarding.note(["pipelines", "list"], 0, at(4), at(5), home=timer_home)
    onboarding.note(["run", "p", "--dry-run"], 0, at(5), at(6), home=timer_home)
    assert main(["onboarding"]) == ExitCode.OK
    text = capsys.readouterr().out
    assert "6 min 0 s" in text and "agentic init" in text and "nothing is sent" in text
    assert main(["--json", "onboarding"]) == ExitCode.OK
    data = json.loads(capsys.readouterr().out)
    assert data["complete"] is True and data["total_seconds"] == 360


def test_reset_starts_the_timer_again(timer_home: Path, capsys):
    onboarding.note(["login"], 0, at(0), at(1), home=timer_home)
    assert main(["onboarding", "--reset"]) == ExitCode.OK
    assert not (timer_home / onboarding.FILE).exists()
    assert "reset" in capsys.readouterr().out


def test_steps_run_out_of_order_still_add_up_to_the_total(timer_home: Path):
    """Each step is timed from the success just before it, whichever step that was."""
    for args, start, end in [
        (["login"], 0, 3),
        (["pipelines", "list"], 4, 5),  # before init
        (["init"], 6, 10),
        (["run", "p", "--dry-run"], 11, 12),
    ]:
        onboarding.note(args, 0, at(start), at(end), home=timer_home)
    report = onboarding.report(home=timer_home)
    seconds = {s["step"]: s["seconds"] for s in report["steps"]}
    assert seconds == {"login": 180, "pipelines": 120, "init": 300, "dry-run": 120}
    assert sum(seconds.values()) == report["total_seconds"] == 720


def test_an_entry_that_is_not_an_object_is_dropped(timer_home: Path, capsys):
    timer_home.mkdir(parents=True, exist_ok=True)
    (timer_home / onboarding.FILE).write_text('{"steps": {"login": "x"}}', encoding="utf-8")
    assert onboarding.report(home=timer_home)["steps"] == []
    onboarding.note(["login"], 0, at(0), at(1), home=timer_home)
    assert saved(timer_home)["steps"]["login"]["first_ok"] == at(1).isoformat()


def test_the_json_report_names_no_path(timer_home: Path, capsys):
    onboarding.note(["login"], 0, at(0), at(1), home=timer_home)
    assert main(["--json", "onboarding"]) == ExitCode.OK
    assert "file" not in json.loads(capsys.readouterr().out)  # pasted into issues
