---
kind: dev-report
schema_version: 1
run_id: golden-01
step: build
agent: developer@1.0.0
produced_at: 2026-10-21T11:00:00Z
status: complete
summary: Invoice export implemented as a streaming queue job with a signed seven-day link; all tests pass.
commits:
  - "a1b2c3d test: failing tests for invoice CSV export"
  - "b2c3d4e feat(billing): stream invoice CSV export to object storage"
  - "c3d4e5f feat(api): invoice export endpoints and ready email"
files_changed:
  - billing/export.py
  - billing/jobs.py
  - api/routes/exports.py
  - notifications/email.py
  - tests/billing/test_export.py
  - tests/api/test_exports.py
tests_run: {passed: 214, failed: 0, skipped: 2}
deviations_from_design:
  - Rows are read with keyset pagination in batches of 1,000 instead of one server-side cursor, as the design's risk section suggested.
open_questions: []
---
# Development report: monthly invoice CSV export

## Summary
The export endpoints, the streaming job and the ready email are in place behind the finance role. The full suite passes.

## What changed
- `billing/export.py`: `write_invoice_csv(rows, out)` with RFC 4180 quoting and the fixed column order.
- `billing/jobs.py`: `ExportInvoicesJob` streams batches of 1,000 to object storage and sends the email.
- `api/routes/exports.py`: `POST /v1/invoice-exports` (role check, enqueue, job id) and `GET /v1/invoice-exports/{id}`.
- `notifications/email.py`: the "export ready" template with the signed link.

## Tests run
214 passed, 0 failed, 2 skipped (the skipped tests need a live object store and run nightly). New tests: column order, quoting of commas and quotes, the empty month, a 60,000-row month, the role check, the 2-second response.

## Deviations from the design
Keyset pagination in batches of 1,000 replaces the single server-side cursor, as the design's risk section suggested; the behaviour is the same and no connection stays open for minutes.

## Notes for review
Look at the quoting in `write_invoice_csv` and at the role check in the POST handler.

## Open questions
None.
