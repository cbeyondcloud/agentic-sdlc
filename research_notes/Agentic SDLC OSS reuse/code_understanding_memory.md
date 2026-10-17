# Code Understanding, Dependency Graph / Impact Analysis, and Repo Memory: OSS Reuse Options (as of 16 Oct 2026)

Scope: what to reuse for (a) an optional pre-built code index / dependency graph that answers "what depends on X", "which tests cover Y" and "what is the blast radius of changing Z", exposed to Design, Review and QA agents as tools (MCP preferred), on Windows, Linux and macOS, with Python, TypeScript and Java in v1; and (b) cross-run repo memory: v1 is a human-reviewable AGENTS.md-style file, and a learned vector memory on Qdrant comes later.

Note on star counts: they are what the GitHub pages showed on fetch (Oct 2026). Several are extremely high for tools this young, so read them as a rough popularity signal, not a quality measure.

---

## Q1. Graphify: what it is, licence, languages, graph build, query interface, health, Windows, limits, impact analysis

### Takeaway
Graphify is real, permissively licensed (Apache-2.0, with an MIT licence file also in the repo), and very actively developed. It is a local tree-sitter "knowledge graph" skill and CLI for coding assistants, covering about 36-40 languages, with an optional MCP stdio server. It does not yet have a symbol-level "reverse dependencies / tests covering Y" tool. Its impact tools are PR-level, built on GitHub PRs and graph communities. It is pre-1.0 and ships about two releases a day, so expect API churn. It is a good overview and orientation graph, but a weaker primary impact-analysis engine than code-review-graph or codebase-memory-mcp.

### Cited Findings
- **What it is:** Turns a codebase plus docs, SQL schemas, configs and PDFs into a queryable knowledge graph. It is offered as a `/graphify` skill for Claude Code, Cursor, Codex, Gemini CLI and others, and calls itself "a real graph you traverse", not a vector index. — [GitHub Graphify-Labs/graphify](https://github.com/Graphify-Labs/graphify)
- **Licence:** The GitHub sidebar shows "Apache-2.0, MIT licenses found", with both LICENSE and LICENSE-MIT files. The official FAQ says the open-source engine is Apache 2.0. — [GitHub](https://github.com/Graphify-Labs/graphify); [Graphify FAQ](https://graphify.com/faq)
- **Open core:** The engine is free, local, and needs no account. A hosted service (app.graphify.com: Free, Pro, Teams and Enterprise plans) and a separate early-access, self-hosted enterprise layer (merge-gate verification, graph-aware review) are commercial. — [Graphify FAQ](https://graphify.com/faq)
- **Languages:** The FAQ says 36 languages via bundled tree-sitter grammars, among them Python, TypeScript, JavaScript, Go, Rust, Java, C, C++, C#, Ruby, Kotlin, Swift, PHP and Terraform (HCL). The README says "~40 programming languages". — [FAQ](https://graphify.com/faq); [GitHub](https://github.com/Graphify-Labs/graphify). Third-party write-ups give other counts (20+, 36). — [search aggregate: braindetox](https://braindetox.kr/en/posts/graphify_codebase_knowledge_graph_2026.html)
- **How the graph is built:**
  - **Code:** tree-sitter AST, "deterministic, no LLM, nothing leaves your machine".
  - **Docs, PDFs, images, video:** a semantic pass using the assistant's model or a configured API key; headless runs need a provider key or local Ollama.
  - **Edges:** tagged `EXTRACTED` or `INFERRED`; relation types include `calls`, `imports`, `inherits` and `mixes_in`.
  - **Communities:** Leiden clustering.
  - **Outputs:** `graph.html`, `GRAPH_REPORT.md`, `graph.json`.
  — [GitHub](https://github.com/Graphify-Labs/graphify)
- **Incremental update:** `/graphify . --update` re-scans only what changed and patches those nodes and edges. — [FAQ](https://graphify.com/faq)
- **Query interface:**
  - **CLI:** `graphify query`, `graphify path "A" "B"`, `graphify explain`.
  - **Install:** `graphify install` sets up the skill and hooks.
  - **MCP:** an optional `mcp` extra gives an MCP stdio server. The PyPI package is `graphifyy` (double y); other `graphify*` packages on PyPI are unrelated.
  — [GitHub](https://github.com/Graphify-Labs/graphify); [FAQ](https://graphify.com/faq)
- **Local MCP tools (10):**
  - Graph exploration: `query_graph` (BFS/DFS, depth 1-6, `token_budget` default 2000), `get_node`, `get_neighbors`, `get_community`, `god_nodes`, `graph_stats`, `shortest_path`.
  - PR tools that need GitHub context: `list_prs`, `get_pr_impact` (changed files, affected communities, nodes touched), `triage_prs` (merge order, conflict risk).
  - No tool is named for dependents or tests.
  - Server runs over stdio, or streamable HTTP with an optional API key.
  — [Graphify MCP tool reference](https://docs.graphify.com/reference/mcp-tools); [search summary of graphify.com/mcp](https://graphify.com/mcp)
- **Conflicting tool count:** The FAQ says 8 MCP tools, while the MCP page lists 10. — [search result summary](https://graphify.com/faq)
- **Impact analysis claim:** The FAQ says the graph answers "what breaks if I change this" through multi-hop blast-radius reasoning. The README itself doesn't claim a reverse-dependency or blast-radius report; it offers `path` and `explain` for manual tracing. — [FAQ](https://graphify.com/faq); [GitHub](https://github.com/Graphify-Labs/graphify)
- **Windows:**
  - The README says it works on macOS, Linux and Windows, with a Windows install path (`graphify install --platform windows`).
  - PowerShell quoting is a known install friction point.
  - Release 0.9.91 (14 Oct 2026) included "a Windows MCP hang fix".
  — [GitHub](https://github.com/Graphify-Labs/graphify); [Releases](https://github.com/Graphify-Labs/graphify/releases)
- **Maintenance health:**
  - Latest release 0.9.91 (14 Oct 2026).
  - Ten releases (0.9.82 to 0.9.91) between 9 and 14 Oct 2026, roughly two a day, each batching 8-20 community PRs.
  - 2,392 commits on the `v8` branch; about 127.2k stars and 12.3k forks shown.
  — [Releases](https://github.com/Graphify-Labs/graphify/releases); [GitHub](https://github.com/Graphify-Labs/graphify)
- **Stated limits:**
  - Non-code files need an LLM or API key.
  - Ambiguous extensions (`.inc`, `.h`, `.m`) can parse as the wrong language unless declared in `.graphifyrc`, and this "fails silently", producing a sparse graph.
  - Install can hit PATH and "command not found" problems.
  - Benchmarks (e.g. 0.497 recall@10 on LOCOMO) are self-reported.
  - Much of the README promotes the paid cloud ("cut agent token spend by 80%" is a marketing claim).
  — [GitHub](https://github.com/Graphify-Labs/graphify)
- **Scale:** No hard size limit is stated. Indexing time scales with code volume, and large monorepos take longer. — [FAQ](https://graphify.com/faq)
- **Third-party wrapper:** `yasinyaman/graphify-mcp` is a separate project that adds token-budgeted queries and git freshness checks. It is not needed, since Graphify ships its own MCP server. — [graphify-mcp](https://github.com/yasinyaman/graphify-mcp)

### Inferences
- Graphify fits as a repo-orientation map for the Design agent: god nodes, communities, and code linked to docs and ADRs. It does not provide the "affected modules, tests, interfaces" list the Review and QA agents need to check against. Its impact model is community and PR-level, not "callers of symbol X up to depth N, plus tests".
- At pre-1.0 with two releases a day, the `graph.json` schema and MCP tool names may change. If adopted, pin the version and keep it behind an adapter.
- The silent wrong-language parse is a correctness risk for a tool whose output is used as a verification checklist.

### Gaps
- I found no published test-to-code mapping in Graphify, and no evaluation of the accuracy of its INFERRED edges.
- No Graphify benchmark for large monorepos (indexing time or memory) was found.
- The exact current language list sits behind a link I didn't fetch; counts conflict (36 vs ~40).
- I did not check the open-issue count or bus factor.

---

## Q2. Alternatives for the code index / dependency graph: comparison

### Takeaway
By late 2026 there is a crowded field of local MCP "code graph" servers. Most are syntactic: tree-sitter, sometimes with lightweight type resolution. The two strongest permissively licensed (MIT) options for impact analysis are:
- **code-review-graph:** a pip-installable Python package with SQLite storage, a blast radius that includes tests, and incremental updates.
- **codebase-memory-mcp:** a single C binary with 162 languages, a Windows binary, very fast indexing, git-diff impact, and partial LSP-style type resolution.

GitNexus is the most feature-rich for impact work but uses **PolyForm Noncommercial**, which is not OSI-approved, so it is excluded. Serena gives semantic, LSP-precise references but its application is **GPL-3.0-or-later**.

### Cited Findings

**Main comparison table**

| Tool | Licence (exact) | Languages | Accuracy approach | Incremental | Query interface / MCP | Impact & tests | Windows | Maintenance / popularity | Scale notes |
|---|---|---|---|---|---|---|---|---|---|
| **Graphify** | Apache-2.0 (plus MIT file) | ~36-40 | tree-sitter (syntactic); LLM pass for docs | `--update` | CLI, skill, MCP stdio/HTTP (10 tools) | PR-level impact by community; no tests tool | Yes (stated) | 0.9.91, ~2 releases/day; 127k stars shown | No stated limit |
| **codebase-memory-mcp** (DeusData) | MIT | 162 (tree-sitter); "Hybrid LSP" type resolution for 13 incl. Python, TS, JS, Java | tree-sitter plus lightweight C re-implementation of type resolution | Background watcher per MCP session (git repos) | MCP, 17 tools incl. `detect_changes`, `trace_path`, Cypher-like `query_graph`, `search_graph`, `get_architecture`, `manage_adr` | `detect_changes` maps git diff to affected symbols with risk class; "related tests" in UI only | Prebuilt windows-amd64 binary and PowerShell installer; Defender/SmartScreen false positives | v0.11.0; 46.7k stars | Linux kernel (28M LOC) about 3 min, 4.81M nodes; Django about 6 s (M3 Pro, self-published) |
| **code-review-graph** | MIT | Python, JS/TS/TSX, Go, Rust, Java, C/C++, C#, Ruby, Kotlin, Swift, PHP, plus Terraform, Vue/Svelte, notebooks; extensible via `languages.toml` | tree-sitter AST; SQLite | SHA-256 diff re-parse; 2-file edit on django about 2.5 s; hooks and watch mode | MCP, 30 tools, incl. `get_impact_radius_tool`, `get_affected_flows_tool`, `detect_changes_tool`, `get_knowledge_gaps_tool`; PyPI `code-review-graph`, Python 3.10+ | Blast radius traces "every caller, dependent and test" (default depth 2, cap 500); test-coverage edges | Troubleshooting entry only (Windows/WSL: point at .exe, `PYTHONUTF8=1`) | 32.2k stars; workflow pins v2.3.9 | Says impact is conservative, with false positives on large graphs |
| **GitNexus** | **PolyForm Noncommercial 1.0.0 (non-OSI)**; commercial use needs a paid enterprise tier | 16: TS, JS, Python, Java, Kotlin, C#, Go, Rust, PHP, Ruby, Swift, C, C++, ObjC, Dart, Zig | tree-sitter (native and WASM); LadybugDB | `--watch` debounced incremental | MCP, 19 tools incl. `impact`, `detect_changes`, `api_impact`, `rename`, `cypher`, `trace` | Upstream callers by depth with confidence; cross-repo contracts | Windows setup documented | 47.9k stars; v1.7.0 (Apr 2026) | Memory-bound indexing; web UI about 5k files |
| **CodeGraphContext** | MIT | 23-25 incl. Python, TS, Java, Go, Rust | tree-sitter (`tree-sitter-language-pack`); optional SCIP indexers (`SCIP_INDEXER=true`) | `cgc watch` | MCP (tool list not published in README) | Caller lookups and "impact analysis" described; no named dependents tool | KuzuDB/LadybugDB work on Windows natively; FalkorDB Lite is Unix-only | 4.3k stars; v0.5.1 | Graph DB backends incl. Neo4j |
| **code-graph-rag** (vitali87) | MIT | Full: Python, TS/TSX, JS, Rust, Go, Java, C, C++, C#, PHP, Lua, Dart, Julia; structural-only for others | tree-sitter, plus opt-in compiler frontends (libclang, go/types, Roslyn, javac, Jedi), plus runtime traces | Not stated | MCP server; NL→Cypher uses an LLM | Not explicit | Pure Python 3.12+, needs ripgrep; Windows not stated | 5.3k stars | Default SQLite; Memgraph/Neo4j opt-in |
| **Serena** (oraios) | **GPL-3.0-or-later** (app); SolidLSP component MIT | 40+ via LSP; JetBrains backend (paid plugin) | Semantic (real language servers) | Live (LSP) | MCP: find symbol, find referencing symbols, symbol overview, rename, replace symbol body; built-in memory | References are precise, but no persistent graph or blast-radius report | Not stated on README | 30.3k stars; Serena v2 released | No benchmarks; LSP startup per language |
| **multilspy** (Microsoft) | MIT | 12: Java (JDTLS), Python (jedi), Rust, C#, TS/JS, Go, Dart, Ruby, Kotlin, PHP, C++ | Semantic LSP client library | n/a | Python library (no MCP) | Definitions and references only | Not stated | No GitHub releases; 43 open issues | n/a |
| **Aider repo-map** | Not stated on page | (separate page) | Graph ranking over file dependency graph; key symbols with signatures; `--map-tokens` budget (default 1k) | Per-session | Embedded in Aider | No impact or tests | — | — | Fits a token budget |
| **Potpie** | Apache-2.0 (PyPI badge, directory listing) | — | Code knowledge graph plus agents; needs PostgreSQL, Neo4j, Redis | — | Agents/API | — | — | — | Heavy infrastructure |
| **RepoGraph** (ICLR 2025) | Not found | — | Research plug-in repo graph; boosted SWE-bench systems (about 32.8% average relative improvement per secondary summary) | Offline build | Python script | — | — | Research code | — |
| **CodeQL** | **GitHub CodeQL Terms (non-OSI)**: OSI-licensed open-source codebases only; no CI on non-GitHub projects; commercial use needs GitHub Advanced Security | Many | Semantic database queries | — | CLI | Possible via custom queries | Yes | Active | Excluded on licence |
| **Semgrep CE** | Engine **LGPL-2.1**; Semgrep-maintained rules under **Semgrep Rules License v1.0 (non-OSI, internal business use only)** | Many | Pattern / syntactic | — | CLI | Not a dependency graph | Yes | Active | — |
| **mcp-codebase-index** | **AGPL-3.0** plus commercial | Python AST; others regex | — | git-diff | MCP (18) | `get_change_impact` | — | 62 stars | Excluded (copyleft, low adoption) |
| **SocratiCode** | **AGPL-3.0** plus commercial | 18+ | Hybrid semantic + BM25; Docker | — | MCP | Symbol-level impact | — | 3.3k stars | Excluded (copyleft) |

Sources for the table:
- Graphify: [GitHub](https://github.com/Graphify-Labs/graphify), [FAQ](https://graphify.com/faq), [MCP tools](https://docs.graphify.com/reference/mcp-tools), [Releases](https://github.com/Graphify-Labs/graphify/releases)
- codebase-memory-mcp: [GitHub DeusData](https://github.com/DeusData/codebase-memory-mcp)
- code-review-graph: [GitHub tirth8205](https://github.com/tirth8205/code-review-graph)
- GitNexus: [GitHub abhigyanpatwari/GitNexus](https://github.com/abhigyanpatwari/GitNexus)
- CodeGraphContext: [GitHub](https://github.com/CodeGraphContext/CodeGraphContext)
- code-graph-rag: [GitHub vitali87](https://github.com/vitali87/code-graph-rag)
- Serena: [GitHub oraios/serena](https://github.com/oraios/serena)
- multilspy: [GitHub microsoft/multilspy](https://github.com/microsoft/multilspy)
- Aider repo-map: [Aider docs](https://aider.chat/docs/repomap.html)
- Potpie: [PyPI](https://pypi.org/project/potpie/), [dev.co listing](https://dev.co/ai/frameworks/potpie)
- RepoGraph: [arXiv 2410.14684](https://arxiv.org/pdf/2410.14684), [ICLR 2025](https://proceedings.iclr.cc/paper_files/paper/2025/hash/4a4a3c197deac042461c677219efd36c-Abstract-Conference.html)
- CodeQL: [CodeQL licence page](https://securitylab.github.com/tools/codeql/license), [ScanCode licence DB entry](https://scancode-licensedb.aboutcode.org/github-codeql-terms-2020.html)
- Semgrep: [Semgrep FAQ](https://semgrep.dev/docs/faq/overview), [Semgrep blog 2024](https://semgrep.dev/blog/2024/important-updates-to-semgrep-oss)
- mcp-codebase-index and SocratiCode: [ChatForest review, updated 24 Aug 2026](https://chatforest.com/reviews/code-intelligence-codebase-graph-mcp-servers/)

**More cited details**
- **codebase-memory-mcp:**
  - Storage is local SQLite under `~/.cache/codebase-memory-mcp/`, with an optional team snapshot at `.codebase-memory/graph.db.zst`.
  - Its LSP-style resolution is "a lightweight C implementation of language type-resolution algorithms", "structurally inspired by" language servers, not the servers themselves.
  - It warns that dead-code results need checking for dynamic dispatch.
  - CLI-indexed repos are not auto-watched.
  — [GitHub](https://github.com/DeusData/codebase-memory-mcp)
- **codebase-memory-mcp, independent-ish evaluation:** A 31-repository preprint reports 83% answer quality for the graph agent vs 92% for a file-exploration agent, at 10x fewer tokens and 2.1x fewer tool calls. — [arXiv 2603.27277](https://arxiv.org/html/2603.27277v1)
- **code-review-graph, own caveats:**
  - Its impact "recall 1.0" is circular, because ground truth comes from the same graph.
  - Flow detection is strongest for Python and PHP/Laravel, weaker for JS and Go.
  - Graph context can exceed a plain file read for trivial edits.
  — [GitHub](https://github.com/tirth8205/code-review-graph)
- **GitNexus:**
  - `gitnexus serve` has no authentication.
  - Kotlin is unavailable if no prebuild matches the platform.
  - npm 11.x `npx` install crash (issue #1939).
  — [GitHub](https://github.com/abhigyanpatwari/GitNexus)
- **ChatForest review (Apr 2026, updated Aug 2026):**
  - Lists code-review-graph as having impact analysis covering "callers, dependents, tests".
  - Calls code-graph-mcp (eas4ai) dormant (last PyPI July 2025).
  - Says Claude Context needs OpenAI plus Zilliz Cloud, the only major one with cloud dependencies.
  — [ChatForest](https://chatforest.com/reviews/code-intelligence-codebase-graph-mcp-servers/)
- **tree-sitter-language-pack (building block):** MIT; pre-compiled grammars for 306+ languages (v1.13) or 371 (v1.14); counts are inconsistent across listings. — [docs.rs 1.14.2](https://docs.rs/crate/tree-sitter-language-pack/1.14.2); [openapps listing](https://openapps.pro/packages/tree-sitter-language-pack)
- **SCIP indexers:** Sourcegraph docs list scip-typescript, scip-java (Java, Kotlin, Scala) and scip-python as "Generally available". Sourcegraph also notes scip-java "is nearly useless without built-in support for build systems such as gradle", meaning precise Java indexing needs a working build. — [Sourcegraph precise code navigation docs](https://sourcegraph.com/docs/code-search/code-navigation/precise_code_navigation); [writing an indexer](https://sourcegraph.com/docs/code-navigation/writing-an-indexer)
- **scip-python:** Built on the Pyright type checker with "very few changes". — [Sourcegraph blog](https://sourcegraph.com/blog/scip-python)
- **Kythe:** Apache-2.0 per a mirror listing. — [Gitee mirror](https://gitee.com/mirrors/Kythe)

### Inferences
- **Syntactic vs semantic:** Every tool that gives a persistent graph plus blast radius (Graphify, code-review-graph, codebase-memory-mcp, GitNexus, CGC) is tree-sitter-based. Cross-file call resolution is therefore heuristic (name matching, import following), and dynamic dispatch, DI, reflection and Spring/Java interface wiring will be under- or over-approximated. Semantic precision needs LSP (Serena, multilspy) or SCIP, and SCIP for Java and TypeScript needs a working build or tsconfig. The framework should present graph impact lists as "candidate" sets that Review and QA agents confirm on demand (grep plus LSP references), not as ground truth.
- **Python fit:** code-review-graph is the best match for a Python framework that needs a test-aware blast radius. It is pip-installable, SQLite-based, MIT, explicitly maps tests, and has impact radius and affected-flows tools. Its weak point is unverified Windows polish.
- **Robustness and breadth:** codebase-memory-mcp (MIT single binary) is the strongest on cross-platform robustness, speed and breadth (Linux-kernel scale, official Windows binary). Its weak points are that test mapping isn't a first-class MCP tool and that it's a C binary rather than a Python library (call via MCP or subprocess).
- **Licence exclusions:**
  - GitNexus is unusable for a framework that may be used commercially.
  - CodeQL is restricted by its terms.
  - mcp-codebase-index and SocratiCode are AGPL.
  - Serena's GPL-3.0 application can arguably be run as a separate, unmodified MCP-server process without the framework becoming a derivative work. That is a legal judgement to confirm, not a conclusion of this research. multilspy (MIT), or Serena's MIT SolidLSP component, avoids the issue for an in-process LSP layer.
- **Aider repo-map:** The technique (tree-sitter tags plus graph ranking under a token budget) is worth copying for a cheap "repo summary" prompt section. It isn't an impact engine.

### Gaps
- Exact licences for Aider, scip-python, scip-typescript, scip-java and RepoGraph were not verified from their LICENSE files in this session. I believe the Sourcegraph SCIP indexers and Aider are Apache-2.0, but that is unverified, so check before relying on it.
- Kythe's current maintenance status is unknown.
- I did not research per-language dependency tools (pydeps, madge, dependency-cruiser, jdeps, import-linter): no sources were collected. They are well known, but their licences and maintenance are not verified here.
- No independent head-to-head accuracy benchmark of these graph tools on impact analysis (precision/recall of affected modules and tests against real change history) was found. All benchmarks are self-published or circular, except the codebase-memory-mcp preprint, which measures Q&A quality, not impact recall.
- Windows behaviour of code-review-graph and CodeGraphContext beyond README notes was not verified.
- Potpie and RepoGraph language coverage was not established.

---

## Q3. Test-impact analysis tools (which tests cover Y)

### Takeaway
For Python, pytest-testmon (MIT) gives dynamic, coverage-based test selection: it records which code each test executes and re-runs only affected tests. It is more accurate than static graph edges, but only for code the tests actually execute, and only after a baseline run. Static graph tools (code-review-graph test edges, codebase-memory-mcp "related tests") give a pre-run estimate that works across languages.

### Cited Findings
- pytest-testmon is MIT-licensed (PyPI metadata). It selects and re-executes only tests affected by recent changes, using Coverage.py to collect dependencies between tests and executed code. Its database is updated each run and is independent of version control. It always re-runs tests that failed last time. — [PyPI pytest-testmon](https://pypi.org/project/pytest-testmon/)
- Version information conflicts: one third-party page lists v2.2.0 (2025-12-01), while PyPI showed 2.1.3. — [dev.co listing](https://dev.co/testing/open-source/pytest-testmon); [PyPI](https://pypi.org/project/pytest-testmon/)
- code-review-graph stores test-coverage edges. Its `detect_changes_tool` reports test gaps and `get_knowledge_gaps_tool` flags untested hotspots. It reports "a tested caller found through the graph" as a note, not as coverage. — [GitHub](https://github.com/tirth8205/code-review-graph)
- codebase-memory-mcp shows "related tests" for a symbol in its Lens UI, but no dedicated test MCP tool is described. — [GitHub](https://github.com/DeusData/codebase-memory-mcp)

### Inferences
- Good layering for the QA agent: a static graph estimate at design time ("tests likely affected"), then dynamic confirmation with pytest-testmon for Python. For TS/JS, Jest and Vitest have `--findRelatedTests` / `--changed`-style modes; for Java, Gradle/Maven test-impact plugins exist. Neither was researched here, so treat this as a pointer only.

### Gaps
- No sources were collected for TS/Java test-impact tools (Jest `--findRelatedTests`, Vitest `--changed`, Gradle/Maven TIA plugins, OpenClover, Launchable-style services). That needs follow-up research.

---

## Q4. Repo memory v1: AGENTS.md / CLAUDE.md convention status and adoption

### Takeaway
AGENTS.md is the de facto cross-tool standard:
- It is stewarded by the Linux Foundation's Agentic AI Foundation (AAIF).
- It is used by more than 60k open-source projects.
- It is supported by Codex, Cursor, Copilot coding agent, Gemini CLI, Aider, Jules, Devin, Windsurf, Zed, Junie and others.
- Claude Code reads it natively since v2.1.277, when no CLAUDE.md is present.

It is informal Markdown with no required fields, and the nearest file in the directory tree takes precedence. It fits a human-reviewable repo knowledge file well.

### Cited Findings
- **Format:** AGENTS.md is "a README for agents", an open, Markdown-based format with no required fields and any headings allowed. — [agents.md](https://agents.md/)
- **Recommended sections:** project overview, build and test commands, code style, testing instructions, security considerations, plus PR guidelines and deployment steps. — [agents.md](https://agents.md/)
- **Nesting:** Agents read the nearest AGENTS.md in the directory tree, the closest file wins, and explicit user prompts override all files. OpenAI's main repo had 88 AGENTS.md files. — [agents.md](https://agents.md/)
- **Governance and adoption:**
  - Stewarded by the Agentic AI Foundation under the Linux Foundation.
  - "Over 60k open-source projects", with examples apache/airflow, openai/codex and temporalio/sdk-java.
  — [agents.md](https://agents.md/)
- **AAIF founding:** Founded December 2025 by OpenAI, Anthropic and Block, with support from Google, Microsoft, AWS, Bloomberg and Cloudflare. AGENTS.md is a founding project. — [OpenAI announcement](https://openai.com/index/agentic-ai-foundation/); [SiliconANGLE](https://siliconangle.com/2025/12/09/linux-foundation-announces-agentic-ai-foundation-joined-anthropic-openai-block/); [InfoQ](https://infoq.com/news/2025/12/agentic-ai-foundation/)
- **Supporting tools listed:** Codex, Jules, Factory, Aider, goose, opencode, Zed, Warp, VS Code, Devin, UiPath, Junie, Amp, Cursor, RooCode, Gemini CLI, Kilo Code, Phoenix, Semgrep, GitHub Copilot coding agent, Ona, Windsurf, Augment Code. — [agents.md](https://agents.md/)
- **Claude Code support:**
  - Claude Code "can read AGENTS.md as your project instructions". By default it reads AGENTS.md only when there is no CLAUDE.md, `.claude/CLAUDE.md` or `CLAUDE.local.md` in the working directory or above.
  - A setting `claude-md-and-agents-md` loads both.
  - `@path` imports work inside AGENTS.md.
  - Requires Claude Code v2.1.277 or later.
  - Fallback for older or restricted sessions: put `@AGENTS.md` in CLAUDE.md.
  — [Claude Code memory docs](https://code.claude.com/docs/en/memory)
- **Conflicting older sources:** Some say Claude Code ignores AGENTS.md and recommend the `@AGENTS.md` import or a symlink. Symlinks on Windows need Admin rights or Developer Mode. These sources predate or contradict the current official docs. — [makandra card](https://makandracards.com/makandra/626671-claude-code-read-agents-md?page=23); [travis.media](https://travis.media/blog/claude-md-import-agents-md); superseded by [official docs](https://code.claude.com/docs/en/memory)
- **Built-in memory in other tools:** Claude Code also has auto memory, notes Claude writes itself (first 200 lines or 25KB loaded), alongside CLAUDE.md. — [Claude Code memory docs](https://code.claude.com/docs/en/memory). Serena has a built-in memory system that can be disabled. — [Serena GitHub](https://github.com/oraios/serena). codebase-memory-mcp has a `manage_adr` tool for architecture decision records stored with the graph. — [GitHub](https://github.com/DeusData/codebase-memory-mcp)

### Inferences
- Use a root `AGENTS.md` as the canonical, human-reviewed repo knowledge file, with optional nested AGENTS.md per package for monorepos. Add a 1-line `CLAUDE.md` containing `@AGENTS.md` only when CLAUDE.md is also needed. That gives maximum tool interoperability at zero cost.
- Keep machine-proposed learnings in a separate, reviewable section or file (e.g. `docs/agent-learnings.md`, imported from AGENTS.md) and update it via PRs. That keeps the "human-reviewable" property and gives a later vector store a curated seed corpus.
- Avoid symlinks for the Windows target; use imports.

### Gaps
- No independent count of AGENTS.md adoption was found; 60k is the project's own figure, repeated by press.
- There is no formal schema or versioned spec. If the framework needs structured sections (e.g. "module ownership", "known dependency hotspots"), it has to define its own heading convention.

---

## Q5. Learned memory stores (later option): licence and Qdrant fit

### Takeaway
**Mem0 (Apache-2.0)** fits the existing Qdrant stack best: Qdrant is its default vector store in both Python and TypeScript. Cognee (Apache-2.0) supports Qdrant through a community-maintained adapter. Graphiti (Apache-2.0) is graph-database-centric (Neo4j or FalkorDB), not Qdrant. Letta (Apache-2.0) is a full agent runtime, heavier than needed. LangMem (MIT) is tied to LangGraph's BaseStore. All of them except Cognee's local mode need an LLM for memory extraction.

### Cited Findings

| Tool | Licence | Qdrant | Model | LLM needed | MCP | Popularity |
|---|---|---|---|---|---|---|
| **Mem0** | Apache-2.0 | **Default** vector store (Python and TS); 20+ others | Memory layer added to an existing agent; LLM fact extraction (one call per add); entity linking | Yes (default gpt-5-mini; any provider) | Not on README | 67.3k stars |
| **Cognee** | Apache-2.0 | Community adapter `cognee-community-vector-adapter-qdrant` (local and cloud) | Graph + vector memory from documents and code ("graph of symbols and dependencies") | No for basic local mode (GLiNER plus local embeddings); yes for generated answers | Yes (`cognee-mcp`) | 32.4k stars |
| **Graphiti** (Zep) | Apache-2.0 | Not mentioned; backends are Neo4j, FalkorDB, Neptune, Kuzu (deprecated) | Temporal knowledge graph | Yes (OpenAI default; Ollama and other OpenAI-compatible endpoints supported) | Yes (`mcp_server`) | 31.8k stars |
| **Letta** | Apache-2.0 | Not mentioned | Agent runtime / app server with self-managed memory | Yes | Not stated | 25.2k stars |
| **LangMem** | MIT | Via LangGraph store only (Postgres recommended) | Memory primitives and tools on LangGraph BaseStore | Yes | No | 1.7k stars |

Sources for the table:
- Mem0: [Mem0 vector DB docs](https://docs.mem0.ai/components/vectordbs/overview), [GitHub mem0ai/mem0](https://github.com/mem0ai/mem0)
- Cognee: [GitHub topoteretes/cognee](https://github.com/topoteretes/cognee), [Cognee Qdrant docs](https://docs.cognee.ai/setup-configuration/community-maintained/qdrant), [cognee-community](https://github.com/topoteretes/cognee-community)
- Graphiti: [GitHub getzep/graphiti](https://github.com/getzep/graphiti)
- Letta: [GitHub letta-ai/letta](https://github.com/letta-ai/letta)
- LangMem: [GitHub langchain-ai/langmem](https://github.com/langchain-ai/langmem)

**More cited details**
- **Mem0:**
  - Docs: "Qdrant will be used as the vector database" when nothing is configured. — [Mem0 docs](https://docs.mem0.ai/components/vectordbs/overview)
  - Self-hosted via Docker Compose, with auth on by default. — [GitHub](https://github.com/mem0ai/mem0)
  - Licence conflict in secondary sources: one guide calls it MIT, another Apache-2.0. The GitHub page shows Apache 2.0. — [contextstudios guide](https://www.contextstudios.ai/guides/best-ai-agent-memory-tools-2026); [GitHub](https://github.com/mem0ai/mem0)
- **Cognee:** Using Postgres as a graph store is a demo feature, with the production version sold as a licensed product. Every vector adapter except LanceDB lives in the cognee-community repo. — [GitHub](https://github.com/topoteretes/cognee); [Cognee Qdrant docs](https://docs.cognee.ai/setup-configuration/community-maintained/qdrant)
- **Graphiti:** Kuzu is deprecated because upstream is unmaintained; Neo4j or FalkorDB are recommended. — [GitHub](https://github.com/getzep/graphiti)
- **Zep self-hosting:** One source says Zep retired its self-hosted Community Edition while Graphiti stays open source. — [techsy.io](https://techsy.io/en/blog/best-ai-agent-memory-tools). The Graphiti README describes Zep only as a managed platform. — [GitHub](https://github.com/getzep/graphiti)
- **Architecture framing:** Mem0 is a memory layer you add, Letta a runtime where the agent manages tiered memory, Zep/Graphiti temporal knowledge graphs, and Cognee graph memory from documents. — [mcp.directory comparison](https://mcp.directory/blog/mem0-vs-letta-vs-zep-vs-cognee-2026)

### Inferences
- For the "learned memory on Qdrant" phase, Mem0 OSS can point at the existing TokenLean Qdrant instance with minimal glue, and its Apache-2.0 licence is clean. Use per-repo namespaces (user_id/agent_id/run_id scoping, or a collection per repo) so memories don't leak across repos.
- Cognee is the alternative if code-aware graph memory is wanted on the same Qdrant, but its Qdrant adapter is community-maintained, which is a maintenance risk.
- Graphiti would add a Neo4j or FalkorDB dependency, and Letta would replace the agent runtime. Neither fits a "plug into existing Qdrant" plan.
- Whatever the store, promote high-value learned memories into AGENTS.md via human-reviewed PRs, so the reviewable file stays the source of truth.

### Gaps
- I didn't verify whether Mem0 OSS still ships the OpenMemory MCP server (not on the current README).
- Mem0's graph-memory backends weren't confirmed.
- Letta's storage (Postgres/pgvector) and MCP support weren't confirmed from the current README, since the code moved to `letta-ai/letta-code`.
- LangMem's recent commit activity wasn't visible.
- No benchmark of these stores for code-repo knowledge (as opposed to chat-personalisation benchmarks like LOCOMO) was found.

---

## Q6. Recommendation (primary plus fallback)

### Takeaway
Define a thin `CodeIndex` port in the framework, with tools such as `dependents(symbol, depth)`, `impact(diff|files)`, `tests_for(symbol|file)`, `path(a,b)` and `overview()`, and back it with MIT-licensed tools:
- **Primary:** code-review-graph for a test-aware blast radius (Python, TS, Java).
- **Fallback, or an alternative backend for large, polyglot or Windows-heavy repos:** codebase-memory-mcp.
- **Semantic confirmation:** multilspy (MIT) or Serena (GPL app, run as a separate MCP process).
- **Dynamic test confirmation:** pytest-testmon for Python.
- **Graphify:** treat it as an optional orientation and doc-linking graph, not the impact source of truth.
- **Repo memory:** AGENTS.md now; Mem0 on the existing Qdrant later.

### Cited Findings
- code-review-graph:
  - MIT, `pip install code-review-graph`, Python 3.10+, single SQLite file.
  - `get_impact_radius_tool` covers "every caller, dependent and test"; there is also `get_affected_flows_tool`.
  - Incremental SHA-256 re-parse.
  - Its own caveats: conservative impact with false positives, and circular recall evaluation.
  — [GitHub](https://github.com/tirth8205/code-review-graph)
- codebase-memory-mcp:
  - MIT, 162 languages, Hybrid type resolution covering Python, TS, JS and Java.
  - `detect_changes` with risk classification.
  - Prebuilt Windows binary.
  - Linux kernel indexed in about 3 minutes (self-published).
  — [GitHub](https://github.com/DeusData/codebase-memory-mcp)
- Graphify: Apache-2.0, impact only via PR-level tools (`get_pr_impact` etc.), pre-1.0 with about 2 releases a day. — [MCP tools](https://docs.graphify.com/reference/mcp-tools); [Releases](https://github.com/Graphify-Labs/graphify/releases)
- GitNexus is PolyForm Noncommercial. CodeQL is limited to OSI open-source codebases without GHAS. Serena's application is GPL-3.0-or-later. — [GitNexus](https://github.com/abhigyanpatwari/GitNexus); [CodeQL licence](https://securitylab.github.com/tools/codeql/license); [Serena](https://github.com/oraios/serena)
- Graph-based exploration used 10x fewer tokens but scored 83% vs 92% answer quality against file-by-file exploration. That supports keeping on-demand exploration as the default, with the graph as an accelerator. — [arXiv 2603.27277](https://arxiv.org/html/2603.27277v1)
- Mem0 uses Qdrant by default and is Apache-2.0. — [Mem0 docs](https://docs.mem0.ai/components/vectordbs/overview); [GitHub](https://github.com/mem0ai/mem0)

### Inferences
- **Why code-review-graph is primary:**
  - It is the only permissive, Python-native option whose blast radius explicitly includes tests, which is exactly the "impact list" contract the Review and QA agents check against.
  - It is embeddable as a library or MCP server.
  - Its weaknesses are Windows polish and conservative over-approximation. Mitigate both by having the Design agent's output say "candidates" and having agents confirm with grep/LSP.
- **Why codebase-memory-mcp is the fallback:**
  - It has the best breadth, speed and official Windows binary.
  - It is MIT.
  - Its git-diff impact covers Review-agent needs.
  - Test mapping is weaker. Pair it with pytest-testmon (Python) and naming/import heuristics (`test_*.py`, `*.spec.ts`, `*Test.java`).
- **Graphify:** Adopt only as an optional add-on, for the community/god-node repo overview and code-to-doc links useful to the Design agent. Pin the version. It does not provide symbol-level dependents or tests coverage today, and its churn rate and silent-misparse failure mode argue against making it the verification backbone. Revisit when it reaches 1.0 and if it adds a symbol-level impact tool.
- **Precision layer:**
  - For Java interface/DI-heavy code and TS type-driven call sites, tree-sitter graphs will miss edges.
  - Expose an on-demand `find_references` tool backed by multilspy (MIT, Python library: JDTLS, TS server, jedi), or by Serena run as an external MCP server if its GPL is acceptable for the distribution model.
  - Optional SCIP indexing is a v2 option where a project builds cleanly; CodeGraphContext already shows the tree-sitter-plus-optional-SCIP pattern.
- **Languages outside v1:** These fall back to on-demand exploration. Both recommended backends already parse many more languages syntactically, so "unsupported" can mean "graph available but flagged low-confidence".
- **Memory:**
  - v1: root (plus nested) AGENTS.md, edited through PRs, with a `CLAUDE.md` containing `@AGENTS.md` only if needed.
  - v2: Mem0 OSS on the existing Qdrant (per-repo namespace), with periodic human-reviewed promotion of learnings into AGENTS.md.
  - Alternative: Cognee, if graph-shaped code memory is wanted.

### Gaps
- No hands-on validation was done: the recommendation rests on documentation and self-published benchmarks. Before committing, run a bake-off of code-review-graph vs codebase-memory-mcp (vs Graphify) on 2-3 real repos (Python, TS, Java) on Windows and Linux. Measure impact recall/precision against historical commits (files actually co-changed or tests actually failing) and index and update times.
- The legal position on running GPL Serena as a separate process should be confirmed by counsel or policy owners.
