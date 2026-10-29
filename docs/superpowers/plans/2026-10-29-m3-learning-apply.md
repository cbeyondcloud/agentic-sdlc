# M3 Learning loop L4-L6 Implementation Plan (M3-14, M3-15, M3-16)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A learning proposal (L3) becomes a concrete, reviewable change (L4): a diff of one target file, the corrections that triggered it, and a regression eval case in the M1-63 format, aimed at the narrowest sensible scope (one user's corrections: personal; several users in one repo: repo; across a team's repos: team). The owner of what changes decides (L5, D30): `agentic learn list | show | approve | edit | reject | defer | endorse | propose --from | revert`, pending proposals shown at the end of a run and mailed as a digest to approvers through the notifications outbox; a developer may endorse a shared proposal but never approve it. Only an approved proposal is applied (L6, D29): a personal change is written into `~/.agentic` with an undo record (Zyroflo `learning.py` through `sdlc_commons.learning`); a shared change lands as a pull request on a new branch through the existing hosting adapter, never on the default branch; `revert` restores the previous content (a revert pull request for shared items). The regression case can be run before and after the change through the eval harness (`run_eval`) with scripted runners; the agent-quality dashboard shows the correction rate per agent before and after each learning; every decision is on the audit chain.

**Architecture:**
- **Core, `agentic.learning` (no new dependency; imports neither `sdlc_commons` nor Zyroflo):**
  - `changes.py` (M3-14): `Evidence` (one triggering correction), `narrowest_scope`, the change kinds and their targets, `render_change` (a marked block under a "Learned from corrections" heading, written in the file's own line endings), `strip_change` (the revert), `instruction_only_problems` (front matter unchanged or within the personal-skill allow-list, no tools/model/limits key, the admission static scan on the added text), `Draft` and `draft(...)` (diff by `difflib`).
  - `regression.py` (M3-14, M3-16, DF-247): `regression_case` (an `EvalCase`, class `scoping`, the lesson as its key point, tagged `learning`), `LessonCovered` (a pydantic-evals `Evaluator`: lexical coverage of the key points), `measure(case, before, after)` over `run_eval` with labels `before`/`after`, and the per-case regression rule (`Measurement.regressed`: the after score is lower).
  - `approval.py` (M3-15): statuses and transitions (`next_status`), the approve permission per scope, `may_decide` (personal: that user only; shared: the scope's permission; standalone: no RBAC, said so), `owner_of`.
  - `clustering.py`: statuses grow (`approved`, `deferred`, `applied`, `reverted`); a `reverted` lesson is remembered like a rejected one; `approved`/`deferred`/`applied` proposals are matched (not re-proposed) but never grow; `LearningSettings.min_confidence` (DF-732).
  - `store.py`: decisions (`decide`), endorsements, and one change record per proposal (`changes/<id>.json`: the draft as approved, how it was applied, how it was reverted).
- **Commons, `sdlc_commons.learning`** (DF-728): `PersonalChanges(home)` writes an applied row in Zyroflo's learning log format (`zyroflo.sdlc.learning.append_log`, `digest`, `history`, `standing`) before the file, and reverts with Zyroflo's `undo` (restores the prior bytes in this checkout's line endings, appends the reversal, records drift); a file the change created is removed after the undo. Gaps (a generic text change instead of `review_suppress`, a configurable log path, removing a created file) are a deferred Zyroflo PR item.
- **CLI:** `agentic_cli/learning_apply.py` (drafts from the workspace, approve/apply/revert, the PR, the end-of-run notice) and the `learn` group grows the L5 commands. The shared PR reuses `repo_setup.commit_on_branch` (git plumbing: the checkout, index and current branch never change), `hosting.git.fetch_branch`/`push_branch` and `HostingProvider.open_pull_request`; branch `agentic/learning/<id>` built on the remote default branch.
- **Control plane:** migration `m3_14_learning_apply` (after `m3_11_learning`): `learning_proposals` gains `owner`, `kind`, `edited_lesson`, `endorsements`, `draft`, `applied_at`, `applied_ref`, `reverted_at`, `reverted_ref`, `measurement`, and the six statuses; the view `dash_learning_effect` (correction rate per agent in the 30 days before and after each applied learning) is created here, not by the M2-37 migration (its tables are newer). Routes: `GET /v1/learning/proposals/{id}`, `POST /v1/learning/proposals` (propose --from), `PUT .../{id}/draft` (edit), `POST .../{id}/decision` (approve/reject/defer), `POST .../{id}/endorse`, `POST .../{id}/applied`, `POST .../{id}/reverted`. A digest mail (`learning.digest`) per approver per day is queued in the ingest transaction that creates a proposal.

**Tech Stack:** Python 3.12, stdlib `difflib`, Pydantic v2, pydantic-evals (existing `eval` extra), FastAPI + SQLAlchemy Core + Alembic (existing), Zyroflo (pinned, through `sdlc_commons`). No new dependency.

**Spec:** master plan "Learning loop" sections 4-6 (D29, D30); rows M3-14..M3-16; deferred items DF-169, DF-181, DF-247, DF-564, DF-726, DF-727, DF-728, DF-730, DF-732, DF-733, DF-735, DF-736, DF-737.

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| Apply with undo, line endings kept, drift recorded | Zyroflo `learning.py` (`append_log`, `history`, `standing`, `digest`, `undo`) via `sdlc_commons.learning` | Zyroflo `apply` renders only a `review_suppress` YAML list and requires its corpus/G3 gates: the generic-text apply row is written by the adapter in Zyroflo's row format, so Zyroflo's `undo` reverses it. Gap -> Zyroflo PR (deferred) |
| Per-case regression | the M1-63 harness (`run_eval`, pydantic-evals `Evaluator`) | Zyroflo `corpus_replay` grades impact-ranking precision on Zyroflo's own history: it cannot test an instruction change (Zyroflo's own `corpus_gate` docstring says so) |
| Diffs | stdlib `difflib.unified_diff` | `unidiff` (MIT) parses diffs; we only write them |
| Branch + PR | `repo_setup.commit_on_branch`, `hosting.git`, `HostingProvider.open_pull_request` (M2-46 skill promotion's path) | PyGithub (LGPL) is not on the allow-list; the adapter exists |
| Repo maintainers | the control plane's CODEOWNERS-derived `repo_maintainers` (M2-03) and `agent.approve.repo` | a second CODEOWNERS parser in the CLI |
| Mail | the notifications outbox (M2-07) | — |

## Global Constraints

- **Nothing is applied without approve (D29).** `apply` refuses any proposal whose status is not `approved`; `approve` is the only path to `approved`. Tests prove a pending, deferred or rejected proposal leaves every target file untouched.
- **Owner-only approval (D30).** personal -> that user; repo -> `agent.approve.repo` on the repo (CODEOWNERS maintainers, team leads); team -> `agent.publish.team`; org -> `skills.approve` (org targets are phase 2: never chosen automatically). Endorse adds the caller to `endorsements` and never changes the status. Standalone has no RBAC: a shared decision proceeds with a warning, as `skills approve` does.
- **Instruction-only.** A change only adds a marked block of text. An agent definition's front matter must be byte-identical; a skill's front matter keys stay within `name, description, license, compatibility, metadata`; the added text passes the admission scan; a personal skill passes `load_personal` with no rejection; a repo skill passes `check_skill`.
- **Never on the default branch.** A shared change is a commit on `agentic/learning/<id>` (or `-<n>`) on top of the fetched remote default branch, pushed and opened as a pull request. Tests use a local bare remote and the fake GitHub only (owner decision 73).
- **Line endings.** Rendering works on an LF view; the result keeps the file's CRLF/LF majority; digests are over the LF view (Zyroflo's rule).
- **Ids and paths.** Targets are POSIX relative paths under `~/.agentic` (`home`), the repo (`repo`) or the policy repo (`policy`); a learned skill is `learned-<agent>`; the regression case is `eval/cases/scoping/learned-<id>.yaml` under the same root (`.agentic/eval/...` in a repo).
- **Formatting, CI and records:** never add `# fmt: skip` / `# fmt: off`; line length 100; check pytest's exit code directly; `uv run python scripts/check.py --all` green on Windows and Linux; TRACKER.md, DEFERRED.md and DECISIONS are updated by the coordinator from this track's output.

## Review Focus

1. **Nothing applied without approve; owner-only approval.** `test_nothing_is_applied_without_approve` (CLI, standalone), `test_developer_cannot_approve_a_shared_proposal` and `test_personal_proposal_is_approvable_by_its_user` (control plane), `test_endorse_adds_weight_but_never_approves`, `next_status` refusing every other transition.
2. **The draft** (row M3-14): `test_draft_holds_diff_evidence_and_eval_case`, `test_narrowest_scope_is_chosen`, `test_changes_stay_instruction_only` (a front matter change, a tools key and an injection phrase are refused), `test_new_agents_md_is_proposed_when_missing` (DF-181), CRLF kept.
3. **Apply/undo** (row M3-16): personal through Zyroflo's undo (`test_personal_apply_and_revert_restore_bytes`, created files removed); shared as a PR on a new branch with the default branch untouched; `test_e2e_three_corrections_to_pr_and_revert` (revert PR restores the previous content).
4. **Audit and isolation.** `learning.proposed/.approved/.rejected/.deferred/.edited/.endorsed/.applied/.reverted` on the chain; every new route an `EndpointCase` (another tenant's proposal is 404); the migration matches the metadata; `dash_learning_effect` is registered, tenant-scoped and provisioned in the agent-quality dashboard.
5. **Measurement.** `test_measure_runs_the_case_before_and_after` (scripted runners) and the regression rule.

---

### Task 1: Statuses, transitions and approval rules (`agentic.learning.approval`, clustering, store)

**Files:** Create `core/src/agentic/learning/approval.py`; modify `clustering.py`, `store.py`; tests `core/tests/learning/test_approval.py`, `test_clustering.py`, `test_local_store.py`.

**Interfaces:** `ProposalStatus` (six values); `next_status(status, action) -> status` (`DecisionError`); `APPROVE_PERMISSION`; `may_decide(scope, owner, user, allowed) -> bool`; `Proposal.decided_by`, `Proposal.endorsements`; `LocalLearningStore.decide(id, action, by, when)`, `endorse(id, by)`, `change(id)`, `save_change(id, record)`; `LearningSettings.min_confidence`.

- [ ] **Step 1: Failing tests**: `test_only_listed_transitions_are_allowed`, `test_personal_is_decided_by_its_user_only`, `test_shared_needs_the_scope_permission`, `test_reverted_lesson_is_remembered_like_a_rejected_one`, `test_approved_proposal_is_not_proposed_again`, `test_store_records_decisions_endorsements_and_changes`, `test_low_confidence_learners_do_not_count`.
- [ ] **Step 2: Run** `uv run pytest core/tests/learning -q`: FAIL. **Step 3:** implement. **Step 4:** PASS. Commit.

### Task 2: Drafts (`agentic.learning.changes`)

**Files:** Create `core/src/agentic/learning/changes.py`; test `core/tests/learning/test_changes.py`.

**Interfaces:** `Evidence`, `Scope`, `ChangeKind`, `KINDS`, `narrowest_scope(evidence, cluster_scope)`, `target(kind, agent, team) -> (root, path)`, `render_change(kind, before, lesson, agent, proposal_id)`, `strip_change(text, proposal_id)`, `instruction_only_problems(kind, before, after)`, `Draft`, `draft(proposal, evidence, read, *, kind, lesson, team, at)`.

- [ ] **Step 1: Failing tests**: `test_draft_holds_diff_evidence_and_eval_case`, `test_narrowest_scope_is_chosen`, `test_changes_stay_instruction_only`, `test_new_agents_md_is_proposed_when_missing`, `test_crlf_file_keeps_crlf`, `test_strip_change_restores_the_previous_text`, `test_agent_instruction_needs_an_existing_definition`.
- [ ] **Step 2-4.** Commit.

### Task 3: Regression case and measurement (`agentic.learning.regression`)

**Files:** Create `core/src/agentic/learning/regression.py`; test `core/tests/learning/test_regression.py`.

- [ ] **Step 1: Failing tests**: `test_regression_case_is_a_valid_eval_case`, `test_measure_runs_the_case_before_and_after`, `test_lower_after_score_is_a_regression`.
- [ ] **Step 2-4.** Commit.

### Task 4: Personal apply/undo through Zyroflo (`sdlc_commons.learning`)

**Files:** Create `commons/src/sdlc_commons/learning.py`; test `commons/tests/test_learning_changes.py`.

- [ ] **Step 1: Failing tests**: `test_personal_apply_and_revert_restore_bytes`, `test_created_file_is_removed_on_revert`, `test_apply_twice_is_refused`, `test_crlf_kept_through_apply_and_revert`, `test_rows_are_zyroflos_learning_log`.
- [ ] **Step 2-4.** Commit.

### Task 5: CLI L5/L6 (standalone)

**Files:** Create `cli/src/agentic_cli/learning_apply.py`; modify `cli/src/agentic_cli/commands/learn.py`, `cli/src/agentic_cli/commands/runs.py` (end-of-run notice); tests `cli/tests/run/test_learn_apply.py`.

- [ ] **Step 1: Failing tests**: `test_show_has_diff_evidence_and_eval_case`, `test_nothing_is_applied_without_approve`, `test_personal_approve_applies_and_revert_restores`, `test_reject_is_remembered_and_defer_keeps_it_pending`, `test_edit_changes_kind_and_lesson`, `test_propose_from_one_feedback_record`, `test_end_of_run_shows_pending_proposals`, `test_e2e_three_corrections_to_pr_and_revert`, `test_bad_agent_name_and_run_id_are_usage_errors` (DF-730), `test_corrupt_proposals_file_has_a_hint` (DF-736).
- [ ] **Step 2-4.** Commit.

### Task 6: Control plane decisions, digest, dashboard

**Files:** Create `control-plane/src/agentic_cp/migrations/versions/m3_14_learning_apply.py`, `control-plane/src/agentic_cp/learning/decisions.py`, `learning/digest.py`, notify templates `learning_digest.{txt,html}`; modify `tables.py`, `learning/api.py`, `learning/store.py` (authors union DF-737, recluster after correction DF-733, min confidence, digest), `dashboards/views.py`, `dashboards/grafana/agent-quality.json`, `core/src/agentic/orgapi/models.py`, `control-plane/tests/isolation.py`; CLI client `control_plane.py` (org mode commands). Tests `control-plane/tests/test_learning_decisions.py`, dashboard tests.

- [ ] **Step 1: Failing tests**: `test_developer_cannot_approve_a_shared_proposal`, `test_personal_proposal_is_approvable_by_its_user`, `test_repo_maintainer_approves_and_applies_then_reverts` (audit events), `test_endorse_adds_weight_but_never_approves`, `test_propose_from_feedback`, `test_digest_is_queued_for_approvers`, `test_authors_are_kept_when_a_proposal_grows`, `test_learning_effect_view_shows_before_and_after`; isolation picks up the routes.
- [ ] **Step 2-4.** Commit.

### Task 7: Full check

- [ ] `uv run python scripts/check.py --all` (Windows + Linux) green; fix with new commits.
