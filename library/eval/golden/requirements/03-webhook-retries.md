---
kind: requirements
schema_version: 1
run_id: golden-03
step: requirements
agent: business-analyst@1.0.0
produced_at: 2026-10-21T09:20:00Z
status: complete
summary: Failed webhook deliveries are retried with backoff for 24 hours, and integrators can see and replay failed deliveries.
size: medium
items:
  - id: REQ-1
    statement: A delivery that fails is retried with exponential backoff for up to 24 hours.
    acceptance_criteria:
      - WHEN a delivery gets a 5xx response or times out THE system SHALL retry it after 1, 5, 30, 120 and 720 minutes.
      - WHEN a delivery gets a 4xx response other than 408 or 429 THE system SHALL not retry it.
  - id: REQ-2
    statement: Integrators can list failed deliveries from the last 7 days.
    acceptance_criteria:
      - WHEN an integrator opens the deliveries page THE system SHALL list each failed delivery with its event type, last status code and next retry time.
  - id: REQ-3
    statement: Integrators can replay a failed delivery by hand.
    acceptance_criteria:
      - WHEN an integrator replays a failed delivery THE system SHALL send it again once with the original payload and signature scheme.
open_questions: []
---
# Requirements: webhook delivery retries

## Summary
Integrators lose events when their endpoint is briefly down, because each webhook is sent once. Retries with backoff recover short outages; a deliveries page and manual replay cover longer ones.

## User scenarios
- **P1 - Short outage recovered.** An integrator's endpoint returns 503 for ten minutes during a deploy; every event sent in that window arrives after it recovers. P1 because lost events are the top integration complaint. Testable alone by failing an endpoint for ten minutes.
  - WHEN a delivery gets a 5xx response or times out THE system SHALL retry it after 1, 5, 30, 120 and 720 minutes.
- **P2 - See what failed.** An integrator lists failed deliveries and sees why each failed and when it is retried next.
- **P2 - Replay after a long outage.** After a day-long outage, the integrator replays the failed deliveries by hand.

## Edge cases
- A permanent client error (400, 401, 404): not retried; shown as failed straight away.
- 408 and 429 are treated as temporary and retried; a Retry-After header is honoured when longer than the backoff step.
- An endpoint that recovers mid-schedule: the remaining retries are cancelled once one succeeds.
- Ordering: retries may deliver events out of order; each event keeps its original id and timestamp.

## Functional requirements
- REQ-1: The system MUST retry failed deliveries with the backoff schedule 1, 5, 30, 120, 720 minutes, and MUST NOT retry permanent client errors.
- REQ-2: The system MUST list failed deliveries of the last 7 days with event type, last status and next retry time.
- REQ-3: The system MUST let an integrator replay a failed delivery once, with the original payload and signature scheme.

## Success criteria
- Fewer than 0.1 % of events are lost after a 10-minute endpoint outage.
- Integrator support tickets about missing events drop by half within a quarter.

## Assumptions
- The existing job scheduler can hold delayed retry jobs.
- Payloads are kept for 7 days already for auditing, so replay needs no new storage.

## Open questions
None.
