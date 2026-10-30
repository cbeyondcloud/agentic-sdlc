"""Proposals kept on the laptop, for standalone mode (M3-13, M3-15, M3-16).

`.agentic/local/learning/proposals.json` (git-ignored, like the run folders) holds every
proposal `propose` made for the repository's runs, with its status, who decided it and who
endorsed it. It is written whole, atomically, under a `FileLock`, so two CLI processes (a
gate decision and a `capture-pr`) never lose each other's update. A rejection is kept, with
when it was made, so the same lesson is not proposed again without new evidence. Decisions
move only along `approval.TRANSITIONS`.

`changes/<id>.json` beside it records, per proposal, the change as approved (the draft),
how it was applied (a personal write or a pull request) and how it was reverted. In org
mode the control plane holds the team's proposals instead; the change records stay here
for the person who applied them.
"""

import json
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from filelock import FileLock

from agentic.learning.approval import next_status
from agentic.learning.clustering import Proposal

FILE = "proposals.json"
CHANGES = "changes"
PROPOSAL_ID = re.compile(r"lp-[0-9a-f]{20}")


class StoreError(Exception):
    """The proposals file cannot be read, or names no such proposal."""


def _to_json(proposal: Proposal) -> dict:
    data = asdict(proposal)
    data["created_at"] = proposal.created_at.isoformat()
    data["decided_at"] = proposal.decided_at.isoformat() if proposal.decided_at else None
    return data


def _from_json(data: dict) -> Proposal:
    return Proposal(
        id=data["id"],
        agent=data["agent"],
        scope=data["scope"],
        lesson=data["lesson"],
        evidence=tuple(data["evidence"]),
        sources=tuple(data["sources"]),
        actors=tuple(data["actors"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        status=data.get("status", "pending"),
        decided_at=datetime.fromisoformat(data["decided_at"]) if data.get("decided_at") else None,
        decided_by=data.get("decided_by"),
        endorsements=tuple(data.get("endorsements") or ()),
    )


class LocalLearningStore:
    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self.path = self.folder / FILE

    def proposals(self) -> list[Proposal]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as error:
            raise StoreError(f"{self.path} cannot be read ({error})") from None
        try:
            return [_from_json(item) for item in data["proposals"]]
        except (KeyError, TypeError, ValueError) as error:
            raise StoreError(f"{self.path} is not a proposals file ({error})") from None

    def save(self, changed: Sequence[Proposal]) -> None:
        """Add or replace (by id) each of `changed`."""
        if not changed:
            return
        with self._lock():
            held = {p.id: p for p in self.proposals()}
            held.update({p.id: p for p in changed})
            self._write(list(held.values()))

    def reject(self, proposal_id: str, when: datetime, by: str | None = None) -> Proposal:
        """Mark a proposal rejected."""
        return self.decide(proposal_id, "reject", by, when)

    def get(self, proposal_id: str) -> Proposal:
        for proposal in self.proposals():
            if proposal.id == proposal_id:
                return proposal
        raise StoreError(f"there is no proposal {proposal_id}")

    def decide(self, proposal_id: str, action: str, by: str | None, when: datetime) -> Proposal:
        """Move a proposal along `approval.TRANSITIONS` (DecisionError when it may not)."""
        return self._change(
            proposal_id,
            lambda p: replace(
                p, status=next_status(p.status, action), decided_at=when, decided_by=by
            ),
        )

    def endorse(self, proposal_id: str, by: str) -> Proposal:
        """Add `by` to the proposal's endorsements (once): weight, never a decision."""
        return self._change(
            proposal_id,
            lambda p: replace(p, endorsements=tuple(dict.fromkeys([*p.endorsements, by]))),
        )

    def _change(self, proposal_id: str, how: Callable[[Proposal], Proposal]) -> Proposal:
        with self._lock():
            held = self.proposals()
            for index, proposal in enumerate(held):
                if proposal.id == proposal_id:
                    held[index] = how(proposal)
                    self._write(held)
                    return held[index]
        raise StoreError(f"there is no proposal {proposal_id}")

    # ── change records (M3-14..M3-16) ────────────────────────────────────────

    def change(self, proposal_id: str) -> dict | None:
        """What was drafted, applied and reverted for a proposal (None: nothing yet)."""
        path = self._change_path(proposal_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise StoreError(f"{path} cannot be read ({error})") from None

    def save_change(self, proposal_id: str, record: dict) -> None:
        path = self._change_path(proposal_id)
        with self._lock():
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, json.dumps(record, indent=2, sort_keys=True) + "\n")

    def _change_path(self, proposal_id: str) -> Path:
        if not PROPOSAL_ID.fullmatch(proposal_id):
            raise StoreError(f"{proposal_id!r} is not a proposal id (lp- and 20 hex digits)")
        return self.folder / CHANGES / f"{proposal_id}.json"

    def _lock(self) -> FileLock:
        self.folder.mkdir(parents=True, exist_ok=True)
        return FileLock(self.folder / f"{FILE}.lock")

    def _write(self, proposals: list[Proposal]) -> None:
        text = json.dumps({"proposals": [_to_json(p) for p in proposals]}, indent=2) + "\n"
        _atomic_write(self.path, text)


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


__all__ = ["FILE", "LocalLearningStore", "StoreError"]
