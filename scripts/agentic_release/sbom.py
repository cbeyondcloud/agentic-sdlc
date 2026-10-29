"""The release's CycloneDX SBOM, third-party notices and constraints file, from uv.lock.

`uv export --format cyclonedx1.5` lists every package the release ships: the four
packages, their dependencies and every optional extra, but no dev tool. This module adds
what uv leaves out:

* each component's licence, from the installed distribution's metadata (the same reading
  as scripts/licence_check.py), else the reason in scripts/licence-exceptions.toml; a
  package not installed on this platform says so in an `agentic:licence` property;
* a `serialNumber` derived from the version and uv.lock, and the commit time as the
  `timestamp`, so a rebuild writes the same bytes;
* the release itself as `metadata.component`.

The notices file lists the same components with their licences and licence texts, after
the notices for code adapted into the framework (THIRD_PARTY_NOTICES.md).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tomllib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

import licence_check

from agentic_release.build import git_sources

RELEASE_NAME = "agentic-sdlc"
LICENCE = "MIT"
_SPDX = re.compile(r"^[A-Za-z0-9.+\-]+( (AND|OR|WITH) [A-Za-z0-9.+\-]+)*$")
_KNOWN_IDS = licence_check.ALLOWED_IDS | {"MPL-2.0"}
#: Trove classifier names (and a free-text spelling) that name exactly one SPDX licence.
_CLASSIFIER_IDS = {"mit license": "MIT", "isc license (iscl)": "ISC", "isc license": "ISC"}
_EXPORT = ["--frozen", "--no-dev", "--all-packages", "--all-extras"]
_LICENCE_FILE = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE|AUTHORS)[^/]*$", re.I)

Distribution = Callable[[str], metadata.Distribution]


def _uv_export(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["uv", "export", *_EXPORT, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if done.returncode != 0:
        raise RuntimeError(f"uv export failed: {done.stderr.strip()}")
    return done.stdout


def _exceptions(repo: Path) -> dict[str, str]:
    path = repo / "scripts" / "licence-exceptions.toml"
    if not path.is_file():
        path = Path(licence_check.EXCEPTIONS)
    data = tomllib.loads(path.read_text(encoding="utf-8")).get("allow", {})
    return {name.lower(): reason for name, reason in data.items()}


def licence_of(name: str, exceptions: dict[str, str], distribution: Distribution) -> dict | None:
    """The CycloneDX `licenses` entry for package `name`, or None when it is not
    installed here. An SPDX expression when the metadata has one, else a name."""
    try:
        dist = distribution(name)
    except metadata.PackageNotFoundError:
        return None
    expression, classifiers = licence_check._licence_of(dist)
    expression = expression.strip()
    if expression and _SPDX.fullmatch(expression):
        return {"expression": _CLASSIFIER_IDS.get(expression.lower(), expression)}
    found = [c.split(" :: ")[-1] for c in classifiers if c.startswith("License ::")]
    if len(found) == 1 and found[0].lower() in _CLASSIFIER_IDS:
        return {"expression": _CLASSIFIER_IDS[found[0].lower()]}
    if found:  # free text such as "Dual License": the classifiers say more
        return {"license": {"name": "; ".join(found)}}
    if expression:
        return {"license": {"name": expression}}
    reason = exceptions.get(name.lower())
    if reason:
        first = re.split(r"[ ,(]", reason, maxsplit=1)[0]
        if first.upper() in _KNOWN_IDS:
            return {"expression": first}
        return {"license": {"name": reason}}
    return {"license": {"name": "no licence metadata"}}


def export_sbom(
    repo: Path, version: str, epoch: int, distribution: Distribution = metadata.distribution
) -> dict:
    """The release SBOM (CycloneDX 1.5) as a dict; see the module docstring."""
    bom = json.loads(_uv_export(repo, "--format", "cyclonedx1.5"))
    lock_hash = hashlib.sha256((repo / "uv.lock").read_bytes()).hexdigest()
    bom["serialNumber"] = "urn:uuid:" + str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{RELEASE_NAME}/{version}/{lock_hash}")
    )
    meta = bom["metadata"]
    meta["timestamp"] = datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta["component"] = {
        "type": "application",
        "bom-ref": meta["component"]["bom-ref"],  # the dependency graph names it
        "name": RELEASE_NAME,
        "version": version,
        "licenses": [{"expression": LICENCE}],
    }
    _drop_project_root(bom)
    exceptions = _exceptions(repo)
    for component in bom["components"]:
        props = component.get("properties", [])
        if any(p["name"] == "uv:workspace:path" for p in props):
            component["purl"] = f"pkg:pypi/{component['name']}@{component['version']}"
            component["licenses"] = [{"expression": LICENCE}]
            continue
        found = licence_of(component["name"], exceptions, distribution)
        if found is None:
            props.append(
                {"name": "agentic:licence", "value": "not installed on this platform: unchecked"}
            )
            component["properties"] = props
        else:
            component["licenses"] = [found]
    return bom


def _drop_project_root(bom: dict) -> None:
    """Leave out the workspace's own root project: it is never published (the release,
    `metadata.component`, already depends on the four packages)."""
    roots = {
        c["bom-ref"]
        for c in bom["components"]
        if {"name": "uv:package:is_project_root", "value": "true"} in c.get("properties", [])
    }
    bom["components"] = [c for c in bom["components"] if c["bom-ref"] not in roots]
    kept = []
    for entry in bom.get("dependencies", []):
        if entry["ref"] in roots:
            continue
        entry["dependsOn"] = [ref for ref in entry.get("dependsOn", []) if ref not in roots]
        kept.append(entry)
    bom["dependencies"] = kept


def dumps(bom: dict) -> str:
    return json.dumps(bom, indent=2, ensure_ascii=False) + "\n"


def _licence_label(component: dict) -> str:
    entries = component.get("licenses") or []
    if not entries:
        return "unchecked (not installed on the build platform)"
    entry = entries[0]
    return entry.get("expression") or entry.get("license", {}).get("name", "")


def _source(component: dict) -> str:
    purl = component.get("purl", "")
    if "vcs_url=" in purl:
        return "git (built from the locked commit)"
    if any(p["name"] == "uv:workspace:path" for p in component.get("properties", [])):
        return "this release"
    return "package index"


def _licence_texts(name: str, distribution: Distribution) -> list[tuple[str, str]]:
    try:
        dist = distribution(name)
    except metadata.PackageNotFoundError:
        return []
    texts = []
    for entry in sorted(str(f) for f in (dist.files or []) if _LICENCE_FILE.search(str(f))):
        if ".dist-info/" not in entry:
            continue
        try:
            text = (dist.locate_file(entry)).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        texts.append((entry.split(".dist-info/", 1)[1], text.replace("\r\n", "\n").strip()))
    return texts


def notices(bom: dict, repo: Path, distribution: Distribution = metadata.distribution) -> str:
    """The release's third-party notices (Markdown): adapted code, then every component
    with its licence, then the licence texts the installed distributions carry."""
    release = bom["metadata"]["component"]
    adapted = (repo / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    adapted = adapted.replace("\r\n", "\n").split("\n", 1)[1].strip()  # drop its H1
    adapted = re.sub(r"(?m)^## ", "### ", adapted)
    lines = [
        f"# Third-party notices for {release['name']} {release['version']}",
        "",
        "Generated from uv.lock by scripts/release.py. The CycloneDX SBOM of this release",
        "lists the same components.",
        "",
        "## Code adapted into agentic-sdlc",
        "",
        adapted,
        "",
        "## Dependencies",
        "",
        "| Package | Version | Licence | Source |",
        "|---|---|---|---|",
    ]
    shipped = sorted(bom["components"], key=lambda c: c["name"])
    for c in shipped:
        label = _licence_label(c).replace("|", "/")
        lines.append(f"| {c['name']} | {c.get('version', '')} | {label} | {_source(c)} |")
    lines += ["", "## Licence texts", ""]
    for c in shipped:
        if _source(c) == "this release":
            continue
        for path, text in _licence_texts(c["name"], distribution):
            lines += [f"### {c['name']} {c.get('version', '')}: {path}", "", "````text"]
            lines += [text, "````", ""]
    return "\n".join(lines).rstrip() + "\n"


def constraints(repo: Path) -> str:
    """Pins for every third-party package the release may install (`uv tool install
    agentic-cli==X --constraints constraints.txt`). A git-sourced package is pinned by
    version: the release publishes it to the internal index."""
    text = _uv_export(
        repo, "--format", "requirements.txt", "--no-emit-workspace", "--no-hashes", "--no-header"
    )
    locked = {s.name: s.version for s in git_sources(repo / "uv.lock")}
    out = [
        "# Constraints for agentic-sdlc: the versions in uv.lock (scripts/release.py).",
    ]
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        requirement, _, marker = stripped.partition(" ; ")
        name = re.split(r"[ =@\[;]", requirement, maxsplit=1)[0]
        if "git+" in requirement and name in locked:
            requirement = f"{name}=={locked[name]}"
        out.append(f"{requirement} ; {marker}" if marker else requirement)
    return "\n".join(out) + "\n"
