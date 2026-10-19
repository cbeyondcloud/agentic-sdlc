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
| DF-04 | SP1 review | Shell allow/deny coverage (`_covers`) compares tokens loosely | A deny entry may fail to drop an allow prefix it covers | M1-31 | Open |
| DF-05 | SP1 review | Handoff compatibility ignores `when` (skipped steps) and the first loop iteration | A pipeline passes preflight but a step's input may not exist at run time | M1-25 | Open |
| DF-06 | SP1 review | A loop with an empty `steps` list is accepted | A loop that does nothing runs until `max_iterations` | M1-24 | Open |
| DF-07 | SP1 review | Agent names are not restricted to a slug pattern | Odd names in paths, headers and ledgers | M1-81 | Open |
| DF-08 | SP1 review | Loaded definitions and results are mutable objects | A caller can change a definition after it was checked | M1-81 | Open |
| DF-09 | SP2 review | `write_artifact`: containment, atomic write and `record_artifact` | A crash mid-write leaves a partial artifact; a path could escape the run folder | M1-21 | Open |
| DF-10 | SP2 review | Path globs on macOS's case-insensitive file system | `Infra/**` vs `infra/**` deny rule mismatch on macOS | M1-31 | Open |
| DF-11 | SP2 review | Ledger identities (`_IDENTITIES`) are keyed by run id only | Two ledgers for one run id in one process could share identity | M1-21 | Open |
| DF-12 | SP2 review | A ledger whose last line is JSON but not a record (e.g. `12`) is neither torn nor recoverable | A rare corrupt tail blocks a run with an unclear error | M1-21 | Open |
| DF-13 | SP2 review | `budget_exempt` in the diff budget is not floored by org policy | A repo could exempt paths from the diff-size budget | M2-24 | Open |
| DF-14 | SP2 rulings | Zyroflo tokenizer gaps: control keywords, leading redirects, quoted paths, `deny_git` gaps | Covered meanwhile by adapter variants in `sdlc_commons.guards`; the fix belongs upstream | M2-58 | Open |
| DF-15 | SP2 rulings | A deny rule naming a wrapper (`sudo`, `env`, `nohup`) is refused by word, so `echo sudo` is over-refused | Harmless commands refused | M2-58 | Open |
| DF-16 | SP2 rulings | Allow-list (shell prefixes) enforcement is not in the guard yet | Until M1-31, an unlisted, non-denied command is not refused | M1-31 | Open |
| DF-17 | SP2 rulings | Indirection (`\| bash`, `$VAR`, `python -c`, docker/ssh/wsl/npx) is not caught by deny lists | Deny lists must not be mistaken for enforcement; the allow-list and sandbox are the controls | M1-31, M1-32, M1-33 | Open |
| DF-18 | SP3 rulings | Budget ledger snapshot/restore across pause and restart | Budgets reset after a crash or resume | M1-21 | Open |
| DF-19 | SP3 review | Cost capture for the OpenAI Responses API | Codex/Responses calls priced as unavailable | M2-40 | Open |
| DF-20 | SP3 review | No tests for streamed tool calls or `n > 1` | A pricing bug on those paths would go unseen | M1-81 | Open |
| DF-21 | SP3 review | Money maths uses the default Decimal context | Rounding of large or many-digit amounts is not pinned | M1-81 | Open |
| DF-22 | SP4 review M3 | A model stream opened and never closed leaves its reservation in flight; `slice.close()` then refuses | A step cannot close its budget slice | M1-21 | Open |
| DF-23 | SP4 review M8 | Tool error text shows absolute host paths; `read_file`/`search`/`run_shell` output is not secret-scrubbed | Host paths and secrets in files reach the model | M1-41, M1-42 | Open |
| DF-24 | SP4 review M13 | Handoff wrappers are not escaped: a handoff containing `</handoff>` breaks out | Prompt injection from one agent's output into the next agent's instructions | M1-42 | Open |
| DF-25 | SP4 finding | Pydantic AI silently drops `temperature` for reasoning models (e.g. gpt-5), with only a warning | Policy says temperature X; the model never gets it | M1-27 | Open |
| DF-26 | SP4 finding | `Limits.budget_usd` is a float; the ledger needs Decimal: convert through `str` when opening run/step slices (as `children._usd` does) | Float rounding in budgets | M1-21 | Open |
| DF-27 | SP4 review fixes | `Sandbox.run` is async: the OS sandboxes must kill their command when the call is cancelled | A stopped run leaves a sandboxed command running | M1-32, M1-33 | Open |
| DF-28 | SP4 review fixes | The runner factory receives the child's own `ChildSpawner`; the engine must add `spawner.tools()` to each runner's tools | Children cannot spawn their own children | M1-21 | Open |
| DF-29 | SP4 (M1-18) | `oscillation()` exists but is not wired into loops | Review/developer loops that repeat do not end early | M1-24 | Open |
| DF-30 | SP5 review M2 | An echoed, unfilled template is accepted as a valid answer | Placeholder text reaches a gate (a person sees it) | M1-81 | Open |
| DF-31 | SP5 review M3 | Extension field names that clash with Pydantic internals (`model_dump`, `model_config`, `json`) give a raw error or warning | An admin gets a stack trace instead of a clear message | M1-81 | Open |
| DF-32 | SP5 review M4 | A section heading may contain a newline (never satisfiable); guidance containing `-->` breaks the rendered comment | A bad extension makes every answer of a kind invalid | M1-81 | Open |
| DF-33 | SP5 review M5 | A required string field accepts `""`; `extend_catalog` reports only the first bad file | Empty required values pass; slow fix-one-error-at-a-time | M1-81 | Open |
| DF-34 | SP5 review M6 | Schema digests depend on Pydantic's JSON-schema output, class names and the order of `values` | A Pydantic upgrade changes every digest; a run pinned to a digest cannot resume | M1-21 | Open |
| DF-35 | SP5 review M7 | Error line numbers count from the cleaned text; a four-backtick outer fence is not stripped | Line numbers off by one after a fence | M1-81 | Open |
| DF-36 | SP5 review M8 | A refused answer followed by an unrelated `UnexpectedModelBehavior` is reported as `invalid` with the old text | A misleading status at the gate | M1-81 | Open |
| DF-37 | SP5 review M9 | With `max_turns=1`, an invalid first answer ends `stopped: turns` with no `errors` | The gate cannot say why the output was refused | M1-81 | Open |
| DF-38 | SP5 review M10 | `parse_handoff` defaults to the built-in catalog | The engine forgets the run's catalog; extension fields fail as "extra" | M1-22 | Open |
| DF-39 | SP5 review M10 | A CRLF human edit at a gate keeps `\r` in `Handoff.body` | Handoff digests differ by OS (master Review Focus 1) | M1-22 | Open |
| DF-40 | SP5 review M10 | U+0085 in a field value is not round-tripped by the YAML writer | The final document differs from the answer (very rare) | M1-81 | Open |
| DF-41 | SP5 review M10 | An agent can mark its own finding `accepted`, which removes it from `open_blocking` | A reviewer agent can exit its own loop with a critical finding open | M1-22 + spec | Needs decision |
| DF-42 | SP5 review I2 | Gates must re-validate an edited handoff with `parse_handoff(text, run_catalog, block_on=run_block_on)` | A person's edit could change the review loop's exit number | M1-22 | Open |
| DF-43 | SP5 plan | The engine builds `HandoffContract` from `DefinitionSet.handoffs` and the regime's `block_on`, and records `catalog.digest` in `run.json` | Steps validated against the wrong schema, or a mid-run policy change | M1-21 | Open |
| DF-44 | SP5 (M1-79) | Preflight calls `kind_violations` for every resolved agent (kinds inherited from an agent type) | An agent type naming an unknown kind fails mid-run instead of at preflight | M1-25 | Open |
