"""The deterministic review pre-pass: what machines find in a change before an agent reviews
it (M2-48, spec §15).

Results are documents in Zyroflo's pre-pass shape (`tools.review.prepass`): `tools[]` - one
row per check with `status`, `detail` and `findings` - and `sources[]` of rdjson-style
diagnostics. The external scanners (gitleaks, ast-grep, osv-scanner) run in commons
(`sdlc_commons.review_prepass`), through Zyroflo; the checks here are pure Python, so they
run on every machine:

  * `secret-scan`   - the M1-41 secret scanner over every added line. A finding names the
                      detector and the place, never the value.
  * `licence-check` - SPDX tags and manifest licence fields (`"license": ...`,
                      `license = ...`) on added lines, against org policy's blocked
                      licences. A blocked id also matches its `-only`, `-or-later` and `+`
                      forms. A licence stated only in a full licence text is not detected
                      here (that needs ScanCode).
  * `ignore-rules`  - every rule added to a `.gitignore` (DF-70, DF-329): files it matches
                      are hidden from `git add -A` and so from the review.

`merge` joins documents. A tool that did not run (`status` other than `ok`) has
`findings: null`, never 0, and is listed in `not_run`. `render` writes the block an agent
reads, with every machine-produced line fenced as untrusted data (M1-42).
"""

import re
from dataclasses import dataclass

from agentic.security.secret_scan import SecretScanner
from agentic.security.untrusted import untrusted

SCHEMA_VERSION = 1
STATUS_OK = "ok"
PRE_EXISTING_SUFFIX = " (pre-existing)"  # Zyroflo's marker for context, not gate material
MAX_SHOWN = 50  # findings rendered per source; the rest are counted

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_SPDX = re.compile(r"SPDX-License-Identifier:\s*(?P<expr>[^\n]+?)\s*(?:\*/|-->|$)")
_MANIFEST = (
    re.compile(r'"license"\s*:\s*"(?P<expr>[^"]{1,200})"', re.IGNORECASE),
    re.compile(r'^\s*license\s*=\s*"(?P<expr>[^"]{1,200})"', re.IGNORECASE),
    re.compile(r'^\s*license\s*=\s*\{\s*text\s*=\s*"(?P<expr>[^"]{1,200})"', re.IGNORECASE),
    re.compile(r"<license>\s*<name>(?P<expr>[^<]{1,200})</name>", re.IGNORECASE),
)
_LICENCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+\-]*")
_OPERATORS = frozenset({"and", "or", "with"})


@dataclass(frozen=True)
class AddedLine:
    path: str  # as the diff names it, without the `b/` prefix
    line: int  # on the new side
    text: str


def added_lines(diff_text: str) -> list[AddedLine]:
    """Every line a unified diff adds, with its file and new-side line number."""
    found: list[AddedLine] = []
    path: str | None = None
    line = 0
    in_hunk = False
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            path, in_hunk = None, False
        elif not in_hunk and raw.startswith("+++ "):
            path = _diff_path(raw[4:])
        elif hunk := _HUNK.match(raw):
            line, in_hunk = int(hunk.group(1)), True
        elif in_hunk and path is not None:
            if raw.startswith("+"):
                found.append(AddedLine(path, line, raw[1:]))
                line += 1
            elif raw.startswith(" ") or raw == "":
                line += 1
    return found


def _diff_path(text: str) -> str | None:
    text = text.strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    if text == "/dev/null":
        return None
    return text[2:] if text.startswith("b/") else text


def builtin_checks(diff_text: str, *, scanner: SecretScanner, blocked_licences: list[str]) -> dict:
    """The three pure-Python checks over the lines `diff_text` adds, as one document."""
    lines = added_lines(diff_text)
    tools: list[dict] = []
    sources: list[dict] = []
    for name, (diagnostics, detail) in (
        ("secret-scan", (_secrets(lines, scanner), "")),
        ("licence-check", _licences(lines, blocked_licences)),
        ("ignore-rules", (_ignore_rules(lines), "")),
    ):
        count = len(diagnostics)
        tools.append({"name": name, "status": STATUS_OK, "detail": detail, "findings": count})
        if diagnostics:
            sources.append({"source": {"name": name}, "diagnostics": diagnostics})
    return _document(tools, sources)


def _diagnostic(added: AddedLine, column: int, severity: str, code: str, message: str) -> dict:
    return {
        "message": message,
        "location": {
            "path": added.path,
            "range": {"start": {"line": added.line, "column": column}},
        },
        "severity": severity,
        "code": {"value": code},
    }


def _secrets(lines: list[AddedLine], scanner: SecretScanner) -> list[dict]:
    found = []
    for added in lines:
        try:
            hits = scanner.findings(added.text)
        except TimeoutError:
            message = "this line could not be scanned in time - treat it as holding a secret"
            found.append(_diagnostic(added, 1, "ERROR", "Unscannable", message))
            continue
        for hit in hits:
            message = f"{hit.kind} (the value is withheld)"
            found.append(_diagnostic(added, hit.start + 1, "ERROR", hit.detector, message))
    return found


def _licences(lines: list[AddedLine], blocked: list[str]) -> tuple[list[dict], str]:
    if not blocked:
        return [], "org policy blocks no licence (licences.yaml), so nothing is blocked"
    found = []
    for added in lines:
        for pattern in (_SPDX, *_MANIFEST):
            match = pattern.search(added.text)
            if match is None:
                continue
            expression = match.group("expr").strip()
            for licence in _blocked_ids(expression, blocked):
                message = (
                    f"licence {licence} is blocked by org policy (licences.yaml); "
                    f"declared as '{expression}'"
                )
                found.append(_diagnostic(added, match.start() + 1, "ERROR", licence, message))
            break
    return found, ""


def _blocked_ids(expression: str, blocked: list[str]) -> list[str]:
    ids = [t for t in _LICENCE_ID.findall(expression) if t.lower() not in _OPERATORS]
    rules = [b.lower() for b in blocked]
    return [
        licence
        for licence in dict.fromkeys(ids)
        if any(
            licence.lower() == rule or licence.lower().startswith((rule + "-", rule + "+"))
            for rule in rules
        )
    ]


def _ignore_rules(lines: list[AddedLine]) -> list[dict]:
    found = []
    for added in lines:
        if added.path.rsplit("/", 1)[-1] != ".gitignore":
            continue
        rule = added.text.strip()
        if not rule or rule.startswith("#"):
            continue
        message = (
            f"new ignore rule '{rule}': files it matches are hidden from `git add -A` and from "
            "this review - check that nothing the change needs is hidden"
        )
        found.append(_diagnostic(added, 1, "WARNING", "new-ignore-rule", message))
    return found


# ── documents ────────────────────────────────────────────────────────────────


def _document(tools: list[dict], sources: list[dict]) -> dict:
    for row in tools:
        if row.get("status") != STATUS_OK:
            row["findings"] = None  # could not look: never a count that reads as clean
    total = sum(row["findings"] for row in tools if isinstance(row.get("findings"), int))
    by_severity: dict[str, int] = {}
    for source in sources:
        if source["source"]["name"].endswith(PRE_EXISTING_SUFFIX):
            continue
        for diagnostic in source["diagnostics"]:
            key = str(diagnostic.get("severity", ""))
            by_severity[key] = by_severity.get(key, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "tools": tools,
        "sources": sources,
        "summary": {"total": total, "by_severity": dict(sorted(by_severity.items()))},
        "not_run": [str(row.get("name")) for row in tools if row.get("status") != STATUS_OK],
    }


def merge(*documents: dict) -> dict:
    """One document from several: their tools and sources, the summary recomputed."""
    tools = [dict(row) for doc in documents for row in doc.get("tools") or []]
    sources = [source for doc in documents for source in doc.get("sources") or []]
    return _document(tools, sources)


def prefixed(document: dict, prefix: str) -> dict:
    """`document` with every finding's path under `prefix` (a component of the run)."""
    sources = []
    for source in document.get("sources") or []:
        diagnostics = []
        for diagnostic in source["diagnostics"]:
            location = dict(diagnostic.get("location") or {})
            location["path"] = f"{prefix}/{location.get('path', '')}"
            diagnostics.append({**diagnostic, "location": location})
        sources.append({**source, "diagnostics": diagnostics})
    return {**document, "sources": sources}


def render(document: dict) -> str:
    """The block a reviewing agent reads: what ran, what did not, and every finding."""
    lines = [_status_line(row) for row in document.get("tools") or []]
    for source in document.get("sources") or []:
        name = source["source"]["name"]
        heading = f"### {name}"
        if name.endswith(PRE_EXISTING_SUFFIX):
            heading += " - context: these were there before the change"
        lines += ["", heading]
        shown = source["diagnostics"][:MAX_SHOWN]
        lines += [_finding_line(d) for d in shown]
        if extra := len(source["diagnostics"]) - len(shown):
            lines.append(f"- ... and {extra} more")
    intro = (
        "## Pre-pass results\n\n"
        "The framework ran these deterministic checks over the change before your review. "
        "Turn each real finding into a review finding. A check that did not run is not a clean "
        "result: report it as not run, never as clean."
    )
    return f"{intro}\n\n{untrusted(chr(10).join(lines), 'review pre-pass')}"


def _status_line(row: dict) -> str:
    name = row.get("name")
    if row.get("status") != STATUS_OK:
        detail = f" - {row['detail']}" if row.get("detail") else ""
        return f"- {name}: DID NOT RUN ({row.get('status')}){detail}"
    count = row.get("findings") or 0
    detail = f" ({row['detail']})" if row.get("detail") else ""
    return f"- {name}: ran, {count} finding{'' if count == 1 else 's'}{detail}"


def _finding_line(diagnostic: dict) -> str:
    location = diagnostic.get("location") or {}
    line = ((location.get("range") or {}).get("start") or {}).get("line", "?")
    code = (diagnostic.get("code") or {}).get("value", "")
    return (
        f"- `{location.get('path', '?')}:{line}` [{diagnostic.get('severity', '?')}] "
        f"{code}: {diagnostic.get('message', '')}"
    )


__all__ = ["AddedLine", "added_lines", "builtin_checks", "merge", "prefixed", "render"]
