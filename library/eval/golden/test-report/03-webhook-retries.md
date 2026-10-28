---
kind: test-report
schema_version: 1
run_id: golden-03
step: qa
agent: qa@1.0.0
produced_at: 2026-10-21T13:20:00Z
status: complete
summary: Retries and the deliveries list pass, but replay with the original signature scheme is unverified because its test is skipped.
verdict: fail
tests: {passed: 156, failed: 0, skipped: 1}
acceptance_results:
  - {criterion_id: REQ-1.1, passed: true, evidence: "tests/webhooks/test_retry.py::test_schedule_steps"}
  - {criterion_id: REQ-1.2, passed: true, evidence: "tests/webhooks/test_retry.py::test_permanent_errors_not_retried"}
  - {criterion_id: REQ-2.1, passed: true, evidence: "tests/api/test_deliveries.py::test_lists_failed_deliveries"}
  - {criterion_id: REQ-3.1, passed: false, evidence: "test_replay_keeps_signature_scheme is skipped: no test shows the original signature scheme is used"}
dependency_scan: {blocked_licences: [], vulnerabilities: []}
open_questions: []
---
# Test report: webhook delivery retries

## Summary
Three of four criteria pass. REQ-3.1 (replay keeps the original signature scheme) has no passing test: the only test for it is skipped until the signing-key fixture exists.

## Verdict
Fail: REQ-3.1 is not shown to be met.

## Test results
156 passed, 0 failed, 1 skipped.

## Acceptance criteria
| Criterion | Result | Evidence |
|---|---|---|
| REQ-1.1 | pass | test_schedule_steps |
| REQ-1.2 | pass | test_permanent_errors_not_retried |
| REQ-2.1 | pass | test_lists_failed_deliveries |
| REQ-3.1 | fail | its test is skipped |

## Dependency scan
No new dependencies.

## Open questions
None.
