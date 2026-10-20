"""The eval case format (M1-59, spike S10): one YAML file per case.

A case says which agent it exercises, what the agent is given, and what the truth is
- mechanical wherever it exists (seeded defects, tests, seeded failing criteria), a
reference and key points for a rubric judge where it does not (BA, Design).

    id: export-csv-01
    agent: business-analyst          # the agent definition's name
    case_class: scoping              # scoping | review | defect | dev_task | qa
    split: holdout                   # dev: iterate on prompts and skills; holdout: gates, A/Bs
    source: {repo: synthetic, synthetic: true, licence: CC0-1.0}   # + classification: <class>
    inputs:
      prompt: Add CSV export to the invoices page.
      handoffs: {requirements: "---\\nkind: requirements ..."}   # consumed handoffs, by kind
      handoff_files: {design: golden/design/01-export.md}      # ... or files holding them
      diff: null                     # a review case's change, unified diff
      workspace: null                # a folder in the corpus with the starting files
    expected:
      golden: golden/requirements/01-export-csv.md   # a reference handoff (corpus path)
      key_points: [the export runs as a background job]
      seeded_defects: [{file: billing/export.py, lines: [40, 44], category: logic}]
      tests: {fail_to_pass: [tests/test_export.py::test_large], pass_to_pass: []}
      failing_criteria: [REQ-1.2]
    rubric: ba-requirements          # the rubric id, for rubric-judged agents
    tags: [billing]

Provenance (repo, sha, parent, `synthetic`, licence) is required: `library/eval/` may
hold only synthetic or permissively licensed cases (decision 17).
"""

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CaseClass = Literal["scoping", "review", "defect", "dev_task", "qa"]
Split = Literal["dev", "holdout"]
_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CaseSource(_Strict):
    repo: str  # a public repo URL, an internal repo name, or "synthetic"
    sha: str | None = None  # the commit the case is taken from
    parent_sha: str | None = None  # the commit the agent starts from
    synthetic: bool
    licence: str  # SPDX id of the case content (CC0-1.0 for our synthetic cases)
    # the data class of the repo the case comes from (org policy classifications); None:
    # the org's default class. It decides which tiers - the judge's included - may see it.
    classification: str | None = None


class SeededDefect(_Strict):
    file: str
    lines: tuple[int, int]  # first and last line of the defect, 1-based, inclusive
    category: str  # e.g. logic, security, error-handling, concurrency, performance
    note: str = ""  # what is wrong, for people reading the case

    @field_validator("lines")
    @classmethod
    def ordered(cls, value: tuple[int, int]) -> tuple[int, int]:
        start, end = value
        if start < 1 or end < start:
            raise ValueError("lines must be [start, end] with 1 <= start <= end")
        return value


class TestTruth(_Strict):
    __test__ = False  # not a pytest test class, despite the name

    fail_to_pass: list[str] = Field(min_length=1)  # must pass after the change
    pass_to_pass: list[str] = []  # passed before and must still pass


class CaseInputs(_Strict):
    prompt: str
    handoffs: dict[str, str] = {}  # consumed handoff documents, by kind
    # ... or corpus-relative files holding them (read into `handoffs` on load)
    handoff_files: dict[str, str] = {}
    diff: str | None = None
    workspace: str | None = None  # corpus-relative folder with the starting files


class Expected(_Strict):
    golden: str | None = None  # corpus-relative path of a reference handoff
    key_points: list[str] = []
    seeded_defects: list[SeededDefect] = []
    tests: TestTruth | None = None
    failing_criteria: list[str] = []  # acceptance criterion ids the work does NOT meet


class EvalCase(_Strict):
    id: str
    agent: str
    case_class: CaseClass
    split: Split
    source: CaseSource
    inputs: CaseInputs
    expected: Expected = Expected()
    rubric: str | None = None
    tags: list[str] = []

    @field_validator("id")
    @classmethod
    def id_is_a_slug(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("a case id is lower-case letters, digits, '.', '_' or '-'")
        return value

    @field_validator("rubric")
    @classmethod
    def rubric_is_a_slug(cls, value: str | None) -> str | None:
        """The id becomes `rubrics/<id>.yaml`, read from the corpus and copied next to the
        report: a separator or '..' would reach outside both."""
        if value is not None and (not _ID.fullmatch(value) or ".." in value):
            raise ValueError(
                "a rubric id is lower-case letters, digits, '.', '_' or '-' (rubrics/<id>.yaml)"
            )
        return value

    @model_validator(mode="after")
    def truth_for_its_class(self) -> Self:
        """Each class carries the truth its metric needs; a case without it measures nothing."""
        expected = self.expected
        if self.case_class == "scoping":
            if not (expected.key_points or expected.golden):
                raise ValueError("a scoping case needs key_points or a golden handoff")
            if not self.rubric:
                raise ValueError("a scoping case needs a rubric (it is judged)")
        elif self.case_class == "review" and not expected.seeded_defects:
            raise ValueError("a review case needs at least one seeded defect")
        elif self.case_class in ("dev_task", "defect") and expected.tests is None:
            raise ValueError(f"a {self.case_class} case needs tests (fail_to_pass)")
        elif self.case_class == "qa" and not expected.failing_criteria:
            raise ValueError("a qa case needs at least one failing criterion")
        return self

    @property
    def truth_count(self) -> int:
        """How many units of mechanical truth the case carries (S10 floors count these)."""
        if self.case_class == "review":
            return len(self.expected.seeded_defects)
        if self.case_class == "qa":
            return len(self.expected.failing_criteria)
        if self.case_class in ("dev_task", "defect") and self.expected.tests:
            return len(self.expected.tests.fail_to_pass)
        return 0

    def references(self) -> list[str]:
        """Corpus-relative paths this case points at."""
        paths = [self.expected.golden, self.inputs.workspace, *self.inputs.handoff_files.values()]
        return [p for p in paths if p]
