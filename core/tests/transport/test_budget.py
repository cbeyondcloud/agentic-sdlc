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


# ── SP3 review fixes ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("estimate", ["0", "-1"])
def test_an_estimate_must_be_above_zero(estimate):  # review I4
    with pytest.raises(BudgetError):
        BudgetLedger(D("5")).run.reserve(D(estimate))


def test_a_child_overshoot_raises_at_the_outermost_limit_it_breaks():  # review I5
    run = BudgetLedger(D("1")).run
    step = run.open("build", "step", D("1"))
    child = step.open("a", "child", D("1"))

    with pytest.raises(LimitHit) as hit:
        child.reserve(D("0.5")).settle(cost(D("3")))
    assert hit.value.scope == "run"  # the run pause outranks the child stop
    assert run.total_spent() == D("3")


def test_a_child_overshoot_within_the_step_raises_for_the_step():
    run = BudgetLedger(D("10")).run
    step = run.open("build", "step", D("1"))
    child = step.open("a", "child", D("0.5"))

    with pytest.raises(LimitHit) as hit:
        child.reserve(D("0.4")).settle(cost(D("2")))
    assert hit.value.scope == "step"


def test_the_limit_cannot_be_changed_by_assignment():  # review I6
    child = BudgetLedger(D("5")).run.open("a", "child", D("1"))

    with pytest.raises(AttributeError):
        child.limit_usd = D("100")


def test_a_human_can_raise_a_limit_within_what_the_parent_has():  # review I6
    run = BudgetLedger(D("5")).run
    step = run.open("build", "step", D("1"))
    spend(step, "1")

    step.raise_limit(D("3"))
    assert step.limit_usd == D("3") and step.available() == D("2")
    with pytest.raises(LimitHit):
        step.raise_limit(D("10"))  # the run has only 5
    with pytest.raises(BudgetError):
        step.raise_limit(D("2"))  # lowering is not raising


def test_unpriced_calls_and_dollars_roll_up_on_close():  # review I6
    step = BudgetLedger(D("5")).run.open("build", "step", D("2"))
    child = step.open("a", "child", D("1"))
    child.reserve(D("0.2")).settle(cost(None))
    child.close()

    assert (step.unpriced_calls, step.unpriced_usd) == (1, D("0.2"))


def test_closing_with_a_call_in_flight_changes_nothing():  # review M3
    step = BudgetLedger(D("5")).run.open("build", "step", D("2"))
    first = step.open("a", "child", D("1"))
    second = step.open("b", "child", D("0.5"))
    in_flight = second.reserve(D("0.1"))

    with pytest.raises(BudgetError):
        step.close()
    assert not (step.closed or first.closed or second.closed)
    in_flight.release()
    step.close()
    assert step.closed and first.closed and second.closed


def test_limit_hit_survives_pickling():  # review M4
    import pickle

    hit = pickle.loads(pickle.dumps(LimitHit("step", "build", D("1"), D("1.2"))))
    assert (hit.kind, hit.scope, hit.name, hit.limit_usd, hit.spent_usd) == (
        "budget", "step", "build", D("1"), D("1.2"))  # fmt: skip


# ── snapshot and restore: the budget survives a pause or a crash (M1-21, DF-18) ──


def test_a_snapshot_round_trips_the_whole_tree():
    ledger = BudgetLedger(D("5"))
    step = ledger.run.open("build", "step", D("2"))
    spend(step, "0.25")
    child = step.open("security", "child", D("0.5"))
    spend(child, "0.1")
    step.raise_limit(D("2.5"))

    again = BudgetLedger.restore(ledger.snapshot())
    assert again.snapshot() == ledger.snapshot()
    assert again.run.total_spent() == D("0.35")
    assert again.open_slice("build").limit_usd == D("2.5")


def test_a_restored_ledger_enforces_the_same_limits():
    ledger = BudgetLedger(D("1"))
    spend(ledger.run.open("build", "step", D("1")), "0.9")

    again = BudgetLedger.restore(ledger.snapshot())
    with pytest.raises(LimitHit):
        again.open_slice("build").reserve(D("0.2"))


def test_money_in_flight_at_the_snapshot_counts_as_spent_on_restore():
    ledger = BudgetLedger(D("5"))
    step = ledger.run.open("build", "step", D("2"))
    step.reserve(D("0.4"))  # a call that was cut off: it may still be billed

    again = BudgetLedger.restore(ledger.snapshot())
    restored = again.open_slice("build")
    assert restored.total_spent() == D("0.4") and restored.unpriced_calls == 1


def test_on_change_is_called_after_each_settle_and_close():
    ledger = BudgetLedger(D("5"))
    seen = []
    ledger.on_change = lambda: seen.append(ledger.run.total_spent())
    step = ledger.run.open("build", "step", D("1"))
    spend(step, "0.2")
    step.close()

    assert seen[-1] == D("0.2") and len(seen) >= 2


def test_a_snapshot_is_plain_json():
    import json

    ledger = BudgetLedger(None)
    spend(ledger.run.open("build", "step", None), "0.3")
    assert json.loads(json.dumps(ledger.snapshot())) == ledger.snapshot()


def test_a_reservation_is_in_the_snapshot_at_once():  # SP6 review I5
    ledger = BudgetLedger(D("5"))
    step = ledger.run.open("build", "step", D("2"))
    seen = []
    ledger.on_change = lambda: seen.append(ledger.snapshot())
    step.reserve(D("0.4"))  # the process may die while the call is in flight

    assert seen and seen[-1]["children"][0]["reserved"] == "0.4"
