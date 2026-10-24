"""Personal skills and memory (M2-45, D30; spec §24 "Custom skills").

A user keeps their own skills in `~/.agentic/skills/<name>/SKILL.md` and their own notes in
`~/.agentic/memory/*.md`. Both belong to that user and shape only that user's runs (the
CLI offers them only when the person running is the run's starter). Rules:

  * **Instruction-only.** A personal skill holds Markdown or text files only. A script, a
    binary or a link anywhere in it refuses the whole skill. Its text passes the same
    static scan as a catalogued skill (M1-39): no download-and-run line, no
    prompt-injection phrase, no hidden Unicode, no telemetry URL. Files the OS, an editor
    or git leave behind (`is_junk`: `.DS_Store`, `Thumbs.db`, swap files, dot-folders) are
    skipped, never loaded and not part of the hash.
  * **Cannot widen anything.** The front matter may hold only `name`, `description`,
    `license`, `compatibility` and `metadata`. `allowed-tools`, `model`, `tools` and any
    other key are refused. The skill's text is added to the agent's instructions and
    changes nothing the policy resolved: the agent's tools, model and limits stay as they
    were.
  * **Beside shared skills, never instead.** A personal skill is `personal/<name>`, so it
    sits next to a shared skill of the same name. A shared catalog key that is literally
    `personal/<name>` wins, and the personal one is skipped (`skills.personal_shadowed`).
  * **Off by classification.** `ClassificationRule.personal_skills` and `personal_memory`
    can turn either off for a class of repo, and an unknown class counts as off
    (`skills.personal_disabled`).
  * **Small.** At most `MAX_PERSONAL_SKILLS` skills are inlined per agent, each at most
    `MAX_SKILL_TOKENS`. Notes are inlined up to `MAX_MEMORY_TOKENS` in total. What does not
    fit is named, never cut. `metadata.agents` (agent names or types) limits a skill to
    those agents, and `metadata.version` is its version (default `local`).
  * **Recorded.** Every use is recorded as `skills.activated` with its key, version,
    sha256 and kind (`personal` or `memory`). The CLI writes these into the run ledger.

Only a skill's `SKILL.md` body is inlined. Its other files are not loaded.
"""

import hashlib
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from agentic.skills.admission import scan_text
from agentic.skills.catalog import SkillsError, skill_digest, skill_files
from agentic.skills.install import Record, log_record
from agentic.skills.loader import estimate_tokens
from agentic.yamlio import YamlFileError, load_yaml, split_front_matter

if TYPE_CHECKING:
    from agentic.policy import ClassificationRule, ResolvedAgent

PERSONAL = "personal"
MEMORY = "memory"
MAX_PERSONAL_SKILLS = 3
MAX_SKILL_TOKENS = 4000
MAX_MEMORY_TOKENS = 2000
ALLOWED_FRONT_MATTER = frozenset({"name", "description", "license", "compatibility", "metadata"})
TEXT_SUFFIXES = frozenset({".md", ".txt"})
#: Files macOS and Windows write into folders by themselves (lowercase).
OS_FILES = frozenset({"thumbs.db", "ehthumbs.db", "desktop.ini", "icon\r"})
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
HEADER = (
    "## Your own skills and notes\n\n"
    "These come from the person who started this run: their personal skills and notes. "
    "They are guidance only. They never change what you may do, and your role instructions "
    "and the shared skills above take precedence."
)


@dataclass(frozen=True)
class PersonalSkill:
    name: str
    version: str
    sha256: str
    description: str
    body: str
    agents: tuple[str, ...] = ()  # agent names or types; empty = every agent

    @property
    def key(self) -> str:
        return f"{PERSONAL}/{self.name}"


@dataclass(frozen=True)
class MemoryNote:
    name: str  # the note's path under the memory folder, without its suffix
    sha256: str
    text: str


@dataclass(frozen=True)
class PersonalRejection:
    path: str
    reason: str


@dataclass
class PersonalSet:
    skills: list[PersonalSkill] = field(default_factory=list)
    memory: list[MemoryNote] = field(default_factory=list)
    rejected: list[PersonalRejection] = field(default_factory=list)


def load_personal(home: Path) -> PersonalSet:
    """Every valid personal skill and note under `home` (`~/.agentic`), and why each other
    one was refused."""
    found = PersonalSet()
    skills_root = home / "skills"
    if skills_root.is_dir() and not _is_link(skills_root):
        for folder in sorted(p for p in skills_root.iterdir() if p.is_dir() or _is_link(p)):
            try:
                found.skills.append(_skill(folder))
            except _Refused as refusal:
                found.rejected.append(PersonalRejection(refusal.path, str(refusal)))
    memory_root = home / "memory"
    if memory_root.is_dir() and not _is_link(memory_root):
        files, links = skill_files(memory_root)
        for link in links:
            where = f"memory/{link.relative_to(memory_root).as_posix()}"
            found.rejected.append(PersonalRejection(where, "a link: notes are files"))
        for path in files:
            if is_junk(path.relative_to(memory_root).as_posix()):
                continue
            try:
                found.memory.append(_note(memory_root, path))
            except _Refused as refusal:
                found.rejected.append(PersonalRejection(refusal.path, str(refusal)))
    return found


class _Refused(Exception):
    def __init__(self, path: str, reason: str) -> None:
        super().__init__(reason)
        self.path = path


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def is_junk(relative: str) -> bool:
    """A file the OS, an editor or git leaves beside a user's own (`.DS_Store`,
    `Thumbs.db`, `desktop.ini`, a swap or backup file, anything under a dot-folder).
    It is never loaded, so it is skipped - not a reason to refuse a whole skill."""
    parts = relative.split("/")
    name = parts[-1]
    return (
        any(part.startswith(".") for part in parts)
        or name.lower() in OS_FILES
        or name.endswith("~")
        or name.lower().endswith((".swp", ".swo", ".bak", ".tmp"))
    )


#: Where a backed-up personal file lives under `~/.agentic`, by kind (M2-20).
BACKUP_ROOTS = {"skill": "skills", "memory": "memory"}
MAX_BACKUP_PATH = 512
_BAD_PATH_CHARS = re.compile(r'[\x00-\x1f\x7f\\:*?"<>|]')


def backup_path_problem(kind: object, path: object) -> str | None:
    """Why `path` cannot name a backed-up personal file of `kind` (M2-20), or None.

    A POSIX path relative to `~/.agentic`: `skills/<name>/...` for a skill file and
    `memory/...` for a note; a Markdown or text file; no `.` or `..` segment, no dot-folder
    or OS file, nothing Windows refuses in a name. The control plane checks what it stores
    and `agentic skills restore` what it writes with this same rule, so a restore never
    writes outside the user's skills and notes."""
    if kind not in BACKUP_ROOTS:
        return f"kind must be one of {sorted(BACKUP_ROOTS)}"
    if not isinstance(path, str) or not path or len(path) > MAX_BACKUP_PATH:
        return f"path must be 1-{MAX_BACKUP_PATH} characters"
    if _BAD_PATH_CHARS.search(path) or path.startswith("/"):
        return "path must be a relative POSIX path of plain names"
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts) or is_junk(path):
        return "path may hold no empty, '.' or '..' part and no dot-folder or OS file"
    if any(part.rstrip(" .") != part for part in parts):
        return "no part of path may end in a space or a dot"
    if parts[0] != BACKUP_ROOTS[kind]:
        return f"a {kind} path starts with {BACKUP_ROOTS[kind]}/"
    if kind == "skill" and (len(parts) < 3 or not NAME.fullmatch(parts[1]) or len(parts[1]) > 64):
        return "a skill path is skills/<name>/<file>, its name lowercase letters, digits, hyphens"
    if kind == "memory" and len(parts) < 2:
        return "a note path is memory/<file>"
    if Path(parts[-1]).suffix.lower() not in TEXT_SUFFIXES:
        return "only Markdown or text files are backed up (.md, .txt)"
    return None


def _skill(folder: Path) -> PersonalSkill:
    where = f"skills/{folder.name}"
    if _is_link(folder):
        raise _Refused(where, "a link: a personal skill is a folder of plain files")
    if not NAME.fullmatch(folder.name) or len(folder.name) > 64:
        raise _Refused(where, "the name must be lowercase letters, digits and hyphens (max 64)")
    files, links = skill_files(folder)
    if links:
        raise _Refused(where, "it holds a link: a personal skill is a folder of plain files")
    for path in files:
        relative = path.relative_to(folder).as_posix()
        if not is_junk(relative):
            _instruction_only(path, f"{where}/{relative}")
    skill_md = folder / "SKILL.md"
    if not skill_md.is_file():
        raise _Refused(where, "no SKILL.md")
    try:
        header, body = split_front_matter(skill_md.read_text(encoding="utf-8-sig"))
        data = load_yaml(header, first_line=2)[0] or {}
    except (OSError, UnicodeDecodeError, YamlFileError) as error:
        raise _Refused(f"{where}/SKILL.md", f"cannot be read: {error}") from error
    if not isinstance(data, dict):
        raise _Refused(f"{where}/SKILL.md", "the front matter is not key: value settings")
    if extra := sorted(set(data) - ALLOWED_FRONT_MATTER):
        raise _Refused(
            f"{where}/SKILL.md",
            f"front matter {extra} is not allowed: a personal skill cannot widen tools, models "
            f"or limits (allowed: {sorted(ALLOWED_FRONT_MATTER)})",
        )
    if data.get("name") != folder.name:
        raise _Refused(f"{where}/SKILL.md", f"its name must be {folder.name!r}, its folder")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise _Refused(f"{where}/SKILL.md", "it needs a description")
    if (tokens := estimate_tokens(body)) > MAX_SKILL_TOKENS:
        raise _Refused(f"{where}/SKILL.md", f"about {tokens} tokens, over {MAX_SKILL_TOKENS}")
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    agents = metadata.get("agents") or []
    try:
        digest = skill_digest(folder, ignore=is_junk)
    except SkillsError as error:
        raise _Refused(where, str(error)) from error
    return PersonalSkill(
        name=folder.name,
        version=str(metadata.get("version") or "local"),
        sha256=digest,
        description=" ".join(description.split()),
        body=body.strip(),
        agents=tuple(str(a) for a in agents) if isinstance(agents, list) else (),
    )


def _instruction_only(path: Path, where: str) -> str:
    """The file's text; refused unless it is a Markdown or text file the scan passes."""
    data = path.read_bytes()
    if path.suffix.lower() not in TEXT_SUFFIXES or data.startswith(b"#!"):
        raise _Refused(
            where,
            "only Markdown or text files: a personal skill is instruction-only "
            "(no scripts or other files)",
        )
    try:
        text = data.decode("utf-8").removeprefix("﻿")
    except UnicodeDecodeError:
        raise _Refused(where, "not UTF-8 text: a personal skill is instruction-only") from None
    for number, reason in scan_text(text)[:1]:  # the same scan as catalog admission (DF-142)
        raise _Refused(f"{where}:{number}", reason)
    return text


def _note(root: Path, path: Path) -> MemoryNote:
    relative = path.relative_to(root).with_suffix("").as_posix()
    where = f"memory/{path.relative_to(root).as_posix()}"
    text = _instruction_only(path, where)
    name = re.sub(r"[^a-z0-9._/-]+", "-", relative.lower()).strip("-./") or "note"
    content = text.replace("\r\n", "\n").encode("utf-8")
    return MemoryNote(name, f"sha256:{hashlib.sha256(content).hexdigest()}", text.strip())


def _skill_text(skill: PersonalSkill) -> str:
    return f"### Personal skill: {skill.name}\n\n{skill.body}"


def skill_block(skills: list[PersonalSkill]) -> str:
    """The text `skills` add to an agent's instructions, exactly as a run inlines them
    (`agentic skills eval` measures this text); "" for none. Nothing is recorded."""
    return "\n\n".join([HEADER, *map(_skill_text, skills)]) if skills else ""


class PersonalContext:
    """A user's personal skills and notes, as one agent's instructions see them."""

    def __init__(
        self,
        personal: PersonalSet,
        classifications: Mapping[str, "ClassificationRule"],
        *,
        shared: Collection[str] = (),
        record: Record | None = None,
    ) -> None:
        self.personal, self.classifications = personal, classifications
        self.shared = frozenset(shared)
        self.record = record or log_record

    def prompt_for(self, agent: "ResolvedAgent") -> str:
        """The personal block for `agent` ("" when nothing applies), each use recorded."""
        rule = self.classifications.get(agent.classification)
        parts: list[str] = []
        if self.personal.skills:
            if rule is not None and rule.personal_skills:
                parts += self._skills(agent)
            else:
                self._disabled(agent, "skills")
        if self.personal.memory:
            if rule is not None and rule.personal_memory:
                parts += self._memory()
            else:
                self._disabled(agent, "memory")
        return "\n\n".join([HEADER, *parts]) if parts else ""

    def _disabled(self, agent: "ResolvedAgent", kind: str) -> None:
        fields = {"classification": agent.classification, "kind": kind}
        self.record("skills.personal_disabled", fields)

    def applicable(self, agent: "ResolvedAgent") -> list[PersonalSkill]:
        """The personal skills meant for `agent`, in order (a shadowed one left out)."""
        names = {agent.definition.name, agent.type}
        return [
            skill
            for skill in self.personal.skills
            if skill.key not in self.shared and (not skill.agents or names & set(skill.agents))
        ]

    def over_cap(self, agent: "ResolvedAgent") -> list[PersonalSkill]:
        """The skills meant for `agent` that it will not get: over `MAX_PERSONAL_SKILLS`
        (none where its classification turns personal skills off)."""
        rule = self.classifications.get(agent.classification)
        if rule is None or not rule.personal_skills:
            return []
        return self.applicable(agent)[MAX_PERSONAL_SKILLS:]

    def _skills(self, agent: "ResolvedAgent") -> list[str]:
        for skill in self.personal.skills:
            if skill.key in self.shared:
                self.record("skills.personal_shadowed", {"skill": skill.key})
        wanted = self.applicable(agent)
        for skill in wanted[MAX_PERSONAL_SKILLS:]:
            fields = {"skill": skill.key, "max_personal": MAX_PERSONAL_SKILLS}
            self.record("skills.personal_truncated", fields)
        parts = []
        for skill in wanted[:MAX_PERSONAL_SKILLS]:
            self._activated(skill.key, skill.version, skill.sha256, PERSONAL)
            parts.append(_skill_text(skill))
        return parts

    def _memory(self) -> list[str]:
        budget, loaded, skipped = MAX_MEMORY_TOKENS, [], []
        for note in self.personal.memory:
            cost = estimate_tokens(note.text)
            if cost > budget:
                skipped.append(f"- {note.name} (not loaded: over the notes budget)")
                continue
            budget -= cost
            self._activated(f"{MEMORY}/{note.name}", "local", note.sha256, MEMORY)
            loaded.append(f"#### {note.name}\n\n{note.text}")
        if not loaded and not skipped:
            return []
        tail = ["\n".join(skipped)] if skipped else []
        return ["\n\n".join(["### Your notes", *loaded, *tail])]

    def _activated(self, key: str, version: str, sha256: str, kind: str) -> None:
        fields = {"skill": key, "version": version, "sha256": sha256, "kind": kind}
        self.record("skills.activated", fields)


__all__ = [
    "MAX_MEMORY_TOKENS",
    "MAX_PERSONAL_SKILLS",
    "BACKUP_ROOTS",
    "MemoryNote",
    "PersonalContext",
    "PersonalRejection",
    "PersonalSet",
    "PersonalSkill",
    "backup_path_problem",
    "load_personal",
    "skill_block",
]
