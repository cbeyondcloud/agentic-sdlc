# M2 Integrations Implementation Plan (M2-50, M2-41, M2-42)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three vertical slices. (1) **Jira (M2-50):** `agentic run <pipeline> --from-jira PROJ-123` reads the issue from Jira Cloud or Data Center, turns it into the run's request through the Zyroflo connector mapping (`sdlc_commons.connectors`), comments the run link back on the issue and, when asked, transitions it. (2) **Runner conformance (M2-41):** one conformance suite, parametrised over the native, claude-code, codex and opencode backends with their existing scripted stand-ins, grouped in named tiers; a backend failing a tier is reported per backend and tier, and the capability matrix decides which tiers apply. (3) **Egress (M2-42):** org policy names the hosts a sandbox may reach (package registries; the TokenLean host is added for an agent process), the names are validated, the srt wrapper enforces them, and a live test proves a blocked host is refused inside the sandbox.

**Architecture:**
- **Jira.**
  - `core/src/agentic/integrations/jira.py` (new, core): `JiraClient` over `httpx2`, the same shape as `agentic.hosting.github`: https only (plain http to loopback for fakes), no redirects, every request kept on the configured origin, the token never in a repr or a message. Cloud and Data Center through REST v2 by default (both serve it); `api_version="3"` sends comments as ADF.
  - `commons/src/sdlc_commons/connectors.py`: `item_for` flattens an Atlassian Document Format body to its text before Zyroflo's `spec_for` (DF-462), so a v3 payload (or webhook) gives readable text and a stable identity.
  - `cli/src/agentic_cli/jira.py` (new): reads the `jira:` user config (`url`, `user`, `credential`, `api_version`, `transition`; `AGENTIC_JIRA_URL` / `AGENTIC_JIRA_USER` override), gets the token from `agentic.auth.Credentials` (`AGENTIC_JIRA_TOKEN`, then the keyring; `agentic login --for jira`), builds the request input (a header line plus the specification fenced as untrusted data, M1-42), and posts the run comment.
  - `cli/src/agentic_cli/commands/runs.py`: `--from-jira KEY` and `--jira-transition NAME` on `agentic run`. The issue is read before preflight (a dry run reads it too and changes nothing). After the run stops running, one comment carries the run id, status, cost and link; a failed comment or transition is a warning, never a failed run. `jira.json` in the run folder records what was posted (no secret).
- **Conformance.**
  - `commons/src/sdlc_commons/conformance.py` (new): Zyroflo's runner evidence tiers (`tools.provider_smoke.RUNNER_TIERS`: null < contract < agent) behind the framework's names. Scripted stand-ins are `contract` evidence; `agent` evidence needs live models (owner decision 73: later).
  - `core/src/agentic/backends/capabilities.py`: `BackendCapabilities.own_process` (the backend starts an agent process of its own; native: False). The matrix stays the one place that says what a backend does.
  - `core/tests/backends/conformance.py`: the cases grouped as `TIERS`: `output` (contract, retry, invalid, text), `limits` (budget, time) and `containment` (the agent process is adopted into the step's process scope and gone when it closes, DF-472; applies when `own_process`).
  - `core/tests/backends/test_conformance.py` (new): one suite, parametrised backend x tier x case, with one harness per backend (native: Pydantic AI `FunctionModel`; claude-code: the bundled pinned binary against the TokenLean stub; codex: `fake_codex.py`; opencode: the OpenCode ACP profile against `acp_fake_agent.py`). The per-backend copies of the conformance loop are removed; the pinned-binary contract modules (codex, opencode, Linux container) keep running the same `CASES`.
  - `core/tests/backends/conftest.py`: a terminal summary table (backend x tier: pass, FAIL, n/a, skipped) after any run that collected conformance tests.
- **Egress.**
  - `core/src/agentic/policy/model.py`: `SandboxPolicy.allowed_domains` entries are validated host names (`host`, `*.host`, optional `:port`, lower-cased; never a URL, `*` or a path); `SandboxPolicy.pass_env` lists environment variables an org lets through to agent commands (DF-365), never `AGENTIC_*`.
  - `core/src/agentic/sandbox/egress.py` (new): `egress_hosts(policy, gateway_url=None)`: the registries, plus the gateway's host (and port when not the default) for a whole agent process.
  - `core/src/agentic/sandbox/process.py`: `safe_env(..., pass_through=())`; `SrtSandbox` and `LocalProcessSandbox` take `pass_env`.
  - `cli/src/agentic_cli/wiring.py`: shell commands get the registries and `pass_env`; `StepWiring.process_wrap_for(step, gateway_url=..., write=...)` is the srt command prefix for a whole agent CLI process (TokenLean plus registries, the guard's write denials, `proxy_everything`), the recipe DF-443 describes, ready for the backend wiring row.

**Tech Stack:** Python 3.12, httpx2 (already core, D36), stdlib `http.server` fakes, pytest. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md` §9.1 (contract tests per backend), §9.2 (egress limited to TokenLean and allow-listed registries), §15 (untrusted content, corporate network), §16 (Jira v1: read issue, post run link, optional transition; connector mappings), §20 (conformance tiers). Master plan rows M2-41, M2-42, M2-50.

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| Jira REST | our thin `httpx2` client (4 endpoints), the GitHub adapter's pattern | `jira` (pycontribs, BSD-2) and `atlassian-python-api` (Apache-2.0) are both `requests`-based: a second HTTP stack that bypasses `agentic.transport.ssl_context` (corporate CA, proxies) and pulls oauthlib/bs4 etc. for four calls |
| Issue -> run request | Zyroflo `connect.spec_for/mapping_for/dig` via `sdlc_commons.connectors` (M2-27) | - |
| ADF to text | a 30-line walk over text nodes | no maintained, permissively licensed ADF reader small enough to justify a dependency |
| Jira response fixtures | `httpx2.MockTransport` with JSON fixtures written from Atlassian's documented Cloud v2/v3 and Data Center v2 shapes (not captured from an instance; a live sandbox run is M3-25) | vcrpy/pytest-recording patch `httpx`/`requests`, not `httpx2`; and no live Jira may be called (decision 73) |
| Conformance vocabulary | Zyroflo `provider_smoke.RUNNER_TIERS` through `sdlc_commons.conformance` | Zyroflo's `provider_smoke` runs its impact-review workflow, not our runners; only its tier vocabulary and ranking apply |
| Egress enforcement | `srt` allowedDomains (already pinned, D38) | - |

## Global Constraints

- **No live calls** to Jira, models or registries (owner decision 73). Jira contract tests use documented response shapes (hand-written from Atlassian's API docs, not captured; M2-50 is done at shape level and a live sandbox run is M3-25); the CLI end-to-end uses a local fake Jira.
- **Provider-agnostic.** No model vendor names in the engine, policy, CLI flags or protocols. Jira is an integration (spec §16), named only in the integration modules and the `--from-jira` flags.
- **Token handling.** The Jira token comes only from `Credentials` (env, then keyring), is never logged, never in a repr, error, `jira.json` or run state, and is sent only to the configured origin.
- **Untrusted content.** Ticket text reaches agents only inside `<untrusted-data>` (M1-42).
- **Cross-platform.** Paths via `pathlib`; JSON written with `newline="\n"`; the live srt test is skipped on native Windows and wherever srt is missing, with the reason (Windows: inside WSL2, D38).
- **Layering.** core never imports commons, the CLI or Zyroflo; Zyroflo only through `sdlc_commons`; `agentic.integrations.jira` only through `agentic_cli.jira` (new import-linter contract, like the hosting one).
- **Formatting, CI and records:** never `# fmt: skip` / `# fmt: off`; line length 100; pytest exit codes checked directly; `uv run python scripts/check.py --all` green on Windows and Linux; TRACKER.md, DEFERRED.md and DECISIONS are updated by the coordinator from this track's output.

## Review Focus

1. **The Jira token never leaves its lane**: not in a repr, an error message (even when the server echoes the request), a log record, `jira.json` or run state; never sent to another origin (redirects not followed, a cross-origin URL refused). Tests: Task 2 `test_the_token_is_scrubbed_from_errors_and_never_logged`, `test_redirects_are_not_followed`; Task 4 `test_the_token_is_nowhere_in_the_run_folder`.
2. **Ticket text is data**: the run's request holds the issue only inside an `<untrusted-data>` fence, and a ticket that writes a closing fence tag cannot escape it. Test: Task 4 `test_a_ticket_cannot_close_the_untrusted_fence`.
3. **A Jira failure after the run started never fails the run**, and one before it starts starts nothing. Tests: Task 4 `test_a_failed_comment_or_transition_is_a_warning`, `test_an_unreadable_issue_starts_nothing`.
4. **A blocked host is refused inside the sandbox** while the TokenLean host is reached, and the blocked server never sees a request. Test: Task 6 `test_a_host_outside_the_egress_list_is_refused_inside_srt` (Linux container; skipped with the reason elsewhere).
5. **Egress entries cannot open everything**: `*`, a URL, a path or a bare wildcard label are refused at policy load; `pass_env` can never let `AGENTIC_*` through. Tests: Task 5.
6. **A non-conforming backend is caught and reported per tier**: a runner that ignores the time limit fails `limits`, one that forgets to adopt its process fails `containment`, and the summary names the backend and tier. Tests: Task 7 `test_a_runner_ignoring_its_time_limit_fails_the_limits_tier`, `test_a_runner_that_never_adopts_fails_containment`, `test_the_summary_names_each_backend_and_tier`.

---

### Task 1: ADF bodies become text (DF-462)

**Files:**
- Modify: `commons/src/sdlc_commons/connectors.py`
- Test: `commons/tests/test_connectors.py`

**Interfaces:**
- `adf_text(node: Any) -> str`: the text of an Atlassian Document Format node (paragraphs and headings on their own lines, list items prefixed `- `, hard breaks as newlines, mentions/emoji/cards by their text).
- `item_for(...)`: when the mapped `body` value is an ADF document (`{"type": "doc", ...}`), the payload is copied with the body replaced by `adf_text` before `spec_for`.

- [ ] **Step 1: Write the failing tests** `test_a_v3_adf_description_becomes_text` (no `{'type':` in the spec; the paragraph text and list items present), `test_adf_identity_is_stable_and_differs_from_the_dict_repr`, `test_a_v2_string_body_is_unchanged`.
- [ ] **Step 2: Run** `uv run pytest commons/tests/test_connectors.py -q`; expect the ADF tests to fail (dict repr in the spec).
- [ ] **Step 3: Implement** `adf_text` and the payload copy.
- [ ] **Step 4: Run** again; expect pass. Commit.

### Task 2: The Jira REST client (core)

**Files:**
- Create: `core/src/agentic/integrations/__init__.py`, `core/src/agentic/integrations/jira.py`
- Create: `core/tests/integrations/fixtures/jira/*.json` (Cloud v2 issue, Cloud v3 issue with ADF, Data Center v2 issue, transitions, comment created, 404 and 401 errors - the documented REST response shapes)
- Test: `core/tests/integrations/test_jira_client.py`
- Modify: `pyproject.toml` (import-linter contract)

**Interfaces:**
- `class JiraError(Exception)` with `status: int | None`, `transient: bool`.
- `issue_key(text: str) -> str`: `PROJ-123` upper-cased and checked (`[A-Z][A-Z0-9_]+-[1-9][0-9]*`), else `JiraError`.
- `@dataclass(frozen=True) class JiraTransition: id: str; name: str; to: str`.
- `class JiraClient(base_url, token, *, user=None, api_version="2", network=None, timeout=30.0, transport=None)`; `user` set: Basic `user:token` (Cloud: the account email); else Bearer (Data Center personal access token).
  - `issue(key) -> dict`, `comment(key, text) -> str` (comment id), `transitions(key) -> list[JiraTransition]`, `transition(key, name) -> JiraTransition` (by transition name or target status, case-insensitive; `JiraError` naming the available ones), `browse_url(key) -> str`, `close()`, context manager.

- [ ] **Step 1: Write the failing tests**: Cloud v2 read (path `/rest/api/2/issue/PROJ-123`, Basic header); Data Center Bearer; v3 comment body is ADF, v2 a string; transition by name and by target status; unknown transition names the available ones; 401/403/404/429/5xx explained (transient for 429/5xx, `Retry-After`); https required except loopback; redirects not followed; a context path (`https://jira.acme.example/jira`) kept; `test_the_token_is_scrubbed_from_errors_and_never_logged` (server echoes the Authorization header; caplog has no token); repr has no token; invalid keys refused before any request.
- [ ] **Step 2: Run** `uv run pytest core/tests/integrations -q`; expect ImportError.
- [ ] **Step 3: Implement** `jira.py`; add the import-linter contract "The Jira adapter only through the CLI's Jira module".
- [ ] **Step 4: Run** again; expect pass. Commit.

### Task 3: Jira settings, token and login (CLI)

**Files:**
- Create: `cli/src/agentic_cli/jira.py`
- Modify: `cli/src/agentic_cli/commands/login.py` (`--for jira` stores `jira_token`)
- Test: `cli/tests/test_jira_settings.py`, `cli/tests/test_login.py`

**Interfaces:**
- `JIRA_TOKEN = "jira_token"`; `@dataclass(frozen=True) class JiraSettings: url, user, credential, api_version, transition`.
- `jira_settings(settings: Mapping, env: Mapping) -> JiraSettings` (CliError INVALID with a config hint for a missing url, a non-mapping, unknown keys, a bad credential name or api_version; Cloud URL without `user` refused with the hint).
- `connect_jira(ws, credentials=None) -> JiraClient` (CliError REFUSED with `agentic login --for jira` when no token).

- [ ] **Step 1: Write the failing tests** for each settings rule, the env override, the missing-token refusal hint, and `login --for jira --key-stdin` storing `jira_token`.
- [ ] **Step 2: Run**; expect failures. **Step 3: Implement.** **Step 4: Run**; expect pass. Commit.

### Task 4: `agentic run --from-jira` end to end

**Files:**
- Modify: `cli/src/agentic_cli/commands/runs.py`, `cli/src/agentic_cli/jira.py`, `cli/src/agentic_cli/publishing.py` (`_title` uses the request's first line)
- Create: `cli/tests/fake_jira.py` (stdlib server: issue, comment, transitions; checks auth; records requests; can echo auth in errors)
- Test: `cli/tests/run/test_jira_run.py`

**Interfaces:**
- `jira_input(pipeline: PipelineDefinition) -> str`: the input that accepts `jira` (`from:` lists it), or `request` for a pipeline declaring none; CliError USAGE otherwise.
- `request_text(item: ExternalItem, key: str) -> str`: `Jira issue PROJ-123` then the spec in `untrusted(..., source="jira PROJ-123")`.
- `run_comment(state, link) -> str`; `report_run(client, key, state, ws, run_folder) -> dict` writes `jira.json`.

- [ ] **Step 1: Write the failing tests**: a run started from a fake Jira issue completes, its request holds the fenced spec, one comment names the run id, status and `agentic status <run>`, `jira.json` records issue/comment id/transition; `--jira-transition "In Progress"` (and the config default) transitions; a dry run reads the issue and posts nothing; `test_an_unreadable_issue_starts_nothing` (404, 401 -> exit 1/2 with a hint, no run folder); `test_a_failed_comment_or_transition_is_a_warning` (run still exit 0); `test_a_ticket_cannot_close_the_untrusted_fence`; `--from-jira` with `--input request=...` is a usage error; a pipeline without a jira input is a usage error; `test_the_token_is_nowhere_in_the_run_folder`; with a control plane configured the comment carries its run link.
- [ ] **Step 2: Run**; expect failures (no option). **Step 3: Implement.** **Step 4: Run**; expect pass. Commit.

### Task 5: Egress policy and environment pass-through (DF-365)

**Files:**
- Modify: `core/src/agentic/policy/model.py`, `core/src/agentic/sandbox/process.py`, `core/src/agentic/sandbox/srt.py`, `core/src/agentic/agents/tools/shell.py`, `templates/standalone/policy/org.yaml` (comment for `pass_env`)
- Create: `core/src/agentic/sandbox/egress.py`
- Test: `core/tests/sandbox/test_egress.py`, `core/tests/sandbox/test_process_env.py`

**Interfaces:**
- `SandboxPolicy.allowed_domains` validator (normalised lower-case); `SandboxPolicy.pass_env: list[str] = []` (names, not `AGENTIC_*`).
- `egress_hosts(policy: SandboxPolicy, gateway_url: str | None = None) -> tuple[str, ...]`.
- `safe_env(environ=None, *, windows=None, pass_through: Collection[str] = ())`.
- `SrtSandbox(rules, settings_dir, *, srt=("srt",), pass_env=())`, `LocalProcessSandbox(pass_env=())`.

- [ ] **Step 1: Write the failing tests**: valid hosts, wildcards, ports accepted and lower-cased; `*`, `https://pypi.org`, `pypi.org/simple`, `*.`, `a*.b`, empty refused; `pass_env` with `AGENTIC_TOKENLEAN_API_KEY` or a bad name refused; `egress_hosts` adds `tokenlean.acme.example` (and `:8443` for a non-default port) only when given, without duplicates; `safe_env(pass_through=["HTTPS_PROXY"])` keeps a credentialed proxy, still drops other secrets, and matches case-insensitively on Windows only; the sandboxes pass the names through.
- [ ] **Step 2: Run**; expect failures. **Step 3: Implement.** **Step 4: Run**; expect pass. Commit.

### Task 6: Egress enforced in the step wiring and inside srt

**Files:**
- Modify: `cli/src/agentic_cli/wiring.py`, `cli/src/agentic_cli/runtime.py` (pass the gateway URL to the wiring)
- Test: `cli/tests/test_wiring.py`, `core/tests/sandbox/test_egress_live.py`

**Interfaces:**
- `StepWiring.gateway_url: str | None`; `StepWiring.process_wrap_for(step, *, write=()) -> list[str]` (refuses without srt).

- [ ] **Step 1: Write the failing tests**: the srt settings a shell step writes carry exactly the policy registries (not the gateway) and the pass-through names reach the sandbox; `process_wrap_for` writes settings with the gateway host plus registries, the guard's write denials and the extra writable folders, and its prefix clears NO_PROXY; it refuses on a machine without srt; live (`skipif` no srt / win32): `test_a_host_outside_the_egress_list_is_refused_inside_srt` (two loopback servers; only the "gateway" one allowed by `egress_hosts`; curl reaches it and is refused by the other, which records no request).
- [ ] **Step 2: Run**; expect failures. **Step 3: Implement.** **Step 4: Run** (the live test runs in the Linux container stage). Commit.

### Task 7: One conformance suite, in named tiers (M2-41, DF-472)

**Files:**
- Create: `commons/src/sdlc_commons/conformance.py`, `commons/tests/test_conformance_tiers.py`, `core/tests/backends/test_conformance.py`
- Modify: `core/src/agentic/backends/capabilities.py` (`own_process`), `core/tests/backends/conformance.py` (`Tier`, `TIERS`, containment case), `core/tests/backends/conftest.py` (summary), `core/tests/backends/test_native.py`, `test_codex_runner.py`, `test_acp_runner.py`, `test_claude_code_contract.py` (their conformance loops move to the suite)

**Interfaces:**
- commons: `RUNNER_TIERS`, `rank(tier) -> int`, `satisfies(have, need) -> bool`, `SCRIPTED = "contract"`.
- tests: `@dataclass(frozen=True) class Tier: name, summary, cases, applies: Callable[[BackendCapabilities], bool]`; `TIERS`; `CASES` (every case, for the pinned-binary modules); `verdicts(results) -> dict[str, dict[str, str]]`; `summary_lines(verdicts, evidence) -> list[str]`.

- [ ] **Step 1: Write the failing tests**: the suite's backends are exactly `CAPABILITIES`'s; every backend x tier x case runs or is n/a by the matrix; containment: the agent process is in the step scope and gone after it closes; `test_a_runner_ignoring_its_time_limit_fails_the_limits_tier`; `test_a_runner_that_never_adopts_fails_containment`; `test_the_summary_names_each_backend_and_tier`; commons tier ranking.
- [ ] **Step 2: Run**; expect failures. **Step 3: Implement**; remove the per-backend loops. **Step 4: Run**; expect pass. Commit.

### Task 8: Conformance-related deferred fixes

**Files:** `core/src/agentic/backends/codex.py` (DF-432, DF-433, DF-431), `core/src/agentic/backends/capabilities.py` (DF-431 wording), `core/src/agentic/backends/acp/profile.py` (DF-447), `core/src/agentic/backends/acp/runner.py` (DF-445), `core/src/agentic/backends/claude_code.py` (DF-481), `core/src/agentic/handoffs/kinds.py` (DF-371); tests beside each.

- [ ] **Step 1: Write the failing tests**: an empty answer without a contract is not a completed step on any backend (a conformance case in `output`); codex errors carry no decision ids and say how to enable the sandbox; `repr(Launch)` hides file contents; `AcpBackend` without `rules_for`/`wrap_for` refuses unless `unsandboxed=True`; claude-code adoption gives up after a deadline with a warning; the test-report messages name duplicates and count mismatches separately.
- [ ] **Step 2: Run**; expect failures. **Step 3: Implement.** **Step 4: Run**; expect pass. Commit.

### Task 9: Definition of done

- [ ] `uv run python scripts/check.py --all` green (Windows + Linux container). Re-run once on a timing flake and report it.
