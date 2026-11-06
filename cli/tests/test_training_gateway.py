"""`agentic training-gateway` (M3-23): the training walkthrough's recorded answers, served
on this machine only, in order, never forwarded."""

import json
import re
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest

from agentic_cli.commands import training
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main


def post(url: str, step: str | None = None) -> tuple[int, dict]:
    headers = {"content-type": "application/json"}
    if step is not None:
        headers["x-agentic-step"] = step
    request = urllib.request.Request(
        url + "/v1/chat/completions",
        data=json.dumps({"model": "any", "messages": []}).encode("utf-8"),
        headers=headers,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def recorded() -> list[dict]:
    path = training.CASSETTES.joinpath(*training.KEY.split("/")).with_suffix(".json")
    return json.loads(path.read_text(encoding="utf-8"))["calls"]


def test_it_serves_the_recording_in_order_then_refuses(monkeypatch, capsys, home):
    calls = recorded()
    seen: dict = {}

    def walkthrough() -> None:  # what a trainee's run does while the gateway runs
        url = json.loads(capsys.readouterr().err.splitlines()[0])["gateway_url"]
        seen["url"] = url
        seen["replies"] = [post(url) for _ in range(len(calls) + 1)]
        raise KeyboardInterrupt  # Ctrl-C

    monkeypatch.setattr(training, "wait", walkthrough)
    assert main(["--json", "training-gateway"]) == ExitCode.OK
    assert seen["url"].startswith("http://127.0.0.1:")  # loopback only
    *answers, extra = seen["replies"]
    assert [body for _, body in answers] == [json.loads(c["body"]) for c in calls]
    assert extra[0] == 409  # one call more than the recording: refused, not forwarded
    result = json.loads(capsys.readouterr().out)
    assert any("more calls than recorded" in p for p in result["problems"])


def test_the_ready_message_says_how_to_point_a_run_at_it(monkeypatch, capsys, home):
    def stop() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(training, "wait", stop)
    assert main(["training-gateway"]) == ExitCode.OK
    err = capsys.readouterr().err
    assert "export AGENTIC_GATEWAY_URL=http://127.0.0.1:" in err
    assert "$env:AGENTIC_GATEWAY_URL" in err and "nothing spent" in err


@pytest.mark.parametrize("name", ["1.json", "prompts.sha256"])
def test_the_recording_ships_with_the_cli(name):
    assert (training.CASSETTES / "training" / "acceptance" / name).is_file()


def test_calls_for_another_step_than_recorded_are_counted(monkeypatch, capsys, home):
    """A training pipeline that differs from the recording still gets answers, but the
    stop says so: the call's pipeline step is not the recorded one. A different request
    body alone is not counted (every run's requests differ: run ids, paths)."""
    steps = json.loads(training.STEPS.read_text(encoding="utf-8"))

    def run(url_of) -> Callable[[], None]:
        def walk() -> None:
            url = url_of(capsys.readouterr().err)
            post(url, steps[0])  # the recorded step, another body: not counted
            post(url, "some-other-step")  # counted
            raise KeyboardInterrupt

        return walk

    monkeypatch.setattr(
        training, "wait", run(lambda err: json.loads(err.splitlines()[0])["gateway_url"])
    )
    assert main(["--json", "training-gateway"]) == ExitCode.OK
    assert json.loads(capsys.readouterr().out)["differed"] == 1

    monkeypatch.setattr(
        training, "wait", run(lambda err: err.split("export AGENTIC_GATEWAY_URL=", 1)[1].split()[0])
    )
    assert main(["training-gateway"]) == ExitCode.OK
    out = capsys.readouterr().out
    assert "1 requests differed from the recording (the training pipeline differs)" in out


def test_the_recorded_steps_ship_with_the_cassette():
    steps = json.loads(training.STEPS.read_text(encoding="utf-8"))
    assert len(steps) == len(recorded()) and steps[0] == "requirements"


# ── a fixed port (DF-998) ────────────────────────────────────────────────────


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_port_serves_on_the_port_given(monkeypatch, capsys, home):
    port, seen = free_port(), {}

    def walkthrough() -> None:
        url = json.loads(capsys.readouterr().err.splitlines()[0])["gateway_url"]
        seen["url"], seen["reply"] = url, post(url)
        raise KeyboardInterrupt

    monkeypatch.setattr(training, "wait", walkthrough)
    assert main(["--json", "training-gateway", "--port", str(port)]) == ExitCode.OK
    assert seen["url"] == f"http://127.0.0.1:{port}/c/{training.KEY}"
    assert seen["reply"][0] == 200


def test_without_port_the_system_picks_one(monkeypatch, capsys, home):
    monkeypatch.setattr(training, "wait", lambda: (_ for _ in ()).throw(KeyboardInterrupt))
    assert main(["--json", "training-gateway"]) == ExitCode.OK
    url = json.loads(capsys.readouterr().out)["gateway_url"]
    assert url.startswith("http://127.0.0.1:") and int(url.split(":")[2].split("/")[0]) > 0


def test_a_busy_port_is_a_clear_error(monkeypatch, capsys, home):
    with socket.socket() as busy:  # another program listening there (no SO_REUSEADDR)
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        monkeypatch.setattr(training, "wait", lambda: pytest.fail("served on a busy port"))
        code = main(["training-gateway", "--port", str(port)])
    err = capsys.readouterr().err
    assert code == ExitCode.ERROR, err
    assert f"port {port}" in err and "in use" in err
    assert "--port" in err  # the fix: another port, or none to let the system pick


@pytest.mark.parametrize("port", ["0", "65536", "http"])
def test_a_port_out_of_range_is_a_usage_error(home, port, capsys):
    assert main(["training-gateway", "--port", port]) == ExitCode.USAGE


TRAINING = Path(__file__).resolve().parents[2] / "docs" / "training"


def test_the_walkthrough_gives_one_fixed_line_that_matches_the_gateway():
    """DF-998: the trainee copies the same URL every time; it must be the one the
    gateway serves on the port the walkthrough starts it on."""
    text = (TRAINING / "walkthrough.md").read_text(encoding="utf-8")
    port = re.search(r"^agentic training-gateway --port (\d+)$", text, re.M)
    assert port, "the walkthrough starts the gateway on a fixed port"
    url = f"http://127.0.0.1:{port.group(1)}/c/{training.KEY}"
    assert f"export AGENTIC_GATEWAY_URL={url}\n" in text
    assert f'$env:AGENTIC_GATEWAY_URL = "{url}"' in text
    assert f"--port {port.group(1)}" in (TRAINING / "facilitator-sheet.md").read_text("utf-8")
