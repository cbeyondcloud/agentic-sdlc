"""Skill and memory activations into the run ledger (M2-45; spec §24: "every skill activation
is logged with kind, name, version and hash - in the run ledger").

`SkillActivity` is the `record` callback of the shared skills library and of the personal
context. It keeps the logging those callbacks always did (`agentic.skills`), and collects
each `skills.activated` as one ledger entry: `<kind>/<name>/<version>/<sha256 hex>`, cut
down to the pattern the ledger declares for `ext.agentic.skills` (lowercase letters,
digits, `.`, `_`, `-`; segments joined by `/`) and to its 128 characters (a long name or
version is shortened, the content hash kept whole). `StampingRecorder` wraps a run's
recorder. On each `step_finished` it hands the entries collected since the last one to the
ledger (`skills=`), inside the hash chain. The control plane therefore receives them with
the event, through the existing sink. Should the ledger still refuse them, the
`step_finished` is recorded without them and the audit log names them
(`skills.ledger_refused`): a stamp never costs a step its event.

One CLI process drives one run at a time, so the activations since the last
`step_finished` are that run's current step's (a child agent's count for its parent's
step).
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Mapping
from pathlib import Path

from agentic.skills.install import log_record
from sdlc_commons.ledger import LedgerError

#: Entries per event: the ledger caps an event's ext block at 4 KiB.
MAX_PER_EVENT = 24
#: Zyroflo's id rule caps every ext value at 128 characters.
MAX_ENTRY = 128
#: A longer version is shortened (the content hash still tells versions apart).
MAX_VERSION = 24
_TAG = 8  # hex characters of the hash that marks a shortened name or version
_UNSAFE = re.compile(r"[^a-z0-9._-]+")
_HEX = re.compile(r"[0-9a-f]{64}")


def _segment(text: str) -> str:
    cleaned = _UNSAFE.sub("-", text.lower()).strip("-._")
    return cleaned or "x"


def _shortened(text: str, limit: int) -> str:
    """`text`, or its first characters and a short hash of all of it, within `limit`."""
    if len(text) <= limit:
        return text
    tag = hashlib.sha256(text.encode("utf-8")).hexdigest()[:_TAG]
    return f"{text[: limit - _TAG - 1].rstrip('-._/')}-{tag}"


def ledger_entry(fields: Mapping[str, object]) -> str | None:
    """The ledger entry for one `skills.activated`, or None when it carries no hash.

    A long name (a dated note's file name) or version is shortened to its first
    characters and a short hash of the whole, so the entry fits the ledger's
    `MAX_ENTRY`; the kind and the full content hash are always kept."""
    digest = str(fields.get("sha256") or "").removeprefix("sha256:")
    if not _HEX.fullmatch(digest):
        return None
    key = str(fields.get("skill") or "")
    kind = str(fields.get("kind") or "shared")
    path = key if kind in ("personal", "memory") else f"shared/{key}"
    name = "/".join(_segment(part) for part in path.split("/") if part)
    version = _shortened(_segment(str(fields.get("version") or "local")), MAX_VERSION)
    name = _shortened(name, MAX_ENTRY - len(version) - len(digest) - 2)
    return f"{name}/{version}/{digest}"


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
            stamped = skills[:MAX_PER_EVENT]
            if left := skills[MAX_PER_EVENT:]:  # still in the audit log, named here
                log_record("skills.ledger_truncated", {"step": phase, "not_in_ledger": left})
            try:
                return self.inner.record(event, phase, skills=stamped, **fields)
            except LedgerError as error:
                # the step's own event matters more than its stamp: record it without
                # one, and name what the ledger refused in the audit log
                refused = {"step": phase, "not_in_ledger": stamped, "error": str(error)}
                log_record("skills.ledger_refused", refused)
        return self.inner.record(event, phase, **fields)

    def write_artifact(self, rel: str, text: str) -> str:
        return self.inner.write_artifact(rel, text)

    def digest(self, rel: str) -> str:
        return self.inner.digest(rel)


__all__ = ["SkillActivity", "StampingRecorder", "ledger_entry"]
