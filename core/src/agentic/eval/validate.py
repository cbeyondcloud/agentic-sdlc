"""The corpus validators (M1-59): what a case must hold for its truth to be true.

`load_corpus` runs `case_problems` on every case, so a corpus the harness would measure
wrongly is refused before anyone measures with it:

  * every handoff a case gives the agent has its kind's fields (the agent reads them;
    an input need not carry the framework's envelope or the body sections);
  * a review case's diff is a well-formed unified diff - each hunk's line counts add
    up - and every seeded defect names a file in it, with its whole line range inside
    one hunk's new side (the lines the reviewer is shown);
  * a QA case's failing criteria exist in its requirements (`REQ-n.m` is criterion m of
    item `REQ-n`, as the QA agent is told) and at least one criterion is left passing,
    so marking every criterion failed cannot score perfectly unnoticed;
  * a dev task with a workspace carries its reference change (`expected.solution`) and
    every test it names lives in a file of the workspace. Whether those tests really
    fail without the change and pass with it is `agentic.eval.devtask.check_dev_task`:
    it runs them, so it is a test of the corpus, not part of every load.

Stdlib only: a unified diff's hunk headers and counts are all that is read.
"""

import os
import re
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from agentic.definitions import describe_validation_error
from agentic.eval.cases import EvalCase
from agentic.handoffs import BUILTIN_CATALOG
from agentic.handoffs.parse import ENVELOPE_FIELDS
from agentic.yamlio import YamlFileError, load_yaml, split_front_matter

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_CRITERION = re.compile(r"(.+)\.([1-9][0-9]*)")


def parse_hunks(diff: str) -> dict[str, list[tuple[int, int]]]:
    """The new-side line spans (first, last; 1-based) of every hunk, per file path as
    the `+++` line names it without its `b/` prefix. Raises ValueError when the text is
    not a unified diff or a hunk's line counts do not add up."""
    spans: dict[str, list[tuple[int, int]]] = {}
    file: str | None = None
    old_left = new_left = 0
    for number, line in enumerate(diff.replace("\r\n", "\n").split("\n"), start=1):
        if old_left or new_left:
            # inside a hunk; an empty line is a blank context line whose space was trimmed
            mark = line[:1] or " "
            if mark == "\\":  # "\ No newline at end of file"
                continue
            if mark not in " +-":
                raise ValueError(f"line {number}: the hunk ended before its line counts")
            old_left -= mark in " -"
            new_left -= mark in " +"
            if old_left < 0 or new_left < 0:
                raise ValueError(f"line {number}: more lines than the hunk header counts")
            continue
        if line.startswith("+++ "):
            name = line[4:].split("\t", 1)[0].strip()
            file = None if name == "/dev/null" else name.removeprefix("b/")
            if file is not None:
                spans.setdefault(file, [])
        elif match := _HUNK.match(line):
            if file is None:
                raise ValueError(f"line {number}: a hunk with no '+++' file before it")
            old_left = 1 if match.group(2) is None else int(match.group(2))
            start = int(match.group(3))
            new_left = 1 if match.group(4) is None else int(match.group(4))
            if new_left:
                spans[file].append((start, start + new_left - 1))
    if old_left or new_left:
        raise ValueError("the last hunk is shorter than its header counts")
    if not any(spans.values()):
        raise ValueError("no hunk found: a review diff is a unified diff (---/+++/@@)")
    return spans


def case_problems(root: Path, case: EvalCase) -> list[str]:
    """Everything wrong with a loaded case (its handoff files already read)."""
    problems = []
    for kind, text in sorted(case.inputs.handoffs.items()):
        problems += [f"handoff {kind}: {p}" for p in input_problems(kind, text)]
    if case.case_class == "review":
        problems += _review_problems(case)
    elif case.case_class == "qa":
        problems += _qa_problems(case)
    elif case.case_class == "dev_task":
        problems += _dev_task_problems(root, case)
    return problems


def input_problems(kind: str, text: str) -> list[str]:
    """Why `text` is not a usable `kind` input: front matter with that kind and its fields."""
    if kind not in BUILTIN_CATALOG.schemas:
        return [f"'{kind}' is not a handoff kind ({', '.join(BUILTIN_CATALOG.kinds())})"]
    try:
        header, _ = split_front_matter(text)
        meta, lines = load_yaml(header, first_line=2)
    except YamlFileError as error:
        return [f"not a {kind} document: {error}"]
    if not isinstance(meta, dict):
        return [f"not a {kind} document: the front matter holds no fields"]
    if meta.get("kind") != kind:
        return [f"the document is a {meta.get('kind')}, not {kind}"]
    data = {k: v for k, v in meta.items() if k not in ENVELOPE_FIELDS}
    try:
        BUILTIN_CATALOG.schema(kind).model.model_validate(data)
    except ValidationError as error:
        return describe_validation_error(error, kind, lines=lines)
    return []


def _review_problems(case: EvalCase) -> list[str]:
    if not case.inputs.diff:
        return ["a review case needs a diff (inputs.diff) to review"]
    try:
        spans = parse_hunks(case.inputs.diff)
    except ValueError as error:
        return [f"the diff is not a valid unified diff: {error}"]
    problems = []
    for defect in case.expected.seeded_defects:
        file = PurePosixPath(defect.file).as_posix()
        start, end = defect.lines
        if file not in spans:
            problems.append(f"seeded defect file {defect.file} is not in the diff")
        elif not any(first <= start and end <= last for first, last in spans[file]):
            problems.append(
                f"seeded defect {defect.file}:{start}-{end} is outside the diff's hunks "
                f"({', '.join(f'{a}-{b}' for a, b in spans[file])})"
            )
    return problems


def _qa_problems(case: EvalCase) -> list[str]:
    text = case.inputs.handoffs.get("requirements")
    if text is None or input_problems("requirements", text):
        return ["a qa case needs a valid requirements handoff to check criteria against"]
    header, _ = split_front_matter(text)
    meta, _ = load_yaml(header)
    criteria = {
        f"{item['id']}.{position}"
        for item in meta["items"]
        for position in range(1, len(item["acceptance_criteria"]) + 1)
    }
    problems = [
        f"failing criterion {c} is not a criterion of the requirements (REQ-n.m: criterion m "
        f"of item REQ-n)"
        for c in case.expected.failing_criteria
        if c not in criteria or not _CRITERION.fullmatch(c)
    ]
    if not problems and criteria <= set(case.expected.failing_criteria):
        problems.append(
            "every criterion is seeded failing: leave at least one criterion passing, so "
            "false alarms are measurable"
        )
    return problems


def _dev_task_problems(root: Path, case: EvalCase) -> list[str]:
    if case.inputs.workspace is None:
        return []
    problems = []
    if case.expected.solution is None:
        problems.append(
            "a dev task with a workspace needs its reference change (expected.solution), "
            "so its tests can be shown to fail without it and pass with it"
        )
    workspace = root / PurePosixPath(case.inputs.workspace)
    tests = case.expected.tests
    for test in [*tests.fail_to_pass, *tests.pass_to_pass] if tests else []:
        file = test.split("::", 1)[0]
        if not _is_file_exactly(workspace, PurePosixPath(file)):
            problems.append(f"test {test}: {file} is not a file of the workspace")
    return problems


def _is_file_exactly(root: Path, relative: PurePosixPath) -> bool:
    """`root / relative` is a file, every component spelled as on disk (case-exact on
    Windows and macOS too, as on the Linux runner)."""
    if relative.is_absolute() or ".." in relative.parts:
        return False
    here = root
    for part in relative.parts:
        try:
            if part not in os.listdir(here):
                return False
        except OSError:
            return False
        here = here / part
    return here.is_file()


__all__ = ["case_problems", "input_problems", "parse_hunks"]
