"""Review pre-pass scanners over Zyroflo's `zyroflo.review` (M2-48; D34: reuse, never rewrite).

Zyroflo's `prepass.prepass` runs gitleaks (the secret gate, matched text redacted) and
ast-grep (its rule set plus the repository's `.agentic/review-rules/`) over a change's
touched files and the diff itself; `sca.scan_one` runs osv-scanner and grades what it
finds. Their documents are the pre-pass shape `agentic.review.prepass` merges and renders.

Two things are the framework's, not Zyroflo's:

  * **Where binaries come from.** `ToolLocator` finds a scanner only in the hash-verified
    tool cache (`ToolFetcher.cached`, M1-51) - never on PATH - and otherwise says why not:
    org policy pins no such tool, has no build for this platform, has no SIGNED Windows
    build (skipped on Windows: no unsigned native binary runs there), or it is pinned but
    not installed. Zyroflo reports that reason as the tool's `not-run` detail.
  * **No dependency list leaves the machine by default.** Zyroflo's osv-scanner call
    queries the OSV service online. Unless policy opts in (`online=True`), the command
    gains `OFFLINE_FLAGS`: the scanner downloads the vulnerability databases and resolves
    nothing through a registry, so no package name is sent anywhere.
  * **The change never configures its own scan.** Zyroflo copies the change's touched files
    into the folder it scans, and gitleaks and osv-scanner read their configuration from
    that folder. The change's `STEERING_FILES` are removed from it (their added lines are
    still scanned, in the diff), gitleaks gets its default rules through
    `GITLEAKS_CONFIG` and osv-scanner an empty `--config`.

Zyroflo looks binaries up in its own vendored folder and builds the scanner commands
itself, so the adapter swaps `prepass.binary` and wraps `prepass.run_tool` and
`prepass.diff_scope` for the length of one call, under a lock, restoring all three even
when the call raises - a shim like those in `sdlc_commons.guards`, until Zyroflo takes a
binary resolver, an offline switch and a trusted scanner configuration (deferred: Zyroflo
PR).
"""

import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from sdlc_commons.tool_fetch import PinLike, windows_refusal

#: osv-scanner without network lookups of the change's packages (see the module).
OFFLINE_FLAGS = ("--offline-vulnerabilities", "--download-offline-databases", "--no-resolve")
#: The repository's own ast-grep rules, beside Zyroflo's defaults.
REPO_RULES = Path(".agentic") / "review-rules"
OSV = "osv-scanner"
#: Files the scanners read from the folder they scan, so a change could steer them: their
#: configuration (an allow-list for every path, ignored advisories) and ignore lists.
STEERING_FILES = frozenset(
    {
        ".gitleaks.toml",
        ".gitleaksignore",
        "osv-scanner.toml",
        ".osv-scanner.toml",
        ".ignore",
        ".rgignore",
        ".gitignore",
        "sgconfig.yml",
        "sgconfig.yaml",
    }
)
#: gitleaks' own default rules, given as its configuration so it never loads a
#: `.gitleaks.toml` from the scanned folder.
GITLEAKS_CONFIG = 'title = "agentic review pre-pass"\n\n[extend]\nuseDefault = true\n'

Locate = Callable[[str], tuple[Path | None, str]]
_SHIM = threading.Lock()


class CachedTools(Protocol):
    def cached(self, name: str, pin: PinLike) -> Path | None: ...


class ToolLocator:
    """Scanners from the verified tool cache only (see the module)."""

    def __init__(
        self, pins: Mapping[str, PinLike], cache: CachedTools | None, platform: str
    ) -> None:
        self.pins, self.cache, self.platform = dict(pins), cache, platform

    def locate(self, name: str) -> tuple[Path | None, str]:
        """(the verified binary, "") or (None, why it cannot run here)."""
        pin = self.pins.get(name)
        if pin is None:
            return None, f"not run: org policy pins no {name} (tool-pins.yaml); none is installed"
        artefact = pin.assets.get(self.platform)
        if artefact is None:
            return None, f"not run: org policy has no {name} build for {self.platform}"
        if windows_refusal(self.platform, artefact):
            return None, (
                f"skipped on Windows: the org manifest has no signed Windows build of {name}, "
                "and no unsigned native binary runs on Windows"
            )
        found = self.cache.cached(name, pin) if self.cache is not None else None
        if found is None:
            return None, (
                f"not run: {name} {pin.version} is not installed, or no longer matches its "
                "pin; run `agentic init` (or `agentic init --install-missing`)"
            )
        return found, ""


@contextmanager
def _zyroflo(locate: Locate, *, offline: bool) -> Iterator[tuple[object, object]]:
    """Zyroflo's prepass and sca modules, with our binaries, trusted scanner configuration
    and (offline) osv-scanner flags for the length of the block; all are put back whatever
    happens."""
    from zyroflo.review import prepass, sca

    with _SHIM, tempfile.TemporaryDirectory(prefix="agentic-scanner-config-") as folder:
        gitleaks_config = Path(folder) / "gitleaks.toml"
        gitleaks_config.write_text(GITLEAKS_CONFIG, encoding="utf-8")
        osv_config = Path(folder) / "osv-scanner.toml"
        osv_config.write_text("", encoding="utf-8")
        binary, run_tool, diff_scope = prepass.binary, prepass.run_tool, prepass.diff_scope

        def trusted_run(cmd: list[str], timeout: int = 300, env=None):
            if cmd[1:2] == ["dir"]:  # gitleaks: the org's rules, never the change's
                env = {**(env or {}), "GITLEAKS_CONFIG": str(gitleaks_config)}
            elif cmd[1:3] == ["scan", "source"]:  # osv-scanner; the scan root stays last
                flags = ["--config", str(osv_config), *(OFFLINE_FLAGS if offline else ())]
                cmd = [*cmd[:-1], *flags, cmd[-1]]
            return run_tool(cmd, timeout=timeout, env=env)

        def scoped(target, diff_text, scratch, exclude=None):
            found = diff_scope(target, diff_text, scratch, exclude)
            _drop_steering(Path(scratch))
            return found

        prepass.binary, prepass.run_tool, prepass.diff_scope = locate, trusted_run, scoped
        try:
            yield prepass, sca
        finally:
            prepass.binary, prepass.run_tool, prepass.diff_scope = binary, run_tool, diff_scope


def _drop_steering(scratch: Path) -> None:
    """Remove the change's own scanner configuration and ignore files from the scan root
    (their added lines are still scanned, in the diff Zyroflo keeps beside them)."""
    for path in sorted(scratch.rglob("*")):
        if path.name in STEERING_FILES and (path.is_file() or path.is_symlink()):
            path.unlink()


def external_checks(
    tree: Path,
    diff_text: str,
    locator: ToolLocator,
    *,
    online: bool = False,
    rule_dirs: Iterable[Path] = (),
) -> dict:
    """gitleaks, ast-grep and osv-scanner over the change in `tree`, as one document."""
    tree = Path(tree)
    rules = [d for d in (tree / REPO_RULES, *rule_dirs) if d.is_dir()]
    with _zyroflo(locator.locate, offline=not online) as (prepass, sca):
        document = prepass.prepass(tree, diff_text, extra_rule_dirs=rules, exclude=[])
        osv_row, osv_sources = _osv(prepass, sca, tree, diff_text, locator, online)
    return {
        "tools": [*document["tools"], osv_row],
        "sources": [*document["sources"], *osv_sources],
    }


def _osv(prepass, sca, tree: Path, diff_text: str, locator: ToolLocator, online: bool):
    mode = "online" if online else "offline"
    exe, why = locator.locate(OSV)
    if exe is None:
        return {"name": OSV, "status": "not-run", "detail": why, "findings": 0, "mode": mode}, []
    with tempfile.TemporaryDirectory(prefix="agentic-sca-") as folder:
        scratch = Path(folder)
        prepass.diff_scope(tree, diff_text, scratch, [])  # the change's touched files
        found = sca.scan_one(exe, scratch, scratch)
    scanned = found["row"]
    row = {
        "name": OSV,
        "status": scanned["status"],
        "detail": scanned.get("detail", ""),
        "findings": scanned.get("findings", 0),
        "scanned": scanned.get("scanned", []),
        "mode": mode,
    }
    if scanned["status"] == "nothing-scanned":
        row["detail"] = "the change touches no dependency manifest osv-scanner reads"
    diagnostics = found["diagnostics"]
    return row, ([{"source": {"name": OSV}, "diagnostics": diagnostics}] if diagnostics else [])


__all__ = ["OFFLINE_FLAGS", "ToolLocator", "external_checks"]
