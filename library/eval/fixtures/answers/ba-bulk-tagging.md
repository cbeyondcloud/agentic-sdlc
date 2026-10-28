---
status: complete
summary: Catalogue editors can add or remove one tag on up to 500 selected products in one action.
size: small
items:
  - id: REQ-1
    statement: An editor can add or remove one tag on up to 500 selected products at once.
    acceptance_criteria:
      - WHEN an editor applies a tag to selected products THE system SHALL tag each of them.
      - WHEN more than 500 products are selected THE system SHALL refuse the action.
  - id: REQ-2
    statement: Products that could not be changed are listed with the reason.
    acceptance_criteria:
      - WHEN some products fail THE system SHALL list each failed product and its reason.
open_questions: []
---
# Requirements: bulk tagging

## Summary
Catalogue editors tag products one at a time today. Bulk tagging lets them add or remove a tag on up to 500 selected products in one action.

## User scenarios
- P1 - Seasonal tagging: an editor selects a season's products and adds the tag "summer".
  - WHEN an editor applies a tag to selected products THE system SHALL tag each of them.
- P2 - Clean-up: an editor removes an obsolete tag from many products.

## Edge cases
- More than 500 products selected: refused (REQ-1).
- A product deleted while the action runs: listed as failed with the reason (REQ-2).

## Functional requirements
- REQ-1: The system MUST apply or remove one tag on up to 500 selected products.
- REQ-2: The system MUST list each product it could not change, with the reason.

## Success criteria
Tagging 500 products takes under 30 seconds.

## Assumptions
Only catalogue editors may bulk-tag, as for single tagging today.

## Open questions
None.
