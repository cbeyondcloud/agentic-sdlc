"""Installing the selected skills into a repo, read-only and pinned (spec §24).

`sync_skills` is the one call: `agentic init` makes it to install, and the CLI makes
it again before every run to verify. It is idempotent:

  * every selected skill's catalog source must match its pinned sha256, or nothing
    is written (SkillsError);
  * a missing skill is copied in; an installed skill whose files differ in any way
    (edited, added or deleted file) is restored from the catalog and reported as
    `skills.tamper_restored`; an installed skill that is no longer selected - or was
    never in the catalog - is removed (`skills.removed`);
  * installed files are made read-only: shared skills are changed only by their
    owner, through the catalog.

It writes only under `dest` (the repo's git-ignored `.agentic/local/skills/`), and it
never follows a link there: `dest` itself being a symlink or junction is refused, and a
link anywhere below it is removed (the link, never what it points to) and reported as
`skills.link_removed` before anything else is touched - a cloned repo could commit one
to make the cleanup pass delete the user's files. Two runs of one repo may sync at the
same time, so the whole sync holds a lock file in `dest` (`.sync.lock`).
"""

import logging
import os
import shutil
import stat
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from agentic.skills.catalog import Catalog, SkillsError, skill_digest, skill_folder

Record = Callable[[str, dict[str, object]], None]
_log = logging.getLogger("agentic.skills")
LOCK_NAME = ".sync.lock"
LOCK_WAIT_SECONDS = 120.0


def log_record(event: str, fields: dict[str, object]) -> None:
    """The default `record`: a tamper restore is a warning, everything else info."""
    level = logging.WARNING if event == "skills.tamper_restored" else logging.INFO
    _log.log(level, "%s %s", event, fields)


@dataclass
class InstallReport:
    installed: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


def sync_skills(
    catalog: Catalog,
    source_root: Path,
    dest: Path,
    names: Iterable[str],
    *,
    record: Record | None = None,
) -> InstallReport:
    record = record or log_record
    keys = sorted(set(names))
    for key in keys:  # every source is checked before anything is written
        entry = catalog.entry(key)
        source = skill_folder(source_root, key)
        if not source.is_dir():
            raise SkillsError(f"{key}: no folder {source} in the catalog source")
        found = skill_digest(source)
        if found != entry.sha256:
            raise SkillsError(
                f"{key}: the catalog source does not match the catalog "
                f"(pinned {entry.sha256}, found {found}); re-pin or restore it"
            )

    _refuse_link(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with _sync_lock(dest):
        for path in _drop_links(dest):
            record("skills.link_removed", {"path": path})
        return _sync(catalog, source_root, dest, keys, record)


def _sync(
    catalog: Catalog, source_root: Path, dest: Path, keys: list[str], record: Record
) -> InstallReport:
    report = InstallReport()
    for key in keys:
        target = skill_folder(dest, key)
        expected = catalog.skills[key].sha256
        if not target.exists():
            _copy(skill_folder(source_root, key), target, dest)
            report.installed.append(key)
            record("skills.installed", {"skill": key, "sha256": expected})
            continue
        found = skill_digest(target) if target.is_dir() else "not a folder"
        if found == expected:
            _make_read_only(target)
            report.unchanged.append(key)
            continue
        _copy(skill_folder(source_root, key), target, dest)
        report.restored.append(key)
        record("skills.tamper_restored", {"skill": key, "expected": expected, "found": found})

    for key, path in _installed(dest):
        if key not in keys:
            _remove(path, dest)
            report.removed.append(key)
            record("skills.removed", {"skill": key})
    for source_dir in _children(dest):
        if source_dir.is_dir() and not any(source_dir.iterdir()):
            source_dir.rmdir()
    return report


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _refuse_link(dest: Path) -> None:
    if _is_link(dest):
        raise SkillsError(
            f"{dest} is a link; the skills folder must be a real folder in the repo - "
            "remove the link and sync again"
        )


def _drop_links(dest: Path) -> list[str]:
    """Remove every symlink and junction below `dest` without following any, and return
    their paths relative to `dest`."""
    dropped: list[str] = []
    pending = [dest]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.is_symlink() or entry.is_junction():
                    os.unlink(entry.path)  # removes the link only, on Windows too
                    dropped.append(Path(entry.path).relative_to(dest).as_posix())
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
    return sorted(dropped)


@contextmanager
def _sync_lock(dest: Path) -> Iterator[None]:
    """Hold `dest/.sync.lock` for the whole sync, waiting for another holder to finish.
    The OS releases it if the holder dies, so a crash never leaves it stuck."""
    path = dest / LOCK_NAME
    if _is_link(path):
        os.unlink(path)
    with open(path, "a+b") as handle:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                _lock_file(handle)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise SkillsError(
                        f"another sync of {dest} still holds {LOCK_NAME} after "
                        f"{LOCK_WAIT_SECONDS:.0f}s"
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            _unlock_file(handle)


def _lock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _installed(dest: Path) -> list[tuple[str, Path]]:
    """Everything under `dest` as (key, path): skill folders two levels down, and any
    stray entry (a file, a half-copied `.partial` folder) under its own path. The lock
    file is not an entry."""
    found: list[tuple[str, Path]] = []
    for first in _children(dest):
        if first.name == LOCK_NAME and first.parent == dest:
            continue
        if not first.is_dir():
            found.append((first.name, first))
            continue
        found.extend((f"{first.name}/{second.name}", second) for second in _children(first))
    return found


def _children(folder: Path) -> list[Path]:
    return sorted(folder.iterdir()) if folder.is_dir() else []


def _copy(source: Path, target: Path, dest: Path) -> None:
    """Replace `target` with a read-only copy of `source`. The copy is made beside it
    first, so a crash never leaves a half-written skill under the real name."""
    partial = target.with_name(f".{target.name}.partial")
    _remove(partial, dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    _check_inside(target.parent, dest)
    shutil.copytree(source, partial)
    _make_read_only(partial)
    _remove(target, dest)
    partial.rename(target)


def _make_read_only(folder: Path) -> None:
    for path in folder.rglob("*"):
        if path.is_file() and not path.is_symlink():
            path.chmod(stat.S_IREAD)


def _check_inside(path: Path, dest: Path) -> None:
    """A last guard before a write or delete: `path` must really be inside `dest` (a link
    added while the sync runs would point it elsewhere)."""
    if not path.resolve().is_relative_to(dest.resolve()):
        raise SkillsError(f"{path} leads outside {dest}; refusing to change it")


def _remove(path: Path, dest: Path) -> None:
    if not (path.exists() or _is_link(path)):
        return
    if _is_link(path):
        os.unlink(path)  # the link only
        return
    _check_inside(path, dest)
    if path.is_dir():
        shutil.rmtree(path, onexc=_writable_and_retry)
    else:
        _writable_and_retry(os.unlink, str(path), None)


def _writable_and_retry(func: Callable, path: str, _error: BaseException | None) -> None:
    """Windows refuses to delete a read-only file: clear the bit, then try again."""
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    func(path)
