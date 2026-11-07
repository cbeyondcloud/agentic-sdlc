# b20-codeintel-net: codebase-memory-mcp under the sandbox, Linux build only on Windows

> Track b20-codeintel-net. Owner decisions settled 2026-11-07 (not re-opened): DF-921, DF-925.

**Goal:** the optional JVM backend codebase-memory-mcp (M3-27) always runs inside the OS
sandbox with no allowed host (DF-921), and Windows developers use its Linux build inside
WSL2 only: the native Windows build is never offered, fetched or run (DF-925). Where
either cannot hold, the backend is refused (never run unsandboxed) and doctor, preflight
and init say why and what to install. The Maven/Gradle conventions keep answering.

**Reuse:** the existing `SrtSandbox` (D38, `srt` Apache-2.0) and the existing "run
agentic inside WSL2" route (D38: native Windows has no sandbox; inside WSL2 the platform
is Linux, so the fetcher picks the Linux build). No new sandbox, no new signing rule, no
new dependency.

## Files

- `core/src/agentic/codeintel/cbm.py`: every call goes through `SrtSandbox` with
  `allowed_domains=()`, writes only the adapter's data folder, policy's `deny_read`
  hidden; `srt` and `deny_read` are constructor arguments (tests pass a fake `srt`).
- `commons/src/sdlc_commons/tool_fetch.py`: `ToolFetcher(wsl2_only=...)`: tools whose
  native Windows build is never offered (`withheld`, `fetch`, `cached`, `planned`).
- `cli/src/agentic_cli/codeintel.py`: `locate_backend(locator, ready)`: native Windows
  host is a permanent refusal pointing to WSL2; no ready sandbox is a refusal naming the
  missing checks; `Backend.fix`; `codeintel_for(..., deny_read=)`.
- `cli/src/agentic_cli/runtime.py`, `doctor.py`, `step_extras.py`, `dev_init.py`: pass
  the sandbox readiness and `wsl2_only`; init's on-request list says why the backend is
  not offered on native Windows.
- `templates/policy-repo/policy/org.yaml`: the commented pin drops the Windows asset.
- Tests beside each.

## Interfaces

```python
CodebaseMemory(command, tree, data, *, srt=("srt",), deny_read=(), index_timeout_s=600)
ToolFetcher(..., wsl2_only: Collection[str] = ()) ; ToolFetcher.withheld(name) -> str | None
locate_backend(locator, ready: Callable[[], Readiness]) -> Backend   # Backend.fix: str
codeintel_for(repo, settings, *, crg_available, backend, deny_read=()) -> Factory | None
```

## TDD steps

1. core: a fake `srt` records its settings: zero allowed domains, writes only the data
   folder (+ srt's tmp), deny_read passed; a missing `srt` is a CodeIntelError, never an
   unsandboxed call. Watch fail, implement, pass.
2. commons: a `wsl2_only` tool on `windows-x86_64` is `unavailable` with a WSL2 detail,
   never downloaded, not `cached`, `planned` raises; other platforms unchanged.
3. cli: `locate_backend` on a Windows platform is permanent with a WSL2 message even
   for a `signed` build; a not-ready sandbox is a non-permanent refusal whose `fix`
   names the missing checks; ready + installed is the path. `codeintel_status` shows
   `Backend.fix`. `codeintel_for` passes `deny_read`. Init on Windows lists the tool as
   not offered (WSL2), never installs it.
4. Template and its test: no Windows asset.
5. Records: DF-921, DF-925 Done; M3-27 notes.

## Review focus

- No path runs the binary outside `srt`; the settings file has `allowedDomains: []`.
- Native Windows never fetches, caches or runs the Windows build, whatever `signed` says.
- Messages are actionable (what to install, or move to WSL2) and `auto` does not nag.
