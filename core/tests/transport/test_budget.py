"""Tests for the run -> step -> child budget ledger (M1-13, spec §8.4 and §12.9)."""

import random
import threading
from decimal import Decimal

import pytest

from agentic.transport.budget import BudgetError, BudgetLedger, LimitHit
from agentic.transport.cost import CallCost

D = Decimal


def cost(usd: Decimal | None) -> CallCost:
    if usd is None:
        return CallCost(source="unavailable", cost_usd=None, tokens_in=None, tokens_out=None,
                        tokens_cache_read=None, tokens_cache_write=None, model="m",
                        request_id=None, trace_id=None, reason="stream ended early")  # fmt: skip
    return CallCost(source="reported", cost_usd=usd, tokens_in=1, tokens_out=1,
                    tokens_cache_read=None, tokens_cache_write=None, model="m",
                    request_id=None, trace_id=None)  # fmt: skip


def spend(slice_, usd: str) -> Decimal:
    return slice_.reserve(D(usd)).settle(cost(D(usd)))


def test_a_child_cannot_spend_past_its_slice():
    run = BudgetLedger(D("5")).run
    step = run.open("build", "step", D("2"))
    child = step.open("security-review", "child", D("1"))
    spend(child, "0.6")

    with pytest.raises(LimitHit) as hit:
        child.reserve(D("0.5"))
    assert (hit.value.kind, hit.value.scope, hit.value.name) == (
        "budget",
        "child",
        "security-review",
    )
    assert (hit.value.limit_usd, hit.value.spent_usd) == (D("1"), D("0.6"))


def test_a_step_limit_hit_raises_limit_hit_budget():
    step = BudgetLedger(D("5")).run.open("build", "step", D("1"))
    spend(step, "0.9")

    with pytest.raises(LimitHit) as hit:
        step.reserve(D("0.2"))
    assert (hit.value.kind, hit.value.scope) == ("budget", "step")


def test_a_run_limit_hit_names_the_run():
    run = BudgetLedger(D("1")).run
    spend(run, "1")

    with pytest.raises(LimitHit) as hit:
        run.reserve(D("0.01"))
    assert (hit.value.scope, hit.value.name) == ("run", "run")


def test_a_slice_never_gets_more_than_its_parent_has_left():
    step = BudgetLedger(D("5")).run.open("build", "step", D("2"))
    spend(step, "0.5")

    assert step.open("big-child", "child", D("5")).limit_usd == D("1.5")


def test_a_slice_with_no_limit_takes_what_its_parent_has_left():
    step = BudgetLedger(D("5")).run.open("build", "step", None)

    assert step.limit_usd == D("5")


def test_open_children_hold_their_budget_from_siblings():
    step = BudgetLedger(D("5")).run.open("build", "step", D("2"))
    step.open("a", "child", D("1.5"))

    assert step.open("b", "child", D("1")).limit_usd == D("0.5")
    assert step.available() == D("0")


def test_opening_a_slice_with_nothing_left_is_a_limit_hit():
    step = BudgetLedger(D("5")).run.open("build", "step", D("1"))
    step.open("a", "child", D("1"))

    with pytest.raises(LimitHit) as hit:
        step.open("b", "child", D("0.1"))
    assert hit.value.scope == "step"


def test_closing_a_slice_returns_what_it_did_not_spend():
    step = BudgetLedger(D("5")).run.open("build", "step", D("2"))
    child = step.open("a", "child", D("1.5"))
    spend(child, "0.4")
    child.close()

    assert step.spent() == D("0.4")
    assert step.available() == D("1.6")
    child.close()  # closing twice is harmless
    assert step.spent() == D("0.4")


def test_a_closed_slice_takes_no_more_spend():
    child = BudgetLedger(D("5")).run.open("a", "child", D("1"))
    child.close()

    with pytest.raises(BudgetError):
        child.reserve(D("0.1"))


def test_an_overshoot_is_recorded_before_limit_hit_is_raised():
    step = BudgetLedger(D("5")).run.open("build", "step", D("1"))
    reservation = step.reserve(D("0.5"))

    with pytest.raises(LimitHit) as hit:
        reservation.settle(cost(D("1.2")))
    assert step.spent() == D("1.2")  # the real spend is never lost
    assert hit.value.spent_usd == D("1.2")


def test_an_unpriced_call_is_charged_its_reservation_and_counted():
    step = BudgetLedger(D("5")).run.open("build", "step", D("2"))

    charged = step.reserve(D("0.3")).settle(cost(None))
    assert charged == D("0.3") and step.spent() == D("0.3")
    assert step.unpriced_calls == 1


def test_release_frees_the_reservation():
    step = BudgetLedger(D("5")).run.open("build", "step", D("1"))
    reservation = step.reserve(D("0.8"))
    reservation.release()

    assert step.available() == D("1") and step.spent() == D("0")


def test_a_reservation_settles_or_releases_once():
    step = BudgetLedger(D("5")).run.open("build", "step", D("1"))
    reservation = step.reserve(D("0.1"))
    reservation.settle(cost(D("0.1")))

    with pytest.raises(BudgetError):
        reservation.settle(cost(D("0.1")))
    with pytest.raises(BudgetError):
        reservation.release()


def test_an_unbounded_run_still_tracks_spend():
    run = BudgetLedger(None).run
    spend(run, "123.45")

    assert run.available() is None
    assert run.total_spent() == D("123.45")


def test_a_negative_reservation_is_refused():
    with pytest.raises(BudgetError):
        BudgetLedger(D("5")).run.reserve(D("-1"))


def test_total_spent_includes_open_children():
    run = BudgetLedger(D("5")).run
    step = run.open("build", "step", D("2"))
    spend(step.open("a", "child", D("1")), "0.25")
    spend(step, "0.5")

    assert run.total_spent() == D("0.75")


def test_totals_reconcile_under_concurrent_children():
    ledger = BudgetLedger(D("1000"))
    step = ledger.run.open("review", "step", D("900"))
    charged: list[Decimal] = []
    lock = threading.Lock()

    def child_work(n: int) -> None:
        child = step.open(f"child-{n}", "child", D("100"))
        rng = random.Random(n)
        for _ in range(100):
            usd = D(rng.randint(1, 50)) / D(1000)
            amount = child.reserve(usd).settle(cost(usd))
            with lock:
                charged.append(amount)
        if n % 2:
            child.close()

    threads = [threading.Thread(target=child_work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert ledger.run.total_spent() == sum(charged)
    assert len(charged) == 800
