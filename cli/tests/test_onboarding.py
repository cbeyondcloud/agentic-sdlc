"""The onboarding timer (M3-23): the first `agentic login`, `init`, `pipelines list` and
successful `run --dry-run` are kept on this machine only, and `agentic onboarding` prints
the time from the first login to the first successful dry run."""

import json
import os
import threading
import time
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


def test_a_dry_run_before_the_first_login_still_completes(timer_home: Path):
    """The standalone path, or a team lead: the file is frozen only once both ends exist."""
    onboarding.note(["run", "p", "--dry-run"], 0, at(0), at(1), home=timer_home)
    onboarding.note(["login"], 0, at(2), at(3), home=timer_home)
    onboarding.note(["run", "p", "--dry-run"], 0, at(4), at(5), home=timer_home)
    report = onboarding.report(home=timer_home)
    assert report["complete"] is True
    assert saved(timer_home)["steps"]["login"]["first_try"] == at(2).isoformat()


def test_the_human_output_names_the_file_from_home(timer_home: Path, capsys):
    onboarding.note(["login"], 0, at(0), at(1), home=timer_home)
    assert main(["onboarding"]) == ExitCode.OK
    text = capsys.readouterr().out
    assert "~/.agentic/onboarding.json" in text
    assert str(timer_home.parent) not in text  # no OS user name


def test_an_agentic_home_elsewhere_is_shown_from_home(home: Path, monkeypatch, capsys):
    monkeypatch.delenv("AGENTIC_ONBOARDING_TIMER", raising=False)
    monkeypatch.setenv("AGENTIC_HOME", str(home / "work" / "agentic"))
    assert main(["onboarding"]) == ExitCode.OK
    assert "~/work/agentic/onboarding.json" in capsys.readouterr().out


def test_a_failed_write_never_changes_the_exit_code(timer_home: Path, monkeypatch, capsys):
    def broken(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(onboarding, "_save", broken)
    assert main(["login", "--help"]) == ExitCode.OK  # not a step: _save never runs
    code = main(["exit-codes"])
    assert code == ExitCode.OK
    monkeypatch.setattr(onboarding, "step_of", lambda args: "login")
    assert main(["version"]) == ExitCode.OK


def test_ctrl_c_during_the_write_never_changes_the_exit_code(timer_home, monkeypatch, capsys):
    def interrupted(path, data):
        raise KeyboardInterrupt

    monkeypatch.setattr(onboarding, "_save", interrupted)
    monkeypatch.setattr(onboarding, "step_of", lambda args: "login")
    assert main(["version"]) == ExitCode.OK


def test_an_agentic_home_that_is_a_file_never_fails_a_command(home: Path, monkeypatch, capsys):
    monkeypatch.delenv("AGENTIC_ONBOARDING_TIMER", raising=False)
    blocker = home / "not-a-folder"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("AGENTIC_HOME", str(blocker))
    monkeypatch.setattr(onboarding, "step_of", lambda args: "login")
    assert main(["version"]) == ExitCode.OK


# ── writes between processes, and on a locked file (DF-996) ─────────────────


def test_two_commands_finishing_together_both_keep_their_step(timer_home: Path, monkeypatch):
    load = onboarding._load

    def slow_load(path):  # both read before either writes, unless the lock holds one off
        data = load(path)
        time.sleep(0.3)
        return data

    monkeypatch.setattr(onboarding, "_load", slow_load)
    runs = [
        threading.Thread(
            target=onboarding.note,
            args=([step], 0, at(n), at(n + 1)),
            kwargs={"home": timer_home},
        )
        for n, step in enumerate(["login", "init"])
    ]
    for run in runs:
        run.start()
    for run in runs:
        run.join()
    assert set(saved(timer_home)["steps"]) == {"login", "init"}


def leftovers(agentic_home: Path) -> list[str]:
    return sorted(p.name for p in agentic_home.iterdir() if p.name.endswith(".tmp"))


def test_a_failed_write_leaves_no_temporary_file(timer_home: Path, monkeypatch):
    def full(source, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(onboarding.os, "replace", full)
    onboarding.note(["login"], 0, at(0), at(1), home=timer_home)  # never raises
    assert leftovers(timer_home) == []
    assert not (timer_home / onboarding.FILE).exists()


def test_a_file_held_by_another_program_is_written_on_the_second_try(timer_home, monkeypatch):
    replace, tries, waits = os.replace, [], []

    def held_once(source, target):  # Windows: an antivirus scan or an indexer has it open
        tries.append(target)
        if len(tries) == 1:
            raise PermissionError(13, "Access is denied")
        replace(source, target)

    monkeypatch.setattr(onboarding.os, "replace", held_once)
    monkeypatch.setattr(onboarding.time, "sleep", waits.append)
    onboarding.note(["login"], 0, at(0), at(1), home=timer_home)
    assert len(tries) == 2 and len(waits) == 1
    assert saved(timer_home)["steps"]["login"]["first_ok"] == at(1).isoformat()
    assert leftovers(timer_home) == []


def test_a_file_still_held_after_the_retry_is_skipped_quietly(timer_home, monkeypatch, capsys):
    def held(source, target):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(onboarding.os, "replace", held)
    monkeypatch.setattr(onboarding.time, "sleep", lambda s: None)
    monkeypatch.setattr(onboarding, "step_of", lambda args: "login")
    assert main(["version"]) == ExitCode.OK
    assert leftovers(timer_home) == []
