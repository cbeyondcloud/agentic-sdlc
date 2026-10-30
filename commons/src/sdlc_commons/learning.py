"""Personal learnings applied and undone through Zyroflo's learning log (M3-16; D34: reuse,
never rewrite).

Zyroflo's `learning.py` keeps an append-only audit of learned changes
(`.sdlc/metrics/learning_log.jsonl` under its target) and reverses one with `undo`: the
applied row holds the file's prior text, `undo` writes it back in the checkout's own line
endings, records whether the file drifted since, and appends the reversal - it never
deletes the application. The framework applies a person's approved personal learning (a
skill or a note under `~/.agentic`) into that same log, with the same reader and the same
undo:

  * `apply` writes an applied row in Zyroflo's format (its `digest` over the LF view, the
    prior text in `before`) BEFORE the file, as Zyroflo's own `apply` does, so the audit
    never lags the file and an undo is never blind to a change that landed;
  * `revert` is Zyroflo's `undo` for the learning and every part of it (`<id>#eval`, its
    regression case), newest first; a file the learning created is then removed, with the
    folders it created, instead of being left empty. When the file changed since (a later
    learning on the same file, a hand edit), only the learning's own block is taken out
    (the caller's `strip`) and the reversal row says `drifted`: writing the whole prior
    file back would lose the later change, or bring back a learning already reverted.

What this adapter adds over Zyroflo's `apply` (gaps for a Zyroflo PR, deferred): a generic
text change (Zyroflo renders only a `review_suppress` YAML list and requires its corpus and
G3 gates, which a personal note has no use for), a path check that the file stays inside
the target, and removing a created file on undo. Both run under one `FileLock`, so two CLI
processes never interleave an apply and an undo (Windows and macOS alike).
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from filelock import FileLock
from zyroflo.sdlc import learning as zyroflo_learning

#: Part ids of one learning: `<proposal id>` (the change) and `<proposal id>#<part>`.
PART = "#"


class LearningApplyError(Exception):
    """The learning cannot be applied or reverted as asked."""


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n")


class PersonalChanges:
    """Learned changes to one person's `~/.agentic` (or any target folder)."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home)

    @property
    def log(self) -> Path:
        return self.home / zyroflo_learning.LOG

    def history(self) -> list[dict]:
        try:
            return zyroflo_learning.history(self.home)
        except zyroflo_learning.LearningError as error:
            raise LearningApplyError(str(error)) from None

    def standing(self, part: str) -> dict | None:
        """The applied row of `part` that has not been undone, or None."""
        return zyroflo_learning.standing(self.history(), part)

    def _path(self, rel: str) -> Path:
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts or "\\" in rel:
            raise LearningApplyError(f"{rel!r} must be a relative path inside {self.home}")
        return self.home.joinpath(*pure.parts)

    def apply(self, part: str, rel: str, after: str, *, by: str, why: str) -> dict:
        """Record, then write, `after` as the content of `rel` under the home. The same
        change already standing (and still in the file) is not applied twice: its row is
        returned, so an apply that failed half way is finished by running it again."""
        path = self._path(rel)
        with self._lock():
            if held := self.standing(part):
                same = held.get("after_sha") == zyroflo_learning.digest(_lf(after))
                if same and path.is_file() and not self._drifted(held):
                    return held
                raise LearningApplyError(
                    f"{part} is already applied: revert it before applying it again"
                )
            created = not path.exists()
            before = path.read_bytes().decode("utf-8") if not created else ""
            folders = []
            folder = path.parent
            while folder != self.home and not folder.exists():
                folders.append(PurePosixPath(folder.relative_to(self.home).as_posix()))
                folder = folder.parent
            row = {
                "schema_version": zyroflo_learning.SCHEMA_VERSION,
                "action": "applied",
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                "by": by,
                "run": "",
                "proposal": part,
                "finding": part.split(PART, 1)[0],
                "home": "repo",
                "path": PurePosixPath(rel).as_posix(),
                "why": why,
                "evidence": {},
                "before_sha": zyroflo_learning.digest(_lf(before)),
                "after_sha": zyroflo_learning.digest(_lf(after)),
                "before": before,
                "created": created,
                "created_folders": [f.as_posix() for f in folders],
            }
            zyroflo_learning.append_log(self.home, row)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(after.encode("utf-8"))
        return row

    def revert(
        self,
        proposal_id: str,
        *,
        by: str,
        strip: Callable[[str, str], str] | None = None,
    ) -> list[dict]:
        """Undo every standing part of a learning (newest first); the reversal rows.

        `strip(text, proposal_id)` takes the learning's own block out of a text. When the
        file changed since the learning was applied (a later learning on the same file, a
        hand edit), only that block is taken out (review F2): Zyroflo's undo would write the
        whole prior file back, losing the later change - or bringing back a learning that
        was already reverted. The reversal row then says `drifted`."""
        with self._lock():
            rows = self.history()
            parts = list(
                dict.fromkeys(
                    r["proposal"]
                    for r in reversed(rows)
                    if r.get("action") == "applied"
                    and str(r.get("proposal", "")).split(PART, 1)[0] == proposal_id
                )
            )
            standing = [(p, zyroflo_learning.standing(rows, p)) for p in parts]
            standing = [(p, row) for p, row in standing if row is not None]
            if not standing:
                raise LearningApplyError(
                    f"{proposal_id} is not applied, so there is nothing to revert"
                )
            undone = []
            for part, applied in standing:
                if strip is not None and PART not in part and self._drifted(applied):
                    undone.append(self._take_out(part, applied, by, strip))
                else:
                    undone.append(zyroflo_learning.undo(self.home, part, by))
                if applied.get("created"):
                    self._remove_created(applied)
        return undone

    def _drifted(self, applied: dict) -> bool:
        path = self._path(applied["path"])
        if not path.is_file():
            return False  # gone: Zyroflo's undo writes the prior text back, said drifted
        try:
            found = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return False  # not text any more: Zyroflo's undo restores it, said drifted
        return zyroflo_learning.digest(_lf(found)) != applied.get("after_sha")

    def _take_out(
        self, part: str, applied: dict, by: str, strip: Callable[[str, str], str]
    ) -> dict:
        """Remove only `part`'s block from a file that changed since; the reversal row in
        Zyroflo's format, file first and row second, as Zyroflo's `undo` does."""
        path = self._path(applied["path"])
        found = path.read_bytes().decode("utf-8")
        restored = strip(found, part)
        if restored != found:
            path.write_bytes(restored.encode("utf-8"))
        row = {
            "schema_version": zyroflo_learning.SCHEMA_VERSION,
            "action": "undone",
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "by": by,
            "proposal": part,
            "run": applied.get("run", ""),
            "finding": applied.get("finding", ""),
            "path": applied["path"],
            "restored_sha": zyroflo_learning.digest(_lf(restored)),
            "drifted": True,
            "found_sha": zyroflo_learning.digest(_lf(found)),
            "only_block": True,
        }
        zyroflo_learning.append_log(self.home, row)
        return row

    def _remove_created(self, applied: dict) -> None:
        path = self._path(applied["path"])
        if path.is_file() and not path.read_bytes():
            path.unlink()
        for folder in applied.get("created_folders") or []:
            where = self._path(folder)
            if where.is_dir() and not any(where.iterdir()):
                where.rmdir()

    def _lock(self) -> FileLock:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(f"{self.log}.lock")


__all__ = ["PART", "LearningApplyError", "PersonalChanges"]
