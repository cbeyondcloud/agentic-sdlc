---
kind: design
schema_version: 1
run_id: r-2026-10-16-7f3a
step: design
agent: design@1.4.0
produced_at: 2026-10-16T10:42:00Z
status: complete
decisions:
  - id: D1
    choice: "Add async job queue for exports"
    rationale: "Export latency exceeds API timeout"
impact:
  modules: [billing/export.py, api/routes/export.py]
  interfaces: [GET /v1/exports]
  tests: [tests/test_export.py]
  confidence: candidate
risks: ["Queue backlog under load"]
---
## Design: async exports

Exports move to a background job; the API returns a job id immediately.
