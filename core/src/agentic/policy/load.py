"""Reading the policy folder into one OrgPolicy.

The folder is the `policy/` directory of the org policy repo (spec §4.3):

    tiers.yaml            tiers, team_overrides
    classifications.yaml  default_classification, classifications
    tools.yaml            catalog, forbidden, deny
    agent-types.yaml      types (caps, ceilings and defaults per agent type, D35)
    limits.yaml           concurrency
    licences.yaml         blocked, scan
    retention.yaml        days                      (placeholder until OR-01)
    tool-pins.yaml        tools (per-platform artefacts `init` fetches from the mirror)
    org.yaml              allowed_repo_patterns, tool_mirror_url, allow_repo_pipelines,
                          index_export_workflow, sandbox, unhonoured, secret_scan
    teams/<team>.yaml     one TeamPolicy per team
"""

import hashlib
import subprocess
from pathlib import Path

from pydantic import ValidationError

from agentic.definitions import DefinitionError
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
    "limits.yaml": {"concurrency": "concurrency"},
    "licences.yaml": {"blocked": "blocked_licences", "scan": "licence_scan"},
    "retention.yaml": {"days": "retention_days"},
    "prices.yaml": {"prices": "prices"},
    "tool-pins.yaml": {"tools": "tool_pins"},
    "org.yaml": {
        "allowed_repo_patterns": "allowed_repo_patterns",
        "tool_mirror_url": "tool_mirror_url",
        "allow_repo_pipelines": "allow_repo_pipelines",
        "index_export_workflow": "index_export_workflow",
        "sandbox": "sandbox",
        "unhonoured": "unhonoured",
        "secret_scan": "secret_scan",
    },
}


def load_policy(root: Path) -> OrgPolicy:
    """Load and validate every policy file under `root`. Raises PolicyError."""
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

    data["version"] = policy_version(root)
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
    bytes, so two different policies never share a version.
    """
    try:
        dirty = _git(root, "status", "--porcelain", "--", ".")
        if dirty == "":
            return f"git:{_git(root, 'rev-parse', 'HEAD')}"
    except (OSError, subprocess.CalledProcessError):
        pass  # not a git repo, or git not installed

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.yaml")):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return f"sha256:{digest.hexdigest()}"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
