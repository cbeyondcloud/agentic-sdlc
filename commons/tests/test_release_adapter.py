"""CI verdict adapter over Zyroflo's release flow (M1-58, D34): five answers, one green."""

import pytest

from sdlc_commons.release import GREEN, NONE, PENDING, RED, UNKNOWN, ci_verdict


def row(name: str, status: str = "completed", conclusion: str | None = "success") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion, "link": ""}


def test_all_successful_checks_are_green():
    found = ci_verdict([row("build"), row("lint")])
    assert (found["state"], found["checks"], found["failed"]) == (GREEN, 2, [])


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "cancelled", "action_required"])
def test_a_failed_check_is_red_and_named(conclusion):
    found = ci_verdict([row("build"), row("tests", conclusion=conclusion)])
    assert found["state"] == RED and found["failed"] == ["tests"]


@pytest.mark.parametrize("status", ["queued", "in_progress", "pending"])
def test_a_running_check_is_pending(status):
    found = ci_verdict([row("build"), row("e2e", status=status, conclusion=None)])
    assert found["state"] == PENDING and found["pending"] == ["e2e"]


@pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
def test_a_skipped_check_is_not_a_pass(conclusion):
    assert ci_verdict([row("deploy", conclusion=conclusion)])["state"] != GREEN


def test_no_checks_at_all_is_none_not_green():
    found = ci_verdict([])
    assert found["state"] == NONE and "nothing ran" in found["why"]


def test_a_word_nobody_knows_is_unknown_not_green():
    found = ci_verdict([row("odd", conclusion="stale")])
    assert found["state"] == UNKNOWN and found["unknown"] == ["odd (stale)"]


def test_red_wins_over_pending():
    found = ci_verdict([row("a", conclusion="failure"), row("b", status="queued", conclusion=None)])
    assert found["state"] == RED


@pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
def test_a_skipped_check_is_named_as_skipped_not_as_still_running(conclusion):
    found = ci_verdict([row("build"), row("deploy", conclusion=conclusion)])
    assert found["state"] == PENDING  # Zyroflo's rule: not a pass, a question for a person
    assert (found["running"], found["skipped"]) == ([], ["deploy"])
    assert "skipped" in found["why"] and "deploy" in found["why"]
    assert "still running" not in found["why"]


def test_running_and_skipped_checks_are_told_apart():
    found = ci_verdict(
        [row("e2e", status="in_progress", conclusion=None), row("deploy", conclusion="skipped")]
    )
    assert (found["running"], found["skipped"]) == (["e2e"], ["deploy"])
    assert "1 check(s) still running" in found["why"] and "deploy" in found["why"]
