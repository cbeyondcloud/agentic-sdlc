"""Run-ledger adapter over Zyroflo's hash-chained ledger (D34: reuse, never rewrite).

Every ledger operation is a call into Zyroflo (`tools.sdlc.events`, `.approve`,
`.state`): the event format, the hash chain and tamper detection are Zyroflo's.
This module only adds what the framework needs around it:

  * RunLedger   - one object per run, opening its folder where Zyroflo expects it;
  * write_artifact - handoffs are always written with LF line endings, so the same
                  document has the same digest on Windows and Linux (Review Focus 1);
  * recover_torn_tail - an append interrupted by a crash leaves half a line, which
                  Zyroflo (correctly) refuses to build on; the fragment is moved to a
                  preserved file so the chain verifies again (Review Focus 2);
  * the framework's ids (M1-07) - team, policy version, agent@version, parent, tier
                  and skills ride each event in Zyroflo's declared `ext` block (Z1),
                  inside the hash chain.
"""

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tools.sdlc import approve, events, state

EVENTS_FILE = "events.jsonl"
APPROVALS_FILE = "approvals.jsonl"

#: Our namespace in Zyroflo's `ext` block.
NAMESPACE = "agentic"
_SLUG = r"[a-z][a-z0-9-]{0,63}"
_AGENT = _SLUG + r"@[0-9A-Za-z][0-9A-Za-z.+-]{0,31}"
#: The ids the framework adds to events. Every field has its own pattern, so only
#: an id of that shape can be recorded - never a note, a prompt or an email.
FIELDS = {
    "team": {"type": "string", "pattern": _SLUG},
    "policy_version": {"type": "string", "pattern": r"git:[0-9a-f]{40}|sha256:[0-9a-f]{64}"},
    "agent": {"type": "string", "pattern": _AGENT},
    "parent": {"type": "string", "pattern": _AGENT},
    "tier": {"type": "string", "pattern": _SLUG},
    "skills": {"type": "string[]", "pattern": r"[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*"},
}


class LedgerError(Exception):
    """A ledger operation was refused (e.g. an unknown event field, or a torn log)."""


@dataclass(frozen=True)
class RunIdentity:
    """Whose run it is and which policy governed it: stamped on EVERY event of the run."""

    team: str
    policy_version: str

    def ext(self) -> dict:
        return {"team": self.team, "policy_version": self.policy_version}


@dataclass(frozen=True)
class AgentIdentity:
    """Which agent an event is about: added to that event only."""

    name: str
    version: str
    tier: str | None = None
    parent: str | None = None  # "<name>@<version>" of the agent that spawned this one
    skills: tuple[str, ...] = ()

    def ext(self) -> dict:
        out: dict = {"agent": f"{self.name}@{self.version}"}
        if self.parent:
            out["parent"] = self.parent
        if self.tier:
            out["tier"] = self.tier
        if self.skills:
            out["skills"] = list(self.skills)
        return out


#: run id -> its identity, for Zyroflo's default ext provider below.
_IDENTITIES: dict[str, RunIdentity] = {}
_LOCK = threading.Lock()


def _provide(event: str, run_id: str) -> dict | None:
    """Zyroflo's default ext provider, so events Zyroflo writes itself (gates,
    moves) carry the run's identity too. It reads only the map above, never the
    run folder: Zyroflo asks it before a run exists (its T465 pre-write check)."""
    identity = _IDENTITIES.get(run_id)
    return {NAMESPACE: identity.ext()} if identity else None


def _install() -> None:
    """Declare our fields and our provider to Zyroflo (both idempotent). The
    provider is process-wide; sdlc_commons is the only package that uses Zyroflo (D34)."""
    events.register_extension(NAMESPACE, FIELDS)
    events.set_default_ext(_provide)


class RunLedger:
    """The append-only, hash-chained record of one run."""

    def __init__(self, target: Path, run_id: str, identity: RunIdentity | None = None) -> None:
        self.target = target
        self.run_id = run_id
        if identity is not None:
            self._bind(identity)

    def _bind(self, identity: RunIdentity) -> None:
        """Register the run's identity, refusing a bad one before anything is written."""
        _install()
        with _LOCK:
            previous = _IDENTITIES.get(self.run_id)
            _IDENTITIES[self.run_id] = identity
        try:
            events.check_default_ext(self.run_id)
        except events.EventError as error:
            with _LOCK:
                if previous is None:
                    _IDENTITIES.pop(self.run_id, None)
                else:
                    _IDENTITIES[self.run_id] = previous
            raise LedgerError(str(error)) from error

    @property
    def folder(self) -> Path:
        """The run's active folder: `<target>/.sdlc/runs/<run_id>/` (Zyroflo layout)."""
        return state.runs_dir(self.target) / self.run_id

    def open(self, *, kind: str, project: str, owner: str, criticality: str) -> dict:
        """Create the run folder and record `run_opened`."""
        # Zyroflo writes to the active folder only if it already exists.
        self.folder.mkdir(parents=True, exist_ok=True)
        return self.record("run_opened", kind=kind, project=project, owner=owner,
                           criticality=criticality)  # fmt: skip

    def record(
        self, event: str, phase: str = "", agent: AgentIdentity | None = None, **fields: object
    ) -> dict:
        """Append one event to the chain (Zyroflo validates the event and its fields).
        `agent` adds that agent's ids to this event; the run's identity is added by
        the provider."""
        ext = None
        if agent is not None:
            _install()
            ext = {NAMESPACE: agent.ext()}
        try:
            return events.append(self.target, self.run_id, event, phase=phase, ext=ext, **fields)
        except events.EventError as error:
            raise LedgerError(str(error)) from error

    def events(self) -> list[dict]:
        try:
            return events.read(self.target, self.run_id)
        except events.EventError as error:
            raise LedgerError(str(error)) from error

    def verify(self) -> list[str]:
        """Every problem found in the events and approvals chains ([] = intact)."""
        problems: list[str] = []
        if self._torn_fragment() is not None:
            problems.append(f"{EVENTS_FILE}: torn last line (an interrupted write); "
                            "recover it with recover_torn_tail()")  # fmt: skip
        try:
            problems += events.verify(self.target, self.run_id)
        except events.EventError as error:
            problems.append(str(error))
        if (self.folder / APPROVALS_FILE).is_file():
            problems += approve.verify_chain(self.target, self.run_id)
        return problems

    def recover_torn_tail(self) -> Path | None:
        """Move a half-written last line aside so the chain is whole again.

        Only a final line that is not valid JSON and has no newline counts as torn:
        it was never a complete record, so it was never part of the chain. It is
        preserved next to the log, never deleted. Returns the preserved file, or
        None when there was nothing to recover.
        """
        fragment = self._torn_fragment()
        if fragment is None:
            return None
        log = self.folder / EVENTS_FILE
        data = log.read_bytes()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        saved = self.folder / f"{EVENTS_FILE}.torn-{stamp}"
        saved.write_bytes(fragment)
        log.write_bytes(data[: len(data) - len(fragment)])
        return saved

    def _torn_fragment(self) -> bytes | None:
        log = self.folder / EVENTS_FILE
        if not log.is_file():
            return None
        data = log.read_bytes()
        if not data or data.endswith(b"\n"):
            return None
        last = data.rsplit(b"\n", 1)[-1]
        try:
            json.loads(last)
            return None  # a complete record that only lacks its newline
        except ValueError:
            return last


def write_artifact(path: Path, text: str) -> str:
    """Write a handoff/artifact with LF line endings and return its digest.

    Zyroflo pins approvals to the stored bytes, so line endings must not depend on
    the OS that wrote the file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    path.write_bytes(normalised.encode("utf-8"))
    return digest(path)


def digest(path: Path) -> str:
    """Zyroflo's digest of one file (sha256 of its bytes)."""
    return state.sha256_of(path)
