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
import os
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tools.sdlc import approve, events, state

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
    """The append-only, hash-chained record of one run.

    A run has ONE identity (team, policy version) for its whole life: given to
    `open`, read back by `resume` in a later process, refused if another is
    offered. Nothing is recorded for a run whose identity this process does not
    hold, so no event is ever written unlabelled."""

    def __init__(self, target: Path, run_id: str) -> None:
        if why := run_id_refusal(run_id):
            raise LedgerError(why)
        self.target = Path(target)
        self.run_id = run_id

    @property
    def folder(self) -> Path:
        """The run's folder (Zyroflo layout, under `.agentic/local/runs/`): the active
        one, or the archived one if the run was archived."""
        active = state.runs_dir(self.target) / self.run_id
        archived = state.archive_dir(self.target) / self.run_id
        return archived if archived.is_dir() and not active.is_dir() else active

    # ── identity ──────────────────────────────────────────────────────────────

    def open(self, *, identity: RunIdentity, kind: str, project: str, owner: str,
             criticality: str) -> dict:  # fmt: skip
        """Start the run under `identity` and record `run_opened`. Every input is
        checked before the run folder exists."""
        if self._has_events():
            recorded = self._recorded_identity()
            if recorded is not None and recorded != identity:
                raise LedgerError(f"run {self.run_id} already belongs to another team or "
                                  f"policy version - its identity never changes")  # fmt: skip
            raise LedgerError(f"run {self.run_id} is already open - resume() it instead")
        fields = {"kind": kind, "project": project, "owner": owner, "criticality": criticality}
        _check_fields(fields)
        self._bind(identity)
        self.folder.mkdir(
            parents=True, exist_ok=True
        )  # Zyroflo appends only into an existing folder
        return self._append("run_opened", "", None, fields)

    def resume(self) -> RunIdentity:
        """Take up a run opened earlier (perhaps by another process): its identity
        is read back from its `run_opened` event."""
        identity = self._recorded_identity()
        if identity is None:
            raise LedgerError(f"run {self.run_id} has no recorded identity - open() it first")
        self._bind(identity)
        return identity

    def release(self) -> None:
        """Forget the run's identity in this process (call once the run is closed)."""
        with _LOCK:
            _IDENTITIES.pop(self.run_id, None)

    def _bind(self, identity: RunIdentity) -> None:
        """Hold the run's identity for this process, refusing a bad or different one
        before anything is written."""
        _install()
        with _LOCK:
            held = _IDENTITIES.get(self.run_id)
            if held is not None and held != identity:
                raise LedgerError(f"run {self.run_id} already belongs to another team or "
                                  f"policy version in this process")  # fmt: skip
            _IDENTITIES[self.run_id] = identity
        try:
            events.check_default_ext(self.run_id)
        except events.EventError as error:
            if held is None:
                with _LOCK:
                    _IDENTITIES.pop(self.run_id, None)
            raise LedgerError(str(error)) from error

    def _recorded_identity(self) -> RunIdentity | None:
        first = next(iter(self.events()), None)
        ids = _agentic(first) if first else {}
        if "team" not in ids or "policy_version" not in ids:
            return None
        return RunIdentity(team=ids["team"], policy_version=ids["policy_version"])

    # ── events ────────────────────────────────────────────────────────────────

    def record(
        self, event: str, phase: str = "", agent: AgentIdentity | None = None, **fields: object
    ) -> dict:
        """Append one event to the chain. Every value is an id - no free text, no
        emails - and the ledger stamps the time. `agent` adds that agent's ids to
        this event; the run's identity is added to every event."""
        if self.run_id not in _IDENTITIES:
            raise LedgerError(f"run {self.run_id} has no identity in this process - open() or "
                              f"resume() it first")  # fmt: skip
        if "at" in fields:
            raise LedgerError("the ledger stamps each event's time - 'at' cannot be passed")
        _check_fields({"phase": phase, **fields} if phase else fields)
        return self._append(event, phase, agent, fields)

    def _append(self, event: str, phase: str, agent: AgentIdentity | None, fields: dict) -> dict:
        ext = None
        if agent is not None:
            _install()
            ext = {NAMESPACE: agent.ext()}
        try:
            return events.append(self.target, self.run_id, event, phase=phase, ext=ext, **fields)
        except (events.EventError, ValueError, OSError) as error:
            raise LedgerError(str(error)) from error

    def events(self) -> list[dict]:
        try:
            return events.read(self.target, self.run_id)
        except (events.EventError, ValueError, OSError) as error:
            raise LedgerError(str(error)) from error

    def _has_events(self) -> bool:
        log = events.events_path(self.target, self.run_id)
        return log.is_file() and log.stat().st_size > 0

    def verify(self) -> list[str]:
        """Every problem found in the events and approvals chains ([] = intact),
        including an event whose identity is not the run's."""
        problems: list[str] = []
        for log in self._logs():
            if log.is_file() and _torn(log.read_bytes()) is not None:
                problems.append(f"{log.name}: torn last line (an interrupted write); "
                                "recover it with recover_torn_tail()")  # fmt: skip
        try:
            problems += events.verify(self.target, self.run_id)
            problems += self._identity_problems()
        except (events.EventError, LedgerError, ValueError) as error:
            problems.append(str(error))
        if approve.approvals_path(self.target, self.run_id).is_file():
            try:
                problems += approve.verify_chain(self.target, self.run_id)
            except (ValueError, OSError) as error:
                problems.append(f"approvals: {error}")
        return problems

    def _identity_problems(self) -> list[str]:
        recorded = self._recorded_identity()
        if recorded is None:
            return [f"run {self.run_id}: its first event carries no run identity"]
        expected = recorded.ext()
        return [f"event {n} ({e.get('event')}): run identity differs from the run's"
                for n, e in enumerate(self.events(), 1)
                if {k: _agentic(e).get(k) for k in expected} != expected]  # fmt: skip

    # ── a crash mid-append ────────────────────────────────────────────────────

    def recover_torn_tail(self) -> list[Path]:
        """Move a half-written last line aside, in the events and approvals logs,
        so each chain is whole again; returns the files the fragments were saved to.

        Only a final line that is not valid JSON and has no newline counts as torn:
        it was never a complete record, so it was never part of the chain. The
        fragment is saved (and flushed to disk) BEFORE the log is truncated in
        place, and the log is left alone if it changed meanwhile."""
        return [kept for log in self._logs() if (kept := _recover(log)) is not None]

    def _logs(self) -> list[Path]:
        return [events.events_path(self.target, self.run_id),
                approve.approvals_path(self.target, self.run_id)]  # fmt: skip


#: A run id: one folder name, safe on Windows too.
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def run_id_refusal(run_id: object) -> str:
    """Why `run_id` cannot name a run, or "". Checked before any folder is made:
    a run id can come from a branch name, which anyone opening a PR chooses."""
    if isinstance(run_id, str) and _RUN_ID.fullmatch(run_id) and not run_id.endswith("."):
        return ""
    return (
        "a run id is letters, digits, '.', '-' or '_' (starting with a letter or digit, "
        "not ending with '.', at most 100)"
    )


#: An event value: an id, name, version, path or digest - never prose or an email.
_EVENT_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+=-]{0,127}")
_CRITICALITY = ("low", "medium", "high")


def _check_fields(fields: dict) -> None:
    """Refuse a value that is not an id. The message names the field, never the value."""
    for name, value in fields.items():
        if name == "criticality":
            if value not in _CRITICALITY:
                raise LedgerError(f"criticality must be one of {', '.join(_CRITICALITY)}")
            continue
        _check_value(name, value)


def _check_value(name: str, value: object) -> None:
    if isinstance(value, str):
        if not _EVENT_VALUE.fullmatch(value):
            raise LedgerError(f"event field {name!r} must be an id (letters, digits and "
                              f"._:/+=-; no spaces, no '@', at most 128) - events carry no "
                              f"free text")  # fmt: skip
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_value(f"{name} key", key)
            _check_value(name, item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_value(name, item)


def _agentic(event: dict) -> dict:
    ext = event.get("ext")
    return ext.get(NAMESPACE, {}) if isinstance(ext, dict) else {}


def _torn(data: bytes) -> bytes | None:
    """The half-written last line of a log, or None."""
    if not data or data.endswith(b"\n"):
        return None
    last = data.rsplit(b"\n", 1)[-1]
    try:
        json.loads(last)
        return None  # a complete record that only lacks its newline
    except ValueError:
        return last


def _recover(log: Path) -> Path | None:
    if not log.is_file():
        return None
    data = log.read_bytes()
    fragment = _torn(data)
    if fragment is None:
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    kept = log.with_name(f"{log.name}.torn-{stamp}")
    try:
        _save_durably(kept, fragment)
    except OSError as error:
        raise LedgerError(f"{log.name}: the torn line could not be preserved ({error}) - "
                          f"nothing was changed") from error  # fmt: skip
    try:
        with log.open("r+b") as fh:
            if fh.seek(0, os.SEEK_END) != len(data):
                raise LedgerError(f"{log.name} changed while it was being recovered - "
                                  f"nothing was truncated; retry")  # fmt: skip
            fh.truncate(len(data) - len(fragment))
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as error:
        raise LedgerError(f"{log.name}: could not truncate the torn line ({error}); it is "
                          f"preserved in {kept.name}") from error  # fmt: skip
    return kept


def _save_durably(path: Path, data: bytes) -> None:
    """Write a new file and flush it to disk before anything depends on it."""
    with path.open("xb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


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
