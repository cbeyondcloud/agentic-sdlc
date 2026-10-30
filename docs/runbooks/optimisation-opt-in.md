# Turning on a gateway optimisation, one group at a time (M3-24, TokenLean ST-7)

**Who:** the platform owner (runs the gateway) and the eval owner (runs the eval harness).
**When:** after the pilot has a harvested holdout corpus, and never during a rollout wave's
first two weeks. **Method:** spike S10 / decision D41, a pre-registered non-inferiority A/B.

The org gateway runs TokenLean's LITE profile (`config/profiles/agentic.yaml` in the
TokenLean repository): the gateway itself is on (keys, spend caps, metering, failover,
provider prompt caching, exact-match caching, safety groups in `flag` mode), and **every
group that changes the prompt, the model or the answer is off**. The framework measures
agent quality on its own harness, so a group comes back on only when that harness shows
the agents are no worse with it and the cost really falls.

## The rules

1. **One group at a time.** Two groups turned on together cannot be told apart when quality
   moves. The next group starts only after the previous one has run a week in production.
2. **Pre-registered.** The plan (agent, variants, corpus, metric, margin, n x k, seed, judge,
   guards, budget) is written and its sha256 recorded before the first run. Changing any of
   it is a new plan; the old record stays.
3. **Every agent the group touches.** Gateway groups act on every call, so each built-in
   agent with an S10 margin gets its own plan: the Business Analyst and Design on the
   rubric (margin -0.5 point), the Developer on tests-pass (margin -15 points). QA and Code
   Review are watched on their gate metrics after the switch (no S10 margin yet).
4. **PROMOTE on every plan, or the group stays off.** NO_BENEFIT, UNMEASURABLE and
   INCOMPLETE all mean off. An inconclusive result is not "almost promoted".
5. **The cost must actually fall:** the bootstrap CI upper bound of the per-case cost
   difference B - A is below zero (`require_cost_decrease`). A group that keeps quality but
   saves nothing is not worth its risk.
6. **Records are kept**, whatever the verdict, in the org policy repo under
   `eval/records/<date>-<group>.md`, and linked from the pull request that turns the group on.

## Which groups, in which order

The table is the platform team's starting recommendation for owner review; the group
names are TokenLean's (LITE profile). Each row still needs its own A/B record.

| Order | Group | What it does | Why this place |
|---|---|---|---|
| 1 | `G21_cache_alignment` (`stabilise`) | moves volatile prompt text so the provider's prompt cache hits more | changes where text sits, not what it says; the cheapest likely saving |
| 2 | `G14_tool_output` | shortens tool results (shell output, file reads) | tool output dominates Developer and QA loops; risk: a test failure's detail is cut |
| 3 | `G26_context_budget` | compacts long histories | long Developer runs; risk: an early instruction is lost |
| 4 | `G8_tools`, `G16_agent_arch` | prunes tool definitions, compacts system prompts | the tool set is already policy-narrowed, so less to gain |
| 5 | `G1_compression` | compresses prompts | broadest effect, so hardest to show non-inferior |

Not candidates without a design change, because they cut across what the framework
governs itself:

| Group | Why not |
|---|---|
| `G4_bypass`, `G5_cache` L2 and step cache | an answer to another question, or a replay, instead of the agent's own call |
| `G6_routing` | the policy repo's tiers choose the model; rules-only routing is TokenLean ST-10, decided separately |
| `G3_doc_pipeline`, `G7_retrieval`, `G10_memory` | inject content outside the run's audited context and the framework's own memory |
| `G11_output`, `G12_reasoning`, `G25_adaptive_reasoning` | output budget and effort are resolved per tier by the framework (unhonoured-setting policy) |
| `G15_server_compute` | runs tools on the gateway, outside the agent sandbox |
| `G17_loop` | the agent types' caps already bound turns, minutes and budget |
| `G13_batch`, `G23_streaming_compression`, `G28_ccr` | defer calls, compress answers or replace content with references the agent cannot read back |
| `G2`, `G9`, `G19`, `g20`, `g22`, `G27` | little expected value for agent pipelines; revisit on evidence |

## The procedure

### 1. Prepare the corpus

S10 sizes are 30 cases x 3 repeats for rubric agents and 40 x 3 for the Developer. The
framework's public corpus (`library/eval`) holds 20 BA, 20 Design and 20 dev-task holdout
cases, so add the pilot's harvested cases (`agentic eval harvest --repo <path> --write`,
kept in the policy repo's `eval/`) until each agent's holdout reaches its n. A run with
fewer cases than its plan's n is refused before it starts.

The judge must have an accepted human calibration for the rubric and the judge model
(`agentic eval calibrate export|score`); without it a rubric A/B is UNMEASURABLE.

### 2. Stand up the two gateways

Run the A/B against two gateways that differ only in the group, never against production:

```bash
# A: the current profile, as its own LITE stack so production traffic is untouched
cd gateway-a/deploy/lite/local && LITE_PROJECT=ab-a LITE_PROXY_PORT=4201 ./up.sh
# B: a second checkout whose .env.lite adds the group's overlay:
#    LITE_EXTRA_OVERLAYS=/app/lite/config/ab-g14.yaml
cd gateway-b/deploy/lite/local && LITE_PROJECT=ab-b LITE_PROXY_PORT=4202 ./up.sh
```

(On Windows, `up.ps1` with the same environment variables.) Each checkout keeps its own
`.env.lite`, so the two stacks cannot share an overlay by accident.

```yaml
# deploy/lite/config/ab-g14.yaml (B only)
groups:
  G14_tool_output:
    enabled: true
```

On **both** stacks, also turn the exact-match cache off for the A/B (`groups.G5_cache.enabled:
false` in a shared overlay), so each repeat is a fresh answer: S10's three repeats measure
the agents' variation, and a cached repeat would hide it. Use the same provider keys, the
same models (the org `tiers.yaml`) and the same TokenLean version on both.

### 3. Write and register the plans

```python
from agentic.eval.abrecord import optimisation_plan

plan = optimisation_plan(
    group="G14_tool_output", agent="business-analyst",
    corpus_manifest_sha=corpus.manifest.sha, seed=20261030,
    judge_model="<eval-judge model id>", rubric_sha=corpus.manifest.files["rubrics/ba-requirements.yaml"],
)
```

`optimisation_plan` fills in S10's metric, margin, n, k, budget cap (100 USD for rubric
agents, 750 for the Developer) and the cost-decrease guard. `run_ab` records the plan's sha
(`eval.plan_registered`) before the first run.

### 4. Run the A/B

Each variant's runs go to its own gateway (A to port 4201, B to 4202); the harness labels
them X and Y at random and reveals which was which only after scoring. The command for this
is `agentic eval ab --plan <plan.yaml>` with a gateway per variant; it lands with the first
live record (DF-855). Until then the procedure has been proven on recorded data only (below).

### 5. Record the result

```python
from agentic.eval.abrecord import render_ab_record

text = render_ab_record(plan, result, group="G14_tool_output", date="2026-11-20", dry_run=False)
```

The record holds the plan and its sha, the verdict and its reasons, the cases scored and
those left unscored, the mean difference with its bootstrap CI, the sign-flip, sign and
Wilcoxon tests, every guard (including the cost difference's CI), the pairwise judgement,
the per-case differences, the next step and a sign-off table. Commit it to the policy repo.

### 6. Turn the group on

Only when every plan for the group says **PROMOTE**:

1. A pull request to the gateway deployment adds the overlay to `LITE_EXTRA_OVERLAYS`
   (staging first), with the records linked. The platform owner and the eval owner approve.
2. Watch staging for a week: **Agent quality** (gate rejection rate per agent, correction
   rate), **Team view** (cost per run), **Limits and safety** (limit hits, gateway safety
   events). Any agent's gate rejection rate up by more than 5 points against the week
   before, or an invalid-output rise, rolls it back.
3. Production, the same way. Note the date in the record.
4. When the first group is on, the Executive ROI dashboard's gateway savings need a baseline
   column (DF-787, now targeting the first enablement).

### 7. Roll back

Remove the overlay from `LITE_EXTRA_OVERLAYS` and restart the proxy (a missing overlay stops
it from starting, so remove the entry, not just the file). Nothing in the framework changes:
runs continue against the gateway as before. Add the rollback, its reason and the dashboards
that showed it to the record.

## The dry run (proof of the template)

`core/tests/eval/test_eval_abrecord.py` runs the whole A/B decision on the recorded fixture
cassettes, with no model called: both variants replay the recorded Business Analyst answers
and judge scores, B with the recorded cost times 0.8. The record it writes is committed as
[`optimisation-opt-in/dry-run-record.md`](optimisation-opt-in/dry-run-record.md), and the
test fails if the template and the committed record drift apart (run the test file as a
script to rewrite it). Its verdict is NO_BENEFIT on purpose: with three cases the smallest
possible sign-flip p is 1/8, which shows why a real record needs S10's sizes.

The first live record (one group, on the Business Analyst and the Developer) needs real
models and is deferred to M3-25 (DF-854).
