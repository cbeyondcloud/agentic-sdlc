---
verdict: pass
tests:
  passed: 42
  failed: 0
  skipped: 1
failures: []
baseline_failures: []
acceptance_results:
  - criterion_id: REQ-1
    passed: true
    evidence: tests/test_export.py::test_export_returns_a_job_id
dependency_scan:
  blocked_licences: []
  vulnerabilities: []
---
# Test report

## Verdict
<!-- Pass or fail, and in one sentence why. A pass means no acceptance criterion failed and no test failed that passed at intake: every failing test is named in failures and listed in baseline_failures (copied from the baseline test report). -->

## Test results
<!-- The suites run, their counts, the baseline they were judged against, and every failure with its cause, new failures first. -->

## Acceptance criteria
<!-- Each criterion by id: pass or fail, with the evidence (a test, a log, a manual check). -->

## Dependency scan
<!-- New dependencies with their licences, blocked licences and known vulnerabilities found. -->
