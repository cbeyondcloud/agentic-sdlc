"""Review pre-pass scanners over Zyroflo's `tools.review` (M2-48; D34: reuse, never rewrite).

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

Zyroflo looks binaries up in its own vendored folder and builds the osv-scanner command
itself, so the adapter swaps `prepass.binary` and wraps `prepass.run_tool` for the length
of one call, under a lock, restoring both even when the call raises - a shim like those in
`sdlc_commons.guards`, until Zyroflo takes a binary resolver and an offline switch
(deferred: Zyroflo PR).
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
    """Zyroflo's prepass and sca modules, with our binaries and (offline) osv-scanner
    flags for the length of the block; both are put back whatever happens."""
    from tools.review import prepass, sca

    with _SHIM:
        binary, run_tool = prepass.binary, prepass.run_tool

        def offline_run(cmd: list[str], timeout: int = 300, env=None):
            if offline and cmd[1:3] == ["scan", "source"]:
                cmd = [*cmd[:-1], *OFFLINE_FLAGS, cmd[-1]]  # the scan root stays last
            return run_tool(cmd, timeout=timeout, env=env)

        prepass.binary, prepass.run_tool = locate, offline_run
        try:
            yield prepass, sca
        finally:
            prepass.binary, prepass.run_tool = binary, run_tool


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
