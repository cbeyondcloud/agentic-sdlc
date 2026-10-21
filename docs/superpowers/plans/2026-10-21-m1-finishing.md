# M1 finishing: `agentic logs --follow`, `init --standalone` from the template, call flow

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close three M1 rows.

- **M1-47.** `agentic logs <run> [--step ID] [--level L] [--follow] [--json]` reads the per-step logs. The output is redacted a second time on read. `--follow` streams new records until the run stops running. The output never crashes, and is never garbled, on a Windows console or pipe, or on a macOS terminal.
- **M1-62.** `agentic init --standalone` with no `--policy` builds a standalone policy repo from `templates/standalone/` and the framework library, and installs it once into `~/.agentic/policy/standalone/`. Later commands find it there. The open deferred items for this row are closed: DF-217, DF-233, DF-234 and DF-237, plus DF-224, which targets M1-53 but is this work.
- **M1-73.** `docs/architecture/call-flow.md` matches the code as it is now. Every box is marked built (file · function) or planned (tracker row). The Mermaid diagrams are validated with a pinned mermaid-cli, inside a Linux container only.

**Architecture:**
- **`agentic.telemetry.logs`** (core):
  - `LogTail(run_dir, step, min_level)`. `poll()` returns only the records added since the last poll, oldest first. It reads only whole lines, keeps a byte offset per file, and starts again from 0 for a file that got shorter.
  - `read_logs` becomes `LogTail(...).poll(final=True)`. On the final read, a last line with no newline is read too, so a crashed writer's partial line is still shown as "not JSON".
  - `redact_record(record, redact)` redacts the message, the error and the field values once more.
- **`agentic.engine.state.RunStore.save`** retries `os.replace` for a short while on `PermissionError`. On Windows, another process that is reading `run.json` (`logs --follow`, `status`) makes the rename fail for a moment.
- **CLI `logs`** (`commands/runs.py`):
  - `--follow` loops. It reads the run's status first, then polls the tail and prints, and stops once the status read before the poll was not `running`, so nothing written before the end is missed. Then it sleeps `FOLLOW_INTERVAL_S` (0.5 s). Ctrl-C exits 130, by convention.
  - With `--json --follow` the output is JSON Lines (one compact record per line), through a new `Output.stream`. Without `--follow` it stays one JSON document.
  - The redactor is `Runtime.redact`, plus the gateway key when a gateway is configured (best effort: `logs` never needs a gateway).
- **Console safety** (`agentic_cli.output.safe_streams`, called by `main`): a stdout or stderr whose encoding is not UTF-8 (a Windows pipe or a legacy code page) is reconfigured with `errors="replace"`, so a log line with `→` or `€` cannot crash the command. JSON output is ASCII-escaped on such a stream, so it stays exact.
- **`agentic_cli.standalone`** (new):
  - `framework_root()` finds the framework's `templates/` and `library/`. It uses `AGENTIC_FRAMEWORK_ROOT` when set, else the source checkout. A wheel does not ship them yet: DF-138/DF-188 and M3-21 own packaging.
  - `assemble(dest)` writes a policy repo:
    - `policy/` from the template, and `README.md`;
    - `agents/` and `pipelines/` from `library/`;
    - `skills/` from `library/skills`, with the template's `skills/preload.yaml` rule written into `catalog.yaml` (DF-224);
    - `templates/agents/<type>.md` for each library agent type (DF-217). The type's first library agent, by name, is the starter, except where a type has an agent of the same name, which wins (`qa`, not `qa-baseline`).
- **`policy_source.resolve_policy`:** `--standalone` with no `--policy` and no `policy:` in the user config uses `~/.agentic/policy/standalone/`.
  - If it exists, it is loaded and kept as it is: it is the person's copy and they edit it.
  - Otherwise the repo is assembled in a scratch folder and loaded from there. `LoadedPolicy.commit()` moves it into place only after the repo guard passed, like the org cache. A refusal leaves `~/.agentic` untouched.
- **`Workspace.policy_repo`** gains a last fallback, `~/.agentic/policy/standalone/`. The org cache is read at `~/.agentic/policy/<org>/repo` (where init writes it), with the old `<org>/` kept as a fallback.
- **Init report (standalone):**
  - `policy: models` is `missing` while any tier still names a `change-me` placeholder; the fix names the file to edit.
  - `policy: runs` says which policy later commands will use, or how to point them at this one.
  - On native Windows the sandbox items carry the exact WSL2 install steps (existing `agentic.sandbox.readiness` guidance), because the template keeps `sandbox.required_for: [team, regulated]` (DF-72, owner decision 54).

**Tech Stack:** Python 3.12, stdlib only (no new dependency). Diagram check: `minlag/mermaid-cli:11.4.2` (MIT, the mermaid-cli project's own image, released 2024-12) run with Docker inside its Linux container, never on the Windows host.

**Spec:** §8.1 and §24 ("Trying it without the org setup"), §16 observability (M1-47 structured logs), the M1-73 row text.

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| Follow a growing log file | stdlib polling (`open`, `seek`), 0.5 s | `watchdog` (Apache-2.0): a native extension on macOS, inotify/ReadDirectoryChangesW differences, and a new dependency for a 0.5 s poll over a handful of files |
| Copy the template | `shutil.copytree` | — |
| Diagram validation | `@mermaid-js/mermaid-cli` 11.4.2 Docker image (MIT) | a host install: it needs Node and Chromium, native binaries not allowed on the Windows host |
| Redaction | the existing `Runtime.redact` (`sdlc_commons.redaction` over Zyroflo's masking) | — |

## Global Constraints

- Windows and macOS:
  - log files are read in binary mode with byte offsets, so CRLF never shifts an offset (the writer writes LF);
  - opening a log file for reading never blocks the writer, because Python opens files with shared read/write;
  - `run.json`'s atomic replace survives a concurrent reader (Task 2).
- `init` still writes only under `~/.agentic` and `<repo>/.agentic/local`. The standalone policy repo is installed only after the guard passed, and only when `~/.agentic` is not a link.
- An installed standalone policy is never overwritten. Upgrading it to a newer template is deferred.
- No vendor or model names in the template (`test_no_vendor_names` keeps guarding it).
- Never `# fmt: skip` / `# fmt: off`. Check pytest's exit code directly. `uv run python scripts/check.py --all` must be green. TRACKER.md, DEFERRED.md and DECISIONS are left to the coordinator.

## Review Focus

1. **Follow never misses the last records.** The status is read before the poll, so records written before the run ended are always printed. Test: `test_follow_prints_records_written_while_running_then_stops`.
2. **A partial line is never parsed while following**, and a truncated file is read again from the start. Test: `test_tail_reads_whole_lines_only`, `test_tail_restarts_a_truncated_file`.
3. **A secret written into a log by an old version is masked on read.** Test: `test_logs_redact_again_on_read`.
4. **A non-UTF-8 console cannot crash `agentic logs`.** Test: `test_safe_streams_replace_unencodable_text`.
5. **`init --standalone` with no policy writes nothing when the guard refuses**, installs the repo once, keeps an edited copy, and the next `agentic run --dry-run` finds it. Tests in `cli/tests/test_init_standalone.py`.
6. **The catalog with the preload rule still loads and pins every skill.** The template's QA deny rules refuse `npm audit fix` and `pip-audit --fix`, and the new test runners are allowed. Tests in `core/tests/policy/test_standalone_policy.py`.

---

### Task 1: `LogTail` and redaction on read (M1-47)

**Files:** Modify `core/src/agentic/telemetry/logs.py`. Test: `core/tests/telemetry/test_logs_tail.py`.

**Interfaces:**
- `class LogTail: __init__(run_dir: Path, step: str | None = None, min_level: str | None = None)`, `poll(final: bool = False) -> list[dict]`.
- `read_logs(run_dir, step=None, min_level=None) -> list[dict]`: unchanged signature and behaviour.
- `redact_record(record: dict, redact: Callable[[str], str]) -> dict`.

- [ ] Tests first: poll returns the new records only, oldest first across files; a half-written line is held back until its newline arrives; a truncated file is read from 0 again; `final=True` returns the partial line as a "not JSON" error; the step filter and level filter work as in `read_logs`; `redact_record` masks the message, the error and nested field strings, and leaves numbers alone.
- [ ] Run them and see them fail (no `LogTail`). Implement. See them and the existing telemetry tests pass.

### Task 2: `run.json` replace survives a concurrent reader on Windows (M1-47)

**Files:** Modify `core/src/agentic/engine/state.py`. Test: `core/tests/engine/test_state_replace.py`.

- [ ] Test first: `os.replace` raises `PermissionError` twice, then succeeds, and the state is saved. A replace that keeps failing re-raises, and the temporary file is removed.
- [ ] See it fail. Implement a bounded retry (10 tries, 0.05 s apart, at most 0.5 s). See it pass.

### Task 3: `agentic logs --follow`, JSON Lines, safe console (M1-47)

**Files:** Modify `cli/src/agentic_cli/commands/runs.py`, `cli/src/agentic_cli/output.py`, `cli/src/agentic_cli/main.py`. Test: `cli/tests/run/test_logs_command.py`, `cli/tests/test_output_streams.py`.

**Interfaces:** `Output.stream(data, text: str)`, `output.safe_streams(*streams)`, `runs.FOLLOW_INTERVAL_S`, `runs._sleep` (a module-level function the tests replace).

- [ ] Tests first:
  - `--follow` on a finished run prints every record and exits 0;
  - on a running run it prints records written between polls, and stops after the run leaves `running`;
  - `--json --follow` gives one JSON object per line;
  - a planted secret in a log file is masked in human and JSON output;
  - an unknown run is an error with a hint;
  - `safe_streams` lets a cp1252 stream take `→`;
  - JSON on a non-UTF-8 stream is ASCII-escaped.
- [ ] See them fail. Implement. See them pass, together with the existing `test_logs_show_the_step_log`.

### Task 4: Template content (M1-62: DF-233, DF-234, DF-237)

**Files:** Modify `templates/standalone/policy/agent-types.yaml`, `templates/standalone/policy/tools.yaml`, `templates/standalone/README.md`. Test: `core/tests/policy/test_standalone_policy.py`.

- [ ] Tests first:
  - developer and qa allow `mvn test`, `./mvnw test`, `./gradlew test`, `npm run test`, `yarn test`, `pnpm test`, `make test`, `coverage run -m pytest` and `tox`, checked through `sdlc_commons.guards.Guard` in a commons-level test, because core may not import commons;
  - qa refuses `npm audit fix`, `pip-audit --fix` and `pip-audit -o out.json`, while `npm audit` and `pip-audit` stay allowed;
  - the README names "Restricted repos", WSL2 and `agentic init --standalone`.
- [ ] See them fail. Edit the template. See them pass.

### Task 5: Assemble the standalone policy repo (M1-62: DF-217, DF-224)

**Files:** Create `cli/src/agentic_cli/standalone.py`. Test: `cli/tests/test_standalone_assemble.py`.

**Interfaces:** `framework_root(env=os.environ) -> Path`, `assemble(dest: Path, root: Path | None = None) -> Path`, `STANDALONE_DIR = Path("policy") / "standalone"`.

- [ ] Tests first:
  - the assembled repo loads with `load_policy`;
  - `DefinitionSet.discover` finds the six library agents and both pipelines;
  - `load_catalog` gives `preload_for` equal to the template's rule, and every skill still verifies (`sync_skills` into a temp folder);
  - `templates/agents/qa.md` is the qa agent, and there is one template per library type;
  - a missing framework root is a `CliError` with a hint.
- [ ] See them fail. Implement. See them pass.

### Task 6: `init --standalone` uses it; later commands find it (M1-62)

**Files:** Modify `cli/src/agentic_cli/policy_source.py`, `cli/src/agentic_cli/dev_init.py`, `cli/src/agentic_cli/workspace.py`. Test: `cli/tests/test_init_standalone.py`, `cli/tests/run/test_workspace.py`.

- [ ] Tests first:
  - `init --standalone` (no `--policy`) installs `~/.agentic/policy/standalone/` and reports `policy: models` missing (placeholders), with the tiers file in the fix;
  - after editing the models, a second init is ready on the policy side and keeps the edit;
  - a guard refusal (not a repo root) writes nothing under `~/.agentic`;
  - `Workspace.load` finds the installed repo, and `agentic run feature-delivery --dry-run` resolves the library pipeline;
  - on `system="Windows"` the sandbox item's fix contains `wsl --install`, and the detail says team runs refuse without it;
  - the org cache is found at `<org>/repo`.
- [ ] See them fail. Implement. See them pass.

### Task 7: Call-flow documentation (M1-73)

**Files:** Rewrite `docs/architecture/call-flow.md`. Create `scripts/check_diagrams.py`, which runs the pinned mermaid-cli image in Docker on every Mermaid block of a Markdown file. It is not part of `check.py`, because it pulls a Chromium image.

- [ ] Survey the code path of `agentic run` → `Runtime` → `Engine` → steps / gates / loops → `StepRunners` / backend → transport → tools / sandbox / secret scan / skills / codeintel → telemetry → ledgers. Record file · function for each box, and the tracker row for each planned box.
- [ ] Write the diagrams and the step table. Run `uv run python scripts/check_diagrams.py docs/architecture/call-flow.md` until every diagram renders.
