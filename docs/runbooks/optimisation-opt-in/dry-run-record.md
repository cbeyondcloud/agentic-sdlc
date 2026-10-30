# Optimisation opt-in record: G14_tool_output on business-analyst

> **DRY RUN.** Recorded or synthetic data, no model was called. This record
> proves the procedure and its template; it is not evidence about the group.

## Plan (registered before the first run)

| Field | Value |
|---|---|
| Date | 2026-10-30 |
| Plan sha | `sha256:22f39315b9effc6dc3a13c978917eb22f70e95665c2f01da19fc955f7547d1d4` |
| Agent | business-analyst |
| A (current) | gateway-current (profile `agentic`) |
| B (candidate) | G14_tool_output (profile `agentic+G14_tool_output`) |
| Corpus | `sha256:a85292ea39c86f6b665faeb28f963ad33d7ea56e3e090d7edc31e8515ad304d4`, split holdout |
| Primary metric | rubric, non-inferiority, margin -0.5 |
| Cases x repeats | 3 x 2, seed 24 |
| Alpha | 0.05 one-sided |
| Judge | judge-model |
| Rubric sha | `sha256:8c3ed2830bd8bf0c87bf0686627247c5df836ad0c0ecb87c177e63b6b0d09cf0` |
| Budget cap | 100 USD |
| Guards set | invalid runs +1 at most, median cost +25 % at most, cost must fall |

## How it ran

| Check | Result |
|---|---|
| Spent | 0.0227 of 100 USD |
| Models (resolved) | not recorded |
| Judge calibration | ACCEPTED |
| Blinding leak check | not run |
| Verbosity check | not recorded |

## Verdict

**NO_BENEFIT**

- sign-flip p = 0.1250 is not below alpha 0.05

## Primary metric

| Measure | Value |
|---|---|
| Cases scored on both sides | 3 of 3 |
| Unscored | none |
| Mean difference B - A | 0.0000 |
| Bootstrap 95 % CI | [+0.0000, +0.0000] |
| SD of the differences | 0.0000 |
| Sign-flip p (one-sided, mean - margin > 0) | 0.1250 |
| Sign test | 0 up, 0 down, 3 tied; p 1.0000 |
| Wilcoxon signed-rank p (two-sided) | 1.0000 |

## Guards

| Guard | Holds | Value | Limit | Note |
|---|---|---|---|---|
| invalid | yes | +0.0000 | +1.0000 | B had 0 invalid runs, A 0 |
| cost | yes | -0.2000 | +0.2500 | median cost per run changed by -20% |
| cost_decrease | yes | -0.0004 | +0.0000 | cost difference CI upper -0.0004 |

## Pairwise judgement

Win rate of B 0.50 over 6 comparisons; 0% of verdicts changed with the order.

## Per-case differences (B - A)

| Case | Difference |
|---|---|
| ba-invoice-export | +0.0000 |
| ba-reset-limits | +0.0000 |
| ba-webhook-retries | +0.0000 |

## Decision

None: a dry run never turns a group on.

## Notes

Both variants replay the recorded fixture answers (`library/eval/fixtures/cassettes`, the business-analyst holdout cases tagged `fixture`) and the recorded judge scores. B's gateway cost is the recorded cost times 0.8, a stand-in for a group that saves tokens without changing the answer. Three cases cannot reach p < 0.05 (the smallest sign-flip p is 1/8), so the verdict is NO_BENEFIT: a real record needs S10's 30 cases.

## Sign-off

| Role | Name | Date |
|---|---|---|
| Platform owner (runs the gateway) | | |
| Eval owner (checked the plan and the report) | | |
