---
kind: requirements
schema_version: 1
run_id: golden-01
step: requirements
agent: business-analyst@1.0.0
produced_at: 2026-10-21T09:00:00Z
status: complete
summary: Finance users can export a month of invoices as a CSV file, produced as a background job.
size: medium
items:
  - id: REQ-1
    statement: A finance user can request a CSV export of the invoices for one calendar month.
    acceptance_criteria:
      - WHEN a finance user requests an export for a month THE system SHALL return an export job id within 2 seconds.
      - WHEN a user without the finance role requests an export THE system SHALL refuse it with a permission error.
  - id: REQ-2
    statement: The export file lists one invoice per row with a fixed column order.
    acceptance_criteria:
      - WHEN an export finishes THE system SHALL produce a UTF-8 CSV with the columns invoice_number, issue_date, customer, net, tax, gross, currency.
      - WHEN a month has no invoices THE system SHALL produce a file containing only the header row.
  - id: REQ-3
    statement: The user is told when the export is ready and can download it for seven days.
    acceptance_criteria:
      - WHEN an export finishes THE system SHALL email the requesting user a download link.
      - WHEN a download link is older than seven days THE system SHALL refuse the download.
open_questions: []
---
# Requirements: monthly invoice CSV export

## Summary
Finance users need the month's invoices in a spreadsheet for reconciliation. Today they copy them from the invoice list by hand. The export runs as a background job because a month can hold tens of thousands of invoices, more than one request can produce within the API timeout.

## User scenarios
- **P1 - Monthly reconciliation.** A finance user exports last month's invoices and opens the file in a spreadsheet to reconcile it against the ledger. Priority P1 because it replaces a manual process done every month. Testable alone: request an export, wait for the email, download and open the file.
  - WHEN a finance user requests an export for a month THE system SHALL return an export job id within 2 seconds.
  - WHEN an export finishes THE system SHALL email the requesting user a download link.
- **P2 - Empty month.** A finance user exports a month with no invoices and gets a file with the header row only, so the reconciliation tooling still runs. Testable alone with an empty test month.
- **P3 - Expired link.** A user opens a link from an old email and is told it has expired and how to request a new export.

## Edge cases
- A month with no invoices: header row only (REQ-2).
- A month with more than 50,000 invoices: the export still completes; the file is streamed, not built in memory.
- A user without the finance role: refused with a permission error (REQ-1).
- Two exports of the same month requested at once: both complete; each gets its own link.
- Customer names containing commas, quotes or non-Latin characters: quoted per RFC 4180 and kept as UTF-8.

## Functional requirements
- REQ-1: The system MUST let a finance user request a CSV export of one calendar month's invoices and return a job id.
- REQ-2: The system MUST write one invoice per row with the columns invoice_number, issue_date, customer, net, tax, gross, currency, in that order.
- REQ-3: The system MUST email the requesting user a download link that expires after seven days.

## Success criteria
- An export of 50,000 invoices finishes within 5 minutes.
- The job id is returned within 2 seconds for any month.
- Finance stops copying invoices by hand for the monthly reconciliation.

## Assumptions
- The existing background job queue is used; no new infrastructure.
- Amounts are exported as decimal strings in the invoice's own currency; no conversion.
- "Month" means the invoice issue date in the company's reporting time zone.

## Open questions
None.
