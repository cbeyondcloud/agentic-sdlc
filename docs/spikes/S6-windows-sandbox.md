# S6 — Sandbox for agent commands on Windows, macOS and Linux

**Question.** Which OS sandbox runs an agent step's commands (tests, builds, scripts, and backend CLIs such as Claude Code or Codex)? Developers use both Mac and Windows laptops, and CI runs Linux. What is the Windows default, and what can the sandbox not block (master plan S6, feeding M1-32 and M1-33)?

**Owner constraints (2026-10-19).**
- No unsigned package installation on developer machines.
- The sandbox must work with any agent, any provider and any model: every model call goes through TokenLean.
- Licences must be permissive and the tool reused, not built.

## Candidates

| Candidate | Mac | Linux | Windows | Licence | Notes |
|---|---|---|---|---|---|
| **`srt`**, Anthropic sandbox-runtime (npm `@anthropic-ai/sandbox-runtime`, pinned **0.0.78**, the newest release at least 2 weeks old) | `sandbox-exec` (Seatbelt) | bubblewrap + network namespace + seccomp | native alpha (`srt-win.exe`), or Linux mode inside WSL2 | Apache-2.0 | A wrapper around any command. Knows nothing about LLMs. A reusable library and CLI. The npm package has SLSA build provenance. |
| Codex CLI sandbox | Seatbelt | Landlock + seccomp | experimental (elevated: local accounts + firewall; unelevated: restricted token) | Apache-2.0 | Built into the Codex program, not a library on its own. |
| Containers (Docker Desktop, Podman Desktop) | Linux VM | native | WSL2 or Hyper-V VM | Docker Desktop: paid licence at 1,000 seats (S12); Podman: Apache-2.0 | Strongest isolation and heaviest. A later alternative, not the default. |
| Windows Sandbox | — | — | Pro/Enterprise only, one instance at a time | Microsoft | Ruled out: unavailable on Windows Home and cannot run in parallel. |
| Cloud microVMs (E2B, Daytona, Modal, Vercel) | — | — | — | various | Server-side agents, not laptops. Code leaves the org unless self-hosted. Relevant later for CI-triggered runs (E2-01). |

No published figures say which sandbox is most used. `srt` (inside Claude Code) and the Codex sandbox are the most deployed laptop sandboxes because they ship inside the two most-used CLI agents. `srt` is the only one of the two packaged as a standalone library. The comparison articles found are mostly vendor blogs, so their adoption claims are unverified.

## Method

1. **Native Windows `srt`.**
   - Ran `npx @anthropic-ai/sandbox-runtime@0.0.78 windows-install` on the owner's Windows 11 Home laptop.
   - Inspected the helper it elevates: `vendor/srt-win/x64/srt-win.exe`, 3,072,000 bytes, SHA-256 `B1927893DA83A1CB28054F63C8B2BC61AC4303CF63DD92C0182F79C5C414D26D`.
2. **Linux `srt`.** Throwaway probe in `spikes/s6_sandbox/`:
   - Debian bookworm, bubblewrap 0.8.0, `srt` 0.0.78, run as a non-root user (uid 1000).
   - Docker container started with `--security-opt seccomp=unconfined --security-opt systempaths=unconfined`, which bubblewrap needs to create namespaces inside a container.
   - Rules: the worktree is the only writable path; `.env` in the worktree is denied for writes; `~/.ssh` is denied for reads; the allowed domains are `pypi.org` and `files.pythonhosted.org`.
3. **WSL2 machine check.** Ubuntu 20.04 on this laptop has bubblewrap 0.4.0, no `socat` and no `ripgrep`. Installing them needs the owner's sudo password.

## Result

**Native Windows `srt` is unusable under the owner's rule.** The one-time admin install elevates `srt-win.exe`, which has no Authenticode signature, publisher or version information, so UAC shows "Unknown publisher". The owner cancelled at the UAC prompt and nothing was changed.

Even if it were signed, native Windows mode has documented limits:
- tools installed per user cannot be reached
- schannel certificate-revocation checks fail
- grants apply to the whole session
- the proxy token is visible in the runner's command line

**Linux `srt`: 16 of 16 cases behaved as required.**

| Case | Wanted | Got | How it was stopped |
|---|---|---|---|
| Write inside the worktree | allowed | allowed | |
| Write outside the worktree | blocked | blocked | Read-only file system |
| Write from a grandchild process | blocked | blocked | Read-only file system |
| Write a denied path inside the worktree (`.env`) | blocked | blocked | Permission denied |
| Write `.git/hooks/pre-commit` inside the worktree | blocked | blocked | Read-only by default, no rule needed |
| Write `.git/config` inside the worktree | blocked | blocked | Read-only by default, no rule needed |
| Write `~/.bashrc` | blocked | blocked | Read-only file system |
| Read the denied `~/.ssh` key | blocked | blocked | Hidden (no such file) |
| HTTPS to an allowed host | allowed | allowed | |
| HTTPS to another host | blocked | blocked | Proxy CONNECT 403 |
| HTTPS ignoring the proxy (`--noproxy '*'`) | blocked | blocked | No route out, name cannot resolve |
| Raw TCP to an IP address | blocked | blocked | Network unreachable |
| DNS lookup | blocked | blocked | Resolution fails |
| `git status` in the worktree | allowed | allowed | |
| `git commit` in the worktree | allowed | allowed | |
| Unix socket to a host-side server | blocked | blocked | seccomp: operation not permitted |

Wrapping one command costs about **0.18 s** (median of 5, `true`, inside the container).

**Behaviour the wrapper must handle (found by the probe):**
- **It fails closed.** A settings file missing any key (e.g. `network.deniedDomains`) makes `srt` refuse to run rather than fall back to its defaults. The wrapper always writes every key.
- **Commands go as separate arguments:** `srt --settings <file> -- bash -c "<cmd>"`. A single string is treated as one program name.
- **Reads are open by default.** The wrapper must deny the home directory and other broad regions, then re-allow the worktree and the toolchain paths it needs.

**Prerequisites per OS (from the package README, version 0.0.78):**
- **All OSes:** Node.js 20.11 or later. `srt` is a Node program, which our Python framework calls as a subprocess.
- **macOS:** `ripgrep`. The README's "no additional dependencies" line is contradicted by its own requirements list.
- **Linux:** `bubblewrap`, `socat`, `ripgrep`.
  - Ubuntu 24.04 and later restrict unprivileged user namespaces by default, so a sysctl has to change.
  - A root caller needs `CAP_SETFCAP`; prefer running as a normal user.
  - Prebuilt seccomp filters exist for x64 and arm64 only. On other architectures Unix sockets are not blocked.

## Decision

**`srt` 0.0.78 is the sandbox on every OS, behind one `agentic` sandbox interface so it can be swapped.**

| OS | How | Prerequisites `init` checks |
|---|---|---|
| macOS | `srt` (Seatbelt) | Node ≥ 20.11, `ripgrep` |
| Linux and CI | `srt` (bubblewrap) | Node ≥ 20.11, `bubblewrap`, `socat`, `ripgrep`; unprivileged user namespaces enabled |
| Windows | **`srt` in Linux mode inside WSL2.** The repo and run folders live on the WSL disk; the CLI runs inside WSL. | WSL2 with a distro carrying the Linux prerequisites (all from the distro's signed package repositories) |
| Windows, later alternative | A container backend (Docker or Podman) | S12 (`docs/spikes/S12-container-runtime-licensing.md`): engine-neutral (Docker or Podman CLI); org standard Rancher Desktop (free, Apache-2.0); Docker Desktop not required |
| Windows, excluded | Native `srt-win` | Allowed only if the org builds it from source and signs it with its own code-signing certificate |

**When no sandbox is available.**
- Org policy decides per regime.
- Default (owner decision 2026-10-20, DF-72): `team` and `regulated` refuse to run; `solo` runs with policy guards only and a warning (deny lists, Zyroflo command guards, worktree confinement).
- The run record says which sandbox each step used, or "none".

**Where the sandbox sits.**
- **Inside the sandbox:** the agent's shell commands (native backend, one `srt` call per command) and backend CLIs (wrapped as a whole process).
- **Outside the sandbox:** the engine, the gates, the ledger and the model calls to TokenLean. Commits and pushes are made by the engine.
  - Commits stay outside because a `git worktree add` worktree keeps its git metadata in the main repo's `.git`, which is outside the writable worktree.
  - Commits made by the engine also carry the run trailers (M1-45).
- **Network allowlist:** the TokenLean host, plus package registries named in org policy. Nothing else.

## What the sandbox cannot block

1. **Data sent to an allowed domain.** The allowlist works per domain: anything may be uploaded to an allowed registry or to TokenLean. Mitigations are the secret scan before egress (M1-41) and keeping the allowlist short.
2. **Reads the policy did not deny.** Reads are open by default, so the wrapper must deny broad regions (home, other repos) and re-allow only what a step needs.
3. **Resource exhaustion.** There are no CPU, memory or disk limits (M3-10).
4. **Anything the engine does itself**, outside the sandbox: model calls, commits, pushes. These are governed by policy guards and gates instead.
5. **Kernel or sandbox escapes.** The command runs as the developer's own user ID; bubblewrap and Seatbelt are the trust boundary.
6. **Files created after a command starts, when they match a Linux deny glob.** Globs are expanded when the command is wrapped. Literal deny paths are not affected.
7. **Native Windows tools.** Builds that need Windows toolchains (MSVC, .NET Framework, PowerShell modules) cannot run inside the WSL2 sandbox. Those steps get the container alternative or fall back to "no sandbox" under the regime rule.
8. **Unix sockets on uncommon CPU architectures.** The prebuilt seccomp filters cover x64 and arm64 only.
9. **macOS future risk.** Apple has deprecated `sandbox-exec`, though it still ships it, and Chrome, Codex and Claude Code still rely on it.

## Not yet tested (carried to the rows that build it)

- **Network enforcement under WSL2.** There is an open public report of unreliable network blocking for the Claude Code sandbox on WSL2. Tested live in M1-33 (DF-60).
- **macOS.** No Mac is available locally. A test script is run once on a Mac in M1-32 (DF-61).

## Cost if wrong

- **Swapping the implementation.** `srt` sits behind one sandbox interface, so a container or Codex-sandbox implementation replaces one module and its tests.
- **If WSL2 network blocking proves unreliable**, Windows falls back to the container alternative. That makes S12 (licensing) blocking for Windows.
