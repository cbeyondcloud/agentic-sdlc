---
status: complete
summary: slugify() now folds accented letters to their plain form before splitting into words.
commits: ["d1e2f3a fix(textutil): fold accents in slugify"]
files_changed: [textutil.py]
tests_run: {passed: 2, failed: 0, skipped: 0}
deviations_from_design: []
open_questions: []
---
# Development report: slugify accents

## Summary
Accented letters are folded with Unicode NFKD normalisation and combining marks dropped.

## What changed
`textutil.py`: `slugify` normalises with `unicodedata.normalize("NFKD", ...)` and drops combining characters before matching words.

## Tests run
2 passed, 0 failed.

## Deviations from the design
None.

## Notes for review
Non-Latin scripts without a plain form still drop out, as before.

## Open questions
None.
