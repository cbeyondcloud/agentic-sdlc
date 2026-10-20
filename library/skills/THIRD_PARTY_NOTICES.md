# Third-party notices: default skills

The skills in this folder are taken from the open-source projects below, at the pinned
commits shown, under their licences. Each skill folder carries its project's full licence
text as `LICENSE.txt`. The changes made to each skill are listed here and in the `notes`
of its `catalog.yaml` entry; the upstream text at the pinned commit is the reference for
any diff.

Common changes:

- References to the harness's own orchestration were removed or rewritten for a pipeline
  step: sub-agent dispatch, worktree and branch workflows, execution-method choices,
  plan-file locations, and `superpowers:`-style skill invocations (now catalog keys such
  as `superpowers/test-driven-development`).
- Scripts, executable examples, vendor-specific metadata and files whose licence is not
  confirmed were left out. Links to them were removed.
- Shared reference files that a skill links to outside its own folder were bundled into
  that skill. The links now point at the bundled copies.

## obra/superpowers

- Source: https://github.com/obra/superpowers
- Commit: 8ca22dba9a94f28898bbce59f2537ff4d87c747d (2026-09-25)
- Licence: MIT. Copyright (c) 2025 Jesse Vincent.

| Skill | Upstream path | Changes |
|---|---|---|
| `superpowers/test-driven-development` | `skills/test-driven-development` | A cross-reference to another skill was removed from `writing-good-tests.md` |
| `superpowers/verification-before-completion` | `skills/verification-before-completion` | None |
| `superpowers/systematic-debugging` | `skills/systematic-debugging` | Left out: `find-polluter.sh`, `condition-based-waiting-example.ts`, the pressure-test files and `CREATION-LOG.md`. The bisection-script and example references were reworded; skill references now use catalog keys |
| `superpowers/receiving-code-review` | `skills/receiving-code-review` | "GitHub Thread Replies" became "Answering Review Findings" (in the handoff); an instruction-file reference was removed |
| `superpowers/writing-plans` | `skills/writing-plans` | Removed: the announce line, worktree context, plan-file location, the sub-skill header line and the execution-method handoff. The plan now goes in the step's handoff |
| `superpowers/writing-skills` | `skills/writing-skills` | Personal-skill paths now point at this framework. Sub-agent, graphviz, render script and `@`-link references were removed. The testing sections moved to the bundled `testing-and-bulletproofing.md`. Left out: `render-graphs.js`, `graphviz-conventions.dot`, `examples/`, `testing-skills-with-subagents.md`, and `anthropic-best-practices.md` (licence of that text not confirmed) |

## addyosmani/agent-skills

- Source: https://github.com/addyosmani/agent-skills
- Commit: e9e172d909744bc7840562cc6a06774b06c0b60b (2026-10-05)
- Licence: MIT. Copyright (c) 2025 Addy Osmani.

| Skill | Upstream path | Changes |
|---|---|---|
| `addyosmani/spec-driven-development` | `skills/spec-driven-development` | Questions go to Open Questions for the gate. The spec and plan go in the step's handoff. References to `planning-and-task-breakdown`, `context-engineering` and slash commands were removed |
| `addyosmani/documentation-and-adrs` | `skills/documentation-and-adrs` | The rules file is named `AGENTS.md` |
| `addyosmani/incremental-implementation` | `skills/incremental-implementation` | `references/definition-of-done.md` bundled from the repository's shared `references/` |
| `addyosmani/code-review-and-quality` | `skills/code-review-and-quality` | `references/security-checklist.md` and `references/performance-checklist.md` bundled from the repository's shared `references/` |
| `addyosmani/security-and-hardening` | `skills/security-and-hardening` | `references/security-checklist.md` bundled from the repository's shared `references/` |

Mentions of addyosmani skills that are not vendored (for example `performance-optimization`)
are left as plain references.

## mattpocock/skills

- Source: https://github.com/mattpocock/skills
- Commit: 423ed08ea2185f951ea9d3a8c68911bbbd3e8d88 (2026-10-05)
- Licence: MIT. Copyright (c) 2026 Matt Pocock.

| Skill | Upstream path | Changes |
|---|---|---|
| `mattpocock/domain-modeling` | `skills/engineering/domain-modeling` | Left out: the vendor-specific `agents/` metadata |
