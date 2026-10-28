# M1 close-out (runtime area) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Leave no Open DEFERRED.md item that targets an M1 row and whose code lies in the runtime area (`core/src/agentic/**` except `eval`, `commons/`, `scripts/` and their tests), except the ones that need live models, the owner or a Mac. Each item is re-graded by what it does to a Windows or macOS developer in a 1000-person org: fixed test-first when it can cause wrong behaviour, a security or safety gap, data loss, a confusing failure or a cross-platform break; accepted with a one-line reason when it is cosmetic or theoretical; retargeted to the M2/M3 row that owns it when it is org-mode work.

**Architecture:** No new modules, no new dependencies. Every fix is local to the file with the defect. Two small seams are added inside existing modules: `SecretScanner.for_step(...)` (the scan event carries the run, step and agent) and `splice_body(...)` in `agentic.hosting.base` (a re-publish replaces only the generated part of a pull-request description). The GitHub adapter's `update_pull_request` reads the current body and splices; the protocol is unchanged.

**Tech Stack:** Python 3.12, Pydantic 2, `regex`, httpx2, sqlite3, pytest. All already locked.

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md` §9.2 (sandbox), §11 (skills), §15 (secret scan), §16 (hosting). Tracker rows M1-21, M1-31..33, M1-41, M1-42, M1-44, M1-53, M1-54, M1-58, M1-60, M1-81 and the DEFERRED.md items below.

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| Linear-time scanning of npm tokens | the `regex` module's `finditer(text, pos, endpos)` over windows around the literal `_authToken` | rewriting detect-secrets' pattern would fork it; a different scanner (gitleaks) is a native binary, not allowed on laptops |
| Scanning off the event loop | `asyncio.to_thread` (stdlib), which copies context variables, so step logs still match | - |
| Filtering the push environment | the sandbox's own `SECRET_NAME` rule (`agentic.sandbox.process`) | full `safe_env` would drop `SSH_AUTH_SOCK` and break SSH pushes |
| PR-description markers | HTML comments (`<!-- agentic:run -->`), which GitHub, GitLab and Azure DevOps all hide | - |

Nothing needs a new package; Zyroflo is not touched (core never imports it).

## Items in scope and their grading

| DF | Grade | Plan |
|---|---|---|
| DF-17 | design: deny lists are a convenience; the allow list refuses such text whole and srt is the boundary (spec §9.2) | Accept |
| DF-34 | confusing: after an upgrade a paused run is refused with "the handoff templates changed" | Fix the message (Task 2); accept the Pydantic dependence (pinned in uv.lock) |
| DF-49 | a step-budget resume already re-runs the step with a fresh slice; `--extra-budget` is refused there with a hint | Accept |
| DF-62 | needs the commit-command override in StepCheckpoints (same as DF-287) | Retarget M2-29 |
| DF-74, DF-81, DF-76, DF-80, DF-322 | claude-code in the CLI runtime (DF-212/DF-343), needs live Claude Code under srt | Retarget M2-41 |
| DF-143, DF-189 | security: a catalog under review sets its own licence allow-list | Fix (Task 7) |
| DF-144 | done in e133355 (preflight cap, skills.truncated, 6+ skill test); remainder is DF-214 | Done |
| DF-146 | waste and duplicate ledger rows on resume | Fix (Task 7) |
| DF-147 | audit: scan events never reach the run folder in structured form | Fix: structured fields in the step log (Task 6); hash-chained copy retargeted to M2-20 |
| DF-153, DF-155 | confusing failure: a 1 MB minified line is blocked as unscannable; the loop stalls | Fix (Task 6) |
| DF-154 | audit: an event cannot be tied to a run, step or agent | Fix (Task 6) |
| DF-156 | wrong behaviour: an edit copied from read_file output fails with "found 0" | Fix (Task 5) |
| DF-180 | wrong behaviour: odd step ids reach paths and trailers | Fix (Task 2) |
| DF-214 | a no-tool agent cannot read bundled checklists | Fix (Task 7) |
| DF-225 | confusing failure: a placeholder model fails at the gateway on the first call | Fix (Task 3) |
| DF-230 | wrong behaviour: a pass with miscounted or duplicated failures validates | Fix (Task 7) |
| DF-232 | safety: a link at the skill folder or above is followed | Fix (Task 7) |
| DF-235 | test gap: rubric deductions and the 8.0 cap can drift | Fix (Task 7) |
| DF-245 | a free-form category would make matching stricter without a shared vocabulary | Accept |
| DF-271, DF-313 | already fixed (58686ea, db5f2a2) | Done |
| DF-289 | `agentic pr checks --watch` covers it; a step waiting on CI would hold the run open | Accept |
| DF-293, DF-312, DF-318 | confusing PR body: "Status: running", `$1E-7` | Fix (Task 8) |
| DF-294 | security: pre-push hooks read the framework's tokens | Fix (Task 8) |
| DF-296 | data loss: a re-publish overwrites a person's PR description | Fix (Task 8) |
| DF-297 | security: part of a password stays in git error text | Fix (Task 8) |
| DF-299 | confusing: a 301 gives no remedy (core part); SSH-alias hint is CLI | Fix core part (Task 8) |
| DF-300 | provider-agnostic rule: base.py prose names vendors | Fix (Task 8) |
| DF-307 | SHA-256 repos as policy repos | Fix or prove (Task 8) |
| DF-310 | already resolved by fc922a6 (starlette 1.7.0, pydantic-settings 2.15.0 locked) | Done |
| DF-319 | wrong guidance for a mis-indented key | Fix (Task 2) |
| DF-323, DF-324, DF-325 | advice that fails; Windows device names; guard coverage gap | Fix (Task 2) |
| DF-326 | release-note item, no code | Accept |
| DF-327 | a skipped record can carry stale marks | Fix (Task 3) |
| DF-330 | a fixed-date exception freezes certifi once the window passes | Fix: check.py warns when an exception is older than the window (Task 1) |
| DF-331 | one bad name loses every file of a diff | Fix (Task 5) |
| DF-332 | `check.py --offline && git push` looks like a pass | Fix (Task 1) |
| DF-333 | slow env filtering on pathological values | Fix (Task 4) |
| DF-334 | confusing failure: proxy or agent socket withheld silently | Fix: name withheld variables in the log (Task 4); policy pass-through list retargeted to M2-42 |
| DF-335 | untested ruling; smoke check gives the wrong fix | Fix (Task 4) |
| DF-336 | cross-platform: quoted Windows PATH entries dropped; empty PATH | Fix (Task 4) |
| DF-337 | data loss of a busy index; PermissionError on Windows | Fix (Task 5) |
| DF-339 | security: read_file reads `.git/config` (remote credentials) | Fix (Task 5) |
| DF-61, DF-104 | need a Mac or other container engines (owner) | Skipped, stay Open |

Items in other areas (CLI, eval, library, TokenLean, live) are left to their tracks.

## Global Constraints

- Windows and macOS both: paths compared case-insensitively where the OS is, `.git` aliases refused on every OS, PATH entries judged without their quotes.
- No vendor names in engine, policy, hosting protocol prose or CLI flags.
- Every fix has a test seen failing first; check pytest exit codes directly.
- No `# fmt: skip`; ruff line length 100.

## Review Focus

1. The push environment keeps the person's own credentials (SSH agent, credential helpers) and drops only the framework's `AGENTIC_*` secrets.
2. `splice_body` never loses text outside the markers, and a body without markers is replaced whole (first publish, or an old PR).
3. The scan windows find every npm token the full scan finds when the token lies within the window.
4. read_file refuses every `.git` alias for reads, while other framework-protected files (AGENTS.md, workflows) stay readable.

## Tasks

### Task 1: scripts (DF-332, DF-330)
**Files:** `scripts/check.py`, `scripts/tests/test_check_engine.py`.
- [ ] RED: an offline run's `main()` exits 3 (INCOMPLETE), not 0; `stale_exceptions(pyproject, today)` lists a package whose exception date is older than the window.
- [ ] GREEN: `run_native` returns "passed" / "incomplete" / "failed"; `main` maps incomplete to 3; the native run prints a warning per stale exception.

### Task 2: definitions and YAML (DF-180, DF-319, DF-323, DF-324, DF-325, DF-34)
**Files:** `core/src/agentic/definitions/{pipeline,agent,discovery}.py`, `core/src/agentic/yamlio.py`, `core/src/agentic/engine/engine.py`, tests in `core/tests/definitions/`, `core/tests/test_yamlio.py`, `core/tests/backends/test_native.py`.
- [ ] RED/GREEN: step, gate and loop ids follow `[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}`; Windows device names refused as ids and agent names; agent-name error is plain words; discovery offers `set name: <stem>` only for a valid stem; the YAML hint names both causes; the vendor guard uses `rglob`; the catalog-changed refusal names an upgrade as a cause.

### Task 3: engine (DF-327, DF-225)
**Files:** `core/src/agentic/engine/{engine,preflight}.py`, tests in `core/tests/engine/`.
- [ ] RED/GREEN: a skipped record has no base/commit; preflight errors on a tier model containing `change-me`.

### Task 4: sandbox environment (DF-333, DF-334, DF-336, DF-335)
**Files:** `core/src/agentic/sandbox/{process,readiness}.py`, `core/tests/sandbox/`.
- [ ] RED/GREEN: credential regex linear on 20 KB adversarial values; quoted absolute PATH entries kept; an all-relative PATH becomes a safe system default; withheld names logged (never values); smoke-check fix chosen by cause; live srt test that an existing denied file cannot be overwritten.

### Task 5: file tools and code intelligence (DF-339, DF-156, DF-331, DF-337)
**Files:** `core/src/agentic/agents/tools/{files,protect}.py`, `core/src/agentic/codeintel/{crg,index,tools}.py`, tests.
- [ ] RED/GREEN: read_file and list_dir refuse `.git` and its aliases; read_file output keeps the tree's absolute path verbatim; `diff_files` survives `\€`; a locked index is not discarded.

### Task 6: secret scanning (DF-153, DF-155, DF-154, DF-147)
**Files:** `core/src/agentic/security/secret_scan.py`, `core/src/agentic/transport/scan.py`, `core/src/agentic/agents/tools/protect.py`, `core/src/agentic/backends/native.py`, tests.
- [ ] RED/GREEN: a 1 MB URL-heavy line scans in well under the deadline; an npm token is still found; the async transport scans in a worker thread; events carry run, step and agent when bound; `audit_log` writes the event as structured `fields`.

### Task 7: skills, handoffs and the library drift test (DF-143/189, DF-146, DF-214, DF-232, DF-230, DF-235)
**Files:** `core/src/agentic/skills/{admission,catalog,loader}.py`, `core/src/agentic/engine/factory.py`, `core/src/agentic/handoffs/kinds.py`, `commons/tests/test_library_agent_sources.py`, tests.
- [ ] RED/GREEN: catalog licences and limits only narrow the caller's floor; each skill folder hashed once per prompt and no record on resume; no-tool agents get bundled `.md` files inlined (bounded); links at or above the skill folder refused with the init hint; test-report pass counts must match; drift test reads deductions and cap.

### Task 8: hosting and ledger (DF-294, DF-297, DF-296, DF-293/312/318, DF-299, DF-300, DF-307)
**Files:** `core/src/agentic/hosting/{base,git,github}.py`, `core/src/agentic/telemetry/trailers.py`, `commons/tests/test_ledger.py`, tests.
- [ ] RED/GREEN: a pre-push hook sees no `AGENTIC_*` secret; credentials with `@` scrubbed whole; re-publish keeps text outside markers; status and cost lines read clearly; 301 names the remedy; base.py prose neutral; a `git:<64 hex>` policy version is recorded.

### Task 9: check.py --all green, structured output.
