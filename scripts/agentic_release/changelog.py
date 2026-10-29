"""The changelog section of a release, from the commit subjects (conventional commits).

`type(scope)!: subject` decides the group; a `!` or a `BREAKING CHANGE:` footer marks a
breaking change. Merge commits are left out (their branch's commits are listed). The
range is everything since the newest `v*` tag reachable from HEAD, or the whole history
before the first release.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SUBJECT = re.compile(r"^(?P<type>[a-z]+)(\((?P<scope>[^)]+)\))?(?P<bang>!)?: (?P<subject>.+)$")
_BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE: ", re.M)
#: Section heading per commit type, in the order sections appear.
GROUPS = {
    "breaking": "Breaking changes",
    "feat": "Added",
    "fix": "Fixed",
    "perf": "Changed",
    "refactor": "Changed",
    "docs": "Documentation",
}
OTHER = "Other"
_FIELD, _RECORD = "\x1f", "\x1e"


@dataclass(frozen=True)
class Commit:
    sha: str
    type: str
    scope: str | None
    subject: str
    breaking: bool


def parse_commit(sha: str, subject: str, body: str) -> Commit:
    found = _SUBJECT.match(subject.strip())
    if found is None:
        return Commit(sha, "other", None, subject.strip(), bool(_BREAKING_FOOTER.search(body)))
    breaking = bool(found["bang"]) or bool(_BREAKING_FOOTER.search(body))
    return Commit(sha, found["type"], found["scope"], found["subject"].strip(), breaking)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8"
    )


def last_tag(repo: Path) -> str | None:
    """The newest `v*` tag reachable from HEAD, or None before the first release."""
    found = _git(repo, "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*")
    if found.returncode != 0:
        return None
    return found.stdout.strip() or None


def commits_since(repo: Path, tag: str | None) -> list[Commit]:
    """The non-merge commits after `tag` (all of them when None), newest first."""
    span = [f"{tag}..HEAD"] if tag else ["HEAD"]
    log = _git(repo, "log", "--no-merges", f"--format=%H{_FIELD}%s{_FIELD}%b{_RECORD}", *span)
    if log.returncode != 0:
        raise RuntimeError(f"git log failed: {log.stderr.strip()}")
    commits = []
    for record in filter(str.strip, log.stdout.split(_RECORD)):
        sha, subject, body = (record.strip("\n").split(_FIELD) + ["", ""])[:3]
        commits.append(parse_commit(sha, subject, body))
    return commits


def _group(commit: Commit) -> str:
    return GROUPS["breaking"] if commit.breaking else GROUPS.get(commit.type, OTHER)


def render(version: str, day: str, commits: list[Commit]) -> str:
    """The Markdown section for `version` released on `day` (YYYY-MM-DD)."""
    order = [*dict.fromkeys(GROUPS.values()), OTHER]
    lines = [f"## {version} ({day})", ""]
    for heading in order:
        chosen = [c for c in commits if _group(c) == heading]
        if not chosen:
            continue
        lines += [f"### {heading}", ""]
        for c in chosen:
            scope = f"**{c.scope}:** " if c.scope else ""
            lines.append(f"- {scope}{c.subject} ({c.sha[:7]})")
        lines.append("")
    if len(lines) == 2:
        lines += ["No changes.", ""]
    return "\n".join(lines)


def suggest_bump(commits: list[Commit]) -> str:
    """major for a breaking change, minor for a feature, else patch."""
    if any(c.breaking for c in commits):
        return "major"
    return "minor" if any(c.type == "feat" for c in commits) else "patch"


def prepend(path: Path, section: str) -> None:
    """Put `section` first in the changelog file (created when missing)."""
    title = "# Changelog\n"
    old = path.read_text(encoding="utf-8").replace("\r\n", "\n") if path.is_file() else title
    rest = old[len(title) :].lstrip("\n") if old.startswith(title) else old
    text = f"{title}\n{section.rstrip()}\n" + (f"\n{rest}" if rest.strip() else "")
    path.write_text(text, encoding="utf-8", newline="\n")
