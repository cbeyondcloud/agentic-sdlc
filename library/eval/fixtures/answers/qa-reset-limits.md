---
status: complete
summary: The account limit answers 429 instead of the normal confirmation, and no audit event is written.
verdict: fail
tests: {passed: 377, failed: 0, skipped: 0}
acceptance_results:
  - {criterion_id: REQ-1.1, passed: true, evidence: "the fourth email is not sent"}
  - {criterion_id: REQ-1.2, passed: false, evidence: "a limited request answers 429, unlike a successful one"}
  - {criterion_id: REQ-2.1, passed: true, evidence: "the address limit answers 429"}
  - {criterion_id: REQ-3.1, passed: false, evidence: "no audit event is written"}
dependency_scan: {blocked_licences: [], vulnerabilities: []}
open_questions: []
---
# Test report: reset limits

## Summary
Two criteria fail: REQ-1.2 and REQ-3.1.

## Verdict
Fail.

## Test results
377 passed, 0 failed, 0 skipped.

## Acceptance criteria
REQ-1.2 and REQ-3.1 fail; REQ-1.1 and REQ-2.1 pass.

## Dependency scan
No new dependencies.

## Open questions
None.
