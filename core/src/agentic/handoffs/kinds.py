"""The five handoff kinds and their structured fields (spec §7.2).

The front matter holds these fields for machines (pipeline conditions, gates,
dashboards); the Markdown body below it holds the readable document for people.
Every model rejects unknown fields, so a misspelt field is caught.

Every kind inherits `HandoffBase` (D37, spec §7.4): a one-line `summary` and the
`open_questions` every handoff may carry.
"""

from collections.abc import Iterable
from typing import ClassVar, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HandoffBase(_Strict):
    """The fields of the generic base template, inherited by every kind."""

    summary: str  # one line: what this handoff says, for gates and dashboards
    open_questions: list[str] = []

    @field_validator("summary")
    @classmethod
    def summary_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("the summary must not be blank")
        return value


class TestCounts(_Strict):
    __test__ = False  # not a pytest test class, despite the name

    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(default=0, ge=0)


# --- requirements: produced by the Business Analyst ----------------------------


class Requirement(_Strict):
    id: str
    statement: str
    # EARS style, e.g. "WHEN a user requests an export THE system SHALL return a job id"
    acceptance_criteria: list[str] = Field(min_length=1)


class RequirementsData(HandoffBase):
    SCHEMA_VERSION: ClassVar[int] = 1

    size: Literal["trivial", "small", "medium", "large"]  # read by `when:` conditions
    items: list[Requirement] = Field(min_length=1)


# --- design: produced by the Design agent -------------------------------------


class Decision(_Strict):
    id: str
    choice: str
    rationale: str  # the WHY, required (audit: "who did what and why")

    @field_validator("rationale")
    @classmethod
    def rationale_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("every decision needs a rationale")
        return value


class Impact(_Strict):
    modules: list[str] = []
    interfaces: list[str] = []
    tests: list[str] = []
    # Graph-derived lists are candidates until Review/QA confirm them (spec §10.1).
    confidence: Literal["candidate", "confirmed"] = "candidate"


class DesignData(HandoffBase):
    SCHEMA_VERSION: ClassVar[int] = 1

    decisions: list[Decision] = Field(min_length=1)
    impact: Impact = Impact()
    risks: list[str] = []


# --- dev-report: produced by the Developer -------------------------------------


class DevReportData(HandoffBase):
    SCHEMA_VERSION: ClassVar[int] = 1

    commits: list[str] = []
    files_changed: list[str] = []
    tests_run: TestCounts | None = None
    deviations_from_design: list[str] = []


# --- review: produced by Code Review (Zyroflo P6 + review rubric) --------------

Severity = Literal["critical", "high", "medium", "low", "info"]
SEVERITIES: tuple[str, ...] = get_args(Severity)


class Finding(_Strict):
    severity: Severity
    file: str | None = None
    line: int | None = None
    issue: str
    status: Literal["open", "resolved", "accepted"] = "open"


def count_open_blocking(findings: Iterable[object] | None, block_on: Iterable[str]) -> int:
    """Open findings at a blocking severity - the review loop's exit number (Zyroflo P6).
    Reads `Finding` models or the raw dicts of an agent's answer (status defaults to open)."""
    blocking = set(block_on)
    count = 0
    for finding in findings or []:
        if isinstance(finding, Finding):
            severity, status = finding.severity, finding.status
        elif isinstance(finding, dict):
            severity, status = finding.get("severity"), finding.get("status", "open")
        else:
            continue
        count += status == "open" and severity in blocking
    return count


class PrepassResult(_Strict):
    tool: str  # e.g. gitleaks, ast-grep, osv-scanner
    findings: int = Field(ge=0)


class ReviewData(HandoffBase):
    SCHEMA_VERSION: ClassVar[int] = 1

    findings: list[Finding] = []
    # Loop exit rule: repeat until no blocking finding is open; the score is
    # reported but never decides the gate (Zyroflo review exit rule).
    open_blocking: int = Field(ge=0)
    score: float | None = Field(default=None, ge=0, le=10)
    prepass_results: list[PrepassResult] = []


# --- test-report: produced by QA ------------------------------------------------


class AcceptanceResult(_Strict):
    criterion_id: str
    passed: bool
    evidence: str | None = None


class DependencyScan(_Strict):
    blocked_licences: list[str] = []
    vulnerabilities: list[str] = []


class TestReportData(HandoffBase):
    __test__ = False  # not a pytest test class, despite the name
    SCHEMA_VERSION: ClassVar[int] = 1

    verdict: Literal["pass", "fail"]
    tests: TestCounts
    acceptance_results: list[AcceptanceResult] = []
    dependency_scan: DependencyScan | None = None

    @model_validator(mode="after")
    def a_pass_has_no_failures(self) -> "TestReportData":
        if self.verdict == "pass":
            if self.tests.failed:
                raise ValueError(f"verdict: 'pass' but {self.tests.failed} test(s) failed")
            if failed := [r.criterion_id for r in self.acceptance_results if not r.passed]:
                raise ValueError(f"verdict: 'pass' but acceptance criterion {failed[0]} failed")
        return self


# kind name (as written in front matter) -> the model for its fields
HANDOFF_KINDS: dict[str, type[BaseModel]] = {
    "requirements": RequirementsData,
    "design": DesignData,
    "dev-report": DevReportData,
    "review": ReviewData,
    "test-report": TestReportData,
}
