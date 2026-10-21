"""Text from runs, repositories and agents is printed literally: square brackets in a run
id, pipeline name, path or reason are never read as Rich markup, so `[/x]` cannot crash a
command and `[bold]x` is not swallowed (DF-218)."""

from rich.console import Console

from agentic_cli.commands.gates import _show
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.output import Output
from agentic_cli.output import table as output_table
from agentic_cli.readiness import Item, Report


def test_warn_prints_brackets_literally(capsys):
    Output(color=False).warn("run [/x] of [bold]build[/bold] failed")
    assert capsys.readouterr().err == "warning: run [/x] of [bold]build[/bold] failed\n"


def test_error_prints_brackets_and_its_hint_literally(capsys):
    error = CliError("pipeline [/x] is unknown", ExitCode.INVALID, hint="see [bold]docs")
    Output(color=False).error(error)
    assert capsys.readouterr().err == "error: pipeline [/x] is unknown\n  see [bold]docs\n"


def test_table_cells_and_plain_prints_are_literal(capsys):
    out = Output(color=False)
    shown = output_table("run", "status")
    shown.add_row("fix-[/x]-1", "[bold]paused")
    out.out.print(shown)
    out.out.print("run [/x] is [bold]done")
    printed = capsys.readouterr().out
    assert "fix-[/x]-1" in printed and "[bold]paused" in printed
    assert "run [/x] is [bold]done" in printed


def test_show_prints_a_bracketed_reason_and_path_literally(capsys):
    shown = {
        "run_id": "r-[/x]",
        "status": "paused",
        "gate": "approve-[bold]",
        "reason": "waiting on [/red] review",
        "handoffs": [{"path": "handoffs/[/x].md", "text": "plain"}],
        "diff": "",
    }
    _show(shown)(Output(color=False).out)
    printed = capsys.readouterr().out
    assert "run r-[/x] waits at approve-[bold]: waiting on [/red] review" in printed
    assert "handoffs/[/x].md" in printed


def test_emoji_codes_in_user_text_are_not_replaced(capsys):
    """A step named `build:ok:2` or a reason with `:x:` prints as written, not as emoji."""
    out = Output(color=False)
    out.out.print("run build:ok:2 failed :x: id :id:")
    shown = output_table("run")
    shown.add_row("fix:tada:1")
    out.out.print(shown)
    out.warn("reason :x:")
    out.error(CliError("step :tada: failed", ExitCode.ERROR, hint="see :id:"))
    captured = capsys.readouterr()
    assert "run build:ok:2 failed :x: id :id:" in captured.out and "fix:tada:1" in captured.out
    assert captured.err == "warning: reason :x:\nerror: step :tada: failed\n  see :id:\n"


def test_a_message_ending_in_a_backslash_keeps_one(capsys):
    """A Windows folder at the end of a warning or error is shown as it is."""
    Output(color=False).warn("cannot write C:\\work\\")
    Output(color=False).error(CliError("no policy in D:\\p\\", ExitCode.REFUSED))
    assert (
        capsys.readouterr().err == "warning: cannot write C:\\work\\\nerror: no policy in D:\\p\\\n"
    )


def test_a_readiness_name_ending_in_a_backslash_keeps_one(capsys):
    Report([Item("tool: C:\\bin\\", "ok", "found :x:")]).show(Output(color=False).out)
    assert "[ok] tool: C:\\bin\\: found :x:" in capsys.readouterr().out


def test_readiness_names_are_literal_and_marks_still_render():
    console = Console(record=True, width=200, no_color=True)
    Report([Item("tool: [/x]", "ok", "found [bold]it")]).show(console)
    text = console.export_text()
    assert "[ok] tool: [/x]: found [bold]it" in text and "Readiness: ready" in text
