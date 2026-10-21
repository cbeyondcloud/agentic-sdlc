---
kind: design
schema_version: 1
run_id: golden-03
step: design
agent: design@1.0.0
produced_at: 2026-10-21T10:20:00Z
status: complete
summary: Each delivery attempt is a scheduled job; a failure classifier decides whether to schedule the next backoff step.
decisions:
  - id: D1
    choice: Model each attempt as a delayed job on the existing scheduler with the backoff schedule as data
    rationale: The scheduler already persists delayed jobs across restarts, and a data schedule can change without a deploy.
  - id: D2
    choice: Classify outcomes as success, temporary (5xx, timeout, 408, 429) or permanent (other 4xx)
    rationale: Retrying permanent errors wastes work and floods integrators' logs; 408 and 429 signal a temporary condition.
  - id: D3
    choice: Store attempts in a deliveries table that backs both the list page and replay
    rationale: One record of attempts serves the integrator view, the support team and the replay action.
impact:
  modules: [webhooks/dispatch.py, webhooks/retry.py, webhooks/models.py, api/routes/deliveries.py]
  interfaces: ["GET /v1/webhook-deliveries", "POST /v1/webhook-deliveries/{id}/replay"]
  tests: [tests/webhooks/test_retry.py, tests/api/test_deliveries.py]
  confidence: candidate
risks:
  - Retries can deliver events out of order.
  - A large outage creates a burst of retries when many endpoints recover together.
open_questions: []
---
# Design: webhook delivery retries

## Summary
Sending a webhook becomes a job. Its outcome is classified; a temporary failure schedules the next attempt from the backoff schedule (1, 5, 30, 120, 720 minutes), a permanent one stops. Attempts are stored, listed for integrators and can be replayed once by hand.

## Context and problem statement
Each webhook is sent once today, so a short endpoint outage loses events (REQ-1). Integrators also cannot see failures (REQ-2) or recover them (REQ-3).

## Decision drivers
- Survive restarts without losing scheduled retries.
- Never retry permanent client errors.
- Reuse the payloads already kept 7 days for auditing.

## Considered options
- In-process retry loop with sleeps - loses retries on restart.
- A dedicated message broker with dead-letter queues - new infrastructure.
- Delayed jobs on the existing scheduler - chosen.

## Decision outcome
### D1: Each attempt is a delayed job; the schedule is data
The scheduler persists delayed jobs; the schedule can change without a deploy.

### D2: Classify outcomes before scheduling
Permanent errors are not retried; 408 and 429 are temporary, and a longer Retry-After wins over the schedule step.

### D3: A deliveries table backs the list and replay
One record of attempts serves integrators, support and replay.

## Impact
`webhooks/dispatch.py`, `webhooks/retry.py` (classifier and schedule), `webhooks/models.py` (deliveries table), `api/routes/deliveries.py`. Candidates until Review confirms.

## Risks
- Out-of-order delivery: documented; each event keeps its id and timestamp so integrators can reorder.
- Retry bursts: each scheduled time gets up to 10 % random jitter.

## Confirmation
`tests/webhooks/test_retry.py` covers the classifier and schedule; `tests/api/test_deliveries.py` covers the list and a single replay.

## Open questions
None.
