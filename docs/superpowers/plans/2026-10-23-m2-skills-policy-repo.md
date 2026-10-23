# M2 Skills Authoring, Superpowers Opt-in and Policy-Repo Template Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rows M2-46, M2-47 and M2-51.

- **M2-46.** A developer captures experience as a personal skill and asks for it to be promoted:
  - `agentic skills new` scaffolds `~/.agentic/skills/<name>/SKILL.md`;
  - `validate` checks format, size, secrets, scripts and permission widening;
  - `eval` runs an agent with and without the skill through the M1-63 harness;
  - `submit --to repo|team|org` opens a pull request with the eval report;
  - `review` lets the scope owner list submissions and approve them, behind an RBAC check;
  - `status` retires the personal copy once the shared one is merged.
- **M2-47.** A team may enable the full superpowers plugin, pinned in the org catalog, for claude-code and codex steps only. The native loop never receives it.
- **M2-51.** A policy-repo template that loads, passes the vendor-name guard and maps CODEOWNERS as D33 says. The control plane's policy sync accepts it on a fresh tenant.

**Architecture:**

- **Core `agentic.skills`** (no commons, CLI or Zyroflo imports):
  - `admission.py`: the static scan becomes one function, `scan_text(text) -> list[(line, reason)]`, used by catalog admission and by personal skills (DF-142):
    - Unicode `Cf`/`Cc` characters (except tab, LF, CR) and the variation-selector supplement count as hidden;
    - line continuations are joined before matching;
    - new download-and-run shapes: `bash <(curl ...)`, `sh -c "$(curl ...)"`;
    - new injection phrases: "forget everything above", an HTML-comment `SYSTEM:` payload.
  - `authoring.py` (new):
    - `scaffold(home, name, description)`;
    - `check_skill(folder, scope, *, limits, licences) -> SkillCheck`: every problem found, not just the first;
    - `promotion(scope, name, team) -> Promotion`: the target paths, and the catalog entry text appended to a catalog file without losing its comments.
  - `personal.py` gains `skill_block(skill)`, the inlined text shared by runs and `skills eval`.
  - `plugins.py` (new, M2-47):
    - `PluginEntry` in the catalog's new `plugins:` section;
    - `plugins_for(agent, team_policy, catalog, root)`: `[]` unless the backend can load plugins and the team enabled the plugin;
    - `install_plugin(source, dest, entry)`: a pinned, scanned copy with `exclude` globs removed. The visual companion is excluded.
  - `gate.py` (new, DF-367): `check_policy_repo(root)`. It runs the load, definitions and admission (with org policy's skill licences and limits) of a policy repo tree. It is the policy-repo pull request gate.
- **Policy model:**
  - `TeamPolicy.backend_plugins: list[str] = []`;
  - `OrgPolicy.skill_licences: list[str] | None`, from `licences.yaml` `skills:`;
  - `OrgPolicy.skill_limits: SkillLimits`, from `limits.yaml` `skills:`;
  - `BackendCapabilities.plugins: bool` (true for claude-code and codex). This is data, so no backend name appears in the engine.
- **Backends:**
  - `claude_code.lock_down(..., plugins=)` adds `plugins=[{"type": "local", "path": ...}]`, copied into the step's own `CLAUDE_CONFIG_DIR`.
  - `codex.lock_down(..., plugins=)` copies each plugin's `skills/*` into the step's own `CODEX_HOME/skills/` and turns `skills.include_instructions` on. `features.plugins` stays false, which keeps the chatgpt.com/github.com fetches off and closes DF-118. A worktree that carries repo skill folders is refused while a plugin is on, because Codex would list them too.
  - The backends take `plugins_for(step)`. The native backend has no such hook.
- **Hosting.** `GitHubHost` gains `list_pull_requests`, `get_pull_request` (with `merged`) and `review_pull_request` (approve or request changes), behind a new small `ReviewHost` protocol. `agentic_cli.hosting.connect_to(repo_ref, settings)` connects without a checkout. `repo_setup.commit_on_branch` takes a `branch` (default unchanged).
- **CLI** (`commands/skills.py`, one COMMANDS line; its commands are added to the existing `skills` group, now created in `catalog.py` as `skills_app`):
  - `new NAME [--description]`;
  - `validate [NAME|PATH...] [--scope]`, default every personal skill (DF-526 half);
  - `eval NAME --agent A [eval-run flags]`, which writes `skill-eval.json` beside the report under `~/.agentic/skills-eval/<name>/<report-id>/`;
  - `submit NAME --to repo|team|org [--team T] [--report DIR] [--policy-checkout DIR]`, which records `~/.agentic/skills-submitted/<name>.json`;
  - `review [--to S] [--team T]`, `review approve N --to S`, `review decline N --to S --reason R`;
  - `status`.
  - The RBAC permission per scope is checked through the control plane's `/v1/rbac/check`, the authorizer `init --repo` already uses:

    | Scope | Permission |
    |---|---|
    | repo | `agent.approve.repo` |
    | team | `agent.publish.team` |
    | org | `skills.approve` |

    In standalone mode there is no RBAC, so the command warns and proceeds, as gate approvals do. In org mode without a control plane it refuses.
  - `agentic policy check [PATH]` runs the gate.
- **Template** `templates/policy-repo/`:
  - `policy/` (every policy file, the audit-fix deny rules (DF-277/347), `sandbox.pass_env`, skill licences and limits, a `restricted` class with personal skills and memory off (decision 89), teams with limits);
  - `agents/`, `pipelines/` (the library's), `teams/payments/{pipelines/,pipelines/overlays/,skills/catalog.yaml}`;
  - `skills/catalog.yaml`, with the superpowers plugin as a commented example;
  - `CODEOWNERS`, `.github/workflows/policy-check.yml`, `README.md` for org admins.
- **Control plane.** `validate_policy_tree` also loads `teams/<team>/skills/catalog.yaml`. A new test syncs the template on a fresh tenant.

**Tech Stack:** Python 3.12, no new dependency. Reused: `skills-ref` (Apache-2.0, already the `skills` extra), detect-secrets through `agentic.security.secret_scan.SecretScanner`, the M1-63 eval harness (`run_eval`, `build_report`, `assign_labels`, cassette replay), the M1-58 hosting adapter, `python-codeowners` (MIT, already a control-plane dependency) in the CODEOWNERS test, and stdlib `unicodedata`.

**Spec:** §24 "Custom skills", "Superpowers"; §4.3 policy repo; §23 permission matrix; D28, D30, D33; decisions 54, 73, 89.

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| SKILL.md format | `skills-ref` `validator.validate` when installed, else our own checks (name, description, allowed keys) | — |
| Skill scaffold | the shape of Anthropic `skill-creator`'s `init_skill.py` template (front matter + sections) | its script itself: a 20-line template does not justify a vendored script needing a reviewer (decision 54) |
| Secret check | detect-secrets via our `SecretScanner` | gitleaks: a native binary, not allowed on Windows hosts |
| Eval with/without | M1-63 `run_eval` + `build_report` with two blinded labels | `run_ab`: its pre-registered plan, calibration and PROMOTE rule are the A/B promotion gate (DF-387, deferred) |
| Pull requests and reviews | our `GitHubHost` (httpx2) | PyGithub (LGPL) |
| CODEOWNERS check | `codeowners` (MIT) in the control-plane test | — |
| Catalog edit keeping comments | append-after-`skills:` text edit, verified by reloading | ruamel.yaml round-trip mode (already a dependency): it can re-indent an admin's whole file; a text insert changes only the new lines |

## Global Constraints

- Core never imports `sdlc_commons`, `agentic_cli`, `agentic_cp` or `tools`. Provider-agnostic: no vendor or model names in core, the policy model, CLI flags or the template. The template is checked by the standalone guard's regex.
- **No live models** (decision 73). `skills eval` tests replay recorded cassettes. Every hosting call goes to the local `FakeGitHub`, every control-plane call to `FakeControlPlane`.
- **Windows and macOS:**
  - paths are joined with `Path`, POSIX paths go in git and in catalog keys;
  - the personal folder is moved on retire with `shutil.move` and retried while antivirus holds it;
  - git runs with `core.autocrlf=false` in plumbing and with LF blobs.
- **Formatting and CI:** never add `# fmt: skip` or `# fmt: off`, line length 100, and `uv run python scripts/check.py --all` green.
- TRACKER.md, DEFERRED.md and DECISIONS are not edited (other tracks run in parallel); this track reports into the structured output.

## Review Focus

1. **A personal or repo skill with a script, a binary, a link, `allowed-tools`/`model` in its front matter, a secret, or a disguised injection is refused by `skills validate` and by `submit`**, and nothing is pushed. Tests: Task 2 `test_check_refuses_*`, Task 5 `test_submit_refuses_an_invalid_skill_and_pushes_nothing`.
2. **A non-owner cannot approve.** `review approve` asks `/v1/rbac/check` for the scope's permission and exits 2 when it is refused. No review is posted. Test: Task 5 `test_non_owner_cannot_approve`.
3. **The native loop never receives orchestration skills or plugins.** The plugin is installed only into the claude-code or codex step's own config folder, and only when the team flag is on and the pinned hash matches. A telemetry URL in an included file refuses the plugin. Tests: Task 6.
4. **The submission pull request carries the eval report of exactly this skill version.** The report's skill sha256 must equal the folder's, else submit refuses. Test: Task 5 `test_submit_needs_an_eval_of_this_version`.
5. **The template loads, names no vendor, maps `pipelines/` to org admins and `teams/<team>/` to that team's leads, and the control plane accepts it on a fresh tenant.** Tests: Task 7.
6. **The scan hardening does not refuse any vendored library skill** (their pins are unchanged). Test: the existing `test_library` admission tests.

---

### Task 1: One static scan, hardened (DF-142)

**Files:** Modify `core/src/agentic/skills/admission.py` and `core/src/agentic/skills/personal.py`. Test: `core/tests/skills/test_admission.py` (add).

**Interfaces:** `scan_text(text: str) -> list[tuple[int, str]]` gives (line number, reason) for download-and-run, injection, hidden characters and telemetry URLs. `HIDDEN` is replaced by `hidden_char(text) -> str | None`.

- [ ] Write failing tests:
  - `test_scan_finds_variation_selector_smuggling` (U+E0100);
  - `test_scan_finds_cf_and_cc_characters` (U+061C, U+00AD, U+0007);
  - `test_scan_joins_line_continuations` (`curl x \` then `| sh`);
  - `test_scan_finds_process_substitution_and_command_substitution`;
  - `test_scan_finds_forget_everything_above_and_html_comment_system`;
  - `test_personal_skill_uses_the_same_scan`.
- [ ] Implement `scan_text` and use it in `_scan` and in `_instruction_only`. Run the library admission tests: the vendored skills still pass.
- [ ] Commit.

### Task 2: Authoring core: scaffold, check, promotion layout

**Files:** Create `core/src/agentic/skills/authoring.py`. Modify `personal.py` (`skill_block`). Test: `core/tests/skills/test_authoring.py`.

**Interfaces:**

- `scaffold(home: Path, name: str, description: str | None = None) -> Path`. It raises `SkillsError` on a bad name or an existing folder.
- `Scope = Literal["personal", "repo", "team", "org"]`.
- `check_skill(folder, scope, *, limits=None, licences=None) -> list[Rejection]`. Every scope gets:
  - format (skills-ref when installed, else our own name, description and allowed-keys checks);
  - size (personal: `MAX_SKILL_TOKENS`; others: the admission limits);
  - secrets (`SecretScanner(detectors=None)`);
  - the scan.

  Personal and repo scope also get: instruction-only (no scripts, binaries or links), and no widening keys. Team and org scope get the catalog admission rules: a script needs `scripts_reviewed_by`.
- `PROMOTION_BRANCH = "agentic/skill/<name>"`.
- `promotion_paths(scope, name, team) -> tuple[str, str | None]`, giving the skill folder and the catalog file:

  | Scope | Skill folder | Catalog file |
  |---|---|---|
  | repo | `.agentic/skills/<name>` | — |
  | team | `teams/<team>/skills/internal/<name>` | `teams/<team>/skills/catalog.yaml` |
  | org | `skills/internal/<name>` | `skills/catalog.yaml` |

- `add_catalog_entry(text: str | None, key, entry: dict) -> str`: appends after `skills:`, keeps comments, and is verified by `load_yaml`.
- `skill_files_for_commit(folder) -> dict[str, bytes]`: junk files skipped, CRLF turned into LF.

- [ ] Write failing tests:
  - `test_scaffold_writes_a_valid_personal_skill`;
  - `test_scaffold_refuses_an_existing_or_bad_name`;
  - `test_check_refuses_a_script_in_personal_and_repo_scope`;
  - `test_check_refuses_widening_front_matter`;
  - `test_check_refuses_a_secret`;
  - `test_check_refuses_oversized`;
  - `test_check_reports_every_problem`;
  - `test_team_scope_needs_a_script_reviewer`;
  - `test_add_catalog_entry_keeps_comments_and_other_entries`;
  - `test_add_catalog_entry_into_an_empty_catalog`;
  - `test_promotion_paths`.
- [ ] Implement. Commit.

### Task 3: `skills eval` through the harness

**Files:** Modify `cli/src/agentic_cli/evaluation.py` (`RunOptions.skill`, two blinded runners, `skill_comparison`). Test: `cli/tests/test_skills_eval.py`.

**Interfaces:**

- `SkillVariant(name, sha256, block)`.
- With `RunOptions.skill` set, `execute_run` runs labels from `assign_labels(["without", "with"], seed)`. The `with` runner's agent instructions end with `block`.
- `skill_comparison(report) -> dict`: per metric, the value without, the value with and the delta; mean cost and tokens per run.
- `run_skill_eval(...)` writes `skill-eval.json`.

- [ ] Write failing tests. They replay the fixture cassettes, with a copy of the X cassettes for Y.
  - `test_skill_eval_runs_both_variants_and_writes_the_comparison`: two labels, each run once per case, and `skill-eval.json` holds the skill sha.
  - `test_with_runs_carry_the_skill_text`: the cassette server's recorded request body for the `with` label contains the skill body; the `without` one does not.
  - `test_no_network`.
- [ ] Implement. Commit.

### Task 4: Hosting: list, read and review pull requests; connect without a checkout

**Files:** Modify `core/src/agentic/hosting/base.py` (`ReviewHost`, `PullRequest.merged`, `title`), `core/src/agentic/hosting/github.py`, `cli/src/agentic_cli/hosting.py` (`connect_to`), `cli/src/agentic_cli/repo_setup.py` (`branch=`) and `cli/tests/fake_github.py`. Test: `core/tests/hosting/test_github_reviews.py`.

- [ ] Write failing contract tests against `FakeGitHub`:
  - `test_list_pull_requests_returns_open_ones_with_bodies`;
  - `test_get_pull_request_reports_merged`;
  - `test_review_posts_approve_and_request_changes`.
- [ ] Implement. Commit.

### Task 5: CLI `agentic skills new|validate|eval|submit|review|status`

**Files:** Create `cli/src/agentic_cli/commands/skills.py` and `cli/src/agentic_cli/skill_promotion.py` (the git, PR and record logic). Modify `commands/__init__.py` and `commands/catalog.py` (`skills_app`). Test: `cli/tests/test_skills_commands.py`.

- [ ] Write failing tests:
  - `test_new_then_validate_passes`;
  - `test_validate_defaults_to_every_personal_skill_and_exits_3_on_a_bad_one`;
  - `test_submit_to_repo_opens_a_pr_with_the_report`: the branch is `agentic/skill/<name>`, it adds only `.agentic/skills/<name>/...`, and the PR body holds the report and a submission block;
  - `test_submit_to_org_appends_the_catalog_entry_in_the_policy_checkout`;
  - `test_submit_needs_an_eval_of_this_version`;
  - `test_submit_refuses_an_invalid_skill_and_pushes_nothing`;
  - `test_review_lists_pending_submissions`;
  - `test_non_owner_cannot_approve` (org mode, `FakeControlPlane.allowed` empty: exit 2, no review);
  - `test_owner_approves_with_the_scope_permission` (the check body names `skills.approve`, and an APPROVE review is posted);
  - `test_status_retires_the_personal_copy_after_merge`;
  - `test_status_keeps_it_while_open_or_declined`.
- [ ] Implement. Commit.

### Task 6: Superpowers plugin opt-in (M2-47, DF-118)

**Files:** Modify `core/src/agentic/skills/catalog.py` (`PluginEntry`, `Catalog.plugins`), `core/src/agentic/policy/model.py` (`TeamPolicy.backend_plugins`), `core/src/agentic/backends/capabilities.py` (`plugins`), `claude_code.py` and `codex.py`. Create `core/src/agentic/skills/plugins.py`. Test: `core/tests/skills/test_plugins.py`.

- [ ] Write failing tests:
  - `test_plugin_only_when_team_flag_on`;
  - `test_native_backend_never_gets_plugins`;
  - `test_hash_mismatch_refused`;
  - `test_telemetry_url_refuses_the_plugin`;
  - `test_excluded_visual_companion_is_not_installed`;
  - `test_claude_code_options_carry_the_local_plugin_in_the_step_config_dir`;
  - `test_codex_home_gets_the_plugin_skills_and_plugins_feature_stays_off`;
  - `test_codex_refuses_plugin_with_repo_skill_folders`;
  - `test_library_catalog_excludes_orchestration_skills`;
  - `test_native_skill_prompt_never_lists_plugin_skills`.
- [ ] Implement. Commit.

### Task 7: Policy-repo template (M2-51, DF-277, DF-347, DF-540, DF-367)

**Files:**

- Create `templates/policy-repo/**`.
- Modify `core/src/agentic/policy/model.py` and `load.py` (`skill_licences`, `skill_limits`).
- Create `core/src/agentic/skills/gate.py`.
- Modify `cli/src/agentic_cli/commands/catalog.py` (`policy check`), `control-plane/src/agentic_cp/policy_sync/validate.py` (team catalogs) and `templates/standalone/policy/org.yaml` (`pass_env`).

Tests: `core/tests/policy/test_policy_repo_template.py`, `control-plane/tests/test_policy_repo_template.py`, `cli/tests/test_policy_check.py`.

- [ ] Write failing tests:
  - `test_template_loads`;
  - `test_no_vendor_names` (the standalone regex);
  - `test_restricted_has_personal_skills_and_memory_off`;
  - `test_org_deny_list_has_audit_fix_rules`;
  - `test_every_team_has_a_policy_file_and_a_codeowners_line`;
  - `test_codeowners_maps_pipelines_to_org_admins_and_teams_to_leads` (codeowners lib);
  - `test_template_validates_with_the_control_plane_validator`;
  - `test_template_syncs_on_a_fresh_tenant` (Postgres);
  - `test_policy_check_passes_the_template`;
  - `test_policy_check_uses_org_skill_licences` (a GPL skill is refused; an org allowing a licence passes it);
  - `test_readme_covers_codeowners_branch_protection_and_bootstrap`.
- [ ] Implement. Re-record the acceptance cassettes if the standalone template change moves their fingerprint (`scripts/record_acceptance.py`; only hashes and fingerprints change). Commit.

### Task 8: Small deferred fixes (DF-389, DF-390, DF-391, DF-549)

- [ ] DF-391: FindingJudge collapses newlines, caps each finding at 500 characters and strips tag-like text. Test first.
- [ ] DF-390: the dev-task validator refuses `' - '` in test ids. Test first.
- [ ] DF-389: `write_eval_manifest.py` validates before replacing (keeps the old bytes on failure). Test first.
- [ ] DF-549: call-flow.md Jira wording, a user doc section for `jira:` and `sandbox.pass_env`, and the egress.py docstring with no vendor names.
- [ ] Commit.

### Task 9: Full CI

- [ ] `uv run python scripts/check.py --all`, in the background, to completion. Fix until green. Report the final lines.

## Deferred from this plan (reported, not built)

- **The A/B promotion gate** for skills: `eval ab --plan` (DF-387, DF-243), calibration extras (DF-250), a two-judge panel (DF-109) and the Benjamini-Hochberg batch (DF-112). The gate needs pre-registered plans and live models (decision 73).
- **LLM scan of skills** (DF-137): needs a live model.
- **Human sampling of finding rulings** (DF-384).
- **Personal skills via `activate_skill` on demand** (DF-526 remainder). Per-run attribution of activations (DF-527, DF-531) belongs with parallel steps.
- **Loading team-scope and repo-scope shared skills at run time.** Submissions land in the layout, but `init` installs only the org catalog.
- **Vendoring the remaining default skills** (DF-134, DF-229, DF-240): waits for a named reviewer and a legal review (decision 54).
- **Policy-repo eval harvest** (DF-110, DF-246, DF-386) and **git hosts in org policy** (DF-288).
