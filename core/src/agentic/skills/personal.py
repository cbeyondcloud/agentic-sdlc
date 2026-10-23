"""Personal skills and memory (M2-45, D30; spec §24 "Custom skills").

A user keeps their own skills in `~/.agentic/skills/<name>/SKILL.md` and their own notes in
`~/.agentic/memory/*.md`. Both belong to that user and shape only that user's runs (the
CLI offers them only when the person running is the run's starter). Rules:

  * **Instruction-only.** A personal skill holds Markdown or text files only. A script, a
    binary or a link anywhere in it refuses the whole skill. Its text passes the same
    static scan as a catalogued skill (M1-39): no download-and-run line, no
    prompt-injection phrase, no hidden Unicode, no telemetry URL.
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

from agentic.skills.admission import DOWNLOAD_AND_RUN, HIDDEN, INJECTION, telemetry_urls
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
        _instruction_only(path, f"{where}/{path.relative_to(folder).as_posix()}")
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
        digest = skill_digest(folder)
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
    for number, line in enumerate(text.splitlines(), start=1):
        if any(p.search(line) for p in DOWNLOAD_AND_RUN):
            raise _Refused(f"{where}:{number}", "it downloads and runs code")
        if any(p.search(line) for p in INJECTION):
            raise _Refused(f"{where}:{number}", "it reads like a prompt injection")
        if char := HIDDEN.search(line):
            raise _Refused(f"{where}:{number}", f"hidden character U+{ord(char.group(0)):04X}")
        if urls := telemetry_urls(line):
            raise _Refused(f"{where}:{number}", f"telemetry or analytics URL: {urls[0]}")
    return text


def _note(root: Path, path: Path) -> MemoryNote:
    relative = path.relative_to(root).with_suffix("").as_posix()
    where = f"memory/{path.relative_to(root).as_posix()}"
    text = _instruction_only(path, where)
    name = re.sub(r"[^a-z0-9._/-]+", "-", relative.lower()).strip("-./") or "note"
    content = text.replace("\r\n", "\n").encode("utf-8")
    return MemoryNote(name, f"sha256:{hashlib.sha256(content).hexdigest()}", text.strip())


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

    def _skills(self, agent: "ResolvedAgent") -> list[str]:
        wanted = []
        for skill in self.personal.skills:
            if skill.key in self.shared:
                self.record("skills.personal_shadowed", {"skill": skill.key})
            elif not skill.agents or {agent.definition.name, agent.type} & set(skill.agents):
                wanted.append(skill)
        for skill in wanted[MAX_PERSONAL_SKILLS:]:
            fields = {"skill": skill.key, "max_personal": MAX_PERSONAL_SKILLS}
            self.record("skills.personal_truncated", fields)
        parts = []
        for skill in wanted[:MAX_PERSONAL_SKILLS]:
            self._activated(skill.key, skill.version, skill.sha256, PERSONAL)
            parts.append(f"### Personal skill: {skill.name}\n\n{skill.body}")
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
    "MemoryNote",
    "PersonalContext",
    "PersonalRejection",
    "PersonalSet",
    "PersonalSkill",
    "load_personal",
]
