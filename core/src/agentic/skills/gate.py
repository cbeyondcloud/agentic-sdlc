"""The policy-repo pull request gate (M2-51, DF-367): `agentic policy check`, run by the
policy repo's CI on every pull request, so a change that would not load, or a skill that
would not be admitted, is caught before a code owner approves it.

It checks a policy repo tree as the control plane's sync will (`load_policy`, the org
layer and each team's layer of definitions) and adds what only a full checkout can:

  * every skill catalog - the org's `skills/` and each team's `teams/<team>/skills/` -
    goes through catalog admission (licence, `skills-ref validate`, size, scan, pin);
  * the licences and size limits are org policy's (`licences.yaml` `skills:`,
    `limits.yaml` `skills:`), never the catalog's under review: one pull request cannot
    add a GPL skill and also allow GPL in the catalog it changes (DF-143);
  * every catalogued plugin (M2-47) still hashes to its pin.

Each problem is one line, starting with its file relative to the tree root.
"""

from pathlib import Path

from agentic.definitions import DefinitionError, DefinitionSet
from agentic.policy import PolicyError, load_policy
from agentic.skills.admission import admit_catalog
from agentic.skills.catalog import (
    CATALOG_FILE,
    PLUGINS,
    AdmissionLimits,
    CatalogError,
    SkillsError,
    load_catalog,
    skill_digest,
)


def check_policy_repo(root: Path, *, version: str | None = None) -> list[str]:
    """Every problem in the policy repo tree at `root` ([] = it passes the gate)."""
    if not (root / "policy").is_dir():
        return ["policy/: missing - the policy repo keeps its policy files in policy/"]
    try:
        policy = load_policy(root / "policy", version=version)
    except PolicyError as error:
        return _named(root, error)
    problems: list[str] = []
    teams_root = root / "teams"
    teams = sorted(p for p in teams_root.iterdir() if p.is_dir()) if teams_root.is_dir() else []
    for team in [None, *teams]:
        try:
            DefinitionSet.discover(
                org=root, team=team, repo=None, allow_repo_pipelines=policy.allow_repo_pipelines
            )
        except DefinitionError as error:
            problems += _named(root, error)
    limits = AdmissionLimits(**policy.skill_limits.given())
    for catalog_root in [root / "skills", *(team / "skills" for team in teams)]:
        if not (catalog_root / CATALOG_FILE).is_file():
            continue
        where = catalog_root.relative_to(root).as_posix()
        try:
            results = admit_catalog(catalog_root, licences=policy.skill_licences, limits=limits)
            catalog = load_catalog(catalog_root)
        except CatalogError as error:
            problems += _named(root, error)
            continue
        for key, rejections in results.items():
            problems += [f"{where}/{key}: {r.check} {r.path}: {r.reason}" for r in rejections]
        for name, entry in catalog.plugins.items():
            folder = catalog_root / PLUGINS / name
            try:
                found = skill_digest(folder) if folder.is_dir() else None
            except SkillsError as error:
                problems.append(f"{where}/{PLUGINS}/{name}: {error}")
                continue
            if found is None:
                problems.append(f"{where}/{PLUGINS}/{name}: catalogued, but the folder is missing")
            elif found != entry.sha256:
                problems.append(
                    f"{where}/{PLUGINS}/{name}: pinned {entry.sha256}, folder hashes to {found}"
                )
    return problems


def _named(root: Path, error: DefinitionError) -> list[str]:
    """`error`'s lines, each prefixed with its file relative to the tree root."""
    try:
        where = Path(error.path).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        where = Path(error.path).name
    return [f"{where}: {line}" for line in error.errors] or [f"{where}: invalid"]


__all__ = ["check_policy_repo"]
