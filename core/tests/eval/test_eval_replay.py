"""The cassette server (M1-63, spike S10 fixture mode): record model traffic once, replay
it in CI with no model. Missing cassettes and call-count mismatches fail; a changed
prompt is reported stale and still replays."""

import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from pydantic_ai import Agent

from agentic.agents.models import GatewaySettings, build_model_for
from agentic.eval.replay import CassetteServer, UpstreamReply, request_sha
from agentic.transport import CallIdentity

IDENTITY = CallIdentity(
    team="eval",
    user="eval",
    repo="synthetic",
    pipeline="eval",
    run_id="c1.X.0",
    step="eval",
    agent="business-analyst@1.0.0",
    policy_version="sha256:" + "0" * 64,
)
KEY = "c1/X/0"


def chat_reply(text: str) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "m1",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def messages_reply(text: str) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "m2",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


class Scripted:
    """An upstream that answers from a script and remembers what it was sent."""

    def __init__(self, replies: list[dict]) -> None:
        self.replies, self.seen = list(replies), []

    def __call__(self, request):
        self.seen.append(request)
        body = json.dumps(self.replies.pop(0)).encode()
        return UpstreamReply(
            200, body, "application/json", {"x-tokenlean-cost-usd": "0.0012", "set-cookie": "s=1"}
        )


def run_agent(server: CassetteServer, model_id: str, prompt: str, key: str = KEY) -> str:
    gateway = GatewaySettings(base_url=server.base_url(key), api_key="not-a-real-key")
    model = build_model_for(model_id, IDENTITY, gateway, httpx2.AsyncHTTPTransport())
    return asyncio.run(Agent(model).run(prompt)).output


@pytest.mark.parametrize(
    "model_id, reply",
    [("family-a/m1", chat_reply("hello")), ("anthropic/m2", messages_reply("hello"))],
)
def test_record_then_replay(tmp_path: Path, model_id: str, reply: dict):
    upstream = Scripted([reply])
    with CassetteServer(tmp_path, "record", upstream=upstream) as server:
        assert run_agent(server, model_id, "say hello") == "hello"
    assert server.finish() == []
    assert upstream.seen[0].path.split("?")[0] in ("/v1/chat/completions", "/v1/messages")
    cassette = tmp_path / "c1" / "X" / "0.json"
    raw = cassette.read_bytes()
    assert b"\r\n" not in raw
    assert b"not-a-real-key" not in raw and b"set-cookie" not in raw  # no secrets stored
    with CassetteServer(tmp_path, "replay") as server:
        assert run_agent(server, model_id, "say hello") == "hello"
    assert server.finish() == [] and server.stale == 0


def test_responses_route_records_and_replays(tmp_path: Path):
    body = {"id": "resp_1", "object": "response", "output": [], "usage": {}}
    with CassetteServer(tmp_path, "record", upstream=Scripted([body])) as server:
        reply = httpx2.post(server.base_url("c2/X/0") + "/v1/responses", json={"input": "hi"})
        assert reply.json()["id"] == "resp_1"
    server.finish()
    with CassetteServer(tmp_path, "replay") as server:
        reply = httpx2.post(server.base_url("c2/X/0") + "/v1/responses", json={"input": "hi"})
        assert reply.status_code == 200 and reply.json()["id"] == "resp_1"
        assert reply.headers["x-tokenlean-cost-usd"] == "0.0012"
    assert server.finish() == []


def test_a_missing_cassette_fails(tmp_path: Path):
    with CassetteServer(tmp_path, "replay") as server:
        with pytest.raises(Exception):  # noqa: B017 - the SDK's error type is not ours
            run_agent(server, "family-a/m1", "say hello", key="nope/X/0")
    assert any("no cassette for nope/X/0" in p for p in server.finish())


def test_extra_and_missing_calls_fail(tmp_path: Path):
    with CassetteServer(tmp_path, "record", upstream=Scripted([chat_reply("one")])) as server:
        run_agent(server, "family-a/m1", "first")
    server.finish()
    with CassetteServer(tmp_path, "replay") as server:
        run_agent(server, "family-a/m1", "first")
        with pytest.raises(Exception):  # noqa: B017
            run_agent(server, "family-a/m1", "a second call")
    assert any("made more calls than recorded" in p for p in server.finish())
    with CassetteServer(tmp_path, "replay") as server:
        server.expect([KEY])
    assert any("recorded 1 calls, the run made 0" in p for p in server.finish())


def test_a_changed_prompt_is_stale_but_served(tmp_path: Path):
    with CassetteServer(tmp_path, "record", upstream=Scripted([chat_reply("hi")])) as server:
        run_agent(server, "family-a/m1", "original prompt")
    server.finish()
    with CassetteServer(tmp_path, "replay") as server:
        assert run_agent(server, "family-a/m1", "an edited prompt") == "hi"
    assert server.finish() == [] and server.stale == 1


def test_the_http_upstream_asks_for_an_uncompressed_reply(monkeypatch):
    from agentic.eval import replay

    sent = {}

    class Reply:
        status = 200
        headers = {"content-type": "application/json"}

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        sent.update({k.lower(): v for k, v in request.header_items()})
        return Reply()

    monkeypatch.setattr(replay.urllib.request, "urlopen", fake_urlopen)
    forward = replay.http_upstream("http://gateway.invalid")
    headers = {"accept-encoding": "gzip, br", "authorization": "Bearer x", "host": "h"}
    forward(replay.UpstreamRequest("c/X/0", "/v1/messages", b"{}", headers))
    assert sent["accept-encoding"] == "identity"  # a gzip body could not be stored or replayed
    assert sent["authorization"] == "Bearer x" and "host" not in sent


def test_request_hash_ignores_key_order_and_volatile_fields():
    a = json.dumps({"model": "m", "messages": [], "metadata": {"user_id": "1"}}).encode()
    b = json.dumps({"messages": [], "model": "m"}).encode()
    assert request_sha(a) == request_sha(b)
    assert request_sha(b"not json") != request_sha(b)


def test_a_key_cannot_escape_the_cassette_folder(tmp_path: Path):
    with CassetteServer(tmp_path, "replay") as server:
        with pytest.raises(ValueError):
            server.base_url("../outside")
        reply = httpx2.post(server.url + "/c/..%2F..%2Fx/v1/messages", json={})
        assert reply.status_code == 400
