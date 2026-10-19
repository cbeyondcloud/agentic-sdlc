"""What a person decides at a gate (M1-22; spec §6, §6.1, §12).

A run waiting at a gate (or at an invalid answer, or an exhausted loop) moves only
on a decision:

  * Approve(approver)               - the pinned handoffs are accepted as they are
  * Reject(approver, feedback)      - the work goes back, with the feedback, to the
                                      gate's `on_reject` step (default: the step
                                      before the gate)
  * Edit(approver, path, text)      - one pinned handoff is replaced by the edited
                                      text (validated first), then approved

Who may decide follows the pipeline's regime: under `solo` the starter may
approve; under `team` the gate's `approver_role` is needed, and four-eyes applies
when the gate sets `four_eyes`; under `regulated` four-eyes always applies.
Roles come from an `Authorizer`: standalone mode (M1) has no RBAC and grants every
role; org mode uses the control plane's RBAC (M2-03).
"""

from dataclasses import dataclass
from typing import Protocol


class GateRefused(Exception):
    """A decision the gate does not accept, with the reason a person can act on."""


class Authorizer(Protocol):
    def holds(self, user: str, role: str) -> bool: ...


class StandaloneAuthorizer:
    """Standalone mode has no RBAC: everyone holds every role (org RBAC is M2-03)."""

    def holds(self, user: str, role: str) -> bool:
        return True


@dataclass(frozen=True)
class Approve:
    approver: str
    notes: str = ""


@dataclass(frozen=True)
class Reject:
    approver: str
    feedback: str


@dataclass(frozen=True)
class Edit:
    approver: str
    path: str  # one of the files the gate pins, relative to the run folder
    text: str
    notes: str = ""


Decision = Approve | Reject | Edit

__all__ = [
    "Approve",
    "Authorizer",
    "Decision",
    "Edit",
    "GateRefused",
    "Reject",
    "StandaloneAuthorizer",
]
