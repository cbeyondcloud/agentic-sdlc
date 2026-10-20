---
status: complete
summary: The export endpoint and file pass; the ready email is missing, so REQ-3.1 fails.
verdict: fail
tests: {passed: 201, failed: 0, skipped: 0}
acceptance_results:
  - {criterion_id: REQ-1.1, passed: true, evidence: "the POST endpoint returns a job id"}
  - {criterion_id: REQ-1.2, passed: true, evidence: "role check test"}
  - {criterion_id: REQ-2.1, passed: true, evidence: "column order test"}
  - {criterion_id: REQ-2.2, passed: true, evidence: "empty month test"}
  - {criterion_id: REQ-3.1, passed: false, evidence: "no email is sent yet (deviation in the dev report)"}
  - {criterion_id: REQ-3.2, passed: true, evidence: "storage links expire by default"}
dependency_scan: {blocked_licences: [], vulnerabilities: []}
open_questions: []
---
# Test report: invoice export (part 1)

## Summary
Five criteria pass; REQ-3.1 fails because no email is sent.

## Verdict
Fail.

## Test results
201 passed, 0 failed, 0 skipped.

## Acceptance criteria
REQ-3.1 fails; the others pass.

## Dependency scan
No new dependencies.

## Open questions
None.
