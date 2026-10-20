"""What every tool returns, made safe to show the model - in one place (M1-41, M1-42).

`build_tools` passes every tool through `protect_tools`; tool modules stay as
they are. Each string a tool returns is, in order:

  1. shown with the run's tree as `.` - an absolute host path tells the model
     (and its provider) about this machine (DF-23);
  2. secret-scanned under the run's policy: a secret is masked, or the whole
     output withheld with a refusal the model can read and work around (DF-23);
     the transport scan stays as the last line;
  3. wrapped as untrusted data (`<untrusted-data source="<tool>">`), unless it is
     a framework refusal or the tool is trusted: `activate_skill` returns
     org-approved instructions.

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

from agentic.agents.tools.context import refused
from agentic.security import SecretBlocked, SecretScanner
from agentic.security.untrusted import untrusted

#: Tools whose output is instructions the org approved, not data.
TRUSTED_TOOLS = frozenset({"activate_skill"})
_REFUSED = refused("")


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

    def finish(result: object) -> object:
        if not isinstance(result, str):
            return result
        text = paths.sub(".", result) if paths else result
        try:
            text = scanner.check_text(text, where)
        except SecretBlocked as blocked:
            return refused(
                f"the output of {tool.name} holds {blocked.total} secret(s) "
                f"({', '.join(blocked.kinds)}) and was withheld by the secret_scan policy"
            )
        if not label or text.startswith(_REFUSED):
            return text
        return untrusted(text, tool.name)

    function = tool.function
    if inspect.iscoroutinefunction(function):

        @functools.wraps(function)
        async def run_async(*args, **kwargs):
            return finish(await function(*args, **kwargs))

        wrapped = run_async
    else:

        @functools.wraps(function)
        def run(*args, **kwargs):
            return finish(function(*args, **kwargs))

        wrapped = run
    protected = copy.copy(tool)
    protected.function = wrapped
    protected.function_schema = dataclasses.replace(tool.function_schema, function=wrapped)
    return protected


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
