"""Gate decisions over Zyroflo's approval ledger (SP6, D34): the engine's GateLedger.

  * `record`  - one hash-chained entry in the run's `approvals.jsonl`, pinning the
                gated artefacts by digest, and the `gate_decided` event
                (`approve.append_entry`, Zyroflo T688 / Z6)
  * `state`   - is the gate satisfied right now: an approval over bytes that have
                not changed, with no rejection over the same bytes
                (`approve.gate_state`)
  * `waiting` - the `gate_waiting` event, when the run stops at the gate (with the
                gate's approver role in our ext block)

Plus two pure rules the engine applies: the blocking severities of a regime
(`policy.PRESETS`) and the self-approval check (`policy.self_approval`).
Zyroflo is used unchanged; only this package imports it.
"""

from typing import Literal

from tools.sdlc import approve, policy

from sdlc_commons.ledger import LedgerError, RunLedger


class ZyrofloGates:
    """The engine's `GateLedger` for one run."""

    def __init__(self, ledger: RunLedger) -> None:
        self.ledger = ledger

    def record(
        self,
        gate: str,
        verdict: Literal["approve", "reject"],
        approver: str,
        artefacts: dict[str, str],
        *,
        step: str,
        notes: str = "",
        extra: dict | None = None,
    ) -> str:
        """Append the decision; returns the entry's hash."""
        try:
            entry = approve.append_entry(
                self.ledger.target,
                self.ledger.run_id,
                gate,
                verdict,
                approver,
                dict(artefacts),
                step=step,
                notes=notes,
                extra=extra,
            )
        except (ValueError, RuntimeError, OSError) as error:
            raise LedgerError(f"{gate}: the decision could not be recorded ({error})") from error
        return entry["hash"]

    def state(self, gate: str) -> tuple[bool, str]:
        return approve.gate_state(self.ledger.target, self.ledger.run_id, gate)

    def waiting(self, gate: str, step: str, *, role: str | None = None) -> None:
        """The `gate_waiting` event; `role` (the gate's approver role) goes in our ext
        block, so the org's control plane can mail the people holding it."""
        self.ledger.record(
            "gate_waiting",
            step,
            ext_ids={"approver_role": role} if role else None,
            gate=gate,
            hard=True,
        )


def regime_block_on(regime: str) -> frozenset[str]:
    """The finding severities that keep a review loop open under `regime`."""
    if regime not in policy.PRESETS:
        raise ValueError(f"unknown regime {regime!r} (known: {', '.join(policy.PRESETS)})")
    return frozenset(policy.PRESETS[regime]["block_on"])


def self_approval_refusal(approver: str, starter: str) -> str | None:
    """Why `approver` may not approve a run `starter` started (four-eyes), or None."""
    refusals = policy.self_approval({"self_approval": "forbid"}, approver, starter)
    return refusals[0] if refusals else None


__all__ = ["ZyrofloGates", "regime_block_on", "self_approval_refusal"]
