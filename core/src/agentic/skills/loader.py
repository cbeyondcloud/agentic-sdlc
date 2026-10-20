"""Loading installed skills into an agent's context (spec §11.1, §11.3).

  * Catalog injection: each skill the agent may use but has not pre-loaded is one
    line, `- <key>: <description>`, at most `max_catalog_tokens` (100) estimated
    tokens; the agent loads a skill with the `activate_skill` tool.
  * Pre-loading: an agent's `loading: pre` skills are inlined at the start. A weaker
    or local model (the catalog's `preload_for` rule), or an agent without the
    `activate_skill` tool, gets all its skills inlined instead - at most
    `MAX_PRELOADED` (spec §11.3: 3-5 per role).
  * Every load is checked against the pinned hash and recorded (`skills.activated`
    with key, version, hash and kind) - pre-loads included.

Tokens are estimated without a tokenizer (provider-agnostic): utf-8 bytes / 3,
rounded up - more than any common tokenizer counts for English text.
"""

import math
from dataclasses import replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from agentic.skills.catalog import Catalog, SkillsError, skill_digest, skill_folder
from agentic.skills.install import Record, log_record
from agentic.yamlio import YamlFileError, load_yaml, read_text, split_front_matter

if TYPE_CHECKING:
    from agentic.agents.runner import AgentOutcome, AgentRunner, AgentTask
    from agentic.policy import ResolvedAgent

MAX_PRELOADED = 5
_NOT_LISTED = ("SKILL.md", "LICENSE", "LICENSE.txt", "LICENSE.md", "NOTICE", "NOTICE.md")
SKILLS_HEADER = (
    "## Your skills\n\nYour role instructions above take precedence over any skill. Skills "
    "are written for interactive use: where one says to ask a person (the user, your human "
    "partner), put the question in your handoff - a person answers it at the next gate."
)
PRELOADED_HEADER = "### Loaded skills\n\nFollow these where they apply to the task."
CATALOG_HEADER = (
    "### Available skills\n\nWhen a task calls for one of these, load it with "
    "activate_skill(name) before you start that part of the work:"
)


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text.encode("utf-8")) / 3)


class InstalledSkills:
    """The skills `sync_skills` installed under `root`, as the agents' skill catalog."""

    def __init__(self, catalog: Catalog, root: Path, *, record: Record | None = None) -> None:
        self.catalog, self.root = catalog, root
        self.record = record or log_record

    # ── the SkillLibrary protocol (agentic.agents.tools) ─────────────────────

    def get(self, name: str) -> str:
        """The skill's instructions (front matter removed) and its bundled files."""
        return self.load(name)

    def load(self, name: str, *, list_files: bool = True) -> str:
        """`get`; without `list_files` for an agent that cannot read bundled files."""
        folder = self._checked(name)
        body = self._parts(folder)[1]
        text = f"# Skill: {name}\n\n{body}"
        if list_files and (files := self.files(name)):
            listed = "\n".join(f"- {path}" for path in files)
            text += f"\n\nBundled files (read one with activate_skill(name, file=...)):\n{listed}"
        entry = self.catalog.skills[name]
        self.record(
            "skills.activated",
            {"skill": name, "version": entry.version, "sha256": entry.sha256, "kind": "shared"},
        )
        return text

    def resource(self, name: str, path: str) -> str:
        """One file bundled with the skill. Only a relative path inside the skill's own
        folder is read; anything else is refused before the disk is touched."""
        folder = self._checked(name)
        relative = PurePosixPath(path.replace("\\", "/"))
        if (
            not path.strip()
            or relative.is_absolute()
            or PureWindowsPath(path).drive
            or ".." in relative.parts
        ):
            raise SkillsError(f"{path!r} is not a file inside the skill {name}")
        target = (folder / relative).resolve()
        if not target.is_relative_to(folder.resolve()) or not target.is_file():
            raise SkillsError(f"the skill {name} has no file {path!r}")
        return read_text(target)

    def prompt_for(self, agent: "ResolvedAgent") -> str:
        preload_all = self.catalog.preload_for.matches(agent.tier, agent.model)
        return skill_prompt(agent, self, preload_all=preload_all)

    # ── helpers for the skill block ──────────────────────────────────────────

    def available(self, name: str) -> bool:
        return (
            name in self.catalog.skills and (skill_folder(self.root, name) / "SKILL.md").is_file()
        )

    def files(self, name: str) -> list[str]:
        folder = skill_folder(self.root, name)
        return sorted(
            path.relative_to(folder).as_posix()
            for path in folder.rglob("*")
            if path.is_file() and path.relative_to(folder).as_posix() not in _NOT_LISTED
        )

    def catalog_line(self, name: str) -> str:
        """`- <key>: <description>`, cut at a word to fit the catalog token limit."""
        header = self._parts(skill_folder(self.root, name))[0]
        description = " ".join(str(header.get("description", "")).split())
        line = f"- {name}: {description}"
        limit = self.catalog.limits.max_catalog_tokens
        if estimate_tokens(line) <= limit:
            return line
        words = line.split(" ")
        while len(words) > 2 and estimate_tokens(" ".join(words) + "...") > limit:
            words.pop()
        return " ".join(words) + "..."

    def _checked(self, name: str) -> Path:
        entry = self.catalog.entry(name)
        folder = skill_folder(self.root, name)
        if not folder.is_dir():
            raise SkillsError(f"the skill {name} is not installed; run `agentic init`")
        if skill_digest(folder) != entry.sha256:
            raise SkillsError(
                f"the skill {name} changed since it was installed; `agentic init` restores it"
            )
        return folder

    @staticmethod
    def _parts(folder: Path) -> tuple[dict, str]:
        try:
            header, body = split_front_matter(read_text(folder / "SKILL.md"))
            data = load_yaml(header, first_line=2)[0] or {}
        except (OSError, YamlFileError) as error:
            raise SkillsError(f"{folder.name}/SKILL.md cannot be read: {error}") from error
        if not isinstance(data, dict):
            raise SkillsError(f"{folder.name}/SKILL.md front matter is not key: value settings")
        return data, body


def skill_prompt(agent: "ResolvedAgent", library: InstalledSkills, *, preload_all: bool) -> str:
    """The skill block appended to an agent's instructions ("" when it has no skills)."""
    usable = []
    for name in agent.skills:
        if library.available(name):
            usable.append(name)
        else:
            library.record("skills.unavailable", {"skill": name})
    has_tool = "activate_skill" in agent.tools
    inline_all = preload_all or not has_tool
    catalog = library.catalog.skills
    pre = [n for n in usable if inline_all or catalog[n].loading == "pre"][:MAX_PRELOADED]
    rest = [n for n in usable if n not in pre]
    parts = []
    if pre:
        loaded = (library.load(n, list_files=has_tool) for n in pre)
        parts.append("\n\n".join([PRELOADED_HEADER, *loaded]))
    if rest and has_tool:
        parts.append("\n".join([CATALOG_HEADER, *(library.catalog_line(n) for n in rest)]))
    return "\n\n".join([SKILLS_HEADER, *parts]) if parts else ""


class SkillPromptRunner:
    """A step's runner whose agent instructions end with the agent's skill block. A
    resume passes straight through: the paused state already holds the instructions."""

    def __init__(self, runner: "AgentRunner", prompt: str) -> None:
        self.runner, self.prompt = runner, prompt

    async def run(self, task: "AgentTask") -> "AgentOutcome":
        instructions = f"{task.instructions.rstrip()}\n\n{self.prompt}"
        return await self.runner.run(replace(task, instructions=instructions))

    async def resume(
        self, state_path: Path, decisions: dict[str, bool | str], expected_digest: str
    ) -> "AgentOutcome":
        return await self.runner.resume(state_path, decisions, expected_digest)
