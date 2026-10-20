---
kind: dev-report
schema_version: 1
run_id: golden-03
step: build
agent: developer@1.0.0
produced_at: 2026-10-21T11:20:00Z
status: complete
summary: Webhook attempts are scheduled jobs with outcome classification, backoff with jitter, a deliveries list and replay; one test is skipped pending a fixture.
commits:
  - "f6a7b8c test: failing tests for webhook retries"
  - "a7b8c9d feat(webhooks): classify outcomes and schedule retries"
  - "b8c9d0e feat(api): deliveries list and replay"
files_changed:
  - webhooks/dispatch.py
  - webhooks/retry.py
  - webhooks/models.py
  - api/routes/deliveries.py
  - migrations/0042_webhook_deliveries.py
  - tests/webhooks/test_retry.py
  - tests/api/test_deliveries.py
tests_run: {passed: 156, failed: 0, skipped: 1}
deviations_from_design:
  - Added a database migration for the deliveries table, which the design implied but did not list.
open_questions: []
---
# Development report: webhook delivery retries

## Summary
Deliveries are retried on temporary failures with the 1, 5, 30, 120, 720 minute schedule plus up to 10 % jitter; permanent errors stop at once. Integrators can list failures from the last 7 days and replay one.

## What changed
- `webhooks/retry.py`: `classify(response)` and `next_attempt_at(attempt, retry_after)`.
- `webhooks/dispatch.py`: each attempt is a delayed job; success cancels the rest.
- `webhooks/models.py` and `migrations/0042_webhook_deliveries.py`: the deliveries table.
- `api/routes/deliveries.py`: `GET /v1/webhook-deliveries`, `POST /v1/webhook-deliveries/{id}/replay`.

## Tests run
156 passed, 0 failed, 1 skipped: the replay signature test needs the signing-key fixture that lands with the key rotation work.

## Deviations from the design
A migration for the deliveries table was added; the design implied it but did not list it.

## Notes for review
Check that Retry-After is honoured only when it is longer than the schedule step.

## Open questions
None.
