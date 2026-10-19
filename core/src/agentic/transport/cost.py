"""What one model call cost (M1-12).

TokenLean reports cost only on non-streaming OpenAI-route responses (body
`_token_opt.cost_actual_usd`); its planned `x-tokenlean-cost-usd` header (ST-3)
is read first when present. Elsewhere - the Anthropic route, live streams - the
call's own `usage` is priced with the org's price table. Every result says which:

  * reported    - TokenLean's number (itself priced from TokenLean's table);
  * estimated   - our token counts x the policy price;
  * unavailable - no cost could be known: `cost_usd` is None with a reason,
                  never a silent zero (the metrics rule: unmeasured is null).

`tokens_in` counts ALL input tokens, cached ones included (as OpenAI's
`prompt_tokens` does); an estimate charges the cached part at its cache price.
"""

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from agentic.policy import ModelPrice

Pricer = Callable[[str], ModelPrice | None]
_MILLION = Decimal(1_000_000)
COST_HEADER = "x-tokenlean-cost-usd"


@dataclass(frozen=True)
class CallCost:
    source: Literal["reported", "estimated", "unavailable"]
    cost_usd: Decimal | None
    tokens_in: int | None
    tokens_out: int | None
    tokens_cache_read: int | None
    tokens_cache_write: int | None
    model: str | None
    request_id: str | None
    trace_id: str | None
    reason: str = ""  # why it is estimated or unavailable

    def __post_init__(self) -> None:
        if (self.cost_usd is None) != (self.source == "unavailable"):
            raise ValueError("a cost is None exactly when its source is 'unavailable'")
        if self.source != "reported" and not self.reason:
            raise ValueError(f"a {self.source} cost must say why")


def estimate_usd(
    price: ModelPrice,
    tokens_in: int,
    tokens_out: int,
    cache_read: int | None = None,
    cache_write: int | None = None,
) -> Decimal:
    """Tokens x price, per million. Cached input is charged at its cache price, or at
    the input price when the model has none; the rest of the input at the input price."""
    read, write = cache_read or 0, cache_write or 0
    uncached = max(tokens_in - read - write, 0)
    total = (uncached * price.input_per_mtok
             + read * (price.cache_read_per_mtok if price.cache_read_per_mtok is not None
                       else price.input_per_mtok)
             + write * (price.cache_write_per_mtok if price.cache_write_per_mtok is not None
                        else price.input_per_mtok)
             + tokens_out * price.output_per_mtok)  # fmt: skip
    return total / _MILLION


def cost_from_response(headers: Mapping[str, str], body: Mapping | None,
                       price_for: Pricer) -> CallCost:  # fmt: skip
    """The cost of one non-streaming response: header, then TokenLean's body cost,
    then usage x price, else unavailable."""
    h = _lower(headers)
    body = body if isinstance(body, Mapping) else {}
    opt = body.get("_token_opt") if isinstance(body.get("_token_opt"), Mapping) else {}
    usage = _usage(body.get("usage"))
    tokens = {
        "tokens_in": _count(opt.get("tokens_provider_billed"), usage.get("in")),
        "tokens_out": _count(opt.get("response_tokens"), usage.get("out")),
        "tokens_cache_read": _count(opt.get("cache_read_tokens"), usage.get("cache_read"),
                                    h.get("x-tokenlean-cache-read-tokens")),
        "tokens_cache_write": _count(opt.get("cache_write_tokens"), usage.get("cache_write"),
                                     h.get("x-tokenlean-cache-write-tokens")),
    }  # fmt: skip
    model = h.get("x-tokenlean-routed-model") or _text(body.get("model"))
    return _judge(h, opt.get("cost_actual_usd"), tokens, model, price_for, "response usage")


class StreamUsage:
    """Collects token usage from a stream's events: the OpenAI final usage chunk, or
    Anthropic's `message_start` / `message_delta` (the largest count wins - the two
    report the same totals at different moments, never parts to add up)."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._model: str | None = None

    def observe(self, event: Mapping) -> None:
        if not isinstance(event, Mapping):
            return
        message = event.get("message") if isinstance(event.get("message"), Mapping) else {}
        self._model = _text(event.get("model")) or _text(message.get("model")) or self._model
        for raw in (event.get("usage"), message.get("usage")):
            for key, value in _usage(raw).items():
                if value is not None:
                    self._counts[key] = max(self._counts.get(key, 0), value)

    def result(self, headers: Mapping[str, str], price_for: Pricer,
               model: str | None = None) -> CallCost:  # fmt: skip
        h = _lower(headers)
        tokens = {
            "tokens_in": self._counts.get("in"),
            "tokens_out": self._counts.get("out"),
            "tokens_cache_read": self._counts.get("cache_read"),
            "tokens_cache_write": self._counts.get("cache_write"),
        }
        model = model or h.get("x-tokenlean-routed-model") or self._model
        return _judge(h, None, tokens, model, price_for, "stream usage chunk")


def ensure_stream_usage(body: dict) -> dict:
    """A copy of a request body that keeps the stream's usage chunk: TokenLean
    strips it when `stream_options` is sent without `include_usage: true`."""
    if not body.get("stream"):
        return body
    options = dict(body.get("stream_options") or {})
    options["include_usage"] = True
    return {**body, "stream_options": options}


def _judge(h: dict[str, str], body_cost: object, tokens: dict, model: str | None,
           price_for: Pricer, what: str) -> CallCost:  # fmt: skip
    ids = {"model": model, "request_id": h.get("x-tokenlean-request-id"),
           "trace_id": h.get("x-trace-id")}  # fmt: skip
    for evidence in (h.get(COST_HEADER), body_cost):
        if (usd := _money(evidence)) is not None:
            return CallCost(source="reported", cost_usd=usd, **tokens, **ids)
    if tokens["tokens_in"] is None or tokens["tokens_out"] is None:
        return CallCost(source="unavailable", cost_usd=None, **tokens, **ids,
                        reason=f"no reported cost and no {what}")  # fmt: skip
    price = price_for(model) if model else None
    if price is None:
        return CallCost(source="unavailable", cost_usd=None, **tokens, **ids,
                        reason=f"no reported cost and no price for model {model!r}")  # fmt: skip
    usd = estimate_usd(price, tokens["tokens_in"], tokens["tokens_out"],
                       tokens["tokens_cache_read"], tokens["tokens_cache_write"])  # fmt: skip
    return CallCost(source="estimated", cost_usd=usd, **tokens, **ids,
                    reason=f"priced from {what} with the policy price for {model}")  # fmt: skip


def _usage(raw: object) -> dict[str, int | None]:
    """OpenAI (`prompt_tokens`, `completion_tokens`, `prompt_tokens_details.cached_tokens`)
    or Anthropic (`input_tokens`, `output_tokens`, `cache_*_input_tokens`) usage."""
    if not isinstance(raw, Mapping):
        return {}
    details = raw.get("prompt_tokens_details")
    details = details if isinstance(details, Mapping) else {}
    return {
        "in": _count(raw.get("prompt_tokens"), raw.get("input_tokens")),
        "out": _count(raw.get("completion_tokens"), raw.get("output_tokens")),
        "cache_read": _count(details.get("cached_tokens"), raw.get("cache_read_input_tokens")),
        "cache_write": _count(raw.get("cache_creation_input_tokens")),
    }


def _count(*candidates: object) -> int | None:
    """The first candidate that is a non-negative whole number (a header string counts)."""
    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _money(value: object) -> Decimal | None:
    """A finite, non-negative USD amount, or None (bad values are not evidence)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in dict(headers).items()}
