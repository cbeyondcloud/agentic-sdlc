"""S2 part 2 (throwaway): which tools the model is offered, whether Claude Code's
permission rules (compiled by `Guard.claude_permissions`) hold, and a negative
control for the refusing proxy.

    <env with claude-agent-sdk==0.2.164>/python spikes/s2_claude_code/spike2.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

sys.path.insert(0, str(Path(__file__).parent))
from stub import RefusingProxy, TokenLeanStub  # noqa: E402

LOCKED_ENV = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1",
}
PERMISSIONS = {  # what Guard.claude_permissions() produces for allow ["echo"], deny git push
    "permissions": {
        "allow": ["Bash(echo:*)"],
        "ask": [],
        "deny": ["Bash(git push:*)", "Edit(infra/**)"],
    }
}


def bash(command: str, n: int) -> list[dict]:
    return [{"type": "tool_use", "id": f"toolu_{n}", "name": "Bash", "input": {"command": command}}]


async def run(stub, proxy, label, *, locked_env=True, queue=(), **changes) -> dict:
    work = Path(tempfile.mkdtemp(prefix="s2b-work-"))
    env = {
        "ANTHROPIC_BASE_URL": stub.url,
        "ANTHROPIC_API_KEY": "tl-test-key-not-real",
        "CLAUDE_CONFIG_DIR": tempfile.mkdtemp(prefix="s2b-config-"),
        "HTTPS_PROXY": proxy.url,
        "HTTP_PROXY": proxy.url,
        "NO_PROXY": "127.0.0.1,localhost",
        **(LOCKED_ENV if locked_env else {}),
    }
    options = dict(
        cwd=str(work),
        model="forced-main-model",
        env=env,
        setting_sources=[],
        permission_mode="dontAsk",
        max_turns=6,
        extra_args={"bare": None, "no-session-persistence": None},
    )
    options.update(changes)
    stub.queue = [list(q) for q in queue]
    before, attempts_before = len(stub.requests), len(proxy.attempts)
    result, errors = None, []
    try:
        async for message in query(prompt="Do the task.", options=ClaudeAgentOptions(**options)):
            if isinstance(message, ResultMessage):
                result = message
    except Exception as error:  # noqa: BLE001
        errors.append(f"{type(error).__name__}: {error}")
    await asyncio.sleep(2)  # late background requests
    seen = stub.requests[before:]
    return {
        "label": label,
        "errors": errors,
        "subtype": getattr(result, "subtype", None),
        "offered_tools": [r.get("tools") for r in seen if r.get("path", "").startswith("/v1/messages?")],
        "tool_results": [r.get("tool_results") for r in seen if r.get("tool_results")],
        "files": sorted(p.name for p in work.iterdir()),
        "proxy_attempts": proxy.attempts[attempts_before:],
        "paths": [r.get("path") for r in seen],
    }


async def main() -> None:
    stub, proxy = TokenLeanStub(), RefusingProxy()
    results = [
        await run(stub, proxy, "tools=['Read'] (availability)", tools=["Read"]),
        await run(
            stub,
            proxy,
            "permissions from the guard: echo allowed, git push denied, curl unlisted",
            tools=["Bash", "Read"],
            settings=json.dumps(PERMISSIONS),
            queue=[
                bash("echo ok > allowed.txt", 1),
                bash("git push origin main", 2),
                bash("curl -s https://example.com -o fetched.html", 3),
                [{"type": "text", "text": "done"}],
            ],
        ),
        await run(
            stub,
            proxy,
            "allowed echo without a redirect",
            tools=["Bash", "Read"],
            settings=json.dumps(PERMISSIONS),
            queue=[bash("echo s2-plain-echo", 4), [{"type": "text", "text": "done"}]],
        ),
        await run(stub, proxy, "negative control: telemetry switches off", locked_env=False,
                  tools=["Read"]),
    ]
    out = Path(__file__).with_name("out")
    out.mkdir(exist_ok=True)
    (out / "s2b-results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    for r in results:
        print(f"== {r['label']}")
        for key in ("errors", "subtype", "offered_tools", "tool_results", "files", "proxy_attempts", "paths"):
            print(f"   {key}: {r[key]}")


if __name__ == "__main__":
    asyncio.run(main())
