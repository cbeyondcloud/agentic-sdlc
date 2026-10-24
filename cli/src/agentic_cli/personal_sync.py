"""Backing up personal skills and notes to the control plane (M2-20; spec §24: "synced to
the control plane for backup/audit").

`~/.agentic/sync/personal/` holds:

  * `journal.jsonl` - one chained event per change, append-only: `personal.saved` (the
    file's text, exactly its bytes), `personal.removed`, or `personal.held` (the publish
    scan, M2-49, found a secret or personal data: the file stays on the laptop; only its
    digest and the finding counts are written). Entries are chained with Zyroflo's entry
    hash (`sdlc_commons.ledger.entry_hash`), like a run ledger, so the control plane
    verifies them on ingest;
  * `state.json` - the sha256 last journaled per path, so an unchanged file is never
    journaled again;
  * `device.json` - this laptop's random device id: each device is its own chain at the
    control plane, so two laptops never fork one;
  * `cursors/<scope>.json` - how far each control plane (and signed-in user) has taken the
    journal.

The journal and a cursor ARE the offline queue (as a run's ledger and its sink cursor):
`deliver` hands the journal to `agentic.telemetry.sinks.forward`, which sends what the
control plane has not taken yet, in order, and moves the cursor only after it accepts.
Replays are no-ops there, so delivery is at least once and never twice. A `FileLock`
serialises the CLI processes that write the journal (an `agentic skills backup` and an
`agentic sync` at once, on Windows and macOS alike).

Only Markdown and text files under `skills/` and `memory/` are backed up (the
instruction-only rule of M2-45); a link, a file over `MAX_CONTENT_BYTES`, a file that is not
UTF-8 text and a file whose name holds a secret or personal data are skipped and named.
`restore` writes the newest backed-up copies back, never outside those two folders and
never over a local file that differs unless forced."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock

from agentic.orgapi import PersonalFile
from agentic.security.publish_scan import PublishGuard, PublishScanEvent
from agentic.skills.catalog import skill_files
from agentic.skills.personal import BACKUP_ROOTS, backup_path_problem, is_junk
from agentic.telemetry.sinks import SinkError, forward
from agentic_cli.control_plane import (
    ControlPlaneClient,
    ControlPlaneError,
    PersonalSink,
    login_record,
)
from sdlc_commons.ledger import entry_hash

SYNC = Path("sync") / "personal"
JOURNAL, STATE, DEVICE, LOCK, CURSORS = (
    "journal.jsonl",
    "state.json",
    "device.json",
    "journal.lock",
    "cursors",
)
#: The largest file backed up (the control plane refuses more).
MAX_CONTENT_BYTES = 256 * 1024


@dataclass
class Recorded:
    saved: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, why)


@dataclass
class Delivered:
    sent: int = 0
    queued: int = 0
    error: str | None = None


@dataclass
class Restored:
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # a different local file, not forced
    refused: list[tuple[str, str]] = field(default_factory=list)  # (path, why)


def _folder(home: Path) -> Path:
    folder = home / SYNC
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _lock(home: Path) -> FileLock:
    return FileLock(_folder(home) / LOCK)


def device_id(home: Path) -> str:
    """This laptop's device id, made once (32 hex characters)."""
    path = _folder(home) / DEVICE
    try:
        found = json.loads(path.read_text(encoding="utf-8")).get("device", "")
        return uuid.UUID(found).hex
    except (OSError, ValueError, AttributeError):
        made = uuid.uuid4().hex
        _write_json(path, {"device": made})
        return made


# ── recording changes ────────────────────────────────────────────────────────


def record_changes(
    home: Path,
    guard: PublishGuard,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Recorded:
    """Journal every personal file that changed, appeared or went since the last time."""
    recorded = Recorded()
    with _lock(home):
        folder = _folder(home)
        state = _read_json(folder / STATE)
        found = _personal_files(home, recorded)
        for path, (kind, data) in sorted(found.items()):
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            if state.get(path) == digest:
                continue
            text = data.decode("utf-8")
            if guard.findings({"path": path}):
                recorded.skipped.append((path, "its name holds a secret or personal data"))
                continue
            findings = guard.findings({"content": text})
            if findings:
                event = {"event": "personal.held", "findings": findings}
                guard.on_event(
                    PublishScanEvent("blocked", f"personal:{kind}", findings, ("content",))
                )
                recorded.held.append(path)
            else:
                event = {"event": "personal.saved", "content": text}
                recorded.saved.append(path)
            _append(folder, {**event, "kind": kind, "path": path, "sha256": digest}, clock)
            state[path] = digest
        for path in sorted(set(state) - set(found)):
            kind = "skill" if path.startswith(f"{BACKUP_ROOTS['skill']}/") else "memory"
            empty = "sha256:" + hashlib.sha256(b"").hexdigest()
            event = {"event": "personal.removed", "kind": kind, "path": path, "sha256": empty}
            _append(folder, event, clock)
            recorded.removed.append(path)
            del state[path]
        _write_json(folder / STATE, state)
    return recorded


def _personal_files(home: Path, recorded: Recorded) -> dict[str, tuple[str, bytes]]:
    """path -> (kind, bytes) for every file that can be backed up; the rest is named in
    `recorded.skipped` (junk the OS or an editor leaves is skipped silently)."""
    found: dict[str, tuple[str, bytes]] = {}
    for kind, root in BACKUP_ROOTS.items():
        base = home / root
        if not base.is_dir() or base.is_symlink() or base.is_junction():
            continue
        files, links = skill_files(base)
        for link in links:
            recorded.skipped.append((_relative(home, link), "a link: only plain files"))
        for file in files:
            path = _relative(home, file)
            if is_junk(path):
                continue
            if why := backup_path_problem(kind, path):
                recorded.skipped.append((path, why))
                continue
            data = file.read_bytes()
            if len(data) > MAX_CONTENT_BYTES:
                recorded.skipped.append((path, f"over {MAX_CONTENT_BYTES} bytes"))
                continue
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                recorded.skipped.append((path, "not UTF-8 text"))
                continue
            if b"\x00" in data:
                recorded.skipped.append((path, "holds a NUL character"))
                continue
            found[path] = (kind, data)
    return found


def _relative(home: Path, path: Path) -> str:
    return path.relative_to(home).as_posix()


# ── the journal ──────────────────────────────────────────────────────────────


def journal_entries(home: Path) -> list[dict]:
    """The journal's entries in order (a line torn by a crash at the end is left out)."""
    path = home / SYNC / JOURNAL
    try:
        lines = path.read_bytes().split(b"\n")
    except FileNotFoundError:
        return []
    entries = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            break  # a torn last line: everything before it is intact
    return entries


def _append(folder: Path, event: dict, clock: Callable[[], datetime]) -> dict:
    entries = journal_entries(folder.parents[1])
    _drop_torn_tail(folder / JOURNAL, len(entries))
    last = entries[-1] if entries else {"seq": 0, "hash": ""}
    entry = {
        "seq": int(last["seq"]) + 1,
        "prev": str(last["hash"]),
        "at": clock().isoformat(timespec="seconds"),
        **event,
    }
    entry["hash"] = entry_hash(entry)
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    with (folder / JOURNAL).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return entry


def _drop_torn_tail(path: Path, intact: int) -> None:
    """Rewrite the journal without a line a crash left half-written, so the next append
    starts on a fresh line."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return
    lines = [line for line in data.split(b"\n") if line.strip()]
    if len(lines) == intact and (not data or data.endswith(b"\n")):
        return
    kept = b"".join(line + b"\n" for line in lines[:intact])
    staged = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    staged.write_bytes(kept)
    os.replace(staged, path)


# ── delivering ───────────────────────────────────────────────────────────────


def default_scope(home: Path, client: ControlPlaneClient) -> str:
    """Which control plane and user the journal goes to (one cursor each)."""
    record = login_record(home) or {}
    return f"{client.base_url}|{record.get('tenant', '')}|{record.get('user', '')}"


def deliver(home: Path, client: ControlPlaneClient, scope: str | None = None) -> Delivered:
    """Send the journal entries the control plane has not taken yet, in order."""
    scope = scope or default_scope(home, client)
    name = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24]
    with _lock(home):
        entries = journal_entries(home)
        if not entries:
            return Delivered()
        cursor = _folder(home) / CURSORS / f"{name}.json"
        device = device_id(home)
        try:
            sent, error = forward(device, entries, PersonalSink(client), cursor), None
        except (ControlPlaneError, SinkError) as failure:  # the journal stays queued
            sent, error = 0, str(failure)
        taken = _cursor_seq(cursor)
    return Delivered(sent=sent, queued=max(len(entries) - taken, 0), error=error)


def _cursor_seq(cursor: Path) -> int:
    try:
        return int(json.loads(cursor.read_text(encoding="utf-8")).get("seq", 0))
    except (OSError, ValueError, AttributeError):
        return 0


# ── restoring ────────────────────────────────────────────────────────────────


def restore(home: Path, files: Sequence[PersonalFile], *, force: bool = False) -> Restored:
    """Write the backed-up copies of `files` under `home` (`~/.agentic`). A removed file
    is not brought back; a local file that differs is kept unless `force`."""
    done = Restored()
    with _lock(home):
        state_path = _folder(home) / STATE
        state = _read_json(state_path)
        for item in sorted(files, key=lambda f: f.path):
            if item.deleted or item.content is None:
                continue
            if why := backup_path_problem(item.kind, item.path):
                done.refused.append((item.path, why))
                continue
            data = item.content.encode("utf-8")
            if "sha256:" + hashlib.sha256(data).hexdigest() != item.sha256:
                done.refused.append((item.path, "the copy does not match its sha256"))
                continue
            target = home.joinpath(*item.path.split("/"))
            if why := _linked(home, target):
                done.refused.append((item.path, why))
                continue
            if target.is_file():
                if target.read_bytes() == data:
                    done.unchanged.append(item.path)
                    state[item.path] = item.sha256
                    continue
                if not force:
                    done.kept.append(item.path)
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            staged = target.with_name(f".{target.name}.restore-{os.getpid()}")
            staged.write_bytes(data)
            os.replace(staged, target)
            done.written.append(item.path)
            state[item.path] = item.sha256  # already backed up: not journaled again
        _write_json(state_path, state)
    return done


def _linked(home: Path, target: Path) -> str | None:
    """Why writing `target` could leave `home`: a link or junction on the way, or a target
    that resolves outside its folder."""
    current = home
    for part in target.relative_to(home).parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            return "a link on the way: a restore writes plain files only"
    root = (home / target.relative_to(home).parts[0]).resolve()
    if not target.resolve().is_relative_to(root):
        return "it resolves outside the skills and notes folders"
    return None


# ── small files ──────────────────────────────────────────────────────────────


def _read_json(path: Path) -> dict:
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with staged.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(staged, path)
