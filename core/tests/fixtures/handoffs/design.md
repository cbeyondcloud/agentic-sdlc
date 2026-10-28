---
kind: design
schema_version: 1
run_id: r-2026-10-16-7f3a
step: design
agent: design@1.4.0
produced_at: 2026-10-16T10:42:00Z
status: complete
summary: Exports move to background jobs.
decisions:
  - id: D1
    choice: "Add async job queue for exports"
    rationale: "Export latency exceeds API timeout"
impact:
  modules: [billing/export.py, api/routes/export.py]
  interfaces: [GET /v1/exports]
  tests: [tests/test_export.py]
  confidence: candidate
risks: ["Queue backlog under load"]
---
# Design: async exports

## Summary
Exports move to a background job; the API returns a job id immediately.

## Context and problem statement
Large exports exceed the API timeout.

## Decision drivers
Latency, reuse of the existing queue.

## Considered options
- Synchronous export with a longer timeout
- Background job on the existing queue

## Decision outcome
### D1: Add async job queue for exports
Export latency exceeds API timeout.

## Impact
billing/export.py, api/routes/export.py (candidates).

## Risks
Queue backlog under load.

## Confirmation
tests/test_export.py covers the job flow.

## Open questions
None.
