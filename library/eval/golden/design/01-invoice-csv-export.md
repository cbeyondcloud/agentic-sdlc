---
kind: design
schema_version: 1
run_id: golden-01
step: design
agent: design@1.0.0
produced_at: 2026-10-21T10:00:00Z
status: complete
summary: The export runs as a queued job that streams invoices to a CSV in object storage and emails a signed link.
decisions:
  - id: D1
    choice: Run the export as a job on the existing background queue
    rationale: A month can exceed 50,000 invoices, which cannot be written within the API timeout; the queue already retries and is monitored.
  - id: D2
    choice: Stream rows from a server-side cursor straight to object storage
    rationale: Building the file in memory fails for large months; streaming keeps memory flat whatever the size.
  - id: D3
    choice: Email a signed download URL that expires after seven days
    rationale: Meets REQ-3 without a new download service; expiry is enforced by the storage signature, not our code.
impact:
  modules: [billing/export.py, billing/jobs.py, api/routes/exports.py, notifications/email.py]
  interfaces: ["POST /v1/invoice-exports", "GET /v1/invoice-exports/{id}"]
  tests: [tests/billing/test_export.py, tests/api/test_exports.py]
  confidence: candidate
risks:
  - A very large month could keep a database cursor open for minutes.
  - Signed URLs forwarded by email can be used by anyone holding them until they expire.
open_questions: []
---
# Design: monthly invoice CSV export

## Summary
`POST /v1/invoice-exports` validates the finance role and enqueues an export job, returning its id at once. The job streams the month's invoices from a server-side cursor into a CSV in object storage, then emails the user a signed link valid for seven days.

## Context and problem statement
Finance reconciles invoices monthly by copying them by hand (requirements REQ-1 to REQ-3). A synchronous export would exceed the 30-second API timeout for large months.

## Decision drivers
- Months with more than 50,000 invoices must work (success criteria).
- The job id must come back within 2 seconds.
- No new infrastructure (assumption in the requirements).

## Considered options
- Synchronous export with a longer timeout - fails for large months and ties up a web worker.
- Background job writing the whole file in memory - simple, but memory grows with the month.
- Background job streaming from a cursor to object storage - flat memory, uses existing services.

## Decision outcome
### D1: Run the export as a job on the existing background queue
A month can exceed 50,000 invoices, which cannot be written within the API timeout; the queue already retries and is monitored.

### D2: Stream rows from a server-side cursor straight to object storage
Building the file in memory fails for large months; streaming keeps memory flat whatever the size.

### D3: Email a signed download URL that expires after seven days
Meets REQ-3 without a new download service; expiry is enforced by the storage signature.

## Impact
- `billing/export.py`: the CSV writer (RFC 4180 quoting, fixed column order).
- `billing/jobs.py`: the export job.
- `api/routes/exports.py`: the two endpoints and the role check.
- `notifications/email.py`: the "export ready" email.
These are candidates from the code graph until Review confirms them.

## Risks
- A long-open cursor: the job reads in batches of 1,000 by keyset pagination instead of one cursor.
- Forwarded links: the link expires after seven days and downloads are logged.

## Confirmation
`tests/billing/test_export.py` covers column order, quoting, the empty month and a 60,000-row month; `tests/api/test_exports.py` covers the role check and the 2-second response.

## Open questions
None.
