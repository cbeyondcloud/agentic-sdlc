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
| `superpowers/requesting-code-review` | `skills/requesting-code-review` | `SKILL.md` is the reviewer checklist from the upstream `code-reviewer.md` (what to check, calibration, output, critical rules), for the Code Review agent itself. Removed: subagent dispatch, the git-SHA and placeholder steps, the requester-side guidance (covered by `superpowers/receiving-code-review`) and the temporary-worktree hint. Upstream Critical / Important / Minor map to the handoff's critical / high / medium-low severities |
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
| `addyosmani/interview-me` | `skills/interview-me` | "Loading Constraints" (no non-interactive use) became "In a Pipeline Step": one round of questions per step, asked through Open Questions and answered at the gate. The confirmed intent goes in the handoff instead of `docs/intent/`; "stop your turn" became "end the step". `spec-driven-development` references use its catalog key |
| `addyosmani/api-and-interface-design` | `skills/api-and-interface-design` | None |
| `addyosmani/browser-testing-with-devtools` | `skills/browser-testing-with-devtools` | The install block (a vendor CLI settings file and an unpinned `npx ...@latest` command) was replaced by a note that the organisation configures pinned browser tools. A quoted prompt-injection example was paraphrased so the admission scan does not refuse the skill |

Mentions of addyosmani skills that are not vendored (for example `performance-optimization`)
are left as plain references.

## mattpocock/skills

- Source: https://github.com/mattpocock/skills
- Commit: 423ed08ea2185f951ea9d3a8c68911bbbd3e8d88 (2026-10-05)
- Licence: MIT. Copyright (c) 2026 Matt Pocock.

| Skill | Upstream path | Changes |
|---|---|---|
| `mattpocock/domain-modeling` | `skills/engineering/domain-modeling` | Left out: the vendor-specific `agents/` metadata |
| `mattpocock/to-spec` | `skills/engineering/to-spec` | The spec goes in the step's handoff instead of the issue tracker (no triage label, no set-up command); the seam check with the user became an Open Question. Left out: the `disable-model-invocation` front matter field and the `agents/` metadata |
| `mattpocock/improve-codebase-architecture` | `skills/engineering/improve-codebase-architecture` | Candidates go in the step's handoff instead of an HTML report (left out: `HTML-REPORT.md` and its CDN scripts). The skill-tool calls (`codebase-design`, `grilling`, `domain-modeling`) and the sub-agent were removed: the vocabulary is listed inline, the grilling loop runs through Open Questions at the gate, glossary and ADR changes are proposed in the handoff, and "design it twice" is done by the agent itself. Left out: the `disable-model-invocation` field and the `agents/` metadata |

`mattpocock/grill-with-docs` is not vendored: its whole body asks the agent to call two
other skills (`grilling`, `domain-modeling`), so it adds nothing to `addyosmani/interview-me`
plus the vendored `mattpocock/domain-modeling`.
