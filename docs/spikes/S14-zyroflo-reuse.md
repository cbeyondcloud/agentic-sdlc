# Spike S14 — How to reuse Zyroflo (sdlc-harness)

| | |
|---|---|
| Date | 2026-10-18 |
| Question | Reuse Zyroflo's ledger, guards, worktree, connectors, graph, pre-pass and lifecycle content by extracting them into a shared package, by vendoring copies, or by depending on Zyroflo directly? |
| Owner direction | Zyroflo is ~99.99 % complete: leverage the code already written, do not reinvent it. |
| Decision | **D34 — depend on Zyroflo directly, pinned to a commit, behind thin adapters in `sdlc_commons`. Gaps are closed by small, backward-compatible PRs to Zyroflo that the owner reviews and merges.** |

## Method

Throwaway project in the scratchpad (never imported by product code):

1. `uv add "sdlc-harness @ git+https://github.com/cbeyondcloud/sdlc-harness@a0c1b14"` on Windows, Python 3.12.
2. Imported every module the plan reuses and printed the signatures of the functions we need.
3. Exercised the guards, the ledger hash, and the events hash chain including a tamper test.
4. Checked which folders ship in the installed wheel.

## Results

| Area | Zyroflo API (unchanged) | Result |
|---|---|---|
| Install | `sdlc-harness` package, import name `tools`, pinned git commit | ✅ installs from the private repo with its dependencies (neo4j driver, tree-sitter + grammars, pyyaml, python-dotenv) |
| Ledger hash | `tools.sdlc.approve.entry_hash(entry)` | ✅ same formula as the plan's global constraint |
| Approval chain | `approve.read_chain / verify_chain / record / gate_state / sign_entry / verify_signatures` | ✅ importable; `record` is coupled to Zyroflo stage/policy names (adapter needed) |
| Events chain | `tools.sdlc.events.append / verify / read / last` | ✅ chain written to `<target>/.sdlc/runs/archive/<run>/events.jsonl`; editing a line is detected and named ("its recorded hash does not match its contents - the line was edited") |
| Digests | `tools.sdlc.state.digest_of / sha256_of / append_record / drift` | ✅ importable |
| Guards | `tools.sdlc.policy.denied_command / destructive / protected / budget / confinement / push_target`, `tools.sdlc.guard.check` | ✅ pure functions over a policy dict; caught `git push -f`, `find . -exec rm -rf {} +` and `sudo kubectl …`; allowed `pytest -q` |
| Worktrees | `tools.sdlc.worktree.plan / create / remove` | ✅ importable; reads components from Zyroflo policy (adapter supplies them) |
| Connectors | `tools.sdlc.connect.dig / spec_for / mapping_for` | ✅ importable |
| Graph | `tools.graph.impact.load_graph / impact / impact_diff` | ✅ importable |
| Review pre-pass | `tools.review.prepass.prepass` | ✅ importable |

## Gaps (each closed by a small, backward-compatible Zyroflo PR)

| ID | Gap | Proposed Zyroflo change | Tracker |
|---|---|---|---|
| Z1 | Event fields are a fixed list; unknown fields are refused (correctly) | Optional namespaced extension block on events (e.g. `ext: {…}`), included in the hash, ignored by Zyroflo's own readers | M1-07 |
| Z2 | Lifecycle content (`sdlc/stages`, `sdlc_templates`) and `third_party/manifest.json` are not in the installed package | Ship them as package data; add a resource accessor | M1-10 |
| Z3 | Run-folder root is fixed at `<target>/.sdlc/runs/` | Optional run-folder root parameter (default unchanged) | M1-10 |
| Z4 | Import name `tools` is very generic (collision risk) | Add an alias package `zyroflo` re-exporting `tools.*`; `tools` keeps working | M2-29 |

Every change keeps Zyroflo's existing behaviour and test suite unchanged for current users.

## Consequences for the plan

- SP2 changes from *extract into a shared package + switch Zyroflo over* to *thin adapters over the pinned Zyroflo dependency + four small Zyroflo PRs*. Estimates drop accordingly.
- `sdlc_commons` (workspace member `commons/`) is now the adapter layer: it is the only place that imports `tools.*`, so a future rename (Z4) touches one package.
- Developer and org installs need access to `sdlc-harness`: private git dependency during development; published to the internal package index alongside `agentic` for org rollout (M3-21).
- Zyroflo's required dependencies (neo4j driver, tree-sitter grammars) come along; all permissive licences.
