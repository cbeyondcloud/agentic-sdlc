"""Handoff documents: what agents pass to each other (and to humans at gates)."""

from agentic.handoffs.compat import check_compatibility
from agentic.handoffs.kinds import (
    HANDOFF_KINDS,
    DesignData,
    DevReportData,
    RequirementsData,
    ReviewData,
    TestReportData,
)
from agentic.handoffs.model import Handoff, HandoffEnvelope, HandoffError
from agentic.handoffs.parse import parse_handoff

__all__ = [
    "HANDOFF_KINDS",
    "DesignData",
    "DevReportData",
    "Handoff",
    "HandoffEnvelope",
    "HandoffError",
    "RequirementsData",
    "ReviewData",
    "TestReportData",
    "check_compatibility",
    "parse_handoff",
]
