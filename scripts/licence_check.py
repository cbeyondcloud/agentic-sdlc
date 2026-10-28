"""Licence allow-list check over every third-party package in uv.lock (P0-03).

Allowed: the permissive licences in the master plan's OSS-first rule (MIT, Apache-2.0,
BSD, ISC, PostgreSQL), Python's own PSF licence, and Unlicense/CC0-1.0 for unmodified
dependencies (owner decision, DF-83). Anything denied or unknown fails the check unless
it is listed with a reason in scripts/licence-exceptions.toml.

    uv run python scripts/licence_check.py

Packages locked only for another platform are not installed here and are skipped; the
Linux stage of scripts/check.py covers the Linux-only ones.
"""

from __future__ import annotations

import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXCEPTIONS = Path(__file__).resolve().parent / "licence-exceptions.toml"

# SPDX ids (upper-cased) and free-text spellings seen in package metadata.
ALLOWED_IDS = {
    "MIT",
    "MIT-0",
    "APACHE-2.0",
    "BSD-2-CLAUSE",
    "BSD-3-CLAUSE",
    "0BSD",
    "ISC",
    "POSTGRESQL",
    "PSF-2.0",
    "PYTHON-2.0",
    "CNRI-PYTHON",
    "UNLICENSE",
    "CC0-1.0",
}
ALLOWED_TEXT = re.compile(
    r"^(mit( license)?|apache( software)?( license)?( 2\.0| version 2\.0| 2)?|"
    r"bsd( license)?|(new |simplified |modified )?bsd|bsd[- ][23][- ]clause( license)?|"
    r"isc( license)?|postgresql( license)?|psfl?|python software foundation license|"
    r"the unlicense|unlicense|cc0)$"
)
DENIED = re.compile(r"\b(a?gpl|lgpl|sspl|busl|bsl-1|polyform|commons clause|proprietary)", re.I)
ALLOWED_CLASSIFIERS = re.compile(
    r"License :: (OSI Approved :: )?(MIT|Apache Software|BSD|ISC|PostgreSQL|"
    r"Python Software Foundation|The Unlicense|CC0)",
)
DENIED_CLASSIFIERS = re.compile(r"License :: .*(GPL|Affero|Proprietary|Other/Proprietary)")


def _term(term: str) -> str:
    """One licence term of an SPDX expression: allowed, denied or unknown."""
    term = term.strip().strip("()").strip()
    if term.upper() in ALLOWED_IDS or ALLOWED_TEXT.match(term.lower()):
        return "allowed"
    if DENIED.search(term) or term.lower() in {"mpl-2.0", "epl-2.0", "cddl-1.0"}:
        return "denied"
    return "unknown"


def classify(expression: str, classifiers: list[str]) -> str:
    """Classify a package's licence as "allowed", "denied" or "unknown".

    The expression decides when it is clear: every AND term must be allowed, and one
    allowed OR option is enough. When it is missing or unclear (free text such as
    "Dual License"), the trove classifiers decide.
    """
    verdict = _expression_verdict(expression.strip()) if expression.strip() else "unknown"
    return verdict if verdict != "unknown" else _classifier_verdict(classifiers)


def _expression_verdict(expression: str) -> str:
    verdicts = []
    for option in re.split(r"\s+OR\s+", expression, flags=re.I):
        terms = [_term(t) for t in re.split(r"\s+AND\s+", option, flags=re.I)]
        if all(t == "allowed" for t in terms):
            return "allowed"
        verdicts.append("denied" if "denied" in terms else "unknown")
    return "denied" if all(v == "denied" for v in verdicts) else "unknown"


def _classifier_verdict(classifiers: list[str]) -> str:
    found = [c for c in classifiers if c.startswith("License ::")]
    if any(DENIED_CLASSIFIERS.search(c) for c in found):
        return "denied"
    if found and all(ALLOWED_CLASSIFIERS.search(c) for c in found):
        return "allowed"
    return "unknown"


def _licence_of(dist: metadata.Distribution) -> tuple[str, list[str]]:
    meta = dist.metadata
    expression = meta.get("License-Expression") or ""
    if not expression:
        text = (meta.get("License") or "").strip()
        expression = text if text and "\n" not in text and len(text) < 80 else ""
    return expression, meta.get_all("Classifier") or []


def third_party_packages(lock: Path) -> list[str]:
    """Names of the packages in uv.lock that come from an index or git (not the workspace)."""
    data = tomllib.loads(lock.read_text(encoding="utf-8"))
    return sorted(
        p["name"]
        for p in data.get("package", [])
        if not {"editable", "virtual", "workspace"} & set(p.get("source", {}))
    )


def check(lock: Path, exceptions_file: Path, distribution=metadata.distribution) -> list[str]:
    """One line per package that fails the allow-list, plus stale exception entries."""
    listed = {}
    if exceptions_file.exists():
        listed = tomllib.loads(exceptions_file.read_text(encoding="utf-8")).get("allow", {})
    exceptions = {name.lower() for name in listed}
    locked = third_party_packages(lock)
    failures, skipped = [], 0
    for name in locked:
        try:
            dist = distribution(name)
        except metadata.PackageNotFoundError:
            skipped += 1
            continue
        expression, classifiers = _licence_of(dist)
        verdict = classify(expression, classifiers)
        if verdict != "allowed" and name.lower() not in exceptions:
            shown = expression or "; ".join(c for c in classifiers if c.startswith("License"))
            failures.append(f"{name} {dist.version}: {verdict} ({shown or 'no licence metadata'})")
    for name in sorted(exceptions - {n.lower() for n in locked}):
        failures.append(f"{name}: listed in {exceptions_file.name} but no longer locked")
    print(f"licence check: {len(locked) - skipped} packages checked, {skipped} not installed here")
    return failures


def main() -> int:
    failures = check(REPO_ROOT / "uv.lock", EXCEPTIONS)
    if failures:
        print(f"Not on the licence allow-list (fix, or add to {EXCEPTIONS.name} with a reason):")
        print("\n".join(f"  {line}" for line in failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
