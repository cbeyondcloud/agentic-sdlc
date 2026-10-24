"""The local code-intelligence index (M1-36, D39; spike S9).

code-review-graph builds a graph of the run's tree into run state - never into the
tree (its `--data-dir`) and never into the user's home (`CRG_HOME`). Before every
answer the index is checked against the tree, and caught up:

  * no index        -> build;
  * the tree is exactly as last indexed (same commit, same uncommitted state) -> nothing;
  * the last index was taken from uncommitted edits and the tree is now clean (a fresh
    attempt, a revert) -> build, because an incremental update cannot remove a file
    that was reverted or restore one that was deleted and brought back (SP9 review I1);
  * otherwise       -> incremental update (about a second).

code-review-graph finds changes through git, which does not see untracked files, so
an agent's new files are marked intent-to-add - in a scratch copy of git's index, never
the tree's own (SP9 review I2): the step checkpoints see the tree exactly as the agent
left it. Every git call runs with fsmonitor off (SP9 review I5).

code-review-graph parses files an agent wrote, so it and git run with the agent
command environment (`safe_env`: no secrets, DF-88). An index that cannot be read (a
crashed first build, a corrupt file) is thrown away and built again (DF-89). One lock
per data folder makes parallel tool calls build once (DF-92) - within this process, and
through a lock file across processes sharing the folder (DF-338).

code-review-graph runs as a contained process tree (`run_contained`): a build that
outlives its step - the run's time limit, a stopped run - is killed with the step's
process scope instead of running on for up to BUILD_TIMEOUT_S (DF-328).
"""

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from filelock import FileLock

from agentic.backends.processes import run_contained
from agentic.codeintel.export import MANIFEST, import_export
from agentic.codeintel.interface import CodeIntelError
from agentic.sandbox.process import safe_env

DB_NAME = "graph.db"
STATE_NAME = "indexed-state.json"
IMPORT_NAME = "graph.db.import"  # an import in progress; renamed over DB_NAME when complete
SCRATCH_INDEX = "git-index"
LOCK_NAME = "build.lock"
BUILD_TIMEOUT_S = 1800
#: Never let repository config run a command for git (a filesystem monitor).
NO_FSMONITOR = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.fsmonitor",
    "GIT_CONFIG_VALUE_0": "false",
}
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(data_dir: Path) -> threading.Lock:
    """One lock per data folder, however its path is spelled."""
    key = os.path.normcase(os.path.abspath(data_dir))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def available() -> bool:
    """Whether the `codeintel` extra (code-review-graph) is installed."""
    return importlib.util.find_spec("code_review_graph") is not None


def _default_cli() -> list[str]:
    name = "code-review-graph.exe" if os.name == "nt" else "code-review-graph"
    beside = Path(sys.executable).parent / name
    found = str(beside) if beside.is_file() else shutil.which("code-review-graph")
    if found is None:
        raise CodeIntelError("code-review-graph is not installed (agentic-core[codeintel])")
    return [found]


class LocalIndex:
    def __init__(
        self,
        tree: Path,
        data_dir: Path,
        *,
        home_dir: Path,
        cli: Sequence[str] | None = None,
        seed: Path | None = None,
    ) -> None:
        self.tree, self.data_dir, self.home_dir = Path(tree), Path(data_dir), Path(home_dir)
        self._cli = list(cli) if cli is not None else None
        self._lock = _lock_for(self.data_dir)
        # a published index export (M2-44) the first build starts from, when there is one
        self.seed = Path(seed) if seed is not None else None
        self.seed_note: str | None = None  # why the seed was not used

    @property
    def env(self) -> dict[str, str]:
        return {
            **safe_env(),
            "CRG_DATA_DIR": str(self.data_dir),
            "CRG_HOME": str(self.home_dir),
            "GIT_INDEX_FILE": str(self.data_dir / SCRATCH_INDEX),
            **NO_FSMONITOR,
        }

    @property
    def db(self) -> Path:
        return self.data_dir / DB_NAME

    def recorded_commit(self) -> str | None:
        """The commit the index was built or last updated at, or None when there is none."""
        if not self.db.is_file():
            return None
        try:
            status = json.loads(self._run("status", "--json") or "{}")
        except ValueError as error:
            raise CodeIntelError(f"code-review-graph status is unreadable: {error}") from error
        return status.get("built_at_commit") or None

    def fresh(self) -> bool:
        """Whether the index reflects the tree exactly as it is now."""
        if not self.db.is_file():
            return False
        state = self._state()
        if state is None:  # indexed before states were recorded: commit and a clean tree
            return self.recorded_commit() == self._head() and not self._dirty()
        now = {"commit": self._head(), "fingerprint": self._fingerprint()}
        return {"commit": state.get("commit"), "fingerprint": state.get("fingerprint")} == now

    def ensure(self) -> None:
        """Build, update or keep the index so it reflects the tree as it is now."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._lock, FileLock(self.data_dir / LOCK_NAME):
            if self.db.is_file() and not self._readable():
                self._discard()  # a crashed first build or a corrupt file (DF-89)
            if not self.db.is_file() and self.seed and (self.seed / MANIFEST).is_file():
                try:  # seconds instead of a full build; `update` catches it up below
                    self._load(self.seed)
                except CodeIntelError as error:
                    self.seed_note = f"the published index export was not used: {error}"
            if self.fresh():
                return
            state, dirty = self._state(), self._dirty()
            self._announce_new_files()
            reverted = state is not None and state.get("dirty") and not dirty
            self._run("build" if not self.db.is_file() or reverted else "update")
            self._save_state(dirty)

    def load_export(self, folder: Path) -> dict:
        """Replace the index with the published export in `folder` (M2-44): checked and
        pinned to this tool's version; the old index stays unless the import completes.
        Returns the export's manifest. Raises ExportError (ExportMismatch)."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._lock, FileLock(self.data_dir / LOCK_NAME):
            return self._load(folder)

    def _load(self, folder: Path) -> dict:
        staged = self.data_dir / IMPORT_NAME
        self.home_dir.mkdir(parents=True, exist_ok=True)
        manifest = import_export(folder, self.tree, staged, self.env, timeout=BUILD_TIMEOUT_S)
        try:  # the old index's journal first: it must never be replayed into the new file
            for name in (f"{DB_NAME}-wal", f"{DB_NAME}-shm"):
                (self.data_dir / name).unlink(missing_ok=True)
            os.replace(staged, self.db)
        except OSError as error:  # Windows: another process has the old index open
            staged.unlink(missing_ok=True)
            raise CodeIntelError(f"the index could not be replaced: {error}") from error
        (self.data_dir / STATE_NAME).unlink(missing_ok=True)  # checked afresh against HEAD
        return manifest

    def indexed_files(self) -> set[str]:
        """The tree-relative files the index holds."""
        if not self.db.is_file():
            return set()
        try:
            with sqlite3.connect(f"{self.db.resolve().as_uri()}?mode=ro", uri=True) as con:
                rows = con.execute("select distinct file_path from nodes").fetchall()
        except sqlite3.Error as error:
            raise CodeIntelError(f"the code index cannot be read: {error}") from error
        root = self.tree.resolve()
        found = set()
        for (path,) in rows:
            try:
                found.add(Path(path).resolve().relative_to(root).as_posix())
            except (ValueError, OSError):
                continue
        return found

    # ── state ───────────────────────────────────────────────────────────────

    def _readable(self) -> bool:
        """Whether the index file is a database holding the graph's nodes. A locked or
        busy database is another process using a valid index: readable, never thrown
        away (a full rebuild; on Windows the open file cannot even be removed, DF-337)."""
        try:  # closed at once: Windows cannot remove a file that is still open
            with closing(sqlite3.connect(f"{self.db.resolve().as_uri()}?mode=ro", uri=True)) as c:
                c.execute("select 1 from nodes limit 1").fetchall()
        except sqlite3.Error as error:
            said = str(error).lower()
            return "no such table" not in said and ("locked" in said or "busy" in said)
        return True

    def _discard(self) -> None:
        for name in (DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm", STATE_NAME):
            (self.data_dir / name).unlink(missing_ok=True)

    def _state(self) -> dict | None:
        try:
            return json.loads((self.data_dir / STATE_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save_state(self, dirty: bool) -> None:
        state = {"commit": self._head(), "fingerprint": self._fingerprint(), "dirty": dirty}
        (self.data_dir / STATE_NAME).write_text(json.dumps(state), encoding="utf-8")

    def _fingerprint(self) -> str:
        """The tree's uncommitted state: what changed, what it contains, what is new."""
        digest = hashlib.sha256()
        status = self._git("status", "--porcelain=v1", "-z", "--untracked-files=all", strip=False)
        digest.update(status.encode())
        digest.update(self._git("diff", "HEAD", "--binary", strip=False).encode())
        for name in self._untracked():
            try:
                info = (self.tree / name).stat()
            except OSError:
                continue
            digest.update(f"{name}\0{info.st_size}\0{info.st_mtime_ns}\0".encode())
        return digest.hexdigest()

    # ── git and code-review-graph ───────────────────────────────────────────

    def _untracked(self) -> list[str]:
        listing = self._git("ls-files", "--others", "--exclude-standard", "-z", strip=False)
        # an untracked folder holding its own repository is listed as `name/`: never add it
        return [name for name in listing.split("\0") if name and not name.endswith("/")]

    def _announce_new_files(self) -> None:
        """Copy git's index to a scratch file and mark the tree's new files intent-to-add
        there, through stdin (any number of files)."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        scratch = self.data_dir / SCRATCH_INDEX
        real = Path(self._git("rev-parse", "--git-path", "index"))
        real = real if real.is_absolute() else self.tree / real
        if real.is_file():
            shutil.copyfile(real, scratch)
        else:
            scratch.unlink(missing_ok=True)
        new = self._untracked()
        if new:
            self._git(
                "--literal-pathspecs",
                "add",
                "-N",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
                stdin="\0".join(new),
                index=scratch,
            )

    def _run(self, command: str, *args: str) -> str:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.home_dir.mkdir(parents=True, exist_ok=True)
        cli = self._cli if self._cli is not None else _default_cli()
        argv = [*cli, command, "--repo", str(self.tree), "--data-dir", str(self.data_dir), *args]
        if command in ("build", "update"):
            argv.append("-q")
        try:
            done = run_contained(argv, env=self.env, timeout=BUILD_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CodeIntelError(f"code-review-graph {command} could not run: {error}") from error
        if done.returncode != 0:
            lines = (done.stderr or done.stdout).strip().splitlines()
            why = lines[-1] if lines else f"exit {done.returncode}"
            raise CodeIntelError(f"code-review-graph {command} failed: {why}")
        return done.stdout

    def _head(self) -> str:
        return self._git("rev-parse", "HEAD")

    def _dirty(self) -> bool:
        return bool(self._git("status", "--porcelain"))

    def _git_env(self, index: Path | None = None) -> dict[str, str]:
        env = {**safe_env(), **NO_FSMONITOR}
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        return env

    def _git(
        self, *args: str, strip: bool = True, stdin: str | None = None, index: Path | None = None
    ) -> str:
        env = self._git_env(index)
        try:
            done = subprocess.run(
                ["git", "-C", str(self.tree), "-c", "core.fsmonitor=false", *args],
                input=stdin,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as error:
            raise CodeIntelError(f"git could not run: {error}") from error
        if done.returncode != 0:
            lines = done.stderr.strip().splitlines()
            why = lines[-1] if lines else f"exit {done.returncode}"
            raise CodeIntelError(f"git {args[0]} failed in {self.tree}: {why}")
        return done.stdout.strip() if strip else done.stdout
