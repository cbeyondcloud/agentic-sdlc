---
kind: test-report
schema_version: 1
run_id: golden-02
step: qa
agent: qa@1.0.0
produced_at: 2026-10-21T13:10:00Z
status: complete
summary: The reset limits meet every acceptance criterion, including identical responses for unknown accounts.
verdict: pass
tests: {passed: 389, failed: 0, skipped: 0}
acceptance_results:
  - {criterion_id: REQ-1.1, passed: true, evidence: "test_fourth_request_sends_no_email"}
  - {criterion_id: REQ-1.2, passed: true, evidence: "test_limited_response_matches_success"}
  - {criterion_id: REQ-2.1, passed: true, evidence: "test_address_limit_returns_429_with_retry_after"}
  - {criterion_id: REQ-3.1, passed: true, evidence: "test_refusal_writes_audit_event"}
dependency_scan: {blocked_licences: [], vulnerabilities: []}
open_questions: []
---
# Test report: rate limiting password resets

## Summary
Every criterion passes. The timing test (identical response time within 5 ms on average) passed on 200 samples per path.

## Verdict
Pass.

## Test results
389 passed, 0 failed, 0 skipped.

## Acceptance criteria
| Criterion | Result | Evidence |
|---|---|---|
| REQ-1.1 | pass | test_fourth_request_sends_no_email |
| REQ-1.2 | pass | test_limited_response_matches_success |
| REQ-2.1 | pass | test_address_limit_returns_429_with_retry_after |
| REQ-3.1 | pass | test_refusal_writes_audit_event |

## Dependency scan
No new dependencies.

## Open questions
None.
