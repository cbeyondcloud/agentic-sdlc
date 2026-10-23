"""The policy repo's definitions checked as a whole (M2-15, M2-23): the check both pull
request gates run - `agentic policy check` (`agentic.skills.gate`) and the control plane's
policy sync (`agentic_cp.policy_sync.validate`) - so a commit they accept is one every
team's laptops can load, in every repository with an overlay.

    the org view           the org layer, every rollout held back (no team is in any
                           rollout), so each rollout's `previous` version must exist
    each team's view       its layer and pins, the versions it gets
    each overlay           applied to that team's view of the pipelines it names
    rollout entries        name an org definition whose current file is `version`
    archives               `<layer>/agents|pipelines/versions/<name>/<version>.*` only,
                           each saying the name and version of its path

Each problem is one line naming its file relative to the tree root, once."""

from datetime import UTC, date, datetime
from pathlib import Path

from agentic.definitions import DefinitionError, DefinitionSet
from agentic.definitions.discovery import load_version
from agentic.definitions.overlay import OVERLAYS, load_overlay
from agentic.definitions.versions import ARCHIVE, FOLDERS
from agentic.policy.model import OrgPolicy

#: The repo key an overlay without `repo:` is checked under: only its stem matters.
_CHECK_HOST = "policy-check"


def named(root: Path, error: DefinitionError) -> list[str]:
    """`error`'s lines, each prefixed with its file relative to `root`."""
    try:
        where = Path(error.path).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        where = Path(error.path).name
    return [f"{where}: {line}" for line in error.errors] or [f"{where}: invalid"]


def definition_problems(root: Path, policy: OrgPolicy, today: date | None = None) -> list[str]:
    """Every problem in the definitions of the policy repo at `root` ([] = none)."""
    today = today if today is not None else datetime.now(UTC).date()
    problems: list[str] = []

    def discover(team_dir: Path | None, team: str | None, repo: str | None, rules: bool = True):
        try:
            return DefinitionSet.discover(
                org=root,
                team=team_dir,
                repo=None,
                allow_repo_pipelines=policy.allow_repo_pipelines,
                rules=policy.rules(team=team, repo=repo, today=today) if rules else None,
            )
        except DefinitionError as error:
            problems.extend(named(root, error))
            return None

    plain = discover(None, None, None, rules=False)
    if plain is not None:
        problems.extend(_rollout_problems(policy, plain))
    discover(None, None, None)  # every rollout held back: each `previous` must exist
    teams_root = root / "teams"
    folders = {p.name for p in teams_root.iterdir() if p.is_dir()} if teams_root.is_dir() else set()
    layers = [root]
    for team in sorted(folders | set(policy.teams)):
        team_dir = teams_root / team if team in folders else None
        discover(team_dir, team, None)
        if team_dir is None:
            continue
        layers.append(team_dir)
        for path in _overlay_files(root, team_dir / OVERLAYS, problems):
            try:
                repo = load_overlay(path).repo or f"{_CHECK_HOST}/{team}/{path.stem}"
            except DefinitionError as error:
                problems.extend(named(root, error))
                continue
            discover(team_dir, team, repo)
    for layer in layers:
        problems.extend(_archive_problems(root, layer))
    return list(dict.fromkeys(problems))


def _rollout_problems(policy: OrgPolicy, plain: DefinitionSet) -> list[str]:
    """Each rollout entry names an org definition whose current file is its `version`."""
    found = []
    for kind, entries, table in (
        ("agent", policy.rollout.agents, plain.agents),
        ("pipeline", policy.rollout.pipelines, plain.pipelines),
    ):
        folder, suffix = FOLDERS[kind]
        for name, entry in entries.items():
            where = f"policy/rollout.yaml: rollout.{folder}.{name}"
            current = table.get(name)
            if current is None or current[1] != "org":
                found.append(f"{where}: no org {kind} named {name!r}")
            elif current[0].version != entry.version:
                found.append(
                    f"{where}: version {entry.version} is not the current "
                    f"{folder}/{name}{suffix} ({current[0].version})"
                )
    return found


def _overlay_files(root: Path, folder: Path, problems: list[str]) -> list[Path]:
    if not folder.is_dir():
        return []
    entries = sorted(entry for entry in folder.iterdir() if not entry.name.startswith("."))
    stray = [entry for entry in entries if not (entry.is_file() and entry.suffix == ".yaml")]
    if stray:
        rel = folder.relative_to(root).as_posix()
        names = ", ".join(entry.name for entry in stray)
        problems.append(f"{rel}: unexpected entries (only *.yaml files): {names}")
    return [entry for entry in entries if entry not in stray]


def _archive_problems(root: Path, layer: Path) -> list[str]:
    """`versions/` holds `<name>/<version><suffix>` files only, each loading as that
    name and version."""
    found = []
    for kind, (folder, suffix) in FOLDERS.items():
        archive = layer / folder / ARCHIVE
        if not archive.is_dir():
            continue
        for entry in _visible(archive):
            rel = entry.relative_to(root).as_posix()
            if not entry.is_dir():
                found.append(
                    f"{rel}: unexpected entry (the archive holds <name>/<version>{suffix})"
                )
                continue
            for path in _visible(entry):
                rel = path.relative_to(root).as_posix()
                if not (path.is_file() and path.name.endswith(suffix)):
                    found.append(f"{rel}: unexpected entry (only <version>{suffix} files)")
                    continue
                try:
                    load_version(kind, entry.name, path.name.removesuffix(suffix), path)
                except DefinitionError as error:
                    found.extend(named(root, error))
    return found


def _visible(folder: Path) -> list[Path]:
    return sorted(entry for entry in folder.iterdir() if not entry.name.startswith("."))
