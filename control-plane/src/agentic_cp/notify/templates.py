"""Mail templates (M2-07): plain text and a simple HTML part from package data.

`string.Template` cannot run code; every value is HTML-escaped in the HTML part. A mail
carries no image, no remote stylesheet and no tracking of any kind: the only link is the
one the mail is about (an invite's), written out in full in both parts."""

from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from string import Template

#: Each kind and its template files' stem.
_FILES = {
    "invite": "invite",
    "gate.waiting": "gate_waiting",
    "budget.threshold": "budget",
    "auto_trip": "auto_trip",
}
KINDS = tuple(_FILES)

_SUBJECTS = {
    "invite": "You are invited to $tenant_name's agentic workspace",
    "gate.waiting": "Run $run_id is waiting for approval at $gate",
    "budget.threshold": "$what has used $percent % of its budget",
    "auto_trip": "Team $team is paused: its $what passed the auto-trip threshold",
}


@dataclass(frozen=True)
class Rendered:
    subject: str
    text: str
    html: str


def _template(stem: str, suffix: str) -> Template:
    source = resources.files("agentic_cp.notify").joinpath("templates", f"{stem}.{suffix}")
    return Template(source.read_text(encoding="utf-8").replace("\r\n", "\n"))


def render(kind: str, values: Mapping[str, str], *, tenant_name: str) -> Rendered:
    """The mail of `kind` with `values` (every placeholder must be given)."""
    if kind not in _FILES:
        raise ValueError(f"unknown mail kind {kind!r}")
    given = {**values, "tenant_name": tenant_name}
    subject = " ".join(Template(_SUBJECTS[kind]).substitute(given).split())
    text = _template(_FILES[kind], "txt").substitute(given)
    escaped = {key: html.escape(str(value), quote=True) for key, value in given.items()}
    body = _template(_FILES[kind], "html").substitute(escaped)
    return Rendered(subject=subject[:200], text=text, html=body)
