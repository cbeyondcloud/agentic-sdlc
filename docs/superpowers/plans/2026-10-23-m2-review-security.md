# M2 Review and Security Implementation Plan (M2-48, M2-49, M2-45)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three guards around what agents read and what leaves the machine.
- **M2-48 review pre-pass.** Before a Code Review agent reads a change, the framework runs deterministic checks over it: secrets, rule matches, known-vulnerable dependencies, blocked licences and new ignore rules. The agent gets the results in its instructions. A tool that could not run is reported as not run, with the reason; it is never shown as a pass.
- **M2-49 publish scan.** Run events are scanned for secrets and personal data before they reach the control plane. Policy decides whether a hit is masked or blocked, and every hit is audited.
- **M2-45 personal skills and memory (D30).** A user's own skills (`~/.agentic/skills/`) and notes (`~/.agentic/memory/`) shape only that user's runs. They are instruction-only and never widen what an agent may do. They sit beside shared skills and never replace one. Policy can turn them off per repo classification. Every use is recorded in the run ledger with kind, name, version and hash.

**Architecture:**
- **Core (`agentic`, no Zyroflo):**
  - `agentic.security.pii` (new): a precision-biased PII detector covering emails, phone numbers, Luhn-checked card numbers, separated US SSNs, UK NINOs and mod-97-checked IBANs.
  - `agentic.security.publish_scan` (new): `PublishGuard`. It combines the M1-41 secret scanner with the PII detector over JSON-like values. `screen(event, fields)` masks before an event is chained. `check(run_id, entries)` refuses an entry that is already chained and carries a hit. Both emit a `publish_scan` audit event naming kinds and counts, never values.
  - `agentic.telemetry.sinks.ForwardingRecorder` gains `screen`.
  - `agentic.review.prepass` (new): the built-in checks, which run everywhere because they are pure Python. These are the secret scan over added lines, the blocked-licence check over added SPDX tags and manifest licence fields, and the ignore-rule check (DF-70/DF-329). This module also merges tool documents and renders the agent's "Pre-pass results" block, fenced as untrusted data.
  - `agentic.skills.personal` (new): discovery and validation of personal skills and memory notes, and the personal block for an agent (`PersonalContext.prompt_for`).
  - `agentic.engine.factory.StepRunners` gains `prompt_extras`. It adds text at the end of a step's instructions, built when the step runs, and has no engine change.
  - Policy:
    - `OrgPolicy.publish_scan` (`PublishScanPolicy`);
    - `OrgPolicy.review_prepass` (`ReviewPrepassPolicy`);
    - `ClassificationRule.personal_skills` and `personal_memory`.
- **Commons (`sdlc_commons`, the only Zyroflo importer):**
  - `review_prepass` (new):
    - `ToolLocator` finds scanners only in the hash-verified tool cache (M1-51, `ToolFetcher.cached`), never on PATH. When a tool is missing it gives a reason: there is no pin, no build for this platform, no signed Windows build (skipped on Windows), or the tool is not installed.
    - `external_checks` runs Zyroflo's `tools.review.prepass.prepass` (gitleaks and ast-grep) and `tools.review.sca.scan_one` (osv-scanner) over the change's touched files.
    - Zyroflo looks binaries up in its own vendored folder and always queries OSV online. The adapter therefore swaps `prepass.binary`, and for the default offline mode `sca._invoke`, under a lock, for the length of one call. This is the same shim pattern as `sdlc_commons.guards`, and the gap is filed as a Zyroflo PR deferred item.
  - `worktrees.change_since_base(target, run_id)`: each live tree's diff from its base.
  - `ledger.RunLedger.record(..., skills=...)`: skill activations ride `ext.agentic.skills` (already declared in M1-07).
- **CLI:**
  - `agentic_cli.step_extras` (new) composes each step's extras. A Code Review agent (by type, from policy) gets the pre-pass block. Every agent in the owner's run gets the personal block, if the classification allows it.
  - `agentic_cli.skill_activity` (new) is a recorder wrapper. It stamps each `step_finished` event with the activations since the last one, both shared and personal.
  - `ControlPlaneSink(guard=...)`. The runtime and `agentic sync` pass the publish guard, and the runtime gives the recorder its `screen` in org mode.

**Tech Stack:** Python 3.12. No new dependencies. Everything reused is already locked: detect-secrets 1.5.0 (M1-41), regex, and Zyroflo's `tools.review` at the pinned commit. External scanners (gitleaks MIT, ast-grep MIT, osv-scanner Apache-2.0) are native binaries. They are optional and come only from the org mirror through M1-51.

**Spec:** §15 (secret scan before egress, supply chain of agent output), §11.3, §24 "Custom skills" (D30), §7.3 (run ledger), §14.5 (audit). Rows M2-45, M2-48 and M2-49; Notes of M2-20.

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| Review pre-pass runner, secret and rule gates | Zyroflo `tools.review.prepass` (`prepass()`, `diff_scope`, `collect_rules`, parsers, `unreviewed`, redaction of matched secrets) through `sdlc_commons` | Not copied (D34). Its binary lookup is not injectable, so a shim is used and Zyroflo PR deferred item ZR-1 filed |
| Dependency vulnerabilities | Zyroflo `tools.review.sca` (`scan_one`, `parse`, `severity_of`, `READ_ONLY_ENV`, `NO_IGNORE`), and osv-scanner via the fetcher | Zyroflo always queries the OSV API online. Offline is the default here (`--offline-vulnerabilities --download-offline-databases --no-resolve`): this downloads databases and sends no dependency list. Online is opt-in (`review_prepass.online_vulnerability_lookup`) |
| Built-in secret check | M1-41 `SecretScanner` (detect-secrets regex plugins plus `key_formats`) | gitleaks is not always present: it has no signed Windows build |
| Licence detection | Own small SPDX tag and manifest-field reader over added lines | ScanCode toolkit (Apache-2.0) is a Python application with native dependencies and has no single verified binary. Deferred (ZR-3); the gap is stated, not hidden |
| PII detection | The TokenLean G29 regex design (Apache-2.0, same owner), extended with UK NINO and IBAN | Presidio (MIT) needs spaCy and language models, far too heavy for a CLI. scrubadub pulls in scikit-learn and textblob. phonenumbers (Apache-2.0) has better phone recall but flags digit ids in event values; it could be a later optional extra |
| Personal-skill validation | Our admission scan (M1-39: `SCRIPT_SUFFIXES`, download-and-run, injection, hidden Unicode, telemetry URLs, `skill_files`, `skill_digest`) | — |

## Global Constraints

- **Never a fake pass.** A pre-pass tool that did not run has `status` set to something other than `ok`, `findings: null`, and a `detail` saying why. The agent's block lists it under "did not run". The built-in checks always run.
- **Windows.** A scanner runs only from the verified cache. If the org manifest has no signed Windows build, the locator returns "skipped on Windows: no signed build of <tool> in the org manifest (no unsigned native binary runs on Windows)". Nothing is looked up on PATH.
- **No third-party upload by default.** osv-scanner runs offline unless policy opts in. Nothing else talks to the network.
- **Chain integrity.** A chained ledger entry is never edited (the control plane verifies `entry_hash`). Masking therefore happens before an event is chained (`ForwardingRecorder.screen`). A hit found in an entry that is already chained cannot be masked, so the sink refuses to send it (`PublishBlocked`, a `SinkError`). The entry stays queued and audited.
- **Masks are ids.** A mask is `masked-<kind>` (for example `masked-email`), so a masked event value still passes the ledger's id rule.
- **Identity fields are exempt** (`approver`, `owner`, `actor` by default, set in policy). They name the people the audit trail is about. Structural fields (`hash`, `prev`, `seq`, `run_id`, `at`, `schema_version`, `event`, `ext`) are not scanned.
- **Personal skills:**
  - The key is `personal/<name>`, so a personal skill sits beside a shared skill of the same name and never replaces it.
  - Instruction-only: Markdown or text files only, no scripts, no binaries, no links.
  - The front matter allow-list is `name`, `description`, `license`, `compatibility` and `metadata`. `allowed-tools`, `model`, `tools` and anything else is refused, so a personal skill cannot widen anything.
  - At most 3 personal skills are inlined per agent, each at most 4000 estimated tokens. Memory notes are inlined up to 2000 tokens.
  - Personal content is used only when the CLI user is the run's starter.
- **Ledger record.** Each skill or memory use is one entry in `ext.agentic.skills` on the step's `step_finished` event: `<kind>/<name>/<version>/<sha256 hex>`, normalised to the declared pattern, at most 24 per event. The entry travels to the control plane through the existing sink, so no new endpoint is needed.
- **Provider-agnostic.** There are no vendor names in core, policy or CLI flags.
- **Formatting, CI and records:**
  - never add `# fmt: skip` or `# fmt: off`; line length 100;
  - check pytest's exit code directly;
  - `uv run python scripts/check.py --all` must be green;
  - TRACKER.md, DEFERRED.md and DECISIONS are updated by the coordinator from this track's output.

## Review Focus

1. **A missing, refused or broken scanner is never a pass.** The cases are: no pin; a pin with no artefact for this platform; Windows with an unsigned artefact; pinned but not installed; a tool that crashes. Each gives a `not-run` or `error` row with a reason and `findings: null`, and the agent block says it did not run. Tests: Task 5 `test_missing_tool_is_not_run_with_reason`, `test_unsigned_windows_build_is_skipped_on_windows`, Task 6 `test_render_lists_tools_that_did_not_run`.
2. **Each seeded problem gives a finding.** A seeded secret is found by the built-in check, and by gitleaks through Zyroflo's parser with the matched text never in the report. A known-vulnerable dependency is found by osv-scanner through Zyroflo's parser. A blocked licence is found by the built-in check. A new `.gitignore` rule is found too. Tests: Task 4 and Task 5.
3. **Offline by default.** The osv-scanner command carries the offline flags unless policy opts in. Test: Task 5 `test_osv_runs_offline_unless_policy_opts_in`.
4. **Planted PII is masked before ingest, and the chain still verifies at the control plane.** A PII hit in an already-chained entry blocks delivery: the entry stays queued and is audited, and nothing is sent. Tests: Task 3 `test_planted_pii_is_masked_before_ingest_and_chain_verifies`, `test_unmaskable_chained_pii_blocks_delivery`.
5. **Personal skills:**
   - used only in the owner's runs;
   - added beside a shared skill of the same name, never replacing it;
   - blocked in a restricted repo;
   - a script or a widening front matter key is refused;
   - the ledger shows kind, name, version and hash.

   Tests: Task 7 and Task 8.
6. **Zyroflo shim safety.** `prepass.binary` and `sca._invoke` are restored after each call, even when the call raises. Test: Task 5 `test_shim_restores_zyroflo_functions`.

---

### Task 1: PII detector (M2-49)

**Files:** Create `core/src/agentic/security/pii.py`. Test `core/tests/security/test_pii.py`.

**Interfaces:**
- `PiiFinding(entity: str, start: int, end: int)`.
- `ENTITIES = ("email", "phone", "credit-card", "us-ssn", "uk-nino", "iban")`, all on by default.
- `PiiDetector(entities: Iterable[str] | None = None)` with `findings(text) -> list[PiiFinding]`, sorted and with overlaps merged.

- [ ] **Step 1:** Failing tests:
  - `test_finds_each_entity`;
  - `test_card_needs_luhn`;
  - `test_bare_nine_digits_is_not_ssn`;
  - `test_ids_and_hashes_are_not_pii` (run ids, sha256 hex, timestamps, versions);
  - `test_unknown_entity_refused`;
  - `test_long_input_is_linear`.
- [ ] **Step 2:** Run; it fails because there is no module.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run; it passes.
- [ ] **Step 5:** Commit `feat(core): PII detector for the publish scan (M2-49)`.

### Task 2: Publish guard and policy (M2-49)

**Files:**
- Create `core/src/agentic/security/publish_scan.py`.
- Modify `core/src/agentic/policy/model.py` (`PublishScanPolicy`, `OrgPolicy.publish_scan`) and `core/src/agentic/policy/load.py` (org.yaml `publish_scan`).
- Test `core/tests/security/test_publish_scan.py`.

**Interfaces:**
- `PublishScanPolicy(action: "mask" | "block" = "mask", pii: list[str] | None = None, identity_fields: list[str] = ["approver", "owner", "actor"])`.
- `PublishScanEvent(action, where, findings, run_id="", seq=0, fields=())` with `fields()`.
- `PublishBlocked(SinkError)`.
- `PublishGuard(scanner: SecretScanner, pii: PiiDetector, action, identity_fields, on_event)`, with:
  - `from_policy(policy: OrgPolicy, on_event=None)`;
  - `screen(event: str, fields: dict) -> dict`;
  - `check(run_id: str, entries: Sequence[Mapping]) -> None`;
  - `findings(value) -> Counter`.

- [ ] **Step 1:** Failing tests:
  - `test_screen_masks_secret_and_pii_as_ids`;
  - `test_screen_under_block_leaves_fields_for_the_sink`;
  - `test_identity_and_structural_fields_are_not_scanned`;
  - `test_check_raises_publish_blocked_naming_seq_and_kinds_never_values`;
  - `test_audit_event_has_counts_only`;
  - `test_policy_loads_from_org_yaml`.
- [ ] **Step 2:** Run; it fails.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run; it passes.
- [ ] **Step 5:** Commit `feat(core): publish guard masks or blocks secrets and PII before central publish (M2-49)`.

### Task 3: Wire the publish scan (M2-49)

**Files:**
- Modify `core/src/agentic/telemetry/sinks.py` (`ForwardingRecorder(screen=...)`), `cli/src/agentic_cli/control_plane.py` (`ControlPlaneSink(guard=...)`), `cli/src/agentic_cli/runtime.py` and `cli/src/agentic_cli/commands/sync.py`.
- Test `core/tests/telemetry/test_sinks_screen.py` and `cli/tests/test_publish_scan_wiring.py`.

- [ ] **Step 1:** Failing tests:
  - `test_recorder_screens_fields_before_the_ledger`;
  - `test_planted_pii_is_masked_before_ingest_and_chain_verifies` (org runtime, fake control plane, `verify_entries` on what arrived) (RF4);
  - `test_unmaskable_chained_pii_blocks_delivery` (a ledger entry written past the screen, then `agentic sync`: an error, still queued, nothing sent) (RF4);
  - `test_block_policy_keeps_pii_local`.
- [ ] **Step 2:** Run; it fails.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run; it passes.
- [ ] **Step 5:** Commit `feat: scan run events for secrets and PII before the control plane ingests them (M2-49)`.

### Task 4: Built-in pre-pass checks (M2-48, DF-70, DF-329)

**Files:**
- Create `core/src/agentic/review/__init__.py` and `core/src/agentic/review/prepass.py`.
- Modify `core/src/agentic/policy/model.py` (`ReviewPrepassPolicy`, `OrgPolicy.review_prepass`) and `load.py`.
- Test `core/tests/review/test_prepass_builtin.py`.

**Interfaces:**
- `ReviewPrepassPolicy(enabled: bool = True, agent_types: list[str] = ["code-review"], online_vulnerability_lookup: bool = False)`.
- `added_lines(diff_text) -> list[AddedLine(path, line, text)]`.
- `builtin_checks(diff_text, *, scanner, blocked_licences) -> dict` returns a Zyroflo-shaped document with the tools `secret-scan`, `licence-check` and `ignore-rules`.
- `merge(*documents) -> dict`.
- `render(document) -> str`.

- [ ] **Step 1:** Failing tests:
  - `test_seeded_secret_is_a_finding_without_its_value`;
  - `test_blocked_licence_spdx_tag_and_manifest_field_are_findings`;
  - `test_allowed_licence_is_not_a_finding`;
  - `test_new_gitignore_rule_is_a_finding` (DF-70/DF-329);
  - `test_removed_lines_are_not_scanned`;
  - `test_render_lists_tools_that_did_not_run` (RF1);
  - `test_render_fences_findings_as_untrusted`.
- [ ] **Step 2:** Run; it fails.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run; it passes.
- [ ] **Step 5:** Commit `feat(core): built-in review pre-pass checks (M2-48)`.

### Task 5: External scanners through Zyroflo and the verified tool cache (M2-48)

**Files:**
- Create `commons/src/sdlc_commons/review_prepass.py`.
- Modify `commons/src/sdlc_commons/worktrees.py` (`change_since_base`).
- Test `commons/tests/test_review_prepass.py`.

**Interfaces:**
- `ToolLocator(pins: Mapping[str, PinLike], fetcher: ToolFetcher | None, platform: str)` with `locate(name) -> tuple[Path | None, str]`.
- `external_checks(tree: Path, diff_text: str, locator, *, online: bool = False, rule_dirs=()) -> dict`.
- `change_since_base(target, run_id) -> list[tuple[str, Path, str]]`.

- [ ] **Step 1:** Failing tests:
  - `test_missing_tool_is_not_run_with_reason` (RF1);
  - `test_unsigned_windows_build_is_skipped_on_windows` (RF1);
  - `test_gitleaks_finding_through_zyroflo_is_redacted` (RF2);
  - `test_known_vulnerable_dependency_is_a_finding` (RF2);
  - `test_osv_runs_offline_unless_policy_opts_in` (RF3);
  - `test_shim_restores_zyroflo_functions` (RF6);
  - `test_change_since_base_is_the_runs_diff`.
- [ ] **Step 2:** Run; it fails.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run; it passes.
- [ ] **Step 5:** Commit `feat(commons): review pre-pass scanners via Zyroflo and the verified tool cache (M2-48)`.

### Task 6: The Code Review agent receives the pre-pass (M2-48, DF-228)

**Files:**
- Modify `core/src/agentic/engine/factory.py` (`StepRunners(prompt_extras=...)`).
- Create `cli/src/agentic_cli/step_extras.py`.
- Modify `cli/src/agentic_cli/runtime.py`.
- Test `core/tests/engine/test_factory_extras.py` and `cli/tests/run/test_prepass_wiring.py`.

- [ ] **Step 1:** Failing tests:
  - `test_extras_end_the_instructions_when_the_step_runs`;
  - `test_code_review_step_gets_prepass_findings` (seeded secret in a run tree; the review agent's block names it, and a developer step gets none);
  - `test_prepass_off_by_policy`.
- [ ] **Step 2:** Run; it fails.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run; it passes.
- [ ] **Step 5:** Commit `feat: the Code Review agent receives the deterministic pre-pass (M2-48)`.

### Task 7: Personal skills and memory (M2-45)

**Files:**
- Create `core/src/agentic/skills/personal.py`.
- Modify `core/src/agentic/policy/model.py` (`ClassificationRule.personal_skills`, `personal_memory`).
- Test `core/tests/skills/test_personal.py`.

**Interfaces:**
- `PersonalSkill(name, key, version, sha256, description, body, agents)` and `MemoryNote(name, sha256, text)`.
- `load_personal(home: Path, *, shared: Collection[str]) -> PersonalSet(skills, memory, rejected)`.
- `PersonalContext(personal_set, classifications: Mapping[str, ClassificationRule], record)` with `prompt_for(agent) -> str`.

- [ ] **Step 1:** Failing tests:
  - `test_instruction_only_skill_loads_with_hash_and_version`;
  - `test_script_or_binary_is_refused`;
  - `test_widening_front_matter_is_refused`;
  - `test_same_name_as_shared_is_added_alongside`;
  - `test_blocked_in_restricted_classification`;
  - `test_memory_notes_inlined_within_budget`;
  - `test_metadata_agents_targets_agents`;
  - `test_every_use_is_recorded_with_kind_name_version_hash`.
- [ ] **Step 2:** Run; it fails.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run; it passes.
- [ ] **Step 5:** Commit `feat(core): personal skills and memory, instruction-only, beside shared skills (M2-45)`.

### Task 8: Owner-only use and the ledger record (M2-45, DF-491)

**Files:**
- Modify `commons/src/sdlc_commons/ledger.py` (`record(..., skills=())`).
- Create `cli/src/agentic_cli/skill_activity.py`.
- Modify `cli/src/agentic_cli/step_extras.py` and `runtime.py`.
- Test `commons/tests/test_ledger_skills.py` and `cli/tests/run/test_personal_wiring.py`.

- [ ] **Step 1:** Failing tests:
  - `test_skills_ride_ext_and_verify`;
  - `test_step_finished_carries_activations` (the ledger shows the hash, and the control-plane sync carries it);
  - `test_personal_only_in_owners_runs`;
  - `test_blocked_in_restricted_repo_end_to_end`.
- [ ] **Step 2:** Run; it fails.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run; it passes.
- [ ] **Step 5:** Commit `feat: personal skill and memory use recorded in the run ledger, owner's runs only (M2-45)`.

### Task 9: Close-out

- [ ] Run `uv run python scripts/check.py --all` until green, and grep the touched files for `fmt:`.
- [ ] Prepare the tracker notes, the deferred items and the decisions for the coordinator.

## Deferred items this plan pulls in

| DF | Plan |
|---|---|
| DF-70, DF-329 | Done in Task 4 (ignore-rule check) |
| DF-228 | Done in Task 6. Every pipeline with a Code Review agent gets the pre-pass when that step runs, with no pipeline action step needed |
| DF-491 | Partly: activations sync as ledger events (Task 8). Backing up the content of personal skills needs a control-plane endpoint, which is out of this track; retargeted |
| DF-152, DF-159 | Egress-scanner coverage (entropy, gitleaks rules as data). Here, gitleaks covers review when it is installed. The egress widening needs a performance budget on every model call; retargeted |
| DF-160, DF-452 | TokenLean repository work; retargeted |
| DF-100 | Shell deny-rule defaults (guard, not pre-pass); retargeted to M2-58 |
