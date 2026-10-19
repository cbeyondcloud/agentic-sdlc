"""The one place every framework file (agent, pipeline, policy, handoff) is read.

Why one reader:
  * duplicate keys are refused — otherwise a second `deny:` block silently
    replaces the first and loosens a guardrail;
  * unsafe tags (e.g. `!!python/object/apply`) are refused;
  * a byte-order mark (common from Windows editors) is ignored;
  * a file that is not UTF-8 gives a clear message instead of a crash;
  * every key's line number is remembered, so validation errors can say
    "line 7" and a person editing at a gate knows exactly where to look.

Each document is parsed twice: in strict safe mode (the values actually used)
and in round-trip mode only to learn line numbers. YAML 1.2 rules apply, so
`yes`/`no` stay strings.
"""

import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML, YAMLError
from ruamel.yaml.comments import CommentedMap, CommentedSeq

Loc = tuple[str | int, ...]


class YamlFileError(Exception):
    """A file could not be read or is not valid YAML. The message is user-ready."""


def read_text(path: Path) -> str:
    """Read a text file as UTF-8, ignoring a leading byte-order mark."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as error:
        raise YamlFileError(
            f"file is not UTF-8 text (byte {error.start}); save it as UTF-8"
        ) from error


def load_yaml(text: str, first_line: int = 1) -> tuple[Any, dict[Loc, int]]:
    """Parse YAML. Returns (plain data, {key path: line number in the file}).

    first_line is the file line where `text` starts (2 for front matter, which
    follows a '---' line), so reported line numbers match the user's editor.
    """
    try:
        data = YAML(typ="safe", pure=True).load(text)
        return data, _line_map(YAML(typ="rt").load(text), first_line)
    except YAMLError as error:
        raise YamlFileError(_describe_yaml_error(error, first_line)) from error
    except RecursionError as error:
        raise YamlFileError("the YAML is nested too deeply") from error


def dump_yaml(data: Mapping[str, Any]) -> str:
    """Block-style YAML for documents the framework writes: keys in the given order,
    UTF-8 text kept, long lines never folded, LF line endings."""
    yaml = YAML(typ="safe", pure=True)
    yaml.default_flow_style = False
    yaml.allow_unicode = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.representer.sort_base_mapping_type_on_output = False
    out = io.StringIO()
    yaml.dump(dict(data), out)
    return out.getvalue()


def split_front_matter(text: str) -> tuple[str, str]:
    """Split '---\\n<yaml>\\n---\\n<body>' into (yaml, body). Header starts on line 2."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise YamlFileError("missing front matter: the file must start with a '---' line")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            header = "\n".join(lines[1:index])
            return header, "\n".join(lines[index + 1 :]).strip()
    raise YamlFileError("front matter is not closed: add a '---' line after the header")


def line_of(lines: dict[Loc, int], loc: Loc) -> int | None:
    """Line of the deepest known part of `loc` (e.g. a missing field's parent)."""
    for end in range(len(loc), 0, -1):
        if loc[:end] in lines:
            return lines[loc[:end]]
    return None


def _describe_yaml_error(error: YAMLError, first_line: int) -> str:
    # problem_mark is where parsing failed. For an unclosed quote or bracket that is
    # the end of the text, so the construct's start (context_mark) is added: that is
    # the line to fix. A "block" context is just the enclosing mapping — not useful.
    mark = getattr(error, "problem_mark", None)
    problem = getattr(error, "problem", None) or str(error).splitlines()[0]
    if "duplicate key" in str(error):
        problem = f"duplicate key ({problem}); each setting may appear only once"
    message = f"line {mark.line + first_line}: {problem}" if mark else problem
    context = getattr(error, "context", None) or ""
    context_mark = getattr(error, "context_mark", None)
    if context_mark and context and "block" not in context and "duplicate" not in problem:
        message += f" (started at line {context_mark.line + first_line}: {context})"
    return message


def _line_map(node: Any, first_line: int, prefix: Loc = ()) -> dict[Loc, int]:
    lines: dict[Loc, int] = {}
    if isinstance(node, CommentedMap):
        for key in node:
            try:
                lines[prefix + (key,)] = node.lc.key(key)[0] + first_line
            except KeyError:
                continue  # a key merged in by '<<: *alias' has no position of its own
            lines.update(_line_map(node[key], first_line, prefix + (key,)))
    elif isinstance(node, CommentedSeq):
        for index, item in enumerate(node):
            lines[prefix + (index,)] = node.lc.item(index)[0] + first_line
            lines.update(_line_map(item, first_line, prefix + (index,)))
    return lines
