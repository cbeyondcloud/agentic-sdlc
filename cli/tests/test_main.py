"""The CLI skeleton (M1-49): command registry, output conventions, exit codes, config."""

import json
from pathlib import Path

import pytest
import typer

from agentic_cli import __version__
from agentic_cli.config import agentic_home, user_config
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import EXIT_CODE_TABLE, ExitCode
from agentic_cli.main import build_app, main
from agentic_cli.output import Output


def test_the_exit_code_table_is_complete_and_distinct():
    assert {code for code, _ in EXIT_CODE_TABLE} == set(ExitCode)
    assert len({int(code) for code in ExitCode}) == len(ExitCode)
    assert ExitCode.OK == 0 and ExitCode.REFUSED == 2  # spec §24: init refuses with 2
    assert ExitCode.USAGE == 64 and ExitCode.INTERRUPTED == 130
    assert all(meaning.strip() for _, meaning in EXIT_CODE_TABLE)


def test_help_lists_the_registered_commands(capsys):
    assert main(["--help"]) == ExitCode.OK
    shown = capsys.readouterr().out
    assert "version" in shown and "exit-codes" in shown


def test_version_prints_the_package_version(capsys):
    assert main(["version"]) == ExitCode.OK
    assert __version__ in capsys.readouterr().out


def test_json_output_is_machine_readable(capsys):
    assert main(["--json", "version"]) == ExitCode.OK
    assert json.loads(capsys.readouterr().out) == {"version": __version__}


def test_exit_codes_command_prints_the_table(capsys):
    assert main(["--json", "exit-codes"]) == ExitCode.OK
    table = json.loads(capsys.readouterr().out)
    assert table["2"].startswith("Refused") and len(table) == len(ExitCode)


def test_a_usage_error_exits_64_not_click_s_2(capsys):
    assert main(["no-such-command"]) == ExitCode.USAGE
    assert main(["version", "--no-such-flag"]) == ExitCode.USAGE
    assert "Usage" in capsys.readouterr().err


def _app_with(handler):
    app = build_app()

    @app.command("boom")
    def boom(ctx: typer.Context) -> None:
        handler(ctx.obj)

    return app


def test_a_cli_error_maps_to_its_exit_code_and_goes_to_stderr(capsys):
    def refuse(_out: Output) -> None:
        raise CliError("Not a git repository", ExitCode.REFUSED, hint="cd into a repo")

    assert main(["boom"], app=_app_with(refuse)) == ExitCode.REFUSED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Not a git repository" in captured.err and "cd into a repo" in captured.err


def test_a_cli_error_in_json_mode_is_a_json_object_on_stderr(capsys):
    def refuse(_out: Output) -> None:
        raise CliError("Policy violation", ExitCode.INVALID)

    assert main(["--json", "boom"], app=_app_with(refuse)) == ExitCode.INVALID
    error = json.loads(capsys.readouterr().err)
    assert error == {"error": "Policy violation", "exit_code": 3, "hint": None}


def test_an_unexpected_exception_is_exit_1_without_a_traceback(capsys):
    def crash(_out: Output) -> None:
        raise RuntimeError("disk on fire")

    assert main(["boom"], app=_app_with(crash)) == ExitCode.ERROR
    err = capsys.readouterr().err
    assert "disk on fire" in err and "Traceback" not in err


def test_ctrl_c_is_exit_130(capsys):
    def interrupt(_out: Output) -> None:
        raise KeyboardInterrupt

    assert main(["boom"], app=_app_with(interrupt)) == ExitCode.INTERRUPTED


def test_agentic_home_defaults_to_the_user_folder_and_can_be_overridden(tmp_path: Path):
    assert agentic_home({}, home=tmp_path) == tmp_path / ".agentic"
    assert agentic_home({"AGENTIC_HOME": str(tmp_path / "x")}, home=tmp_path) == tmp_path / "x"


def test_user_config_is_empty_when_missing_and_read_when_present(tmp_path: Path):
    assert user_config(tmp_path) == {}
    (tmp_path / "config.yaml").write_text("org: acme\n", encoding="utf-8")
    assert user_config(tmp_path) == {"org": "acme"}


def test_a_broken_user_config_is_an_invalid_error_naming_the_file(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("org: [unclosed\n", encoding="utf-8")
    with pytest.raises(CliError) as caught:
        user_config(tmp_path)
    assert caught.value.code == ExitCode.INVALID and "config.yaml" in str(caught.value)
