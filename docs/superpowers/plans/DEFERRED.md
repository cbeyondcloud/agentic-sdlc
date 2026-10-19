# Deferred items register

Everything found but not fixed: review minors that were deferred, and notes a later tracker row must act on. Nothing deferred lives only in the executor ledger, which is git-ignored and local to one machine.

**Rules**
1. When a review's minors are deferred, or a task finds something a later row must handle, the item is added here **in the same commit** as the work that found it.
2. Each item names a **target** tracker row. That row's Notes column lists its open items (`Deferred: DF-nn`).
3. The plan for a sub-plan pulls in every open item that targets its rows. The sub-plan's review checks they are closed.
4. An item is closed by a commit with a test, or by a recorded decision. Its status then becomes `Done (<commit>)`, `Accepted (<reason>)` or `Dropped` (dropped items also go in `docs/de-scope-register.md`).
5. `Needs decision` items are raised with the owner, never decided silently.
6. Items with no natural home go to **M1-81** (the hardening pass before the M1 demo).

**Status values:** `Open` · `Needs decision` · `Done (<commit>)` · `Accepted (<reason>)` · `Dropped`

| ID | Found in | Item | Effect if never done | Target | Status |
|---|---|---|---|---|---|
| DF-01 | SP1 review | An agent with an explicit `model` can still carry its tier's `effort`/`temperature` | A setting meant for another model is sent, or silently ignored | M1-27 | Open |
| DF-02 | SP1 review | `policy_version` content hash: CRLF vs LF bytes and the exact set of files hashed | The same policy gets a different version id on Windows and Linux; audit records disagree | M1-81 | Open |
| DF-03 | SP1 review | Discovery: a file's stem need not equal its `name`; names are not checked unique within a layer | Two files define one agent; which wins is not obvious | M1-81 | Open |
| DF-04 | SP1 review | Shell allow/deny coverage (`_covers`) compares tokens loosely | A deny entry may fail to drop an allow prefix it covers | M1-31 | Done (SP8 Task 1) |
| DF-05 | SP1 review | Handoff compatibility ignores `when` (skipped steps) and the first loop iteration | A pipeline passes preflight but a step's input may not exist at run time | M1-25 | Done (9e041a4: conditional producers warn) · loop first iteration Accepted (intended feedback pattern) |
| DF-06 | SP1 review | A loop with an empty `steps` list is accepted | A loop that does nothing runs until `max_iterations` | M1-24 | Done (9e041a4) |
| DF-07 | SP1 review | Agent names are not restricted to a slug pattern | Odd names in paths, headers and ledgers | M1-81 | Open |
| DF-08 | SP1 review | Loaded definitions and results are mutable objects | A caller can change a definition after it was checked | M1-81 | Open |
| DF-09 | SP2 review | `write_artifact`: containment, atomic write and `record_artifact` | A crash mid-write leaves a partial artifact; a path could escape the run folder | M1-21 | Done (4968cdf) |
| DF-10 | SP2 review | Path globs on macOS's case-insensitive file system | `Infra/**` vs `infra/**` deny rule mismatch on macOS | M1-31 | Done (SP8 Task 2) |
| DF-11 | SP2 review | Ledger identities (`_IDENTITIES`) are keyed by run id only | Two ledgers for one run id in one process could share identity | M1-21 | Accepted (Zyroflo's default-ext provider gets only the run id; one process serves one repository) |
| DF-12 | SP2 review | A ledger whose last line is JSON but not a record (e.g. `12`) is neither torn nor recoverable | A rare corrupt tail blocks a run with an unclear error | M1-21 | Done (4968cdf) |
| DF-13 | SP2 review | `budget_exempt` in the diff budget is not floored by org policy | A repo could exempt paths from the diff-size budget | M2-24 | Open |
| DF-14 | SP2 rulings | Zyroflo tokenizer gaps: control keywords, leading redirects, quoted paths, `deny_git` gaps | Covered meanwhile by adapter variants in `sdlc_commons.guards`; the fix belongs upstream | M2-58 | Open |
| DF-15 | SP2 rulings | A deny rule naming a wrapper (`sudo`, `env`, `nohup`) is refused by word, so `echo sudo` is over-refused | Harmless commands refused | M2-58 | Open |
| DF-16 | SP2 rulings | Allow-list (shell prefixes) enforcement is not in the guard yet | Until M1-31, an unlisted, non-denied command is not refused | M1-31 | Done (SP8 Task 2) |
| DF-17 | SP2 rulings | Indirection (`\| bash`, `$VAR`, `python -c`, docker/ssh/wsl/npx) is not caught by deny lists | Deny lists must not be mistaken for enforcement; the allow-list and sandbox are the controls | M1-31, M1-32, M1-33 | Open |
| DF-18 | SP3 rulings | Budget ledger snapshot/restore across pause and restart | Budgets reset after a crash or resume | M1-21 | Done (e9dd3c4) |
| DF-19 | SP3 review | Cost capture for the OpenAI Responses API | Codex/Responses calls priced as unavailable | M2-40 | Open |
| DF-20 | SP3 review | No tests for streamed tool calls or `n > 1` | A pricing bug on those paths would go unseen | M1-81 | Open |
| DF-21 | SP3 review | Money maths uses the default Decimal context | Rounding of large or many-digit amounts is not pinned | M1-81 | Open |
| DF-22 | SP4 review M3 | A model stream opened and never closed leaves its reservation in flight; `slice.close()` then refuses | A step cannot close its budget slice | M1-21 | Done (c87109d: a slice that cannot close is recorded, the run continues) |
| DF-23 | SP4 review M8 | Tool error text shows absolute host paths; `read_file`/`search`/`run_shell` output is not secret-scrubbed | Host paths and secrets in files reach the model | M1-41, M1-42 | Open |
| DF-24 | SP4 review M13 | Handoff wrappers are not escaped: a handoff containing `</handoff>` breaks out | Prompt injection from one agent's output into the next agent's instructions | M1-42 | Open |
| DF-25 | SP4 finding | Pydantic AI silently drops `temperature` for reasoning models (e.g. gpt-5), with only a warning | Policy says temperature X; the model never gets it | M1-27 | Open |
| DF-26 | SP4 finding | `Limits.budget_usd` is a float; the ledger needs Decimal: convert through `str` when opening run/step slices (as `children._usd` does) | Float rounding in budgets | M1-21 | Done (c87109d) |
| DF-27 | SP4 review fixes | `Sandbox.run` is async: the OS sandboxes must kill their command when the call is cancelled | A stopped run leaves a sandboxed command running | M1-32, M1-33 | Done (SP8 Task 4) |
| DF-28 | SP4 review fixes | The runner factory receives the child's own `ChildSpawner`; the engine must add `spawner.tools()` to each runner's tools | Children cannot spawn their own children | M1-21 | Done (a2b5833) |
| DF-29 | SP4 (M1-18) | `oscillation()` exists but is not wired into loops | Review/developer loops that repeat do not end early | M1-24 | Done (c87109d) |
| DF-30 | SP5 review M2 | An echoed, unfilled template is accepted as a valid answer | Placeholder text reaches a gate (a person sees it) | M1-81 | Open |
| DF-31 | SP5 review M3 | Extension field names that clash with Pydantic internals (`model_dump`, `model_config`, `json`) give a raw error or warning | An admin gets a stack trace instead of a clear message | M1-81 | Open |
| DF-32 | SP5 review M4 | A section heading may contain a newline (never satisfiable); guidance containing `-->` breaks the rendered comment | A bad extension makes every answer of a kind invalid | M1-81 | Open |
| DF-33 | SP5 review M5 | A required string field accepts `""`; `extend_catalog` reports only the first bad file | Empty required values pass; slow fix-one-error-at-a-time | M1-81 | Open |
| DF-34 | SP5 review M6 | Schema digests depend on Pydantic's JSON-schema output, class names and the order of `values` | A Pydantic upgrade changes every digest; a run pinned to a digest cannot resume | M1-21 | Open (c87109d: a run refuses a changed catalog with a clear message; the digest still depends on Pydantic's output) |
| DF-35 | SP5 review M7 | Error line numbers count from the cleaned text; a four-backtick outer fence is not stripped | Line numbers off by one after a fence | M1-81 | Open |
| DF-36 | SP5 review M8 | A refused answer followed by an unrelated `UnexpectedModelBehavior` is reported as `invalid` with the old text | A misleading status at the gate | M1-81 | Open |
| DF-37 | SP5 review M9 | With `max_turns=1`, an invalid first answer ends `stopped: turns` with no `errors` | The gate cannot say why the output was refused | M1-81 | Open |
| DF-38 | SP5 review M10 | `parse_handoff` defaults to the built-in catalog | The engine forgets the run's catalog; extension fields fail as "extra" | M1-22 | Done (81d8b31) |
| DF-39 | SP5 review M10 | A CRLF human edit at a gate keeps `\r` in `Handoff.body` | Handoff digests differ by OS (master Review Focus 1) | M1-22 | Done (81d8b31) |
| DF-40 | SP5 review M10 | U+0085 in a field value is not round-tripped by the YAML writer | The final document differs from the answer (very rare) | M1-81 | Open |
| DF-41 | SP5 review M10 | An agent can mark its own finding `accepted`, which removes it from `open_blocking` | A reviewer agent can exit its own loop with a critical finding open | M1-22 + spec | Done (81d8b31; owner agreed 2026-10-20) |
| DF-42 | SP5 review I2 | Gates must re-validate an edited handoff with `parse_handoff(text, run_catalog, block_on=run_block_on)` | A person's edit could change the review loop's exit number | M1-22 | Done (81d8b31) |
| DF-43 | SP5 plan | The engine builds `HandoffContract` from `DefinitionSet.handoffs` and the regime's `block_on`, and records `catalog.digest` in `run.json` | Steps validated against the wrong schema, or a mid-run policy change | M1-21 | Done (c87109d) |
| DF-44 | SP5 (M1-79) | Preflight calls `kind_violations` for every resolved agent (kinds inherited from an agent type) | An agent type naming an unknown kind fails mid-run instead of at preflight | M1-25 | Done (9e041a4) |
| DF-45 | SP6 plan | Regulated regime: approvals are not signed (DSSE) and no evidence is required yet; Zyroflo's `append_entry` already takes a signer | A regulated run's approvals are four-eyes but unsigned | E2-03 | Open |
| DF-46 | SP6 Task 7 | The step-runner factory's `tools_for` returns no tool context yet: the worktree per step, the guard and the sandbox are wired in M1-30 | Agents run without file or shell tools until then | M1-30 | Open |
| DF-47 | SP6 | The engine is not yet wired into the CLI: opening/resuming the run ledger, the run store path, the authorizer, `self_approval_refusal`, `regime_block_on` | No `agentic run` / `approve` until then | M1-54 | Open |
| DF-48 | SP6 Task 3 | `commons` is pinned to the Zyroflo Z6 PR commit (560ad81) until the owner merges; then the pin moves to the merge commit | A pin to an unmerged commit | M2-59 | Done (pin moved to the merge commit ce4ab12 of Zyroflo PR #80) |
| DF-49 | SP6 Task 4 | `resume(extra_budget_usd)` raises the run limit only; a step-level budget pause re-runs the step with a fresh step slice | A person cannot raise one step's budget specifically | M1-54 | Open |
| DF-50 | SP6 review | A refused approve keeps an edit already applied (the `.edited-1` file stays pinned) and the refusal does not say so | A person is not told their edit was kept | M1-81 | Open |
| DF-51 | SP6 review | A crash between `gates.record` and the save leaves a stale pending gate; the next approve writes a second entry (the chain still verifies) | A duplicate approval entry | M1-81 | Open |
| DF-52 | SP6 review | Child slices restored after a crash keep holding budget while the step re-runs | Less step budget for the re-run than intended | M1-81 | Open |
| DF-53 | SP6 review | A budget-stopped step's StepRecord shows cost 0 | History under-reports that step's spend (the ledger is right) | M1-81 | Open |
| DF-54 | SP6 review | Engine errors reach callers in six families (EngineError, GateRefused, RunStateError, ResumeError, LedgerError, ExpressionError); `recorder_for` must open or resume the ledger; no run list or status accessor; `start` does not check `inputs` against the pipeline's `inputs:` | The CLI must map each family to messages and exit codes | M1-54 | Open |
| DF-55 | SP6 review | Only the pipeline and the handoff catalog are pinned for a run; `policy_version` is stored but not compared, and agent definitions can change mid-run | A policy or agent change takes effect inside a running run | M2-21 | Open |
| DF-56 | SP6 review | After a rejection to an earlier step, `unpinned` keeps the superseded handoffs, so the next gate lists them too | A gate shows stale files beside the new ones | M1-81 | Open |
| DF-57 | SP6 review | Spawned children of one step share `children/<agent name>` as their state folder | Two children pausing for approval at once would share a state file name | M1-81 | Open |
| DF-58 | SP6 review | Small items: approver recorded unstripped; a hard kill leaves `run.json.<pid>.tmp`; no retry on a transient Windows PermissionError from `os.replace`; preflight warns about a conditional producer even when the consumer has the same `when`; a `when` reading a conditionally produced kind is not flagged in preflight | Minor noise and leftovers | M1-81 | Open |
| DF-59 | SP6 review | No "skip this step" decision for a condition pause; the only exits are a fix that needs a new run, or stopping | A person cannot route around one broken condition | M1-55 | Open |
| DF-60 | S6 | Network enforcement of `srt` under WSL2 is untested (an open public report says it is unreliable for the Claude Code sandbox on WSL2); this laptop's WSL2 Ubuntu 20.04 lacks `socat`/`ripgrep` and has bubblewrap 0.4.0. **To close (owner action):** inside WSL2 install the prerequisites, then run `python -m agentic.sandbox.probe` once and record its table | If it leaks, Windows needs the container alternative and S12 becomes blocking | M1-33 | Open |
| DF-61 | S6 | `srt` on macOS is untested here (no Mac); **to close (owner action):** on a Mac with Node ≥ 20.11, `ripgrep` and `srt` 0.0.78, run `python -m agentic.sandbox.probe` once and record its table | A Mac-only gap would surface at the pilot | M1-32 | Open |
