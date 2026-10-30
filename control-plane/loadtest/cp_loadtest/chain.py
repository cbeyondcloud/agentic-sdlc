"""A simulated run's ledger: the template's events, chained again for a new run (M3-22).

Each entry keeps the template's fields (so its size is a real event's) and gets the run's
id, its team and policy version, its own time, `seq` and `prev`, and `hash` from
`sdlc_commons.ledger.entry_hash` - Zyroflo's formula, the one the control plane verifies
an ingested batch with."""

from __future__ import annotations

import copy
from datetime import datetime

from sdlc_commons.ledger import NAMESPACE, entry_hash


class RunChain:
    def __init__(self, template: list[dict], *, run_id: str, team: str, policy_version: str):
        self._template = template
        self.run_id = run_id
        self.team = team
        self.policy_version = policy_version
        self.entries: list[dict] = []

    @property
    def done(self) -> bool:
        return len(self.entries) == len(self._template)

    @property
    def head(self) -> tuple[int, str]:
        return (len(self.entries), self.entries[-1]["hash"] if self.entries else "")

    def next(self, at: datetime) -> dict:
        """The run's next event, stamped `at`; IndexError once the template is used up."""
        if self.done:
            raise IndexError(f"run {self.run_id} already holds every template event")
        entry = copy.deepcopy(self._template[len(self.entries)])
        entry.pop("hash", None)
        entry["run_id"] = self.run_id
        entry["seq"] = len(self.entries) + 1
        entry["prev"] = self.head[1]
        entry["at"] = at.isoformat(timespec="milliseconds")
        block = entry.setdefault("ext", {}).setdefault(NAMESPACE, {})
        block["team"] = self.team
        block["policy_version"] = self.policy_version
        if entry.get("event") == "run_opened":
            entry["owner"] = self.team
        entry["hash"] = entry_hash(entry)
        self.entries.append(entry)
        return entry
