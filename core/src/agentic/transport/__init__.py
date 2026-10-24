"""Transport: the one HTTP client every model call uses (identity, network, cost, budget)."""

from agentic.transport.budget import BudgetError, BudgetLedger, BudgetSlice, LimitHit, Reservation
from agentic.transport.client import (
    BEHAVIOUR_HEADERS,
    ForbiddenHeaderError,
    ForeignHostError,
    GatewayKeyRejected,
    GatewayProbe,
    IdentityHeaderError,
    make_async_client,
    make_client,
    make_network_transport,
    probe_gateway,
    verify_gateway_key,
)
from agentic.transport.cost import (
    CallCost,
    Pricer,
    StreamUsage,
    cost_from_response,
    ensure_stream_usage,
    estimate_usd,
)
from agentic.transport.identity import CallIdentity, IdentityError
from agentic.transport.network import NetworkError, NetworkSettings, ssl_context

__all__ = [
    "BudgetError",
    "BudgetLedger",
    "BudgetSlice",
    "LimitHit",
    "Reservation",
    "BEHAVIOUR_HEADERS",
    "CallCost",
    "Pricer",
    "StreamUsage",
    "cost_from_response",
    "ensure_stream_usage",
    "estimate_usd",
    "CallIdentity",
    "ForbiddenHeaderError",
    "ForeignHostError",
    "GatewayKeyRejected",
    "GatewayProbe",
    "probe_gateway",
    "IdentityError",
    "IdentityHeaderError",
    "verify_gateway_key",
    "make_async_client",
    "make_network_transport",
    "make_client",
    "NetworkError",
    "NetworkSettings",
    "ssl_context",
]
