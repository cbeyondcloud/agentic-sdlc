"""Retention (M3-06, OR-01, owner decision 115): the periods of `retention.yaml` applied to
the control plane's data, the gateway's share handed over as configuration, and tenant
deletion (DF-568)."""

from agentic_cp.retention.gateway import gateway_config, gateway_yaml
from agentic_cp.retention.prune import (
    LEADER_KEY,
    RETENTION_ACTOR,
    PruneReport,
    prune_tenant,
    run_retention,
)
from agentic_cp.retention.tenants import TenantDeleteError, delete_tenant, purge_deleted

__all__ = [
    "LEADER_KEY",
    "RETENTION_ACTOR",
    "PruneReport",
    "TenantDeleteError",
    "delete_tenant",
    "gateway_config",
    "gateway_yaml",
    "prune_tenant",
    "purge_deleted",
    "run_retention",
]
