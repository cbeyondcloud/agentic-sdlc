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

It writes only under `dest` (the repo's git-ignored `.agentic/local/skills/`).
"""

import logging
import os
import shutil
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from agentic.skills.catalog import Catalog, SkillsError, skill_digest, skill_folder

Record = Callable[[str, dict[str, object]], None]
_log = logging.getLogger("agentic.skills")


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

    report = InstallReport()
    for key in keys:
        target = skill_folder(dest, key)
        expected = catalog.skills[key].sha256
        if not target.exists():
            _copy(skill_folder(source_root, key), target)
            report.installed.append(key)
            record("skills.installed", {"skill": key, "sha256": expected})
            continue
        found = skill_digest(target) if target.is_dir() else "not a folder"
        if found == expected:
            _make_read_only(target)
            report.unchanged.append(key)
            continue
        _copy(skill_folder(source_root, key), target)
        report.restored.append(key)
        record("skills.tamper_restored", {"skill": key, "expected": expected, "found": found})

    for key, path in _installed(dest):
        if key not in keys:
            _remove(path)
            report.removed.append(key)
            record("skills.removed", {"skill": key})
    for source_dir in _children(dest):
        if source_dir.is_dir() and not any(source_dir.iterdir()):
            source_dir.rmdir()
    return report


def _installed(dest: Path) -> list[tuple[str, Path]]:
    """Everything under `dest` as (key, path): skill folders two levels down, and any
    stray entry (a file, a half-copied `.partial` folder) under its own path."""
    found: list[tuple[str, Path]] = []
    for first in _children(dest):
        if not first.is_dir():
            found.append((first.name, first))
            continue
        found.extend((f"{first.name}/{second.name}", second) for second in _children(first))
    return found


def _children(folder: Path) -> list[Path]:
    return sorted(folder.iterdir()) if folder.is_dir() else []


def _copy(source: Path, target: Path) -> None:
    """Replace `target` with a read-only copy of `source`. The copy is made beside it
    first, so a crash never leaves a half-written skill under the real name."""
    partial = target.with_name(f".{target.name}.partial")
    _remove(partial)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, partial)
    _make_read_only(partial)
    _remove(target)
    partial.rename(target)


def _make_read_only(folder: Path) -> None:
    for path in folder.rglob("*"):
        if path.is_file():
            path.chmod(stat.S_IREAD)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, onexc=_writable_and_retry)
    elif path.exists() or path.is_symlink():
        _writable_and_retry(os.unlink, str(path), None)


def _writable_and_retry(func: Callable, path: str, _error: BaseException | None) -> None:
    """Windows refuses to delete a read-only file: clear the bit, then try again."""
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    func(path)
