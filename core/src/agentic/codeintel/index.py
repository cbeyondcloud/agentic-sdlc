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
"""

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from agentic.codeintel.interface import CodeIntelError

DB_NAME = "graph.db"
STATE_NAME = "indexed-state.json"
SCRATCH_INDEX = "git-index"
#: Never let repository config run a command for git (a filesystem monitor).
NO_FSMONITOR = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.fsmonitor",
    "GIT_CONFIG_VALUE_0": "false",
}


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
    ) -> None:
        self.tree, self.data_dir, self.home_dir = Path(tree), Path(data_dir), Path(home_dir)
        self._cli = list(cli) if cli is not None else None

    @property
    def env(self) -> dict[str, str]:
        return {
            **os.environ,
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
        if self.fresh():
            return
        state, dirty = self._state(), self._dirty()
        self._announce_new_files()
        reverted = state is not None and state.get("dirty") and not dirty
        self._run("build" if not self.db.is_file() or reverted else "update")
        self._save_state(dirty)

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
            done = subprocess.run(
                argv,
                env=self.env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1800,
            )
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

    def _git(
        self, *args: str, strip: bool = True, stdin: str | None = None, index: Path | None = None
    ) -> str:
        env = {**os.environ, **NO_FSMONITOR}
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
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
