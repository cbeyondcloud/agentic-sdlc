"""One version for the four workspace packages (lockstep semver).

The root `pyproject.toml` `[project].version` is the source. `uv_build` reads only static
versions, so `set_version` writes the new version into every member and moves every
internal pin with it, and `check_versions` (run before each build) fails on any drift:

* each member's `version` equals the source, and so does each workspace package's
  version in `uv.lock` (the SBOM is exported from the lock);
* each internal requirement (`agentic-core`, `sdlc-commons`, ...) is pinned `==<source>`,
  so a published CLI can never resolve another release's core from the index;
* each git-sourced requirement (Zyroflo's `sdlc-harness`) is pinned exactly, and to the
  version in `uv.lock`, so an index can never offer a look-alike in its place;
* the umbrella Helm chart (M3-02) has the release as `version` (SemVer: 1.4.0rc1 is
  1.4.0-rc.1) and as `appVersion` (the control-plane image tag).

A version is MAJOR.MINOR.PATCH with an optional aN, bN or rcN: always a valid value for
an org's `min_cli_version` (M2-08), and always greater than the release before it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import Version

MEMBERS = ("core", "commons", "cli", "control-plane")
INTERNAL = frozenset({"agentic-core", "sdlc-commons", "agentic-cli", "agentic-control-plane"})
PARTS = ("major", "minor", "patch")
RELEASE_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?$")
_PIN = re.compile(
    r'"(?P<name>agentic-core|sdlc-commons|agentic-cli|agentic-control-plane)'
    r'(?P<extras>\[[^\]]*\])?==[^"]*"'
)
_PROJECT_VERSION = re.compile(r'(?ms)^(\[project\]\n(?:(?!^\[).)*?^version = ")([^"]*)(")')
#: The umbrella Helm chart (M3-02), released with the packages: `version` is the release as
#: SemVer (Helm requires it), `appVersion` the release itself (the image tag).
CHART = Path("deploy/helm/agentic-platform/Chart.yaml")
_CHART_VERSION = re.compile(r"(?m)^version: *(\S*) *$")
_CHART_APP_VERSION = re.compile(r'(?m)^appVersion: *"?([^"\s]*)"? *$')
_SEMVER_PRE = {"a": "alpha", "b": "beta", "rc": "rc"}


class VersionError(ValueError):
    """A version that cannot be released."""


def parse(text: str) -> Version:
    """`text` as a release version, or VersionError."""
    if not RELEASE_PATTERN.fullmatch(text.strip()):
        raise VersionError(
            f"{text!r} is not a release version: use MAJOR.MINOR.PATCH, optionally followed "
            "by aN, bN or rcN (e.g. 1.4.0 or 1.4.0rc1)"
        )
    return Version(text.strip())


def _project(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def read_version(root: Path) -> str:
    """The release version: the root pyproject's `[project].version`."""
    return str(_project(root / "pyproject.toml")["project"]["version"])


def chart_version(release: str) -> str:
    """`release` as the chart's SemVer version: 1.4.0rc1 is 1.4.0-rc.1, a3 alpha.3, b1
    beta.1 (SemVer orders alpha < beta < rc, as PEP 440 does)."""
    found = parse(release)
    core = ".".join(str(part) for part in found.release)
    if found.pre is None:
        return core
    label, number = found.pre
    return f"{core}-{_SEMVER_PRE[label]}.{number}"


def _chart_problems(root: Path, current: str) -> list[str]:
    path = root / CHART
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    problems = []
    found = _CHART_VERSION.search(text)
    want = chart_version(current) if RELEASE_PATTERN.fullmatch(current) else current
    if found is None or found[1] != want:
        shown = found[1] if found else "missing"
        problems.append(f"{CHART.as_posix()}: version {shown} (the release is {want})")
    app = _CHART_APP_VERSION.search(text)
    if app is None or app[1] != current:
        shown = app[1] if app else "missing"
        problems.append(f"{CHART.as_posix()}: appVersion {shown} (the release is {current})")
    return problems


def _locked_versions(root: Path) -> dict[str, str]:
    lock = root / "uv.lock"
    if not lock.is_file():
        return {}
    data = tomllib.loads(lock.read_text(encoding="utf-8"))
    return {p["name"]: str(p.get("version", "")) for p in data.get("package", [])}


def check_versions(root: Path) -> list[str]:
    """One line per disagreement with the root version; [] when all agree."""
    current = read_version(root)
    problems = []
    try:
        parse(current)
    except VersionError as error:
        problems.append(f"pyproject.toml: {error}")
    locked = _locked_versions(root)
    # the SBOM is exported from uv.lock as it stands: a lock still at the last release
    # would sign an SBOM that names it
    workspace = [root / "pyproject.toml", *(root / m / "pyproject.toml" for m in MEMBERS)]
    for path in workspace if locked else []:
        name = str(_project(path)["project"]["name"])
        if name in locked and locked[name] != current:
            problems.append(
                f"uv.lock: {name} {locked[name]} (the release is {current}): run uv lock"
            )
    for member in MEMBERS:
        rel = f"{member}/pyproject.toml"
        data = _project(root / member / "pyproject.toml")
        found = str(data["project"].get("version", ""))
        if found != current:
            problems.append(f"{rel}: version {found} (the release is {current})")
        git_sourced = {
            name
            for name, source in data.get("tool", {}).get("uv", {}).get("sources", {}).items()
            if isinstance(source, dict) and "git" in source
        }
        for text in data["project"].get("dependencies", []):
            try:
                req = Requirement(text)
            except InvalidRequirement:
                problems.append(f"{rel}: cannot parse the requirement {text!r}")
                continue
            pins = [s.version for s in req.specifier if s.operator == "=="]
            exact = len(req.specifier) == 1 and len(pins) == 1
            if req.name in INTERNAL and not (exact and pins[0] == current):
                problems.append(f"{rel}: {req.name} must be pinned =={current} (found {text!r})")
            elif req.name in git_sourced:
                want = locked.get(req.name)
                if not exact or (want and pins[0] != want):
                    shown = f"=={want}" if want else "exactly"
                    problems.append(
                        f"{rel}: {req.name} comes from git and must be pinned {shown}, the "
                        f"version in uv.lock, so no index can offer another (found {text!r})"
                    )
    return problems + _chart_problems(root, current)


def set_version(root: Path, new: str) -> list[Path]:
    """Write `new` into the root and every member, moving the internal pins; returns the
    files changed. Refuses a version that is not greater than the current one."""
    wanted = parse(new)
    current = read_version(root)
    if wanted <= Version(current):
        raise VersionError(f"{new} is not greater than the current version {current}")
    changed = []
    for path in [root / "pyproject.toml", *(root / m / "pyproject.toml" for m in MEMBERS)]:
        text = path.read_text(encoding="utf-8")
        updated, count = _PROJECT_VERSION.subn(rf"\g<1>{wanted}\g<3>", text, count=1)
        if count != 1:
            raise VersionError(f"{path}: no [project] version line to update")
        updated = _PIN.sub(lambda m: f'"{m["name"]}{m["extras"] or ""}=={wanted}"', updated)
        if updated != text:
            path.write_text(updated, encoding="utf-8", newline="\n")
            changed.append(path)
    chart = root / CHART
    if chart.is_file():
        text = chart.read_text(encoding="utf-8")
        updated = _CHART_VERSION.sub(f"version: {chart_version(str(wanted))}", text, count=1)
        updated = _CHART_APP_VERSION.sub(f'appVersion: "{wanted}"', updated, count=1)
        if updated != text:
            chart.write_text(updated, encoding="utf-8", newline="\n")
            changed.append(chart)
    return changed


def bump(current: str, part: str) -> str:
    """The next version after `current` for a `part` change. A pre-release bumps to its
    own final release when the part's lower numbers are zero (1.2.0rc1 + minor = 1.2.0)."""
    if part not in PARTS:
        raise VersionError(f"bump one of {', '.join(PARTS)}, not {part!r}")
    found = parse(current)
    major, minor, patch = found.release
    if found.pre is not None:
        if (
            part == "patch"
            or (part == "minor" and patch == 0)
            or (part == "major" and minor == patch == 0)
        ):
            return f"{major}.{minor}.{patch}"
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"
