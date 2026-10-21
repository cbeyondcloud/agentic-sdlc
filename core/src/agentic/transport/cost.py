"""What one model call cost (M1-12).

TokenLean reports cost only on non-streaming OpenAI-route responses (body
`_token_opt.cost_actual_usd`); its planned `x-tokenlean-cost-usd` header (ST-3)
is read first when present. Elsewhere - the Anthropic route, live streams - the
call's own `usage` is priced with the org's price table. Every result says which:

  * reported    - TokenLean's number (itself priced from TokenLean's table);
  * estimated   - our token counts x the policy price;
  * unavailable - no cost could be known: `cost_usd` is None with a reason,
                  never a silent zero (the metrics rule: unmeasured is null).

`tokens_in` counts ALL input tokens, cached ones included. OpenAI's
`prompt_tokens` already does; Anthropic's `input_tokens` excludes its cache
tokens, so they are added. An estimate charges the cached part at its cache price.

Token counts come from the provider's own `usage` first, TokenLean's `_token_opt`
second; cost comes from TokenLean first. A cost is per HTTP request: an agent
run that makes several requests has several costs.
"""

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from agentic.policy import ModelPrice

Pricer = Callable[[str], ModelPrice | None]
_MILLION = Decimal(1_000_000)
#: No single model call costs more than this; a larger "cost" is a bad value, not evidence.
MAX_CALL_USD = Decimal(100_000)
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
        if self.cost_usd is not None and (
            not isinstance(self.cost_usd, Decimal)
            or not self.cost_usd.is_finite()
            or self.cost_usd < 0
        ):
            raise ValueError("a cost is a finite, non-negative Decimal")
        if self.source != "reported" and not self.reason:
            raise ValueError(f"a {self.source} cost must say why")


def estimate_usd(
    price: ModelPrice,
    tokens_in: int,
    tokens_out: int,
    cache_read: int | None = None,
    cache_write: int | None = None,
) -> Decimal:
    """Tokens x price, per million. `tokens_in` includes the cached tokens: they are
    charged at their cache price (or the input price when the model has none), the
    rest of the input at the input price."""
    read, write = cache_read or 0, cache_write or 0
    uncached = max(tokens_in - read - write, 0)
    read_price = price.cache_read_per_mtok
    write_price = price.cache_write_per_mtok
    total = (
        uncached * price.input_per_mtok
        + read * (read_price if read_price is not None else price.input_per_mtok)
        + write * (write_price if write_price is not None else price.input_per_mtok)
        + tokens_out * price.output_per_mtok
    )
    return total / _MILLION


def cost_from_response(
    headers: Mapping[str, str], body: Mapping | None, price_for: Pricer
) -> CallCost:
    """The cost of one non-streaming response: header, then TokenLean's body cost,
    then usage x price, else unavailable."""
    h = _lower(headers)
    body = body if isinstance(body, Mapping) else {}
    opt = body.get("_token_opt") if isinstance(body.get("_token_opt"), Mapping) else {}
    usage = _usage(body.get("usage"))
    tokens = {
        "tokens_in": _count(usage.get("in"), opt.get("tokens_provider_billed")),
        "tokens_out": _count(usage.get("out"), opt.get("response_tokens")),
        "tokens_cache_read": _count(
            usage.get("cache_read"),
            opt.get("cache_read_tokens"),
            h.get("x-tokenlean-cache-read-tokens"),
        ),
        "tokens_cache_write": _count(
            usage.get("cache_write"),
            opt.get("cache_write_tokens"),
            h.get("x-tokenlean-cache-write-tokens"),
        ),
    }
    model = h.get("x-tokenlean-routed-model") or _text(body.get("model"))
    cache_hit = opt.get("cache_hit") is True or h.get("x-tokenlean-cache", "").startswith("hit")
    return _judge(
        h,
        opt.get("cost_actual_usd"),
        tokens,
        model,
        price_for,
        "response usage",
        cache_hit=cache_hit,
    )


class StreamUsage:
    """Token usage collected from a stream's events.

    A stream's usage is only trusted once it FINISHED: OpenAI sends its usage
    chunk last; Anthropic's final `message_delta` carries the closing counts
    (`message_start` only opens the message, and through TokenLean its input is
    0). A stream that never finished is `unavailable` - its partial counts are
    kept for information, never priced. Counts reported twice (start and delta)
    are the same totals at different moments: the largest wins, never their sum.
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._model: str | None = None
        self._finished = False
        self._anthropic = False

    def observe(self, event: Mapping) -> None:
        if not isinstance(event, Mapping):
            return
        kind = event.get("type")
        message = event.get("message") if isinstance(event.get("message"), Mapping) else {}
        self._model = _text(event.get("model")) or _text(message.get("model")) or self._model
        if kind in ("message_start", "message_delta", "message_stop"):
            self._anthropic = True
        usages = [message.get("usage")] if kind == "message_start" else [event.get("usage")]
        for raw in usages:
            counts = _usage(raw)
            for key, value in counts.items():
                if value is not None:
                    self._counts[key] = max(self._counts.get(key, 0), value)
            if counts and kind == "message_delta":
                self._finished = True  # Anthropic's closing counts
            elif counts and kind is None and counts.get("in") is not None:
                self._finished = True  # OpenAI's final usage chunk

    def result(
        self, headers: Mapping[str, str], price_for: Pricer, model: str | None = None
    ) -> CallCost:
        h = _lower(headers)
        tokens = {
            "tokens_in": self._counts.get("in"),
            "tokens_out": self._counts.get("out"),
            "tokens_cache_read": self._counts.get("cache_read"),
            "tokens_cache_write": self._counts.get("cache_write"),
        }
        model = model or h.get("x-tokenlean-routed-model") or self._model
        ids = _ids(h, model)
        if _money(h.get(COST_HEADER)) is None:
            if not self._finished:
                return CallCost(
                    source="unavailable",
                    cost_usd=None,
                    **tokens,
                    **ids,
                    reason="the stream did not finish (no closing usage)",
                )
            if self._anthropic and not tokens["tokens_in"]:
                return CallCost(
                    source="unavailable",
                    cost_usd=None,
                    **tokens,
                    **ids,
                    reason="the stream reported no input tokens",
                )
        return _judge(h, None, tokens, model, price_for, "stream usage chunk", cache_hit=False)


def ensure_stream_usage(body: dict, route: Literal["openai", "anthropic"] = "openai") -> dict:
    """A copy of an OpenAI-route request body that keeps the stream's usage chunk:
    TokenLean strips it when `stream_options` is sent without `include_usage: true`.
    Anthropic-route bodies are returned unchanged (they have no `stream_options`)."""
    if route != "openai" or not body.get("stream"):
        return body
    given = body.get("stream_options")
    options = dict(given) if isinstance(given, Mapping) else {}
    options["include_usage"] = True
    return {**body, "stream_options": options}


def _judge(
    h: dict[str, str],
    body_cost: object,
    tokens: dict,
    model: str | None,
    price_for: Pricer,
    what: str,
    *,
    cache_hit: bool,
) -> CallCost:
    ids = _ids(h, model)
    billed = (tokens["tokens_in"] or 0) + (tokens["tokens_out"] or 0)
    for evidence in (h.get(COST_HEADER), body_cost):
        usd = _money(evidence)
        if usd is None or (usd == 0 and billed and not cache_hit):
            continue  # a zero with tokens billed and no cache hit is not believed
        return CallCost(source="reported", cost_usd=usd, **tokens, **ids)
    if tokens["tokens_in"] is None or tokens["tokens_out"] is None:
        return CallCost(
            source="unavailable",
            cost_usd=None,
            **tokens,
            **ids,
            reason=f"no reported cost and no {what}",
        )
    price = price_for(model) if model else None
    if price is None:
        return CallCost(
            source="unavailable",
            cost_usd=None,
            **tokens,
            **ids,
            reason=f"no reported cost and no price for model {model!r}",
        )
    usd = estimate_usd(
        price,
        tokens["tokens_in"],
        tokens["tokens_out"],
        tokens["tokens_cache_read"],
        tokens["tokens_cache_write"],
    )
    return CallCost(
        source="estimated",
        cost_usd=usd,
        **tokens,
        **ids,
        reason=f"priced from {what} with the policy price for {model}",
    )


def _ids(h: dict[str, str], model: str | None) -> dict[str, str | None]:
    return {
        "model": model,
        "request_id": h.get("x-tokenlean-request-id"),
        "trace_id": h.get("x-trace-id"),
    }


def _usage(raw: object) -> dict[str, int | None]:
    """Usage as all-input / output / cache counts, from OpenAI (`prompt_tokens` -
    cached tokens included) or Anthropic (`input_tokens` - cache tokens separate,
    so they are added) shapes."""
    if not isinstance(raw, Mapping):
        return {}
    details = raw.get("prompt_tokens_details")
    details = details if isinstance(details, Mapping) else {}
    read = _count(details.get("cached_tokens"), raw.get("cache_read_input_tokens"))
    write = _count(raw.get("cache_creation_input_tokens"))
    if "input_tokens" in raw:  # Anthropic: input excludes the cache tokens
        given = _count(raw.get("input_tokens"))
        total_in = None if given is None else given + (read or 0) + (write or 0)
    else:
        total_in = _count(raw.get("prompt_tokens"))
    return {
        "in": total_in,
        "out": _count(raw.get("completion_tokens"), raw.get("output_tokens")),
        "cache_read": read,
        "cache_write": write,
    }


def _count(*candidates: object) -> int | None:
    """The first candidate that is a non-negative whole number (a header string counts)."""
    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, str):
            text = value.strip()
            if text.isascii() and text.isdigit():
                return int(text)
            continue
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _money(value: object) -> Decimal | None:
    """A plausible USD amount (finite, 0 to MAX_CALL_USD), or None: a bad value is not evidence."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    text = str(value).strip()
    if "_" in text:
        return None
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and 0 <= amount <= MAX_CALL_USD else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in dict(headers).items()}
