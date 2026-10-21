"""What every tool returns, made safe to show the model - in one place (M1-41, M1-42).

`build_tools` passes every tool through `protect_tools`; tool modules stay as
they are. A call is refused before it runs when its arguments carry a mask
marker (`[MASKED SECRET: ...]` - written back, it would replace the real secret)
or a secret, whatever the policy's action (written to a file, run as a command
or handed to a child, it would leak from there; the model is told to use a
placeholder, and the run goes on - review F2). What a tool returns (text;
anything else as JSON) is, in order:

  1. shown with the run's tree as `.` - an absolute host path tells the model
     (and its provider) about this machine (DF-23); except a file's own content
     (`read_file`), shown verbatim, because an edit copies its old text from it and
     a rewritten path would never be found (DF-156);
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

import asyncio
import copy
import dataclasses
import functools
import inspect
import os
import re
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

from pydantic_ai import Tool
from pydantic_core import to_json

from agentic.agents.tools.context import Refusal, refused
from agentic.security import SecretBlocked, SecretScanner
from agentic.security.secret_scan import MASKED
from agentic.security.untrusted import untrusted

#: Tools whose output is instructions the org approved, not data.
TRUSTED_TOOLS = frozenset({"activate_skill"})
#: Tools whose output is a file's content: its paths are not rewritten (DF-156).
VERBATIM_TOOLS = frozenset({"read_file"})


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
        text = paths.sub(".", text) if paths and tool.name not in VERBATIM_TOOLS else text
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
            if why := _refused_call(scanner, tool.name, args, kwargs):
                return why
            result = await function(*args, **kwargs)
            return await asyncio.to_thread(finish, result)  # the scan: off the loop (DF-153)

        wrapped = run_async
    else:

        @functools.wraps(function)
        def run(*args, **kwargs):
            if why := _refused_call(scanner, tool.name, args, kwargs):
                return why
            return finish(function(*args, **kwargs))

        wrapped = run
    protected = copy.copy(tool)
    protected.function = wrapped
    protected.function_schema = dataclasses.replace(tool.function_schema, function=wrapped)
    return protected


def _refused_call(scanner: SecretScanner, name: str, args: tuple, kwargs: dict) -> str | None:
    """A refusal when an argument carries a mask marker (written back to a file it would
    replace the real secret - mask mode shows the model only markers) or a secret."""
    values = [*args, *kwargs.values()]
    if any(MASKED.search(text) for text in _strings(values)):
        return refused(
            "an argument holds a masked secret marker - the real value was withheld from "
            "you, so it cannot be written; leave that text as it is and change only the rest"
        )
    if blocked := scanner.refusal(values, f"tool-call:{name}"):
        return refused(
            f"an argument holds {blocked.total} secret(s) ({', '.join(blocked.kinds)}); "
            "secrets are never written to files, commands or other agents. Use a "
            "placeholder (such as an environment variable reference) instead"
        )
    return None


def _strings(value: object) -> Iterator[str]:
    """Every string in `value`, nested lists and dicts included (keys too)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)


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
