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
    .agentic-template.json   the CLI version and template digest the copy came from: a
                             later CLI with another template says so (DF-273)

The template and library are read from `AGENTIC_FRAMEWORK_ROOT`, the framework checkout,
or (an installed wheel) the copy the release bundles into `agentic_cli/_framework/`
(DF-138, DF-188, DF-341).
"""

import hashlib
import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agentic.definitions import DefinitionError, load_agent
from agentic.skills import CatalogError, load_catalog
from agentic.skills.catalog import CATALOG_FILE
from agentic.yamlio import YamlFileError, load_yaml, read_text
from agentic_cli import __version__
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode

FRAMEWORK_ENV = "AGENTIC_FRAMEWORK_ROOT"
STANDALONE_DIR = Path("policy") / "standalone"  # under the agentic home
TEMPLATE = Path("templates") / "standalone"
LIBRARY = Path("library")
_CHECKOUT = Path(__file__).resolve().parents[3]  # cli/src/agentic_cli -> the repo root
#: The template and library a release bundles into the CLI wheel (M3-21,
#: scripts/agentic_release/build.py): an installed CLI has no checkout.
BUNDLED = Path(__file__).resolve().parent / "_framework"
#: In the template's tier models until the person picks real ones (init and preflight say so).
PLACEHOLDER = "change-me"
#: Written into an assembled copy: the CLI version and template digest it came from, so a
#: later CLI can say the copy is from another template (DF-273).
STAMP = ".agentic-template.json"


def framework_root(env: Mapping[str, str] = os.environ) -> Path:
    """Where the framework's `templates/` and `library/` are: AGENTIC_FRAMEWORK_ROOT, else
    the checkout this CLI runs from, else the bundle in the installed wheel. With none of
    them, the checkout path, so the error names what is missing."""
    given = env.get(FRAMEWORK_ENV, "").strip()
    if given:
        return Path(given).expanduser()
    for root in (_CHECKOUT, BUNDLED):
        if (root / TEMPLATE).is_dir():
            return root
    return _CHECKOUT


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
    stamp = {"agentic": __version__, "template": template_digest(root)}
    (dest / STAMP).write_text(json.dumps(stamp, indent=2) + "\n", encoding="utf-8", newline="\n")
    return dest


def template_digest(root: Path) -> str:
    """The content hash of what `assemble` copies from `root` (line endings ignored)."""
    template, library = root / TEMPLATE, root / LIBRARY
    parts = (
        template / "policy",
        template / "README.md",
        template / "skills" / "preload.yaml",
        library / "agents",
        library / "pipelines",
        library / "skills",
    )
    digest = hashlib.sha256()
    for base in parts:
        found = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
        for path in found:
            digest.update(path.relative_to(root).as_posix().encode() + b"\0")
            digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return "sha256:" + digest.hexdigest()


def template_news(installed: Path, root: Path | None = None) -> str | None:
    """What to tell a person whose installed copy came from another template than the
    one this CLI ships (DF-273), or None. Their copy is theirs: never replaced."""
    root = framework_root() if root is None else Path(root)
    if not (root / TEMPLATE).is_dir():
        return None
    try:
        stamp = json.loads((installed / STAMP).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stamp = {}
    if not isinstance(stamp, dict):
        stamp = {}
    if stamp.get("template") == template_digest(root):
        return None
    came = f"agentic {stamp['agentic']}" if stamp.get("agentic") else "an earlier agentic"
    return (
        f"your standalone policy ({installed}) came from {came}; agentic {__version__} ships "
        "a different template (agents, pipelines, skills). init never overwrites your copy "
        "on its own: run `agentic init --standalone --refresh-template` to take the new one "
        "(your policy/ files are kept and the old copy is saved beside it)"
    )


@dataclass(frozen=True)
class Refresh:
    """What `refresh` changed, as paths relative to the copy (POSIX form)."""

    kept: tuple[str, ...]  # the person's policy/ files carried over (theirs differ or are new)
    added: tuple[str, ...]  # in the new template only
    removed: tuple[str, ...]  # in the old copy only (outside policy/: dropped)
    changed: tuple[str, ...]  # outside policy/, different in the new template

    @property
    def template_changed(self) -> bool:
        return bool(self.added or self.removed or self.changed)


def refresh(installed: Path, dest: Path, root: Path | None = None) -> Refresh:
    """Assemble the current template into `dest` (DF-273), then carry the person's
    policy/ files over from `installed` (their tiers, classifications and limits are
    theirs). Everything else is the template's; `installed` is not touched. The caller
    keeps the old copy: edits outside policy/ are in it, and `changed` names them."""
    assemble(dest, root)
    old, new = _contents(installed), _contents(dest)
    kept = []
    for rel, body in old.items():
        if rel.startswith("policy/") and new.get(rel) != body:
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(installed / rel, target)
            kept.append(rel)
    outside = {rel for rel in old.keys() | new.keys() if not rel.startswith("policy/")}
    return Refresh(
        kept=tuple(sorted(kept)),
        added=tuple(sorted(rel for rel in new.keys() - old.keys())),
        removed=tuple(sorted(rel for rel in outside if rel not in new)),
        changed=tuple(sorted(rel for rel in outside if rel in old and old[rel] != new.get(rel))),
    )


def _contents(folder: Path) -> dict[str, str]:
    """Each file under `folder` (but the stamp) by relative path, to a hash of its bytes
    with line endings ignored."""
    found = {}
    for path in folder.rglob("*"):
        rel = path.relative_to(folder).as_posix()
        if path.is_file() and rel != STAMP:
            body = path.read_bytes().replace(b"\r\n", b"\n")
            found[rel] = hashlib.sha256(body).hexdigest()
    return found


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
