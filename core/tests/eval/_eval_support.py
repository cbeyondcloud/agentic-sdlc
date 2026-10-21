"""Shared helpers for the eval harness tests: cases and handoff documents."""

from agentic.eval.cases import EvalCase

SOURCE = {"repo": "synthetic", "synthetic": True, "licence": "CC0-1.0"}

REQUIREMENTS_ANSWER = """---
status: complete
summary: Users can export invoices as CSV.
size: small
items:
  - id: REQ-1
    statement: Users can export their invoices as a CSV file.
    acceptance_criteria:
      - WHEN a user requests an export THE system SHALL return a job id.
---
# Requirements: CSV export

## Summary
Users export invoices as CSV through a background job.

## User scenarios
P1: a finance user exports a month of invoices.

## Edge cases
An empty month gives a file with only the header row.

## Functional requirements
REQ-1: The system MUST export invoices as CSV.

## Success criteria
An export of 10,000 invoices finishes within a minute.

## Assumptions
Exports use the existing job queue.

## Open questions
None.
"""


def scoping_case(case_id: str, split: str = "holdout") -> EvalCase:
    return EvalCase.model_validate(
        {
            "id": case_id,
            "agent": "business-analyst",
            "case_class": "scoping",
            "split": split,
            "source": SOURCE,
            "inputs": {"prompt": f"Write requirements for {case_id}."},
            "expected": {"key_points": ["export runs as a background job"]},
            "rubric": "ba-requirements",
        }
    )
