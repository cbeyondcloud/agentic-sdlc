"""The local code-intelligence index (M1-36, D39; spike S9).

code-review-graph builds a graph of the run's tree into run state - never into the
tree (its `--data-dir`) and never into the user's home (`CRG_HOME`). Before every
answer the index is checked: built at the tree's HEAD with no uncommitted changes
means fresh; otherwise an incremental `update` (about a second) catches it up, and a
missing index is built (seconds). Nothing is committed anywhere.
"""

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


def available() -> bool:
    """Whether the `codeintel` extra (code-review-graph) is installed."""
    return importlib.util.find_spec("code_review_graph") is not None


def _default_cli() -> list[str]:
    beside = Path(sys.executable).parent / (
        "code-review-graph.exe" if os.name == "nt" else "code-review-graph"
    )
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
        return {**os.environ, "CRG_DATA_DIR": str(self.data_dir), "CRG_HOME": str(self.home_dir)}

    @property
    def db(self) -> Path:
        return self.data_dir / DB_NAME

    def recorded_commit(self) -> str | None:
        """The commit the index was built or last updated at, or None when there is none."""
        if not self.db.is_file():
            return None
        status = json.loads(self._run("status", "--json") or "{}")
        return status.get("built_at_commit") or None

    def fresh(self) -> bool:
        recorded = self.recorded_commit()
        return recorded is not None and recorded == self._head() and not self._dirty()

    def ensure(self) -> None:
        """Build the index if there is none, update it if stale; nothing if fresh."""
        if not self.db.is_file():
            self._announce_new_files()
            self._run("build")
        elif not self.fresh():
            self._announce_new_files()
            self._run("update")

    def _announce_new_files(self) -> None:
        """Mark untracked files intent-to-add (`git add -N`): code-review-graph finds
        changes through git, which does not see untracked files - an agent's new file
        would be missing from every answer. Only the path enters git's index, not the
        content; the step's checkpoint adds them properly, and a fresh attempt's hard
        reset discards them with the rest of the attempt (M1-30)."""
        listing = self._git("ls-files", "--others", "--exclude-standard", "-z", strip=False)
        new = [name for name in listing.split("\0") if name]
        if new:
            self._git("add", "-N", "--", *new)

    def indexed_files(self) -> set[str]:
        """The tree-relative files the index holds."""
        if not self.db.is_file():
            return set()
        with sqlite3.connect(f"file:{self.db.as_posix()}?mode=ro", uri=True) as con:
            rows = con.execute("select distinct file_path from nodes").fetchall()
        root = self.tree.resolve()
        found = set()
        for (path,) in rows:
            try:
                found.add(Path(path).resolve().relative_to(root).as_posix())
            except (ValueError, OSError):
                continue
        return found

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

    def _git(self, *args: str, strip: bool = True) -> str:
        done = subprocess.run(
            ["git", "-C", str(self.tree), *args], capture_output=True, text=True, check=False
        )
        return done.stdout.strip() if strip else done.stdout
