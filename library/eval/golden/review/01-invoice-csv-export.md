---
kind: review
schema_version: 1
run_id: golden-01
step: review
agent: code-review@1.0.0
produced_at: 2026-10-21T12:00:00Z
status: complete
summary: Two blocking issues - CSV fields are not quoted, and the export endpoint has no finance role check.
findings:
  - severity: high
    file: billing/export.py
    line: 11
    issue: Fields are joined with commas without RFC 4180 quoting; a customer name containing a comma or quote corrupts the row.
    status: open
  - severity: critical
    file: api/routes/exports.py
    line: 7
    issue: Any authenticated user can export every invoice; the finance role required by REQ-1 is never checked.
    status: open
  - severity: low
    file: billing/export.py
    line: 10
    issue: The gross amount is recomputed as net plus tax instead of read from the invoice, which can differ after rounding.
    status: open
open_blocking: 2
score: 4
prepass_results:
  - {tool: gitleaks, findings: 0}
  - {tool: ast-grep, findings: 0}
open_questions: []
---
# Review: invoice CSV export

## Summary
The change adds the CSV writer and the export endpoint. Two blocking issues: unquoted CSV fields and a missing permission check.

## Findings
- **high** `billing/export.py:11` - fields are joined without quoting; use the `csv` module with `QUOTE_MINIMAL`.
- **critical** `api/routes/exports.py:7` - no finance role check before enqueueing the export.
- **low** `billing/export.py:10` - read `gross` from the invoice rather than recomputing it.

## Category breakdown
- Correctness: 2 (quoting, gross amount)
- Security: 1 (missing authorisation)

## Pre-pass results
gitleaks and ast-grep found nothing.

## Verdict
Changes requested: fix the two blocking findings, then re-review.

## Open questions
None.
