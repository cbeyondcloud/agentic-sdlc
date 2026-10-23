"""Skill and memory activations into the run ledger (M2-45; spec §24: "every skill activation
is logged with kind, name, version and hash - in the run ledger").

`SkillActivity` is the `record` callback of the shared skills library and of the personal
context. It keeps the logging those callbacks always did (`agentic.skills`), and collects
each `skills.activated` as one ledger entry: `<kind>/<name>/<version>/<sha256 hex>`, cut
down to the pattern the ledger declares for `ext.agentic.skills` (lowercase letters,
digits, `.`, `_`, `-`; segments joined by `/`). `StampingRecorder` wraps a run's recorder.
On each `step_finished` it hands the entries collected since the last one to the ledger
(`skills=`), inside the hash chain. The control plane therefore receives them with the
event, through the existing sink.

One CLI process drives one run at a time, so the activations since the last
`step_finished` are that run's current step's (a child agent's count for its parent's
step).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from pathlib import Path

from agentic.skills.install import log_record

#: Entries per event: the ledger caps an event's ext block at 4 KiB.
MAX_PER_EVENT = 24
_UNSAFE = re.compile(r"[^a-z0-9._-]+")
_HEX = re.compile(r"[0-9a-f]{64}")


def _segment(text: str) -> str:
    cleaned = _UNSAFE.sub("-", text.lower()).strip("-._")
    return cleaned or "x"


def ledger_entry(fields: Mapping[str, object]) -> str | None:
    """The ledger entry for one `skills.activated`, or None when it carries no hash."""
    digest = str(fields.get("sha256") or "").removeprefix("sha256:")
    if not _HEX.fullmatch(digest):
        return None
    key = str(fields.get("skill") or "")
    kind = str(fields.get("kind") or "shared")
    path = key if kind in ("personal", "memory") else f"shared/{key}"
    name = "/".join(_segment(part) for part in path.split("/") if part)
    return f"{name}/{_segment(str(fields.get('version') or 'local'))}/{digest}"


class SkillActivity:
    def __init__(self) -> None:
        self._pending: list[str] = []
        self._lock = threading.Lock()

    def record(self, event: str, fields: dict[str, object]) -> None:
        log_record(event, fields)  # the audit log, as before
        if event != "skills.activated":
            return
        entry = ledger_entry(fields)
        if entry is None:
            return
        with self._lock:
            if entry not in self._pending:
                self._pending.append(entry)

    def take(self) -> list[str]:
        with self._lock:
            taken, self._pending = self._pending, []
        return taken


class StampingRecorder:
    """A run recorder whose `step_finished` events carry the step's activations."""

    def __init__(self, inner, activity: SkillActivity) -> None:
        self.inner, self.activity = inner, activity

    @property
    def run_dir(self) -> Path:
        return self.inner.run_dir

    def record(self, event: str, phase: str = "", **fields: object) -> object:
        if event == "step_finished" and (skills := self.activity.take()):
            fields["skills"] = skills[:MAX_PER_EVENT]
            if left := skills[MAX_PER_EVENT:]:  # still in the audit log, named here
                log_record("skills.ledger_truncated", {"step": phase, "not_in_ledger": left})
        return self.inner.record(event, phase, **fields)

    def write_artifact(self, rel: str, text: str) -> str:
        return self.inner.write_artifact(rel, text)

    def digest(self, rel: str) -> str:
        return self.inner.digest(rel)


__all__ = ["SkillActivity", "StampingRecorder", "ledger_entry"]
