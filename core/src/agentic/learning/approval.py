"""Who decides a learning proposal, and what may follow what (M3-15, spec §24 "Approve";
D29, D30).

A proposal is decided by the owner of what it would change:

  * **personal** (the user's own `~/.agentic` skill or note) - that user, and only that
    user: no permission stands in for them;
  * **repo** (AGENTS.md, a repo skill, a repo agent definition) - a holder of
    `agent.approve.repo` on the repository (its CODEOWNERS maintainers, the team's leads);
  * **team** (a team skill or agent in the policy repo) - `agent.publish.team`;
  * **org** - `skills.approve` (org targets are phase 2: never chosen automatically).

Anyone who may read a proposal may endorse it: that adds weight and never decides it.
Without an org there is no RBAC to ask (standalone): a shared decision proceeds, and the
CLI says so, as `agentic skills approve` does.

The statuses move only along `TRANSITIONS`. Nothing reaches `applied` without passing
through `approved`, so nothing is applied without an explicit approval (D29).
"""

from collections.abc import Callable
from typing import Literal

from agentic.learning.clustering import ProposalStatus

Action = Literal["approve", "reject", "defer", "apply", "revert"]
DECISIONS: tuple[str, ...] = ("approve", "reject", "defer")
TRANSITIONS: dict[tuple[str, str], ProposalStatus] = {
    ("pending", "approve"): "approved",
    ("deferred", "approve"): "approved",
    ("pending", "reject"): "rejected",
    ("deferred", "reject"): "rejected",
    ("pending", "defer"): "deferred",
    ("approved", "apply"): "applied",
    # an approved change that could not be made (no remote, a failed push) may be dropped
    ("approved", "reject"): "rejected",
    ("applied", "revert"): "reverted",
}
#: The permission that approves a shared proposal of each scope (the same ones that approve
#: a promoted skill, M2-46).
APPROVE_PERMISSION: dict[str, str] = {
    "repo": "agent.approve.repo",
    "team": "agent.publish.team",
    "org": "skills.approve",
}


class DecisionError(Exception):
    """The proposal's status does not allow this action."""


def next_status(status: str, action: str) -> ProposalStatus:
    """The status after `action`, or DecisionError when it may not follow `status`."""
    try:
        return TRANSITIONS[(status, action)]
    except KeyError:
        allowed = sorted(a for (s, a) in TRANSITIONS if s == status)
        hint = f" (it may be: {', '.join(allowed)})" if allowed else ""
        raise DecisionError(f"{action!r} is not possible for a {status} proposal{hint}") from None


def may_decide(scope: str, owner: str, user: str, allowed: Callable[[str], bool] | None) -> bool:
    """Whether `user` may approve, edit, reject or defer a proposal of `scope` owned by
    `owner`. `allowed(permission)` answers for the user in the proposal's repo or team;
    None means there is no RBAC to ask (standalone)."""
    if scope == "personal":
        return owner.strip().casefold() == user.strip().casefold()
    if allowed is None:
        return True
    return allowed(APPROVE_PERMISSION[scope])


__all__ = [
    "APPROVE_PERMISSION",
    "DECISIONS",
    "TRANSITIONS",
    "Action",
    "DecisionError",
    "may_decide",
    "next_status",
]
