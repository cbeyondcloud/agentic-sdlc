---
status: complete
summary: split_evenly() spreads the remainder one cent at a time over the first shares.
commits: ["e2f3a4b fix(money): keep the remainder when splitting"]
files_changed: [money.py]
tests_run: {passed: 2, failed: 0, skipped: 0}
deviations_from_design: []
open_questions: []
---
# Development report: money split

## Summary
The remainder of the integer division is added one cent each to the first shares.

## What changed
`money.py`: `split_evenly` uses `divmod` and adds 1 to the first `remainder` shares.

## Tests run
2 passed, 0 failed.

## Deviations from the design
None.

## Notes for review
`parts` of 0 still raises ZeroDivisionError, as before.

## Open questions
None.
