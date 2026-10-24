# M2-44 Opt-in Index Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A repo whose full local index build is too slow can opt in (org.yaml `index_export_workflow`) to a nightly CI job that builds the code-intelligence index once and publishes a deterministic, path-independent sharded export **outside `main`** (a workflow artifact, or an `agentic-index` ref). Developers and runs import it, pinned to the tool and schema version, and catch up with an incremental `update` instead of a full build. The export is byte-identical across rebuilds, checkout roots and Windows/Linux; import + update equals a fresh build.

**Architecture:**
- **Core `agentic.codeintel.export`** (no code-review-graph import; plain `sqlite3`):
  - `export_index(db, tree, out) -> dict`: the S9 `crg_export.py` design, hardened. Nodes and edges only (the parsed ground truth); volatile columns (`id`, `updated_at`) and derived ones (`community_id`, `signature`, `target_resolution`) dropped; the tree's absolute root replaced by `${ROOT}` only at a path boundary; one sorted JSONL shard per source file, named by the sha256 of its repo-relative path (`shards/ab/<30 hex>.jsonl`: no Windows-invalid characters, no case collisions on Windows/macOS, no MAX_PATH trouble); `manifest.json` with format, tool, tool version, schema version, commit, kept metadata and each shard's path, sha256 and counts. Written LF, UTF-8, `sort_keys`, no timestamps. A root that leaks into a shard is refused.
  - `read_export(folder) -> dict`: checks the manifest (format, tool, safe shard names, every shard's sha256) and refuses a damaged or tampered export (`ExportError`).
  - `check_tool(manifest, installed)`: refuses a different code-review-graph version with a clear message (`ExportMismatch`), naming both versions and the fix.
  - `import_export(folder, tree, data_dir, env)`: runs `_crg_import` in a subprocess.
- **Core `agentic.codeintel._crg_import`** (the second module allowed to import code-review-graph; import-linter `ignore_imports` gains one line): creates the schema through `GraphStore` in a temporary file, refuses a schema version other than the manifest's, inserts the rows with `${ROOT}` rewritten to this tree, restores metadata (`git_head_sha` = the export's commit, `repo_root` = this tree, `build_state` complete), runs the tool's own private `_run_postprocess` (S9: the public CLI leaves two derived tables empty) and moves the file into place atomically.
- **`LocalIndex(seed=...)`**: with no index yet and a seed export folder, `ensure()` imports the seed and then runs the usual freshness check (an `update` from the export's commit to HEAD, uncommitted edits included). A seed that cannot be imported (another tool version, damaged) falls back to a full build and records why (`seed_note`).
- **CLI `agentic index`** (`commands/index.py`, standalone - no login or policy needed):
  - `export --out DIR [--repo]`: refuses a dirty tree (the export is of a commit), builds into a temporary data folder outside the tree, writes the export; `--out` must be new, empty or an earlier export (never `--out .`).
  - `import DIR | --ref REF [--repo]`: verifies, copies the export to `.agentic/local/index-export/` (the seed every run's index starts from) and catches the developer's `.agentic/local/index/` up. `--ref` reads a fetched `agentic-index` ref through `git archive` (tarfile `data` filter).
  - `publish DIR [--ref agentic-index] [--remote origin] [--dry-run]`: a commit of the export (scratch `GIT_INDEX_FILE`, `* -text` attributes) on that one ref, as a child of the ref's last export so only changed shards change (an identical export pushes nothing; after `HISTORY` = 30 commits the ref restarts from one commit), pushed with `--force-with-lease` against the tip it inspected. Refuses `main`, `master`, the remote's default branch, the current branch, tags, refs outside `refs/heads/`/`refs/agentic/`, and any existing ref whose tip is not an earlier index export (review fix F1/F3). `--dry-run` builds the commit and pushes nothing.
- **Runs seed from the export**: `agentic_cli.codeintel.codeintel_for` passes `.agentic/local/index-export` as the seed when it holds a manifest.
- **Job template** `cli/src/agentic_cli/templates/agentic-index-export.yml` (the single source; `repo_setup.INDEX_EXPORT_WORKFLOW` reads it): nightly + `workflow_dispatch` with `dry_run`; top-level `permissions: contents: read`; job `export` (checkout without persisted credentials, `agentic index export` into `$RUNNER_TEMP`, upload artifact unless dry run); job `publish-ref` only when the repo variable `AGENTIC_INDEX_PUBLISH == 'ref'` and not a dry run, the only job with `contents: write`, which runs `agentic index publish`. Concurrency group, timeout and an off-the-hour cron are taken from Zyroflo's `snapshot-publish.yml` pattern (that template commits to the default branch, which D39 forbids, so its file is not reused).
- **actionlint** v1.7.12 (MIT, released 2026-03-30), pinned by version and sha256 for amd64/arm64 in `scripts/linux-check.Dockerfile`, runs as the Linux-only check `workflow lint` over the shipped workflow templates. A pytest test runs it where it is on PATH and skips natively with the reason.

**Tech Stack:** Python 3.12 stdlib (`sqlite3`, `json`, `hashlib`, `tarfile`), code-review-graph 2.3.9 (already pinned, extra `codeintel`), git CLI plumbing, Typer, actionlint 1.7.12 in the Linux check image only. No new Python dependency.

**Spec:** §10.3 (D39), spike S9 (`docs/spikes/S9-committed-index.md`, `spikes/s9_committed_index/crg_export.py`), §4.2/§4.3 (`index_export_workflow`, `agentic-index-export.yml`). Tracker row M2-44; deferred DF-82, DF-190, DF-460, DF-464, DF-465, DF-467.

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| Index build, update, schema, post-processing | code-review-graph 2.3.9 (MIT): `GraphStore`, `_run_postprocess`, `migrations.LATEST_VERSION`, `_canonical_repo_root` | It has no public import and its JSON/GraphML exports keep absolute paths and are not deterministic (checked: `exports.py`) |
| Export format and importer | spike S9 `crg_export.py` design (our own throwaway code) | — |
| Nightly job shape | Zyroflo `sdlc_templates/snapshot-publish.yml` (pinned sdlc-harness): concurrency group, timeout, off-the-hour cron, the 60-day schedule note | Its last step commits and pushes to the default branch; D39 publishes outside `main`. The file is read, not shipped or imported (core never imports Zyroflo) |
| Workflow lint | actionlint 1.7.12 (MIT), pinned binary in the Linux check image | shellcheck integration (GPL-3.0: not on the allow-list) is left off |
| Git ref publishing | git plumbing (`hash-object`, `update-index`, `write-tree`, `commit-tree`, `push`) as `repo_setup.commit_on_branch` already does | GitPython/dulwich: one more dependency for five plumbing calls |

## Global Constraints

- **Determinism.** The export is a function of the committed file bytes only: no timestamps, no ids, no absolute paths, no OS separators, LF, sorted keys and lines, shard names from a hash of the POSIX relative path. A CRLF checkout has different bytes (and so different `file_hash` values): the CI job builds from the checkout CI makes, and an import into a CRLF checkout re-parses the files whose hash differs on `update` (correct, slower).
- **Pinned import.** An export whose `format`, `tool` or `tool_version` differs from this install is refused before anything is written; a `schema_version` different from the installed tool's is refused by the import process. The local index is replaced only by a complete import (temporary file, then rename).
- **Never `main`.** No command writes to the default branch or the current branch. The job template never runs `git commit`/`git push` itself; only `agentic index publish` pushes, to one non-default ref, in the one job with `contents: write`.
- **Tokens.** The job uses only the workflow's own `GITHUB_TOKEN`: `contents: read` to export and upload the artifact; `contents: write` in `publish-ref`, for the `agentic-index` ref only (protect `main` with branch protection or a ruleset the token cannot bypass). It needs **no agentic service-account token**: M2-14 service accounts hold only `run.trigger`, and this job starts no run and calls no control plane.
- **Opt-in.** `agentic init --repo` proposes the workflow only when org.yaml sets `index_export_workflow: true` (already built, SP14a); default off.
- **Provider-agnostic, cross-platform.** No vendor names; paths through `pathlib`; every git call `core.fsmonitor=false`; text written with `newline="\n"`; tests run on Windows natively and Linux in the container.
- **Formatting, CI and records:**
  - never add `# fmt: skip` or `# fmt: off`; line length 100;
  - check pytest's exit code directly;
  - `uv run python scripts/check.py --all` must be green on Windows and Linux;
  - TRACKER.md, DEFERRED.md and DECISIONS are updated by the coordinator from this track's output.

## Review Focus

1. **Byte-identical exports.** Two builds of one commit in two checkouts with different roots (one with a space in its name) produce identical bytes in every file; the committed golden manifest matches on Windows and Linux. Test: Task 1 `test_export_is_byte_identical_across_rebuilds_and_roots`, `test_export_matches_the_golden_manifest`.
2. **Root rewriting at a boundary only.** A sibling folder that shares the root's prefix (`/w/repo2` next to `/w/repo`) is never rewritten, and a root that would leak is refused. Test: Task 1 `test_root_is_replaced_only_at_a_path_boundary`, `test_a_leaked_root_is_refused`.
3. **Pinned, all-or-nothing import.** Another tool version, schema version or format is refused with both versions named; a tampered shard is refused; a failed import leaves the previous index untouched. Test: Task 2 `test_a_different_tool_version_is_refused`, `test_a_different_schema_version_is_refused`, `test_a_tampered_shard_is_refused`, `test_a_failed_import_keeps_the_old_index`.
4. **Import + update equals a fresh build**, in a checkout at another root, one commit ahead, with a file added, changed and deleted - nodes and edges including derived columns. Test: Task 2 `test_import_then_update_equals_a_fresh_build`.
5. **Nothing reaches `main`.** `publish` refuses the default, current, `main`/`master` and tag refs; a dry run leaves every remote ref and the local branch unchanged; a real publish moves only `agentic-index`. The template's only write permission is in `publish-ref`, guarded by the opt-in variable and `!inputs.dry_run`. Test: Task 4 `test_publish_dry_run_writes_nothing`, `test_publish_never_targets_main`; Task 5 `test_the_workflow_never_writes_main`.
6. **Paths from an export are never trusted.** A manifest naming `../x` or an absolute shard path, and a ref archive with a link or `..` member, are refused. Test: Task 2 `test_unsafe_shard_names_are_refused`; Task 4 `test_import_from_a_ref_refuses_unsafe_members`.

---

### Task 1: Deterministic sharded export (DF-82)

**Files:**
- Create: `core/src/agentic/codeintel/export.py`
- Test: `core/tests/codeintel/test_index_export.py`, golden `core/tests/codeintel/golden/export-manifest.json`

**Interfaces:**
- `ROOT_TOKEN = "${ROOT}"`, `FORMAT = 1`, `TOOL = "code-review-graph"`, `MANIFEST = "manifest.json"`.
- `class ExportError(CodeIntelError)`; `class ExportMismatch(ExportError)`.
- `export_index(db: Path, tree: Path, out: Path) -> dict` (the manifest). `out` is replaced only when it is missing, empty or holds a `manifest.json`.

- [ ] Write failing tests: rebuild/root identity (two checkouts, fixed commit dates, LF bytes), golden manifest, boundary rewrite on a synthetic DB, leak refusal, `out` safety.
- [ ] Run them; see `ModuleNotFoundError`.
- [ ] Implement `export_index`.
- [ ] Run; green. Commit.

### Task 2: Pinned import, seed, and import + update equals fresh (DF-82)

**Files:**
- Create: `core/src/agentic/codeintel/_crg_import.py`
- Modify: `core/src/agentic/codeintel/export.py` (`read_export`, `check_tool`, `installed_tool_version`, `import_export`), `core/src/agentic/codeintel/index.py` (`seed`, `seed_note`, `load_export`), `pyproject.toml` (import-linter ignore line)
- Test: `core/tests/codeintel/test_index_export.py`

**Interfaces:**
- `read_export(folder: Path) -> dict`; `check_tool(manifest: dict, installed: str | None) -> None`; `installed_tool_version() -> str | None` (`importlib.metadata`).
- `import_export(folder, tree, data_dir, env, *, python=sys.executable) -> dict`.
- `LocalIndex(..., seed: Path | None = None)`; `LocalIndex.load_export(folder)` (under the index lock); `LocalIndex.seed_note: str | None`.

- [ ] Write failing tests: roundtrip export(import(export)) identical; import + update equals fresh (nodes and edges incl. derived columns); tool/schema/format mismatch refused with both versions; tampered shard; unsafe shard names; failed import keeps the old index; `ensure()` with a seed imports instead of building; a bad seed falls back to build with `seed_note`.
- [ ] Run; fail for the missing functions.
- [ ] Implement; add the import-linter line.
- [ ] Run; green. Commit.

### Task 3: `agentic index export` and `import` (DF-190)

**Files:**
- Create: `cli/src/agentic_cli/commands/index.py`; Modify: `cli/src/agentic_cli/commands/__init__.py` (one line), `cli/src/agentic_cli/codeintel.py` (seed for runs)
- Test: `cli/tests/test_index_command.py`

**Interfaces:**
- `agentic index export --out DIR [--repo PATH]` -> `{out, commit, files, nodes, edges, tool_version, schema_version}`; dirty tree -> REFUSED; no extra -> ERROR with hint; unsafe `--out` -> REFUSED.
- `agentic index import [DIR] [--ref REF] [--repo PATH]` -> `{commit, seed, index, recorded_commit}`; mismatch -> REFUSED (hint names the versions); damaged -> INVALID.
- `codeintel_for(...)` passes `repo/.agentic/local/index-export` as `seed` when it holds a manifest.

- [ ] Failing CLI tests through `CliRunner` with `--json`; run, see the missing command.
- [ ] Implement; green. Commit.

### Task 4: `agentic index publish` to a ref outside `main`

**Files:**
- Modify: `cli/src/agentic_cli/commands/index.py`; Create: `cli/src/agentic_cli/index_publish.py`
- Test: `cli/tests/test_index_command.py`

**Interfaces:**
- `publish_export(repo, folder, *, ref="agentic-index", remote="origin", dry_run=False) -> dict` (`{ref, remote, commit, files, pushed, dry_run}`).
- `archive_export(repo, ref, into)` for `import --ref`.

- [ ] Failing tests with a bare remote: dry run leaves all remote refs and the local branch unchanged; a publish moves only `refs/heads/agentic-index`; `main`, `master`, the remote default, the current branch, a tag and `refs/notes/x` are refused; import from the fetched ref equals import from the folder; unsafe archive members refused.
- [ ] Implement; green. Commit.

### Task 5: The job template, actionlint, dry-run proof

**Files:**
- Create: `cli/src/agentic_cli/templates/agentic-index-export.yml`
- Modify: `cli/src/agentic_cli/repo_setup.py` (read the template), `scripts/linux-check.Dockerfile` (actionlint), `scripts/check.py` (`workflow lint` Linux-only check), `scripts/tests/test_check_engine.py`
- Test: `cli/tests/test_index_workflow.py`

- [ ] Failing tests: the template is what `init --repo` proposes; top-level `contents: read`; no `run:` commits or pushes; only `publish-ref` has `contents: write`, gated on `vars.AGENTIC_INDEX_PUBLISH == 'ref'` and not a dry run; artifact upload skipped on dry run; the job runs the commands Task 3/4 built; the documented token section exists; actionlint clean (skipped natively with the reason); check.py lists `workflow lint` as Linux-only with a pinned, checksummed actionlint.
- [ ] Implement; green. Commit.

### Task 6: codeintel review follow-ups (DF-464, DF-465, DF-467 part)

**Files:**
- Modify: `cli/src/agentic_cli/codeintel.py`, `core/src/agentic/codeintel/combined.py`, `cli/src/agentic_cli/runtime.py`
- Test: `cli/tests/test_codeintel_wiring.py`, `core/tests/codeintel/test_combined.py`, `cli/tests/run/test_codeintel_runtime.py`

- [ ] DF-464: the adapter hint only when the error is about `adapter`; a non-mapping section, an unknown key, an empty `graph`/`component` each get their own hint; tests first.
- [ ] DF-465: `Combined` keeps the note of an adapter that answered nothing only when no adapter answered; tests first.
- [ ] DF-467 (part): preflight reports an invalid `codeintel` section as a Problem instead of raising, and a domain graph whose `graph.jsonl` does not parse as a warning; tests first. Graph freshness moves with DF-460 (graph build job).
- [ ] Green. Commit.

### Task 7: Full check

- [ ] `uv run python scripts/check.py --all` to completion; fix and rerun until green.
