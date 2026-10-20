# S10 — Skill and agent A/B method: judging protocol, blinding, sample size

**Date:** 2026-10-20 · **Code (throwaway):** `spikes/s10_ab_eval/`:
- `abstats.py`: the stdlib paired statistics proposed for `agentic.eval.stats`;
- `check_vs_scipy.py`: cross-checks them against scipy;
- `power.py`: Monte Carlo power of the decision rule (`--signflip` compares the two tests);
- `toy_ab.py`: a pydantic-evals A/B with pointwise and pairwise judges, all on `FunctionModel`, no network.

**Throwaway deps:** `uv run --with`: pydantic-evals 2.54.0, numpy 2.5.3, scipy 1.18.1. `pyproject.toml` and `uv.lock` are untouched. **Platform:** Windows 11, Python 3.12.

## Question

How does the framework decide that a skill, an agent version or a TokenLean optimisation group is better than (or no worse than) what it replaces, and that a built-in agent meets its quality gate? The answer covers:
- the judging protocol: pointwise or pairwise, position bias, judge family, human calibration;
- blinding;
- sample size and repeats under nondeterminism;
- the cost of one A/B.

It must also give a harness sketch that M1-63 builds and that M1-64, M2-46, M3-14 and M3-24 use. The constraints are provider-agnostic (any model behind TokenLean via `base_url`), Windows and macOS, a 1,000-person org, and quality first.

## Method

1. **OSS survey.** For each candidate: licence, Python fit, provider agnosticism, Windows/macOS, dependency footprint and release dates, from PyPI/npm metadata fetched 2026-10-20. Pins must be at least two weeks old (on or before 2026-10-06).
2. **Zyroflo reuse.** Read `corpus_harvest.py`, `corpus_replay.py` and `impact_ab.py` in the pinned `sdlc-harness` (`a0c1b14`) and `review-rubric.md`.
3. **Statistics.** Wrote the paired tests in stdlib Python and checked them against scipy on 300 random inputs. Then simulated the proposed decision rule:
   - 2,000 simulated experiments per cell;
   - 2,000 bootstrap resamples or sign flips each;
   - effects: rubric +1 / +0.5 points, pass rate +10 / +15 / +20 points;
   - non-inferiority at -0.5 rubric points;
   - alpha checked at zero effect.
4. **Harness probe.** Ran a toy BA "skill" A/B on pydantic-evals 2.54.0:
   - 8 cases, 3 repeats per variant;
   - a structured per-criterion rubric judge and a pairwise judge with a deliberate first-position bias, all on `FunctionModel`;
   - the skill leaves a "Using skill: …" tell in its output, so the scrubber has something to catch;
   - a leak check over every judge input.

## OSS comparison

| Candidate | Licence | Pin candidate (date) | Python fit | Provider-agnostic via `base_url` | Win / macOS | Footprint and notes | Verdict |
|---|---|---|---|---|---|---|---|
| **pydantic-evals** (pydantic-ai repo) | MIT | **2.54.0 (2026-10-03)**, the same version as our pinned `pydantic-ai-slim==2.54.0` | Native: `Dataset`/`Case` (YAML/JSON round-trip), async task per case, `repeat=k` grouped per case (`report.case_groups()`), custom `Evaluator` and `ReportEvaluator`, `LLMJudge`/`GEval`, `PrecisionRecallEvaluator`, baseline diff in `report.print(baseline=…)`, retries, concurrency | Yes. Judges take any pydantic-ai `Model`, so the TokenLean-backed model from `agentic.transport` works | Pure Python; ran on Windows | Adds **one** package: its deps (anyio, logfire-api, pydantic, pyyaml, rich) are already in `uv.lock`. Pitfall: `LLMJudge` without `model=` falls back to `openai:gpt-5.2`; our wrapper must always pass the model. API churn: `Dataset(name=…)` is now required | **Adopt** as the replay/run/report engine |
| inspect_ai (UK AISI) | MIT | 0.3.277 (2026-10-06); 0.3.x, almost daily releases | Good concepts: epochs plus reducers, model-graded scorers, log viewer | Has its own model layer (OpenAI-compatible `base_url` works), duplicating pydantic-ai | Python; Docker sandboxes for agentic tasks | About 40 direct deps (boto3, aiobotocore, s3fs, fastapi, uvicorn, textual, numpy, tiktoken, debugpy…). Churn makes pinning costly | **Reject as a dependency**; borrow epochs/reducers and per-sample logs |
| deepeval | Apache-2.0 | 4.2.8 (2026-10-02) | pytest-centric metrics, G-Eval | Configurable, but OpenAI by default | Python | Depends on **posthog** (telemetry), grpcio, opentelemetry and pytest plugins; upsells the Confident AI cloud | **Reject**: telemetry and cloud coupling on 1,000 laptops |
| promptfoo | MIT | 0.123.1 (2026-09-18) | Not Python: a Node CLI with YAML config, `llm-rubric` and `select-best` | Yes, OpenAI-compatible providers | Node on both | Adds a Node runtime to every machine; not embeddable in our engine | **Reject for core**; teams may use it ad hoc |
| OpenAI evals | MIT (repo) | PyPI 3.0.1.post1 (**2024-05-01**) | Registry of YAML evals | OpenAI-centric | Python | Stale for 2.5 years | **Reject** |
| lm-evaluation-harness | MIT | 0.4.13 (2026-08-31) | Benchmarks models (log-likelihood or generation) on datasets, not agent runs | n/a | Python | datasets, evaluate, scikit-learn… | **Reject**: wrong problem |
| SWE-bench harness | MIT | 5.0.2 (2026-08-18) | Patch then run tests in per-instance Docker images | n/a (scores patches) | Docker | Docker on developer machines conflicts with D38 | **Reuse the idea**: FAIL_TO_PASS / PASS_TO_PASS test lists as Developer truth |
| openevals (LangChain) | MIT | 0.2.0 (2026-04-07) | A library of judge prompts | via LangChain | Python | LangChain ecosystem | **Reject**; prompts are a reference only |
| vcrpy | MIT | 8.3.0 (2026-07-04) | HTTP record/replay, in-process only | yes | yes | Can't see the `claude-code` backend's subprocess traffic | **Reject** for fixture mode (the cassette server below covers every backend) |
| scipy | BSD-3 | 1.18.1 (2026-08-21) | All of these tests | n/a | yes | About 35 MB, and we need 5 functions | **Dev/test dependency only**, to cross-check `agentic.eval.stats` |
| **Zyroflo** `corpus_harvest`, `corpus_replay`, `impact_ab` (pinned `sdlc-harness`) | owner's | `a0c1b14` | Harvest: mechanical classes `scoping`, `review` and `defect` from git history, `synthetic: false` stamped, harness-owned paths set aside. Replay: compares each case only against the last run of the same sha; an empty replay is a failure. Metrics: `None` = unmeasurable, never 0. Blinding: `blind_and_score` scores X/Y first and attaches the mapping after. The verdict is pre-registered | n/a | yes | The replay itself is specific to impact ranking (it rebuilds the graph at the parent); harvest, blinding and the per-case regression rule are generic | **Reuse via `sdlc_commons`** (D34): harvest and the regression rule. Core cannot import commons, so the CLI wires them |

## Results

**Statistics code.** On 300 random inputs, the stdlib sign test, exact and normal-approximation Wilcoxon, and Wilson interval all agree with scipy 1.18.1 to within 2.2e-16. So `agentic.eval.stats` can ship without numpy or scipy.

**Choosing the decision test** (`power.py --signflip`, one-sided 5 %):

| Metric | n cases × k repeats | Percentile bootstrap: power / **real alpha** | Paired sign-flip: power / **real alpha** |
|---|---|---|---|
| rubric +1 | 10 × 3 | .93 / **.076** | .85 / .036 |
| rubric +1 | 20 × 3 | 1.00 / **.064** | .99 / .045 |
| pass rate +10 pp | 60 × 3 | .84 / **.066** | .80 / .043 |

The bootstrap's lower bound is anti-conservative at the sizes we can afford: 6–8 % false "better" verdicts instead of 5 %. The sign-flip randomisation test holds its size whatever the distribution's shape, and costs little power from 20 × 3 upwards. **Decision: sign-flip decides, bootstrap CI is reported.**

**Power of the proposed rule** (sign-flip, one-sided 5 %, 80 % target).

Noise assumptions:
- rubric: case quality N(6, 1.5), run noise SD 0.8, judge noise SD 0.7, effect heterogeneity SD 0.5, integer scores 0–10;
- pass rate: per-case pass probability drawn from Beta, either U-shaped (0.5, 0.5) or uniform (1, 1), and lifted by a logit shift.

| Comparison | Effect to detect | n × k | Power | Notes |
|---|---|---|---|---|
| BA / Design rubric (superiority) | **+1.0 point** | **20 × 3** | **0.99** | 15 × 3: 0.96; 12 × 3: 0.92; 20 × 1: 0.79 |
| BA / Design rubric | +0.5 point | 20 × 3 | 0.65 | bootstrap 40 × 3: 0.92, so about 40 × 3 for +0.5 |
| Pass rate (Developer tests, Review per-case recall, QA catch) | +20 pp | 20 × 3 | about 0.80–0.85 (estimated) | bootstrap .89–.93; sign-flip not run at +20 pp, but it runs 0.04–0.17 lower than the bootstrap at this size |
| Pass rate | **+15 pp** | **40 × 3** | **0.84–0.91** | 30 × 3: .72–.82; 30 × 5: .91–.96 |
| Pass rate | +10 pp | 80 × 3 (or 60 × 5) | 0.84–0.90 (0.89–0.95) | 60 × 3: .69–.80; 20 × 1 has only .17–.20 power |
| TokenLean optimisation, rubric (non-inferiority, margin -0.5, true effect 0) | not worse than -0.5 | **30 × 3** | **0.88** | 20 × 3: 0.74; 40 × 3: 0.94 |

**Robustness.** The rubric figures depend on the SD of the per-case difference at k = 3, measured at 0.98 under these assumptions. Analytically, n = ((1.645 + 0.842)·σ_d / Δ)²:
- if real σ_d is 1.5, 20 × 3 still detects +1 at 80 %;
- the plan holds up to σ_d ≈ 1.8.

M1-63's first live run measures σ_d and re-runs `power.py` with it.

**Repeats matter most for binary metrics.** One run per case gives a 0/1 per-case outcome, and 20 × 1 has under 20 % power for +10 pp. Three repeats turn it into a per-case rate, roughly doubling power at the same n.

**Absolute-gate precision** (Wilson 95 % half-width):

| n | at p = .5 | at p = .7 |
|---|---|---|
| 10 | ± .26 | ± .25 |
| 20 | ± .20 | ± .19 |
| 40 | ± .15 | ± .14 |
| 60 | ± .12 | ± .11 |
| 100 | ± .10 | ± .09 |

With M1-59's 10 defect cases, "Developer ≥ 50 %" or "Review recall ≥ 70 %" cannot be told apart from 30 % or 50 %.

**Toy pydantic-evals run** (`toy_ab.py`, 8 cases × 3 repeats × 2 variants, 120 judge calls):
- `repeat=3` grouped correctly per case.
- The per-case rubric differences gave mean +1.67, bootstrap 95 % CI [0.96, 2.54], sign-flip p = 0.0045.
- **Position bias:** the judge was biased to favour the first response. With A always shown first, B's pairwise win rate was 0.42. With both orders judged, and order-dependent verdicts counted as ties, it was **0.69** (13 of 24 pairs were order-dependent). Without the swap, a biased judge would have *reversed* the conclusion.
- **Blinding:** zero leaks of "skill", "variant", "baseline" or "candidate" across all 120 judge inputs. The skill's "Using skill: …" tell, present in 100 % of B outputs, was scrubbed.
- pydantic-ai 2.54 prints a Logfire banner unless `PYDANTIC_AI_NO_BANNER=1` is set. The harness sets it.

## Protocol (what M1-63 implements)

### Judging

| Agent output | Primary metric | Judge needed for the primary? |
|---|---|---|
| BA `requirements`, Design `design` | **Pointwise analytic rubric**, 0–10 | yes |
| Developer `change` | tests pass: all FAIL_TO_PASS tests pass and no PASS_TO_PASS test regresses, run in the sandbox | no |
| Code Review `review` | seeded-defect recall: a finding matches if it is in the same file, within ±3 lines of the seeded range, and in a compatible category | only to adjudicate unmatched findings |
| QA `test-report` | seeded failing criteria caught | no |
| every agent | schema-valid after ≤ 1 retry (`AgentOutcome.status != "invalid"`), cost, tokens, duration | no |

1. **Use objective truth wherever it exists.** An LLM judge is used only where there is no mechanical answer: BA/Design quality, and deciding whether a review finding with no seeded match is a real issue.
2. **Pointwise analytic rubric is primary; pairwise is secondary.** The absolute gate (M1-64) needs pointwise scores anyway, so the A/B gets them at no extra cost. Pairwise (both orders) is reported as corroboration: a PROMOTE needs a pairwise win rate ≥ 0.5. Primary for promotion is the pointwise rubric because:
   - it is interpretable per criterion;
   - it is less exposed to position bias;
   - pairwise adds about 50 % judge tokens.
3. **Rubric shape:**
   - 5 criteria × 0–2 integer anchors = 10. Analytic, not holistic, to cut variance. Seeded from Zyroflo's P1/P2 SOP expectations and `review-rubric.md`'s "deterministic, don't feel a number" rule.
   - Each criterion must carry a verbatim evidence quote. A missing quote counts as unmeasurable, with one re-ask.
   - Reference-guided: the golden handoff and the case's key points are given to the judge.
   - Structured output is a Pydantic model.
   - Temperature 0, or the lowest the backend honours.
   - The rubric is versioned (`ba-requirements@1`, its sha256 recorded).
4. **The judge comes from a different model family than the candidate.**
   - Family = the vendor prefix of the resolved model id (`anthropic/…`, `openai/…`, `google/…`), overridable by a `family:` key in `tiers.yaml`.
   - The judge is a new admin-mapped tier, `eval-judge`, so no vendor name appears in code.
   - If policy offers only one family, the run proceeds with `judge_same_family: true` in the report and the human calibration sample doubles. Reason: self-preference bias.
   - An optional `judge.panel` (two families, mean score) is available for the org GA gate.
5. **Pairwise judging.**
   - Both orders are judged; labels are "Response 1/2".
   - A win counts only if it survives the swap; an order-dependent verdict is a tie (0.5).
   - The per-case score is the mean over the k run-pairs.
   - The order-inconsistency rate is reported. Above 30 %, the judge or rubric is flagged as unreliable.
6. **Verbosity check.** Both outputs are truncated at the same cap, and the report shows how score correlates with output length. |r| > 0.5 is flagged.

### Blinding

- **Opaque labels.** Variants become X/Y with a per-run random flip, which is ledgered. Scores are computed on X/Y first, and the mapping is attached afterwards (Zyroflo `blind_and_score`).
- **Scrubber, before any judge or human sees an output.** It removes:
  - skill and agent names from the resolved definitions;
  - "Using skill …" or "As the … agent" tells;
  - model self-identification;
  - skill-specific markers listed in the skill's eval metadata.

  A **leak check** greps every judge input for variant, skill and model names. Any hit makes the A/B `UNMEASURABLE`, not a pass.
- **Humans see what the judge sees.** Calibration items are exported with opaque ids only.

### Repeats and nondeterminism

- Agents run with **production settings** (tier, temperature, effort) and **k = 3** repeats per case per variant. We measure the system people will use, not a temperature-0 artefact.
- Variants are **interleaved per case in random order**, so provider drift during the run hits both sides equally.
- The resolved model id (from the TokenLean response) is recorded per run. A mid-run model change is flagged.
- The judge runs once per output at temperature 0. Judge noise is part of what the repeats average out.
- The unit of analysis is the **case**: repeats are averaged within a case before any test, and all CIs resample cases. Repeats are correlated, so treating them as independent cases overstates the evidence.

### Pre-registration and decision rule

An `ABPlan` is written and its sha256 ledgered **before** the first run. It holds:
- the variants;
- the agent;
- the corpus manifest hash and split;
- the primary metric and direction (superiority or non-inferiority) with its margin;
- n and k, alpha 0.05 one-sided;
- the judge model id and rubric hash;
- the seed;
- the guard metrics;
- the budget cap.

Amendments are new plans; the old result stays in the record (Zyroflo's pre-registered protocol).

| Verdict | Rule |
|---|---|
| `PROMOTE` | paired **sign-flip** test of mean(d − margin) > 0 has p < 0.05; **and** the guards hold; **and**, for rubric agents, the pairwise win rate is ≥ 0.5 |
| `NO_BENEFIT` | otherwise. Per spec §11, a skill that shows no benefit is not promoted or is removed from defaults. An inconclusive result is not "almost promoted": a re-test is a new, larger pre-registered plan |
| `UNMEASURABLE` | any of: fewer than the plan's n cases scored; a blinding leak; a judge calibration failure; or the budget cap hit (`INCOMPLETE`). An empty comparison is a failure, never a tie |

**Guards** (all must hold):
- invalid outcomes are not higher in B (at most +1 run);
- median cost per run B/A ≤ policy `eval.max_cost_increase` (default +25 %);
- for Review, the false-finding rate is not more than 10 pp higher.

The report always includes:
- the bootstrap 95 % CI of the mean difference;
- the per-case differences;
- the sign test;
- the cases left unscored, named beside the denominator.

**Batch of skills** (the ~16 default skills before GA, spec §11): each skill gets its own test, with Benjamini–Hochberg at q = 0.10 across the batch. Without that, about 1 in 20 useless skills would be promoted by chance.

**M3-24 (TokenLean optimisation groups) uses non-inferiority.**
- Rubric agents: margin -0.5 point.
- Developer: margin -15 pp.
- Cost must actually fall: the bootstrap CI upper bound of the cost difference must be < 0.

### Human calibration

- **Who and how much:** per (rubric version × judge model id), two blinded humans (team leads, via `agentic eval calibrate`) score 20 % of gate items, at least 10. That doubles if `judge_same_family`.
- **Accept the judge when:**
  - quadratic-weighted Cohen's κ between the judge and the human mean is ≥ 0.6;
  - judge and human mean are within ±1 point on ≥ 80 % of items;
  - for pairwise, it agrees with the human majority on ≥ 75 % of non-tie pairs.
- **If the humans disagree with each other** (κ < 0.6), the rubric is ambiguous and gets fixed first.
- **When to recalibrate:** whenever the judge model id or rubric hash changes, plus a 5-item drift check each quarter.
- κ is implemented in stdlib next to the other stats.

### Corpus rules

- **Splits.** Each case is `dev` (prompt and skill iteration allowed) or `holdout` (gates and A/Bs only). A case the skill author or Learning Analyst used to write a change is excluded from that change's evaluation. M3-14's regression case enters `dev` for the next proposal and `holdout` only after it.
- **Seeded defects** (Review, QA) are mechanical truth, from three sources:
  - harvested `defect` cases: the pre-fix code inside a realistic diff;
  - mutation-injected bugs, each confirmed by a failing test;
  - human-written cases.

  Every seeded defect records file, line range and category.
- **Case files carry provenance:** repo, sha, parent, `synthetic`, licence. They are hashed into a `manifest.json`.

## Sample size and cost per run (defaults in policy `eval:`)

**Cost assumptions** (not measured):
- tokens per run: BA 40k, Design 60k, Code Review 80k, QA 150k, Developer 600k (an agentic loop);
- judge calls: pointwise 6k, pairwise 9k per order;
- a blended **$4 per million tokens** (placeholder; the harness reports real `x-tokenlean-cost-usd`).

| Use | n (holdout) × k | Detects at ≥ 80 % power | Est. tokens | Est. cost |
|---|---|---|---|---|
| A/B, BA | 20 × 3 | +1.0 rubric | 6.6 M (agent 4.8 + judge 1.8) | about $26 |
| A/B, Design | 20 × 3 | +1.0 rubric | 9.0 M | about $36 |
| A/B, Code Review | 40 × 3 | +15 pp recall | 20 M | about $80 |
| A/B, QA | 40 × 3 | +15 pp catch | 36 M | about $144 |
| A/B, Developer | 40 × 3 | +15 pp tests-pass (+10 pp needs 80 × 3, about $1.15k) | 144 M | about $576 |
| Non-inferiority (M3-24), rubric agent | 30 × 3 | not worse than -0.5 | 10–14 M | about $40–55 |
| M1-64 gate (one variant, all five agents) | 20 × 3 (BA, Design, Developer, Review diffs); 30 × 3 QA | n/a: absolute gate, Wilson / bootstrap CI | about 61 M (Developer 36 M, QA 13.5 M) | about $245 |
| Nightly CI fixture mode | fixture corpus, recorded | plumbing and metric code only | 0 | $0 |

**Budget caps** (policy, defaults): $100 per A/B for BA, Design and Review; $250 for QA; $750 for Developer. At the cap, the run stops as `INCOMPLETE`. The cap also bounds runaway agent loops.

**Why this is worth paying.** A skill that adds 3k tokens to each of 5,000 runs a day costs about 15 M tokens, roughly $60 a day, across the org. The most expensive A/B costs about ten days of an unhelpful skill's overhead.

## Harness sketch (M1-63)

**Module layout:** `core/src/agentic/eval/`. Core stays standalone; Zyroflo is reached only through commons, wired by the CLI.

| Module | Responsibility |
|---|---|
| `cases.py` | Pydantic models `EvalCase`, `SeededDefect`, `TestTruth`, `CorpusManifest` |
| `corpus.py` | load and validate corpus dirs (fixture `library/eval/`, policy repo `eval/`, repo `.agentic/eval/`); per-class floors; manifest hashes; set-aside of contaminated cases |
| `runner.py` | builds a pydantic-evals `Dataset` from cases. The task wraps `agents.runner.AgentRunner.run(AgentTask)` in a fresh worktree at the case's parent sha, inside the sandbox; `repeat=k`, interleaved variants, concurrency limit. Records `AgentOutcome` (status, output, usage, `CostLog`) |
| `metrics.py` | pydantic-evals `Evaluator`s: `SchemaValid`, `TestsPass` (FAIL_TO_PASS / PASS_TO_PASS), `DefectRecall` + `FalseFindings` (matching, plus judge adjudication of unmatched findings), `QACatch`, `Cost` (`None` when unmeasured, never 0), `Duration` |
| `judge.py` | `RubricJudge` (pointwise, evidence quotes, reference-guided), `PairwiseJudge` (both orders), judge resolution from tier `eval-judge` with the family rule; always passes an explicit `model=` |
| `blind.py` | scrubber, X/Y flip with the mapping sealed until scoring, leak check |
| `stats.py` | `signflip_test`, `paired_bootstrap`, `sign_test`, `wilcoxon_signed_rank`, `wilson`, case-cluster bootstrap for proportions, weighted κ, Benjamini–Hochberg. Stdlib only; scipy cross-check in tests |
| `abtest.py` | `ABPlan` (pre-registered, hashed to the ledger), run orchestration, budget cap, verdict |
| `report.py` | `EvalReport` / `ABReport`, JSON plus Markdown; gate comparison against policy `eval.targets` |
| `replay.py` | cassette server for fixture mode (see below) |

Commons adds `sdlc_commons/corpus.py`: an adapter over Zyroflo `corpus_harvest` (classes, `synthetic`, set-aside) and the `corpus_replay` per-case regression rule (the same sha against its last run; a drop of more than 0.10, or any drop to zero, is a regression).

### Data model

Fields named, not final.

```text
EvalCase      id, agent_kind, case_class(scoping|review|defect|dev_task|qa), split(dev|holdout),
              source{repo, sha, parent_sha, synthetic, licence}, inputs{handoffs by kind, prompt},
              expected{golden_handoff? | key_points[] | seeded_defects[{file, lines, category}] |
                       tests{fail_to_pass[], pass_to_pass[]} | failing_criteria[]}, tags[]
EvalRun       run_id, case_id, label(X|Y), repeat, seed, agent@version + definition_sha,
              skills[name@sha], tier, resolved_model_id, backend, tokenlean_profile,
              status, retries, output_sha, metrics{name: float|None}, judge{model_id, rubric_sha,
              per_criterion, evidence}, cost_usd|None, cost_estimated, tokens, duration_s, error
EvalReport    report_id, mode(live|replay|record), agent, corpus_manifest_sha, split, n, k,
              judge config, runs[], aggregates{metric: {value, ci_low, ci_high, n_scored,
              unmeasurable}}, targets{metric: {target, value, verdict}}, cost_total, sigma_d
ABReport      EvalReport + plan_sha, variants (mapping revealed after scoring),
              per_case_diffs[], test{signflip_p, bootstrap_ci, sign_test, wilcoxon}, pairwise
              {win_rate, order_inconsistency}, guards{...}, verdict(PROMOTE|NO_BENEFIT|
              UNMEASURABLE|INCOMPLETE), calibration{kappa_w, within_1, n}
```

Reports go to `.agentic/local/eval/<report_id>/` (JSON, Markdown and per-run artefacts). Gate and promotion reports are committed or attached to the PR (M1-64, M2-46). Each run writes a ledger event: `eval.run_started`, `eval.plan_registered`, `eval.report_written`.

### CLI

```text
agentic eval run   --agent <ba|design|developer|code-review|qa|custom> [--corpus <dir>] [--split holdout]
                   [--repeat 3] [--mode live|replay|record] [--budget-usd N] [--out <dir>]
agentic eval ab    --agent <x> --plan <plan.yaml>        # variants: skill on/off, agent version,
                                                           # tier/model, TokenLean profile group
agentic eval gate  [--agents all]                         # M1-64: every target, one report
agentic eval calibrate export|import|score --rubric <id>  # human calibration, blinded
agentic eval harvest --repo <path> [--write]              # commons -> Zyroflo corpus_harvest
agentic skills eval <name> --agent <x>                    # M2-46 = `eval ab` with base vs base+skill
```

### CI fixture-corpus mode (M1-63 nightly)

- **Corpus:** `library/eval/fixtures/` holds a small corpus, about 3 cases per agent, from permissively licensed public repos or synthetic cases (`synthetic: true`), with **recorded responses**.
- **Cassette server.** `replay.py` grows the existing `TokenLeanStub` test helper (spike S2, `core/tests/backends/anthropic_stub.py`) into a stdlib HTTP **cassette server**. It speaks `/v1/chat/completions`, `/v1/messages` and `/v1/responses`, so it covers the native and `claude-code` backends alike: the CLI points `base_url` at it.
- **Cassette keys:** (case, variant, repeat, call index). Each cassette also stores the sha256 of the canonical request.
- **Mismatches:**
  - a missing cassette or a call-count mismatch **fails**;
  - a request-hash mismatch (an agent prompt changed) is reported as `stale_cassettes: n`, a warning that a re-record is due.

  So prompt work never breaks CI, but plumbing does.
- **Recording:** `--mode record` against local TokenLean (M1-67), run by a maintainer.
- **What CI asserts**, on Windows, macOS and Linux:
  - the report is produced and schema-valid;
  - every metric is computed and equals the committed expected report, which is deterministic because judge answers are recorded too;
  - the stats functions match their scipy-checked test vectors.
- **What it does not measure: quality.** Quality comes from live runs: M1-64 at M1, then on demand or weekly under the budget caps.

### Users of the harness

| Row | Use |
|---|---|
| M1-64 | `eval gate` on the holdout corpus |
| M2-46 | `skills eval` / `submit` attaches the `ABReport` |
| M3-14 | each proposal carries a regression `EvalCase`, run before and after |
| M3-24 | `eval ab` with non-inferiority plans per TokenLean group |
| M2-38 | the dashboard reads `EvalReport` JSON |

## Decision

1. **Engine: pydantic-evals 2.54.0** (MIT, same version as our pinned pydantic-ai, one new package). On top of it, our own blinding, judging, statistics and A/B layer in `core/src/agentic/eval/`. The rest are rejected as dependencies: inspect_ai (40+ deps, its own model layer, daily churn), deepeval (posthog telemetry, cloud coupling), promptfoo (Node), OpenAI evals (stale), lm-eval (wrong problem). The harvest and the per-case regression rule are reused from Zyroflo through `sdlc_commons`.
2. **Judging.**
   - Objective truth wherever possible: tests, seeded defects, seeded failing criteria, schema validity, cost.
   - For BA/Design, a pointwise analytic 0–10 rubric with evidence quotes and a reference, at temperature 0, by a judge **from a different model family** (tier `eval-judge`).
   - Pairwise with **position swap** as corroboration.
   - Human calibration on 20 % of items (κw ≥ 0.6).
3. **Blinding:** opaque X/Y labels with the mapping attached after scoring, a scrubber for skill and agent tells, and a leak check that voids the run.
4. **Statistics:**
   - the case is the unit, with **k = 3** repeats averaged per case;
   - the **paired sign-flip test** (one-sided 5 %) decides; the **paired bootstrap 95 % CI** is reported;
   - Benjamini–Hochberg (q = 0.10) across a batch of skills;
   - non-inferiority for TokenLean groups.
5. **Sample sizes:**
   - rubric agents: **20 × 3** (+1 point at 0.99 power);
   - pass-rate agents: **40 × 3** (+15 pp at 0.84–0.91). Detecting +10 pp needs 80 × 3, which is not the default;
   - TokenLean non-inferiority: **30 × 3**.

   The budget caps above apply.

## Recommended changes to plan rows

- **M1-59 (corpus).**
  - Counts: raise to **≥ 20 scoping, ≥ 20 review diffs carrying ≥ 40 seeded defects, ≥ 20 Developer tasks with FAIL_TO_PASS tests, ≥ 30 QA seeded failing criteria**, plus ≥ 5 `dev`-split cases per class. The 10 defect cases give ± 0.25 on the Developer and Review targets.
  - Location: the pilot-repo cases are internal data, so they live in the **policy repo `eval/`** (or the pilot repo's `.agentic/eval/`), not in `library/eval/`. `library/eval/` holds only the fixture corpus from permissive OSS repos or synthetic cases. This follows Zyroflo's "cases are the adopter's data" rule.
  - Estimate: likely +0.5–1 PW.
- **M1-63:** add the cassette server, `eval calibrate`, `ABPlan` pre-registration and the stdlib stats to its scope. Acceptance gains:
  - stats match scipy test vectors;
  - fixture mode is green on all three OSes;
  - a blinding leak voids a run.
- **M1-64 targets.** Keep the values; define how they are measured:
  1. **Schema validity:** 100 % schema-valid after ≤ 1 retry, over all n × k runs. 0 failures in 60 runs bounds the true rate at ≥ 95 % (rule of three).
  2. **BA & Design:** median of per-case mean rubric ≥ 7/10 over **20 holdout cases × 3**. Optional tail guard: ≤ 10 % of cases below 5. Valid only with a passed human calibration.
  3. **Developer:** ≥ 50 % pass@1 (mean over 3 runs) on **≥ 20 tasks**, and ≥ 75 % after one review loop.
  4. **Code Review:** recall ≥ 70 % over **≥ 40 seeded defects in ≥ 15 diffs**. "False findings ≤ 30 %" is the share of blocking or high findings that match no seeded defect **and** are judged not a real issue, with human confirmation on a sample. A finding that matches no seeded defect is not automatically false: real diffs contain real issues.
  5. **QA:** ≥ 80 % over ≥ 30 seeded failing criteria.
  6. **Pass rule:** the point estimate meets the target, with the 95 % CI shown. Targets are recalibrated at the M1 retro with the measured σ_d and CIs.
- **M3-24:** use the non-inferiority plan above as the documented procedure. The first A/B record is one TokenLean group on BA and Developer.

## Not measured

- **Noise parameters are assumed.** Run noise, judge noise and the per-case Beta mixtures are guesses; no real model call was made. The power tables must be re-run with the σ_d and per-case pass-rate spread from M1-63's first live run.
- **Token and dollar figures are placeholders**, until TokenLean cost headers exist (M1-65).
- **Not run:** inspect_ai, deepeval and promptfoo were judged from package metadata and documentation memory only. pydantic-evals was not run under a network block, though it needed no network.
- **Literature cited from memory, not re-verified:** position bias (Zheng et al. 2023; Wang et al. 2023), self-preference (Panickssery et al. 2024), judge panels (Verga et al. 2024).
- No macOS run of the probe (pure Python, so a low risk).

## Cost if wrong

- **Statistics and judging:** a policy setting plus the `ABPlan`, so n, k, margins and judge tier change without code. If σ_d turns out larger, the plans grow and so does cost, within the caps.
- **Engine:** if pydantic-evals churns badly, it sits behind `runner.py` and `report.py`. Our models, stats and judging don't depend on its types beyond `Dataset`, `Case` and `Evaluator`, so swapping it costs about 1 PW.
- **The highest-cost error would be skipping the larger corpus** (M1-59). Then the M1-64 gate and every Developer or Review A/B would be decided on noise.
