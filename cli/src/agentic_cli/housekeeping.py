"""A laptop's own run data (M3-08): what `agentic gc` prunes, and the disk cap that
`agentic status` warns about.

Each run leaves a folder under `.agentic/local/runs/<run>` (state, ledger, handoffs, logs)
and, for code-editing steps, linked git worktrees under the trees folder
(`sdlc_commons.worktrees.worktrees_base`, `.sdlc-worktrees` by default). Nothing deletes
them on its own (spec §14.6, OR-01); `agentic gc` does, when a person runs it:

  * only a **finished** run goes (completed, failed, stopped, killed: the engine's
    `TERMINAL`), and only once it is older than the cut-off - `--older-than DAYS`, else
    the user config's `gc.older_than_days`, else the org policy's `local.gc_after_days`,
    else 14. A running, paused or gate-waiting run is never touched, nor a `run.json`
    that cannot be read;
  * in org mode a run whose ledger holds events the control plane has not taken
    (`agentic.telemetry.sinks.queued`) is kept, with `agentic sync` as the remedy - a
    run the publish scan held back included. With no control plane configured, every
    run is kept: its ledger is the only copy of its audit record;
  * trees go first, through Zyroflo (`RunWorktrees.remove`, never forced): uncommitted
    work, or a tree a process serves from, keeps the whole run, so its record still
    says where its trees are. Branches are kept (a pull request may name them);
  * then the run folder is renamed aside (`.gc-<run>-<pid>`) and deleted. On Windows a
    rename is refused while any file in the folder is open: the run is then kept, whole,
    and the reason says so. Read-only files (git's pack files) are made writable first;
    whatever a failed delete leaves in a `.gc-*` folder, the next gc removes.

The disk cap is the user config's `gc.disk_cap_gb`, else the policy's `local.disk_cap_gb`,
else 20 GB, over `.agentic/local` and the trees folder together.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agentic.engine.engine import TERMINAL
from agentic.engine.state import RunState, RunStateError, RunStore
from agentic.policy.model import LocalDataPolicy
from agentic.telemetry.sinks import queued
from agentic_cli import control_plane
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.workspace import Workspace
from sdlc_commons.worktrees import RunWorktrees, WorktreeError, worktree_git, worktrees_base

DEFAULT = LocalDataPolicy()
GB = 1024**3
TRASH_PREFIX = ".gc-"
LOCAL = Path(".agentic") / "local"
SINK = control_plane.ControlPlaneSink.name


def utc_now() -> datetime:
    return datetime.now(UTC)


# ── settings ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GcSettings:
    older_than_days: int
    older_than_source: str  # "--older-than", "user config", "policy" or "default"
    disk_cap_bytes: int


def gc_settings(ws: Workspace, older_than: int | None = None) -> GcSettings:
    """The cut-off and the cap: the flag, else the user config's `gc:`, else the org
    policy's `local:`, else the built-in defaults."""
    user = _user_gc(ws)
    policy = _policy_local(ws)
    if older_than is not None:
        days, source = older_than, "--older-than"
    elif "older_than_days" in user:
        days, source = user["older_than_days"], "user config"
    elif policy is not None:
        days, source = policy.gc_after_days, "policy"
    else:
        days, source = DEFAULT.gc_after_days, "default"
    cap = user.get("disk_cap_gb", policy.disk_cap_gb if policy else DEFAULT.disk_cap_gb)
    return GcSettings(days, source, int(cap * GB))


def _user_gc(ws: Workspace) -> dict[str, Any]:
    raw = ws.settings.get("gc")
    if raw is None:
        return {}
    hint = (
        f"fix gc in {ws.home / 'config.yaml'}: older_than_days (a whole number, 1 or more) "
        "and disk_cap_gb (a number above 0)"
    )
    if not isinstance(raw, dict) or set(raw) - {"older_than_days", "disk_cap_gb"}:
        raise CliError("gc in the user config has unknown settings", ExitCode.INVALID, hint=hint)
    days = raw.get("older_than_days", 1)
    cap = raw.get("disk_cap_gb", 1)
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise CliError(
            f"gc.older_than_days is not a number of days: {days!r}", ExitCode.INVALID, hint=hint
        )
    if not isinstance(cap, int | float) or isinstance(cap, bool) or not 0 < cap < float("inf"):
        raise CliError(f"gc.disk_cap_gb is not a size in GB: {cap!r}", ExitCode.INVALID, hint=hint)
    return dict(raw)


def _policy_local(ws: Workspace) -> LocalDataPolicy | None:
    """The org policy's `local:` settings, or None when no policy can be read here
    (housekeeping must work in a repository that was never set up)."""
    try:
        return ws.policy().local_data
    except CliError:
        return None


# ── what goes ─────────────────────────────────────────────────────────────────


@dataclass
class Candidate:
    """One run old enough to be considered: it goes unless `keep` says why not."""

    run_id: str
    status: str
    updated_at: datetime | None
    folder: Path
    trees: Path | None  # the folder of its trees, when it has one on disk
    bytes: int = 0
    keep: str | None = None

    def as_dict(self) -> dict:
        data = {
            "run_id": self.run_id,
            "status": self.status,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "bytes": self.bytes,
        }
        if self.keep is not None:
            data["reason"] = self.keep
        return data


def plan_gc(ws: Workspace, settings: GcSettings, now: datetime | None = None) -> list[Candidate]:
    """Every run old enough to be considered (and every unreadable one), each with the
    reason it stays, if any. Reads only; nothing is changed."""
    root = ws.runs_root
    if not root.is_dir():
        return []
    cutoff = (now or utc_now()) - timedelta(days=settings.older_than_days)
    store = RunStore(root)
    found = []
    for folder in sorted(root.iterdir(), key=lambda p: p.name):
        if not folder.is_dir() or folder.name.startswith(".") or folder.is_symlink():
            continue  # our own trash, or not a run
        if not (folder / "run.json").is_file():
            continue  # not a run of this framework (a Zyroflo archive, a stray folder)
        try:
            state = store.load(folder.name)
        except RunStateError as error:
            found.append(
                Candidate(folder.name, "unknown", None, folder, None, keep=f"kept: {error}")
            )
            continue
        if state.updated_at > cutoff:
            continue  # young enough: not mentioned
        found.append(_consider(ws, state, folder))
    return found


def _consider(ws: Workspace, state: RunState, folder: Path) -> Candidate:
    candidate = Candidate(state.run_id, state.status, state.updated_at, folder, None)
    try:
        worktrees = RunWorktrees(ws.repo, state.run_id)
        trees_root = worktrees.root()
        live = worktrees.trees()
    except WorktreeError as error:
        candidate.keep = f"where its trees are cannot be read ({error})"
        candidate.bytes = tree_size(folder)
        return candidate
    candidate.trees = trees_root if trees_root.is_dir() else None
    candidate.bytes = tree_size(folder) + (tree_size(trees_root) if candidate.trees else 0)
    candidate.keep = _why_kept(ws, state, folder, live.values())
    return candidate


def _why_kept(ws: Workspace, state: RunState, folder: Path, trees: Iterable) -> str | None:
    if state.status not in TERMINAL:
        return f"not finished ({state.status})"
    if ws.settings.get("org"):
        if control_plane.control_plane_url(ws.env, ws.settings) is None:
            return (
                "org mode with no control plane configured: its ledger is the only copy "
                "of its audit record"
            )
        left = queued(folder, SINK)
        if left:
            return (
                f"{left} event(s) not yet delivered to the control plane: run `agentic sync` "
                "(a run the publish scan held back stays until your org admin decides)"
            )
    for tree in trees:
        if tree.mode != "worktree" or not tree.path.is_dir():
            continue
        try:
            dirty = worktree_git(tree.path, "status", "--porcelain", check=False)
        except WorktreeError as error:
            return f"its tree {tree.path} cannot be checked ({error})"
        if dirty.strip():
            count = len(dirty.strip().splitlines())
            return (
                f"{count} uncommitted change(s) in its tree {tree.path}: the only copy of "
                "that work (commit or discard it, then run gc again)"
            )
    return None


# ── collecting ────────────────────────────────────────────────────────────────


@dataclass
class Outcome:
    removed: list[Candidate] = field(default_factory=list)
    kept: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def freed(self) -> int:
        return sum(c.bytes for c in self.removed)


def collect(ws: Workspace, candidates: list[Candidate], *, dry_run: bool = False) -> Outcome:
    """Remove each candidate nothing keeps: its trees, then its folder. A run whose trees
    or folder cannot be removed whole is kept, with the reason."""
    outcome = Outcome()
    trees_gone = False
    for candidate in candidates:
        if candidate.keep is not None:
            outcome.kept.append(candidate)
            continue
        if dry_run:
            outcome.removed.append(candidate)
            continue
        why = _remove_trees(ws, candidate)
        trees_gone = trees_gone or candidate.trees is not None
        if why is None:
            why = _remove_folder(ws, candidate, outcome.notes)
        if why is not None:
            candidate.keep = why
            outcome.kept.append(candidate)
        else:
            outcome.removed.append(candidate)
    if not dry_run:
        if trees_gone:  # registrations of trees deleted by hand, too
            try:
                worktree_git(ws.repo, "worktree", "prune", check=False)
            except WorktreeError:
                pass
        outcome.notes += sweep_leftovers(ws.runs_root)
    return outcome


def _remove_trees(ws: Workspace, candidate: Candidate) -> str | None:
    try:
        RunWorktrees(ws.repo, candidate.run_id).remove(force=False)
    except WorktreeError as error:
        return f"its trees could not be removed: {error}"
    if candidate.trees is not None and candidate.trees.is_dir():
        try:
            candidate.trees.rmdir()  # empty once git removed every tree in it
        except OSError:
            pass
    return None


def _remove_folder(ws: Workspace, candidate: Candidate, notes: list[str]) -> str | None:
    trash = candidate.folder.with_name(f"{TRASH_PREFIX}{candidate.run_id}-{os.getpid()}")
    try:
        os.replace(candidate.folder, trash)
    except OSError as error:
        trees = " (its trees, clean, were removed)" if candidate.trees is not None else ""
        return (
            f"in use: its run folder could not be moved aside ({error.strerror or error})"
            f"{trees}; close whatever has a file in it open (an editor, `agentic logs "
            "--follow`), then run gc again"
        )
    if not remove_tree(trash):
        notes.append(f"{trash} is partly left (a file is in use); the next gc removes it")
    return None


def sweep_leftovers(runs_root: Path) -> list[str]:
    """Delete what earlier gcs left behind (`.gc-*` folders); notes what stays."""
    notes = []
    if not runs_root.is_dir():
        return notes
    for leftover in runs_root.glob(f"{TRASH_PREFIX}*"):
        if leftover.is_dir() and not leftover.is_symlink() and not remove_tree(leftover):
            notes.append(f"{leftover} is partly left (a file is in use); the next gc removes it")
    return notes


def remove_tree(path: Path) -> bool:
    """Delete a folder; read-only files (git's pack files on Windows) are made writable
    first. False when something could not be deleted."""
    shutil.rmtree(path, onexc=_writable_and_retry)
    return not path.exists()


def _writable_and_retry(func, path, _error) -> None:
    try:
        os.chmod(path, 0o700)
        func(path)
    except OSError:
        pass  # in use: reported by remove_tree, removed by a later gc


# ── sizes ─────────────────────────────────────────────────────────────────────


def tree_size(path: Path) -> int:
    """Bytes of every file under `path`, links not followed, unreadable entries skipped."""
    return _walk([Path(path)], None, None)[0]


@dataclass(frozen=True)
class Usage:
    bytes: int
    complete: bool  # False: the walk stopped at the cap or the deadline


def local_usage(repo: Path, *, stop_at: int | None, deadline_s: float) -> Usage:
    """The size of this repository's local data: `.agentic/local` and the run trees. The
    walk stops once past `stop_at` bytes or after `deadline_s` seconds."""
    roots = [Path(repo) / LOCAL]
    try:
        base = worktrees_base(repo)
    except WorktreeError:
        base = None
    if base is not None and not _inside(base, roots[0]):
        roots.append(base)
    total, complete = _walk(roots, stop_at, time.monotonic() + deadline_s)
    return Usage(total, complete)


def _inside(path: Path, folder: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(folder).resolve())
        return True
    except ValueError:
        return False


def _walk(roots: list[Path], stop_at: int | None, deadline: float | None) -> tuple[int, bool]:
    total = 0
    pending = [r for r in roots if r.is_dir()]
    while pending:
        folder = pending.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
        if stop_at is not None and total > stop_at:
            return total, False
        if deadline is not None and time.monotonic() > deadline:
            return total, False
    return total, True


def human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover - the loop returns


def cap_warning(ws: Workspace, deadline_s: float = 2.0) -> str | None:
    """The warning `agentic status` shows when local data is over the cap, or None (also
    when the size could not be measured within `deadline_s`)."""
    settings = gc_settings(ws)
    usage = local_usage(ws.repo, stop_at=settings.disk_cap_bytes, deadline_s=deadline_s)
    if usage.bytes <= settings.disk_cap_bytes:
        return None
    size = human_bytes(usage.bytes)
    more = "" if usage.complete else "over "
    return (
        f"this repository's local data (.agentic/local and run trees) is {more}{size}, past "
        f"the {human_bytes(settings.disk_cap_bytes)} cap: `agentic gc --dry-run` shows "
        "which finished runs `agentic gc` would remove"
    )
