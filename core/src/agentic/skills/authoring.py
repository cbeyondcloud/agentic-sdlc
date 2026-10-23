"""Authoring a skill and preparing its promotion (M2-46; spec §24 "Custom skills", D30).

* `scaffold` writes a new **personal** skill, `~/.agentic/skills/<name>/SKILL.md`, in the
  open Agent Skills format: front matter (`name`, `description`, `metadata.version`) and
  a plain-English body with three sections to fill in.
* `check_skill` is `agentic skills validate`: every problem, not only the first, each
  a `Rejection` naming its check:

    validate  the format: skills-ref (the Agent Skills reference validator) when it is
              installed, else our own rules (SKILL.md, name == folder, a description)
    widening  personal and repo skills: front matter beyond name, description,
              license, compatibility and metadata (`allowed-tools`, `model`, ...)
    scripts   personal and repo skills are instruction-only: only Markdown or text,
              no script, binary or link; a team or org skill's scripts need a
              person's review (`scripts_reviewed_by`), as catalog admission says
    size      SKILL.md within the token budget, the folder within its byte budget
    secrets   a credential the secret scanner finds (detect-secrets' patterns)
    scan      the static scan of catalog admission (download-and-run, injection,
              hidden characters, telemetry URLs)

  Files the OS or an editor leaves behind (`is_junk`) are skipped, as when the skill
  is loaded.
* `promotion_paths`, `catalog_entry` and `add_catalog_entry` say where a promoted copy
  goes (`agentic skills submit`): `.agentic/skills/<name>/` in the repo, or the policy
  repo's team or org catalog. The catalog file gets the new entry as text inserted
  after its `skills:` line, so an admin's comments and layout survive, and the result
  is loaded again to prove nothing else changed.
"""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from agentic.security.secret_scan import SecretScanner
from agentic.skills.admission import (
    BOM,
    SCRIPT_SUFFIXES,
    Rejection,
    scan_text,
    skills_ref_problems,
)
from agentic.skills.catalog import AdmissionLimits, SkillsError, skill_digest, skill_files
from agentic.skills.loader import estimate_tokens
from agentic.skills.personal import (
    ALLOWED_FRONT_MATTER,
    MAX_SKILL_TOKENS,
    NAME,
    TEXT_SUFFIXES,
    is_junk,
)
from agentic.yamlio import YamlFileError, dump_yaml, load_yaml, split_front_matter

Scope = Literal["personal", "repo", "team", "org"]
SCOPES: tuple[Scope, ...] = ("personal", "repo", "team", "org")
#: The branch a promotion is proposed on (format with `name=`).
PROMOTION_BRANCH = "agentic/skill/{name}"
#: The catalog source of skills an org writes itself (`<root>/internal/<name>/`).
INTERNAL = "internal"
MAX_DESCRIPTION = 1024
_INSTRUCTION_ONLY: frozenset[str] = frozenset({"personal", "repo"})
#: Front matter keys the Agent Skills format allows (team and org skills).
_AGENT_SKILLS_KEYS = frozenset(ALLOWED_FRONT_MATTER | {"allowed-tools"})
_UNEXPECTED = "Unexpected fields in frontmatter"
_SKILLS_LINE = re.compile(r"^skills:[ \t]*(\{[ \t]*\})?[ \t]*(#.*)?$")

SKILL_TEMPLATE = """\
---
name: {name}
description: {description}
metadata:
  version: "0.1.0"
---

# {title}

## When to use

(The situations where an agent should follow this skill: the kind of change, the part of
the code, the signal that it applies.)

## Instructions

(Plain-English steps: what to do, what to avoid, and why. Write them for a careful new
colleague.)

## Example

(A short example of doing it right.)
"""
PLACEHOLDER_DESCRIPTION = "Use when (describe the situation where an agent should apply this)."


def check_name(name: str) -> None:
    if not NAME.fullmatch(name) or len(name) > 64:
        raise SkillsError(
            f"{name!r}: a skill name is lowercase letters, digits and single hyphens "
            "(at most 64 characters), e.g. payments-retries"
        )


def scaffold(home: Path, name: str, description: str | None = None) -> Path:
    """Write `<home>/skills/<name>/SKILL.md` from the template; the folder. Never
    overwrites: an existing skill of that name is a SkillsError."""
    check_name(name)
    folder = home / "skills" / name
    if folder.exists() or folder.is_symlink():
        raise SkillsError(f"{folder} already exists: edit it, or pick another name")
    text = " ".join((description or PLACEHOLDER_DESCRIPTION).split())
    if len(text) > MAX_DESCRIPTION:
        raise SkillsError(f"the description is over {MAX_DESCRIPTION} characters")
    title = name.replace("-", " ").capitalize()
    content = SKILL_TEMPLATE.format(name=name, description=_scalar(text), title=title)
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_bytes(content.encode("utf-8"))
    return folder


def _scalar(text: str) -> str:
    """`text` as a one-line YAML scalar."""
    return dump_yaml({"v": text}).strip().removeprefix("v: ")


# ── check ────────────────────────────────────────────────────────────────────


def check_skill(
    folder: Path,
    scope: Scope,
    *,
    limits: AdmissionLimits | None = None,
    scripts_reviewed_by: str | None = None,
) -> list[Rejection]:
    """Every reason `folder` may not be used (personal) or promoted to `scope` ([] = ok)."""
    if scope not in SCOPES:
        raise ValueError(f"scope is one of {SCOPES}, not {scope!r}")
    if folder.is_symlink() or folder.is_junction() or not folder.is_dir():
        return [Rejection("validate", ".", f"no skill folder at {folder}")]
    limits = limits or AdmissionLimits()
    files, links = skill_files(folder)
    found = [
        Rejection("scripts", link.relative_to(folder).as_posix(), "a link: skills hold files")
        for link in links
    ]
    texts: dict[str, str] = {}
    total = 0
    for path in files:
        relative = path.relative_to(folder).as_posix()
        if is_junk(relative):
            continue
        data = path.read_bytes()
        total += len(data)
        try:
            text: str | None = data.decode("utf-8").removeprefix(BOM)
        except UnicodeDecodeError:
            text = None
        script = path.suffix.lower() in SCRIPT_SUFFIXES or data.startswith(b"#!")
        if scope in _INSTRUCTION_ONLY:
            if path.suffix.lower() not in TEXT_SUFFIXES or script or text is None:
                why = (
                    f"only Markdown or text files: a {scope} skill is instruction-only "
                    "(no scripts, binaries or other files)"
                )
                found.append(Rejection("scripts", relative, why))
                continue
        elif (script or text is None) and scripts_reviewed_by is None:
            what = "a script" if script else "a binary file"
            why = f"{what}: a person must review it (scripts_reviewed_by) before it is catalogued"
            found.append(Rejection("scripts", relative, why))
        if text is not None:
            texts[relative] = text
    found += _format(folder, scope)
    found += _size(texts.get("SKILL.md"), total, scope, limits)
    scanner = SecretScanner()
    for relative, text in texts.items():
        for finding in scanner.findings(text):
            line = text.count("\n", 0, finding.start) + 1
            why = f"looks like a secret ({finding.detector}): remove it, a skill is shared text"
            found.append(Rejection("secrets", f"{relative}:{line}", why))
        for number, reason in scan_text(text):
            found.append(Rejection("scan", f"{relative}:{number}", reason))
    return found


def _format(folder: Path, scope: Scope) -> list[Rejection]:
    skill_md = folder / "SKILL.md"
    if not skill_md.is_file():
        return [Rejection("validate", "SKILL.md", "no SKILL.md")]
    try:
        header, _ = split_front_matter(skill_md.read_text(encoding="utf-8-sig"))
        data = load_yaml(header, first_line=2)[0] or {}
    except (OSError, UnicodeDecodeError, YamlFileError) as error:
        return [Rejection("validate", "SKILL.md", f"cannot be read: {error}")]
    if not isinstance(data, dict):
        return [Rejection("validate", "SKILL.md", "the front matter is not key: value settings")]
    found = []
    wider = sorted(set(data) - ALLOWED_FRONT_MATTER) if scope in _INSTRUCTION_ONLY else []
    if wider:
        why = (
            f"front matter {wider} is not allowed: a {scope} skill cannot widen tools, models "
            f"or limits (allowed: {sorted(ALLOWED_FRONT_MATTER)})"
        )
        found.append(Rejection("widening", "SKILL.md", why))
    problems = skills_ref_problems(folder)
    if problems is None:
        problems = _own_format(folder, data)
    elif wider:  # already named, as widening
        problems = [p for p in problems if not p.startswith(_UNEXPECTED)]
    return found + [Rejection("validate", "SKILL.md", problem) for problem in problems]


def _own_format(folder: Path, data: dict) -> list[str]:
    """The format rules when skills-ref is not installed."""
    problems = []
    name = data.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name) or len(name) > 64:
        problems.append("name must be lowercase letters, digits and hyphens (at most 64)")
    elif name != folder.name:
        problems.append(f"name {name!r} must match the folder name {folder.name!r}")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        problems.append("a description is required: when should an agent use this skill?")
    elif len(description) > MAX_DESCRIPTION:
        problems.append(f"the description is over {MAX_DESCRIPTION} characters")
    if extra := sorted(set(data) - _AGENT_SKILLS_KEYS):
        problems.append(f"{_UNEXPECTED}: {', '.join(extra)}")
    return problems


def _size(
    skill_md: str | None, total: int, scope: Scope, limits: AdmissionLimits
) -> list[Rejection]:
    found = []
    budget = limits.max_skill_tokens
    if scope == "personal":
        budget = min(budget, MAX_SKILL_TOKENS)
    if skill_md is not None and (tokens := estimate_tokens(skill_md)) > budget:
        found.append(Rejection("size", "SKILL.md", f"about {tokens} tokens, over {budget}"))
    if total > limits.max_total_bytes:
        found.append(Rejection("size", ".", f"{total} bytes, over {limits.max_total_bytes}"))
    return found


# ── promotion ────────────────────────────────────────────────────────────────


def skill_sha(folder: Path) -> str:
    """The skill's content hash as a promoted copy will have it (junk files left out)."""
    return skill_digest(folder, ignore=is_junk)


def skill_files_for_commit(folder: Path) -> dict[str, bytes]:
    """POSIX relative path -> bytes (CRLF as LF) of every file a promoted copy holds."""
    files, links = skill_files(folder)
    if links:
        raise SkillsError(f"{folder} holds a link: a skill holds plain files")
    out = {}
    for path in files:
        relative = path.relative_to(folder).as_posix()
        if not is_junk(relative):
            out[relative] = path.read_bytes().replace(b"\r\n", b"\n")
    return out


def promotion_paths(scope: Scope, name: str, team: str | None = None) -> tuple[str, str | None]:
    """(the promoted skill's folder, the catalog file to add it to or None), as POSIX
    paths inside the target repository (the repo itself, or the policy repo)."""
    check_name(name)
    if scope == "repo":
        return f".agentic/skills/{name}", None
    if scope == "team":
        if not team or not NAME.fullmatch(team):
            raise SkillsError("a team promotion names its team (lowercase-with-hyphens)")
        root = f"teams/{team}/skills"
        return f"{root}/{INTERNAL}/{name}", f"{root}/catalog.yaml"
    if scope == "org":
        return f"skills/{INTERNAL}/{name}", "skills/catalog.yaml"
    raise SkillsError(f"a skill is promoted to repo, team or org, not {scope!r}")


def catalog_entry(
    name: str, sha256: str, *, owner: str, version: str = "1.0.0", path: str | None = None
) -> dict[str, str]:
    """The catalog entry of a promoted internal skill (`SkillEntry` fields)."""
    return {
        "source": INTERNAL,
        "commit": INTERNAL,
        "path": path or f"{INTERNAL}/{name}",
        "licence": INTERNAL,
        "owner": owner,
        "version": version,
        "sha256": sha256,
        "loading": "on-demand",
        "notes": "promoted from a personal skill (agentic skills submit)",
    }


def add_catalog_entry(text: str | None, key: str, entry: Mapping[str, str]) -> str:
    """`text` (a catalog.yaml, or None for a new one) with `key: entry` added under
    `skills:`, everything else kept as written. SkillsError when the key is there."""
    original = text or ""
    crlf = "\r\n" in original
    lf = original.replace("\r\n", "\n")
    try:
        before = load_yaml(lf)[0] if lf.strip() else {}
    except YamlFileError as error:
        raise SkillsError(f"the catalog cannot be read: {error}") from error
    before = before if isinstance(before, dict) else {}
    skills = before.get("skills") or {}
    if key in skills:
        raise SkillsError(f"{key} is already in the catalog")
    lines = lf.split("\n") if lf else []
    at = next((i for i, line in enumerate(lines) if _SKILLS_LINE.match(line)), None)
    if at is None:
        if lines and lines[-1] == "":
            lines.pop()
        lines += ["skills:"]
        at = len(lines) - 1
        lines.append("")
    else:
        lines[at] = "skills:"
    indent = _entry_indent(lines[at + 1 :])
    block = dump_yaml({key: dict(entry)}).rstrip("\n").split("\n")
    lines[at + 1 : at + 1] = [indent + line for line in block]
    result = "\n".join(lines)
    result = result if result.endswith("\n") else result + "\n"
    after = load_yaml(result)[0]
    kept = {k: v for k, v in after["skills"].items() if k != key}
    if after["skills"].get(key) != dict(entry) or kept != dict(skills):
        raise SkillsError("the catalog could not be edited safely; add the entry by hand")
    return result.replace("\n", "\r\n") if crlf else result


def _entry_indent(rest: list[str]) -> str:
    """The indentation of the existing entries under `skills:` (two spaces if none)."""
    for line in rest:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = line[: len(line) - len(line.lstrip())]
        return indent or "  "
    return "  "


__all__ = [
    "INTERNAL",
    "PROMOTION_BRANCH",
    "SCOPES",
    "Scope",
    "add_catalog_entry",
    "catalog_entry",
    "check_name",
    "check_skill",
    "promotion_paths",
    "scaffold",
    "skill_files_for_commit",
    "skill_sha",
]
