"""Agent Skills (`SKILL.md`, agentskills.io): catalog, selection, installer, loader and
admission checks (spec §11, rows M1-37..M1-40)."""

from agentic.skills.catalog import (
    AdmissionLimits,
    Catalog,
    CatalogError,
    PreloadRule,
    SkillEntry,
    SkillsError,
    load_catalog,
    skill_digest,
    skill_folder,
)

__all__ = [
    "AdmissionLimits",
    "Catalog",
    "CatalogError",
    "PreloadRule",
    "SkillEntry",
    "SkillsError",
    "load_catalog",
    "skill_digest",
    "skill_folder",
]
