"""Reading the policy folder into one OrgPolicy.

The folder is the `policy/` directory of the org policy repo (spec §4.3):

    tiers.yaml            tiers, team_overrides
    classifications.yaml  default_classification, classifications
    tools.yaml            catalog, forbidden, deny
    agent-types.yaml      types (caps, ceilings and defaults per agent type, D35)
    limits.yaml           concurrency, skills (skill admission limits)
    licences.yaml         blocked, scan, skills (licences a catalogued skill may carry)
    retention.yaml        days                      (placeholder until OR-01)
    tool-pins.yaml        tools (per-platform artefacts `init` fetches from the mirror)
    org.yaml              allowed_repo_patterns, tool_mirror_url, allow_repo_pipelines,
                          required_steps, checkpoint_exclude, index_export_workflow,
                          sandbox, unhonoured,
                          secret_scan, publish_scan, review_prepass, review_batch
    eval.yaml             eval                      (agent evaluation defaults, M1-63)
    rollout.yaml          rollout, deprecated       (versions team by team, M2-15)
    teams/<team>.yaml     one TeamPolicy per team
"""

import hashlib
import re
import subprocess
from pathlib import Path

from pydantic import ValidationError

from agentic.definitions import DefinitionError
from agentic.definitions.overlay import OVERLAYS, load_overlay
from agentic.definitions.pipeline import load_pipeline
from agentic.definitions.versions import ARCHIVE
from agentic.policy.model import OrgPolicy
from agentic.yamlio import YamlFileError, load_yaml, read_text


class PolicyError(DefinitionError):
    """The policy folder could not be loaded or is inconsistent."""

    kind = "policy"


# file name -> {key in that file: OrgPolicy field}
FILE_FIELDS: dict[str, dict[str, str]] = {
    "tiers.yaml": {"tiers": "tiers", "team_overrides": "team_overrides"},
    "classifications.yaml": {
        "default_classification": "default_classification",
        "classifications": "classifications",
    },
    "tools.yaml": {"catalog": "tools_catalog", "forbidden": "forbidden_tools", "deny": "deny"},
    "agent-types.yaml": {"types": "agent_types"},
    "limits.yaml": {"concurrency": "concurrency", "skills": "skill_limits"},
    "licences.yaml": {
        "blocked": "blocked_licences",
        "scan": "licence_scan",
        "skills": "skill_licences",
    },
    "retention.yaml": {"days": "retention_days"},
    "prices.yaml": {"prices": "prices"},
    "tool-pins.yaml": {"tools": "tool_pins"},
    "org.yaml": {
        "allowed_repo_patterns": "allowed_repo_patterns",
        "tool_mirror_url": "tool_mirror_url",
        "allow_repo_pipelines": "allow_repo_pipelines",
        "required_steps": "required_steps",
        "checkpoint_exclude": "checkpoint_exclude",
        "min_cli_version": "min_cli_version",
        "index_export_workflow": "index_export_workflow",
        "sandbox": "sandbox",
        "unhonoured": "unhonoured",
        "secret_scan": "secret_scan",
        "publish_scan": "publish_scan",
        "review_prepass": "review_prepass",
        "review_batch": "review_batch",
    },
    "eval.yaml": {"eval": "eval"},
    "rollout.yaml": {"rollout": "rollout", "deprecated": "deprecated"},
}


#: A version id the run ledger accepts (`sdlc_commons.ledger.FIELDS["policy_version"]`): a
#: git commit (SHA-1, or SHA-256's 64 hex) or a content hash.
VERSION_PATTERN = re.compile(r"git:[0-9a-f]{40}(?:[0-9a-f]{24})?|sha256:[0-9a-f]{64}")


def load_policy(root: Path, *, version: str | None = None) -> OrgPolicy:
    """Load and validate every policy file under `root`. Raises PolicyError.

    `version` is the id a caller already knows - the commit the control plane validated,
    for a policy extracted from its bundle (SP15) - instead of the one computed from the
    folder; it must have the ledger's shape."""
    if version is not None and not VERSION_PATTERN.fullmatch(version):
        raise PolicyError(root, ["the policy version must be git:<commit> or sha256:<digest>"])
    problems: list[str] = []
    data: dict[str, object] = {"teams": {}}

    # Look at EVERY entry, not just *.yaml: a policy file saved as tools.yml or a team
    # file as payments.yml must be an error, never silently ignored (it would loosen
    # policy). Hidden files such as .gitkeep are skipped.
    for path in _visible_entries(root):
        if path.is_dir():
            if path.name != "teams":
                problems.append(f"{path.name}/: unexpected folder (only teams/ is allowed)")
            continue
        if path.name not in FILE_FIELDS:
            problems.append(
                f"{path.name}: unknown policy file (expected one of: {', '.join(FILE_FIELDS)})"
            )
            continue
        content = _read_yaml(path, problems)
        for key, value in content.items():
            field = FILE_FIELDS[path.name].get(key)
            if field is None:
                problems.append(f"{path.name}: unknown setting '{key}'")
            else:
                data[field] = value

    for path in _visible_entries(root / "teams"):
        if path.is_file() and path.suffix == ".yaml":
            data["teams"][path.stem] = _read_yaml(path, problems)  # type: ignore[index]
        else:
            problems.append(f"teams/{path.name}: unexpected entry (team files are <team>.yaml)")

    if problems:
        raise PolicyError(root, problems)

    data["version"] = version if version is not None else policy_version(root)
    try:
        return OrgPolicy.model_validate(data)
    except ValidationError as error:
        raise PolicyError.from_validation_error(root, error, "policy") from error


def _visible_entries(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(entry for entry in folder.iterdir() if not entry.name.startswith("."))


def _read_yaml(path: Path, problems: list[str]) -> dict:
    try:
        content = load_yaml(read_text(path))[0] or {}
    except YamlFileError as error:
        problems.append(f"{path.name}: {error}")
        return {}
    if not isinstance(content, dict):
        problems.append(f"{path.name}: must contain key: value settings")
        return {}
    return content


def policy_version(root: Path) -> str:
    """Identify exactly which policy a run used.

    `git:<commit>` when the folder is inside a git repo with no uncommitted
    changes under it; otherwise `sha256:<digest>` of the policy files' names and
    text, so two different policies never share a version. Only the files
    `load_policy` reads are hashed, as LF text without a byte-order mark: a Windows
    checkout (CRLF, an editor's BOM) gets the version a Linux or macOS one gets (DF-02).
    """
    try:
        dirty = _git(root, "status", "--porcelain", "--", ".")
        if dirty == "":
            return f"git:{_git(root, 'rev-parse', 'HEAD')}"
    except (OSError, subprocess.CalledProcessError):
        pass  # not a git repo, or git not installed

    files = [p for p in _visible_entries(root) if p.is_file() and p.name in FILE_FIELDS]
    files += [p for p in _visible_entries(root / "teams") if p.is_file() and p.suffix == ".yaml"]
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
        text = path.read_bytes().removeprefix(b"\xef\xbb\xbf")
        text = text.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(text + b"\0")
    return f"sha256:{digest.hexdigest()}"


def tree_digest(root: Path) -> str:
    """`sha256:` over every file under `root` (a policy repo tree), `.git` skipped: for each
    file in POSIX-path order, the length-prefixed path then the length-prefixed bytes,
    exactly as stored (DF-140: without the lengths, `a`+`bc` and `ab`+`c` would collide).

    The control plane computes it over the tree it validated and the CLI over the copy it
    extracted, so a locally edited cache is found; the bytes are not normalised, because the
    bundle carries LF text on every platform."""
    digest = hashlib.sha256()
    files = sorted(
        (path.relative_to(root).as_posix(), path)
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    )
    for rel, path in files:
        name, data = rel.encode("utf-8"), path.read_bytes()
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(data).to_bytes(8, "big") + data)
    return f"sha256:{digest.hexdigest()}"


def pipeline_digests(root: Path) -> dict[str, str]:
    """The approved pipelines of a policy repo tree (D33): POSIX path -> `sha256:` of the
    file's bytes exactly as stored, for `pipelines/*.yaml` and `teams/*/pipelines/*.yaml`,
    and their archived versions `.../pipelines/versions/<name>/<version>.yaml` (M2-15).

    The control plane records it for each version it validates and the CLI for the bundle
    it extracted (the same bytes on every platform, as for `tree_digest`), so a run can be
    refused when its pipeline is not byte for byte the approved one."""
    found = {}
    for folder in _pipeline_folders(root):
        archive = folder / ARCHIVE
        archived = (
            [p for name in _sorted_dirs(archive) for p in sorted(name.iterdir())]
            if archive.is_dir()
            else []
        )
        for path in [*sorted(folder.iterdir()), *archived]:
            if path.is_file() and path.suffix == ".yaml":
                found[path.relative_to(root).as_posix()] = _sha256(path)
    return found


def overlay_index(root: Path) -> dict[str, dict]:
    """The approved overlays of a policy repo tree (M2-23, D33): POSIX path of each
    `teams/<team>/pipelines/overlays/*.yaml` -> its `sha256` (bytes as stored), `team`, the
    `repo` it binds (None: every repository of its file name) and the `pipelines` it
    changes. A file that does not load is left out: the tree's validation rejects it."""
    found: dict[str, dict] = {}
    teams = root / "teams"
    for team in _sorted_dirs(teams) if teams.is_dir() else []:
        folder = team / OVERLAYS
        for path in sorted(folder.iterdir()) if folder.is_dir() else []:
            if not (path.is_file() and path.suffix == ".yaml"):
                continue
            try:
                overlay = load_overlay(path)
            except DefinitionError:
                continue
            found[path.relative_to(root).as_posix()] = {
                "sha256": _sha256(path),
                "team": team.name,
                "repo": overlay.repo,
                "pipelines": sorted(overlay.pipelines),
            }
    return found


def version_index(root: Path) -> dict[str, dict]:
    """What run registration needs to judge a run's versions without the bundle (M2-15):
    `files`, POSIX path of each current pipeline file -> the version it declares (so the
    control plane reads the version of the approved file itself), and `team_owned`, team ->
    the `agents` and `pipelines` its own layer defines (the org rollout never holds those
    back). A file that does not load is left out: the tree's validation rejects it."""
    files: dict[str, str] = {}
    owned: dict[str, dict[str, list[str]]] = {}
    for folder in _pipeline_folders(root):
        team = folder.parent.name if folder.parent.parent == root / "teams" else None
        for path in sorted(folder.iterdir()):
            if not (path.is_file() and path.suffix == ".yaml"):
                continue
            try:
                pipeline = load_pipeline(path)
            except DefinitionError:
                continue
            files[path.relative_to(root).as_posix()] = pipeline.version
            if team is not None:
                owned.setdefault(team, {"agents": [], "pipelines": []})["pipelines"].append(
                    pipeline.name
                )
    teams = root / "teams"
    for team_dir in _sorted_dirs(teams) if teams.is_dir() else []:
        # an agent file's stem is its name (discovery refuses any other, DF-03)
        agents = sorted(p.stem for p in (team_dir / "agents").glob("*.md") if p.is_file())
        if agents:
            owned.setdefault(team_dir.name, {"agents": [], "pipelines": []})["agents"] = agents
    return {"files": files, "team_owned": dict(sorted(owned.items()))}


def _pipeline_folders(root: Path) -> list[Path]:
    folders = [root / "pipelines"]
    teams = root / "teams"
    if teams.is_dir():
        folders += [team / "pipelines" for team in _sorted_dirs(teams)]
    return [folder for folder in folders if folder.is_dir()]


def _sorted_dirs(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_dir())


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
