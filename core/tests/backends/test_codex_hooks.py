"""Tests for the codex PreToolUse hook table (M2-40, DF-116): the hook that runs our guard
before each shell command and patch, and the `trusted_hash` Codex requires before it runs
a hook from the user layer (codex-rs `hooks/src/engine/discovery.rs`, `config/src/
fingerprint.rs` at rust-v0.160.1; the contract test checks it against the real binary)."""

import hashlib
import json
import tomllib
from pathlib import Path

from conformance import slice_

from agentic.backends.codex import CodexSettings, lock_down
from agentic.backends.codex_hooks import MATCHER, CodexHook, hook_table, trusted_hash
from agentic.backends.codex_rules import ShellRules
from agentic.engine.engine import StepContext
from agentic.transport import CallIdentity

HOOK = CodexHook(argv=("/venv/bin/python", "-I", "-m", "guard_hook", "/tmp/guard spec.json"))


def test_hook_table_shape():
    table = hook_table(HOOK, Path("/codex/home/config.toml"))
    [group] = table["PreToolUse"]
    assert group["matcher"] == MATCHER
    [handler] = group["hooks"]
    assert handler == {
        "type": "command",
        "command": "/venv/bin/python -I -m guard_hook '/tmp/guard spec.json'",
        "timeout": 60,
    }
    key = "/codex/home/config.toml:pre_tool_use:0:0"
    assert table["state"] == {key: {"trusted_hash": trusted_hash(MATCHER, handler)}}


def test_trusted_hash_is_sha256_over_codex_canonical_json():
    handler = {"type": "command", "command": "guard", "timeout": 5}
    identity = {
        "event_name": "pre_tool_use",
        "hooks": [{"async": False, "command": "guard", "timeout": 5, "type": "command"}],
        "matcher": "Bash",
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    expected = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    assert trusted_hash("Bash", handler) == expected


def test_a_changed_command_changes_the_hash():
    one = {"type": "command", "command": "guard a", "timeout": 5}
    two = {**one, "command": "guard b"}
    assert trusted_hash(MATCHER, one) != trusted_hash(MATCHER, two)


def test_lock_down_writes_hooks_in_the_file_but_never_as_overrides(resolved, tmp_path):
    agent = resolved.model_copy(update={"backend": "codex", "temperature": None, "effort": None})
    step = StepContext("r-1", "develop", 1, agent, slice_(), None)
    identity = CallIdentity(
        team="t",
        user="u",
        repo="r",
        pipeline="p",
        run_id="r-1",
        step="develop",
        agent="developer@1.2.0",
        policy_version="v1",
    )
    (tmp_path / "tree").mkdir()
    launch = lock_down(
        step,
        settings=CodexSettings(gateway_url="http://gw", api_key="k", home_root=tmp_path / "h"),
        identity=identity,
        rules=ShellRules(),
        cwd=tmp_path / "tree",
        srt_boundary=True,
        hook=HOOK,
        environ={"PATH": "/usr/bin"},
    )
    launch.write("x")
    config = tomllib.loads((launch.home / "config.toml").read_text(encoding="utf-8"))
    key = f"{(launch.home / 'config.toml').as_posix()}:pre_tool_use:0:0"
    assert key in config["hooks"]["state"]
    assert launch.env["CODEX_HOME"] == str(launch.home)
    assert launch.home.is_absolute() and launch.home == launch.home.resolve()
    assert not any(arg.startswith("hooks") for arg in launch.argv)
