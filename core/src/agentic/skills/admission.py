"""Admission checks: whether a skill may enter the catalog (spec §11.3).

Each check names what it refused, so a person can fix it:

  licence   the catalog entry's licence is one org policy allows (catalog.yaml may
            narrow that list, never widen it: DF-143); the folder carries
            the licence text (LICENSE*, unless the skill is `internal`); a licence in
            the SKILL.md front matter, if it is an SPDX id, agrees with the entry
  validate  `skills-ref validate` (the Agent Skills reference validator): SKILL.md
            exists, front matter fields, name format and name == folder name
  size      SKILL.md within `max_skill_tokens`, the folder within `max_total_bytes`
            (each the smaller of org policy's and the catalog's)
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

import bisect
import re
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from agentic.skills.catalog import (
    DEFAULT_LICENCES,
    PLUGINS,
    AdmissionLimits,
    Catalog,
    CatalogError,
    SkillEntry,
    SkillsError,
    load_catalog,
    skill_digest,
    skill_files,
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

_FETCH = r"(curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)"
_SHELL = r"(ba|z|da|k)?sh"
DOWNLOAD_AND_RUN = [
    re.compile(rf"\b{_FETCH}\b[^\n|]*\|\s*(sudo\s+)?{_SHELL}\b", re.IGNORECASE),
    re.compile(r"\|\s*(iex|Invoke-Expression)\b", re.IGNORECASE),
    re.compile(rf"\bbase64\s+(-d|--decode)\b[^\n|]*\|\s*(sudo\s+)?{_SHELL}\b"),
    # process and command substitution (DF-142): `bash <(curl ...)`, `sh -c "$(curl ...)"`
    re.compile(rf"(\b{_SHELL}|\bsource|(^|\s)\.)\s+<\(\s*{_FETCH}\b", re.IGNORECASE),
    re.compile(rf"\b{_SHELL}\s+-c\s+[\"']?\$\(\s*{_FETCH}\b", re.IGNORECASE),
]
INJECTION = [
    re.compile(r"\bignore\s+(all\s+)?(the\s+)?(previous|prior|above)\s+instructions\b", re.I),
    re.compile(r"\bdisregard\s+(all\s+)?(your|previous|prior|the\s+above)\s+instructions\b", re.I),
    re.compile(r"\bdo\s+not\s+(tell|inform)\s+the\s+user\b", re.I),
    re.compile(
        r"\bforget\s+(everything|all(\s+of)?(\s+the)?)\s+(above|before|prior|previous)\b", re.I
    ),
    # a role header hidden in an HTML comment, which a rendered page never shows (DF-142)
    re.compile(r"<!--\s*(system|assistant|developer)\s*:", re.I),
]
#: Hidden characters besides Unicode's format (Cf) and control (Cc) categories: the
#: variation-selector supplement, which can smuggle bytes past a reader (DF-142).
_SMUGGLING = re.compile(r"[\U000e0100-\U000e01ef]")
_ASCII_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_VISIBLE_CONTROLS = frozenset("\t\n\r")

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
        # a #fragment never leaves the machine, so it cannot report anything (DF-139)
        host_and_path = url.split("://", 1)[1].split("#", 1)[0]
        host = host_and_path.split("/", 1)[0].split(":", 1)[0].lower()
        if any(host == known or host.endswith("." + known) for known in TELEMETRY_HOSTS):
            found.append(url)
        elif TELEMETRY_WORDS.search(host_and_path.split("?", 1)[0]):
            found.append(url)
    return found


def hidden_char(text: str) -> str | None:
    """The first hidden character in `text`: a format (Cf: bidi controls, zero-width,
    tag characters, the soft hyphen) or control (Cc) character other than tab, LF and
    CR, or a supplementary variation selector. None when there is none."""
    if text.isascii():
        found = _ASCII_CONTROL.search(text)
        return found.group(0) if found else None
    for char in text:
        if char in _VISIBLE_CONTROLS:
            continue
        if unicodedata.category(char) in ("Cf", "Cc") or _SMUGGLING.match(char):
            return char
    return None


def scan_text(text: str) -> list[tuple[int, str]]:
    """Every (line number, reason) the static scan finds in one file's text.

    The scan is a tripwire for careless or lazy payloads, not a proof of safety (an LLM
    scan and a person's review are the later steps, spec §11.3):

      * download-and-run commands, matched on logical lines - a line ending with `\\`
        or a PowerShell backtick continues on the next, and is reported where it starts;
      * prompt-injection phrases, matched on the whole text with every run of white
        space read as one space, so a phrase split across lines is still found;
      * hidden characters (`hidden_char`) and telemetry or analytics URLs, per line."""
    lines = text.splitlines()
    found: list[tuple[int, str]] = []
    for number, line in _logical_lines(lines):
        if any(p.search(line) for p in DOWNLOAD_AND_RUN):
            found.append((number, "downloads and runs code"))
    words = [(m.group(0), n) for n, line in enumerate(lines, 1) for m in re.finditer(r"\S+", line)]
    flat, starts = "", []
    for word, _ in words:
        starts.append(len(flat))
        flat += word + " "
    for pattern in INJECTION:
        for match in pattern.finditer(flat):
            index = bisect.bisect_right(starts, match.start()) - 1
            found.append((words[max(index, 0)][1], "reads like a prompt injection"))
    for number, line in enumerate(lines, start=1):
        if char := hidden_char(line):
            found.append((number, f"hidden character U+{ord(char):04X}"))
        for url in telemetry_urls(line):
            found.append((number, f"telemetry or analytics URL: {url}"))
    return sorted(dict.fromkeys(found), key=lambda item: item[0])


def _logical_lines(lines: list[str]) -> list[tuple[int, str]]:
    """(first line number, text) with continued lines joined."""
    joined: list[tuple[int, str]] = []
    current: tuple[int, str] | None = None
    for number, line in enumerate(lines, start=1):
        stripped = line.rstrip()
        continues = stripped.endswith(("\\", "`"))
        part = stripped[:-1] if continues else line
        current = (number, part) if current is None else (current[0], f"{current[1]} {part}")
        if not continues:
            joined.append(current)
            current = None
    if current is not None:
        joined.append(current)
    return joined


def admit(
    folder: Path,
    key: str,
    catalog: Catalog,
    *,
    licences: Collection[str] | None = None,
    limits: AdmissionLimits | None = None,
) -> list[Rejection]:
    """Every reason `folder` may not be catalogued as `key` ([] = admitted).

    `licences` and `limits` are org policy's (default: the framework's own list and
    limits). The catalog under review can only narrow them - never widen - so one
    policy-repo pull request cannot add a skill and also allow its licence (DF-143)."""
    entry = catalog.entry(key)
    if not folder.is_dir():
        return [Rejection("validate", ".", f"no skill folder at {folder}")]
    floor = list(DEFAULT_LICENCES if licences is None else licences)
    allowed = [name for name in catalog.licences if name in floor]
    return [
        *_licence(folder, entry, allowed, floor),
        *_validate(folder),
        *_size(folder, _narrowest(catalog.limits, limits or AdmissionLimits())),
        *_scan(folder, entry),
    ]


def _narrowest(catalog: AdmissionLimits, floor: AdmissionLimits) -> AdmissionLimits:
    """Each limit the smaller of the catalog's and org policy's."""
    names = type(floor).model_fields
    return AdmissionLimits(**{n: min(getattr(catalog, n), getattr(floor, n)) for n in names})


def admit_catalog(
    root: Path,
    *,
    licences: Collection[str] | None = None,
    limits: AdmissionLimits | None = None,
) -> dict[str, list[Rejection]]:
    """Admit every skill of a catalog folder, plus the pin check: each catalogued skill
    exists and matches its sha256, and no skill folder is uncatalogued. `licences` and
    `limits` are org policy's, as for `admit`."""
    catalog = load_catalog(root)
    results: dict[str, list[Rejection]] = {}
    for key, entry in catalog.skills.items():
        folder = skill_folder(root, key)
        if not folder.is_dir():
            results[key] = [Rejection("pin", key, f"no folder {folder.relative_to(root)}")]
            continue
        checked = admit(folder, key, catalog, licences=licences, limits=limits)
        try:
            found = skill_digest(folder)
        except SkillsError as error:  # a link inside the skill: it has no digest
            results[key] = [Rejection("pin", key, str(error)), *checked]
            continue
        pin = (
            [Rejection("pin", key, f"pinned {entry.sha256}, folder hashes to {found}")]
            if found != entry.sha256
            else []
        )
        results[key] = pin + checked
    for source in sorted(p for p in root.iterdir() if p.is_dir() and p.name != PLUGINS):
        for folder in sorted(p for p in source.iterdir() if p.is_dir()):
            key = f"{source.name}/{folder.name}"
            if key not in catalog.skills:
                results[key] = [Rejection("pin", key, "a skill folder that is not catalogued")]
    return results


def _licence(
    folder: Path, entry: SkillEntry, allowed: list[str], floor: list[str]
) -> list[Rejection]:
    found = []
    if entry.licence not in allowed:
        why = (
            f"licence {entry.licence!r} is not allowed by org policy (allowed: {floor}); "
            "catalog.yaml may only narrow that list"
            if entry.licence not in floor
            else f"licence {entry.licence!r} is not allowed (allowed: {allowed})"
        )
        found.append(Rejection("licence", "catalog.yaml", why))
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


def skills_ref_problems(folder: Path) -> list[str] | None:
    """`skills-ref validate`'s problems with the folder (the Agent Skills reference
    validator), or None when skills-ref is not installed (agentic-core[skills])."""
    try:
        from skills_ref.validator import validate
    except ImportError:
        return None
    return list(validate(folder))


def _validate(folder: Path) -> list[Rejection]:
    problems = skills_ref_problems(folder)
    if problems is None:
        return [Rejection("validate", ".", "skills-ref is not installed (agentic-core[skills])")]
    return [Rejection("validate", "SKILL.md", problem) for problem in problems]


def _size(folder: Path, limits: AdmissionLimits) -> list[Rejection]:
    found = []
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
    total = sum(p.stat().st_size for p in skill_files(folder)[0])
    if total > limits.max_total_bytes:
        found.append(
            Rejection("size", ".", f"{total} bytes, over the budget of {limits.max_total_bytes}")
        )
    return found


def _scan(folder: Path, entry: SkillEntry) -> list[Rejection]:
    files, links = skill_files(folder)
    # a symlink or junction could point anywhere on the machine that installs the skill
    found = [
        Rejection("scan", link.relative_to(folder).as_posix(), "a link: skills hold plain files")
        for link in links
    ]
    for path in files:
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
        for number, reason in scan_text(text.removeprefix(BOM)):
            found.append(Rejection("scan", f"{relative}:{number}", reason))
    return found


def _front_matter(folder: Path) -> dict:
    try:
        header, _ = split_front_matter((folder / "SKILL.md").read_text(encoding="utf-8-sig"))
        data = load_yaml(header, first_line=2)[0]
    except (OSError, UnicodeDecodeError, YamlFileError):
        return {}  # `validate` reports an unreadable SKILL.md
    return data if isinstance(data, dict) else {}


__all__ = [
    "CatalogError",
    "Rejection",
    "admit",
    "admit_catalog",
    "hidden_char",
    "scan_text",
    "skills_ref_problems",
    "telemetry_urls",
]
