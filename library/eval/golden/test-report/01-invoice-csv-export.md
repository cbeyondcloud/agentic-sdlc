---
kind: test-report
schema_version: 1
run_id: golden-01
step: qa
agent: qa@1.0.0
produced_at: 2026-10-21T13:00:00Z
status: complete
summary: Every acceptance criterion of the invoice export passes; no blocked licences or known vulnerabilities.
verdict: pass
tests: {passed: 214, failed: 0, skipped: 2}
acceptance_results:
  - {criterion_id: REQ-1.1, passed: true, evidence: "tests/api/test_exports.py::test_job_id_within_two_seconds"}
  - {criterion_id: REQ-1.2, passed: true, evidence: "tests/api/test_exports.py::test_non_finance_user_refused"}
  - {criterion_id: REQ-2.1, passed: true, evidence: "tests/billing/test_export.py::test_column_order_and_utf8"}
  - {criterion_id: REQ-2.2, passed: true, evidence: "tests/billing/test_export.py::test_empty_month_header_only"}
  - {criterion_id: REQ-3.1, passed: true, evidence: "tests/billing/test_export.py::test_ready_email_has_link"}
  - {criterion_id: REQ-3.2, passed: true, evidence: "tests/api/test_exports.py::test_link_expires_after_seven_days"}
dependency_scan: {blocked_licences: [], vulnerabilities: []}
open_questions: []
---
# Test report: monthly invoice CSV export

## Summary
All six acceptance criteria are covered by passing tests. The two skipped tests need a live object store and run in the nightly suite.

## Verdict
Pass.

## Test results
214 passed, 0 failed, 2 skipped.

## Acceptance criteria
| Criterion | Result | Evidence |
|---|---|---|
| REQ-1.1 | pass | test_job_id_within_two_seconds |
| REQ-1.2 | pass | test_non_finance_user_refused |
| REQ-2.1 | pass | test_column_order_and_utf8 |
| REQ-2.2 | pass | test_empty_month_header_only |
| REQ-3.1 | pass | test_ready_email_has_link |
| REQ-3.2 | pass | test_link_expires_after_seven_days |

## Dependency scan
No new dependencies; no blocked licences or known vulnerabilities.

## Open questions
None.
