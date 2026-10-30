"""The gateway's share of retention (M3-06): the control plane never writes the gateway's
tables, and TokenLean has no admin API for retention, so its periods are handed over as
its own configuration (`retention:` in TokenLean's config.yaml, WS25). `retention
gateway-config` prints the block for one tenant's policy; a gateway shared by several
tenants takes the longest period of any of them, as its purge is deployment-wide."""

from __future__ import annotations

from collections.abc import Mapping


def gateway_config(stored: Mapping) -> dict:
    """TokenLean's `retention:` block for a tenant's stored retention settings. 0 means
    "keep forever" there; a legal hold on gateway usage or audit (of any team) keeps
    that forever too, as the gateway's purge cannot exempt a scope."""
    days = stored.get("days", {})
    holds = stored.get("legal_holds", [])

    def is_held(kind: str) -> bool:
        return any(not hold.get("data") or kind in hold["data"] for hold in holds)

    usage = 0 if is_held("gateway_usage") else days.get("gateway_usage") or 0
    audit = 0 if is_held("audit") else days.get("audit") or 0
    return {
        "retention": {
            "enabled": bool(usage or audit),
            "interval_hours": 24,
            "audit_days": audit,
            "usage_days": usage,
            "cache_l2_expired_cleanup": True,
        }
    }


def gateway_yaml(config: Mapping) -> str:
    """`gateway_config`'s block as YAML text (one level of scalars: no YAML library)."""
    lines = []
    for section, values in config.items():
        lines.append(f"{section}:")
        for key, value in values.items():
            shown = str(value).lower() if isinstance(value, bool) else str(value)
            lines.append(f"  {key}: {shown}")
    return "".join(f"{line}\n" for line in lines)
