"""Validating a policy repo tree with the same core code the CLI runs, so a commit the control
plane accepts is one every laptop can load.

The tree is the policy repo at one commit, extracted from `git archive`:

    policy/            the org policy (`agentic.policy.load_policy`)
    agents/, pipelines/  the org layer of definitions
    teams/<team>/      each team's layer at the versions it gets, with its per-repo
                       overlays (`agentic.policy.tree.definition_problems`, M2-15, M2-23),
                       and its own skills catalog `teams/<team>/skills/` when present (M2-51)
    skills/            the skills catalog, when present (`agentic.skills.load_catalog`)

The full admission of every catalogued skill (licence, format, size, scan, pins) is the
policy repo's pull request gate, `agentic policy check`, which needs the skill files.

Each problem is one line naming the file, relative to the tree root."""

from __future__ import annotations

from pathlib import Path

from agentic.definitions import DefinitionError
from agentic.policy import OrgPolicy, PolicyError, load_policy
from agentic.policy.tree import definition_problems
from agentic.skills import CatalogError, load_catalog

#: Problems kept per version (an audit record carries at most AUDITED of them).
MAX_PROBLEMS = 200
AUDITED = 50


def validate_policy_tree(root: Path, version: str) -> tuple[list[str], OrgPolicy | None]:
    """The problems in the tree ([] = valid) and the loaded policy when it loads."""
    problems: list[str] = []
    if not (root / "policy").is_dir():
        return ["policy/: missing - the policy repo keeps its policy files in policy/"], None
    try:
        policy = load_policy(root / "policy", version=version)
    except PolicyError as error:
        return _named(root, error), None
    # every team's view at the versions it gets, its overlays, the rollout, pins and
    # archives (M2-15, M2-23): the same check as `agentic policy check`
    problems += definition_problems(root, policy)
    teams = root / "teams"
    team_dirs = sorted(p for p in teams.iterdir() if p.is_dir()) if teams.is_dir() else []
    for catalog in [root / "skills", *(team / "skills" for team in team_dirs)]:
        if not (catalog / "catalog.yaml").is_file() and catalog != root / "skills":
            continue  # a team without skills of its own
        if catalog.is_dir():
            try:
                load_catalog(catalog)
            except CatalogError as error:
                problems += _named(root, error)
    return problems[:MAX_PROBLEMS], policy


def _named(root: Path, error: DefinitionError) -> list[str]:
    """`error`'s lines, each prefixed with its file relative to the tree root."""
    try:
        where = Path(error.path).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        where = Path(error.path).name
    return [f"{where}: {line}" for line in error.errors] or [f"{where}: invalid"]
