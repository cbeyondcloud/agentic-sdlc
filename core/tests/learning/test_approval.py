"""Who decides a learning proposal, and what may follow what (M3-15, D29, D30)."""

import pytest

from agentic.learning.approval import (
    APPROVE_PERMISSION,
    DecisionError,
    may_decide,
    next_status,
)


def test_only_listed_transitions_are_allowed():
    assert next_status("pending", "approve") == "approved"
    assert next_status("deferred", "approve") == "approved"
    assert next_status("pending", "reject") == "rejected"
    assert next_status("deferred", "reject") == "rejected"
    assert next_status("pending", "defer") == "deferred"
    assert next_status("approved", "apply") == "applied"
    assert next_status("applied", "revert") == "reverted"
    for status, action in [
        ("pending", "apply"),  # nothing is applied without approve (D29)
        ("deferred", "apply"),
        ("rejected", "apply"),
        ("rejected", "approve"),
        ("applied", "approve"),
        ("approved", "revert"),
        ("reverted", "apply"),
    ]:
        with pytest.raises(DecisionError):
            next_status(status, action)


def test_personal_is_decided_by_its_user_only():
    assert may_decide("personal", "Alice@Acme.example", "alice@acme.example", None)
    assert not may_decide("personal", "alice@acme.example", "bob@acme.example", None)
    # a permission never stands in for the owner of a personal item
    assert not may_decide("personal", "alice@acme.example", "admin@acme.example", lambda p: True)


def test_shared_needs_the_scope_permission():
    assert APPROVE_PERMISSION == {
        "repo": "agent.approve.repo",
        "team": "agent.publish.team",
        "org": "skills.approve",
    }
    held = {"agent.approve.repo"}
    assert may_decide("repo", "repo:github.com/acme/app", "lead@x", held.__contains__)
    assert not may_decide("team", "team:payments", "lead@x", held.__contains__)
    assert not may_decide("repo", "repo:github.com/acme/app", "dev@x", set().__contains__)
    # standalone: no RBAC to ask, so a shared decision proceeds (the CLI says so)
    assert may_decide("repo", "repo:local", "dev@x", None)
