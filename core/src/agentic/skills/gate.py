"""The policy-repo pull request gate (M2-51, DF-367): `agentic policy check`, run by the
policy repo's CI on every pull request, so a change that would not load, or a skill that
would not be admitted, is caught before a code owner approves it.

It checks a policy repo tree as the control plane's sync will (`load_policy`, and
`agentic.policy.tree.definition_problems`: the org layer, each team's layer at the versions
it gets, its overlays, the rollout and the archives) and adds what only a full checkout can:

  * every skill catalog - the org's `skills/` and each team's `teams/<team>/skills/` -
    goes through catalog admission (licence, `skills-ref validate`, size, scan, pin);
  * the licences and size limits are org policy's (`licences.yaml` `skills:`,
    `limits.yaml` `skills:`), never the catalog's under review: one pull request cannot
    add a GPL skill and also allow GPL in the catalog it changes (DF-143);
  * every catalogued plugin (M2-47) still hashes to its pin.

Each problem is one line, starting with its file relative to the tree root.
"""

from pathlib import Path

from agentic.policy import PolicyError, load_policy
from agentic.policy.tree import definition_problems
from agentic.policy.tree import named as _named
from agentic.skills.admission import admit_catalog
from agentic.skills.catalog import (
    CATALOG_FILE,
    PLUGINS,
    AdmissionLimits,
    Catalog,
    CatalogError,
    SkillsError,
    load_catalog,
    skill_digest,
)
from agentic.skills.plugins import CheckedPlugin, telemetry_problem


def check_policy_repo(root: Path, *, version: str | None = None) -> list[str]:
    """Every problem in the policy repo tree at `root` ([] = it passes the gate)."""
    if not (root / "policy").is_dir():
        return ["policy/: missing - the policy repo keeps its policy files in policy/"]
    try:
        policy = load_policy(root / "policy", version=version)
    except PolicyError as error:
        return _named(root, error)
    teams_root = root / "teams"
    teams = sorted(p for p in teams_root.iterdir() if p.is_dir()) if teams_root.is_dir() else []
    # every team's view, its overlays, the rollout, pins and archives (M2-15, M2-23)
    problems: list[str] = definition_problems(root, policy)
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
        problems += [f"{where}/{problem}" for problem in plugin_problems(catalog_root, catalog)]
    return problems


def plugin_problems(catalog_root: Path, catalog: Catalog) -> list[str]:
    """Each catalogued plugin's problems, relative to the catalog folder: its folder is
    missing, holds a link, does not hash to its pin, or installs a file naming a
    telemetry URL (checked here once, at the pull request, as well as before each step)."""
    problems = []
    for name, entry in catalog.plugins.items():
        folder = catalog_root / PLUGINS / name
        where = f"{PLUGINS}/{name}"
        try:
            found = skill_digest(folder) if folder.is_dir() else None
        except SkillsError as error:
            problems.append(f"{where}: {error}")
            continue
        if found is None:
            problems.append(f"{where}: catalogued, but the folder is missing")
        elif found != entry.sha256:
            problems.append(f"{where}: pinned {entry.sha256}, folder hashes to {found}")
        else:
            plugin = CheckedPlugin(name, entry.version, entry.sha256, folder, tuple(entry.exclude))
            if why := telemetry_problem(plugin):
                problems.append(f"{where}: {why}")
    return problems


__all__ = ["check_policy_repo", "plugin_problems"]
