# b17 Java code intelligence (M3-27, SP9) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:executing-plans. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Java Spring Boot repo is in the pilot (owner decision 2026-10-31), so Java code
intelligence must work. Today `tests_for` finds 0% of gson's tests (S5). This plan does
three things:

1. A pure-Python JVM adapter answers on every machine with no binary.
2. codebase-memory-mcp, which S5 found best on Java, becomes an optional backend. It is
   routed by repo language, fetched only through the hash-pinned tool fetcher, and falls
   back with a warning when it is missing.
3. The sandbox template lets Maven and Gradle reach their registries.

**Architecture:** This reuses the M2-43 patterns: `CodeIntelConfig.adapter`, `Combined`,
and the CLI factory `codeintel_for`. The binary is found through the M1-51 verified tool
cache (`ToolLocator`). It is never downloaded at run time.

## Upstream check (2026-10-31, before any code)

| Question | Finding |
|---|---|
| Current release | v0.12.1 (2026-10-16); S5 measured v0.11.0 |
| Licence | MIT, at tag v0.12.1 |
| Builds | windows-amd64/arm64 (.zip); darwin-amd64/arm64, linux-amd64/arm64 (+ `-portable`) (.tar.gz). Every asset has a sigstore `.bundle`; `checksums.txt` matches the GitHub asset digests |
| Telemetry | none, per README and SECURITY.md |
| Network | **contradictory at v0.12.1.** README: "makes no network request of its own accord". SECURITY.md: after MCP `initialize`, a background thread GETs `api.github.com/.../releases/latest` (curl, 5 s cap). CLI mode does not send `initialize`. |
| Process model | v0.12 CLI calls start or join a shared coordination daemon. It caches under `CBM_CACHE_DIR`, and the daemon may serve the "Lens" UI on loopback |
| Windows code signing | not stated (sigstore bundles are not Authenticode) |

**Design consequence (needs owner):** v0.12.1 may call the network, and on native Windows we
cannot run it under srt (srt needs WSL2). So the template ships **no** pin. Java runs on the
pure-Python adapter by default. An org that approves the binary pins it in
`tool-pins.yaml` (the exact v0.12.1 hashes are commented in `org.yaml`). The fetcher then
refuses the Windows build unless the org marks it `signed`. The adapter always runs it in
CLI mode (never MCP `initialize`), with HOME and the caches inside the run's data folder.

## Files

- Create `core/src/agentic/codeintel/jvm.py`: `is_jvm_repo`, `JvmConventions` (tests_for,
  dependents, impact).
- Create `core/src/agentic/codeintel/cbm.py`: `CodebaseMemory` (CLI-mode adapter, command
  injectable for tests).
- Modify `core/src/agentic/repoconfig.py`: adapter adds `auto` (new default) and
  `codebase-memory`.
- Modify `cli/src/agentic_cli/codeintel.py`: route by language, combine, warn, report status.
- Modify `cli/src/agentic_cli/runtime.py`: pass the located binary and the warning.
- Modify `cli/src/agentic_cli/doctor.py`: a `code intelligence` check.
- Modify `templates/policy-repo/policy/org.yaml`: Maven and Gradle egress hosts; commented
  cbm pin.
- Create `spikes/s5_code_graph/score_tests_for.py`: the before/after replay for tests_for.
- Modify `docs/spikes/S5-code-graph-bake-off.md` (addendum), `docs/runbooks/pilot-rollout.md`,
  TRACKER (M3-27) and DEFERRED (DF-920..929).
- Tests: `core/tests/codeintel/test_jvm.py`, `core/tests/codeintel/test_cbm.py`,
  `cli/tests/test_codeintel_wiring.py`, `cli/tests/test_doctor*.py`,
  `core/tests/policy/test_policy_repo_template.py`.

## Interfaces

```python
# core/src/agentic/codeintel/jvm.py
JVM_BUILD_FILES: tuple[str, ...]  # pom.xml, build.gradle(.kts), settings.gradle(.kts)
def is_jvm_repo(root: Path) -> bool: ...
class JvmConventions:  # CodeIntel; name "jvm-conventions"
    supports = {"dependents", "impact", "tests_for"}
    def __init__(self, tree: Path) -> None: ...

# core/src/agentic/codeintel/cbm.py
class CodebaseMemory:  # CodeIntel; name "codebase-memory-mcp"
    def __init__(self, command: Sequence[str], tree: Path, data: Path, *, timeout_s=600): ...

# cli/src/agentic_cli/codeintel.py
CBM_TOOL = "codebase-memory-mcp"  # its name in tool-pins.yaml
def symbol_route(repo: Path, settings) -> Literal["code-review-graph", "codebase-memory"]: ...
def codeintel_for(repo, settings, *, crg_available, cbm: Path | None = None) -> Factory | None
def codeintel_warning(repo, settings, *, crg_available, cbm_missing: str | None = None)
```

Routing:
- `auto` (no setting): `codebase-memory` for a JVM repo, otherwise `code-review-graph`.
- `domain-graph`: routes the same way, plus the domain graph.
- `code-review-graph` and `codebase-memory`: explicit overrides.

A JVM repo always gets `JvmConventions` first. Its answer is small, so its tests rank
first. After it comes the routed symbol adapter: cbm when it is located, otherwise crg
with a warning. Every answer names its producer (`Candidates.source`). They stay
candidates.

## TDD steps

- [ ] 1. `test_jvm.py`: write the failing tests first:
  - build-file detection;
  - FooTest, FooTests, FooIT and TestFoo in the same package under `src/test/java`;
  - prefix tests (`FooPathTest`);
  - same-package tests naming Foo;
  - tests importing `pkg.Foo`;
  - Kotlin;
  - dependents include source importers;
  - impact unions diff files;
  - non-JVM path → empty with a note;
  - paths confined to the tree.

  Then implement `jvm.py`.
- [ ] 2. `test_cbm.py`, against a fake CLI (a Python script):
  - index, then query, answer the files;
  - TESTS edges → tests_for;
  - noise edges dropped;
  - a non-zero exit → `CodeIntelError`;
  - HOME and the cache dirs point inside the data folder;
  - no `initialize` sent.

  Then implement `cbm.py`.
- [ ] 3. Repo config: test that `auto` is the default, that `codebase-memory` is accepted,
  and that an unknown value is still refused. Then implement.
- [ ] 4. CLI wiring tests:
  - routing per language and override;
  - JvmConventions first on JVM;
  - cbm used when located;
  - warning text when it is not;
  - Python repo unchanged;
  - JVM repo without the crg extra still gets JvmConventions.

  Then implement, plus the runtime and doctor check.
- [ ] 5. Template test: Maven Central and Gradle hosts allowed; the commented pin is
  not active. Then edit `org.yaml`.
- [ ] 6. Replay `score_tests_for.py` on the gson corpus: crg before, crg plus
  JvmConventions after, and cbm v0.11.0 from the S5 raw output. Write the S5 addendum.
- [ ] 7. Runbook, TRACKER, DEFERRED. Then lint, format, import-linter, pip-audit, licence
  check, and the tests.

## Review focus

- Nothing downloads or runs an unpinned binary.
- A missing or failing cbm never fails a run (`Combined` and preflight warning).
- Path confinement in `JvmConventions` (no reads outside the tree, links skipped).
- Bounded work per query: a file cap, and a size cap per file.
- No vendor names in engine or protocols. The tool name appears only in the adapter and
  in policy.
