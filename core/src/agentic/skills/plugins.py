"""Agent-CLI plugins as a team opt-in (M2-47, D28; spec §24 "Superpowers").

A team may enable the complete superpowers plugin - its orchestration skills included -
for the steps it delegates to an agent CLI that loads plugins (claude-code, codex). The
native loop never gets it: its skills come from the catalog, which excludes the
orchestration skills (spec §11.4), and no native code path asks for plugins.

`plugins_for(agent, team, catalog, root)` is what a delegated backend installs for one
step. It is empty unless all of these hold:

  * the agent's backend loads plugins (`BackendCapabilities.plugins`: data, so no
    backend is named here) and the plugin does not limit itself to other backends;
  * the team enabled the plugin (`TeamPolicy.backend_plugins`) and the org catalog lists
    it, pinned by sha256 and with a person's script review (`PluginEntry`);
  * its folder, `<catalog>/plugins/<name>/`, still hashes to the pin and holds no link;
  * no installed file names a telemetry or analytics URL. The catalog's `exclude` globs
    take the visual companion out first: its files are neither scanned nor installed.

A plugin that fails a check is left out of the step and recorded
(`skills.plugin_refused`): the step runs without it rather than with an unpinned copy.
`install_plugin` copies a checked plugin, minus the excluded files, into the step's own
config folder, so nothing outside the run sees it.
"""

import fnmatch
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agentic.backends.capabilities import CAPABILITIES
from agentic.skills.admission import telemetry_urls
from agentic.skills.catalog import PLUGINS, Catalog, SkillsError, skill_digest, skill_files
from agentic.skills.install import Record, log_record

if TYPE_CHECKING:
    from agentic.policy import ResolvedAgent, TeamPolicy


@dataclass(frozen=True)
class CheckedPlugin:
    """A plugin verified against its pin, ready to install into one step."""

    name: str
    version: str
    sha256: str
    folder: Path
    exclude: tuple[str, ...] = ()

    def files(self) -> list[tuple[str, Path]]:
        """(POSIX relative path, file) of every file a step gets."""
        found, _ = skill_files(self.folder)
        rows = [(path.relative_to(self.folder).as_posix(), path) for path in found]
        return [(rel, path) for rel, path in rows if not excluded(rel, self.exclude)]


def excluded(relative: str, globs: tuple[str, ...] | list[str]) -> bool:
    """Whether a file is left out by an `exclude` glob (`dir/**` covers the folder)."""
    for glob in globs:
        if fnmatch.fnmatchcase(relative, glob):
            return True
        if glob.endswith("/**") and (relative + "/").startswith(glob[:-2]):
            return True
    return False


def plugins_for(
    agent: "ResolvedAgent",
    team: "TeamPolicy | None",
    catalog: Catalog,
    root: Path,
    *,
    record: Record | None = None,
) -> list[CheckedPlugin]:
    """The plugins one step of `agent` gets ([] for the native loop, or with the flag off)."""
    caps = CAPABILITIES.get(agent.backend)
    if caps is None or not caps.plugins or team is None:
        return []
    record = record or log_record
    found = []
    for name in team.backend_plugins:
        entry = catalog.plugins.get(name)
        if entry is None:
            _refused(record, name, agent.backend, "not in the org catalog")
            continue
        if entry.backends and agent.backend not in entry.backends:
            continue
        folder = root / PLUGINS / name
        try:
            if not folder.is_dir() or folder.is_symlink() or folder.is_junction():
                raise SkillsError(f"no plugin folder at {folder}")
            digest = skill_digest(folder)
        except SkillsError as error:
            _refused(record, name, agent.backend, str(error))
            continue
        if digest != entry.sha256:
            why = f"its folder hash {digest} is not the pinned {entry.sha256}"
            _refused(record, name, agent.backend, why)
            continue
        plugin = CheckedPlugin(name, entry.version, entry.sha256, folder, tuple(entry.exclude))
        if why := telemetry_problem(plugin):
            _refused(record, name, agent.backend, why)
            continue
        record(
            "skills.plugin_installed",
            {"plugin": name, "version": entry.version, "sha256": entry.sha256},
        )
        found.append(plugin)
    return found


def install_plugin(plugin: CheckedPlugin, dest_root: Path) -> Path:
    """Copy `plugin` (without its excluded files) to `<dest_root>/<name>/`, replacing any
    copy a retried step left there; the installed folder."""
    target = dest_root / plugin.name
    if target.exists():
        shutil.rmtree(target)
    for relative, path in plugin.files():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    return target


def telemetry_problem(plugin: CheckedPlugin) -> str | None:
    """The first telemetry or analytics URL in a file the plugin installs, or None."""
    for relative, path in plugin.files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if urls := telemetry_urls(line):
                return f"{relative}:{number} names a telemetry or analytics URL ({urls[0]})"
    return None


def _refused(record: Record, name: str, backend: str, reason: str) -> None:
    record("skills.plugin_refused", {"plugin": name, "backend": backend, "reason": reason})


__all__ = ["CheckedPlugin", "excluded", "install_plugin", "plugins_for", "telemetry_problem"]
