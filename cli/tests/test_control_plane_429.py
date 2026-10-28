"""A rate-limited control plane (DF-615): the control plane limits every token holder by
default and answers 429 with `Retry-After`. The CLI waits and retries a bounded number of
times when the wait is short, and refuses at once when it is long or not given; the
kill-switch poll, which a run waits on, retries at most once after a very short wait."""

from __future__ import annotations

import httpx2
import pytest
from cp_fakes import CP_URL, TOKEN, FakeControlPlane

from agentic_cli import control_plane
from agentic_cli.control_plane import ControlPlaneClient, ControlPlaneRefused


def limited(fake: FakeControlPlane, answers: list[str | None]):
    """A transport answering 429 (with each `Retry-After` in turn) before the fake."""
    pending = list(answers)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if pending:
            wait = pending.pop(0)
            headers = {"content-type": "application/problem+json"}
            if wait is not None:
                headers["Retry-After"] = wait
            body = {"status": 429, "code": "rate.limited", "detail": "too many requests"}
            return httpx2.Response(429, json=body, headers=headers)
        return fake.handle(request)

    return httpx2.MockTransport(handler)


@pytest.fixture
def slept(monkeypatch) -> list[float]:
    waits: list[float] = []
    monkeypatch.setattr(control_plane.time, "sleep", waits.append)
    return waits


def test_a_short_retry_after_is_waited_out(slept):
    client = ControlPlaneClient(CP_URL, TOKEN, transport=limited(FakeControlPlane(), ["1", "2"]))
    assert client.me().user.email == "dev@acme.example"
    assert slept == [1.0, 2.0]


def test_retries_are_bounded(slept):
    client = ControlPlaneClient(
        CP_URL, TOKEN, transport=limited(FakeControlPlane(), ["1", "1", "1", "1"])
    )
    with pytest.raises(ControlPlaneRefused) as caught:
        client.me()
    assert caught.value.status == 429 and len(slept) == control_plane.RETRIES_429


@pytest.mark.parametrize("wait", ["120", None, "soon"])
def test_a_long_or_missing_retry_after_is_refused_at_once(slept, wait):
    client = ControlPlaneClient(CP_URL, TOKEN, transport=limited(FakeControlPlane(), [wait]))
    with pytest.raises(ControlPlaneRefused) as caught:
        client.me()
    assert caught.value.status == 429 and slept == []


def test_the_kill_poll_retries_once_after_a_very_short_wait(slept):
    fake = FakeControlPlane()
    client = ControlPlaneClient(CP_URL, TOKEN, transport=limited(fake, ["1"]))
    assert client.kill_state(team="payments", repo=None, pipeline=None).switches == []
    assert slept == [1.0]
    client = ControlPlaneClient(CP_URL, TOKEN, transport=limited(fake, ["5"]))
    with pytest.raises(ControlPlaneRefused):
        client.kill_state(team="payments", repo=None, pipeline=None)
    assert slept == [1.0]
