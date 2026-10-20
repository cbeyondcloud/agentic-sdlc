"""Admission checks: whether a skill may enter the catalog (spec §11.3).

Each check names what it refused, so a person can fix it:

  licence   the catalog entry's licence is one the catalog allows; the folder carries
            the licence text (LICENSE*, unless the skill is `internal`); a licence in
            the SKILL.md front matter, if it is an SPDX id, agrees with the entry
  validate  `skills-ref validate` (the Agent Skills reference validator): SKILL.md
            exists, front matter fields, name format and name == folder name
  size      SKILL.md within `max_skill_tokens`, the folder within `max_total_bytes`
  scan      static scan of every file:
              * scripts or binary files need a person's review (`scripts_reviewed_by`);
              * download-and-run lines, hidden Unicode (bidi, zero-width, tag
                characters), prompt-injection phrases and telemetry or analytics URLs
                are refused outright - a review never waives them
  pin       (`admit_catalog` only) the folder exists, is catalogued and matches its
            pinned sha256

The LLM scan, the human review of scripts and the A/B promotion gate (M1-63) are
separate steps; this module is the static part.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from agentic.skills.catalog import (
    Catalog,
    CatalogError,
    SkillEntry,
    load_catalog,
    skill_digest,
    skill_folder,
)
from agentic.skills.loader import estimate_tokens
from agentic.yamlio import YamlFileError, load_yaml, split_front_matter

SCRIPT_SUFFIXES = frozenset(
    ".py .sh .bash .zsh .fish .ps1 .psm1 .bat .cmd .js .cjs .mjs .ts .rb .pl .php .lua "
    ".exe .dll .so .dylib .jar .bin".split()
)
LICENCE_FILE = re.compile(r"^(LICEN[CS]E|COPYING)(\.(txt|md))?$", re.IGNORECASE)
SPDX_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]*$")

DOWNLOAD_AND_RUN = [
    re.compile(
        r"\b(curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^\n|]*\|\s*"
        r"(sudo\s+)?(ba|z|da|k)?sh\b",
        re.IGNORECASE,
    ),
    re.compile(r"\|\s*(iex|Invoke-Expression)\b", re.IGNORECASE),
    re.compile(r"\bbase64\s+(-d|--decode)\b[^\n|]*\|\s*(sudo\s+)?(ba|z)?sh\b"),
]
INJECTION = [
    re.compile(r"\bignore\s+(all\s+)?(the\s+)?(previous|prior|above)\s+instructions\b", re.I),
    re.compile(r"\bdisregard\s+(all\s+)?(your|previous|prior|the\s+above)\s+instructions\b", re.I),
    re.compile(r"\bdo\s+not\s+(tell|inform)\s+the\s+user\b", re.I),
]
HIDDEN = re.compile(
    r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]"
)

BOM = chr(0xFEFF)
URL = re.compile(r"https?://[^\s<>()\[\]\"'`]+", re.IGNORECASE)
# Analytics and error-reporting hosts, and URL words that mean usage reporting.
TELEMETRY_HOSTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "segment.io",
    "segment.com",
    "mixpanel.com",
    "amplitude.com",
    "posthog.com",
    "plausible.io",
    "sentry.io",
    "hotjar.com",
    "heap.io",
    "statsig.com",
)
TELEMETRY_WORDS = re.compile(
    r"(^|[./_-])(telemetry|analytics|tracking|track|collect|beacon|pixel|metrics)([./?_-]|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Rejection:
    check: str  # licence | validate | size | scan | pin
    path: str  # the file (relative to the skill) or the skill key
    reason: str


def telemetry_urls(text: str) -> list[str]:
    """Every URL in `text` that points at an analytics host or names usage reporting."""
    found = []
    for match in URL.finditer(text):
        url = match.group(0).rstrip(".,;:!?")
        host_and_path = url.split("://", 1)[1]
        host = host_and_path.split("/", 1)[0].split(":", 1)[0].lower()
        if any(host == known or host.endswith("." + known) for known in TELEMETRY_HOSTS):
            found.append(url)
        elif TELEMETRY_WORDS.search(host_and_path.split("?", 1)[0]):
            found.append(url)
    return found


def admit(folder: Path, key: str, catalog: Catalog) -> list[Rejection]:
    """Every reason `folder` may not be catalogued as `key` ([] = admitted)."""
    entry = catalog.entry(key)
    return [
        *_licence(folder, entry, catalog),
        *_validate(folder),
        *_size(folder, catalog),
        *_scan(folder, entry),
    ]


def admit_catalog(root: Path) -> dict[str, list[Rejection]]:
    """Admit every skill of a catalog folder, plus the pin check: each catalogued skill
    exists and matches its sha256, and no skill folder is uncatalogued."""
    catalog = load_catalog(root)
    results: dict[str, list[Rejection]] = {}
    for key, entry in catalog.skills.items():
        folder = skill_folder(root, key)
        if not folder.is_dir():
            results[key] = [Rejection("pin", key, f"no folder {folder.relative_to(root)}")]
            continue
        found = skill_digest(folder)
        pin = (
            [Rejection("pin", key, f"pinned {entry.sha256}, folder hashes to {found}")]
            if found != entry.sha256
            else []
        )
        results[key] = pin + admit(folder, key, catalog)
    for source in sorted(p for p in root.iterdir() if p.is_dir()):
        for folder in sorted(p for p in source.iterdir() if p.is_dir()):
            key = f"{source.name}/{folder.name}"
            if key not in catalog.skills:
                results[key] = [Rejection("pin", key, "a skill folder that is not catalogued")]
    return results


def _licence(folder: Path, entry: SkillEntry, catalog: Catalog) -> list[Rejection]:
    found = []
    if entry.licence not in catalog.licences:
        found.append(
            Rejection(
                "licence",
                "catalog.yaml",
                f"licence {entry.licence!r} is not allowed (allowed: {catalog.licences})",
            )
        )
    has_file = any(LICENCE_FILE.match(p.name) for p in folder.iterdir() if p.is_file())
    if entry.licence != "internal" and not has_file:
        found.append(Rejection("licence", ".", "no licence text (LICENSE, LICENSE.txt, ...)"))
    declared = _front_matter(folder).get("license")
    if isinstance(declared, str) and SPDX_ID.match(declared) and declared != entry.licence:
        found.append(
            Rejection(
                "licence",
                "SKILL.md",
                f"SKILL.md says licence {declared!r}, the catalog says {entry.licence!r}",
            )
        )
    return found


def _validate(folder: Path) -> list[Rejection]:
    try:
        from skills_ref.validator import validate
    except ImportError:
        return [Rejection("validate", ".", "skills-ref is not installed (agentic-core[skills])")]
    return [Rejection("validate", "SKILL.md", problem) for problem in validate(folder)]


def _size(folder: Path, catalog: Catalog) -> list[Rejection]:
    limits, found = catalog.limits, []
    skill_md = folder / "SKILL.md"
    if skill_md.is_file():
        tokens = estimate_tokens(skill_md.read_text(encoding="utf-8", errors="replace"))
        if tokens > limits.max_skill_tokens:
            found.append(
                Rejection(
                    "size",
                    "SKILL.md",
                    f"about {tokens} tokens, over the budget of {limits.max_skill_tokens}",
                )
            )
    total = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
    if total > limits.max_total_bytes:
        found.append(
            Rejection("size", ".", f"{total} bytes, over the budget of {limits.max_total_bytes}")
        )
    return found


def _scan(folder: Path, entry: SkillEntry) -> list[Rejection]:
    found = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        relative = path.relative_to(folder).as_posix()
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        script = path.suffix.lower() in SCRIPT_SUFFIXES or data.startswith(b"#!")
        if (script or text is None) and entry.scripts_reviewed_by is None:
            what = "a script" if script else "a binary file"
            found.append(
                Rejection(
                    "scan", relative, f"{what}: a person must review it (scripts_reviewed_by)"
                )
            )
        if text is None:
            continue
        text = text.removeprefix(BOM)
        for number, line in enumerate(text.splitlines(), start=1):
            where = f"{relative}:{number}"
            if any(p.search(line) for p in DOWNLOAD_AND_RUN):
                found.append(Rejection("scan", where, "downloads and runs code"))
            if any(p.search(line) for p in INJECTION):
                found.append(Rejection("scan", where, "reads like a prompt injection"))
            if char := HIDDEN.search(line):
                found.append(
                    Rejection("scan", where, f"hidden character U+{ord(char.group(0)):04X}")
                )
            for url in telemetry_urls(line):
                found.append(Rejection("scan", where, f"telemetry or analytics URL: {url}"))
    return found


def _front_matter(folder: Path) -> dict:
    try:
        header, _ = split_front_matter((folder / "SKILL.md").read_text(encoding="utf-8-sig"))
        data = load_yaml(header, first_line=2)[0]
    except (OSError, UnicodeDecodeError, YamlFileError):
        return {}  # `validate` reports an unreadable SKILL.md
    return data if isinstance(data, dict) else {}


__all__ = ["CatalogError", "Rejection", "admit", "admit_catalog", "telemetry_urls"]
