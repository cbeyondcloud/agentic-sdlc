---
kind: design
schema_version: 1
run_id: golden-02
step: design
agent: design@1.0.0
produced_at: 2026-10-21T10:10:00Z
status: complete
summary: Two sliding-window counters in the shared cache limit resets per account and per client address; responses stay identical.
decisions:
  - id: D1
    choice: Sliding-window counters in the shared cache, keyed by account hash and by client address hash
    rationale: The cache already backs login limits, so no new store; sliding windows stop requests bunched at a clock-hour boundary.
  - id: D2
    choice: Check limits after the account lookup but before deciding to send, and always return the same response
    rationale: The per-account limit must apply to real accounts only, while identical responses keep account existence secret.
  - id: D3
    choice: Fail closed on the per-address limit when the cache is down
    rationale: An outage must not reopen the flooding hole; legitimate users can retry later.
impact:
  modules: [auth/password_reset.py, auth/rate_limit.py, audit/events.py]
  interfaces: [POST /v1/password-resets]
  tests: [tests/auth/test_password_reset_limits.py]
  confidence: candidate
risks:
  - A large NAT can push many real users over the per-address limit.
  - Timing differences between the account and no-account paths could still leak existence.
open_questions: []
---
# Design: rate limiting password resets

## Summary
`POST /v1/password-resets` checks a per-address sliding window first (HTTP 429 when over), then looks up the account and checks its per-account window. Over the account limit, no email is sent but the response is the normal confirmation. Every refusal writes an audit event.

## Context and problem statement
The reset form is used to flood inboxes and to probe for accounts (REQ-1 to REQ-3). The login endpoint already has a cache-backed limiter we can reuse.

## Decision drivers
- No account existence leak, in body or timing.
- Reuse the existing limiter and cache.
- Safe behaviour when the cache is unavailable.

## Considered options
- Fixed-window counters per clock hour - simple, but allow 2x bursts across the boundary.
- A CAPTCHA on the form - stops scripts but adds friction for every user and does not stop targeted floods.
- Sliding-window counters per account and per address - chosen.

## Decision outcome
### D1: Sliding-window counters in the shared cache
The cache already backs login limits; sliding windows stop boundary bursts.

### D2: Check limits after the lookup, always the same response
The account limit applies to real accounts only; identical responses keep existence secret. The no-account path performs a dummy hash so both paths take the same time.

### D3: Fail closed on the per-address limit when the cache is down
An outage must not reopen the flooding hole.

## Impact
`auth/password_reset.py` (the flow), `auth/rate_limit.py` (a sliding-window helper shared with login), `audit/events.py` (the refusal event). Candidates until Review confirms.

## Risks
- NAT users: 20 per hour per address leaves headroom; the limit is a setting.
- Timing leak: the dummy hash equalises the paths; a test asserts the timing difference stays under 5 ms on average.

## Confirmation
`tests/auth/test_password_reset_limits.py` covers the 4th request, the 21st request from one address, identical responses and the audit event.

## Open questions
None.
