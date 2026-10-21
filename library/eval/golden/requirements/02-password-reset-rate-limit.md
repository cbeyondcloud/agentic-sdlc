---
kind: requirements
schema_version: 1
run_id: golden-02
step: requirements
agent: business-analyst@1.0.0
produced_at: 2026-10-21T09:10:00Z
status: complete
summary: Password reset requests are rate limited per account and per client address, without revealing whether an account exists.
size: small
items:
  - id: REQ-1
    statement: Password reset emails for one account are limited to 3 per hour.
    acceptance_criteria:
      - WHEN a fourth reset is requested for the same account within one hour THE system SHALL not send another email.
      - WHEN a reset is refused by the limit THE system SHALL show the same confirmation message as a successful request.
  - id: REQ-2
    statement: Reset requests from one client address are limited to 20 per hour across all accounts.
    acceptance_criteria:
      - WHEN a client address exceeds 20 reset requests in one hour THE system SHALL answer with HTTP 429 and a Retry-After header.
  - id: REQ-3
    statement: Every refused reset request is recorded for the security team.
    acceptance_criteria:
      - WHEN a reset request is refused by a limit THE system SHALL write an audit event with the limit that applied and a hash of the client address.
open_questions: []
---
# Requirements: rate limiting password resets

## Summary
Attackers use the password reset form to flood users' inboxes and to find out which email addresses have accounts. Limits per account and per client address stop the flooding; identical responses stop the probing.

## User scenarios
- **P1 - Inbox flooding is stopped.** An attacker scripts reset requests for a victim's address; the victim receives at most 3 emails an hour. P1 because it is an active abuse report. Testable alone by sending 4 requests for one account.
  - WHEN a fourth reset is requested for the same account within one hour THE system SHALL not send another email.
- **P1 - No account probing.** Responses for existing and non-existing accounts, limited or not, are identical, so the form reveals nothing.
  - WHEN a reset is refused by the limit THE system SHALL show the same confirmation message as a successful request.
- **P2 - Scripted bulk requests.** One client address requesting resets for many accounts is slowed down with HTTP 429.

## Edge cases
- Requests spread just under the limit over a sliding hour: the window is sliding, not fixed per clock hour.
- Many users behind one corporate NAT address: the per-address limit (20/hour) is high enough for normal use; the per-account limit still protects each user.
- The limit store is unavailable: the system fails closed for the per-address limit (429) and logs the outage.

## Functional requirements
- REQ-1: The system MUST send at most 3 reset emails per account per sliding hour, with an identical response either way.
- REQ-2: The system MUST answer HTTP 429 with Retry-After when one client address exceeds 20 reset requests per sliding hour.
- REQ-3: The system MUST audit every refused request with the limit that applied and a salted hash of the client address.

## Success criteria
- Abuse reports about reset email floods drop to zero within a month of release.
- No measurable difference in response time or body between existing and non-existing accounts.

## Assumptions
- The existing shared cache can hold the sliding-window counters.
- The client address is taken from the trusted proxy header already used for login limits.

## Open questions
None.
