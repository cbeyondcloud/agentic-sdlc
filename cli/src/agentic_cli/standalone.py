"""The standalone policy repo `agentic init --standalone` installs (M1-62, spec §24).

With no `--policy`, init uses the framework's own template: `assemble` writes a policy
repo laid out like an org's, which init then installs once into
`~/.agentic/policy/standalone/` (see `policy_source`):

    policy/                  templates/standalone/policy (the person edits tiers.yaml)
    agents/, pipelines/      library/agents, library/pipelines (the org layer)
    skills/                  library/skills, with templates/standalone/skills/preload.yaml's
                             rule written into catalog.yaml (DF-224)
    templates/agents/<type>.md   a starter per library agent type, for `agents new` (DF-217):
                             the type's own agent (qa, not qa-baseline), else the first by name
    README.md                templates/standalone/README.md

The template and library are read from the framework checkout, or from
`AGENTIC_FRAMEWORK_ROOT`. An installed wheel does not ship them yet (DF-138, DF-188):
init then says so and how to proceed.
"""

import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from agentic.definitions import DefinitionError, load_agent
from agentic.skills import CatalogError, load_catalog
from agentic.skills.catalog import CATALOG_FILE
from agentic.yamlio import YamlFileError, load_yaml, read_text
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode

FRAMEWORK_ENV = "AGENTIC_FRAMEWORK_ROOT"
STANDALONE_DIR = Path("policy") / "standalone"  # under the agentic home
TEMPLATE = Path("templates") / "standalone"
LIBRARY = Path("library")
_CHECKOUT = Path(__file__).resolve().parents[3]  # cli/src/agentic_cli -> the repo root
#: In the template's tier models until the person picks real ones (init and preflight say so).
PLACEHOLDER = "change-me"


def framework_root(env: Mapping[str, str] = os.environ) -> Path:
    """Where the framework's `templates/` and `library/` are."""
    given = env.get(FRAMEWORK_ENV, "").strip()
    return Path(given).expanduser() if given else _CHECKOUT


def assemble(dest: Path, root: Path | None = None) -> Path:
    """Write the standalone policy repo into `dest` (a folder that does not exist yet)."""
    root = framework_root() if root is None else Path(root)
    template, library = root / TEMPLATE, root / LIBRARY
    needed = (
        template / "policy",
        template / "skills" / "preload.yaml",
        library / "agents",
        library / "pipelines",
        library / "skills" / CATALOG_FILE,
    )
    missing = [path for path in needed if not path.exists()]
    if missing:
        raise CliError(
            f"the framework's standalone template was not found ({missing[0]} is missing)",
            ExitCode.ERROR,
            hint=f"set {FRAMEWORK_ENV} to a checkout of the framework (the folder holding "
            "templates/ and library/), or pass --policy <your policy repo>",
        )
    dest.mkdir(parents=True)
    shutil.copytree(template / "policy", dest / "policy")
    shutil.copy2(template / "README.md", dest / "README.md")
    for folder in ("agents", "pipelines", "skills"):
        shutil.copytree(library / folder, dest / folder)
    _apply_preload(dest / "skills", template / "skills" / "preload.yaml")
    _agent_templates(dest / "agents", dest / "templates" / "agents")
    return dest


def _apply_preload(skills: Path, rule_file: Path) -> None:
    """Write the template's `preload_for` rule into the catalog (a catalog that already
    names one keeps it), then check the catalog still loads."""
    catalog = skills / CATALOG_FILE
    text = read_text(catalog).replace("\r\n", "\n")
    try:
        data, _ = load_yaml(text)
        rule, _ = load_yaml(read_text(rule_file))
    except YamlFileError as error:
        raise CliError(f"standalone template: {error}", ExitCode.INVALID) from error
    if "preload_for" not in (data or {}):
        found = (rule or {}).get("preload_for") or {}
        lines = [
            "",
            "# Applied by `agentic init --standalone` (templates/standalone/skills/preload.yaml)",
            "preload_for:",
            f"  tiers: {json.dumps(list(found.get('tiers') or []))}",
            f"  models: {json.dumps(list(found.get('models') or []))}",
        ]
        text = text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    catalog.write_text(text, encoding="utf-8", newline="\n")
    try:
        load_catalog(skills)
    except CatalogError as error:
        raise CliError(f"standalone template: {error}", ExitCode.INVALID) from error


def _agent_templates(agents: Path, out: Path) -> None:
    """One starter per agent type: the type's own agent, else the first by name."""
    chosen: dict[str, Path] = {}
    for path in sorted(agents.glob("*.md")):
        try:
            agent = load_agent(path)
        except DefinitionError as error:
            raise CliError(f"standalone template: {error}", ExitCode.INVALID) from error
        if agent.type not in chosen or agent.name == agent.type:
            chosen[agent.type] = path
    out.mkdir(parents=True)
    for agent_type, path in chosen.items():
        shutil.copy2(path, out / f"{agent_type}.md")
