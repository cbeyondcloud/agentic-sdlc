"""The run's feedback journal (M3-11) over Zyroflo's `feedback.jsonl` (D34: reuse, never rewrite).

Zyroflo keeps a run's corrections in `feedback.jsonl` beside its approval ledger
(`tools.sdlc.feedback`): an append that never shares a line with a torn or unterminated
last line, a reader that marks a damaged line instead of dropping it, and de-duplication
by each row's `id`. The framework writes its learning-loop records into that same file,
through those same calls. What this adapter adds:

  * a chain - each of our rows carries `seq`, `prev` and `hash` (Zyroflo's `entry_hash`,
    as the run ledger and the personal-backup journal use), so the control plane verifies
    a delivered batch entry by entry and a replay is a no-op;
  * a lock - the read of the chain head and the append happen under one `FileLock`, so a
    gate decision and an `agentic learn capture-pr` writing at once never fork the chain
    (Windows and macOS alike);
  * a filter - only rows whose `event` starts with `learning.` are ours; a row Zyroflo's
    own `feedback.capture` wrote (`gate`, `artifact-edit`, ...) is left alone and is not
    part of the chain.

The rows hold prose (a person's feedback, a diff): that is why they live here and never in
the run ledger, whose events carry ids only.
"""

from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from filelock import FileLock
from tools.sdlc import feedback

from sdlc_commons.ledger import entry_hash, run_id_refusal

EVENT_PREFIX = "learning."
#: The fields the chain owns; a caller's fields may not set them.
CHAIN_FIELDS = frozenset({"seq", "prev", "hash", "at", "event", "id"})


class FeedbackJournal:
    """The feedback journal of one run (its folder may not exist yet)."""

    def __init__(self, target: Path, run_id: str) -> None:
        if why := run_id_refusal(run_id):
            raise ValueError(why)
        self.target = Path(target)
        self.run_id = run_id

    @property
    def path(self) -> Path:
        return feedback.path_of(self.target, self.run_id)

    @property
    def run_dir(self) -> Path:
        return self.path.parent

    def entries(self) -> list[dict]:
        """Our rows in file order (damaged lines and Zyroflo's own rows are left out)."""
        return [
            row
            for row in feedback.read(self.target, self.run_id)
            if "_unreadable" not in row and str(row.get("event", "")).startswith(EVENT_PREFIX)
        ]

    def append(
        self,
        event: str,
        fields: Mapping[str, object],
        *,
        row_id: str,
        clock: Callable[[], datetime],
    ) -> dict | None:
        """Chain and append one row; None when a row with `row_id` is already there."""
        if not event.startswith(EVENT_PREFIX):
            raise ValueError(f"a feedback journal event starts with {EVENT_PREFIX!r}: {event!r}")
        if clash := sorted(CHAIN_FIELDS & set(fields)):
            raise ValueError(f"the journal writes {', '.join(clash)} itself")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(self.run_dir / "feedback.lock"):
            mine = self.entries()
            if any(row.get("id") == row_id for row in mine):
                return None
            last = mine[-1] if mine else None
            entry = {
                **fields,
                "id": row_id,
                "event": event,
                "at": clock().isoformat(timespec="seconds"),
                "seq": (int(last["seq"]) + 1) if last else 1,
                "prev": str(last["hash"]) if last else "",
            }
            entry["hash"] = entry_hash(entry)
            if feedback.append(self.target, self.run_id, [entry]) != 1:
                return None
        return entry

    def verify(self) -> list[str]:
        """Every problem in our chain ([] = intact), naming each row by its position."""
        problems: list[str] = []
        prev = ""
        for n, entry in enumerate(self.entries(), 1):
            if entry.get("seq") != n:
                problems.append(f"entry {n}: seq {entry.get('seq')!r}, expected {n}")
            if entry.get("prev", "") != prev:
                problems.append(f"entry {n}: does not link to the entry before it")
            if entry.get("hash") != entry_hash(entry):
                problems.append(f"entry {n}: its hash does not match its contents")
            prev = str(entry.get("hash", ""))
        return problems


__all__ = ["CHAIN_FIELDS", "EVENT_PREFIX", "FeedbackJournal"]
