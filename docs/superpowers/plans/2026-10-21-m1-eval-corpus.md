# M1 eval corpus and harness close-out (M1-59, M1-63)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The public eval corpus in `library/eval/` meets the decision-18 floors (20 scoping per rubric agent, 20 review diffs with at least 40 seeded defects, 20 Developer tasks whose tests fail before and pass after a reference change, 30 seeded QA failing criteria, at least 5 dev-split cases per class), every case is checked by the harness's own validators, CI fixture mode stays fast (a tagged subset with cassettes), and every Open deferred item that targets M1-59 or M1-63 - plus the eval-harness items parked on M1-64 that need no live model - is fixed, accepted or retargeted, so the owner's M1-64 gate session measures what it claims to measure.

**Architecture:**
- **Fixture subset.** Cases tagged `fixture` are the CI subset with cassettes. `agentic eval run --tag <tag>` (and `RunOptions.tag`) selects them; the record script and the CLI tests pass `--tag fixture`. The report records the tag. The full corpus is for live runs.
- **Validators** (`agentic.eval.validate`, stdlib only), run by `load_corpus` on every load:
  - every handoff a case gives (inline or file) parses as its kind; a golden parses;
  - a review diff is a well-formed unified diff (hunk counts add up) and every seeded defect names a file in the diff, with its line range inside a hunk's new-side span;
  - a QA case's failing criteria exist in its requirements (`REQ-n.m` = item `REQ-n`, criterion m) and at least one criterion is left passing (so false alarms are measurable);
  - a dev task names a workspace and a reference `expected.solution` folder, and every test it names is in a file of the workspace.
- **Dev-task executor** (`agentic.eval.devtask`): `run_tests(folder, test_ids)` runs pytest in a subprocess (`run_process`, exec form, safe env, timeout) and reads each node id's outcome from the `-rA` summary. `check_dev_task(corpus, case)` copies the workspace to a temporary folder: fail-to-pass tests must fail and pass-to-pass tests pass; with the solution overlaid, all pass. The CI test checks all dev tasks concurrently.
- **Metrics hardening** (DF-258): QA false alarms (criteria marked failed that the case leaves passing) as a pooled rate; review recall counts only open findings above `info`; the line tolerance shrinks for files with few changed lines; `a/`/`b/` prefixes match; judge evidence needs at least 12 characters.
- **Judge family** (DF-302, DF-256, DF-262): `resolve_judge` returns warnings (shared gateway prefix with no `family:`; an id with no family prefix) shown by `eval run` and kept in the report; it refuses a same-family judge when policy offers another family; the judge's temperature comes from its tier.
- **Harness robustness** (DF-260, DF-254, DF-259, DF-248): the whole of `execute_run` after the ledger opens is covered (an exception closes the ledger run); a judge budget stop sets `budget_hit`; an errored run keeps its partial cost; replay problems, judge failures and budget stops get their own messages and exit codes; clearer messages for a missing policy folder; a README in `cases/` is allowed; the label is hidden in single-variant reports; live identity takes the team from `AGENTIC_TEAM` or the user config.
- **Calibration** (DF-257): raters see the reference and key points the judge saw; the answer key is written outside the folder handed to raters.
- **Report** (DF-261): `sigma_case` (per-case SD of each mean metric) per label; outputs longer than a cap are truncated before the judge sees them (the same cap for every variant).
- **Gate** (DF-243, gate half): `agentic eval gate --report DIR ...` compares reports with policy `eval.targets` (defaults = the M1-64 initial targets) and writes `gate.json` and `gate.md`.
- **Developer tests** (DF-241): a dev task's run works in a copy of its workspace with its tools; after the run, the case's tests execute there and fill `RunRecord.tests`, so `tests_pass` is measured.

**Tech Stack:** Python 3.12, pydantic, pydantic-evals (existing), pytest (subprocess, already a dev dependency and the executor of the dev tasks). No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md` (agent quality), spike record `docs/spikes/S10-ab-eval-method.md` (corpus rules, judging, fixture mode), decisions 17 and 18, tracker rows M1-59, M1-63 (and the M1-64 notes).

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| Eval engine | pydantic-evals 2.54.0 (MIT), already the engine | - |
| Corpus content | synthetic CC0-1.0 cases written for this corpus | SWE-bench / SWE-bench Lite (MIT): each task needs its upstream repo's environment (Docker per repo), far too heavy for CI and laptops; HumanEval (MIT): single functions, not change requests with a design or a diff; MBPP (CC-BY-4.0) and CodeReviewer (custom terms): not on the allow-list |
| Unified diff parsing | stdlib, about 40 lines | `unidiff` (MIT): a new core dependency for hunk headers and counts only |
| Test outcomes | pytest's own `-rA` summary (node ids, outcomes) | JUnit XML: classnames do not map back to node ids for files in folders |
| Pilot-repo harvest | Zyroflo `corpus_harvest` via a commons adapter (later, see retargets) | - |

## Global Constraints

- `library/eval/` holds only synthetic or allow-listed cases (decision 17); every new case is `synthetic: true, licence: CC0-1.0`.
- Cases are realistic and varied: Python, TypeScript, Go, Java, C#, Rust, SQL, PowerShell, bash and YAML in review diffs; billing, health, logistics, HR, IoT, education, retail, developer tooling and more in scoping and QA; Windows and POSIX concerns (path separators, CRLF, case-insensitive file systems, file locking, shell quoting, home folders, encodings).
- Dev tasks are Python with pytest so they execute on Windows, macOS and Linux runners with no extra toolchain.
- CI fixture mode replays only `fixture`-tagged cases; the corpus validators and the dev-task fail-then-pass check run in CI over the whole corpus, concurrently.
- Paths in cases are POSIX and case-exact; hashes are over LF bytes.
- Never add `# fmt: skip` or `# fmt: off`; ruff line length 100; check pytest exit codes directly.
- TRACKER.md, DEFERRED.md and DECISIONS-*.md are updated by the coordinator from this track's output.
- No vendor names in `agentic.eval` or the CLI.

## Review Focus

1. **A validator that lies.** A seeded defect outside its diff, a QA criterion missing from the requirements, a dev task whose fail-to-pass test already passes, or whose solution does not make it pass, is refused. Test: Task 2 `test_a_defect_outside_the_diff_is_refused`, `test_an_unknown_failing_criterion_is_refused`; Task 3 `test_a_task_whose_test_already_passes_is_refused`, `test_every_library_dev_task_fails_then_passes`.
2. **Degenerate output cannot inflate metrics.** Failing every QA criterion shows a false-alarm rate of 1.0; info or resolved findings do not count for recall; a one-word quote is not evidence. Test: Task 5.
3. **The judge family rule.** A shared gateway prefix with no `family:` is warned about; a same-family judge is refused when policy offers another family. Test: Task 6.
4. **A run never stays open in the ledger**, whatever raises after it starts. Test: Task 7 `test_a_failure_after_the_runs_closes_the_ledger`.
5. **Raters never see the judge's totals.** Test: Task 8.
6. **Fixture mode stays fast and faithful**: replay of the fixture subset matches the expected reports, and no network call is made. Test: `cli/tests/test_eval_command.py`.

---

### Task 1: Fixture subset (`--tag`)

**Files:** `cli/src/agentic_cli/evaluation.py`, `cli/src/agentic_cli/commands/evals.py`, `core/src/agentic/eval/corpus.py` (`Corpus.select(tag=)`), `core/src/agentic/eval/report.py` (`EvalReport.tag`), `scripts/record_eval_fixtures.py`, `cli/tests/test_eval_command.py`, the 17 seed cases (tag `fixture`).

- [ ] RED: `test_tag_selects_the_fixture_subset` (a corpus copy with one untagged case added: `--tag fixture` runs only tagged cases; an unknown tag is INVALID with the known tags in the hint).
- [ ] GREEN: `Corpus.select(agent, split, tag)`, `RunOptions.tag`, `--tag`; the report records `tag`.

### Task 2: Corpus validators

**Files:** create `core/src/agentic/eval/validate.py`; modify `corpus.py` (call it), `cases.py` (`Expected.solution`); test `core/tests/eval/test_eval_validate.py`.

**Interfaces:** `parse_hunks(diff) -> dict[str, list[tuple[int, int]]]` (new-side spans per file; raises `ValueError` on bad counts); `case_problems(root, case) -> list[str]`.

- [ ] RED: defect outside the diff; file not in the diff; bad hunk counts; unknown failing criterion; all criteria failing; an inline handoff that does not parse; a dev task with no solution or a test file not in the workspace.
- [ ] GREEN: implement and call from `load_corpus`.

### Task 3: Dev-task executor

**Files:** create `core/src/agentic/eval/devtask.py`; test `core/tests/eval/test_eval_devtask.py`.

**Interfaces:** `async run_tests(folder, test_ids, *, timeout_s) -> dict[str, bool]`; `async check_dev_task(corpus, case) -> list[str]`; `overlay(solution, folder)`.

- [ ] RED: a passing and a failing test are read correctly; a missing test id is False; a task whose fail-to-pass test already passes is refused; a solution that does not fix it is refused; every library dev task fails then passes.
- [ ] GREEN: implement.

### Task 4: Corpus growth (M1-59)

**Files:** `library/eval/cases/**`, `library/eval/workspaces/**`, `library/eval/solutions/**`, `library/eval/README.md`, `library/eval/manifest.json`; test `core/tests/eval/test_eval_library_assets.py` (`floor_shortfalls == []`, at least 20 holdout scoping per rubric agent, varied languages and tags).

- [ ] RED: the floors test fails on the seed corpus.
- [ ] GREEN: write the cases; rewrite the manifest.

### Task 5: Metric hardening (DF-258)

**Files:** `core/src/agentic/eval/metrics.py`, `report.py` (pooled `qa_false_alarm`), `judge.py` (evidence length); tests in `test_eval_metrics.py`, `test_eval_judge.py`.

- [ ] RED/GREEN for each: false alarms; open, non-info findings only; tolerance per file; `a/`/`b/` prefixes; short evidence re-asked.

### Task 6: Judge family (DF-302, DF-256, DF-262)

**Files:** `core/src/agentic/eval/judge.py`, `cli/src/agentic_cli/evaluation.py`, `report.py` (`JudgeInfo.warnings`), the SP18a plan's Review Focus 3; tests `test_eval_judge.py`, `test_eval_command.py`.

- [ ] RED: gateway-prefix warning; no-prefix id warning; same family refused when policy offers another; tier temperature used.
- [ ] GREEN.

### Task 7: Harness robustness (DF-260, DF-254, DF-259, DF-248)

**Files:** `cli/src/agentic_cli/evaluation.py`, `core/src/agentic/eval/runner.py`, `metrics.py`, `corpus.py`, `report.py`; tests.

- [ ] RED/GREEN for each listed behaviour.

### Task 8: Calibration (DF-257)

- [ ] RED: the pack carries reference and key points; `calibration/` holds no key. GREEN.

### Task 9: Report sigma and verbosity cap (DF-261)

- [ ] RED: `sigma_case` present; a long output is truncated before the judge. GREEN.

### Task 10: `agentic eval gate` (DF-243, gate half)

**Files:** `core/src/agentic/policy/model.py` (`EvalPolicy.targets`), create `core/src/agentic/eval/gate.py`, `cli` command; tests.

- [ ] RED: reports meeting and missing targets give PASS/FAIL per metric; a report that is not MEASURED is NOT_MEASURED; mixed corpora are refused. GREEN.

### Task 11: Developer runs execute their tests (DF-241)

**Files:** `core/src/agentic/eval/runner.py`, `devtask.py`, `cli/src/agentic_cli/evaluation.py`; tests.

- [ ] RED: a dev task run with a tool-using model edits its workspace copy and `tests_pass` is 1.0; a run that changes nothing scores 0.0. GREEN.

### Task 12: False findings adjudicated (DF-244)

**Files:** `core/src/agentic/eval/judge.py` (`FindingJudge`), `metrics.py` (`FalseFindingMetric`), `report.py` (pooled `false_finding_rate`, rulings written per run), `cli/src/agentic_cli/evaluation.py`, `scripts/record_eval_fixtures.py`; tests.

- [ ] RED: unmatched findings judged not real count as false; a real one does not; a wrong verdict count is re-asked then unmeasured; the fixture review report has a false-finding rate. GREEN.

### Task 13: Re-record fixtures and finish

- [ ] `uv run python scripts/record_eval_fixtures.py`; review the expected-report diff.
- [ ] `uv run python scripts/check.py --all` green.
