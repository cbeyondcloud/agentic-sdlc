"""Proposals kept on the laptop, for standalone mode (M3-13).

`.agentic/local/learning/proposals.json` (git-ignored, like the run folders) holds every
proposal `propose` made for the repository's runs, pending or rejected. It is written
whole, atomically, under a `FileLock`, so two CLI processes (a gate decision and a
`capture-pr`) never lose each other's update. A rejection is kept, with when it was
made, so the same lesson is not proposed again without new evidence. In org mode the
control plane holds the team's proposals instead; this file is not used there.
"""

import json
import os
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from filelock import FileLock

from agentic.learning.clustering import Proposal

FILE = "proposals.json"


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

    def reject(self, proposal_id: str, when: datetime) -> Proposal:
        """Mark a proposal rejected (L5's `agentic learn reject` uses this)."""
        with self._lock():
            held = self.proposals()
            for index, proposal in enumerate(held):
                if proposal.id == proposal_id:
                    held[index] = replace(proposal, status="rejected", decided_at=when)
                    self._write(held)
                    return held[index]
        raise StoreError(f"there is no proposal {proposal_id}")

    def _lock(self) -> FileLock:
        self.folder.mkdir(parents=True, exist_ok=True)
        return FileLock(self.folder / f"{FILE}.lock")

    def _write(self, proposals: list[Proposal]) -> None:
        temporary = self.path.with_name(f"{FILE}.{os.getpid()}.tmp")
        text = json.dumps({"proposals": [_to_json(p) for p in proposals]}, indent=2) + "\n"
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)


__all__ = ["FILE", "LocalLearningStore", "StoreError"]
