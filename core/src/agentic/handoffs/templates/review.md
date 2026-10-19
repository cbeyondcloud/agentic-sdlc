---
findings:
  - severity: high
    file: billing/export.py
    line: 42
    issue: The export job has no timeout, so a stuck job holds a worker forever.
    status: open
score: 7.5
prepass_results:
  - tool: gitleaks
    findings: 0
---
# Review

## Findings
<!-- One line per finding, most severe first: severity — file:line — the issue → the fix. Mark each open, resolved or accepted. -->

## Category breakdown
<!-- The rubric categories and their scores, and the final score. The score is reported only; it never decides whether the review passes. -->

## Pre-pass results
<!-- What the deterministic tools found (secrets, rule matches, vulnerable dependencies) and what was done about each. -->

## Verdict
<!-- The open findings at a blocking severity, by severity. The review passes only when there are none. -->
