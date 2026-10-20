---
kind: dev-report
schema_version: 1
run_id: golden-02
step: build
agent: developer@1.0.0
produced_at: 2026-10-21T11:10:00Z
status: complete
summary: Per-account and per-address reset limits added with identical responses and audit events; all tests pass.
commits:
  - "d4e5f6a test: failing tests for password reset limits"
  - "e5f6a7b feat(auth): sliding-window limits on password resets"
files_changed:
  - auth/password_reset.py
  - auth/rate_limit.py
  - audit/events.py
  - tests/auth/test_password_reset_limits.py
tests_run: {passed: 389, failed: 0, skipped: 0}
deviations_from_design: []
open_questions: []
---
# Development report: rate limiting password resets

## Summary
Resets are limited to 3 per account and 20 per client address per sliding hour. Refusals return the normal confirmation (account limit) or HTTP 429 (address limit) and are audited.

## What changed
- `auth/rate_limit.py`: `SlidingWindow(cache, key, limit, window_s)`, now shared with the login limiter.
- `auth/password_reset.py`: the address check, the account lookup with a dummy hash for unknown accounts, the account check.
- `audit/events.py`: `password_reset.refused` with the limit name and a salted hash of the address.

## Tests run
389 passed, 0 failed, 0 skipped. New tests cover the 4th request per account, the 21st per address, identical bodies for existing and unknown accounts, the audit event, and the cache-down path (429).

## Deviations from the design
None.

## Notes for review
The timing test is statistical (200 samples per path); it allows a 5 ms average difference.

## Open questions
None.
