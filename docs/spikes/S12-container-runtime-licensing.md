# S12: Container runtime at 1,000 seats (Docker Desktop licensing, and what to use instead)

**Date:** 2026-10-20 · **Code (throwaway):** `spikes/s12_containers/probe.py` · **Platform:** Windows 11 Home (x64) with Docker Desktop 29.8.2 already installed. No Mac was available, and no software was installed.

## Question

Developers in an organisation of about 1,000 people use Windows laptops and macOS laptops (Apple Silicon and Intel). CI runs Linux. At that size, what does Docker Desktop cost? Which free alternative works on both Windows and Mac for what the framework needs? The needs are:
- the Linux stage of `scripts/check.py`: `build`, `image inspect`, and `run -i` with files piped in on stdin, plus `--security-opt seccomp=unconfined --security-opt systempaths=unconfined` so bubblewrap runs;
- the compose stacks planned in M1-67 (TokenLean LITE, with up/down/smoke scripts in bash and PowerShell) and M2-39 (local org stack);
- the container fallback for the Windows sandbox if `srt` under WSL2 proves unreliable (S6, DF-60).

**Owner rules applied:**
- no unsigned package installation on Windows;
- licences on the allow-list (MIT, Apache-2.0, BSD, ISC, PostgreSQL);
- OSS first;
- quality and developer experience first;
- pin only releases at least two weeks old (published on or before 2026-10-06).

## Method

1. **Terms and prices** from official pages, all read on 2026-10-20:
   - Docker pricing: <https://www.docker.com/pricing/>
   - Docker Desktop licence: <https://docs.docker.com/subscription/desktop-license/>
   - Docker Engine install and licence: <https://docs.docker.com/engine/install/>
2. **Versions and release dates** from the GitHub releases API of each project, read on 2026-10-20.
3. **Platform support** from the release notes and install docs:
   - Podman `RELEASE_NOTES.md`
   - <https://podman-desktop.io/docs/installation/windows-install>
   - <https://podman-desktop.io/docs/installation/macos-install>
   - <https://podman-desktop.io/docs/configuration/managed-configuration>
   - <https://docs.rancherdesktop.io/getting-started/installation/>
   - <https://docs.rancherdesktop.io/getting-started/deployment/>
4. **Windows signatures, checked directly.**
   - Each Windows installer and CLI was downloaded into a scratch folder, never run, and checked with `Get-AuthenticodeSignature`.
   - Signing configuration in the source repositories was read for anything that could not be downloaded and checked. That covers macOS, and the files inside Rancher's MSI.
5. **Licences** from the GitHub licence API, or the repository's LICENSE file.
6. **Podman's handling of `--security-opt`** read in the source at tag v6.1.3.
7. **Probe** (`spikes/s12_containers/probe.py`).
   - It reports every engine CLI on PATH with its version, Windows signature, server, licence note and whether compose works.
   - It resolves a `CONTAINER_ENGINE` switch.
   - With `--run`, it runs bubblewrap inside the check image using `check.py`'s flags.

## Results

### Docker Desktop terms (official pages, 2026-10-20)

- **Free** only for "small businesses (fewer than 250 employees AND less than $10 million in annual revenue)", personal use, education and non-commercial open source. "Professional use in larger organizations" and government entities need a paid subscription.
  - A 1,000-person organisation is over the employee limit, whatever its revenue.
- **Docker Engine is not covered by this.** The Engine install page states its licence as Apache 2.0. Its paid-subscription sentence applies only to "Docker Engine obtained via Docker Desktop".
  - So Docker Engine installed from Docker's Linux packages (CI, or inside WSL2) costs nothing at any size.

| Plan | Price per user per month | Seat limit | Relevant inclusions |
|---|---|---|---|
| Personal | $0 | 1 | Not allowed for this organisation's professional use |
| Pro | $11 monthly / $9 annual | 1 | Per individual; not an organisation plan |
| Team | $16 monthly / $15 annual | **up to 100** | Audit logs, Docker Hub role-based access control |
| Business | **$24** (annual; monthly price on request) | no cap | SSO, SCIM, Hardened Docker Desktop, Image and Registry Access Management, Enhanced Container Isolation, purchase by invoice |

**Cost at 1,000 seats.**
- Team is capped at 100 users, so Business is the only plan that fits: 1,000 × $24 × 12 = **$288,000 a year at list price**.
- Seats are needed only for people who actually run Docker Desktop. At 600 users it would be $172,800.
- Volume discounts go through Docker sales and were not explored.
- Docker last changed its prices on 2024-12-10, when Pro and Team rose and Business stayed at $24 (per the pricing page's "legacy plans prior to December 10, 2024" note). No 2026 change was found.

### Alternatives

Every version named below is the newest stable release at least two weeks old.

| | Docker Desktop | **Rancher Desktop** | **Podman Desktop + Podman** | Docker Engine inside WSL2 | Colima |
|---|---|---|---|---|---|
| **Version** | 29.8.2 (here) | 1.24.0 (2026-07-29) | Desktop 1.29.3 (2026-09-01), with Podman 6.1.3 (2026-09-29); Podman 5.8.8 (2026-09-29) for Intel Macs | docker-ce 29.8.2 (2026-09-30) | 0.10.3 (2026-06-04) |
| **Licence** | Proprietary subscription | Apache-2.0 | Apache-2.0 (both). The `podman-compose` add-on is **GPL-2.0**, so excluded | Apache-2.0 (moby, cli, compose, buildx) | MIT (Lima: Apache-2.0) |
| **Cost at 1,000 seats** | $288k a year (list) | $0 | $0 | $0 | $0 |
| **Windows** | Yes | Windows 11 (Home included) via WSL2 | Windows 11 via WSL2 (default, any edition) or Hyper-V (Pro or Enterprise). Podman 6 dropped Windows 10 | Yes, inside any WSL2 distro; no Windows binaries | No |
| **Mac, Apple Silicon** | Yes | Yes (macOS 13 or later) | Yes. libkrun is the default machine provider from Podman 6.0 | n/a | Yes |
| **Mac, Intel** | Yes | **Yes** (macOS 13 or later) | **Podman 6 removed Intel Macs.** They need Podman 5.8.x, which is still patched (5.8.9 on 2026-10-12) but has no published end date | n/a | Yes |
| **Windows installer signature (verified)** | `docker.exe` and `docker-compose.exe`: valid, "Docker Inc" | MSI: valid EV signature, "SUSE LLC" (SSL.com EV CA). **Inside it, `docker-compose.exe` and `docker-buildx.exe` are deliberately unsigned** (`build/signing-config-win.yaml` at v1.24.0); `docker.exe` and `nerdctl.exe` are signed | Podman 6.1.3 MSI, Podman 5.8.8 MSI and Podman Desktop 1.29.3 setup: all valid EV signatures, "Red Hat, LLC" (DigiCert G4) | No Windows installer. Packages come from Docker's GPG-signed apt repository. The static Windows `docker.exe`/`dockerd.exe` 29.8.2 are **unsigned** | n/a |
| **Mac signing** | Signed and notarized | Code-signed and notarized (`scripts/lib/sign-macos.ts`) | Desktop: hardened runtime and notarized (`.electron-builder.config.cjs`). Podman pkg: `codesign` + `productsign` with timestamp (`contrib/pkginstaller/package.sh`); notarization not confirmed | n/a | Homebrew only; no signed installer |
| **Docker CLI and API** | Native | **Native dockerd (moby)**, chosen in settings; containerd/nerdctl is the other option | Podman CLI, plus a Docker-compatible API (v1.44 in Podman 6.0) on a socket | Native | Native dockerd in a Lima VM; docker CLI from Homebrew |
| **Compose** | Bundled, signed | Bundled `docker compose` (unsigned exe on Windows) | `podman compose` hands off to docker-compose. Podman Desktop's Compose extension **downloads the upstream `docker-compose` from GitHub, which is unsigned on Windows** (v5.6.0 checked) | `docker-compose-plugin` from the signed apt repo | Homebrew `docker-compose` |
| **`--security-opt seccomp=unconfined systempaths=unconfined`** | Works (probe) | Same dockerd, so the same flags; not run here | Both accepted. `systempaths=unconfined` is a Docker-compatibility alias for `unmask=ALL` (`pkg/specgenutil/specgen.go:751-756`, v6.1.3). Not run here | Same dockerd | Same dockerd |
| **Image build, arm64** | buildx | buildx (moby) | buildah inside `podman build`; native arm64 on Apple Silicon | buildx | buildx |
| **Fleet management** | Settings Management (Business) | **Deployment profiles**: defaults and locked settings via the HKLM policy registry (Group Policy) or `/Library/Managed Preferences` plists (MDM), e.g. `containerEngine.name=moby`, `kubernetes.enabled=false` | **Managed configuration**: `default-settings.json` and `locked.json` under `%PROGRAMDATA%` or `/Library/Application Support` | None (apt plus a `wsl.conf` with systemd) | None |
| **Weight** | Moderate | Heavy: 640 MB MSI, bundles k3s (can be switched off) | Light: 27 MB MSI, 144 MB Desktop | Lightest | Light |

**Excluded.**
- OrbStack: proprietary licence, macOS only.
- `podman-compose`: GPL-2.0.
- The static Docker CLI for Windows: unsigned.
- Native Windows Docker Engine: it runs Windows containers only, so it cannot run our Linux images.

### Probe on this machine

```
docker: Docker version 29.8.2 | signature Valid, CN=Docker Inc | server Docker Desktop x86_64
        compose v5.6.0 | licence: needs a paid seat at >=250 staff | bwrap_in_check_image: bubblewrap OK
podman, nerdctl: not on PATH
CONTAINER_ENGINE=podman -> resolved None (switch honoured; no fallback to docker)
```

**Control:** the same `bwrap` run in `agentic-check-linux:ffe7cd98771a` *without* the two `--security-opt` flags fails with "No permissions to create a new namespace". Any engine we support must accept both flags, and Podman's CLI does (source check above).

## Decision (recommendation, pending owner approval)

1. **Do not buy Docker Desktop for the framework.** It costs $288k a year at list price for 1,000 seats, and nothing we need depends on it. If the organisation already licenses Business seats for other reasons, the framework works with it unchanged.
2. **The framework is engine-neutral.**
   - Containers are driven only through the Docker CLI command surface (`build`, `image inspect`, `run`, `compose`) and the Compose Specification.
   - The engine binary is resolved once: `CONTAINER_ENGINE` (`docker` | `podman`) if set, otherwise the first of `docker`, `podman` on PATH.
   - No code, script or doc hard-codes `docker`, and no compose file uses Docker-only extensions.
   - `agentic init` (M1-53) reports the engine found and its server. When the server is Docker Desktop it adds a one-line licence note, but never blocks.
3. **Org-standard laptop runtime: Rancher Desktop 1.24.0, with the dockerd (moby) engine and Kubernetes off, set and locked through a deployment profile.**
   - **Why this one:**
     - It is the only free option that is one product on all three laptop types: Windows 11, Apple Silicon Mac and Intel Mac.
     - It runs the real dockerd and docker CLI, so `check.py`, the `--security-opt` flags and the compose stacks behave as they do on Linux CI with Docker Engine. No second code path, and no Podman compatibility gaps to chase.
     - Its installers are EV-signed on Windows and notarized on Mac.
     - IT can push and lock its settings by Group Policy or MDM.
   - **One gap: its bundled `docker-compose.exe` and `docker-buildx.exe` are unsigned on Windows (owner ruling below).**
4. **Supported alternative: Podman Desktop 1.29.3 with Podman 6.1.3, on Windows 11 and Apple Silicon**, selected with `CONTAINER_ENGINE=podman` or through Podman's Docker-compatible socket.
   - **Why:** it is lighter, rootless by default and fully Red Hat EV-signed on Windows.
   - **Why it is not the default:**
     - Podman 6 does not support Intel Macs, so those would need the separate 5.8.x line.
     - Compose comes from an unsigned GitHub download.
     - bubblewrap inside a rootless, SELinux-enforcing Podman machine is untested (it may also need `--security-opt label=disable`).
5. **For Windows developers who already work inside WSL2** (the S6 layout, where the repo and the agentic CLI live in WSL): Docker Engine (docker-ce plus the compose plugin) inside their WSL2 distro, from Docker's signed apt repository.
   - It is free, has no Windows binaries at all, so the signing question does not arise, and is identical to Linux CI.
   - It is not the default because it has no GUI and each developer has to set up apt, sudo and systemd.
   - It is also the natural container backend if the WSL2 `srt` network test (DF-60) fails.
6. **macOS command-line option: Colima 0.10.3 with the Homebrew docker CLI**, for developers who prefer no GUI. It is not the org standard: Homebrew only, no signed installer, no managed settings.
7. **Linux CI:** Docker Engine (docker-ce) from Docker's apt repository, or the distribution's Podman. Free, so no licence question.

## What this cannot do or did not test

- **Only Docker Desktop was exercised.** Rancher Desktop and Podman were not installed (owner rule), so these are untested: the check stage, bubblewrap inside the VM, compose stacks, stdin piping through `podman.exe` (a remote client), and WSL integration.
- **No Mac.** The Mac signing claims come from the projects' signing scripts, not from checking `codesign` or `spctl` output. Which Podman version Podman Desktop installs on an Intel Mac was not checked.
- **Unsigned compose on Windows.** Under a strict WDAC or AppLocker policy that blocks unsigned executables, `docker compose` is blocked with every free option except Docker Engine inside WSL2. Docker Desktop is the only Windows product with a signed compose.
- **Podman's Docker-API compatibility.** v1.44, with v1.45 in progress, is good for `run` and `build`. Compose features such as build secrets, healthcheck dependencies and some network modes differ in practice and would need testing per stack.
- **Prices and terms are list prices** read on 2026-10-20. Volume discounts were not explored. This record is not legal advice.
- **Windows 10 is out of scope.** Both Rancher Desktop 1.24 and Podman 6 require Windows 11. Microsoft's Windows 10 support ended on 2025-10-14, with paid extended security updates after that.

## Needs the owner

1. **Approve "do not buy Docker Desktop"**, or decide that the organisation buys Business seats anyway, for example for Hardened Desktop, SSO or vendor support.
2. **Signing rule scope.** Is a signed installer (an EV-signed MSI) that contains unsigned Apache-2.0 helper executables (`docker-compose.exe`, `docker-buildx.exe`) acceptable? If not, choose one:
   - the organisation re-signs those two binaries with its own code-signing certificate, as S6 allows for `srt-win`;
   - compose on Windows runs only inside WSL2.

## Suggested follow-up rows and deferred items

| Item | Target row |
|---|---|
| Add the `CONTAINER_ENGINE` switch to `scripts/check.py`: resolve the engine once and replace the literal `"docker"`. For Podman, also pass `--security-opt label=disable` if the live test shows SELinux denies bubblewrap's `/proc` mount | next change to `check.py` (P0-03 follow-up) or M1-67 |
| Live run of `check.py --linux` and the probe (`--run`) under Rancher Desktop (moby) and Podman 6.1 on Windows 11 and macOS (Apple Silicon and Intel) | M1-67 (its done-when already requires Windows, macOS and Linux) |
| M1-67 and M2-39 scripts (bash and PowerShell) resolve the engine; compose files are limited to features both `docker compose` and `docker-compose` against the Podman socket support; smoke runs on Rancher Desktop and Podman | M1-67, M2-39 |
| `init` readiness: detect the engine and server, print the Docker Desktop licence note, never block | M1-53 |
| Owner ruling on unsigned compose and buildx executables inside a signed installer | P0-04 (spike review) |
| Intel Mac fleet: Podman 6 dropped Intel and Rancher still supports it; review yearly | P0-04 note, then a yearly review |
| DF-96 follow-on: with a free engine standard on every laptop, the Linux-stage secret scan is available to every Windows developer | M3-21 |

## Cost if wrong

- **Everything is behind the engine switch,** so changing the recommended product is an IT packaging and documentation change, not a code change.
- **If Rancher Desktop proves unreliable on the fleet,** Podman Desktop is already supported. Intel Macs would then need Podman 5.8.x or Colima.
- **If the organisation buys Docker Desktop later,** the framework already works with it.
- **The real risk is the signing rule.** If unsigned compose executables are disallowed and re-signing is refused, Windows compose (M1-67, M2-39) must run inside WSL2.
