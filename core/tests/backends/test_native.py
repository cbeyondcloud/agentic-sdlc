"""The native backend (Pydantic AI over TokenLean): conformance, dispatch and provider
neutrality (M1-28; owner rule: provider-agnostic)."""

import asyncio
import json
import re
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
from conformance import CASES, TASK, FakeContract, slice_
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agentic.agents.models import GatewaySettings, model_settings
from agentic.agents.runner import AgentTask
from agentic.backends.native import NativeBackend
from agentic.engine.engine import EngineError, StepContext
from agentic.engine.factory import StepRunners
from agentic.security import SecretScanEvent, SecretScanner
from agentic.transport import CallIdentity, LimitHit

GATEWAY = GatewaySettings(base_url="https://tokenlean.acme.internal", api_key="k")
IDENTITY = CallIdentity(
    team="payments",
    user="dev@acme.example",
    repo="acme/app",
    pipeline="feature-delivery",
    run_id="r-1",
    step="develop",
    agent="developer@1.2.0",
    policy_version="v1",
)


def backend(**changes) -> NativeBackend:
    settings = {
        "identity_for": lambda ctx: IDENTITY,
        "gateway": GATEWAY,
        "prices": lambda model: None,
        "fallback_estimate_usd": Decimal("0.01"),
    }
    return NativeBackend(**{**settings, **changes})


def step(agent, budget, contract=None) -> StepContext:
    return StepContext("r-1", "develop", 1, agent, budget, contract)


def make_runner(tmp_path):
    def make(
        agent, *, answers, budget, contract, time_limit_s=None, delay_s=0.0, over_budget=False
    ):
        replies = list(answers)

        async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if over_budget:
                raise LimitHit("step", "develop-1", Decimal("0.01"), Decimal("0.02"))
            await asyncio.sleep(delay_s)
            return ModelResponse(parts=[TextPart(replies.pop(0) if replies else "")])

        native = backend(model_for=lambda ctx: FunctionModel(respond))
        runner = native.runner(
            step(agent, budget, contract), tool_context=None, state_dir=tmp_path, spawner=None
        )
        if time_limit_s is not None:
            runner.time_limit_s = time_limit_s
        return runner

    return make


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.__name__)
def test_native_conformance(case, resolved, tmp_path):
    case(make_runner(tmp_path), resolved.model_copy(update={"tools": []}))


# ── dispatch ──────────────────────────────────────────────────────────────────


class RecordingBackend:
    name = "recording"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def runner(self, step, *, tool_context, state_dir, spawner, is_child=False):
        self.calls.append(step.agent.backend)
        return "a runner"


def runners(tmp_path, backends) -> StepRunners:
    return StepRunners(
        backends, tools_for=lambda ctx: None, state_dir_for=lambda ctx: tmp_path / ctx.step
    )


def test_step_runners_dispatch_on_the_agents_backend(resolved, tmp_path):
    recording = RecordingBackend()
    agent = resolved.model_copy(update={"backend": "claude-code", "tools": []})
    chosen = runners(tmp_path, {"native": backend(), "claude-code": recording})(
        step(agent, slice_())
    )
    assert (chosen, recording.calls) == ("a runner", ["claude-code"])


def test_an_unknown_backend_is_an_engine_error(resolved, tmp_path):
    agent = resolved.model_copy(update={"backend": "codex", "tools": []})
    with pytest.raises(EngineError, match="codex"):
        runners(tmp_path, {"native": backend()})(step(agent, slice_()))


def test_effort_reaches_model_settings_as_thinking(resolved):
    assert model_settings(resolved.model_copy(update={"effort": "high"}))["thinking"] == "high"
    assert "thinking" not in model_settings(resolved.model_copy(update={"effort": None}))


# ── provider-agnostic: any model, through TokenLean ──────────────────────────


def tokenlean(seen: list[httpx2.Request]):
    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        body = json.loads(request.content or b"{}")
        if request.url.path.endswith("/messages"):
            return httpx2.Response(
                200,
                json={
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": body.get("model"),
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "content": [{"type": "text", "text": "VALID from tokenlean"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            )
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1,
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "VALID from tokenlean"},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    return httpx2.MockTransport(answer)


@pytest.mark.parametrize(
    "model", ["anthropic/claude-sonnet", "openai/gpt-5-mini", "google/gemini-2.5-pro"]
)
def test_native_runs_any_provider_through_tokenlean(resolved, tmp_path, model):
    seen: list[httpx2.Request] = []
    agent = resolved.model_copy(update={"model": model, "tools": [], "temperature": None})
    native = backend(network_transport=tokenlean(seen))
    runner = native.runner(
        step(agent, slice_(), FakeContract()), tool_context=None, state_dir=tmp_path, spawner=None
    )
    outcome = asyncio.run(runner.run(TASK))

    assert (outcome.status, outcome.output) == ("completed", "VALID from tokenlean")
    assert seen and all(str(r.url).startswith(GATEWAY.base_url) for r in seen)
    assert all(r.headers.get("x-agentic-run-id") == "r-1" for r in seen)


def test_each_step_scans_with_the_scanner_made_for_it(resolved, tmp_path):
    # M1-41: the step's scanner (policy action, audit sink bound to the step) is the one used
    seen: list[httpx2.Request] = []
    events: list[tuple[str, SecretScanEvent]] = []

    def scanner_for(ctx: StepContext) -> SecretScanner:
        return SecretScanner("mask", on_event=lambda event: events.append((ctx.step, event)))

    agent = resolved.model_copy(update={"tools": []})
    native = backend(network_transport=tokenlean(seen), scanner_for=scanner_for)
    runner = native.runner(
        step(agent, slice_()), tool_context=None, state_dir=tmp_path, spawner=None
    )
    key = "AKIA" + "Z7Q4R2XW" + "9PLM3TKB"
    asyncio.run(runner.run(AgentTask(instructions="Dev.", inputs={}, prompt=f"use {key}")))

    assert seen and all(key.encode() not in r.content for r in seen)
    assert events[0] == ("develop", SecretScanEvent("masked", "model-call", {"AWSKeyDetector": 1}))


VENDOR = re.compile(r"claude|anthropic|openai|gemini|codex", re.IGNORECASE)


def test_no_vendor_names_outside_backend_modules():
    core = Path(__file__).resolve().parents[2] / "src" / "agentic"
    neutral = [*sorted((core / "engine").glob("*.py")), core / "backends" / "base.py"]
    for path in neutral:
        code = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        hits = [line for line in code if VENDOR.search(line) and '"""' not in line]
        assert hits == [], f"{path.name} names a vendor: {hits[:3]}"
