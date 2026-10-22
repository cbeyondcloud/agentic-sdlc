"""The per-tenant, hash-chained audit trail (M2-05)."""

from agentic_cp.audit.chain import (
    OPERATOR,
    AuditError,
    AuditEvent,
    AuditRecord,
    append,
    format_at,
    read_records,
    verify,
)

__all__ = [
    "OPERATOR",
    "AuditError",
    "AuditEvent",
    "AuditRecord",
    "append",
    "format_at",
    "read_records",
    "verify",
]
