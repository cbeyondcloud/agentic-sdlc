---
kind: review
schema_version: 1
run_id: golden-02
step: review
agent: code-review@1.0.0
produced_at: 2026-10-21T12:10:00Z
status: complete
summary: The limits work, but two responses reveal whether an account exists, and refusals are not audited.
findings:
  - severity: high
    file: auth/password_reset.py
    line: 12
    issue: Unknown emails now get 404 "no such account", which reveals whether an account exists; REQ-1 requires the same confirmation either way.
    status: open
  - severity: high
    file: auth/password_reset.py
    line: 15
    issue: Over the per-account limit the response is a 429 naming the account, unlike the normal confirmation; this also leaks existence.
    status: open
  - severity: medium
    file: auth/password_reset.py
    line: 9
    issue: Refused requests write no audit event, so REQ-3 is not met.
    status: open
open_blocking: 2
score: 5
prepass_results:
  - {tool: gitleaks, findings: 0}
open_questions: []
---
# Review: password reset rate limits

## Summary
The sliding-window limits are wired correctly, but the responses break the "no account probing" requirement and refusals are not audited.

## Findings
- **high** `auth/password_reset.py:12` - return the normal confirmation for unknown emails (and do the dummy hash from the design).
- **high** `auth/password_reset.py:15` - over the account limit, return the normal confirmation and send nothing.
- **medium** `auth/password_reset.py:9` - add the `password_reset.refused` audit event for both limits.

## Category breakdown
- Security: 2 (account enumeration)
- Requirements: 1 (missing audit)

## Pre-pass results
gitleaks found nothing.

## Verdict
Changes requested.

## Open questions
None.
