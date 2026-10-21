"""How a claude-code step is charged when Claude Code's own totals are missing or its
model is unpriced (M1-81, DF-77). Driven with SDK message objects, no Claude Code run."""

import asyncio
from decimal import Decimal

import pytest
from conformance import slice_
from test_claude_code_contract import IDENTITY, PRICE

from agentic.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRunner, ClaudeCodeSettings
from agentic.engine.engine import StepContext

sdk = pytest.importorskip("claude_agent_sdk")


class FakeClient:
    def __init__(self, messages) -> None:
        self.messages = messages

    async def query(self, prompt: str) -> None:
        return None

    async def receive_response(self):
        for message in self.messages:
            yield message


def block(message_id: str, text: str, **usage) -> "sdk.AssistantMessage":
    return sdk.AssistantMessage(
        content=[sdk.TextBlock(text)], model="m", usage=usage, message_id=message_id
    )


def result(usage: dict | None) -> "sdk.ResultMessage":
    return sdk.ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        usage=usage,
        result="done",
    )


def runner(resolved, *, price=PRICE, fallback=Decimal("0.01")) -> ClaudeCodeRunner:
    agent = resolved.model_copy(update={"backend": "claude-code", "temperature": None})
    return ClaudeCodeRunner(
        agent,
        options={},
        budget=slice_(),
        price_for=lambda model: price,
        contract=None,
        time_limit_s=None,
        fallback_estimate_usd=fallback,
    )


def turn(r: ClaudeCodeRunner, messages) -> None:
    asyncio.run(r._turn(FakeClient(messages), "go"))


def test_streamed_usage_counts_each_model_call_once_with_its_cache_tokens(resolved):
    """Claude Code streams one message per content block, each carrying its call's usage:
    summing them counted a call twice, and cache tokens were left out (DF-77)."""
    usage = {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 1000}
    r = runner(resolved)

    turn(r, [block("m1", "a", **usage), block("m1", "b", **usage), result(None)])

    [(cost, _)] = r.costs.calls
    assert (cost.tokens_in, cost.tokens_out) == (1010, 20)
    assert cost.tokens_cache_read == 1000


def test_an_unpriced_model_is_charged_the_fallback_per_model_call(resolved):
    """One flat estimate per turn under-charged a turn of many model calls (DF-77)."""
    usage = {"input_tokens": 10, "output_tokens": 5}
    r = runner(resolved, price=None, fallback=Decimal("0.02"))

    turn(r, [block("m1", "a", **usage), block("m2", "b", **usage), result(usage)])

    assert r.budget.total_spent() == Decimal("0.04")


def test_the_backend_passes_the_configured_fallback_to_its_runners(resolved, tmp_path):
    backend = ClaudeCodeBackend(
        ClaudeCodeSettings(gateway_url="http://127.0.0.1:9", api_key="k", config_root=tmp_path),
        identity_for=lambda step: IDENTITY,
        prices=lambda model: None,
        permissions_for=lambda step: {},
        fallback_estimate_usd=Decimal("0.07"),
    )
    agent = resolved.model_copy(update={"backend": "claude-code", "temperature": None})
    step = StepContext("r-1", "develop", 1, agent, slice_(), None)

    made = backend.runner(step, tool_context=None, state_dir=tmp_path, spawner=None)

    assert made.fallback_estimate_usd == Decimal("0.07")
