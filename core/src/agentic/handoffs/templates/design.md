---
decisions:
  - id: D1
    choice: Run exports as background jobs on the existing task queue.
    rationale: A large export takes longer than the API timeout.
impact:
  modules: [billing/export.py, api/routes/export.py]
  interfaces: [POST /v1/exports]
  tests: [tests/test_export.py]
  confidence: candidate
risks:
  - The task queue has no retry policy for long jobs.
---
# Design

## Context and problem statement
<!-- The problem this design solves and the forces at play, in two to four sentences; link the requirements it answers. -->

## Decision drivers
<!-- What the choice must satisfy: requirements, constraints, qualities such as latency, cost or ease of change. -->

## Considered options
<!-- Every option seriously considered, one bullet each, including "do nothing" when it is one. -->

## Decision outcome
<!-- One subsection per decision above, "### D1: <choice>", with its rationale (why it beat the other options) and its consequences, good and bad. -->

## Impact
<!-- Modules, interfaces and tests the change touches. Lists taken from the code graph are candidates for Review and QA to confirm. -->

## Risks
<!-- What could go wrong and how it is mitigated or watched. -->

## Confirmation
<!-- How the implementation will be checked against this design: tests, review points, measurements. -->
