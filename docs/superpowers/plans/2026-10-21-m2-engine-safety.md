# M2 Engine Safety Implementation Plan (M2-22, M2-24)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** No process an agent step started outlives the step, whichever way it ends: completed, stopped by a limit, cancelled by a stopped run, a Ctrl-C of the CLI, or (on Windows) the CLI itself being killed. Every backend (claude-code, codex, the ACP runner, the native backend's shell commands and the code-intelligence build) uses one helper, `agentic.backends.processes`, proved by one cross-OS test set (M2-22, DF-451, DF-328, DF-338). And no Developer step hands a reviewer more than one readable batch: a step whose change exceeds the policy's diff budget is sent back once to split its work, and then waits at a `diff-budget` gate; the reviewer only ever sees batches within budget (M2-24, DF-13).

**Architecture:**
- **`agentic.backends.processes`** (core, standard library only):
  - `spawn_group()`: the `subprocess`/`asyncio` keyword arguments that give a child its own process group (POSIX: a new session; Windows: a new process group).
  - `session_leader(argv)`: unchanged; for CLIs an SDK starts (the POSIX launcher).
  - `ProcessTree`: one child and everything below it. `adopt(process)` contains it: on Windows the child is assigned to a **Job Object** with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (ctypes, kernel32), so every process it starts afterwards is in the job, an orphan included, and the job dies with the handle (when the CLI exits, however it exits). `kill()` kills the tree: `taskkill /T` plus `TerminateJobObject` on Windows; descendants by parent id, the process group, and (Linux) the session on POSIX. A root that has already exited is never signalled by pid (pid reuse), only its group/session/job. `close()` is `kill()` plus releasing the job: called when the child's step is over.
  - `ProcessScope` + `process_scope()`: a context-variable scope. Each adopted tree registers in the innermost scope; closing the scope closes every tree still registered. Context variables reach `asyncio` tasks and the threads pydantic-ai runs synchronous tools in, so a build started inside a tool is in the step's scope.
  - `run_contained(argv, ...)`: the synchronous `subprocess.run` replacement for helpers (code intelligence): own group, adopted tree, whole tree killed on timeout, closed at return.
  - `kill_tree(pid)`: kept for callers with only a pid.
- **The engine opens a scope per agent step** (`_agent`: around `runner.run` / `runner.resume`). Whatever the backend, whatever the outcome, leftovers die when the step ends; a backend's own kill on a time limit or a cancellation still runs first.
- **Backends consolidated** (DF-451): claude-code, codex, the ACP runner and `agentic.sandbox.process.run_process` adopt their child into a `ProcessTree` and kill through it; their private `_kill_tree` / `_descendants` / `_SESSION` copies go. An architecture test refuses `taskkill` / `killpg` anywhere else in core.
- **Code intelligence** (DF-328, DF-338): `LocalIndex._run` and the query subprocess use `run_contained` (cancellable by the step's scope); the build lock becomes cross-process (`filelock`, a lock file in the data folder) besides the in-process lock.
- **Diff budget** (M2-24):
  - Policy: `OrgPolicy.review_batch` (`org.yaml`): `max_files`, `max_lines`, `exempt` (None = Zyroflo's defaults).
  - `sdlc_commons.guards.Guard` gains `budget_exempt`: the org's list (default Zyroflo's); a repository's own `budget_exempt` may only drop entries, never add (DF-13).
  - `sdlc_commons.worktrees.ReviewBatchBudget(guard, checkpoints)`: `limits()` (text for the prompt) and `over(base, head)` (Zyroflo's `policy.budget` over `git diff base head` of every component, with a fixed prefix and no external diff or textconv).
  - Engine protocol `ReviewBatch` and `Engine(batch_budget_for=...)`. A writing step is told the budget in its prompt. After its checkpoint, an over-budget change is recorded (`diff_budget_exceeded`), rolled back, and the step runs again at once with the breach as feedback (asking for a first self-contained batch). A second breach writes the handoff and waits at the `diff-budget` gate: approve accepts the batch (attributed in the gate ledger), reject with feedback runs the step again, or stop.
  - The CLI wires it (`Runtime.engine`).

**Tech Stack:** Python 3.12 standard library (`ctypes` for kernel32 Job Objects; `signal`, `os.killpg`), `filelock==4.0.12` (Unlicense, pure Python, already in `uv.lock` via dev tools, uploaded 2026-10-05) as a new core dependency, Zyroflo's `policy.budget` through `sdlc_commons`. pytest.

**Spec:** §6.1 (diff-size budget per review batch), §8.6 (children are terminated), §27 "No runaway process" / "Safe execution". Tracker rows M2-22, M2-24; deferred DF-13, DF-328, DF-338, DF-451.

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| Windows process-tree containment | Win32 Job Objects through `ctypes` (stdlib) | `psutil` (BSD-3): a compiled, unsigned `.pyd` - no unsigned native binaries on developer laptops; `pywin32`: native, and its licence is PSF-like plus extras; `jobobject`-style PyPI wrappers: unmaintained, tiny |
| POSIX tree kill | `os.killpg`, sessions, `/proc` / `ps` (existing helper) | `psutil` as above |
| Diff budget | Zyroflo `policy.budget` via `sdlc_commons.guards.Guard.diff` (already the adapter) | - |
| Cross-process lock | `filelock` 4.0.12 (Unlicense, unmodified, pure Python) | hand-written `msvcrt`/`fcntl` loop: filelock is the standard and already locked |

## Global Constraints

- core imports neither `sdlc_commons` nor Zyroflo; the engine sees the diff budget only through its `ReviewBatch` protocol.
- No vendor names in the engine, policy model or protocols.
- Windows and POSIX alike: every process test runs on Windows native and in the Linux container; macOS takes the POSIX path (`ps` instead of `/proc`).
- A pid whose process has exited is never signalled (Windows and POSIX reuse pids).
- Never `# fmt: skip` / `# fmt: off`; ruff line length 100; check pytest exit codes directly; `uv run python scripts/check.py --all` green.
- TRACKER.md, DEFERRED.md and DECISIONS-*.md are not edited here (the coordinator applies this track's output).

## Review Focus

1. **An orphaned grandchild** (its parent exits first) dies when the tree is killed or closed - Windows via the job (taskkill /T cannot see it), POSIX via the process group. Test: `test_an_orphaned_grandchild_dies_with_the_tree`.
2. **A killed CLI on Windows** (TerminateProcess, CTRL_BREAK) takes its agent processes with it; a Ctrl-C (SIGINT) of a CLI-like asyncio program on POSIX kills the tree through cancellation. Test: `test_ctrl_c_of_the_cli_leaves_no_orphan`, `test_a_killed_cli_leaves_no_orphan_on_windows`.
3. **A code-intelligence build in a tool thread** is killed when the step's scope closes (time limit). Test: `test_closing_the_scope_kills_a_build_in_a_thread`.
4. **pid reuse**: a tree whose root exited is never killed by pid. Test: `test_a_finished_root_is_not_signalled_by_pid`.
5. **A repo cannot widen the budget exemptions** (`budget_exempt: ["**"]`). Test: `test_a_repo_cannot_add_budget_exemptions`.
6. **The reviewer never sees an over-budget batch**: the review step does not run after an over-budget develop step until it is split or a person accepts it. Test: `test_the_reviewer_sees_only_batches_within_budget`.

---

### Task 1: The shared process helper (M2-22, DF-451)

**Files:**
- Modify: `core/src/agentic/backends/processes.py`
- Test: `core/tests/backends/test_processes.py` (one cross-OS set)

**Interfaces:**
- `spawn_group() -> dict[str, object]`
- `class ProcessTree`: `adopt(process) -> ProcessTree` (`process` has `.pid` and `.returncode`), `kill() -> None`, `close() -> None`, `contained: bool` (Windows: in a job), context manager closing on exit.
- `class ProcessScope`: `add(tree)`, `discard(tree)`, `close()`; `process_scope()` context manager; `current_scope()`.
- `run_contained(argv, *, env=None, cwd=None, input=None, timeout=None) -> subprocess.CompletedProcess[str]` (UTF-8, errors replaced; `subprocess.TimeoutExpired` after the tree is killed).

- [ ] Step 1: failing tests: kill stops the tree; an orphaned grandchild dies with the tree (RF1); a grandchild in its own group/session dies while its parent lives; close kills leftovers after the root exited; a finished root is not signalled by pid (RF4); scope close kills every registered tree; scope reaches a worker thread; `run_contained` returns output and on timeout kills the grandchild; Windows: the child is in a job.
- [ ] Step 2: run, see them fail (no `ProcessTree`).
- [ ] Step 3: implement (ctypes job: `CreateJobObjectW`, `SetInformationJobObject` with `JOBOBJECT_EXTENDED_LIMIT_INFORMATION`, `OpenProcess(PROCESS_SET_QUOTA|PROCESS_TERMINATE)`, `AssignProcessToJobObject`, `TerminateJobObject`, `CloseHandle`; argtypes declared for 64-bit handles).
- [ ] Step 4: pass on Windows; commit `feat(core): one process-tree helper with Windows job objects and step scopes (M2-22)`.

### Task 2: Backends onto the helper (DF-451)

**Files:**
- Modify: `core/src/agentic/backends/claude_code.py`, `codex.py`, `acp/runner.py`, `core/src/agentic/sandbox/process.py`
- Test: `core/tests/backends/test_processes.py` (`test_only_the_helper_kills_process_trees`), existing backend tests unchanged

- [ ] Step 1: failing architecture test: no `taskkill`, `killpg` or `_descendants` outside `processes.py` in `core/src`.
- [ ] Step 2: claude-code: the client is made before `async with`, a watcher adopts the CLI as soon as the SDK has started it (also fixes a stop during the SDK handshake killing nothing); `_launcher` uses `session_leader`. codex and ACP: adopt after spawn, kill through the tree, close in `finally`. `run_process`: `spawn_group`, adopt, kill through the tree; a finished command's background processes are left to the step scope (they may serve later commands of the same step).
- [ ] Step 3: backend tests pass (claude-code I4, codex time limit, ACP, srt unit/live); commit `refactor(core): claude-code, codex, ACP and the sandbox runner share one process helper (DF-451)`.

### Task 3: A process scope per agent step (M2-22)

**Files:** Modify `core/src/agentic/engine/engine.py`. Test: `core/tests/engine/test_process_scope.py`.

- [ ] Step 1: failing tests: a runner that leaves a grandchild behind (completed outcome) - it is gone when the step ends; a runner raising an error - gone; a runner cancelled (stopped run) - gone.
- [ ] Step 2: wrap `runner.run`/`runner.resume` in `process_scope()`.
- [ ] Step 3: pass; commit `feat(core): nothing an agent step started outlives the step (M2-22)`.

### Task 4: Code intelligence cancellable and locked across processes (DF-328, DF-338)

**Files:** Modify `core/src/agentic/codeintel/index.py`, `crg.py`, `core/pyproject.toml` (`filelock==4.0.12`). Test: `core/tests/codeintel/test_index_processes.py`.

- [ ] Step 1: failing tests: a slow build in a worker thread is killed when the scope closes (RF3); two processes ensuring one data folder build once (the lock file).
- [ ] Step 2: `_run` and `_query` use `run_contained`; `ensure` holds the in-process lock and `FileLock(data_dir / "build.lock")`.
- [ ] Step 3: pass; commit `fix(core): code-intelligence builds die with the step and lock across processes (DF-328, DF-338)`.

### Task 5: Ctrl-C and a killed CLI (M2-22)

**Files:** Test: `core/tests/backends/test_processes_cli.py`, helper `core/tests/backends/cli_like.py` (an `asyncio.run` program that starts a contained child with a grandchild inside an engine-like scope, prints pids, waits).

- [ ] Step 1: POSIX: SIGINT the program - KeyboardInterrupt cancels the task, the scope closes, no pid survives. Windows: CTRL_BREAK_EVENT to its process group, and TerminateProcess - the job closes with the program, no pid survives.
- [ ] Step 2: pass on Windows and Linux; commit `test(core): no orphan survives a Ctrl-C or a killed CLI (M2-22)`.

### Task 6: Review batch policy and the exemption floor (M2-24, DF-13)

**Files:** Modify `core/src/agentic/policy/model.py`, `load.py`, `templates/standalone/policy/org.yaml`, `commons/src/sdlc_commons/guards.py`. Test: `core/tests/policy/test_review_batch_policy.py`, `commons/tests/test_guard_budget.py`.

**Interfaces:** `ReviewBatchPolicy(max_files: int | None, max_lines: int | None, exempt: list[str] | None)`; `OrgPolicy.review_batch`; `Guard(..., budget_exempt: Iterable[str] | None = None)`.

- [ ] Step 1: failing tests: `review_batch` loads from `org.yaml`, refuses 0 or unknown keys; a repo's `budget_exempt: ["**"]` is ignored (RF5); a repo may drop an exemption; the org's list replaces Zyroflo's.
- [ ] Step 2: implement; pass; commit `feat: review batch policy; a repository cannot widen the diff budget's exemptions (M2-24, DF-13)`.

### Task 7: The review batch budget over the run's trees (M2-24)

**Files:** Modify `commons/src/sdlc_commons/worktrees.py`. Test: `commons/tests/test_review_batch_budget.py` (real git repo).

**Interfaces:** `ReviewBatchBudget(guard: Guard, checkpoints: StepCheckpoints)`: `limits() -> str`, `over(base, head) -> list[str]`.

- [ ] Step 1: failing tests: a change of 3 files within a 2-file budget is over; a lock file is not counted; `diff.noprefix` / `color.ui=always` / an external diff configured in the repo do not change the count; no change is never over.
- [ ] Step 2: implement; pass; commit `feat(commons): review batch budget over a step's checkpoints (M2-24)`.

### Task 8: The engine asks for the work to be split (M2-24)

**Files:** Modify `core/src/agentic/engine/engine.py`. Test: `core/tests/engine/test_diff_budget.py`.

**Interfaces:** `class ReviewBatch(Protocol)`: `limits() -> str`, `over(base, head) -> list[str]`; `Engine(batch_budget_for: Callable[[str], ReviewBatch] | None = None)`; pending gate id `diff-budget`.

- [ ] Step 1: failing tests: a writing step's prompt names the budget; an over-budget step is rolled back and re-run with the breach as feedback, and the split batch goes on to review (RF6); a second breach waits at `diff-budget` and the review step has not run; approve continues to review (gate ledger names the approver); reject with feedback re-runs the step after a rollback; a step without a change or without trees is never checked.
- [ ] Step 2: implement; pass; commit `feat(core): over-budget Developer steps are split before review (M2-24)`.

### Task 9: CLI wiring (M2-24)

**Files:** Modify `cli/src/agentic_cli/wiring.py`, `runtime.py`. Test: `cli/tests/test_review_batch_wiring.py`.

- [ ] Step 1: failing test: `Runtime.engine(...)` builds an engine whose `batch_budget_for(run_id)` is a `ReviewBatchBudget` with the org's `review_batch` values.
- [ ] Step 2: implement; pass; commit `feat(cli): wire the review batch budget into runs (M2-24)`.

### Task 10: Definition of done

- [ ] `uv run python scripts/check.py --all` green on Windows and in the Linux container; grep touched files for `fmt: skip`/`fmt: off`.
