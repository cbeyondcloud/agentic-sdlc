# S9 — Commit the code-intelligence index to `main`, or build it locally?

**Date:** 2026-10-20 · **Code (throwaway):** `spikes/s9_committed_index/` (`crg_export.py` exporter/importer, `nightly.py`, `det.py`, `incremental.py`, `roundtrip.py`, `merge.py`; raw results in the git-ignored `out/`) · **Tool:** code-review-graph 2.3.9 (S5's default) · **Platform:** Windows 11, plus a Linux x86 container for cross-OS checks.

## Question

Spec §10.3 has a nightly bot commit the index to `main` at `.agentic/index/`, as binary with `merge=ours`, with optional Git LFS. With code-review-graph as the indexer, what does that cost, does it work, and is a local build the better default?

## Method

- **Repos:** the S5 clones of pallets/flask (86 files, 8.5k edges) and google/gson (264 files, 47.6k edges).
- **Nightly runs:** 20 commit-spaced stand-ins for nightly runs. Each night the index was built from scratch and committed to a scratch repo, then measured after `git gc --aggressive`.
- **Artifacts compared:** two, each repeated for every run:
  - the raw SQLite database;
  - a deterministic sharded text export written for this spike: one sorted JSONL file per source file, absolute paths replaced by `${ROOT}`, volatile columns dropped, and a manifest with sha256 values and the tool version.
- **Also measured:**
  - determinism over 5 rebuilds;
  - rebuilding the database from the export;
  - local full builds and incremental `update` over 5 consecutive commits;
  - merges replayed from 3 real merge commits under each `.gitattributes` setting.

## Results

| | flask raw DB | flask text export | gson raw DB | gson text export |
|---|---|---|---|---|
| Artifact size | 12.7 MB | 3.5 MB (86 shards) | 88.7 MB | 22.1 MB (264 shards) |
| Repo growth per night (after gc) | **330 KB** | **4.8 KB** | **2.74 MB** | **23 KB** |
| Growth on a night with no source change | 122 KB | 0.5 KB | 0.62 MB | 0.5 KB |
| `.git` after 20 nights | 7.6 MB (56% of flask's whole source history) | 0.36 MB | 58 MB (2.8 times gson's source history) | 1.3 MB |
| Byte-identical on rebuild | **no** (timestamp columns) | **yes**, across rebuilds, checkout paths and Windows/Linux | no | yes |
| Usable in another checkout | **no**: absolute paths; the tool refuses ("built with a different repository root") | yes (`${ROOT}` rewritten on import) | no | yes |
| Rebuild time from the artifact | n/a | 0.7–0.9 s (fresh build 3.7–4.0 s) | n/a | 3.3 s (fresh build 6.7 s) |

**Local build and update.**
- Full build: flask 4.0 s at 233 MB peak memory; gson 6.7 s at 360 MB.
- `update` after one commit: median 1.6 s (flask) and 1.7 s (gson), at 40–84 MB.
- A no-op update takes 0.9 s.
- The updated index matched a fresh build at the same commit in 5 of 5 steps on both repos.
- `update` after `git pull` is the cheapest refresh: it starts from the commit stored in the index.

**Merges** (3 real merge commits):
- **Raw database:**
  - With no attributes: a binary conflict.
  - With `binary merge=ours` but no driver configured in the clone: the same conflict. The attribute alone does nothing.
  - With `merge.ours.driver true` configured per clone: a silent merge that keeps one side's index, so it is stale and P2's changes are missing.
  - GitHub's server-side merge is believed to ignore custom drivers; that was not tested.
- **Text export:**
  - With no attributes: conflicts are confined to overlapping shards plus the manifest.
  - With `merge=union`: no conflicts, but stale and missing lines (up to 1,322 and 657 on gson) and an invalid manifest JSON.
  - Even a clean merge of disjoint shards differed from the rebuilt truth, because of edges across files.
  - So a generated index must never be merged: take either side and regenerate.

**LFS.**
- **Raw database:**
  - It costs about 1.5–1.9 KB per edge, so GitHub's 50 MB per-file warning arrives at about 27–37k edges. That is smaller than gson, which is already past it.
  - The 100 MB block arrives at about 54–74k edges.
  - LFS would mean full, non-delta storage of about 32 GB per year for one gson-sized repo, and about 88 MB downloaded per developer per day.
- **Export:** its largest shard is 2 MB and it grows about 8 MB a year, so it never needs LFS.

**Rebuild from the export.**
- **API:** code-review-graph has no public import, so the spike's importer writes nodes and edges back into its schema and then calls a **private** post-processing function. The public CLI leaves two derived tables empty.
- **Fidelity:** equal, apart from list ordering and 5 of 1,447 community labels on flask.
- **Constraint:** the importer must be pinned to the tool and schema version recorded in the manifest.

## Decision (approved by the owner 2026-10-20: D39, spec §10.3 rewritten)

1. **Never commit the raw database.** It is tied to the checkout path, rewritten whole every night, past GitHub's file-size warning on a modest Java repo, and it can't be merged safely.
2. **Default: build and update locally.** On first use `codeintel` runs a full build of 4–7 s on these repos. After each `git pull`, or before a step, it runs `update` (1–3 s). The index lives in the git-ignored `.agentic/local/index/`. Nothing goes on `main`, so there is no bot commit, no branch-protection bypass, no merge driver and no LFS.
3. **Large repos opt in to a published export.** When a repo's full local build is too slow (a policy threshold, proposed at 60 s; that figure is extrapolated, not measured), a nightly CI job builds once and publishes the deterministic sharded export **outside `main`**: a CI artifact or an `agentic-index` ref. Developers import it and run `update`, so they spend seconds instead of a long build, and CI pays the build once instead of 1,000 developers each paying it.
4. **If an org still requires a committed index,** it is the sharded export only:
   - marked `linguist-generated -diff`;
   - written only by the bot;
   - never merged: take either side and regenerate;
   - `manifest.json` excluded from merges.
5. **M1-36 is rescoped accordingly.** It covers:
   - the local index location and freshness: is the index's stored commit the checked-out HEAD, or else run `update`;
   - the export and import with its pinned tool and schema version;
   - a policy setting for when to use a published export.

   The nightly workflow template (M2-44) becomes the opt-in export job.

## Not measured

- No large repo: nothing above 4k nodes, and no monorepo, so the 60 s threshold and the per-year figures are linear extrapolations.
- No macOS or ARM.
- No real daily runs: commit-spaced stand-ins only.
- GitHub's behaviour (file limits, server-side merges, LFS pricing) is from documentation memory, not tested.
- Database size reductions (`VACUUM`, shorter paths) were not tried. They would not fix the path or determinism problems.

## Cost if wrong

Local-first needs no infrastructure; adding the published export later is additive (M2-44). If local builds turn out slow on typical org repos, the opt-in threshold is a policy value, not a code change.
