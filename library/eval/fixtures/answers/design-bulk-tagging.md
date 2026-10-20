---
status: complete
summary: Bulk tagging is one API call that applies the tag per product in a background job and reports failures per product.
decisions:
  - id: D1
    choice: Apply the tag per product inside one background job
    rationale: One failure must not undo the other products (REQ-2), and 500 updates can exceed a request's time budget.
  - id: D2
    choice: Enforce the 500-product limit in the API before enqueueing
    rationale: The limit protects the database; a client-side check alone can be bypassed.
impact:
  modules: [catalogue/tags.py, catalogue/jobs.py, api/routes/tags.py]
  interfaces: ["POST /v1/products/tags/bulk"]
  tests: [tests/catalogue/test_bulk_tags.py]
  confidence: candidate
risks:
  - Two bulk actions on the same products can interleave; the last write wins.
open_questions: []
---
# Design: bulk tagging

## Summary
`POST /v1/products/tags/bulk` checks the limit and enqueues a job that applies the tag product by product and records failures.

## Context and problem statement
Editors tag up to 500 products at once (REQ-1) and need to see which failed (REQ-2).

## Decision drivers
Partial failure must be reported per product; bounded request time.

## Considered options
- One database transaction for all products: a single failure undoes every change.
- Per-product updates in a background job: chosen.

## Decision outcome
### D1: Apply the tag per product inside one background job
One failure must not undo the others, and 500 updates can exceed a request's time budget.

### D2: Enforce the limit in the API
A client-side check alone can be bypassed.

## Impact
`catalogue/tags.py`, `catalogue/jobs.py`, `api/routes/tags.py` (candidates).

## Risks
Interleaved bulk actions: last write wins, accepted for tags.

## Confirmation
`tests/catalogue/test_bulk_tags.py` covers 500 products, 501 refused, and one failing product.

## Open questions
None.
