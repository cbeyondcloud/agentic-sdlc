# b20-prelive-fixes: four blockers for the first live run and the agent gate

**Goal:** close DF-590, DF-398, DF-397 and DF-392 before the first live standalone run
(`agentic run feature-delivery` through a local TokenLean LITE, native backend) and the
M1-64 agent quality gate. Each is confirmed in the code first, then fixed test-first.

## Files

- `commons/src/sdlc_commons/worktrees.py` - `StepCheckpoints.begin` / `finish` (DF-590).
- `core/src/agentic/policy/model.py`, `policy/resolve.py`, `agents/models.py` - an output
  token cap per tier, sent on every native model call (DF-398).
- `core/src/agentic/transport/cost.py` - TokenLean's default-rate flag (DF-397).
- `cli/src/agentic_cli/evaluation.py`, `cli/src/agentic_cli/commands/evals.py` - the eval
  corpus hidden from a live Developer eval (DF-392).
- Tests: `commons/tests/test_step_checkpoints.py`, `core/tests/...` (models, cost,
  policy), `cli/tests/test_eval_workspaces.py`.

## Interfaces

- `StepCheckpoints.begin(fresh=True)` puts every tree back on its run branch
  (`Tree.branch`) at the step's base. `finish` brings work an agent committed on its own
  branch or a detached HEAD back onto the run branch when the run branch's tip is an
  ancestor (a fast-forward); otherwise it raises `WorktreeError`, commits nothing and
  leaves the work where it is.
- `TierSpec.max_output_tokens: int | None` (optional, > 0) -> `ResolvedAgent.max_output_tokens`
  -> `model_settings()["max_tokens"]`, falling back to `DEFAULT_MAX_OUTPUT` (16,384: the
  same figure the budget hold already assumes).
- `cost.ESTIMATED_HEADER = "x-tokenlean-cost-estimated"`: when present, TokenLean's cost
  is not `reported`: it is re-priced with the policy price when the tokens and a price are
  known, else kept as `estimated` with the reason.
- `eval run --allow-unsandboxed`: a live or record run of dev-task holdout cases without
  srt is refused unless given; under srt the corpus folder is `deny_read`.

## TDD steps

1. DF-590: failing tests on a real temp repo (fresh begin returns to the run branch;
   finish fast-forwards an agent's branch and a detached HEAD; finish refuses a diverged
   HEAD and keeps the work). Fix `begin`/`finish`. Commons tests green.
2. DF-398: failing tests (default cap sent; tier cap resolved and sent; schema accepts
   an optional field and refuses 0). Fix. Core policy/agents tests green.
3. DF-397: failing tests (flag + policy price -> estimated at the policy price; flag and
   no price -> estimated at TokenLean's figure; no flag -> reported as before; stream).
   Fix. Core transport tests green.
4. DF-392: failing tests (srt rules deny the corpus; a live holdout dev-task run without
   srt is refused; the flag lets it run with a note). Fix. CLI eval tests green.
5. Records: DEFERRED.md rows Done; TRACKER.md M1-64 / M3-17 notes (rows that list them).

## Review Focus

- DF-590: no path loses agent work silently; hooks never run (the checkout and ref moves
  use the empty hooks folder).
- DF-398: backward compatible policy (field optional); no vendor names.
- DF-397: a TokenLean estimate is never shown as `reported`.
- DF-392: srt `deny_read` never hides the agent's own tree (`allow_read` when the tree is
  inside the corpus); the refusal names the remedy.

## Added mid-track (coordinator): DF-1004

Eval calls (agent under test and judge) and the learning before/after measurement send
the gateway's per-request cache opt-out (`x-no-cache: true`, which every TokenLean ingress
maps to `x_no_cache`), through a `fresh` option on `make_async_client` / `build_model_for`
/ `metered_model` in the transport layer. Pipeline runs keep the cache. Test-first in
`core/tests/transport/test_client.py`, `core/tests/agents/test_models.py`,
`cli/tests/test_eval_command.py` and `cli/tests/run/test_learn_apply.py`.
