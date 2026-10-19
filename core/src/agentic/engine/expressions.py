"""Pipeline conditions: `when` and `until` (M1-23, spike S13).

Conditions are CEL (Common Expression Language), evaluated by cel-python: no
loops, no I/O, no host objects, typed with no implicit coercion. They read the
front-matter fields of the handoffs produced so far, by kind:

    when:  "requirements.size != 'trivial'"
    until: "review.open_blocking == 0"

CEL names cannot hold '-', so a kind with a dash is read with an underscore:
`dev_report.tests_run.failed == 0` (see `cel_name`).

A condition is compiled when the pipeline is loaded, so preflight reports bad
syntax before any spend; it is evaluated with the run's handoffs. A missing kind
or field is an error, never silently false - conditions test optional fields
with `has(review.score)`. This is the only module that imports `celpy`.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import celpy
from celpy import celtypes

_STRINGS = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_ROOT_NAME = re.compile(r"(?<![.\w])([A-Za-z_]\w*)(?!\w)(?!\s*\()")
_NOT_NAMES = frozenset({"true", "false", "null", "in"})
_ENV = celpy.Environment()


class ExpressionError(Exception):
    """A condition that cannot be compiled or evaluated. Names the expression."""

    def __init__(self, expression: str, reason: str) -> None:
        self.expression, self.reason = expression, reason
        super().__init__(f"{expression}: {reason}")


@dataclass(frozen=True)
class Condition:
    text: str
    kinds: frozenset[str]  # the handoff kinds it reads, e.g. {"review"}
    _program: Any = field(repr=False, compare=False)

    def evaluate(self, context: Mapping[str, Mapping[str, object]]) -> bool:
        """True or False over `{kind: front-matter fields}`; ExpressionError otherwise."""
        variables = {cel_name(kind): fields for kind, fields in context.items()}
        try:
            result = self._program.evaluate(celpy.json_to_cel(variables))
        except celpy.CELEvalError as error:
            raise ExpressionError(self.text, _reason(error)) from error
        except (RecursionError, TypeError, ValueError) as error:
            raise ExpressionError(self.text, f"{type(error).__name__}: {error}") from error
        if isinstance(result, celpy.CELEvalError):
            raise ExpressionError(self.text, _reason(result))
        if not isinstance(result, celtypes.BoolType):
            kind = type(result).__name__.removesuffix("Type").lower()
            article = "an" if kind[:1] in "aeiou" else "a"
            raise ExpressionError(self.text, f"the result is {article} {kind}, not a boolean")
        return bool(result)


def compile_condition(text: str) -> Condition:
    """The compiled condition, or ExpressionError for an empty or malformed one."""
    if not isinstance(text, str) or not text.strip():
        raise ExpressionError(str(text), "the condition is empty")
    try:
        program = _ENV.program(_ENV.compile(text))
    except celpy.CELParseError as error:
        raise ExpressionError(text, f"not a valid condition: {_first_line(error)}") from error
    except RecursionError as error:
        raise ExpressionError(text, "the condition is nested too deeply") from error
    kinds = {name for name in _ROOT_NAME.findall(_STRINGS.sub("''", text))}
    return Condition(text=text, kinds=frozenset(kinds - _NOT_NAMES), _program=program)


def cel_name(kind: str) -> str:
    """How a condition names a handoff kind: CEL names cannot hold '-', so
    `dev-report` is read as `dev_report` (and `test-report` as `test_report`)."""
    return kind.replace("-", "_")


def _reason(error: Exception) -> str:
    """celpy's message without the evaluation context it appends (its whole name table)."""
    args = getattr(error, "args", ())
    message = str(args[0]) if args else str(error)
    message = message.split(" (in activation", 1)[0]
    if message.startswith("undeclared reference to "):
        return f"{message}: no earlier step produced that handoff kind"
    return message


def _first_line(error: Exception) -> str:
    return str(error).strip().split("\n")[0]
