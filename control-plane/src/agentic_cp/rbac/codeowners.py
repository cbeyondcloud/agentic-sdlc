"""Repo maintainers from CODEOWNERS (M2-03, ND-6). The only module that imports `codeowners`
(an import contract enforces it).

The control plane fetches the file itself over git from the repo's default branch - it
never trusts a client's copy - and takes the owners of the agentic paths (`PROBE_PATHS`),
last match winning per path as GitHub and GitLab do. A `@handle` becomes host-qualified
(`github.com:alice`) so it only ever matches a user of the same host; `@org/team` is
reported unresolved until host team sync (phase 3)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from codeowners import CodeOwners

from agentic_cp.gitops import GitRunner

#: Where hosts look for the file, in GitHub's order, then GitLab's extra location.
CANDIDATES = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS", ".gitlab/CODEOWNERS")
#: Representative agentic paths: whoever owns these maintains the repo's agentic setup.
PROBE_PATHS = (
    ".agentic/config.yaml",
    ".agentic/agents/x.md",
    ".agentic/skills/x/SKILL.md",
    "AGENTS.md",
)


@dataclass(frozen=True)
class CodeownersFile:
    commit: str
    path: str
    text: str


@dataclass
class Maintainers:
    handles: set[str] = field(default_factory=set)  # host-qualified, lower case
    emails: set[str] = field(default_factory=set)
    unresolved: set[str] = field(default_factory=set)
    entries: dict[str, str] = field(default_factory=dict)  # owner -> "line N: pattern"

    @property
    def owners(self) -> set[str]:
        return self.handles | self.emails


def maintainers(text: str, host: str) -> Maintainers:
    """The owners of the agentic paths in `text` (a CODEOWNERS file of a repo on `host`)."""
    found = Maintainers()
    try:
        parsed = CodeOwners(text)
    except ValueError as error:  # a malformed pattern: nobody is derived from this file
        found.unresolved.add(f"CODEOWNERS: {error}"[:200])
        return found
    for path in PROBE_PATHS:
        owners, line, pattern, _ = parsed.matching_line(path)
        entry = f"line {line}: {pattern}" if line is not None else "CODEOWNERS"
        for kind, owner in owners:
            if kind == "TEAM":
                found.unresolved.add(owner)
                continue
            if kind == "USERNAME":
                value = f"{host.lower()}:{owner.removeprefix('@').lower()}"
                found.handles.add(value)
            else:
                value = owner.lower()
                found.emails.add(value)
            found.entries.setdefault(value, entry)
    return found


def fetch_codeowners(
    git: GitRunner, workdir: Path, remote: str, tenant_slug: str | None
) -> CodeownersFile | None:
    """The CODEOWNERS file at the remote's default branch head, or None when it has none.
    `workdir` is the tenant's repos folder; each remote gets its own bare repo in it."""
    folder = Path(workdir) / hashlib.sha256(remote.encode("utf-8")).hexdigest()[:24]
    if not (folder / "HEAD").is_file():
        folder.mkdir(parents=True, exist_ok=True)
        git.run(["init", "--bare", "-q"], cwd=folder, tenant_slug=tenant_slug)
    git.run(
        ["fetch", "--depth", "1", "--no-tags", "-q", remote, "HEAD"],
        cwd=folder,
        tenant_slug=tenant_slug,
    )
    commit = git.run(
        ["rev-parse", "--verify", "FETCH_HEAD^{commit}"], cwd=folder, tenant_slug=tenant_slug
    )
    sha = commit.decode("ascii").strip()
    listing = git.run(
        ["ls-tree", "--name-only", "-z", sha, "--", *CANDIDATES],
        cwd=folder,
        tenant_slug=tenant_slug,
    )
    present = {name.decode("utf-8") for name in listing.split(b"\0") if name}
    for path in CANDIDATES:
        if path in present:
            blob = git.run(["show", f"{sha}:{path}"], cwd=folder, tenant_slug=tenant_slug)
            return CodeownersFile(sha, path, blob.decode("utf-8", errors="replace"))
    return None
