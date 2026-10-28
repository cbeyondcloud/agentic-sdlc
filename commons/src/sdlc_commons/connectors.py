"""Connector adapter over Zyroflo's JSON-path mappings (M2-27; D34: reuse, never rewrite).

An external item - a Jira issue, a GitHub issue, a firing alert - becomes a run's
title and specification through a *mapping*: a JSON file saying where in the
payload the title, body, id and link are (`{"title": "fields.summary", ...}`).
Zyroflo (`tools.sdlc.connect`) owns the format and every rule about it; this
module only puts it behind the framework's names:

  * shipped_mappings() - the Jira, GitHub-issue and alert mappings Zyroflo ships
                         as content (`sdlc_templates/connectors/`);
  * mapping_for()      - a repo's own mapping, read and checked by Zyroflo's
                         `mapping_for` from `<repo>/.sdlc/connectors/<source>.json`
                         (Zyroflo's fixed folder - a folder parameter is Zyroflo
                         PR Z8), else the shipped one;
  * item_for()         - the item: Zyroflo's deterministic `spec_for` text, the
                         declared kind, the external id and link (`dig`), and
                         Zyroflo's `spec_hash` as its identity - the same item seen
                         twice has the same identity; a body in Atlassian Document
                         Format (Jira Cloud REST v3) is read as its text (`adf_text`);
  * dig / ABSENT      - Zyroflo's dotted-path reader and its "not there" sentinel
                         (a JSON `null` is a value, not an absence).

Zyroflo's `ConnectError` becomes `ConnectorError`, with Zyroflo's own words.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.sdlc import connect, content, intake

ABSENT = connect.ABSENT
dig = connect.dig

#: The sources Zyroflo ships a mapping for.
SHIPPED = ("alert", "github-issue", "jira")
_SHIPPED_DIR = Path("sdlc_templates") / "connectors"
_SOURCE = re.compile(r"[a-z0-9][a-z0-9_-]*")


class ConnectorError(Exception):
    """A mapping or a payload could not be read, or carries no title."""


@dataclass(frozen=True)
class ExternalItem:
    """One external item, ready to start a run from."""

    source: str  # the connector's name ("jira", "github-issue", ...)
    kind: str  # the mapping's declared kind ("requirement", "defect", ...)
    title: str
    spec: str  # Zyroflo's specification text: deterministic, never per-invocation
    identity: str  # Zyroflo's spec_hash over title and spec ("sha256:...")
    external_id: str | None  # the item's id in its source ("PAY-42", "7"), if mapped
    url: str | None  # where the item lives, if mapped


def shipped_mappings() -> dict[str, dict]:
    """Zyroflo's shipped mappings by source name, each as written (with its `_comment`)."""
    return {source: _shipped(source) for source in SHIPPED}


def mapping_for(repo: Path | None, source: str) -> dict:
    """The repo's mapping for `source`, else Zyroflo's shipped one. Raises ConnectorError.

    `source` names one file in the connectors folder: a lower-case name of letters,
    digits, `-` and `_` - never a path, so no spelling reaches a file outside it and a
    name means the same file on every OS (final review F3)."""
    if not isinstance(source, str) or not _SOURCE.fullmatch(source):
        raise ConnectorError(
            f"{source!r} is not a connector name: use lower-case letters, digits, '-' and "
            "'_', starting with a letter or digit (e.g. 'jira', 'github-issue')"
        )
    declared = Path(repo) / connect.CONNECTORS / f"{source}.json" if repo is not None else None
    if declared is not None and declared.exists():
        try:
            return connect.mapping_for(Path(repo), source)
        except connect.ConnectError as error:
            raise ConnectorError(str(error)) from error
    if source in SHIPPED:
        return _shipped(source)
    where = declared if declared is not None else connect.CONNECTORS / f"{source}.json"
    raise ConnectorError(
        f"no connector called {source!r}: {where} does not exist and Zyroflo ships "
        f"mappings only for {', '.join(SHIPPED)}. A connector is a mapping you write - "
        "the dotted paths of the title, body, id and link in your source's payload"
    )


def item_for(source: str, mapping: dict, payload: dict) -> ExternalItem:
    """The external item `payload` describes, read through `mapping`. Raises ConnectorError.

    A body in Atlassian Document Format (Jira Cloud's REST v3 and its webhooks) is read as
    its text first (DF-462): Zyroflo's `spec_for` would otherwise put the dict's repr in
    the specification, and the identity would be computed over that."""
    if missing := [key for key in connect.REQUIRED if not str(mapping.get(key) or "").strip()]:
        raise ConnectorError(f"the {source!r} mapping declares no {', '.join(missing)}")
    body = str(mapping.get("body") or "")
    if body and _is_adf(found := dig(payload, body)):
        payload = _replaced(payload, body.split("."), adf_text(found))
    try:
        title, spec = connect.spec_for(source, mapping, payload)
    except (connect.ConnectError, KeyError, TypeError) as error:
        raise ConnectorError(str(error)) from error
    return ExternalItem(
        source=source,
        kind=connect.declared_kind(mapping),
        title=title,
        spec=spec,
        identity=intake.spec_hash(title, spec),
        external_id=_text(payload, mapping.get("id")),
        url=_text(payload, mapping.get("url")),
    )


#: ADF nodes that sit inside a line, and the attribute holding their text (None: none).
_INLINE = {
    "text": "text",
    "hardBreak": None,
    "mention": "text",
    "emoji": "shortName",
    "inlineCard": "url",
    "status": "text",
    "date": "timestamp",
}


def adf_text(node: Any) -> str:
    """The text of an Atlassian Document Format node: each paragraph, heading or code
    block on its own line(s), list items as `- ` lines, hard breaks as new lines, and
    mentions, emoji, cards and statuses by their text. Formatting marks are dropped."""
    return "\n".join(_lines(node))


def _lines(node: Any) -> list[str]:
    if not isinstance(node, dict):
        return []
    kind = node.get("type")
    if kind in _INLINE:
        return _inline(node).split("\n")
    content = node.get("content")
    children = (
        [child for child in content if isinstance(child, dict)] if isinstance(content, list) else []
    )
    if kind == "listItem":
        inner = [line for child in children for line in _lines(child)]
        return [f"- {inner[0]}", *inner[1:]] if inner else ["-"]
    if children and all(child.get("type") in _INLINE for child in children):
        return "".join(_inline(child) for child in children).split("\n")
    return [line for child in children for line in _lines(child)]


def _inline(node: dict) -> str:
    kind = node.get("type")
    if kind == "hardBreak":
        return "\n"
    if kind == "text":
        return str(node.get("text") or "")
    attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
    return str(attrs.get(_INLINE[kind]) or node.get("text") or "")


def _is_adf(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("type") == "doc"
        and isinstance(value.get("content"), list)
    )


def _replaced(node: Any, parts: list[str], value: str) -> Any:
    """`node` with the value at the dotted path `parts` replaced: the containers along
    the path are copied, so the caller's payload is never changed. The path exists (it
    was just read with `dig`)."""
    copy = dict(node) if isinstance(node, dict) else list(node)
    key: Any = parts[0] if isinstance(node, dict) else int(parts[0])
    copy[key] = value if len(parts) == 1 else _replaced(node[key], parts[1:], value)
    return copy


def _text(payload: dict, path: Any) -> str | None:
    if not path:
        return None
    found = dig(payload, str(path))
    return None if found is None or str(found).strip() == "" else str(found)


def _shipped(source: str) -> dict:
    path = content.root() / _SHIPPED_DIR / f"{source}.json"
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        message = f"Zyroflo's shipped mapping {path} could not be read: {error}"
        raise ConnectorError(message) from error
    if not isinstance(found, dict) or not str(found.get("title") or "").strip():
        raise ConnectorError(f"Zyroflo's shipped mapping {path} declares no title")
    return found
