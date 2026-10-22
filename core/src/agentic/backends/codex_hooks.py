"""The PreToolUse hook that runs our guard inside Codex (M2-40, DF-116).

Codex's execpolicy has no default-deny, and a redirect hides a listed command from its
prefix rules (spike S3 Q6; `echo ok > f && git push` pushed). Codex 0.160.1 runs
Claude-Code-style hooks before each tool call. A PreToolUse command hook on the shell
tool (`Bash`) and on `apply_patch` hands the call to the guard, which then judges
every command:
  * deny rules and the framework floor;
  * the allow-list, so an unlisted program is refused (default-deny);
  * redirect-aware splitting;
  * writes to protected or denied paths.
The guard is `sdlc_commons.codex_hook`, wired by the CLI (core cannot import commons).
This module builds only the hook's config table.

Codex runs a hook from the user layer (our CODEX_HOME config) only when it is trusted:
`hooks.state."<config path>:pre_tool_use:<group>:<handler>".trusted_hash` must equal
Codex's hash of the normalised hook. That hash is sha256 over canonical JSON (sorted
keys, compact) of `{event_name, matcher, hooks: [handler]}`, the handler with its
timeout and `async` filled in (`hooks/src/engine/discovery.rs::hook_hash`,
`config/src/fingerprint.rs::version_for_toml`). The contract test checks it against
the real binary: a wrong hash means an untrusted hook, which silently does not run.

Codex treats a hook that crashes, times out or prints malformed JSON as passed (it
fails open). The guard hook therefore answers with exit code 2 and the reason on
stderr, and turns every error it catches into a refusal.
"""

import hashlib
import json
import shlex
from dataclasses import dataclass
from pathlib import Path

EVENT = "PreToolUse"
EVENT_KEY = "pre_tool_use"
#: The shell tool (Codex names it `Bash` for hooks) and file patches.
MATCHER = "^(Bash|apply_patch)$"


@dataclass(frozen=True)
class CodexHook:
    """A command Codex runs before each shell command and patch: its stdin is the call
    as JSON; exit code 2 with a reason on stderr refuses it."""

    argv: tuple[str, ...]
    timeout_s: int = 60

    @property
    def command(self) -> str:
        return shlex.join(self.argv)


def hook_table(hook: CodexHook, config_path: Path) -> dict:
    """The `[hooks]` table for `hook`, trusted for the config file at `config_path`."""
    handler = {"type": "command", "command": hook.command, "timeout": hook.timeout_s}
    key = f"{Path(config_path).as_posix()}:{EVENT_KEY}:0:0"
    return {
        EVENT: [{"matcher": MATCHER, "hooks": [handler]}],
        "state": {key: {"trusted_hash": trusted_hash(MATCHER, handler)}},
    }


def trusted_hash(matcher: str, handler: dict) -> str:
    """Codex's trust hash of one PreToolUse command handler under `matcher`."""
    normalised = {"async": False, **handler}
    identity = {"event_name": EVENT_KEY, "matcher": matcher, "hooks": [normalised]}
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
