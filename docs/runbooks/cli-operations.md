# CLI operations: gc, doctor, support bundle, resource limits

This runbook is for developers and the platform team. It covers four commands and
settings (M3-08, M3-09, M3-10). Everything here works the same in PowerShell and in
bash or zsh, on Windows, macOS and Linux, unless a section says otherwise.

## `agentic gc`: remove finished runs

```text
agentic gc --dry-run            # what would go, with sizes; nothing changes
agentic gc                      # remove it
agentic gc --older-than 30      # only runs that finished more than 30 days ago
```

A run leaves two things on disk:

- its folder, `.agentic/local/runs/<run>` (state, ledger, handoffs, logs);
- its git worktrees, under `.sdlc-worktrees/<run>`.

Nothing removes them automatically (spec §14.6, OR-01). `gc` removes them when you run
it.

**What gc removes.** A run that finished as completed, failed, stopped or killed, and
that is older than the cut-off. The cut-off is the first of these that is set:

1. `--older-than`;
2. `gc.older_than_days` in `~/.agentic/config.yaml`;
3. the org policy's `local.gc_after_days`;
4. 14 days.

**What gc keeps.** Each of these is listed with its reason, and gc still exits 0:

| Kept | Why |
|---|---|
| a running, paused or gate-waiting run | it is not finished |
| a `run.json` that cannot be read | gc cannot tell what the run is |
| in org mode, a run with events the control plane has not received | `agentic sync` sends them; then run gc again |
| a run the publish scan held back | ask your org admin how to handle it |
| in org mode with no control plane configured, every run | its ledger is the only copy of its audit record |
| a run whose tree has uncommitted changes | it is the only copy of that work: commit or discard it |
| a run with a file open in its folder (Windows) | close the editor or `agentic logs --follow`, then run gc again |

Branches (`sdlc/<run>`) are kept, because a pull request may name them.

**On Windows**, gc first renames the run folder aside (`.gc-<run>-<pid>`). Windows
refuses that rename while a file inside is open, so a run in use is never half-deleted.
Read-only git files are deleted too. If a deletion is interrupted, the next gc finishes
it.

**Disk cap.** `agentic status` warns when this repository's local data is over the cap:
`.agentic/local` plus the run trees. The cap is the first of these that is set:

1. `gc.disk_cap_gb` in the user config;
2. the policy's `local.disk_cap_gb`;
3. 20 GB.

## `agentic doctor`: what is wrong with this machine

```text
agentic doctor          # every check, with a fix for each problem
agentic doctor --json   # the same, for a script or the platform team
```

Doctor changes nothing. It exits 0 when no check fails and 1 when one does. Each check
shows one of these statuses:

| Status | Meaning |
|---|---|
| `ok` | fine |
| `FAIL` | a run would fail or be refused |
| `warn` | it works, with a limit |
| `info` | for you to know |
| `skip` | it could not be checked here, and the detail says why |

| Check | It looks at |
|---|---|
| python, git, sandbox (node, srt, ripgrep, bubblewrap, socat, smoke), container engine, git bash, credential | what `agentic init` checks. On native Windows the sandbox check says how to set up WSL2 |
| uv | installed |
| repository | `agentic init` has run here |
| policy, policy: models, policy: cache | the policy loads, no tier still has a placeholder model, and the org policy cache has not expired |
| keyring | the OS keyring can be read. Skipped in CI |
| gateway, gateway: models | reachable, the key is accepted, and every tier's model is served (`GET /v1/models`, which calls no model) |
| control plane | in org mode: reachable, signed in, and the token does not expire within 7 days |
| clock | within 1 minute of the server (warn) or 5 minutes (fail) |
| disk space | at least 5 GB free (warn below) or 1 GB (fail below) |
| local data | under the disk cap |
| withheld variables | the environment variables agent commands do not get, by name only. An org admin can let one through with `sandbox.pass_env` |
| policy repo protection | D33: the org policy repo's default branch requires review by code owners, through branch protection or a ruleset. With no token for the host it says "cannot check" and does not fail |

## `agentic support-bundle`: send the platform team what they need

```text
agentic support-bundle                    # agentic-support-<time>.zip here
agentic support-bundle --output C:\temp\bundle.zip
```

**What is in the zip:**

- `manifest.json`: versions, and the list of files;
- `doctor.json`: doctor's report;
- your user config and the repository's `.agentic/config.yaml`, with secret-named settings removed;
- `init.json`;
- the last 10 runs' summaries;
- the last 200 lines of each step log of the last 3 runs.

**What is never in the zip:** a ledger, handoff, prompt, diff or source file.

**How it is cleaned.** Every line passes through four steps:

1. The commons redactor masks your environment's secret values, your stored tokens and anything shaped like a credential.
2. The publish scan masks secrets and personal data, such as emails and card numbers.
3. The redactor runs again.
4. Your home folder's path becomes `~`.

The finished files are then scanned once more. If anything is still found, nothing is
written. Open the zip before you send it.

## Sandbox resource limits (`sandbox.limits`)

An org admin sets these limits in the policy repo's `policy/org.yaml`. They apply to
every shell command an agent runs, inside the srt sandbox or not:

```yaml
sandbox:
  limits:
    cpu_seconds: 3600     # CPU time per process
    memory_mb: 8192       # resident memory of the whole command
    max_file_mb: 4096     # the largest file a command may write
    processes: 512        # processes a command may run at once
```

A limit that is left out is not applied.

When a limit stops a command, the agent is told `stopped at the memory (8192 MB) limit
(org policy sandbox.limits)`. The run itself goes on.

### How each limit is enforced (ruling)

| Limit | Linux and macOS | Native Windows (best effort) |
|---|---|---|
| CPU time | hard `RLIMIT_CPU` per process | Job Object job time: every process in the job ends when it is spent |
| Memory | a watchdog sums the resident memory of the command's processes every 0.1 s on Linux (0.5 s on macOS, where each look runs `ps`) and kills the command past the limit | Job Object job memory (committed memory): an allocation past the limit fails |
| File size | hard `RLIMIT_FSIZE` | **not enforced** |
| Processes | a watchdog counts the command's processes, with a hard `RLIMIT_NPROC` backstop (non-root) | Job Object active process limit: starting one more process fails |

**How the Linux and macOS rlimits are applied.** A small launcher sets them before the
command starts: `python -I -S`, inside the srt sandbox, so srt itself is not limited.
They are hard limits, so the command cannot raise them. `RLIMIT_NPROC` counts all of
your processes, so it is set to your current count plus the limit. It is a backstop
against a fork bomb, not an exact count. Root, as in a CI container, is exempt from it,
and there the watchdog's count holds alone.

**Why there is no address-space limit.** Node and the JVM reserve far more address space
than they use. An `RLIMIT_AS` limit would break `npm test` or a Gradle build long before
memory ran out.

**Why there are no cgroups.** cgroups v2 (`memory.max`, `pids.max`) would be exact. But
they are not delegated inside srt's bubblewrap, in a Docker CI container, or on macOS.

**Known gaps:**

- **Memory overshoot.** A burst of allocation faster than one poll can exceed the memory limit briefly.
- **Helper processes.** Under srt, the process count includes the sandbox's two or three helper processes.
- **Windows: no file-size limit.**
- **Windows: early children.** A child started in the moment before the command joins its Job Object is not limited.
- **Windows: console host.** A console host that Windows adds may count as one process.
- **Agent CLI processes.** The agent CLI processes themselves (claude-code, codex, opencode) are not limited yet. Only the shell commands run through the framework's sandbox are.
