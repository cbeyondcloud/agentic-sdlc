# S5 — Code-graph bake-off: the default symbol-level adapter

**Date:** 2026-10-20 · **Code (throwaway):** `spikes/s5_code_graph/` (`corpus.py`, `run_*.py`, `score.py`; outputs in the git-ignored `out/`).

## Question

Which tool answers the code-intelligence questions agents ask (M1-34/35) best on real repos:
- `dependents(file)`;
- `impact(diff)` (blast radius, tests included);
- `tests_for(file)`.

The answer must also fit our constraints: permissive licence, Windows and Linux, no unsigned binaries on developer machines, and no network or telemetry.

## Method: corpus replay

- **Repos:** three permissively licensed open-source repos, each pinned at a commit:
  - **pallets/flask** (Python, BSD-3, 10k non-test LOC);
  - **sindresorhus/ky** (TypeScript, MIT, 5k);
  - **google/gson** (Java, Apache-2.0, 23k).
- **Commits:** for each repo, the 40 most recent non-merge commits changing 2–15 files with at least one non-test source file.
- **Seed and truth:** the seed is the commit's most-changed non-test source file. The truth is the commit's other changed code files, including tests.
- **Index:** each tool's index is built once at the pinned HEAD.
- **Scoring:** recall@20, precision@10, empty-answer share, test-hit (any changed test file among the results), build and query time, and index size.
- **Baseline:** a naive reverse-import scan plus name-matched tests.
- **No unsigned binaries:** codebase-memory-mcp (a 299 MB native binary) ran only in a Linux container with no network. The two Python tools ran natively on Windows in throwaway environments.

| Tool | Pin (release date) | Licence | Ran |
|---|---|---|---|
| code-review-graph | 2.3.9 (2026-09-18) | MIT | Windows, native |
| Graphify (`graphifyy`) | 0.9.79 (2026-10-06) | Apache-2.0 | Windows, native |
| codebase-memory-mcp | v0.11.0 linux-amd64 (2026-09-15) | MIT | Linux Docker, `--network none` |

## Results

Values are means over 40 commits per repo; "all" is the mean over all 120. The rows marked *(composed)* are our adapter compositions, not single tool commands.

| Variant | recall@20 flask / ky / gson / **all** | precision@10 all | test-hit flask / ky / gson / **all** | query time | index size (gson) |
|---|---|---|---|---|---|
| baseline (reverse imports + name-matched tests) | .38 / .15 / .46 / **.33** | .15 | 25 / 18 / 60 / **34%** | <1 ms | — |
| code-review-graph `impact` (raw) | .57 / .37 / .09 / **.34** | .07 | 83 / 78 / 0 / **52%** | 33–126 ms | 87 MB |
| **code-review-graph (composed):** importers, then `tests_for` per symbol, then impact | .79 / .41 / .27 / **.49** | .09 | 100 / 78 / 63 / **78%** | ~2 s | 87 MB |
| Graphify `affected` (file) | .62 / .39 / .14 / **.38** | .11 | 75 / 80 / 66 / **74%** | 1–7 ms | 32 MB |
| Graphify (composed, every symbol) | .66 / .38 / .23 / **.43** | .11 | 75 / 80 / 80 / **79%** | 1–7 ms | 32 MB |
| codebase-memory-mcp (2-hop) | .66 / .29 / .57 / **.50** | .13 | 96 / 45 / 71 / **67%** | 40–450 ms warm MCP; 5.9 s cold CLI in Docker | 28 MB |

**What the numbers support.** The sample is small (13, 8 and 22 distinct seeds; correlated rows), so the 0.49 against 0.50 gap is noise. The large effects are real:
- every graph tool finds far more of the changed tests than the baseline (67–79% against 34%);
- raw code-review-graph `impact` is no better than the baseline on recall (0.34) and blind to Java tests (0% on gson). Its `TESTED_BY` edges to Java same-package tests mostly don't resolve;
- codebase-memory-mcp is clearly best on Java (recall 0.57);
- precision@10 is low for every tool, because truth sets average 2–5 files. The graph answers are **candidates**, as the spec already says (§10.1: `confidence: candidate`; Review and QA verify).

**Other findings:**
- code-review-graph's `impact` result is unordered (it varies with `PYTHONHASHSEED`), so our adapter ranks it.
- code-review-graph writes an empty `~/.code-review-graph/` on build, outside the repo.
- Graphify has no `tests_for`, over-reports on Java (82–86 files per query), skips SQL files without the SQL grammar, and is fast: 1–7 ms per query, 5–10 s to build.
- codebase-memory-mcp needs a container-local filesystem for its cache, and costs about 6 s per cold CLI call through Docker. Its git co-change edges would leak ground truth in a replay like this, so they were excluded.
- No telemetry was found in code-review-graph's source. Graphify's AST-only update makes no LLM call. codebase-memory-mcp indexed and queried with no network.
- code-review-graph's transitive `certifi` (MPL-2.0, unmodified) is already in our lock file through the HTTP stack, so it adds nothing new.

## Decision

1. **The default symbol adapter is code-review-graph 2.3.9,** behind our `codeintel` interface. It is pure Python (no binary), so it runs natively on Windows, macOS and Linux. It is MIT, pip-installable, with no telemetry. The adapter is the **composed** query that scored best:
   - files that import the seed;
   - then `tests_for` each symbol defined in the seed file (qualified names);
   - then the remaining impact files;
   - ranked in that order and de-duplicated.

   About 2 s per call is acceptable for an agent tool call. M1-35 should cut it by opening the store once per call rather than once per symbol.
2. **Fallback: Graphify 0.9.79** (Apache-2.0, pure Python), for languages or repos where code-review-graph returns nothing. Its `affected` gives file-level dependents in milliseconds, but it has no `tests_for`, so the adapter adds name-matched tests.
3. **codebase-memory-mcp is an optional backend for Java/JVM repos,** routed by language in M2 (with M2-43, the domain-graph adapter). It is the most accurate on Java, but it is a large native binary. Under the owner's rule it may only run where it is approved: in a container, or on an org-signed or allow-listed build. It is not a default.
4. **The interface reports answers as candidates.** Every result carries the tool that produced it. The Design handoff marks impact `confidence: candidate`, and Review and QA verify.
5. **Serena (S11) is not needed.** The tree-sitter tools answer file-level impact well enough for candidates, and precise references, if needed, come from multilspy (MIT). S11 stays unrun unless M1-35 finds a gap.

## Not tested here (carried to M1-35)

- **code-review-graph and Graphify on Linux.** They ran natively only on Windows; M1-35's adapter tests run in the Linux check image.
- **Monorepo scale.** The largest repo here is 57k LOC including tests. Index build time (4–10 s) suggests a local build is cheap at this size, which S9 must weigh against committing an index.
- **Incremental update of each tool's index after an edit.** M1-36 covers it.

## Cost if wrong

The adapter sits behind the `codeintel` interface (M1-34). Swapping the default for Graphify or codebase-memory-mcp replaces one module and re-runs this replay harness, which stays in `spikes/s5_code_graph/` as the benchmark for any future candidate.

## Addendum (2026-10-31, M3-27): Java test linking without the binary

A Java Spring Boot team is in the pilot (owner decision 2026-10-31), so Java needed a
`tests_for` that works on every laptop. `agentic.codeintel.jvm.JvmConventions` is pure
Python. It reads Maven and Gradle conventions from file names and each file's package
and imports, in this order:

1. same-package `FooTest`, `FooTests`, `FooIT` and similar names, then the same names in
   another package;
2. same-package tests whose name starts with `Foo`;
3. same-package tests that name `Foo`;
4. tests that import `Foo`.

A JVM repo gets it first, ahead of the routed symbol adapter.

**Replay:** `spikes/s5_code_graph/score_tests_for.py`, on the same 40 gson commits and
pinned head as above. There are no new tool builds and no network calls. code-review-graph
and codebase-memory-mcp are scored from the recorded S5 answers.

- `tests_for`: the 35 commits that change a test; truth is the changed tests.
- `dependents`: all 40 commits; truth is every other changed file.

| Variant | n | recall@20 | precision@10 | hit |
|---|---|---|---|---|
| tests_for: baseline (name-matched) | 35 | 0.44 | 0.51 | 51% |
| tests_for: code-review-graph, file + symbols (**before**) | 35 | 0.52 | 0.07 | 63% |
| tests_for: JvmConventions alone | 35 | 0.55 | 0.20 | 63% |
| tests_for: JvmConventions + code-review-graph (**after**) | 35 | **0.63** | 0.10 | 77% |
| tests_for: codebase-memory-mcp v0.11.0 | 35 | 0.44 | 0.10 | 49% |
| dependents: code-review-graph composed (**before**) | 40 | 0.27 | 0.03 | 70% |
| dependents: JvmConventions + code-review-graph (**after**) | 40 | **0.53** | 0.11 | 82% |
| dependents: codebase-memory-mcp v0.11.0, 2-hop | 40 | 0.57 | 0.12 | 80% |

**What it shows:**
- Without any binary, Java recall nearly doubles for dependents (0.27 to 0.53). That is
  within noise of codebase-memory-mcp (0.57).
- Without any binary, tests_for gains 11 points (0.52 to 0.63) and beats
  codebase-memory-mcp's own tests_for (0.44).
- Precision stays low, as it does for every tool. The answers remain candidates.
- The numbers come from a single library repo. DF-924 repeats the replay on a Spring
  Boot sample.

**codebase-memory-mcp today (checked 2026-10-31).** The current release is v0.12.1. It is
MIT and has builds for Windows x64 and arm64, macOS arm64 and x64, and Linux. Every
archive has a sigstore bundle, and `checksums.txt` matches the published digests.

Its documents disagree about the network. The README says it makes no request of its
own accord. SECURITY.md describes a background update check to `api.github.com` after
MCP `initialize`. v0.12 CLI calls also start or join a shared coordination daemon.

So S5 decision 3 stands, with one change: the backend is routed by language
(`codeintel.adapter: auto`, the new default). It runs only from a build the org pins in
`tool-pins.yaml`. It runs in CLI mode, never as an MCP session, with its home and caches
in the run's data folder. When it is missing, the conventions and code-review-graph
answer, with a warning. Running it with no network at all is DF-921. Checking the v0.12.1
CLI contract and replaying it is DF-920.
