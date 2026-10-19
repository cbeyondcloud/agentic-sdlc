"""Handoff documents: what agents pass to each other (and to humans at gates)."""

from agentic.handoffs.compat import check_compatibility
from agentic.handoffs.contract import HandoffContract
from agentic.handoffs.extensions import HandoffExtension, extend_catalog, load_extension
from agentic.handoffs.finalise import BLOCK_ON_DEFAULT, EnvelopeStamp, finalise_handoff
from agentic.handoffs.kinds import (
    HANDOFF_KINDS,
    DesignData,
    DevReportData,
    HandoffBase,
    RequirementsData,
    ReviewData,
    TestReportData,
)
from agentic.handoffs.model import Handoff, HandoffEnvelope, HandoffError
from agentic.handoffs.parse import parse_handoff
from agentic.handoffs.schema import BUILTIN_CATALOG, HandoffCatalog, HandoffSchema, render_template

__all__ = [
    "BLOCK_ON_DEFAULT",
    "BUILTIN_CATALOG",
    "HANDOFF_KINDS",
    "DesignData",
    "DevReportData",
    "EnvelopeStamp",
    "Handoff",
    "HandoffBase",
    "HandoffCatalog",
    "HandoffContract",
    "HandoffEnvelope",
    "HandoffError",
    "HandoffExtension",
    "HandoffSchema",
    "RequirementsData",
    "ReviewData",
    "TestReportData",
    "check_compatibility",
    "extend_catalog",
    "finalise_handoff",
    "load_extension",
    "parse_handoff",
    "render_template",
]
