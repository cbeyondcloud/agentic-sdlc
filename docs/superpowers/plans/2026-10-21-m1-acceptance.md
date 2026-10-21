# M1 Acceptance (SP20: M1-68..M1-71) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Spec §1.3 scenarios 1, 2 and 4 pass in **standalone mode** as scripted end-to-end tests that drive the real `agentic` CLI on temporary git repositories: `feature-delivery` on a small existing repo (M1-68), a plain-English "Docs Writer" agent added with no code (M1-69), the same pipeline on a brand-new empty repo (M1-70), and the whole suite green on Windows native and the Linux container three times in a row (M1-71). A scenario that exposes a bug gets the fix and a regression test.

**Architecture:**
- **Folder `cli/tests/acceptance/`** (decision: the tests, the sample repo and the cassettes live together, next to the CLI tests whose fixtures they reuse; `library/` is shipped content and stays free of test data):
  - `sample-repo/` - the "existing repo": a tiny Python package (`invoices/`) with a passing test suite and a README. Copied into a temporary folder and committed as a real repo with history. `conftest.py` keeps pytest from collecting its tests.
  - `cassettes/<scenario>/acceptance/1.json` - the recorded model traffic, one cassette per scenario (`agentic.eval.replay.CassetteServer`; key `<scenario>/acceptance/1`).
  - `_acceptance_support.py` - the plumbing: the `Scenario` fixture (temporary HOME/AGENTIC_HOME, bare remote + `FakeGitHub` from `cli/tests/fake_github.py`, the cassette server in replay or record mode, the CLI runner), the scripted upstream used only when recording, the fake editor, and git helpers.
  - `test_accept_feature_delivery.py` (M1-68), `test_accept_custom_agent.py` (M1-69), `test_accept_empty_repo.py` (M1-70). Basenames are unique across the suite (pytest `prepend` import mode).
- **Model answers.** The CLI's gateway URL (`AGENTIC_GATEWAY_URL`) is the cassette server's per-scenario base URL; the real native backend, metering, contract and transport run unchanged over real HTTP to 127.0.0.1. **Replay** (default, CI) answers every call from the cassette and fails on a missing cassette, an extra call or an unused recorded call. **Record** (`uv run python scripts/record_acceptance.py`) forwards each call to a scripted upstream (no model, no network) that answers by the `X-Agentic-Step` identity header from a per-step queue of tool calls and handoffs written in the test file; the cassette is written only when every call succeeded. A changed prompt only counts as `stale` (reported, never a failure), as in eval fixture mode.
- **GitHub.** `FakeGitHub` (stdlib HTTP server) plus a local bare remote, exactly as `cli/tests/run/test_pr_contract.py` wires them: pushes really go to the bare repo, the PR is really opened through the hosting adapter.
- **Sandbox.** Real machine readiness. The installed standalone policy keeps `sandbox.required_for: [team, regulated]` only where a sandbox exists; on Windows native the scenario sets `required_for: []` in the person's copy (a documented user edit, like choosing models), so commands run guarded but unsandboxed there and inside `srt` in the Linux container.

**Tech Stack:** Python 3.12, pytest, the existing cassette server and fake GitHub. No new dependency.

**Spec:** §1.3 (acceptance), §6.1-6.2 (gates, loops, feature-delivery), §8.1 (init), §9.2 (sandbox), §14.5 (PR body), §24 (developer daily loop). Master plan rows M1-68..M1-71 and their Notes; DEFERRED items targeting them (DF-75, DF-129, DF-251, DF-253, DF-255).

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| Record/replay of model calls | `agentic.eval.replay.CassetteServer` (ours, S10; the task requires it) | vcrpy / pytest-recording (MIT): patch one HTTP library in process, cannot serve an agent CLI subprocess, and would duplicate the cassette server |
| Fake GitHub + remote | `cli/tests/fake_github.py` + bare remote (M1-58) | a recorded GitHub: our adapter contract tests already pin the API shapes |
| Driving the CLI | `agentic_cli.main.main` in process (as every CLI test) | pytest-console-scripts (MIT): a subprocess per command would triple the run time on Windows; the cassette server is HTTP so a subprocess also works if ever needed |

## Global Constraints

- Every acceptance step is marked **real** or **stubbed** in the test docstrings and in the track output. Stubbed by design: the model (cassettes of a scripted upstream), GitHub (fake API + bare remote), the human (CLI verbs with a scripted `$EDITOR`), "merge the init PR" (a local `git merge`), `agentic init`'s machine probe and downloads (`dev_init.default_deps`), and the OS sandbox on Windows native (not available there).
- Replay must be faithful: `CassetteServer.finish()` returns no problems; the test fails otherwise.
- No real network, no real model, no token on disk: the token-never-stored checks of M1-58 still hold.
- Windows and macOS: paths via `pathlib`, LF handoffs, the editor is `python <script>` quoted for both shells.
- Formatting, CI and records: no `# fmt: skip`; ruff line length 100; check pytest exit codes directly; `scripts/check.py --all` green on Windows and Linux three times in a row; TRACKER/DEFERRED/DECISIONS updated by the coordinator from this track's output.

## Review Focus

1. **Replay fidelity.** A run that makes one call more or fewer than recorded fails (the cassette server's `problems`). Test: every scenario asserts `finish() == []`.
2. **The QA loop is real.** The failing QA round is backed by a real failing test in the run's worktree (the step log shows pytest's failure), and the next Developer round receives the QA report. Test: `test_accept_feature_delivery`.
3. **Isolation.** The developer's checkout is untouched by the run (status clean, HEAD unchanged); every agent step is a commit on `sdlc/<run>`; the PR head is the pushed branch.
4. **Empty repo.** `agentic init --repo` on a repository with no commits does not crash.
5. **Custom agent without code.** The diff that adds Docs Writer is one agent file plus one pipeline entry.

---

### Task 1: Acceptance plumbing and sample repo

**Files:**
- Create: `cli/tests/acceptance/conftest.py`, `cli/tests/acceptance/_acceptance_support.py`, `cli/tests/acceptance/sample-repo/**`, `scripts/record_acceptance.py`

**Interfaces:**
- `Scenario` (fixture `scenario`): `.repo`, `.home`, `.bare`, `.github` (`FakeGitHub`), `.cli(*args) -> Result`, `.start_models(name, script)` (replay or record), `.finish_models() -> list[str]`, `.edit_with(old, new)`, `.standalone_policy` (path of `~/.agentic/policy/standalone`).
- `ScriptedUpstream(script: dict[str, list[Reply]])` - `Reply` is a handoff text or `{"tool": name, "args": {...}}`.
- `scripts/record_acceptance.py` - sets `AGENTIC_ACCEPTANCE_RECORD=1` and runs pytest on the folder.

- [ ] Write the support module and a smoke test that the sample repo is copied, committed and pushed to the bare remote.

### Task 2: M1-68 feature-delivery on the existing repo

**Files:** `cli/tests/acceptance/test_accept_feature_delivery.py`, cassette `cassettes/m1-68/...`

Steps (real CLI): `init --repo --standalone` (PR on fake GitHub) -> merge -> `init --standalone` (not ready: placeholder models) -> edit tiers (and the sandbox line on Windows) -> `init --standalone` (ready) -> `run feature-delivery --dry-run` -> `run` -> requirements gate: `reject --feedback`, then `approve` -> baseline, design -> design gate: `edit --file <design>` -> loop round 1 (review blocks), round 2 (QA fails on a real failing test, routes back to Developer), round 3 (clean) -> review gate `approve` -> publish (push + PR) -> `status`, `cost`, `logs`, `show`, `pr checks`.

- [ ] Write the test with the step script, record the cassette, fix what it exposes (each with a regression test), replay green.

### Task 3: M1-69 Docs Writer

**Files:** `cli/tests/acceptance/test_accept_custom_agent.py`, cassette `cassettes/m1-69/...`

- [ ] A plain-English `docs-writer.md` (type `general`) committed in the repo's `.agentic/agents/`, one step entry added to the person's `feature-delivery.yaml`; the run's PR carries the README section the agent wrote; assert the change set is exactly those two edits.

### Task 4: M1-70 brand-new empty repo

**Files:** `cli/tests/acceptance/test_accept_empty_repo.py`, cassette `cassettes/m1-70/...`; likely `cli/src/agentic_cli/repo_setup.py` (unborn HEAD).

- [ ] `git init` + `agentic init --repo --standalone` -> merge -> the same pipeline -> PR.

### Task 5: PR body shows cost and logs

**Files:** `core/src/agentic/telemetry/trailers.py` (`pr_body(..., cost_usd=, logs=)`), `cli/src/agentic_cli/publishing.py`, tests in `core/tests/telemetry/` and the M1-68 test.

- [ ] The PR description names the run's total cost and how to read its logs (`agentic logs <run>`, or the control plane link).

### Task 6: Deferred items for M1-71 and the green runs

- [ ] DF-255: corpus references compare each path component's case with the directory entry (regression test).
- [ ] DF-253: confirm the architecture test passes when `check.py` runs from PowerShell.
- [ ] `uv run python scripts/check.py --all` three consecutive green runs; record them.
