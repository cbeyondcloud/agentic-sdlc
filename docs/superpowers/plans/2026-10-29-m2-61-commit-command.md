# M2-61 Commit-command override Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A code step can run on a component Zyroflo makes in place (`vcs.deny_raw_git`). Its
checkpoints and rollbacks go through the component's own commit and rollback commands (the
spec's commit-command override, §9.2), and publishing pushes it through its own push command.
The framework never runs raw git writes on it. Closes DF-62, DF-287 and DF-360.

**Architecture:**
- **Where the override lives (ruling R1).** The commit and push commands are Zyroflo's existing
  profile keys, `vcs.commit_command` (`{message}`) and `vcs.push_command` (`{branch}`) in
  `.sdlc/profile.yml` - the spec calls the override a Zyroflo feature, and Zyroflo's own stage
  instructions already read them. Zyroflo has no rollback key (its `vcs:` keys are a closed
  list), so the rollback command is a repo setting: `.agentic/config.yaml`
  `rollback_commands: {<component path>: "<command with {commit}>"}` ("." is the repository
  itself; keyed by path because a single repository's component is named after its folder,
  which changes from clone to clone). The commit command may take `{message_file}` instead of
  `{message}`: the message in a file in the run folder.
- **Who may run it (ruling R2).** These commands run outside the agent's sandbox, as the
  framework's own checkpoint commits do. A repository alone must not make the framework run a
  command. Org policy decides: `org.yaml` `commit_override.allow` is a list of word prefixes
  ("monotool commit"); each command of an override must start with one, or nothing runs. Empty
  (the default) = no override anywhere. Teams cannot loosen it (org-only key).
- **How it runs.** `sdlc_commons.commit_override.CommitOverride`:
  - parses each command into an argv list (`shlex`, POSIX quoting, backslashes kept literal so
    Windows paths work); each placeholder must be a whole word; unknown `{...}` is refused;
  - refuses raw git as the program (the component denies raw git);
  - checks the command with placeholders unfilled against the guard (`Guard.command`:
    Zyroflo's destructive floor, the repo's deny lists, the org's `deny.shell`, the framework
    floor) - a denied command is refused before it runs;
  - refuses a command that names a file inside the checkout which changed since the run's base
    or is uncommitted (an agent could have rewritten the commit script);
  - runs `subprocess.run(argv, shell=False)` in the component's checkout, with the
    environment minus the framework's `AGENTIC_*` credentials, stdin closed, a timeout;
    on Windows a `.bat`/`.cmd` program is refused any argument cmd.exe would reinterpret
    (the commit message goes through `{message_file}`);
  - records every command in the run folder (`vcs-commands.jsonl`: time, component, action,
    the command as configured, exit code, duration); never the message text.
- **The override is fixed at the run's first `prepare` (ruling R3).** The checked commands are
  kept in the run folder (`vcs-override.json`, under `.agentic/local/runs`, a protected path),
  and every later step and resume uses that copy: an agent working in the checkout cannot
  change the command by editing `.sdlc/profile.yml`. The first `prepare` also refuses a dirty
  checkout: a fresh attempt discards uncommitted work, and in place that work would be the
  developer's own.
- **`StepCheckpoints` (commons).** Takes an optional `override`. `prepare` accepts a run whose
  components are all in place when there is exactly one in-place component with an accepted
  override (home = its checkout); a run mixing in-place and worktree components, or with
  several in-place components, is still refused with a precise reason (DF for later).
  Without the override the refusal names the settings to add. `begin(fresh)` discards
  through the rollback command (only when the checkout differs from the base); `finish`
  commits through the commit command when `git status` shows a change; `rollback` checks the
  target commit is an ancestor (a git read), then runs the rollback command. `changed` and
  `diff` use git reads only (Zyroflo: `deny_raw_git` forbids commit, push and worktree, not a
  read). HEAD is read with `git rev-parse` after each command.
- **Rollback contract (ruling R4).** `rollback_commands` puts the checkout back at `{commit}`
  (a commit of the checkout's history) and discards uncommitted work, including untracked
  files that are not ignored. The framework then reads HEAD and refuses when it is not
  `{commit}`.
- **Guard.** An in-place home with an accepted override is a place agents may write (file
  tools, `changes`, srt write denials), except the harness's own folders inside it: `.sdlc/`
  and the worktrees root. Everything else (protected paths, deny lists) applies as in a tree.
- **Publishing (cli).** An in-place component is published through its `push_command`
  (through the push wall for its run branch name, then the override's checks), then its pull
  request is opened as for a tree. No push command -> refused, naming `vcs.push_command`.
- **Wiring (cli).** `StepWiring` builds the `CommitOverride` from org policy
  (`commit_override`, `deny.shell`) and the repo config's `rollback_commands`.

**Tech Stack:** Python 3.12, stdlib `shlex`, `subprocess`; Zyroflo `policy.component` (profile
reader). No new dependency.

**Spec:** §9.2 Sandbox ("Repos where raw git is unsafe can set a commit-command override
(Zyroflo feature)"). Tracker row M2-61. Deferred: DF-62, DF-287, DF-360.

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| The commit and push commands | Zyroflo profile keys `vcs.commit_command`, `vcs.push_command`, read by `zyroflo.sdlc.policy.component` | a second place to declare them would disagree with what Zyroflo's own stages read |
| Splitting a command into argv | stdlib `shlex` (POSIX mode, no escape character) | a shell (`shell=True`) would interpolate untrusted text |
| Running it | stdlib `subprocess.run` with an argv list | no library adds anything over this |
| Which commands are refused | the existing `Guard.command` (Zyroflo walls + deny lists) | - |

## Global Constraints

- Never `shell=True`; never a placeholder inside a word; values substituted only as whole argv
  elements; commit values must be hex ids, branch names a safe character set.
- Windows and POSIX: argv lists, paths through `pathlib`, `.bat`/`.cmd` handled, LF records.
- The framework never runs `git commit`, `git reset`, `git clean`, `git add`, `git push` or
  `git worktree` on an in-place component; a test spies on every git call to prove it.
- No live GitHub calls: the fake GitHub server and a local bare remote.
- ruff, line length 100, no `# fmt: skip`; `uv run python scripts/check.py --all` green.

## Review Focus

1. **Who can make the framework run a command.** A repository alone cannot: no org allow, no
   run. The command is fixed at the first `prepare`; an agent's edit of the profile, the repo
   config or the commit script inside the checkout cannot change what runs.
2. **No raw git writes on an in-place component**, and no shell anywhere (argv lists; the
   Windows batch-file case).
3. **The developer's checkout.** In place, the agent works in the developer's own checkout: a
   dirty checkout is refused at the first `prepare`; the harness folders (`.sdlc/`, the
   worktrees root, `.agentic/`) stay unwritable.
4. **Refusal messages** name the setting to add (`vcs.commit_command`, `rollback_commands`,
   `commit_override.allow`, `vcs.push_command`).
5. **Publishing** of an in-place component pushes through its push command after the push wall,
   and records it.

---

## Task 1: Org policy and repo config settings

**Files:** `core/src/agentic/policy/model.py`, `core/src/agentic/policy/load.py`,
`core/src/agentic/repoconfig.py`, `templates/policy-repo/policy/org.yaml`, tests in
`core/tests/policy/` and `core/tests/test_repoconfig.py`.

**Interfaces:** `CommitOverridePolicy(allow: list[str] = [], timeout_s: int = 600)`;
`OrgPolicy.commit_override`; `RepoConfig.rollback_commands: dict[str, str]` (keys repo-relative
POSIX paths, "." allowed; values non-empty).

- [ ] Test: org.yaml `commit_override: {allow: ["monotool commit"]}` loads; an empty rule or a
  rule with a placeholder is refused; default is empty. See it fail.
- [ ] Test: repo config `rollback_commands: {".": "monotool restore {commit}"}` loads; a key
  outside the repo is refused. See it fail.
- [ ] Implement; template shows a commented example. Tests pass. Commit.

## Task 2: `CommitOverride` (commons)

**Files:** `commons/src/sdlc_commons/commit_override.py`, `commons/tests/test_commit_override.py`.

**Interfaces:** `split_command(text) -> tuple[str, ...]`; `CommitOverride(target, run_id, *,
allow, rollback_commands, deny_shell=(), timeout_s=600)`; `.resolve(trees) -> dict[str,
OverrideCommands]`; `.commit(cmds, message)`, `.rollback(cmds, commit)`, `.push(cmds, branch)`;
`OverrideError(WorktreeError)`.

- [ ] Tests first: splitting keeps Windows backslashes and quotes; a placeholder inside a word
  is refused; unknown placeholder refused; no org allow -> refusal names
  `commit_override.allow`; a command outside the allow list refused; raw git refused; a
  denied command (`rm -rf ...`) refused by the guard; a command naming a changed checkout file
  refused; it runs with argv (a message with `;`, `&&` and quotes reaches the tool as one
  argument); a failure says the exit code; every run is in `vcs-commands.jsonl`. See them fail.
- [ ] Implement. Pass. Commit.

## Task 3: `StepCheckpoints` through the override

**Files:** `commons/src/sdlc_commons/worktrees.py`, `commons/tests/test_step_checkpoints.py`.

- [ ] Tests first: an in-place component with the override: `prepare` returns its checkout,
  a code step's change is committed by the commit command, a fresh begin and a rollback go
  through the rollback command, `changed`/`diff` work; a spy proves no git write ran.
  Without the override the refusal names `vcs.commit_command` and `rollback_commands`; a dirty
  checkout at the first `prepare` is refused; the fixed copy outlives an edit of the profile;
  a mixed run is refused naming the components. See them fail.
- [ ] Implement. Pass. Commit.

## Task 4: Guard admits the in-place home

**Files:** `commons/src/sdlc_commons/guards.py`, `commons/tests/test_guards.py`.

- [ ] Tests first: with an accepted override, a write in the checkout passes; `.sdlc/x`, the
  worktrees root and `.agentic/x` inside it are refused; `write_denials(checkout)` lists the
  harness folders; without an accepted override the write is refused as before. Fail, then
  implement, pass, commit.

## Task 5: Wiring and publishing (cli)

**Files:** `cli/src/agentic_cli/wiring.py`, `cli/src/agentic_cli/runtime.py`,
`cli/src/agentic_cli/publishing.py`, `cli/tests/test_wiring.py`,
`cli/tests/run/test_pr_contract.py` (or a new `test_pr_in_place.py`).

- [ ] Tests first: `StepWiring.trees_for` carries the override from org policy and repo
  config; an end-to-end run on an in-place repository (fake commit tool, local bare remote,
  fake GitHub) checkpoints through the commit command and publishes through the push command;
  without `vcs.push_command` publishing is refused naming it. Fail, implement, pass, commit.

## Task 6: Full check

- [ ] `uv run python scripts/check.py --all` in the background to completion; fix; report.
