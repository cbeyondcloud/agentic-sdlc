"""The run -> step -> child budget ledger (M1-13, spec §8.4 and §12.9).

TokenLean enforces the tenant and team monthly caps; the runner enforces the
run, step and child budgets here, from each call's cost (`cost.CallCost`).

  * A slice is carved from its parent and never larger than what the parent has
    left; while it is open its whole limit is held from its siblings, and on
    close what it did not spend goes back.
  * Before a call, `reserve` an estimate (above zero); after it, `settle` the
    real cost (or `release` if the call failed). A cost that could not be known
    is charged at the reserved estimate and counted (`unpriced_calls`,
    `unpriced_usd`, rolled up on close) - never at zero.
  * Crossing a limit raises `LimitHit` at the OUTERMOST limit crossed: the
    engine pauses the run for a human on a run or step limit and stops a child
    (partial result) on a child limit. A settle that overshoots records the
    spend first, then raises. After an overshoot `available()` is negative.
  * A human may raise a limit (`raise_limit`), never beyond what the parent has.

Money is `Decimal`. One lock guards the whole tree: children may run on threads.
Persisting the ledger across a pause or restart belongs with the run state (M1-21).
"""

import threading
from decimal import Decimal
from typing import Literal

from agentic.transport.cost import CallCost

Scope = Literal["run", "step", "child"]


class BudgetError(Exception):
    """A misuse of the ledger (a closed slice, a reservation used twice, a bad amount)."""


class LimitHit(Exception):
    """A budget limit was reached at `scope` (the engine stops a child, pauses a step or run)."""

    def __init__(self, scope: Scope, name: str, limit_usd: Decimal, spent_usd: Decimal) -> None:
        super().__init__(scope, name, limit_usd, spent_usd)  # picklable: args rebuild it
        self.kind: Literal["budget"] = "budget"
        self.scope, self.name = scope, name
        self.limit_usd, self.spent_usd = limit_usd, spent_usd

    def __str__(self) -> str:
        return (f"{self.scope} {self.name!r} reached its budget: {self.spent_usd} of "
                f"{self.limit_usd} USD")  # fmt: skip


class BudgetLedger:
    """The budget of one run; `run` is the root slice."""

    def __init__(self, run_limit_usd: Decimal | None) -> None:
        self._lock = threading.RLock()
        self.run = BudgetSlice(self._lock, "run", "run", _amount(run_limit_usd, "run limit"), None)


class BudgetSlice:
    def __init__(self, lock: threading.RLock, name: str, scope: Scope,
                 limit_usd: Decimal | None, parent: "BudgetSlice | None") -> None:  # fmt: skip
        self._lock = lock
        self.name, self.scope, self.parent = name, scope, parent
        self._limit = limit_usd
        self._spent = Decimal(0)
        self._reserved = Decimal(0)
        self._open_children: list[BudgetSlice] = []
        self._open_reservations = 0
        self.closed = False
        self.unpriced_calls = 0
        self.unpriced_usd = Decimal(0)
        self.limit_changes: list[tuple[Decimal, Decimal]] = []  # (old, new), for the audit trail

    @property
    def limit_usd(self) -> Decimal | None:
        return self._limit

    def open(self, name: str, scope: Literal["step", "child"],
             limit_usd: Decimal | None) -> "BudgetSlice":  # fmt: skip
        """Carve a slice: never more than this slice has left; held while open."""
        limit_usd = _amount(limit_usd, "slice limit")
        with self._lock:
            self._check_open()
            left = self.available()
            if left is not None:
                if left <= 0:
                    raise LimitHit(self.scope, self.name, self._limit, self.total_spent())
                limit_usd = left if limit_usd is None else min(limit_usd, left)
            child = BudgetSlice(self._lock, name, scope, limit_usd, self)
            self._open_children.append(child)
            return child

    def reserve(self, estimate_usd: Decimal) -> "Reservation":
        """Hold an estimate (above zero) before a call; LimitHit if it does not fit."""
        estimate_usd = _amount(estimate_usd, "estimate")
        if estimate_usd is None or estimate_usd <= 0:
            raise BudgetError("a reservation needs an estimate above zero - it is what an "
                              "unpriced call is charged")  # fmt: skip
        with self._lock:
            self._check_open()
            left = self.available()
            if left is not None and estimate_usd > left:
                raise LimitHit(self.scope, self.name, self._limit, self.total_spent())
            self._reserved += estimate_usd
            self._open_reservations += 1
            return Reservation(self, estimate_usd)

    def raise_limit(self, new_limit_usd: Decimal) -> None:
        """A human grants more budget: never lower, never more than the parent has left."""
        new_limit_usd = _amount(new_limit_usd, "new limit")
        with self._lock:
            self._check_open()
            if self._limit is None or new_limit_usd is None or new_limit_usd < self._limit:
                raise BudgetError(f"{self.scope} {self.name!r}: a limit can only be raised")
            parent_left = self.parent.available() if self.parent is not None else None
            if parent_left is not None and new_limit_usd - self._limit > parent_left:
                raise LimitHit(self.parent.scope, self.parent.name, self.parent._limit,
                               self.parent.total_spent())  # fmt: skip
            self.limit_changes.append((self._limit, new_limit_usd))
            self._limit = new_limit_usd

    def available(self) -> Decimal | None:
        """Limit minus spend, open reservations and what open children hold; None if unbounded."""
        with self._lock:
            if self._limit is None:
                return None
            held = sum((max(c._limit or Decimal(0), c.total_spent())
                        for c in self._open_children), Decimal(0))  # fmt: skip
            return self._limit - self._spent - self._reserved - held

    def spent(self) -> Decimal:
        """Settled spend of this slice, including its closed children."""
        with self._lock:
            return self._spent

    def total_spent(self) -> Decimal:
        """Settled spend including what is inside still-open children."""
        with self._lock:
            return self._spent + sum((c.total_spent() for c in self._open_children), Decimal(0))

    def close(self) -> None:
        """Hand the slice's spend to its parent and release what it held. Refused,
        changing nothing, while any call in this slice or below is in flight."""
        with self._lock:
            if self.closed:
                return
            if self._in_flight():
                raise BudgetError(f"{self.scope} {self.name!r} still has calls in flight - "
                                  f"settle or release them first")  # fmt: skip
            self._close()

    def _close(self) -> None:
        for child in list(self._open_children):
            child._close()
        self.closed = True
        if self.parent is not None:
            self.parent._open_children.remove(self)
            self.parent._spent += self._spent
            self.parent.unpriced_calls += self.unpriced_calls
            self.parent.unpriced_usd += self.unpriced_usd

    def _in_flight(self) -> bool:
        return self._open_reservations > 0 or any(c._in_flight() for c in self._open_children)

    def _settle(self, estimate: Decimal, charged: Decimal, unpriced: bool) -> None:
        with self._lock:
            self._reserved -= estimate
            self._open_reservations -= 1
            self._spent += charged
            if unpriced:
                self.unpriced_calls += 1
                self.unpriced_usd += charged
            crossed, node = None, self
            while node is not None:  # the outermost limit crossed wins
                if node._limit is not None and node.total_spent() > node._limit:
                    crossed = node
                node = node.parent
            if crossed is not None:
                raise LimitHit(crossed.scope, crossed.name, crossed._limit, crossed.total_spent())

    def _release(self, estimate: Decimal) -> None:
        with self._lock:
            self._reserved -= estimate
            self._open_reservations -= 1

    def _check_open(self) -> None:
        if self.closed:
            raise BudgetError(f"{self.scope} {self.name!r} is closed")


class Reservation:
    """An estimate held for one call: settle it with the real cost, or release it."""

    def __init__(self, slice_: BudgetSlice, estimate_usd: Decimal) -> None:
        self._slice, self.estimate_usd = slice_, estimate_usd
        self._done = False
        self._lock = slice_._lock

    def settle(self, cost: CallCost) -> Decimal:
        """Charge the call's cost (the estimate if it is unknown); returns the amount charged.
        Raises LimitHit AFTER recording, at the outermost limit now crossed."""
        with self._lock:
            unpriced = cost.cost_usd is None
            charged = self.estimate_usd if unpriced else _amount(cost.cost_usd, "cost")
            self._finish()
            self._slice._settle(self.estimate_usd, charged, unpriced)
            return charged

    def release(self) -> None:
        """The call failed: nothing was spent."""
        with self._lock:
            self._finish()
            self._slice._release(self.estimate_usd)

    def _finish(self) -> None:
        if self._done:
            raise BudgetError("this reservation was already settled or released")
        self._done = True


def _amount(value: Decimal | None, what: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise BudgetError(f"{what} must be a finite, non-negative Decimal, not {value!r}")
    return value
