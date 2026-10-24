# M2 Policy Rollout (M2-23 overlays, M2-15 versioned rollout) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Team leads tailor an org pipeline per repository with an **overlay** kept in the policy repo (`teams/<team>/pipelines/overlays/<repo>.yaml`, D33). An overlay may add steps, narrow gates and remove steps, but never remove or weaken an **org-required** step or gate. A gate whose step was removed **re-anchors** to the previous remaining step. Org admins roll a new version of an agent or pipeline out **team by team** (`policy/rollout.yaml`); a team outside the rollout keeps the previous version, a team may **pin** a version, and a **deprecated** version warns until its date and is refused from it. The resolved pipeline (base + overlay, at the version the team gets) is what preflight checks, the CLI shows and the control plane's approved-hash check verifies.

**Architecture:**
- **Core, `agentic.definitions`** (no new dependency):
  - `overlay.py`: the overlay file model (`OverlayFile`, `PipelineOverlay`, `Insert`, `GateChange`), `load_overlay(path)`, `overlay_for(team_root, repo_key)` (which file applies), `apply_overlay(base, overlay, required) -> Overlaid` (the resolved `PipelineDefinition` plus one note per change, including each re-anchored gate). A refusal is `OverlayRefused(problems)`, a `DefinitionError` whose lines name the rule (`required-step`, `required-gate`, `four-eyes`, `regime`, `max-minutes`, `unknown-step`, `empty-loop`, `invalid`).
  - `versions.py`: the version rule models (`RolloutEntry`, `Rollout`, `Deprecation`, `Deprecations`, `Pins`) that `OrgPolicy` and `TeamPolicy` use as field types (policy already imports definitions, never the other way), `choose_version(...) -> VersionChoice`, `deprecation_state(...)`, `version_problem(...)` (the pure check the control plane runs too), and the archive layout `archived(layer_root, kind, name) -> {version: path}`.
  - `discovery.py`: `DefinitionSet.discover(..., rules: PolicyRules | None = None)`. `PolicyRules` carries the team, the repo key, today's date and the policy's rollout, deprecations, pins and required steps. Discovery picks each definition's version (a pinned or held-back version is read from `<layer>/agents|pipelines/versions/<name>/<version>.md|yaml`), then applies the team's overlay. It records `versions` (every non-current or deprecated choice), `overlays` (pipeline -> `AppliedOverlay(file, notes)`) and keeps `pipeline_files` pointing at the base file actually used.
  - `agentic.engine.preflight`: two new checks - `required-step` (an org-required name missing from the resolved pipeline: a team pipeline that shadows the org one is caught too) and `version` (the pipeline's or an agent's version is deprecated: warning; blocked: error).
- **Core, `agentic.policy`:** `OrgPolicy.required_steps` (`org.yaml`), `OrgPolicy.rollout` and `OrgPolicy.deprecated` (new file `rollout.yaml`), `TeamPolicy.pins`; `OrgPolicy.rules(team, repo, today) -> PolicyRules`. `load.pipeline_digests` also hashes archived pipeline versions; new `load.overlay_index(root)` lists every approved overlay with its digest, repo and pipelines.
- **Core, `agentic.policy.tree`** (new): `tree_problems(root, policy, today)` validates rollout entries, pins and every team's overlays against the tree. Both PR gates call it: `agentic.skills.gate.check_policy_repo` (`agentic policy check`) and the control plane's `policy_sync.validate.validate_policy_tree`.
- **Control plane (no schema change, no new table, no new route):** `run_settings` gains `overlays` and `versions`; run registration's `pipeline` check verifies the overlay (approved, in the run's team folder, digest matches) and refuses a run that skips an approved overlay applying to its team, repo and pipeline; a new `versions` check refuses a blocked (deprecated) version by the server's clock, a version other than the team's pin, and a new version sent by a team outside its rollout.
- **CLI:** `Workspace.definitions()` passes the rules (team, origin repo key, injectable clock); registration sends `overlay_file`/`overlay_sha256`/`pipeline_version`; `run --dry-run` shows `overlay` (file and changes) and `versions`; `pipelines list` shows the overlay column; a locally edited overlay is named in the cache refusal.
- **Templates and docs:** `templates/policy-repo/policy/rollout.yaml`, an example overlay `teams/payments/pipelines/overlays/payments-api.yaml`, `required_steps` in `org.yaml`, a commented `pins:` in `policy/teams/payments.yaml`, README sections (overlays, rollout, resolution order), spec §6 / §12.5 text.

**Tech Stack:** Python 3.12, pydantic, pytest, the existing control plane (FastAPI, SQLAlchemy, Postgres in Docker for its tests). No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md` §4.3 (policy repo), §6 and §6.1 (overlays, org-required phases and gates, re-anchoring, D33), §12.5 (versioning and rollout), §27. Tracker rows M2-23 and M2-15 and their deferred items.

## OSS reuse decisions

| Need | Considered | Decision |
|---|---|---|
| Overlay merge | RFC 6902 JSON Patch (`jsonpatch`, BSD), `jsonmerge` (MIT), kustomize strategic merge (Apache-2.0, Go), Zyroflo (`tools.sdlc`: no overlay, rollout or re-anchor code) | Own ~200 lines. JSON Patch addresses list positions, so it cannot name the step it removes, refuse a required one by rule or re-anchor a gate; kustomize is a Go binary. Our overlay addresses steps by id, which is what the rules need. |
| Version ordering | `packaging.version`, `semver` (BSD) | Not needed: a rollout entry names its `previous` version explicitly, so nothing is ordered (and nothing guesses which archived version a team falls back to). |
| Dates / clock | stdlib `datetime.date` | Injected `today` everywhere; the CLI's `Workspace.clock`, the control plane's existing `now`. |

## Global Constraints

- **Overlays live only in the policy repo** under `teams/<team>/pipelines/overlays/`, owned by that team's leads through CODEOWNERS (D33). A repo-layer or org-layer overlay folder is not read. The file stem is the repository's name (the last part of its `host/owner/name` origin key, compared case-insensitively); an optional `repo: host/owner/name` binds it to exactly one repository. A repository without an origin gets no overlay (the control plane cannot identify it either).
- **An overlay never weakens.** It may: `remove` steps, gates and loops that are not org-required; `add` steps (`after:` or `before:` a named step, top level or inside a loop); change a gate's `approver_role` (not for an org-required gate), turn `four_eyes` on (never off), set `on_reject`; tighten `regime` (solo < team < regulated); lower `max_minutes`. Anything else is refused with the rule named. It cannot add a `when` to a step or loosen a loop.
- **Re-anchoring.** A gate's anchor is the nearest step before it that is not a gate. When an overlay removes a gate's anchor, the gate stays and anchors to the previous remaining step; with none before it, it moves right after the next remaining step. A gate whose `on_reject` named a removed step routes to its new anchor. Every re-anchoring is a note the CLI shows.
- **Org-required steps** are `org.yaml` `required_steps: {<pipeline>: [<step, gate or loop name>]}`. Preflight refuses a resolved pipeline missing one, whatever the cause (overlay, team pipeline shadowing the org one, an archived version).
- **Version resolution order** (documented in the policy-repo README and spec §12.5): (1) the team's pin (`policy/teams/<team>.yaml` `pins:`); else (2) when `rollout.yaml` lists the definition and the team is not in its `teams`, its `previous` version; else (3) the current file. Then the chosen version's deprecation: before `blocked_from` a warning, from that date (UTC) a refusal. No team is "not in the rollout". Rollout and pins apply to the org and team layers of the policy repo; a repo-layer definition is never versioned.
- **Archive layout.** `<layer>/agents/versions/<name>/<version>.md`, `<layer>/pipelines/versions/<name>/<version>.yaml`. An archived file's `name` and `version` must match its path.
- **Integrity (D33, M2-21).** The approved hashes cover the base file (current or archived) and the overlay file. The control plane checks both and refuses a run that leaves out an approved overlay that applies to it.
- **Provider-agnostic:** no vendor names in any new model, rule, flag or message.
- **No schema change:** the control plane stores the new settings in the existing `policy_versions.settings` JSON; versions stored before this release are read back from their bundle (`RUN_SETTINGS_KEYS`).
- **Formatting, CI and records:** never add `# fmt: skip` or `# fmt: off`; line length 100; pytest exit codes checked directly; `uv run python scripts/check.py --all` green on Windows and Linux; TRACKER.md, DEFERRED.md and DECISIONS-*.md are updated by the coordinator from this track's output.

## Review Focus

1. **An overlay cannot remove or weaken an org-required step or gate**, including by removing the loop that holds a required step, changing a required gate's approver, turning `four_eyes` off, or loosening the regime; each refusal names its rule. Test: Task 2 `test_overlay_cannot_remove_required_gate`, `test_removing_a_loop_with_a_required_step_is_refused`, `test_required_gate_approver_cannot_change`, `test_four_eyes_cannot_be_turned_off`, `test_regime_only_tightens`.
2. **Re-anchoring is right at the edges:** a removed step before a gate, a removed first step (gate moves after the next step), `on_reject` naming a removed step, a removed loop. Test: Task 2 `test_removed_step_reanchors_gate`, `test_gate_at_head_moves_after_next_step`, `test_on_reject_follows_the_new_anchor`.
3. **A team not in the rollout keeps the previous version; a pin wins; a deprecated version warns before its date and is refused from it** (the clock injected). Test: Task 3 `test_team_not_in_rollout_keeps_previous_version`, `test_pin_wins_over_rollout`, `test_deprecated_warns_then_blocks`; Task 4 `test_preflight_blocks_a_deprecated_agent_after_its_date`.
4. **The approved-hash check covers base and overlay**: an edited overlay, an overlay of another team, and a run that skips an approved overlay are each refused by the control plane; an unchanged overlaid run passes. Test: Task 6 `test_edited_overlay_refused`, `test_overlay_of_another_team_refused`, `test_skipped_overlay_refused`, `test_overlaid_run_passes`.
5. **A broken overlay, rollout or pin is caught at the policy repo's pull request gate and by policy sync** (the version is rejected and the previous one stays active), not first on a laptop. Test: Task 5 `test_gate_names_a_refused_overlay`, `test_rollout_without_previous_archive_is_a_problem`; control plane `test_sync_rejects_a_policy_with_a_refused_overlay`.
6. **Windows and macOS:** file stems compared case-insensitively, POSIX relative paths in every digest key, no CRLF dependence (digests are of the bundle's bytes, as M2-21).

---

### Task 1: Policy model - required steps, rollout, deprecations, pins

**Files:**
- Create: `core/src/agentic/definitions/versions.py` (models only in this task)
- Modify: `core/src/agentic/policy/model.py`, `core/src/agentic/policy/load.py` (`rollout.yaml` in `FILE_FIELDS`, `org.yaml` `required_steps`)
- Test: `core/tests/policy/test_rollout_policy.py`

**Interfaces:**
- `RolloutEntry(version: str, previous: str, teams: list[str])` (strict; `version != previous`).
- `Rollout(agents: dict[str, RolloutEntry] = {}, pipelines: dict[str, RolloutEntry] = {})`.
- `Deprecation(blocked_from: date, note: str = "")`; `Deprecations(agents|pipelines|skills: dict[str, dict[str, Deprecation]])` (name -> version -> deprecation).
- `Pins(agents: dict[str, str] = {}, pipelines: dict[str, str] = {})`.
- `OrgPolicy.required_steps: dict[str, list[str]] = {}`, `OrgPolicy.rollout: Rollout`, `OrgPolicy.deprecated: Deprecations`, `TeamPolicy.pins: Pins`. Consistency: rollout `teams` name known teams.
- [ ] Step 1: failing tests: `test_rollout_file_loads`, `test_rollout_names_unknown_team_refused`, `test_rollout_previous_must_differ`, `test_team_pins_load`, `test_required_steps_load_from_org_yaml`, `test_unknown_rollout_kind_refused`.
- [ ] Step 2: run, see FAIL (unknown policy file `rollout.yaml`).
- [ ] Step 3: implement. Step 4: PASS. Step 5: commit `feat(core): rollout, deprecation, pins and required steps in org policy (M2-15, M2-23)`.

### Task 2: Overlays and gate re-anchoring (M2-23)

**Files:** Create `core/src/agentic/definitions/overlay.py`; Test `core/tests/definitions/test_overlay.py`.

**Interfaces:** `OverlayFile(repo: str | None, pipelines: dict[str, PipelineOverlay])`; `PipelineOverlay(remove: list[str], add: list[Insert], gates: dict[str, GateChange], regime, max_minutes)`; `Insert(after | before, step: Step)`; `GateChange(approver_role, four_eyes, on_reject)`; `load_overlay(path)`; `overlay_for(team_root, repo_key) -> Path | None`; `overlay_applies(stem, repo_field, repo_key) -> bool`; `apply_overlay(base, overlay, required=()) -> Overlaid(pipeline, notes)`; `OverlayRefused(DefinitionError)`.
- [ ] Step 1: failing tests: `test_overlay_cannot_remove_required_gate` (RF1), `test_removed_step_reanchors_gate` (RF2), `test_gate_at_head_moves_after_next_step`, `test_on_reject_follows_the_new_anchor`, `test_removing_a_loop_with_a_required_step_is_refused`, `test_required_gate_approver_cannot_change`, `test_four_eyes_cannot_be_turned_off`, `test_regime_only_tightens`, `test_max_minutes_only_lowers`, `test_add_after_and_before_including_inside_a_loop`, `test_gate_added_inside_a_loop_is_invalid`, `test_unknown_step_named`, `test_removing_the_last_loop_step_is_refused`, `test_overlay_file_matching_by_repo_name_and_key`.
- [ ] Step 2: FAIL (no module). Step 3: implement. Step 4: PASS. Step 5: commit `feat(core): pipeline overlays with org-required steps and gate re-anchoring (M2-23)`.

### Task 3: Version choice and deprecation (M2-15)

**Files:** Modify `core/src/agentic/definitions/versions.py`; Test `core/tests/definitions/test_versions.py`.

**Interfaces:** `VersionChoice(kind, name, version, current, reason: "current"|"pinned"|"held", deprecation: Deprecation | None, blocked: bool)` with `describe()`; `choose_version(kind, name, current, *, rollout, pins, team) -> tuple[str, reason]`; `deprecation_state(kind, name, version, deprecated, today) -> (Deprecation | None, blocked)`; `version_problem(kind, name, version, *, rollout, pins, deprecated, team, today) -> str | None`; `archived(layer_root, kind, name) -> dict[str, Path]`.
- [ ] Step 1: failing tests: `test_team_not_in_rollout_keeps_previous_version` (row test), `test_team_in_rollout_gets_new_version`, `test_no_team_is_outside_every_rollout`, `test_pin_wins_over_rollout`, `test_deprecated_warns_then_blocks` (row test: blocked on and after `blocked_from`, warning the day before), `test_version_problem_for_the_control_plane`, `test_archived_versions_listed`.
- [ ] Steps 2-5 as above; commit `feat(core): team-by-team version choice and deprecation (M2-15)`.

### Task 4: Discovery and preflight

**Files:** Modify `core/src/agentic/definitions/discovery.py`, `core/src/agentic/policy/model.py` (`OrgPolicy.rules`), `core/src/agentic/engine/preflight.py`; Test `core/tests/definitions/test_discovery_versions.py`, `core/tests/engine/test_preflight_required.py`.

**Interfaces:** `PolicyRules(team, repo, today, rollout, deprecated, pins, required_steps)`; `DefinitionSet.discover(..., rules=None)`; `DefinitionSet.versions: dict[tuple[str, str], VersionChoice]`, `DefinitionSet.overlays: dict[str, AppliedOverlay]`, `overlay_file(name)`, `version_choice(kind, name)`.
- [ ] Step 1: failing tests: `test_held_back_team_gets_the_archived_agent`, `test_pinned_pipeline_read_from_archive`, `test_missing_archive_is_a_definition_error`, `test_archived_file_must_match_its_path`, `test_overlay_applied_for_the_repo`, `test_overlay_of_other_repo_not_applied`, `test_overlay_refusal_names_the_file_and_rule`, `test_preflight_refuses_missing_required_step`, `test_preflight_warns_on_a_deprecated_pipeline`, `test_preflight_blocks_a_deprecated_agent_after_its_date` (RF3).
- [ ] Steps 2-5; commit `feat(core): discovery applies versions and overlays; preflight checks required steps and deprecations`.

### Task 5: Policy-tree validation (both PR gates)

**Files:** Create `core/src/agentic/policy/tree.py`; Modify `core/src/agentic/skills/gate.py`, `control-plane/src/agentic_cp/policy_sync/validate.py`; Test `core/tests/policy/test_policy_tree.py`, control plane `test_policy_sync.py` (one case).

**Interfaces:** `tree_problems(root, policy, today) -> list[str]` (each line names the file relative to the root): rollout `version` is the current file's version and `previous` is archived; each pin names an existing version; every overlay of every team loads and applies to that team's pipeline (at the version the team gets) without refusal; the overlay's pipelines exist.
- [ ] Step 1: failing tests: `test_gate_names_a_refused_overlay` (RF5), `test_rollout_without_previous_archive_is_a_problem`, `test_rollout_version_must_be_current`, `test_pin_of_unknown_version_is_a_problem`, `test_overlay_of_unknown_pipeline_is_a_problem`, `test_valid_tree_has_no_problems`; control plane `test_sync_rejects_a_policy_with_a_refused_overlay`.
- [ ] Steps 2-5; commit `feat: the policy-repo gate and policy sync validate overlays, rollout and pins`.

### Task 6: Integrity of overlaid and versioned pipelines (D33, M2-21)

**Files:** Modify `core/src/agentic/policy/load.py` (`pipeline_digests` + archives, `overlay_index`), `core/src/agentic/orgapi/models.py` (`RunRegistration.overlay_file`, `overlay_sha256`, `pipeline_version`), `control-plane/src/agentic_cp/policy_sync/service.py` (`run_settings` `overlays`, `versions`), `control-plane/src/agentic_cp/ingest/register.py` (`_pipeline` overlay rules, `_versions` check), `cli/src/agentic_cli/org_preflight.py`, `cli/src/agentic_cli/policy_source.py`; Tests `core/tests/policy/test_pipeline_digests.py`, `control-plane/tests/test_run_registration.py` (new cases), `cli/tests/run/test_org_preflight.py` (new cases).
- [ ] Step 1: failing tests: `test_digests_cover_archived_versions`, `test_overlay_index_lists_overlays`; control plane `test_overlaid_run_passes`, `test_edited_overlay_refused`, `test_overlay_of_another_team_refused`, `test_skipped_overlay_refused`, `test_blocked_version_refused_by_server_clock`, `test_version_outside_rollout_refused`, `test_pinned_version_mismatch_refused`; CLI `test_registration_sends_the_overlay`, `test_edited_overlay_named_in_the_cache_refusal`.
- [ ] Steps 2-5; commit `feat: approved-hash check covers overlays and archived versions; versions org check (M2-21, M2-23, M2-15)`.

### Task 7: CLI shows what applied

**Files:** Modify `cli/src/agentic_cli/workspace.py` (`clock`, `repo_key`, rules), `cli/src/agentic_cli/runtime.py` (plan `overlay`, `versions`; reuse `Workspace.repo_key`), `cli/src/agentic_cli/commands/catalog.py` (`pipelines list` overlay column, version notes); Test `cli/tests/test_overlay_rollout_cli.py`.
- [ ] Step 1: failing tests: `test_dry_run_shows_the_overlay_and_its_changes`, `test_pipelines_list_shows_overlay`, `test_team_outside_rollout_runs_previous_agent_version`, `test_deprecated_agent_blocked_after_date_in_cli` (clock injected), `test_standalone_policy_honours_rollout`.
- [ ] Steps 2-5; commit `feat(cli): show the overlay and versions that applied (M2-23, M2-15)`.

### Task 8: Templates and docs

**Files:** `templates/policy-repo/policy/rollout.yaml`, `templates/policy-repo/policy/org.yaml` (`required_steps`), `templates/policy-repo/policy/teams/payments.yaml` (commented `pins:`), `templates/policy-repo/teams/payments/pipelines/overlays/payments-api.yaml`, `templates/policy-repo/README.md`, `templates/policy-repo/CODEOWNERS` (comment), spec §6 / §12.5; Test `core/tests/policy/test_policy_repo_template.py` (`test_template_overlay_applies_to_the_library_pipeline`, `test_template_rollout_loads`).
- [ ] Commit `docs: policy-repo template overlay and rollout examples; resolution order`.

### Task 9: Deferred items targeting M2-23 / M2-15

Each one is closed with a regression test, or recorded with why it stands:
- DF-227, DF-236 (QA fail route back): already closed by SP6 on-fail (QA inside the build loop, `failing-handoff` pause) - verify and report Done with the evidence.
- DF-263, DF-269 (opt-out of the completion rule for review-only pipelines): pipeline key `failing_handoff: pause | complete` (default `pause`); an overlay cannot change it.
- DF-530, DF-533 (pre-pass reporting and keys): small fixes where they stay inside this track's files; otherwise retarget.
- DF-315, DF-317 (checkpoint exclusion list): narrow the glob, log each excluded path, and let org policy extend (never shrink) the list - if it fits; otherwise retarget.
- DF-264, DF-272, DF-473, DF-525 (consume-by-step, remaining-work loop, deterministic pre-pass gate): engine/handoff features outside overlays and rollout - retarget with notes.

**Outcomes (continuation after the API outages):**
- DF-227, DF-236: Done by SP6 on-fail (DF-238): QA runs inside the build loop, a failing report routes back to the Developer, and a run ending on one pauses `failing-handoff` (`core/tests/engine/test_on_fail.py::test_a_run_ending_on_a_failing_qa_pauses_instead_of_completing`).
- DF-263, DF-269: Done: `failing_handoff: pause | complete` (default `pause`, left out of the pipeline digest so earlier runs resume); the pause reason names the opt-out; preflight's judging-step warning is skipped for `complete`; an overlay cannot set it.
- DF-315, DF-317: Done: `**/*.py[co]` (a `.pyd` is work), each path left out logged at info, `org.yaml` `checkpoint_exclude` adds (never removes) and reaches every step's checkpoint (`StepWiring.checkpoint_exclude`).
- DF-530: Done: `nothing-scanned` renders "ran, nothing to scan" (findings 0, not in `not_run`).
- DF-533: Partial: the pre-pass failing is a "could not run" block, never a crashed review step; the template's `org.yaml` shows `review_prepass` and `checkpoint_exclude`; `personal_skills`/`personal_memory` were already off for `restricted` in both templates. A run-ledger record that the pre-pass ran is retargeted (the prepass JSON report is written to the run folder already).
- DF-264, DF-272, DF-473, DF-525: Retarget (engine/handoff features: consume-by-step, a remaining-work loop condition, a deterministic gate on pre-pass errors).

Review fixes made in the continuation: the rollout holds back org definitions only (a team's own definition is the team's to pin); run registration reads a current pipeline file's version from the approved tree (`version_index`) instead of trusting the CLI's `pipeline_version`, and never applies the org rollout to a team-owned definition; catalogued skills' deprecations are checked by CLI preflight.

### Task 10: Full CI

- [ ] `uv run python scripts/check.py --all` in the background to completion; fix and rerun until green; re-record acceptance cassettes only if a prompt changed.
