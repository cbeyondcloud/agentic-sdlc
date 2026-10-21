---
status: complete
summary: Rounding drops cents from every discounted total; one concern outside the change.
findings:
  - severity: high
    file: billing/discounts.py
    line: 4
    issue: round() returns a whole number, so every discounted total loses its cents.
    status: open
  - severity: high
    file: billing/invoices.py
    line: 30
    issue: Invoices call apply_discount without checking the customer's discount eligibility.
    status: open
open_blocking: 2
score: 6
prepass_results: []
open_questions: []
---
# Review: discount cap

## Summary
The cap works, but the new rounding is wrong.

## Findings
- **high** `billing/discounts.py:4` - keep two decimals (or use Decimal with quantize).
- **high** `billing/invoices.py:30` - check eligibility before applying a discount.

## Category breakdown
- Logic: 2

## Pre-pass results
None run.

## Verdict
Changes requested.

## Open questions
None.
