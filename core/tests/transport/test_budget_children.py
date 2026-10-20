"""What each child slice spent, kept by its parent for the cost breakdown (M1-57)."""

from decimal import Decimal

from agentic.transport.budget import BudgetLedger
from agentic.transport.cost import CallCost

D = Decimal


def spend(slice_, usd: str) -> None:
    cost = CallCost(
        source="reported",
        cost_usd=D(usd),
        tokens_in=1,
        tokens_out=1,
        tokens_cache_read=None,
        tokens_cache_write=None,
        model="m",
        request_id=None,
        trace_id=None,
    )
    slice_.reserve(D(usd)).settle(cost)


def test_a_closed_child_s_spend_is_kept_by_name_descendants_included():
    step = BudgetLedger(D("5")).run.open("develop-1", "step", D("3"))
    child = step.open("security", "child", D("1"))
    spend(child, "0.20")
    grandchild = child.open("deps", "child", D("0.5"))
    spend(grandchild, "0.05")
    grandchild.close()
    child.close()
    again = step.open("security", "child", D("1"))
    spend(again, "0.01")
    again.close()
    assert step.child_spend == {"security": D("0.26")}
    assert child.child_spend == {"deps": D("0.05")}


def test_an_open_child_is_not_in_the_breakdown_yet():
    step = BudgetLedger(None).run.open("s", "step", None)
    child = step.open("c", "child", None)
    spend(child, "0.10")
    assert step.child_spend == {}


def test_the_breakdown_survives_a_snapshot():
    ledger = BudgetLedger(D("5"))
    step = ledger.run.open("develop-1", "step", D("3"))
    child = step.open("perf", "child", D("1"))
    spend(child, "0.30")
    child.close()
    restored = BudgetLedger.restore(ledger.snapshot())
    assert restored.open_slice("develop-1").child_spend == {"perf": D("0.30")}


def test_an_old_snapshot_without_a_breakdown_still_restores():
    ledger = BudgetLedger(D("5"))
    ledger.run.open("develop-1", "step", D("3"))
    snapshot = ledger.snapshot()
    for child in snapshot["children"]:
        child.pop("child_spend")
    assert BudgetLedger.restore(snapshot).open_slice("develop-1").child_spend == {}
