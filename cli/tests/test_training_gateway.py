"""`agentic training-gateway` (M3-23): the training walkthrough's recorded answers, served
on this machine only, in order, never forwarded."""

import json
import urllib.error
import urllib.request
from collections.abc import Callable

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
