"""Tests for cost capture from TokenLean responses and streams (M1-12).

The fixtures mirror TokenLean's real response shapes (each names its source).
A cost is `reported` (TokenLean's number), `estimated` (our tokens x policy
price) or `unavailable` (None with a reason) - never a silent zero.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from agentic.policy import ModelPrice
from agentic.transport.cost import (
    CallCost,
    StreamUsage,
    cost_from_response,
    ensure_stream_usage,
    estimate_usd,
)

PRICES = {
    "claude-sonnet-5-5": ModelPrice(input_per_mtok=Decimal("3"), output_per_mtok=Decimal("15")),
    "gpt-5-mini": ModelPrice(input_per_mtok=Decimal("0.25"), output_per_mtok=Decimal("2")),
}


def price_for(model: str) -> ModelPrice | None:
    return PRICES.get(model)


def fixture(fixtures: Path, name: str) -> dict:
    return json.loads((fixtures / "transport" / name).read_text(encoding="utf-8"))


def stream(fixtures: Path, name: str) -> tuple[dict, StreamUsage]:
    data = fixture(fixtures, name)
    usage = StreamUsage()
    for event in data["events"]:
        usage.observe(event)
    return data["headers"], usage


# ── non-streaming ─────────────────────────────────────────────────────────────


def test_tokenlean_body_cost_is_reported_exactly(fixtures):
    data = fixture(fixtures, "openai_nonstream.json")
    cost = cost_from_response(data["headers"], data["body"], price_for)

    assert cost == CallCost(source="reported", cost_usd=Decimal("0.004321"), tokens_in=1200,
                            tokens_out=300, tokens_cache_read=None, tokens_cache_write=None,
                            model="gpt-5-mini", request_id="req-1",
                            trace_id="0af7651916cd43dd8448eb211c80319c")  # fmt: skip


def test_the_cost_header_wins_when_tokenlean_sends_it(fixtures):
    data = fixture(fixtures, "openai_nonstream.json")
    headers = {**data["headers"], "X-TokenLean-Cost-USD": "0.01"}

    cost = cost_from_response(headers, data["body"], price_for)
    assert (cost.source, cost.cost_usd) == ("reported", Decimal("0.01"))


def test_the_anthropic_route_is_estimated_from_usage_and_prices(fixtures):
    data = fixture(fixtures, "anthropic_nonstream.json")
    cost = cost_from_response(data["headers"], data["body"], price_for)

    assert (cost.source, cost.cost_usd) == ("estimated", Decimal("0.006"))  # 1000x3 + 200x15
    assert (cost.tokens_in, cost.tokens_out, cost.model) == (1000, 200, "claude-sonnet-5-5")
    assert cost.reason


def test_a_model_without_a_price_is_unavailable_not_zero(fixtures):
    data = fixture(fixtures, "anthropic_nonstream.json")
    cost = cost_from_response(data["headers"], data["body"], lambda model: None)

    assert (cost.source, cost.cost_usd) == ("unavailable", None)
    assert (cost.tokens_in, cost.tokens_out) == (1000, 200)
    assert "claude-sonnet-5-5" in cost.reason


@pytest.mark.parametrize("bad", ["NaN", "-1", "abc", "inf", None, True, -0.5])
def test_a_bad_cost_value_is_not_evidence(fixtures, bad):
    data = fixture(fixtures, "openai_nonstream.json")
    data["body"]["_token_opt"]["cost_actual_usd"] = bad

    cost = cost_from_response(data["headers"], data["body"], price_for)
    assert cost.source == "estimated"  # fell through to usage x price
    assert cost.cost_usd == Decimal("0.0009")  # 1200x0.25 + 300x2 per million


def test_a_cache_hit_costs_what_tokenlean_says(fixtures):
    data = fixture(fixtures, "cache_hit.json")
    cost = cost_from_response(data["headers"], data["body"], price_for)

    assert (cost.source, cost.cost_usd, cost.request_id) == ("reported", Decimal("0"), "req-3")


def test_cache_tokens_come_from_the_headers_when_the_body_has_none(fixtures):
    data = fixture(fixtures, "anthropic_nonstream.json")
    headers = {**data["headers"], "x-tokenlean-cache-read-tokens": "400"}

    cost = cost_from_response(headers, data["body"], price_for)
    assert cost.tokens_cache_read == 400


def test_nothing_measured_means_none_not_zero():
    cost = cost_from_response({}, {"id": "x"}, price_for)

    assert cost == CallCost(source="unavailable", cost_usd=None, tokens_in=None, tokens_out=None,
                            tokens_cache_read=None, tokens_cache_write=None, model=None,
                            request_id=None, trace_id=None, reason=cost.reason)  # fmt: skip
    assert cost.reason


def test_header_names_are_case_insensitive(fixtures):
    data = fixture(fixtures, "openai_nonstream.json")
    headers = {name.upper(): value for name, value in data["headers"].items()}

    assert cost_from_response(headers, data["body"], price_for).request_id == "req-1"


# ── streaming ─────────────────────────────────────────────────────────────────


def test_an_openai_stream_usage_chunk_is_estimated(fixtures):
    headers, usage = stream(fixtures, "openai_stream_events.json")
    cost = usage.result(headers, price_for)

    assert cost.source == "estimated"
    assert (cost.tokens_in, cost.tokens_out, cost.tokens_cache_read) == (800, 150, 200)
    assert cost.cost_usd == Decimal("0.0005")  # (600 + 200 cached at input) x 0.25 + 150 x 2
    assert cost.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_an_anthropic_stream_takes_the_largest_counts_not_their_sum(fixtures):
    headers, usage = stream(fixtures, "anthropic_stream_events.json")
    cost = usage.result(headers, price_for)

    assert (cost.tokens_in, cost.tokens_out) == (812, 95)  # message_start's 0/1 not added
    assert cost.cost_usd == Decimal("0.003861")  # 812x3 + 95x15 per million


def test_a_stream_that_ended_without_usage_is_unavailable(fixtures):
    headers, usage = stream(fixtures, "openai_stream_no_usage.json")
    cost = usage.result(headers, price_for)

    assert (cost.source, cost.cost_usd, cost.tokens_in, cost.tokens_out) == (
        "unavailable", None, None, None)  # fmt: skip
    assert cost.reason


def test_a_stream_cost_header_still_wins(fixtures):
    headers, usage = stream(fixtures, "openai_stream_events.json")
    cost = usage.result({"x-tokenlean-cost-usd": "0.002"}, price_for)

    assert (cost.source, cost.cost_usd) == ("reported", Decimal("0.002"))


# ── keeping the usage chunk, and the estimate itself ──────────────────────────


def test_ensure_stream_usage_adds_include_usage_and_keeps_other_options():
    body = {"model": "m", "stream": True, "stream_options": {"include_obfuscation": False}}

    fixed = ensure_stream_usage(body)
    assert fixed["stream_options"] == {"include_obfuscation": False, "include_usage": True}
    assert body["stream_options"] == {"include_obfuscation": False}  # the caller's dict untouched


def test_ensure_stream_usage_leaves_non_stream_bodies_alone():
    body = {"model": "m", "stream": False}

    assert ensure_stream_usage(body) == body


def test_estimate_charges_cache_at_its_price_or_the_input_price():
    priced = ModelPrice(input_per_mtok=Decimal("3"), output_per_mtok=Decimal("15"),
                        cache_read_per_mtok=Decimal("0.3"))  # fmt: skip

    # 1000 in of which 400 read from cache and 100 written: 500x3 + 400x0.3 + 100x3 + 10x15
    assert estimate_usd(priced, 1000, 10, cache_read=400, cache_write=100) == Decimal("0.00207")


def test_a_call_cost_must_be_consistent():
    with pytest.raises(ValueError):
        CallCost(source="unavailable", cost_usd=Decimal("1"), tokens_in=None, tokens_out=None,
                 tokens_cache_read=None, tokens_cache_write=None, model=None, request_id=None,
                 trace_id=None, reason="x")  # fmt: skip
    with pytest.raises(ValueError):
        CallCost(source="estimated", cost_usd=Decimal("1"), tokens_in=1, tokens_out=1,
                 tokens_cache_read=None, tokens_cache_write=None, model="m", request_id=None,
                 trace_id=None)  # fmt: skip
