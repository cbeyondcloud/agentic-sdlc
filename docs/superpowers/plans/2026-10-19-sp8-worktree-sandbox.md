# SP8 Worktree and Sandbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each agent step works in the run's own git tree with a checkpoint per step (rollback = back to the step's base). Its file and shell tools go through one shell policy: allow, ask or forbid prefixes, compiled for the native guard, `srt` and Claude Code. Its commands run inside `srt` on macOS, Linux and WSL2, and a readiness check says exactly what is missing on each machine.

**Architecture:**
- **Core.**
  - Gains `agentic.sandbox`: the process runner, `SrtSandbox`, readiness and the sandbox decision.
  - The engine calls a small `StepTrees` protocol at step start, step end and on reject.
- **Commons.**
  - The guard gains the allow and ask lists, case folding and two compilers.
  - `StepCheckpoints` makes per-step commits on the run's Zyroflo tree.
- **CLI.** The first module, `agentic_cli/wiring.py`, joins core and commons. It is the only place that knows both.

**Tech Stack:** Python 3.12, Pydantic v2, pydantic-ai-slim 2.54.0 (`ApprovalRequired`, `RunContext.tool_call_approved`), Zyroflo pinned at `ce4ab12` (`tools.sdlc.worktree.git`, `policy.protected`), Anthropic `srt` 0.0.78 (an external Node tool, not a Python dependency), pytest.

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md`: §9.2 Sandbox, D31 deny lists, D34 Zyroflo reuse, D38 sandbox. Also spike record `docs/spikes/S6-windows-sandbox.md` and master plan rows M1-30..M1-33.

## Global Constraints

- **Python and licences.** Python 3.12. No new Python dependencies. Only permissive licences (MIT, Apache-2.0, BSD, ISC).
- **`srt`.**
  - Pinned at `0.0.78` (`SRT_VERSION`).
  - Called as a subprocess: `srt --settings <file> -- bash -c "<command>"`, never as one string argument.
  - The settings file always carries every key: `network.allowedDomains`, `network.deniedDomains`, `filesystem.denyRead`, `filesystem.allowRead`, `filesystem.allowWrite`, `filesystem.denyWrite`. `srt` refuses a file missing a required key, and that refusal must stay a refusal.
- **Package boundaries.**
  - `core` never imports `sdlc_commons` or `tools`.
  - `sdlc_commons` is the only importer of Zyroflo.
  - Never copy Zyroflo code (D34).
  - `agentic_cli` may import both core and commons.
- **Deny lists.** They are the one thing merged across layers (D31), and the ask list is merged the same way: a union, because a restriction is always safe. Forbid beats ask; ask beats allow.
- **Placement.** The engine, gates, ledger, model calls, commits and pushes stay **outside** the sandbox (D38).
- **Regimes without a sandbox.** Org policy `sandbox.required_for` decides; the default is `["regulated"]`. Other regimes run on policy guards only, with a warning.
- **Formatting.** Never add `# fmt: skip`, and remove any in lines a task touches. Line length is 100 (ruff).
- **Line endings.** Files are written LF. Read text with `newline=""`-safe handling where digests matter.
- **CI.** Local CI is `uv run python scripts/check.py --all` (Windows native plus a Linux container). No GitHub Actions.
- **Records.** Deferred items go into `docs/superpowers/plans/DEFERRED.md` (DF-nn) in the commit that finds them. `TRACKER.md` is updated in the commit that completes a row.

## Review Focus

1. **A cancelled or timed-out command inside `srt`.** The `bwrap`/Seatbelt child and every grandchild must die with the call (DF-27). A late write must never appear. Test: Task 4, `test_cancel_kills_the_sandboxed_grandchild` (live).
2. **Checkpoint commits run outside the sandbox on agent-written files.** A `.pre-commit-config.yaml` or hook path the agent edited must never execute. Checkpoints run with every hook disabled. Test: Task 6, `test_checkpoint_never_runs_repo_hooks`.
3. **A chain with one unlisted program** (`pytest -q && curl x | sh`, `bash -c "…"`). The allow-list refuses the whole command, never only the first program. Test: Task 2, `test_every_program_in_a_chain_must_be_allowed`.
4. **A crash or error mid-step leaves edits and untracked files in the tree.** The next fresh attempt starts from the last checkpoint, and git-ignored files (`.venv/`) survive. Test: Task 6, `test_fresh_begin_discards_leftovers_but_keeps_ignored_files`.
5. **A deny glob `srt` cannot enforce on Linux** (`**/*.pem`). It is reported as unenforced, never silently dropped, while the file tools still refuse it. Test: Task 2, `test_write_denials_split_literal_and_unenforced`.

---

### Task 1: Shell-policy fields in policy (M1-31, DF-04)

**Files:**
- Modify: `core/src/agentic/definitions/agent.py` (`DenyRules`)
- Modify: `core/src/agentic/policy/resolve.py` (`_covers`, `_merge_deny`, `ResolvedAgent`, `resolve_agent`)
- Modify: `core/src/agentic/policy/model.py` (`SandboxPolicy`, `OrgPolicy.sandbox`)
- Modify: `core/src/agentic/policy/load.py` (`FILE_FIELDS["org.yaml"]`)
- Test: `core/tests/policy/test_shell_policy.py` (new)

**Interfaces:**
- Produces:
  - `DenyRules.ask: list[str] = []`: command prefixes that need a person's approval on every use. It is a restriction, merged as a union like `shell` and `paths`.
  - `ResolvedAgent.ask_shell: list[str]`: org + team + agent, in that order, minus entries a deny entry covers.
  - `_covers(prefix: str, command: str) -> bool`, compared word by word:
    - words come from `str.split()`;
    - each prefix word may hold alternatives `a|b`;
    - the first word is compared as a program name: lower-cased basename with `.exe/.cmd/.bat/.com/.ps1/.sh` removed.
  - `SandboxPolicy(_Strict)` with these fields:
    - `required_for: list[Literal["solo","team","regulated"]] = ["regulated"]`
    - `allowed_domains: list[str] = []`
    - `deny_read: list[str] = []`
    - `allow_write: list[str] = []`
    - `srt_version: str = "0.0.78"`
    - property `effective_deny_read -> list[str]` = `DEFAULT_DENY_READ` followed by `deny_read`, duplicates removed.
  - `DEFAULT_DENY_READ = ("~/.ssh", "~/.aws", "~/.azure", "~/.config/gcloud", "~/.config/gh", "~/.docker", "~/.kube", "~/.gnupg", "~/.netrc", "~/.git-credentials", "~/.pypirc", "~/.npmrc", "~/.agentic")`
  - `OrgPolicy.sandbox: SandboxPolicy = SandboxPolicy()`, loaded from the `sandbox:` key of `org.yaml`.

- [ ] **Step 1: Write the failing tests** in `core/tests/policy/test_shell_policy.py`. Use the existing policy fixtures and helpers from `core/tests/policy/test_resolve.py`, copying the minimal setup.

```python
def test_covers_reads_alternatives():
    assert _covers("git push|fetch", "git push origin main")
    assert _covers("git push|fetch", "git fetch")
    assert not _covers("git push|fetch", "git pull")

def test_covers_ignores_spacing_and_program_spelling():
    assert _covers("git  push", "git push")
    assert _covers("git push", "GIT.exe push --force")
    assert not _covers("git push", "git")

def test_deny_with_alternatives_drops_the_allow_prefix(...):
    # agent type allows shell ["git push", "pytest"]; org deny.shell ["git push|fetch"]
    assert agent.shell == ["pytest"]

def test_ask_lists_merge_as_a_union(...):
    # org deny.ask ["npm publish"], team deny.ask ["pip install"], agent deny.ask ["npm publish"]
    assert agent.ask_shell == ["npm publish", "pip install"]

def test_a_denied_command_is_not_also_an_ask(...):
    # deny.shell ["npm"], deny.ask ["npm publish"]
    assert agent.ask_shell == []

def test_sandbox_policy_defaults_and_floor(tmp_path):
    policy = load_policy(<fixture copy with org.yaml sandbox: {deny_read: ["~/secrets"]}>)
    assert policy.sandbox.required_for == ["regulated"]
    assert policy.sandbox.effective_deny_read[:1] == ["~/.ssh"]
    assert policy.sandbox.effective_deny_read[-1] == "~/secrets"

@pytest.mark.parametrize("bad", ["/", "~", "~/", "~/.ssh", "~/.ssh/keys"])
def test_allow_write_may_not_open_home_root_or_a_denied_read(tmp_path, bad):
    with pytest.raises(PolicyError, match="allow_write"):
        load_policy(<fixture with sandbox: {allow_write: [bad]}>)
```

- [ ] **Step 2: Run** `uv run pytest core/tests/policy/test_shell_policy.py -v`. Expected: FAIL (`DenyRules` has no `ask`; `OrgPolicy` has no `sandbox`).

- [ ] **Step 3: Implement.**
  - `_covers` as specified. Every existing caller (type ceiling, deny dropping) uses the new comparison.
  - `_merge_deny` also merges `ask`.
  - `ResolvedAgent.ask_shell` drops entries covered by any deny entry.
  - `SandboxPolicy` gets a `model_validator` refusing an `allow_write` entry that:
    - is `/`, `~` or `~/`;
    - is equal to an `effective_deny_read` entry, or inside one;
    - contains a glob character.
  - Add `"sandbox": "sandbox"` to `FILE_FIELDS["org.yaml"]`.

- [ ] **Step 4: Run** `uv run pytest core/tests/policy -q`. Expected: all PASS, including the existing resolve tests.

- [ ] **Step 5: Commit.** `feat(core): ask list, word-wise prefix cover, sandbox policy (M1-31, DF-04)`. In the same commit, mark DF-04 `Done (<sha>)` in DEFERRED.md.

### Task 2: Guard allow and ask lists, case folding, compilers (M1-31, DF-16, DF-10)

**Files:**
- Modify: `commons/src/sdlc_commons/guards.py`
- Test: `commons/tests/test_guard_shell_policy.py` (new)

**Interfaces:**
- Consumes: `ResolvedAgent.shell`, `.ask_shell`, `.deny_shell`, `.deny_paths` (Task 1). The guard reads them by attribute, never by import.
- Produces:
  - `Guard.__init__(…, allow_shell: Iterable[str] | None = None, ask_shell: Iterable[str] = ())`.
    - `None` means no allow-list check (adapter-level use).
    - `[]` means nothing is allowed.
  - `Guard.for_agent(...)` passes `allow_shell=agent.shell, ask_shell=agent.ask_shell`.
  - `Guard.command(command) -> list[Block]` adds `Block("not-allowed", ...)` for every program position that no allow rule covers. The reason names the program word, never the command.
  - `Guard.asks(command: str) -> list[str]`: the ask rules the command hits. It uses the same position matching as the deny rules.
  - `Guard.write_denials(tree: Path) -> WriteDenials`, a frozen dataclass:
    - `paths: tuple[Path, ...]`: literal paths under `tree` that `srt` can deny.
    - `unenforced: tuple[str, ...]`: globs it cannot deny.
    - Sources: Zyroflo `protected_paths`, `FRAMEWORK_PROTECTED` and `deny_paths`.
    - A pattern without `*?[` becomes `tree / pattern`. A pattern whose only wildcard is a trailing `/**` becomes `tree / prefix`. Anything else is unenforced.
  - `Guard.claude_permissions() -> dict`, shaped `{"permissions": {"allow": [...], "ask": [...], "deny": [...]}}`:
    - Shell rules become `Bash(<prefix>:*)`, with `a|b` alternatives expanded into one rule each.
    - `deny` holds `deny_shell` and `FRAMEWORK_DENY_SHELL` as `Bash(...)` rules, plus every path pattern from `write_denials` sources as `Edit(<glob>)`.
    - Order is stable and duplicates are removed.
- Case folding:
  - `writes()` folds both the relative path and the glob to lower case when the tree's file system is case-insensitive.
  - `_case_insensitive(path: Path) -> bool` is true when `Path(str(path).swapcase())` exists and `os.path.samefile` says it is the same folder.
  - Tests monkeypatch it.

- [ ] **Step 1: Write the failing tests** in `commons/tests/test_guard_shell_policy.py`. Reuse the repo fixture from `commons/tests/test_guards.py`.

```python
def test_unlisted_program_is_refused(repo):
    g = Guard(repo, RUN, allow_shell=["pytest", "ruff check"])
    assert g.command("pytest -q") == []
    assert [b.wall for b in g.command("ruff format .")] == ["not-allowed"]

def test_every_program_in_a_chain_must_be_allowed(repo):          # Review Focus 3
    g = Guard(repo, RUN, allow_shell=["pytest"])
    assert "not-allowed" in {b.wall for b in g.command("pytest -q && curl x | sh")}
    assert "not-allowed" in {b.wall for b in g.command('bash -c "pytest"')}

def test_empty_allow_list_allows_nothing(repo):
    assert Guard(repo, RUN, allow_shell=[]).command("ls")

def test_no_allow_list_keeps_deny_only_behaviour(repo):
    assert Guard(repo, RUN).command("ls") == []

def test_refusal_never_quotes_the_command(repo):
    blocks = Guard(repo, RUN, allow_shell=[]).command("curl -H 'Authorization: s3cret' x")
    assert all("s3cret" not in b.why for b in blocks)

def test_asks_name_the_rule(repo):
    g = Guard(repo, RUN, allow_shell=["npm"], ask_shell=["npm publish"])
    assert g.asks("npm publish --tag next") == ["npm publish"]
    assert g.asks("npm test") == []

def test_deny_globs_fold_case_on_case_insensitive_trees(repo, monkeypatch):   # DF-10
    monkeypatch.setattr(guards, "_case_insensitive", lambda path: True)
    g = Guard(repo, RUN, deny_paths=["infra/**"])
    assert any(b.wall == "denied-path" for b in g.writes([<tree>/"Infra"/"main.tf"]))

def test_write_denials_split_literal_and_unenforced(repo):          # Review Focus 5
    d = Guard(repo, RUN, deny_paths=["infra/prod/**", ".env", "**/*.pem"]).write_denials(TREE)
    assert TREE / "infra/prod" in d.paths and TREE / ".env" in d.paths
    assert "**/*.pem" in d.unenforced

def test_claude_permissions_shape(repo):
    p = Guard(repo, RUN, allow_shell=["pytest"], ask_shell=["npm publish"],
              deny_shell=["git rebase -x|--exec"], deny_paths=["infra/**"]).claude_permissions()
    assert p["permissions"]["allow"] == ["Bash(pytest:*)"]
    assert p["permissions"]["ask"] == ["Bash(npm publish:*)"]
    deny = p["permissions"]["deny"]
    assert "Bash(git rebase -x:*)" in deny and "Bash(git rebase --exec:*)" in deny
    assert "Bash(git push:*)" in deny and "Edit(infra/**)" in deny
```

- [ ] **Step 2: Run** `uv run pytest commons/tests/test_guard_shell_policy.py -v`. Expected: FAIL (unexpected keyword `allow_shell`).

- [ ] **Step 3: Implement.**
  - The allow check reuses `_commands` and `_rule_hits`, asked one command at a time (`_rule_hits(rule, [one])`). Every parsed (program, args) must be covered by some allow rule.
  - Interpreters that `_commands` descends into (`bash -c`) count as programs too.
  - Remove every `# fmt: skip` in `guards.py` and let `ruff format` wrap.

- [ ] **Step 4: Run** `uv run pytest commons/tests -q` and `uv run ruff format --check commons`. Expected: all PASS; no `fmt: skip` left (`grep -c "fmt: skip" commons/src/sdlc_commons/guards.py` prints 0).

- [ ] **Step 5: Commit.** `feat(commons): guard allow/ask lists, case folding, srt and Claude Code compilers (M1-31, DF-16, DF-10)`. Mark DF-16 and DF-10 Done.

### Task 3: `run_shell` asks a person for ask-listed commands (M1-31)

**Files:**
- Modify: `core/src/agentic/agents/tools/context.py` (`PathGuard.asks`)
- Modify: `core/src/agentic/agents/tools/shell.py` (`shell_tools`)
- Modify: every fake guard in `core/tests/agents/` (add `asks`)
- Test: `core/tests/agents/test_shell_ask.py` (new)

**Interfaces:**
- Consumes: `Guard.asks(command) -> list[str]` (Task 2) through the protocol.
- Produces:
  - `PathGuard.asks(self, command: str) -> Sequence[str]`.
  - `run_shell` takes a `RunContext` (`Tool(run_shell, takes_ctx=True)`). The order is:
    1. guard blocks: refused;
    2. asks and not `ctx.tool_call_approved`: `raise ApprovalRequired(metadata={"ask_rules": [...]})`;
    3. run.
  - A denied approval reaches the model as the runner's existing `ToolDenied` text.

- [ ] **Step 1: Write the failing tests.** Drive `PydanticAIRunner` with a `FunctionModel` that calls `run_shell`, using the runner test helpers already in `core/tests/agents/`.

```python
async def test_an_ask_listed_command_pauses_for_approval(tmp_path):
    outcome = await runner(guard=FakeGuard(asks={"npm publish": ["npm publish"]})).run(task)
    assert outcome.status == "paused" and outcome.pending[0].tool == "run_shell"

async def test_an_approved_ask_runs_the_command(tmp_path): ...  # resume with True -> sandbox ran it
async def test_a_denied_ask_never_runs(tmp_path): ...          # resume with "no" -> sandbox not called
async def test_a_blocked_command_is_refused_before_asking(tmp_path): ...  # block wins, no pause
```

- [ ] **Step 2: Run** `uv run pytest core/tests/agents/test_shell_ask.py -v`. Expected: FAIL (no pause; `FakeGuard` has no `asks`).

- [ ] **Step 3: Implement** as specified. Fake guards across the agent tests gain `asks` returning `[]`.

- [ ] **Step 4: Run** `uv run pytest core/tests/agents -q`. Expected: all PASS.

- [ ] **Step 5: Commit.** `feat(core): run_shell pauses for approval of ask-listed commands (M1-31)`. Flip M1-31 to Done in TRACKER.md (Tasks 1-3 complete it; the claude-code backend consuming `claude_permissions` is M1-29).

### Task 4: `srt` sandbox (M1-32, DF-27)

**Files:**
- Create: `core/src/agentic/sandbox/__init__.py`, `process.py`, `rules.py`, `srt.py`
- Modify: `core/src/agentic/agents/tools/shell.py` (`LocalProcessSandbox` delegates to `run_process`; `_Capture`, `_stop` and the secret filter move to `process.py`)
- Create: `scripts/linux-check.Dockerfile`; Modify: `scripts/check.py` (`run_linux`)
- Test: `core/tests/sandbox/test_rules.py`, `test_srt_unit.py`, `test_srt_live.py`, plus `core/tests/sandbox/fake_srt.py` (test helper)

**Interfaces:**
- Produces:
  - `async def run_process(command: str | Sequence[str], cwd: Path, timeout_s: float, env: Mapping[str, str] | None = None) -> ShellResult`.
    - A `str` runs through the shell; a sequence is exec'd.
    - It runs in a new process group or session and kills the whole tree on timeout and on cancellation.
    - The default env is `os.environ` without secret-named variables.
  - `@dataclass(frozen=True) SandboxRules`:
    - `write: tuple[Path, ...]`
    - `deny_write: tuple[Path, ...] = ()`
    - `deny_read: tuple[Path, ...] = ()`
    - `allow_read: tuple[Path, ...] = ()`
    - `allowed_domains: tuple[str, ...] = ()`
  - `srt_settings(rules: SandboxRules) -> dict`: all six keys always, paths as POSIX strings. It raises `SandboxError` when `write` is empty.
  - `expand_home(entries: Iterable[str], home: Path) -> tuple[Path, ...]`.
  - `class SandboxError(Exception)`.
  - `SRT_VERSION = "0.0.78"`.
  - `class SrtSandbox`:
    - `isolated = True`.
    - `__init__(self, rules: SandboxRules, settings_dir: Path, *, srt: Sequence[str] = ("srt",))`.
    - `async run(command, cwd, timeout_s) -> ShellResult`. It writes `settings_dir/"srt-settings.json"` atomically on first use, then runs `[*srt, "--settings", <file>, "--", "bash", "-c", command]` through `run_process`.

- [ ] **Step 1: Write the failing unit tests** (run on every OS).

```python
def test_settings_always_carry_every_key(tmp_path):
    s = srt_settings(SandboxRules(write=(tmp_path,)))
    assert set(s["network"]) == {"allowedDomains", "deniedDomains"}
    assert set(s["filesystem"]) == {"denyRead", "allowRead", "allowWrite", "denyWrite"}

def test_paths_with_spaces_survive(tmp_path):
    d = tmp_path / "my repo"
    assert srt_settings(SandboxRules(write=(d,)))["filesystem"]["allowWrite"] == [d.as_posix()]

def test_no_writable_path_is_refused():
    with pytest.raises(SandboxError): srt_settings(SandboxRules(write=()))

def test_expand_home(tmp_path):
    assert expand_home(["~/.ssh", "/etc/x"], tmp_path) == (tmp_path / ".ssh", Path("/etc/x"))

async def test_srt_is_called_with_separate_arguments(tmp_path):
    # fake_srt.py records argv to a file, checks the settings file parses, then runs argv[-1]
    sb = SrtSandbox(SandboxRules(write=(tmp_path,)), tmp_path / "s",
                    srt=(sys.executable, FAKE_SRT))
    result = await sb.run("echo hi", tmp_path, 30)
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert argv[1:] == ["--settings", str(tmp_path / "s" / "srt-settings.json"),
                        "--", "bash", "-c", "echo hi"]
    assert result.exit_code == 0

async def test_cancelling_the_call_stops_the_command(tmp_path): ...  # fake srt sleeps; cancel; process gone
```

- [ ] **Step 2: Write the live tests** in `test_srt_live.py`. Module-level skip: `sys.platform == "win32" or shutil.which("srt") is None`. Each test builds `SrtSandbox` with `write=(tree,)`, `deny_write=(tree/".env",)`, `allowed_domains=("pypi.org",)`.

```python
async def test_write_inside_the_tree_is_allowed(...)
async def test_write_outside_the_tree_is_blocked(...)       # file absent afterwards
async def test_denied_path_inside_the_tree_is_blocked(...)
async def test_https_to_an_unlisted_host_is_blocked(...)    # curl https://example.com exit != 0
async def test_https_to_an_allowed_host_works(...)          # curl https://pypi.org/simple/ exit 0
async def test_cancel_kills_the_sandboxed_grandchild(...)   # Review Focus 1:
    # "(sleep 2; echo late > marker) & wait", cancel after 0.5 s, wait 3 s, marker absent
```

- [ ] **Step 3: Run** `uv run pytest core/tests/sandbox -v`. Expected: unit tests FAIL (no module `agentic.sandbox`); live tests SKIPPED on Windows.

- [ ] **Step 4: Implement** `process.py`, `rules.py` and `srt.py`. Point `LocalProcessSandbox` at `run_process`; existing shell tests must stay green unchanged.

- [ ] **Step 5: Linux CI image.**
  - `scripts/linux-check.Dockerfile`:
    - `FROM ghcr.io/astral-sh/uv:0.12.1-python3.12-trixie-slim`
    - `apt-get install git bubblewrap socat ripgrep nodejs npm curl`
    - `npm install -g @anthropic-ai/sandbox-runtime@0.0.78`
  - `run_linux` builds it as `agentic-check-linux:<first 12 hex of the Dockerfile's sha256>` with `docker build -q`, cached, and runs it with `--security-opt seccomp=unconfined --security-opt systempaths=unconfined` (needed for bubblewrap inside a container, S6).
  - If `srt` refuses to run as root in the container, run the checks as a created non-root user and ledger the ruling.

- [ ] **Step 6: Run** `uv run python scripts/check.py --all`. Expected: Windows passes with the live tests skipped; Linux passes with the 6 live tests **run** (check the pytest summary shows them passed, not skipped).

- [ ] **Step 7: Commit.** `feat(core): srt sandbox, shared process runner, Linux CI with srt (M1-32, DF-27)`. Mark DF-27 Done. M1-32 stays open until Task 5 (DF-61 needs the probe).

### Task 5: Readiness, sandbox decision, probe; Windows guidance (M1-33, M1-32)

**Files:**
- Create: `core/src/agentic/sandbox/readiness.py`, `core/src/agentic/sandbox/probe.py`
- Modify: `core/src/agentic/engine/preflight.py` (`preflight(..., sandbox=None)`)
- Modify: `core/src/agentic/engine/state.py` (`RunState.sandbox: str | None = None`), `core/src/agentic/engine/engine.py` (`Engine(..., sandbox_label: str = "none")`, stored on start)
- Test: `core/tests/sandbox/test_readiness.py`, `test_probe.py`; additions to `core/tests/engine/test_preflight.py`, `test_state.py`

**Interfaces:**
- Consumes: `SrtSandbox`, `SandboxRules`, `SRT_VERSION` (Task 4); `SandboxPolicy` (Task 1).
- Produces:
  - `@dataclass(frozen=True) Check(name: str, ok: bool, detail: str, fix: str = "")`.
  - `@dataclass(frozen=True) Readiness(platform: Literal["macos","linux","wsl","windows","other"], checks: tuple[Check, ...])`, with property `available: bool` and `text() -> str`. `text()` gives one line per check, and the fix under each failing one.
  - `readiness(*, system: str | None = None, kernel_release: str | None = None, which=shutil.which, run=<subprocess runner returning (code, stdout)>, tree: Path | None = None, srt_version: str = SRT_VERSION) -> Readiness`:
    - `system` defaults to `platform.system()`; Linux with `microsoft` in the kernel release is `wsl`.
    - **macOS checks:** `node` ≥ 20.11, `srt --version` == pin, `rg`, smoke.
    - **Linux and WSL checks:** the macOS ones plus `bwrap` and `socat`. Smoke is `srt --settings <tmp file> -- bash -c true` exiting 0, which catches disabled user namespaces.
    - **WSL also checks:** `tree` is not under `/mnt/`. The fix is to clone into the WSL disk, e.g. `~/src`.
    - **Windows:** one failing `Check("sandbox", False, …, fix=WINDOWS_GUIDANCE)`.
  - `WINDOWS_GUIDANCE`, exact text:
    ```
    Native Windows has no supported sandbox (D38). Run agentic inside WSL2:
      1. In PowerShell: wsl --install -d Ubuntu-24.04
      2. In Ubuntu: sudo apt update && sudo apt install -y bubblewrap socat ripgrep nodejs npm
      3. In Ubuntu: sudo npm install -g @anthropic-ai/sandbox-runtime@0.0.78
      4. Ubuntu 24.04+: sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
      5. Install agentic inside Ubuntu and clone repos to the WSL disk (~/src), not /mnt/c
    ```
  - `@dataclass(frozen=True) SandboxDecision(use_srt: bool, label: str, refusal: str | None, warning: str | None)`.
  - `decide_sandbox(readiness: Readiness, regime: str, policy: SandboxPolicy) -> SandboxDecision`:
    - when available: `SandboxDecision(True, f"srt {policy.srt_version}", None, None)`;
    - when the regime is in `required_for`: a refusal naming the regime and pointing at `readiness.text()`;
    - otherwise: `use_srt=False`, label `"none"`, with a warning.
  - `preflight(..., sandbox: SandboxDecision | None = None)`: a refusal becomes an error `Problem(kind="sandbox")`; a warning becomes a warning Problem.
  - `probe.main(argv: list[str] | None = None) -> int` (`python -m agentic.sandbox.probe`):
    - runs the S6 cases as data in `CASES` (write in/out, grandchild, denied path, git hook, git config, `~/.bashrc`, denied read, allowed/other host, `--noproxy`, raw TCP, DNS, git status/commit, unix socket) through `SrtSandbox` in a temp folder;
    - prints a want/got table;
    - returns 1 on any mismatch;
    - `--unsandboxed` runs them through `LocalProcessSandbox`, used by its test.

- [ ] **Step 1: Write the failing tests.**

```python
def test_native_windows_gets_the_exact_guidance():
    r = readiness(system="Windows", which=lambda n: None, run=boom)
    assert not r.available and r.platform == "windows"
    assert "wsl --install" in r.text() and "@anthropic-ai/sandbox-runtime@0.0.78" in r.text()

def test_wsl_is_detected_and_a_repo_on_mnt_c_is_flagged():
    r = readiness(system="Linux", kernel_release="5.15.167.4-microsoft-standard-WSL2",
                  which=all_present, run=all_ok, tree=Path("/mnt/c/src/app"))
    assert r.platform == "wsl" and not r.available
    assert any(c.name == "tree location" and not c.ok for c in r.checks)

def test_linux_with_everything_is_available(): ...           # all checks ok
def test_old_node_is_flagged(): ...                           # node v18.19.0 -> fix names 20.11
def test_wrong_srt_version_is_flagged(): ...                  # 0.0.80 -> fix names 0.0.78
def test_a_failing_smoke_names_user_namespaces(): ...         # smoke exit 1 -> fix mentions the sysctl
def test_macos_needs_no_bwrap(): ...                          # bwrap/socat absent, still available

def test_regulated_refuses_without_a_sandbox():
    d = decide_sandbox(unavailable, "regulated", SandboxPolicy())
    assert d.refusal and not d.use_srt

def test_team_runs_unsandboxed_with_a_warning():
    d = decide_sandbox(unavailable, "team", SandboxPolicy())
    assert (d.use_srt, d.label, d.refusal) == (False, "none", None) and d.warning

def test_preflight_reports_a_sandbox_refusal(...): ...         # error Problem kind "sandbox"
def test_the_run_records_its_sandbox(make_harness): ...        # Engine(sandbox_label="srt 0.0.78") -> state.sandbox
def test_old_run_json_without_sandbox_still_loads(...): ...

def test_probe_catches_a_missing_sandbox(tmp_path, capsys):
    assert probe.main(["--unsandboxed", "--workdir", str(tmp_path)]) == 1
    assert "write outside worktree" in capsys.readouterr().out
```

Add one live probe test to `test_srt_live.py`: `assert probe.main(["--workdir", str(tmp_path)]) == 0`.

- [ ] **Step 2: Run** `uv run pytest core/tests/sandbox core/tests/engine -q`. Expected: the new tests FAIL.

- [ ] **Step 3: Implement** as specified. `make_harness` passes `sandbox_label` through where the test needs it; other engine tests are unchanged.

- [ ] **Step 4: Run** `uv run python scripts/check.py --all`. Expected: green on both platforms; the live probe passes on Linux.

- [ ] **Step 5: Commit.** `feat(core): sandbox readiness, regime decision, probe, Windows guidance (M1-33, M1-32)`.
  - Flip M1-32 and M1-33 to Done in TRACKER.md.
  - DF-60 and DF-61 stay Open; their text now says "run `python -m agentic.sandbox.probe` once in WSL2 / on a Mac". Both are owner actions.

### Task 6: Step checkpoints and engine hooks (M1-30)

**Files:**
- Modify: `commons/src/sdlc_commons/worktrees.py` (`StepCheckpoints`; remove its `# fmt: skip` lines)
- Modify: `core/src/agentic/engine/engine.py` (`StepTrees` protocol, `Engine(..., trees_for=None)`, hooks)
- Modify: `core/src/agentic/engine/state.py` (`StepRecord.base`, `StepRecord.commit`)
- Test: `commons/tests/test_step_checkpoints.py` (new), `core/tests/engine/test_step_trees.py` (new); `FakeTrees` in `core/tests/engine/_engine_support.py`

**Interfaces:**
- Produces (commons):
  - `class StepCheckpoints(target: Path, run_id: str)` with:
    - `prepare() -> Path`:
      - makes the run's trees (`RunWorktrees.create`, idempotent);
      - returns the folder holding them, which equals `Guard(target, run_id).trees_root`;
      - raises `WorktreeError` naming any component whose mode is not `"worktree"` (in-place components are not supported for code steps).
    - `begin(*, fresh: bool) -> dict[str, str]`. When `fresh`, for each tree: `git reset --hard HEAD` then `git clean -fd`, which keeps ignored files. Returns `{component: HEAD sha}`.
    - `finish(message: str) -> dict[str, str]`. For each tree: `git add -A`, then commit if anything is staged. Returns `{component: HEAD sha}`.
    - `rollback(to: Mapping[str, str]) -> None`. For each component: refuse a sha that is not an ancestor of HEAD (`git merge-base --is-ancestor`); then `git reset --hard <sha>` and `git clean -fd`.
  - All git runs through Zyroflo `tools.sdlc.worktree.git`. Every commit runs with `-c core.hooksPath=<an empty folder in the run's state>` and `-c commit.gpgsign=false`. It sets `-c user.name=agentic -c user.email=agentic@localhost` only when the repo has no `user.email`.
- Produces (core):
  - `class StepTrees(Protocol)` with `prepare() -> Path`, `begin(*, fresh: bool) -> dict[str, str]`, `finish(message: str) -> dict[str, str]` and `rollback(to: dict[str, str]) -> None`.
  - `Engine(..., trees_for: Callable[[str], StepTrees] | None = None)`.
  - `StepRecord.base: dict[str, str] | None = None` and `StepRecord.commit: dict[str, str] | None = None`.
- **Engine rules:**
  - **Start.** `start()` calls `prepare()` once, after the ledger is opened.
  - **Each agent step.** Before the runner: `base = begin(fresh=resume is None)`. After the runner returns an outcome, any status except a tool-approval pause: `commit = finish(f"agentic: {step} #{iteration} ({run_id})")`. The attempt's StepRecord carries both. An exception or a `LimitHit` makes no commit; the next fresh `begin` discards the leftovers. Action steps make no tree calls.
  - **Reject.** Before `go()`, `rollback(record.base)` for the latest StepRecord whose `step` is the rejection target and whose `iteration` is:
    - the cursor's loop iteration (`1`) for a gate or exhausted-loop reject;
    - the pending step's iteration for an `invalid-output`/`blocked-output` reject.
    No such record means no rollback.
  - **Tree failure.** Any exception from a tree call pauses the run with id `worktree` and the error text; the step is not run.

- [ ] **Step 1: Write the failing commons tests** (real git, the Zyroflo repo fixture used by `commons/tests/test_worktrees.py`).

```python
def test_prepare_returns_the_guard_trees_root(repo): ...
def test_finish_commits_each_step_on_the_run_branch(repo): ...   # file in commit; branch sdlc/<run>
def test_finish_with_no_change_returns_head_unchanged(repo): ...
def test_fresh_begin_discards_leftovers_but_keeps_ignored_files(repo):    # Review Focus 4
    # modified tracked file + new untracked file + ignored .venv/x -> after begin(fresh=True):
    # tracked restored, untracked gone, .venv/x still there
def test_resume_begin_keeps_work_in_progress(repo): ...
def test_rollback_returns_to_the_step_base(repo): ...
def test_rollback_refuses_a_commit_outside_the_branch(repo): ...
def test_checkpoint_never_runs_repo_hooks(repo):                         # Review Focus 2
    # a hooks dir with pre-commit/post-commit writing a marker, set via core.hooksPath in repo config
    # -> finish() commits, marker absent
def test_in_place_component_is_refused(repo_with_deny_raw_git): ...
```

- [ ] **Step 2: Write the failing engine tests** with `FakeTrees` (records calls, returns `{"app": f"c{n}"}`).

```python
def test_each_agent_step_begins_fresh_and_finishes(make_harness): ...
def test_tool_approval_resume_keeps_the_tree(make_harness): ...   # begin(fresh=False), no finish while paused
def test_a_failed_step_is_not_committed(make_harness): ...        # step-error pause: no finish call
def test_reject_rolls_back_to_the_target_steps_base(make_harness): ...
def test_invalid_output_reject_rolls_back_only_that_attempt(make_harness): ...
def test_a_tree_failure_pauses_as_worktree(make_harness): ...
def test_records_carry_base_and_commit(make_harness): ...
def test_engine_without_trees_is_unchanged(make_harness): ...      # trees_for=None: existing behaviour
```

- [ ] **Step 3: Run** `uv run pytest commons/tests/test_step_checkpoints.py core/tests/engine/test_step_trees.py -v`. Expected: FAIL.

- [ ] **Step 4: Implement** as specified.

- [ ] **Step 5: Run** `uv run pytest -q`. Expected: all PASS.

- [ ] **Step 6: Commit.** `feat: per-step checkpoints on the run tree, rollback on reject (M1-30)`.

### Task 7: Wiring: tool context per step (M1-30, DF-46)

**Files:**
- Modify: `cli/pyproject.toml` (workspace dependencies `agentic-core`, `sdlc-commons`, declared the way the root workspace declares members); then `uv lock`
- Create: `cli/src/agentic_cli/wiring.py`
- Test: `cli/tests/test_wiring.py` (new)

**Interfaces:**
- Consumes:
  - `Guard.for_agent`, `Guard.trees_root`, `Guard.write_denials` (Task 2);
  - `StepCheckpoints` (Task 6);
  - `SrtSandbox`, `SandboxRules`, `expand_home` (Task 4);
  - `SandboxDecision` (Task 5);
  - `ToolContext`, `LocalProcessSandbox` (core).
- Produces:
  - `class StepWiring(target: Path, policy: SandboxPolicy, decision: SandboxDecision, state_root: Path, home: Path = Path.home())` with:
    - `trees_for(run_id: str) -> StepCheckpoints`.
    - `tools_for(step: StepContext) -> ToolContext`:
      - `tree = Guard.trees_root`, `guard = Guard.for_agent(target, run_id, step.agent)`.
      - When `decision.use_srt`: `SrtSandbox(rules, state_root / run_id / "sandbox" / step.agent.definition.name)` with these rules:
        - `write = (tree, *expand_home(policy.allow_write, home))`
        - `deny_write = guard.write_denials(tree).paths`
        - `deny_read = expand_home(policy.effective_deny_read, home)`
        - `allowed_domains = tuple(policy.allowed_domains)`
      - Otherwise `LocalProcessSandbox()` with `allow_unisolated=True`. That path is reachable only when `decision.refusal is None`; a refusal raises `ValueError`.
    - `unenforced(agent: ResolvedAgent, run_id: str) -> list[str]`: the deny globs `srt` cannot enforce, for the CLI to show (M1-54).

- [ ] **Step 1: Write the failing tests** (real git repo plus Zyroflo trees, as in Task 6; a `ResolvedAgent` with tools `["read_files", "edit_files", "run_shell"]` and shell `["echo", "git status"]`).

```python
def test_tools_write_inside_the_run_tree_and_the_step_commit_holds_it(repo): ...
    # wiring.trees_for(RUN).prepare(); write_file tool -> file in tree; finish() -> in commit on sdlc/<run>
def test_tools_refuse_a_write_outside_the_tree(repo): ...
def test_shell_runs_in_the_tree_and_refuses_unlisted_programs(repo): ...   # "echo ok" ok; "ls" refused
def test_a_refused_decision_cannot_build_tools(repo): ...
def test_srt_rules_carry_policy_and_guard(repo): ...   # decision.use_srt=True: inspect SrtSandbox rules
def test_unenforced_globs_are_reported(repo): ...       # deny_paths ["**/*.pem"] -> listed
```

- [ ] **Step 2: Run** `uv run pytest cli/tests/test_wiring.py -v`. Expected: FAIL (no module `agentic_cli.wiring`).

- [ ] **Step 3: Implement** as specified.

- [ ] **Step 4: Run** `uv run python scripts/check.py --all`. Expected: green on Windows and Linux.

- [ ] **Step 5: Commit.** `feat(cli): step wiring - tree, guard and sandbox per step (M1-30, DF-46)`.
  - Mark DF-46 Done. Flip M1-30 to Done in TRACKER.md.
  - Add the new DEFERRED rows found on the way:
    - in-place (`deny_raw_git`) components unsupported for code steps → M1-58;
    - team narrowing of `sandbox` policy → M2-21;
    - show `unenforced` globs → M1-54;
    - the claude-code backend applies `claude_permissions` and wraps the CLI in `srt` with the TokenLean host allowed → M1-29.
