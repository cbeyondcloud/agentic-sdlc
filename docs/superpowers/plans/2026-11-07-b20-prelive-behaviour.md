# b20-prelive-behaviour: three owner decisions before the first live run

> Track plan (writing-plans format). Owner decisions taken 2026-11-07: DF-359, DF-357, DF-108.

**Goal:** before any model spend, (1) a repo whose test command the Developer or QA agent
may not run fails preflight with the line to add; (2) a step paused at its own budget can be
given more with `agentic resume <run> --add-budget <usd>`, clamped to the run's remaining
budget and the policy cap, and recorded with who granted it; (3) the first live acceptance
run and the M1-64 gate have configured spend caps that the runbooks and the eval harness read.

**Reuse:** the existing preflight (`Runtime.preflight`, M1-25) and the runtime shell guard
(`sdlc_commons.guards.Guard.for_agent(...).command`, the very check a run applies), the
budget ledger (`BudgetSlice.open`, M1-13), the approval ledger (`GateLedger.record`, the
ledger adapter), the eval policy's `eval.budget_usd_by_agent` (M1-63). No new dependency:
the manifest readers are `tomllib`, `json` and a regex (OSS search: test-runner detectors
such as `detect-test-runner`-style packages are JS-only or heavyweight; a ten-row table is
smaller than any dependency).

## Files

- `cli/src/agentic_cli/test_commands.py` (new): the manifest table, `detect(repo)`, and
  `problems(report, pipeline, definitions, policy_file, repo)` -> preflight `Problem`s.
- `cli/src/agentic_cli/runtime.py`: `Runtime.preflight` adds those problems.
- `core/src/agentic/policy/resolve.py`: `budget_ceiling(policy, type, team)`, the type/team cap.
- `core/src/agentic/engine/state.py`: `RunState.step_grants` (step -> USD granted).
- `core/src/agentic/engine/engine.py`: `resume(add_budget_usd=..., who=...)`; a step slice is
  opened at its agent's limit plus its grant.
- `cli/src/agentic_cli/commands/runs.py`, `cli/src/agentic_cli/driving.py`: `--add-budget`,
  the refusal of a bare resume of a step-budget pause, the next-step hint.
- `docs/runbooks/live-spend-caps.yaml` (new), `scripts/live_caps.py` (new),
  `docs/runbooks/m1-64/make_policy.py`, `docs/runbooks/m1-64-quality-gate.md`,
  `docs/spikes/S10-ab-eval-method.md`, `docs/runbooks/cli-operations.md` if it lists resume flags.

## Interfaces

```python
# agentic_cli.test_commands
@dataclass(frozen=True)
class TestCommand:
    command: str   # what an agent would type, e.g. "go test ./..."
    allow: str     # the allow-list line that covers it, e.g. "go test"
    source: str    # the manifest it came from, e.g. "go.mod"
def detect(repo: Path) -> list[TestCommand]
def problems(report, pipeline, definitions, *, repo, policy_file) -> list[Problem]

# agentic.policy.resolve
def budget_ceiling(policy: OrgPolicy, agent_type: str, team: str | None) -> Decimal | None

# Engine.resume(..., add_budget_usd: Decimal = Decimal(0), who: str | None = None)
```

## TDD steps

1. DF-359: a failing test per manifest row for `detect` (pyproject/pytest, uv.lock, package.json
   test script with npm/yarn/pnpm, Makefile `test:`, go.mod, pom.xml/mvnw, build.gradle/gradlew,
   Cargo.toml; none for an empty repo or npm's placeholder script). Then a failing CLI test: a
   repo with `go.mod` and a developer agent whose `shell:` lacks `go test` -> `run --dry-run`
   exits 3 naming the step, the agent, `go test ./...`, the line `- go test` and the files;
   a deny rule covering it -> the message says deny beats allow. Implement; green.
2. DF-357: failing engine tests: `add_budget_usd` re-runs the step with own + grant; clamped
   by the run's remaining budget and by the type/team cap (the result says so); refused on a
   pause that is not step-budget and without a person; recorded in the approval ledger with
   who and how much. Failing CLI tests: `--add-budget` resumes; a bare resume of a step-budget
   pause exits 2 with the exact command. Implement; green.
3. DF-108: failing tests for `scripts/live_caps.py`: the caps load and the gate's per-agent
   split sums to its total; remaining = cap minus recorded spend (eval report.json, run.json
   budget snapshots); exit 1 with "cap reached" at zero. make_policy writes the per-agent caps
   into eval.yaml. Implement; update the runbook and S10.
4. Records: DEFERRED DF-359/357/108 Done; ruff; package tests.

## Review Focus

1. The preflight check uses the guard's own allow/deny reading (no second parser), so a
   command preflight passes is one a run allows.
2. A grant never exceeds the run's remaining budget nor the policy cap; the clamp is shown;
   the record names the person and the amounts; old run.json files still load.
3. The caps are config (YAML), read by the helper and by make_policy; nothing in engine code.
