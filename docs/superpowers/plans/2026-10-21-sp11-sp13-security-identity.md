# SP11 Security + SP13 Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nothing secret leaves the machine for a model, text the agent reads stays data, and the gateway key is found without being written anywhere. Every model call through `agentic.transport` is scanned for secrets first and blocked or masked per org policy, with an audit event (M1-41). Repo files, tool output and earlier agents' handoffs reach the model labelled as data, with wrappers that cannot be broken out of (M1-42). The TokenLean key comes from the environment first, then the OS keyring, with an actionable error when neither has it (M1-48).

**Architecture:**
- **New package `agentic.security`:**
  - `secret_scan.py`: `SecretScanner`. It reuses detect-secrets' detector catalogue, i.e. its regex-based plugins' `denylist` patterns. It runs them over whole strings itself, because detect-secrets' `scan_line` has two problems here:
    - it reports only the first regex group (`ghp`, not the GitHub token; the PEM header, not the key);
    - it costs about 0.5 ms per line.
  - `untrusted.py`: `fence` (an escaped wrapper), `untrusted` (`<untrusted-data source="...">`) and the standing `DATA_NOTICE`.
- **Transport.** `make_client` and `make_async_client` always put a scanning transport under the identity hook, so the scan is on by default. It sits above metering, so a blocked call reserves nothing and sends nothing. The scanner is a parameter; `None` means block with the default detectors.
- **Tools.** `build_tools` passes every tool's output through one wrapper:
  1. host paths are scrubbed (DF-23);
  2. secrets are blocked or masked by the same policy (DF-23);
  3. a result that is not a framework refusal is wrapped as untrusted data.

  `activate_skill` is trusted (org-approved instructions) and is not wrapped. `spawn_agent` output is wrapped where the native backend adds it.
- **Runner.** The instructions always carry `DATA_NOTICE`. Handoffs go through `fence`, so a handoff containing `</handoff>` cannot close its wrapper (DF-24).
- **New package `agentic.auth`:** `credentials.py` holds `Credentials`, an env-then-keyring provider. In CI (`CI` set) the keyring is never consulted.
- **Policy.** `OrgPolicy.secret_scan: SecretScanPolicy` (`action: block|mask`, `detectors`) is read from the `org.yaml` key `secret_scan`.

**Tech Stack:** Python 3.12, detect-secrets 1.5.0 (Apache-2.0, pure Python, 2024-05-06), keyring 25.7.0 (MIT, pure Python, 2025-11-16; already in the lock transitively), httpx2, Pydantic AI 2.54.0, pytest.

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md`:
- §13 Identity and credentials
- §15 security (secret scan before egress; untrusted content)
- §16 audit events ("secret scan block")
- layout `security/`, `auth/`

Master plan rows M1-41, M1-42, M1-48; deferred items DF-23, DF-24.

## Global Constraints

- **OSS first.**
  - detect-secrets supplies the detector catalogue. Its regexes are used, never copied.
  - keyring supplies the OS keychain.
  - gitleaks is not used: it would be an unsigned native binary on Windows.
- **Provider-agnostic.**
  - The scanner walks every JSON string in any request body. It knows no provider's message format.
  - The credential provider names no vendor.
- **Default on.** A client built without a scanner still scans (block, default detectors). A tool context without a scanner still scans its tool output.
- **Never log a secret.**
  - Events and errors name detector types and counts only.
  - `Credential.value` has `repr=False`.
- **Package boundaries.** `core` never imports `sdlc_commons`; tests may.
- **Hands off.** The coordinator's files `codeintel/tools.py` and `codeintel/index.py` are not edited.
- **Formatting and CI.**
  - Never add `# fmt: skip`; line length 100.
  - `uv run python scripts/check.py --all` must be green.

## Review Focus

1. **A planted secret in the prompt.** It never reaches the network, and no budget is reserved. Test: Task 3, `test_a_planted_secret_is_blocked_before_any_http_call`.
2. **Mask mode.** The request that leaves has the masked text, never the secret: the whole GitHub token, not just `ghp`, and the whole PEM block, not just its header. Tests: Task 2, `test_mask_covers_the_whole_token` and `test_mask_covers_a_whole_pem_block`; Task 3, `test_the_masked_variant_is_sent`.
3. **A file that says "ignore your instructions".** It arrives inside `<untrusted-data>`, and a forged `</untrusted-data>` in it is escaped. Test: Task 4, `test_an_injected_instruction_in_a_file_stays_data`.
4. **A handoff containing `</handoff>`.** It cannot close its wrapper (DF-24). Test: Task 4, `test_a_handoff_cannot_close_its_wrapper`.
5. **CI with a key in both env and keyring.** Env wins, and the keyring is never touched in CI. Test: Task 5, `test_env_wins_and_ci_never_touches_the_keyring`.

---

### Task 1: Secret-scan policy (M1-41)

**Files:**
- Modify: `core/src/agentic/policy/model.py`, `core/src/agentic/policy/load.py`, `core/src/agentic/policy/__init__.py`
- Test: `core/tests/policy/test_secret_scan_policy.py`

**Interfaces:**
- `SecretScanPolicy(_Strict)`:
  - `action: Literal["block", "mask"] = "block"`
  - `detectors: list[str] | None = None`: `None` is the default set. Any name given must be in `agentic.security.known_detectors()`; the check is imported lazily to avoid an import cycle.
- `OrgPolicy.secret_scan: SecretScanPolicy = SecretScanPolicy()`; `org.yaml` key `secret_scan`.

- [ ] **Step 1:** Write the tests:
  - defaults (block, `None`);
  - `secret_scan: {action: mask}` loads from `org.yaml`;
  - an unknown action is a `PolicyError`;
  - an unknown detector name is a `PolicyError` naming it.
- [ ] **Step 2:** Run them and see them fail. **Step 3:** Implement. **Step 4:** Run them and see them pass.

### Task 2: The scanner (M1-41)

**Files:**
- Create: `core/src/agentic/security/__init__.py`, `core/src/agentic/security/secret_scan.py`
- Test: `core/tests/security/test_secret_scan.py`

**Interfaces:**
- `known_detectors() -> list[str]`: the regex-based detect-secrets plugin class names.
- `DEFAULT_DETECTORS`: all of them except `IPPublicDetector`. A public IP is not a secret.
- `@dataclass(frozen=True) SecretScanEvent`:
  - fields `action: Literal["blocked", "masked"]`, `where: str`, `findings: dict[str, int]`;
  - `fields() -> dict`.
- `class SecretBlocked(Exception)`: carries `.event`. Its message names types and counts, never values.
- `SecretScanner(action="block", detectors=None, on_event=None)`:
  - `from_policy(policy, on_event=None)`;
  - `findings(text) -> list[Finding]`;
  - `mask(text) -> tuple[str, Counter]`;
  - `check_text(text, where) -> str`: raises `SecretBlocked` or returns the masked text, and emits the event;
  - `check_body(content: bytes, where) -> bytes`: JSON bodies have every string value scanned and masked; other bodies are scanned as latin-1, so bytes round-trip.
- Mask spans:
  - the whole regex match, extended forward over token characters (`[A-Za-z0-9_\-+/=.]`);
  - a private-key header extends to its `-----END ...-----` line, or to the end of the string when there is none;
  - overlapping spans merge.
- `on_event` defaults to a `logging` warning.
- `record_to(recorder, step) -> Callable[[SecretScanEvent], None]`: writes `recorder.record("secret_scan", step, action=..., where=..., findings=...)`. `recorder` is any object with the engine's `RunRecorder.record`.

- [ ] **Step 1:** Write the tests:
  - AWS, GitHub and Slack keys and a PEM block are found;
  - clean code gives no finding;
  - `test_mask_covers_the_whole_token`;
  - `test_mask_covers_a_whole_pem_block`;
  - block raises with no value in the message;
  - the event carries counts;
  - a JSON body with an escaped PEM (`\n`) is masked, and the result is still valid JSON;
  - a non-JSON body round-trips its other bytes;
  - `record_to` writes a real `RunLedger` event with no secret in `events.jsonl`;
  - a narrowed `detectors` list ignores the others;
  - a 1 MB body scans in under 1 s.
- [ ] **Steps 2-4:** Fail, implement, pass.

### Task 3: Every model call scanned (M1-41)

**Files:**
- Create: `core/src/agentic/transport/scan.py` (`ScanningTransport`, `SyncScanningTransport`)
- Modify:
  - `core/src/agentic/transport/client.py`: `scanner=` on both factories; the inner transport defaults to the network transport; a sync client-wrapping transport is added;
  - `core/src/agentic/agents/models.py`: `scanner=` on `build_model` and `metered_model`;
  - `core/src/agentic/backends/native.py`: `scanner_for(step)`;
  - `core/src/agentic/engine/factory.py`: pass-through.
- Test: `core/tests/transport/test_scan.py`; an addition to `core/tests/agents/test_models.py`

- [ ] **Step 1:** Write the tests:
  - `test_a_planted_secret_is_blocked_before_any_http_call`: a metered model whose prompt has an AWS key raises `SecretBlocked`, the mock network saw no request, and the budget has nothing reserved or spent;
  - `test_the_masked_variant_is_sent`: mask policy; the request reaches the mock, with the mask and without the key;
  - the sync client blocks too;
  - a client built without a scanner still blocks (default on);
  - a clean request is passed through byte for byte;
  - the existing `test_network.py` still passes through the network transport (CA, proxy env).
- [ ] **Steps 2-4:** Fail, implement, pass.

### Task 4: Untrusted content (M1-42, DF-23, DF-24)

**Files:**
- Create: `core/src/agentic/security/untrusted.py`
- Modify:
  - `core/src/agentic/agents/tools/__init__.py` (`protect_tools`, applied in `build_tools`);
  - `core/src/agentic/agents/tools/context.py` (`ToolContext.secret_scanner`);
  - `core/src/agentic/agents/runner.py` (`DATA_NOTICE`, fenced handoffs);
  - `core/src/agentic/backends/native.py` (spawn tools protected).
- Test: `core/tests/security/test_untrusted.py`; additions to `core/tests/agents/test_tools.py` and `test_runner.py`

**Interfaces:**
- `fence(text, tag, **attrs) -> str`:
  - escapes `<` before any fence tag (`untrusted-data`, `handoff`) in `text`, case-insensitive, whitespace-tolerant, as `&lt;`;
  - escapes attribute values (`&`, `<`, `>` and `"`).
- `untrusted(text, source) -> str`.
- `DATA_NOTICE: str`.
- `protect_tools(tools, *, scanner, tree=None, trusted=frozenset({"activate_skill"})) -> list[Tool]`. Each tool keeps its name, schema and approval settings, because it is a `copy.copy` with `function_schema.function` replaced.

- [ ] **Step 1:** Write the tests:
  - `test_an_injected_instruction_in_a_file_stays_data`: `read_file` on a file holding an injected instruction and `</untrusted-data>`, through a real Agent;
  - refusals stay unwrapped;
  - `activate_skill` stays unwrapped;
  - a tool output holding a secret is withheld (block) or masked (mask), with an event;
  - an absolute tree path in a refusal is shown relative;
  - `test_a_handoff_cannot_close_its_wrapper`;
  - the instructions carry `DATA_NOTICE`;
  - the prompt structure is intact: instructions, then handoffs, then contract, in order.
- [ ] **Steps 2-4:** Fail, implement, pass.

### Task 5: Credential provider (M1-48)

**Files:**
- Create: `core/src/agentic/auth/__init__.py`, `core/src/agentic/auth/credentials.py`
- Test: `core/tests/auth/test_credentials.py`

**Interfaces:**
- `TOKENLEAN_KEY = "tokenlean_api_key"`.
- `env_var(name) -> str`: gives `AGENTIC_<NAME>`.
- `@dataclass(frozen=True) Credential(value: str (repr=False), source: Literal["env", "keyring"])`.
- `class CredentialError(Exception)`.
- `Credentials(environ=None, keyring=None, service="agentic-sdlc")`:
  - `find(name) -> Credential | None`;
  - `get(name) -> Credential`, which raises `CredentialError` naming the env var and `agentic login`;
  - `store(name, value)`.
- Lookup order:
  1. env, non-blank and stripped;
  2. in CI (`CI` truthy), stop there;
  3. the keyring.
- Any `keyring.errors.KeyringError`, a missing backend included, becomes an actionable `CredentialError`.

- [ ] **Step 1:** Write the tests:
  - `test_env_wins_and_ci_never_touches_the_keyring`;
  - the keyring is used otherwise (a real `keyring.backend.KeyringBackend` subclass held in memory);
  - a missing key gives an actionable error naming the env var and `agentic login`;
  - a broken keyring gives an actionable error;
  - `repr` never shows the value;
  - `store` writes to the keyring.
- [ ] **Steps 2-4:** Fail, implement, pass.

### Task 6: Verify

- [ ] `uv run python scripts/check.py --all` is green.
- [ ] A Sonnet review subagent goes over the branch diff.
- [ ] Tracker notes and DF items are returned to the coordinator. This track does not edit TRACKER.md or DEFERRED.md.
