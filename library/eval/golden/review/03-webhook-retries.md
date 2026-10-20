---
kind: review
schema_version: 1
run_id: golden-03
step: review
agent: code-review@1.0.0
produced_at: 2026-10-21T12:20:00Z
status: complete
summary: The classifier treats 408 and 429 as permanent, and the backoff crashes after the last scheduled attempt.
findings:
  - severity: high
    file: webhooks/retry.py
    line: 10
    issue: 408 and 429 fall into the 4xx branch and are classified permanent, so throttled or timed-out deliveries are never retried (REQ-1 says they are temporary).
    status: open
  - severity: high
    file: webhooks/retry.py
    line: 16
    issue: SCHEDULE_MIN[attempt] raises IndexError after the fifth retry instead of marking the delivery failed.
    status: open
open_blocking: 2
score: 5
prepass_results:
  - {tool: ast-grep, findings: 0}
open_questions: []
---
# Review: webhook retry classifier

## Summary
The schedule and jitter are right; the classifier and the end of the schedule are not.

## Findings
- **high** `webhooks/retry.py:10` - check 408 and 429 before the generic 4xx branch and return "temporary".
- **high** `webhooks/retry.py:16` - return None (no more attempts) when `attempt >= len(SCHEDULE_MIN)` and let the caller mark the delivery failed.

## Category breakdown
- Logic: 1 (classification)
- Error handling: 1 (end of schedule)

## Pre-pass results
ast-grep found nothing.

## Verdict
Changes requested.

## Open questions
None.
