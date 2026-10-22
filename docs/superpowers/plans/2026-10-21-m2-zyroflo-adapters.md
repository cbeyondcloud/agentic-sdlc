# M2 Zyroflo Adapters Implementation Plan (M2-27, M2-28, M2-43)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three thin adapters over Zyroflo (`sdlc-harness`, pinned, used unchanged - D34). (1) A connector adapter turns an external item (a Jira issue, a GitHub issue, an alert) into a run's title and specification through Zyroflo's JSON-path mappings (`connect.dig / spec_for / mapping_for`). (2) Domain-graph access: build a Zyroflo graph of an estate, ask impact of a file or a diff, serve the graph over MCP, and load it into a store (SQLite always; neo4j only when a server is configured). (3) A `domain-graph` code-intelligence adapter answering `agentic.codeintel`'s `CodeIntel` protocol, so agents can ask cross-repo impact questions. A repo selects it with `codeintel.adapter: domain-graph`; it then answers alongside code-review-graph.

**Architecture:**
- **`sdlc_commons.connectors`** (M2-27): `shipped_mappings()` (the Jira, GitHub-issue and alert mappings Zyroflo ships as content), `mapping_for(repo, source)` (the repo's declared mapping, read by Zyroflo's `mapping_for`; else the shipped one), `item_for(source, mapping, payload)` -> `ExternalItem` (title and specification from `spec_for`, plus kind, external id, link and Zyroflo's `spec_hash` identity), and `dig` re-exported with its `ABSENT` sentinel. `ConnectError` becomes `ConnectorError`.
- **`sdlc_commons.domain_graph`** (M2-28): `build_graph(workspace, out, *, harness=None)` over `tools.graph.build.build`; `DomainGraph(graph_dir)` over Zyroflo's `GraphService` (the MCP server's eight tools as methods, reloaded when the graph on disk changes): `status`, `components`, `impact`, `impact_of_targets`, `test_files`, `context_pack`; `mcp_server(graph_dir)` (Zyroflo's `build_server`; refused when fastmcp is absent); `open_store(graph_dir, backend)` over `core.store.load.load_into`. Every Zyroflo refusal becomes `DomainGraphError` with Zyroflo's own words.
- **`sdlc_commons.graph_codeintel`** (M2-43): `DomainGraphCodeIntel(graph, tree, component=None)`, the `CodeIntel` protocol by shape (commons does not import core, as `release.py` already does): `dependents`, `impact`, `tests_for`, `overview`. Answers are `GraphCandidates(files, source, note)`: files inside the run's tree go in `files`; files in other repositories and the services reached go in `note`, so a cross-repo answer reaches the agent without a path outside the tree.
- **Core:** `agentic.codeintel.combined.Combined(*adapters)` (several adapters answering as one: files merged best first without repeats; one adapter failing is named in the note, all failing is `CodeIntelError`). `RepoConfig.codeintel` gains `adapter: domain-graph`, `graph` (default `.agentic/local/graph`) and `component`.
- **CLI:** `agentic_cli.codeintel`: `codeintel_settings(config)` (the repo's `codeintel` section validated by core's model), `codeintel_for(repo, settings)` (the factory `StepWiring` takes: code-review-graph when installed, plus the domain graph when selected and built, combined), `codeintel_warning(...)` for preflight, and the boundary that turns commons answers and errors into core's `Candidates` and `CodeIntelError`. `runtime._wiring` and the preflight codeintel check call it.

**Tech Stack:** Python 3.12; Zyroflo `sdlc-harness` at the pinned commit (`tools.sdlc.connect`, `tools.sdlc.content`, `tools.sdlc.intake.spec_hash`, `tools.graph.build`, `tools.graph.impact`, `tools.graph.mcp_server`, `tools.graph.rules`, `tools.graph.core.store.load`); fastmcp (already locked through the `codeintel` extra) for the MCP server; pytest. **No new dependency.**

**Spec:** §1.4 (Zyroflo artefacts: domain graph as the second codeintel adapter, JSON-path connectors), §10.1 (two graph levels, answers are candidates), §16 (intake mapping uses Zyroflo's connector format). Spike S14 (D34), spike S5 (decision 2: Graphify fallback; decision 3: domain-graph row).

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| Item -> title/specification | Zyroflo `connect.mapping_for / spec_for / dig / declared_kind`, `intake.spec_hash` | - |
| Jira / GitHub-issue / alert mappings | Zyroflo's shipped `sdlc_templates/connectors/*.json` via `content.root()` | - |
| Graph build, impact, rules, MCP | Zyroflo `tools.graph` (`build`, `impact`, `impact_diff`, `parse_diff`, `discover_rules`, `GraphService`, `build_server`) | - |
| Store (neo4j optional) | Zyroflo `load_into` (`sqlite` / `neo4j`) | - |
| Fallback where code-review-graph answers nothing (DF-85) | the domain graph through `Combined` | Graphify 0.9.79 (Apache-2.0): pulls ~25 native tree-sitter grammar wheels, numpy and rapidfuzz, and caps tree-sitter below 0.26 (Zyroflo's lock has 0.26.0) - **needs decision** |

## Zyroflo gaps found (owner-merged Zyroflo PRs, never commits here)

- **Z7 - the graph build cannot run from the installed wheel.** `tools.graph.build.load_installer()` imports `sdlc-init.py` from the checkout root, which the wheel does not ship, so `build`, `check_freshness` (and the MCP `check_fresh` tool) raise `FileNotFoundError` when installed. Proposed PR: ship `sdlc-init.py` as package data (`tools/_content/`, as Z2 did for the lifecycle) and resolve it through `content.root()`. Meanwhile `build_graph` uses Zyroflo's own loader when it works, and otherwise loads `sdlc-init.py` from a Zyroflo checkout the caller names (`harness=`); with neither it refuses, naming Z7.
- **Z8 - the connectors folder is fixed at `.sdlc/connectors/`.** `mapping_for(target, source)` reads only `<target>/.sdlc/connectors/<source>.json`. Proposed PR: an optional folder parameter (default unchanged). Meanwhile a repo declares mappings where Zyroflo reads them (`.sdlc/connectors/`), which a repo already using Zyroflo shares.

## Global Constraints

- **D34.** Only `sdlc_commons` imports `tools`; core never imports commons; no Zyroflo code is copied. Where Zyroflo lacks something, the adapter uses what exists and the gap is recorded (Z7, Z8).
- **Candidates.** Every answer names its adapter (`domain-graph`) and is labelled a candidate by the agent tools. Files in `files` are tree-relative POSIX paths that exist in the tree; a graph path that is absolute, has `..` or names `.git` is never joined to the tree.
- **Cross-repo answers** go in the note, capped (`MAX_NOTED = 20` files, 10 services), best first, as `component/path (score)`.
- **Tree mapping.** With `component` set, the run's tree is that component (a repo whose root is the component root). Without it, the tree is the workspace root and a path belongs to the component with the longest matching path (`repo` nodes' `props.path`; `.` matches everything).
- **neo4j is optional.** The neo4j test runs only when `NEO4J_URI` is set and skips with that reason otherwise; nothing installs a server on a developer laptop.
- **Fixture estate.** Tests build Zyroflo's `pantry` fixture (a copy in `tmp_path`, never the installed package). Until Z7 merges, the build needs `sdlc-init.py` from a checkout at the pinned commit: the test helper finds it in uv's git cache (present after any `uv sync` of this workspace, Windows and Linux) and skips with a reason naming Z7 when it is not there.
- **Provider-agnostic, cross-platform.** No vendor names in core or CLI flags; paths through `pathlib`/POSIX conversion; graph paths compared case-sensitively as Zyroflo cites them.
- **Formatting, CI and records:** never `# fmt: skip`/`# fmt: off`; line length 100; pytest exit codes checked directly; `uv run python scripts/check.py --all` green on Windows and Linux; TRACKER.md, DEFERRED.md and DECISIONS are updated by the coordinator from this track's output.

## Review Focus

1. **A graph path that escapes the tree** (`../x`, `/etc/passwd`, `.git/config`) never reaches `files` and is never joined to the tree. Test: Task 3 `test_graph_paths_outside_the_tree_are_never_listed`.
2. **Cross-repo impact reaches the agent**: on the pantry estate, `dependents` of `pantry-api`'s publisher names `restock-worker`'s consumer (note in component mode, files in workspace mode). Test: Task 3 `test_component_mode_puts_other_repos_in_the_note`, `test_workspace_mode_lists_other_components_as_tree_files`.
3. **No Zyroflo refusal escapes as a traceback**: a damaged `graph.jsonl`, an unknown component, a missing graph, a missing installer become `DomainGraphError`/`CodeIntelError` with the reason. Test: Task 2 `test_a_damaged_graph_is_refused_by_line`, Task 4 `test_a_commons_error_becomes_a_codeintel_error`.
4. **Selecting `domain-graph` without a built graph** degrades with one preflight warning and code-review-graph alone; it never refuses the run. Test: Task 5 `test_domain_graph_without_a_graph_warns_and_keeps_crg`.
5. **The connector adapter is deterministic** (same payload, same identity) and keeps Zyroflo's absent-vs-null distinction. Test: Task 1 `test_the_same_item_has_the_same_identity`, `test_a_null_title_is_refused_differently_from_an_absent_one`.

---

### Task 1: Connector adapter (M2-27)

**Files:**
- Create: `commons/src/sdlc_commons/connectors.py`
- Test: `commons/tests/test_connectors.py`

**Interfaces:**
- `class ConnectorError(Exception)`.
- `SHIPPED = ("alert", "github-issue", "jira")`; `shipped_mappings() -> dict[str, dict]`.
- `mapping_for(repo: Path | None, source: str) -> dict` - the repo's `.sdlc/connectors/<source>.json` through Zyroflo's `mapping_for`; else the shipped mapping; else `ConnectorError` naming both places.
- `@dataclass(frozen=True) ExternalItem(source, kind, title, spec, identity, external_id, url)`; `item_for(source, mapping, payload) -> ExternalItem`.
- `ABSENT`, `dig(payload, path, missing=None)` - Zyroflo's.

**TDD steps:**
- [ ] Tests: the three shipped mappings load and each declares a title; a Jira payload and a GitHub-issue payload (shapes from each API) give the expected title, kind (`requirement` / `defect`), id, link and specification text; the same item twice has the same identity, a changed body a different one; a repo mapping overrides the shipped one; a null title and an absent title are refused with different reasons; an unknown source with no shipped mapping is refused naming `.sdlc/connectors/`; `dig` returns `ABSENT` for a missing path and `None` for a null value. Run: fail on import.
- [ ] Implement; run green; commit.

### Task 2: Domain-graph access (M2-28)

**Files:**
- Create: `commons/src/sdlc_commons/domain_graph.py`, `commons/tests/graph_estate.py` (fixture helpers: `pinned_checkout()`, `pantry(tmp_path)`)
- Test: `commons/tests/test_domain_graph.py`

**Interfaces:**
- `class DomainGraphError(Exception)`.
- `build_graph(workspace: Path, out: Path, *, harness: Path | None = None) -> dict` (the build report).
- `class DomainGraph(graph_dir)`: `status() -> dict`; `components() -> dict[str, str]` (name -> workspace-relative path); `impact(component, path, lines=None) -> dict`; `impact_of_targets(component, targets) -> dict` (Zyroflo `impact_diff`); `parse_diff(text)`; `test_files() -> set[tuple[str, str]]`; `context_pack(component, path=None, diff_text=None, budget=...) -> dict`.
- `mcp_server(graph_dir, *, workspace=None)` -> Zyroflo's FastMCP server.
- `open_store(graph_dir, backend="sqlite", **options) -> tuple[store, counts]`.

**TDD steps:**
- [ ] Tests (module-scoped built pantry graph): impact of `pantry-api/src/pantry_api/publisher.py` reaches `restock-worker`'s consumer and the restock migration, with the `restock-worker` service at one hop (EXPECTED.md chain); impact of a diff touching `routes.py` reaches `restock-worker/src/restock_worker/client.py`; `components()` maps the six components to their paths; an unknown component and a damaged graph are refused (`DomainGraphError`, the line named); a missing graph folder is refused; without Zyroflo's installer and without `harness`, `build_graph` refuses naming Z7; the MCP server lists Zyroflo's eight tools; the SQLite store holds the graph's nodes and edges; neo4j runs only with `NEO4J_URI` (skip reason says so). Run: fail on import.
- [ ] Implement; run green; commit.

### Task 3: Domain-graph codeintel adapter (M2-43)

**Files:**
- Create: `commons/src/sdlc_commons/graph_codeintel.py`
- Test: `commons/tests/test_graph_codeintel.py`

**Interfaces:**
- `@dataclass(frozen=True) GraphCandidates(files: tuple[str, ...], source: str, note: str = "")`.
- `class DomainGraphCodeIntel`: `name = "domain-graph"`, `supports = {"dependents", "impact", "tests_for", "overview"}`; `__init__(graph: DomainGraph, tree: Path, component: str | None = None)`.

**TDD steps:**
- [ ] Tests on the pantry estate: component mode (tree = a copy of `pantry-api`): `dependents("src/pantry_api/publisher.py")` lists in-tree `src/pantry_api/routes.py` and `tests/test_routes.py`, and the note names `restock-worker/src/restock_worker/consumer.py` and the service `restock-worker`; workspace mode (tree = the estate): the same question lists `restock-worker/src/restock_worker/consumer.py` in `files`; `impact(diff)` of a routes change; `tests_for("src/pantry_api/routes.py")` is `tests/test_routes.py`; a file the graph does not cite answers empty with a note saying so; `overview()` names the components and node counts; graph paths that escape the tree are dropped; a path in no component answers empty. Run: fail on import.
- [ ] Implement; run green; commit.

### Task 4: Combined adapter and repo config (core)

**Files:**
- Create: `core/src/agentic/codeintel/combined.py`; Modify: `core/src/agentic/codeintel/__init__.py`, `core/src/agentic/repoconfig.py`
- Test: `core/tests/codeintel/test_combined.py`, `core/tests/test_repoconfig.py` (extend)

**Interfaces:**
- `class Combined`: `__init__(*adapters)`; `name` (`a + b`), `supports` (union); each operation asks the adapters that support it in order; files merged without repeats; notes joined; a failing adapter is named in the note; all failing raises `CodeIntelError`.
- `CodeIntelConfig.adapter: Literal["code-review-graph", "domain-graph", "none"]`, `graph: str = ".agentic/local/graph"`, `component: str | None = None`.

**TDD steps:**
- [ ] Tests: merge order and de-duplication; an operation only one adapter supports; one failure noted; all failing raises; `overview` joins both; repo config accepts `domain-graph` with `graph`/`component`, refuses an empty `graph` and an unknown key. Run: fail.
- [ ] Implement; run green; commit.

### Task 5: CLI wiring (`codeintel.adapter`)

**Files:**
- Create: `cli/src/agentic_cli/codeintel.py`; Modify: `cli/src/agentic_cli/runtime.py`
- Test: `cli/tests/test_codeintel_wiring.py`

**Interfaces:**
- `codeintel_settings(config: dict) -> CodeIntelConfig` (`CliError` INVALID on a bad section).
- `graph_dir(repo, settings) -> Path`; `codeintel_warning(repo, settings) -> str | None`.
- `codeintel_for(repo, settings, *, crg_available) -> Callable[[Path, Path], CodeIntel] | None`.
- `class GraphIntel` - the commons adapter seen through core's types.

**TDD steps:**
- [ ] Tests: `none` gives no factory; `code-review-graph` without the extra gives none; `domain-graph` with a built pantry graph gives the domain graph (combined with code-review-graph when installed) and its answers are core `Candidates`; `domain-graph` without a graph warns once and keeps code-review-graph; a commons error becomes `CodeIntelError`; an invalid section is `CliError` INVALID; runtime `_wiring` uses it. Run: fail.
- [ ] Implement; run green; commit.

### Task 6: Full check

- [ ] `uv run python scripts/check.py --all` green on Windows and Linux; grep touched files for `fmt: skip`/`fmt: off`; commit.
