"""What every tool returns, made safe to show the model - in one place (M1-41, M1-42).

`build_tools` passes every tool through `protect_tools`; tool modules stay as
they are. A call whose arguments carry a mask marker (`[MASKED SECRET: ...]`)
is refused: written back, it would replace the real secret. What a tool returns
(text; anything else as JSON) is, in order:

  1. shown with the run's tree as `.` - an absolute host path tells the model
     (and its provider) about this machine (DF-23);
  2. secret-scanned under the run's policy: a secret is masked, or the whole
     output withheld with a refusal the model can read and work around (DF-23);
     the transport scan stays as the last line;
  3. wrapped as untrusted data (`<untrusted-data source="<tool>">`), unless the
     tool is trusted: `activate_skill` returns org-approved instructions.

A framework refusal (`Refusal`, known by its type, never by its text) is only
path-scrubbed: it is the framework speaking, not data.

A protected tool is a copy with the same name, schema, description and approval
settings; only the function it calls is wrapped.
"""

import copy
import dataclasses
import functools
import inspect
import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic_ai import Tool
from pydantic_core import to_json

from agentic.agents.tools.context import Refusal, refused
from agentic.security import SecretBlocked, SecretScanner
from agentic.security.secret_scan import MASKED
from agentic.security.untrusted import untrusted

#: Tools whose output is instructions the org approved, not data.
TRUSTED_TOOLS = frozenset({"activate_skill"})


def protect_tools(
    tools: Iterable[Tool],
    *,
    scanner: SecretScanner | None,
    tree: Path | None = None,
    trusted: frozenset[str] = TRUSTED_TOOLS,
) -> list[Tool]:
    """`tools`, each returning scrubbed, scanned and labelled text (see the module)."""
    scanner = scanner or SecretScanner()
    paths = _host_paths(tree)
    return [_protected(tool, scanner, paths, label=tool.name not in trusted) for tool in tools]


def _protected(tool: Tool, scanner: SecretScanner, paths: re.Pattern | None, label: bool) -> Tool:
    where = f"tool:{tool.name}"

    def finish(result: object) -> str:
        if isinstance(result, Refusal):
            return paths.sub(".", result) if paths else result  # written by us: not data
        text = result if isinstance(result, str) else to_json(result, fallback=str).decode()
        text = paths.sub(".", text) if paths else text
        try:
            text = scanner.check_text(text, where)
        except SecretBlocked as blocked:
            return refused(
                f"the output of {tool.name} holds {blocked.total} secret(s) "
                f"({', '.join(blocked.kinds)}) and was withheld by the secret_scan policy"
            )
        return untrusted(text, tool.name) if label else text

    function = tool.function
    if inspect.iscoroutinefunction(function):

        @functools.wraps(function)
        async def run_async(*args, **kwargs):
            if why := _copies_a_mask(args, kwargs):
                return why
            return finish(await function(*args, **kwargs))

        wrapped = run_async
    else:

        @functools.wraps(function)
        def run(*args, **kwargs):
            if why := _copies_a_mask(args, kwargs):
                return why
            return finish(function(*args, **kwargs))

        wrapped = run
    protected = copy.copy(tool)
    protected.function = wrapped
    protected.function_schema = dataclasses.replace(tool.function_schema, function=wrapped)
    return protected


def _copies_a_mask(args: tuple, kwargs: dict) -> str | None:
    """A refusal when an argument carries a mask marker: written back to a file it would
    replace the real secret with the marker (mask mode shows the model only markers)."""
    if any(isinstance(v, str) and MASKED.search(v) for v in (*args, *kwargs.values())):
        return refused(
            "an argument holds a masked secret marker - the real value was withheld from "
            "you, so it cannot be written; leave that text as it is and change only the rest"
        )
    return None


def _host_paths(tree: Path | None) -> re.Pattern | None:
    """The tree's absolute path as it may be printed: native, POSIX, and with the
    doubled backslashes of a repr - given and resolved - longest first."""
    if tree is None:
        return None
    forms: set[str] = set()
    for path in {Path(tree), Path(tree).resolve()}:
        native = str(path)
        forms |= {native, path.as_posix(), native.replace("\\", "\\\\")}
    ordered: Sequence[str] = sorted((f for f in forms if len(f) > 3), key=len, reverse=True)
    if not ordered:
        return None
    flags = re.IGNORECASE if os.name == "nt" else 0  # Windows paths are case-insensitive
    alternatives = "|".join(re.escape(form) for form in ordered)
    return re.compile(f"(?:{alternatives})(?![A-Za-z0-9_.-])", flags)
