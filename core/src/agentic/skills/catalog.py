"""The skills catalog: every approved skill, pinned by content hash (spec §11.3).

The catalog is `catalog.yaml` in a catalog folder - the policy repo's `skills/`
folder, or `library/skills/` for the shipped defaults. Each skill sits next to it in
`<source>/<name>/` (an Agent Skills folder with a `SKILL.md`), and its key is
`<source>/<name>`, the way agent files name skills:

    skills:
      superpowers/test-driven-development:
        source: https://github.com/obra/superpowers
        commit: 8ca22dba...            # the upstream commit it was taken from
        path: skills/test-driven-development
        licence: MIT
        owner: platform-team
        version: 1.0.0
        sha256: sha256:<64 hex>       # skill_digest() of the folder
        loading: pre                  # pre (in context at start) | on-demand
        audience: agents              # agents | admins (authoring skills)
        pack: null                    # an opt-in pack name: installed only if enabled
    excluded:                         # names that may never be catalogued, with why
      using-superpowers: duplicates the orchestrator
    preload_for: {tiers: [], models: []}   # weak/local models get skills pre-loaded
    plugins:                          # agent-CLI plugins a team may opt into (M2-47)
      superpowers:                    # folder: plugins/superpowers/
        source: https://github.com/obra/superpowers
        commit: ...
        licence: MIT
        owner: platform-team
        version: 5.0.0
        sha256: sha256:<64 hex>
        scripts_reviewed_by: security-team
        exclude: [skills/brainstorming/visual-companion/**]
"""

import fnmatch
import hashlib
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agentic.definitions import DefinitionError
from agentic.yamlio import YamlFileError, load_yaml, read_text

CATALOG_FILE = "catalog.yaml"
PLUGINS = "plugins"  # `<catalog>/plugins/<name>/`: agent-CLI plugins, not skills (M2-47)
_SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
KEY = re.compile(rf"^{_SLUG}/{_SLUG}$")
DEFAULT_LICENCES = ["MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "internal"]


class SkillsError(Exception):
    """A skill cannot be installed or loaded as the catalog pins it."""


class CatalogError(DefinitionError):
    """The catalog file could not be loaded or is inconsistent."""

    kind = "skills catalog"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SkillEntry(_Strict):
    """One approved skill: where it came from, under what licence, and its pinned hash."""

    source: str  # upstream repository URL, or "internal" for an org-written skill
    commit: str  # upstream commit (or the policy-repo commit for an internal skill)
    path: str  # the skill's folder in the source repository
    licence: str
    owner: str
    version: str
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    loading: Literal["pre", "on-demand"] = "on-demand"
    audience: Literal["agents", "admins"] = "agents"  # admins: authoring skills
    pack: str | None = None  # an opt-in pack: installed only when a team enables it
    # a person who reviewed its scripts (§11.3); never blank, which would waive the review
    scripts_reviewed_by: str | None = Field(default=None, pattern=r"^.*\S.*$")
    notes: str = ""  # what was changed from upstream, if anything


class PluginEntry(_Strict):
    """One approved agent-CLI plugin (M2-47, D28): installed whole, only into the step
    config of a backend that loads plugins (claude-code, codex), never into the native
    loop. Its folder is `<catalog>/plugins/<name>/`, pinned by `sha256`. A plugin carries
    scripts and hooks, so a person must have reviewed them (decision 54)."""

    source: str
    commit: str
    licence: str
    owner: str
    version: str
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scripts_reviewed_by: str = Field(pattern=r"^.*\S.*$")
    backends: list[str] = []  # empty: every backend that loads plugins
    # POSIX globs of files never installed (the visual companion, its telemetry)
    exclude: list[str] = []
    notes: str = ""


class PreloadRule(_Strict):
    """Which agents get their skills pre-loaded instead of discovered: weaker or local
    models, named by tier or by model glob (spec §11.1). Empty: none."""

    tiers: list[str] = []
    models: list[str] = []

    def matches(self, tier: str, model: str) -> bool:
        return tier in self.tiers or any(fnmatch.fnmatchcase(model, glob) for glob in self.models)


class AdmissionLimits(_Strict):
    max_skill_tokens: int = Field(default=8000, ge=1)  # SKILL.md, estimated
    max_total_bytes: int = Field(default=262_144, ge=1)  # the whole folder
    max_catalog_tokens: int = Field(default=100, ge=1)  # one catalog line in the context


class Catalog(_Strict):
    skills: dict[str, SkillEntry] = {}
    excluded: dict[str, str] = {}  # skill name or key -> why it may never be catalogued
    licences: list[str] = DEFAULT_LICENCES  # licences a skill may carry
    preload_for: PreloadRule = PreloadRule()
    limits: AdmissionLimits = AdmissionLimits()
    plugins: dict[str, PluginEntry] = {}  # agent-CLI plugins a team may opt into (M2-47)

    @model_validator(mode="after")
    def keys_are_valid(self) -> Self:
        problems = [
            f"plugin name {name!r} must be lowercase-with-hyphens"
            for name in self.plugins
            if not re.fullmatch(_SLUG, name)
        ]
        for key in self.skills:
            if not KEY.match(key):
                problems.append(f"key {key!r} must be '<source>/<name>' in lowercase-with-hyphens")
            elif key.split("/")[0] == PLUGINS:
                problems.append(f"{key}: '{PLUGINS}/' holds plugins, not a skill source")
            elif key in self.excluded or key.split("/")[1] in self.excluded:
                why = self.excluded.get(key) or self.excluded[key.split("/")[1]]
                problems.append(f"{key} is excluded and may not be catalogued: {why}")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def entry(self, key: str) -> SkillEntry:
        if key not in self.skills:
            raise SkillsError(f"{key!r} is not in the skills catalog")
        return self.skills[key]


def load_catalog(root: Path) -> Catalog:
    """Read and validate `<root>/catalog.yaml`. Raises CatalogError."""
    path = root / CATALOG_FILE
    if not path.is_file():
        raise CatalogError(path, [f"no {CATALOG_FILE} in {root}"])
    try:
        data, lines = load_yaml(read_text(path))
    except YamlFileError as error:
        raise CatalogError(path, [str(error)]) from error
    try:
        return Catalog.model_validate(data or {})
    except ValidationError as error:
        raise CatalogError.from_validation_error(path, error, "catalog", lines=lines) from error


def skill_folder(root: Path, key: str) -> Path:
    if not KEY.match(key):
        raise SkillsError(f"not a skill key: {key!r}")
    source, name = key.split("/")
    return root / source / name


def skill_digest(folder: Path, ignore: Callable[[str], bool] | None = None) -> str:
    """The content hash of a skill folder: every file's POSIX relative path and bytes,
    sorted by path, each length-prefixed so two layouts can never feed the same bytes
    (DF-140). CRLF counts as LF, so Windows and Linux checkouts agree. `ignore` (given a
    file's POSIX relative path) leaves files out of the hash.

    A link (symlink or Windows junction) anywhere in the folder is refused: one OS
    would follow it and another skip it, and either way it reaches outside the skill."""
    files, links = skill_files(folder)
    if links:
        names = ", ".join(link.relative_to(folder).as_posix() for link in links)
        raise SkillsError(f"{folder}: skills hold plain files, not links ({names})")
    if ignore is not None:
        files = [p for p in files if not ignore(p.relative_to(folder).as_posix())]
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda p: p.relative_to(folder).as_posix()):
        name = path.relative_to(folder).as_posix().encode("utf-8")
        content = path.read_bytes().replace(b"\r\n", b"\n")
        for part in (name, content):
            digest.update(len(part).to_bytes(8, "big") + part)
    return f"sha256:{digest.hexdigest()}"


def skill_files(folder: Path) -> tuple[list[Path], list[Path]]:
    """(files, links) below `folder`, sorted, never following a link: a symlink or a
    junction is reported as a link and not entered (Path.rglob enters a junction)."""
    files: list[Path] = []
    links: list[Path] = []
    pending = [folder]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink() or entry.is_junction():
                    links.append(path)
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    files.append(path)
    return sorted(files), sorted(links)
