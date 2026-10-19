---
size: small
items:
  - id: REQ-1
    statement: Users can export their invoices as a CSV file.
    acceptance_criteria:
      - WHEN a user requests an export THE system SHALL return a job id within 2 seconds.
      - WHEN the export finishes THE system SHALL email the user a download link.
---
# Requirements

## User scenarios
<!-- One story per journey, most important first, each marked P1, P2 or P3: who wants what and why, why it has that priority, and how it can be tested on its own. Under each story list its acceptance criteria in EARS form (WHEN <trigger> THE system SHALL <response>), matching the items above. -->

## Edge cases
<!-- Boundary conditions and failures to handle: empty or huge inputs, missing permissions, timeouts, concurrent use. -->

## Functional requirements
<!-- One bullet per item above, by id: "REQ-1: The system MUST ...". -->

## Success criteria
<!-- Measurable outcomes that show the feature works, independent of how it is built (times, rates, counts). -->

## Assumptions
<!-- Defaults you chose where the request was silent, and what the work depends on. -->
