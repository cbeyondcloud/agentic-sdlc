# SP9 Code Intelligence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agents can ask, through tools, what depends on a file, what a change affects (tests included), which tests cover a file, and for a budgeted context pack. Answers come from a code-review-graph index built and kept fresh locally for each run's tree, and are always labelled as candidates.

**Architecture:**
- **New package `agentic.codeintel`:**
  - the `CodeIntel` protocol, with declared operations and `Candidates` results;
  - a generic `context_pack` built on the protocol;
  - the agent tools, which offer only the operations the adapter supports;
  - `LocalIndex`: build, freshness by recorded commit, incremental update; all data in run state;
  - `CodeReviewGraph`: the adapter. Its queries run in a short-lived subprocess with code-review-graph's data folder set, so its heavy imports and environment never touch the framework process.
- **Wiring.** The CLI wiring hands each step's tool context an adapter for that run's tree.

**Tech Stack:** Python 3.12, `code-review-graph==2.3.9` (MIT; optional extra `codeintel` of agentic-core, installed in the dev group), pytest.

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md` (§10.1 interface, §10.3 as rewritten for D39), spike records `docs/spikes/S5-code-graph-bake-off.md` and `docs/spikes/S9-committed-index.md`, master plan rows M1-34..M1-36.

## Global Constraints

- **Answers are candidates (§10.1).** Every result names the adapter that produced it, and the agent sees "candidates - verify".
- **Index location (D39).** The index is built and updated locally, in run state: `<state_root>/index/<run_id>/<component>/` via code-review-graph's `--data-dir`, with `CRG_HOME` also in run state. It is never inside the tree and never in the user's home, and nothing is committed.
- **Composed answer (S5).** It is: files importing the seed; then `tests_for` for the seed file and for each symbol defined in it (qualified names, at most 150); then the remaining impact files (depth 2). The order is kept and duplicates removed.
- **Packaging.** code-review-graph is an optional extra (`agentic-core[codeintel]`); core works without it (no tools offered).
- **Provider-agnostic.** No vendor names in `agentic.codeintel`'s interface.
- **Formatting, CI and records:**
  - never add `# fmt: skip` or `# fmt: off`; line length 100;
  - check pytest's exit code directly (never through a pipe);
  - `uv run python scripts/check.py --all` must be green on Windows and Linux;
  - deferred items go into DEFERRED.md and TRACKER.md in the completing commit.

## Review Focus

1. **A seed path spelled differently** (`src\app.py`, `./src/app.py`, an absolute path inside the tree) gives the same answer, and a path outside the tree is refused, never queried. Test: Task 1 `test_paths_are_normalised_and_confined`.
2. **The tree changed since the last build** (the step before edited files). The next question sees the edit, because freshness is checked before every answer. Test: Task 2 `test_an_edit_is_seen_after_update`.
3. **code-review-graph is missing, or its build fails.** The tools are not offered (missing) or answer with a refusal naming the problem (failed build); the step never crashes. Test: Task 3 `test_a_failed_build_is_a_refusal_not_a_crash`.
4. **A huge answer** (hundreds of dependents) is capped, with a "+N more" line, so it cannot flood the model's context. Test: Task 1 `test_large_answers_are_capped`.
5. **A unified diff naming renamed, deleted or binary files.** `impact(diff)` uses the files that exist in the tree and ignores the rest. Test: Task 3 `test_impact_reads_files_from_a_diff`.

---

### Task 1: The `codeintel` interface and agent tools (M1-34)

**Files:**
- Create: `core/src/agentic/codeintel/__init__.py`, `core/src/agentic/codeintel/interface.py`, `core/src/agentic/codeintel/tools.py`
- Modify: `core/src/agentic/agents/tools/__init__.py` (the `codeintel` builder uses `agentic.codeintel.tools`), `core/src/agentic/agents/tools/context.py` (`CodeIntel` re-exported from `agentic.codeintel`)
- Test: `core/tests/codeintel/test_interface.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Candidates(files: tuple[str, ...], source: str, note: str = "")`: tree-relative POSIX paths, best first.
  - `class CodeIntel(Protocol)`:
    - `name: str`
    - `supports: frozenset[str]`, a subset of `{"dependents", "impact", "tests_for", "overview"}`
    - `dependents(path: str) -> Candidates`
    - `impact(diff: str) -> Candidates`
    - `tests_for(path: str) -> Candidates`
    - `overview() -> str`
  - `codeintel_tools(intel: CodeIntel, tree: Path) -> list[Tool]`:
    - one tool per supported operation (`dependents`, `impact`, `tests_for`, `overview`), plus `context_pack`, which is always offered when `dependents` or `tests_for` is supported;
    - paths are normalised against the tree (backslashes, `./`, absolute paths inside the tree) and a path outside it is refused;
    - answers list at most `MAX_LISTED = 50` files, then `"+N more"`, and end with `"(candidates from <source> - verify before relying on them)"`.
  - `context_pack(path: str, budget_chars: int = 20_000) -> str`: the seed file's text, then its tests and its dependents by name, until the budget is reached. The agent names the file it is working on. This is a ruling against spec §10.1's `context_pack(task, budget)`: the agent, not the adapter, knows which file the task touches.

- [ ] **Step 1: Write the failing tests** with a `FakeIntel` (supports all four, records calls):

```python
def test_each_supported_operation_is_a_tool(tmp_path): ...       # names == {dependents, impact, tests_for, overview, context_pack}
def test_unsupported_operations_are_not_offered(tmp_path): ...   # supports={"tests_for"} -> {tests_for, context_pack}
def test_answers_are_labelled_candidates(tmp_path): ...          # output ends "(candidates from fake - verify ...)"
def test_paths_are_normalised_and_confined(tmp_path): ...        # RF1: "src\\app.py", "./src/app.py", abs -> "src/app.py"; "../x" REFUSED, intel not called
def test_large_answers_are_capped(tmp_path): ...                 # RF4: 300 files -> 50 listed + "+250 more"
def test_context_pack_stays_within_its_budget(tmp_path): ...     # seed text + tests + dependents; len <= budget
def test_a_codeintel_capability_without_an_adapter_is_a_config_error(...): ...  # existing ToolConfigError kept
```

- [ ] **Step 2: Run** `uv run pytest core/tests/codeintel -q`. Expected: FAIL (no module `agentic.codeintel`).
- [ ] **Step 3: Implement** as specified. The existing three-method `CodeIntel` users (the agent tool tests) move to the new protocol.
- [ ] **Step 4: Run** `uv run pytest core/tests/codeintel core/tests/agents -q`. Expected: PASS, with the exit code checked.
- [ ] **Step 5: Commit.** `feat(core): codeintel interface, candidates and agent tools (M1-34)`. Mark M1-34 Done.

### Task 2: The local index: build, freshness, update (M1-36)

**Files:**
- Modify: `core/pyproject.toml` (`[project.optional-dependencies] codeintel = ["code-review-graph==2.3.9"]`), root `pyproject.toml` (dev group gains `agentic-core[codeintel]`); then `uv lock`
- Create: `core/src/agentic/codeintel/index.py`
- Test: `core/tests/codeintel/test_index.py`, with a fixture repo built by `core/tests/codeintel/conftest.py`: a small Python package (`pkg/models.py`, `pkg/service.py` importing models, `pkg/api.py` importing service) with `tests/test_models.py` and `tests/test_service.py`, committed in a git repo.

**Interfaces:**
- Produces:
  - `class IndexError_(Exception)`, exported as `CodeIntelError`.
  - `class LocalIndex(tree: Path, data_dir: Path, *, home_dir: Path, cli: Sequence[str] | None = None)`:
    - `env -> dict[str, str]`: `CRG_DATA_DIR=data_dir` and `CRG_HOME=home_dir`, added to the current environment.
    - `recorded_commit() -> str | None`: the `git_head_sha` code-review-graph stored, read through its `status`.
    - `fresh() -> bool`: the recorded commit equals the tree's HEAD and the tree has no uncommitted changes.
    - `ensure() -> None`:
      - no index: `build`;
      - stale: `update`;
      - fresh: nothing.
      - Each runs as `code-review-graph <cmd> --repo <tree> --data-dir <data_dir> -q` in a subprocess with `env`.
      - A non-zero exit raises `CodeIntelError` with the tool's last error line.
  - `cli` defaults to the `code-review-graph` script next to the running interpreter.
  - `available() -> bool`: the extra is installed (`importlib.util.find_spec("code_review_graph")`).

- [ ] **Step 1: Write the failing tests** (they run code-review-graph; skip when the extra is missing):

```python
def test_first_use_builds_into_run_state_only(fixture_repo, tmp_path): ...  # data_dir has graph.db; nothing new in tree or home
def test_a_fresh_index_is_not_rebuilt(fixture_repo, tmp_path): ...          # second ensure(): no build/update subprocess
def test_a_new_commit_makes_it_stale_and_update_refreshes_it(...): ...      # recorded_commit == new HEAD after ensure()
def test_an_edit_is_seen_after_update(...): ...                              # RF2: uncommitted edit -> fresh() False -> ensure() -> new import visible
def test_a_failing_build_raises_codeintel_error(tmp_path): ...               # cli pointing at a failing stand-in -> CodeIntelError with its message
```

- [ ] **Step 2: Run** `uv run pytest core/tests/codeintel/test_index.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** as specified. Ledger whether code-review-graph still writes anything outside `data_dir` and `CRG_HOME`.
- [ ] **Step 4: Run** `uv run pytest core/tests/codeintel -q`. Expected: PASS.
- [ ] **Step 5: Commit.** `feat(core): local code-intelligence index - build, freshness, update (M1-36, D39)`. Mark M1-36 Done. Add a DEFERRED row: the deterministic export and import (S9's `crg_export.py` design, pinned to the tool and schema version) move to M2-44, the opt-in export job.

### Task 3: The code-review-graph adapter (M1-35)

**Files:**
- Create: `core/src/agentic/codeintel/crg.py` (the adapter), `core/src/agentic/codeintel/_crg_query.py` (the subprocess entry point: reads a JSON request from stdin, writes a JSON answer to stdout)
- Test: `core/tests/codeintel/test_crg.py`

**Interfaces:**
- Consumes: `LocalIndex` (Task 2); `Candidates`, `CodeIntel` (Task 1).
- Produces:
  - `class CodeReviewGraph(index: LocalIndex)`, with `name = "code-review-graph"` and `supports = {"dependents", "impact", "tests_for", "overview"}`:
    - Every call first runs `index.ensure()`.
    - It then runs `python -m agentic.codeintel._crg_query` once per call, with `index.env`.
    - Inside, the subprocess uses `code_review_graph.tools.query_graph` (`importers_of`, `tests_for`) and `get_impact_radius`. It reads the symbols of a file through the same graph store, and returns tree-relative paths.
  - **`dependents(path)`:** the composed answer from Global Constraints.
  - **`tests_for(path)`:** file-level and symbol-level `tests_for`, merged.
  - **`impact(diff)`:** the files named in the diff's `+++ b/` and `--- a/` lines that exist in the tree; their composed answers merged; the diff's own files first.
  - **`overview()`:** the file, node and edge counts, plus the 10 most-imported files.
  - **Errors:** a `CodeIntelError` (failed build or query) becomes the tool's refusal text through the Task 1 tools, never an exception out of the agent run.

- [ ] **Step 1: Write the failing tests** (skip without the extra), on the Task 2 fixture repo:

```python
def test_dependents_of_models_are_service_api_and_tests(...):
    # {"pkg/service.py", "pkg/api.py", "tests/test_models.py", "tests/test_service.py"} <= set(answer.files)
def test_tests_for_service_includes_its_test_file(...): ...
def test_impact_reads_files_from_a_diff(...): ...             # RF5: a diff touching models.py, a deleted file and a binary
def test_overview_names_counts_and_hubs(...): ...
def test_a_failed_build_is_a_refusal_not_a_crash(...): ...    # RF3: tools answer "REFUSED: ... <tool error>"
def test_answers_name_their_source(...): ...                  # source == "code-review-graph"
```

- [ ] **Step 2: Run** `uv run pytest core/tests/codeintel/test_crg.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** as specified.
- [ ] **Step 4: Run** `uv run python scripts/check.py --all`. Expected: green, with the codeintel tests running (not skipped) on Windows and Linux.
- [ ] **Step 5: Commit.** `feat(core): code-review-graph adapter - composed dependents, tests, impact (M1-35)`. Mark M1-35 Done. Add DEFERRED rows:
  - the Graphify fallback for repos where code-review-graph returns nothing (S5 decision 2) → M1-81;
  - query time of about 2 s per composed call (one store open per symbol) → M1-81;
  - wiring each step's tool context with a `CodeReviewGraph` for the run's tree → M1-54, with DF-47.
