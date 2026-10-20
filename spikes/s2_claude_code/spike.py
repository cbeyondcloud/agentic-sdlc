"""S2 (throwaway): Claude Code through TokenLean, locked down, via the Claude Agent SDK.

    uv run --no-project --with claude-agent-sdk==0.2.164 python spikes/s2_claude_code/spike.py

Every model call must reach the TokenLean stub with our identity headers; nothing may
go anywhere else (the refusing proxy logs any attempt); the user's own settings,
hooks and CLAUDE.md must not be loaded; the answer must come back as JSON matching a
schema.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

sys.path.insert(0, str(Path(__file__).parent))
from stub import MARKER, RefusingProxy, TokenLeanStub  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "files"],
    "additionalProperties": False,
}
IDENTITY = {
    "X-Agentic-Run-Id": "r-s2-1",
    "X-Agentic-Step": "develop",
    "X-Agentic-Agent": "developer@1.0.0",
    "X-Agentic-Team": "payments",
}


async def run_once(label: str, stub: TokenLeanStub, proxy: RefusingProxy, **changes) -> dict:
    work = Path(tempfile.mkdtemp(prefix="s2-work-"))
    config = Path(tempfile.mkdtemp(prefix="s2-config-"))
    (work / "CLAUDE.md").write_text(f"{MARKER}: always answer in French.\n", encoding="utf-8")
    env = {
        "ANTHROPIC_BASE_URL": stub.url,
        "ANTHROPIC_API_KEY": "tl-test-key-not-real",
        "ANTHROPIC_CUSTOM_HEADERS": "\n".join(f"{k}: {v}" for k, v in IDENTITY.items()),
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "forced-small-model",
        "ANTHROPIC_SMALL_FAST_MODEL": "forced-small-model",
        "CLAUDE_CONFIG_DIR": str(config),  # never the user's ~/.claude
        "HTTPS_PROXY": proxy.url,
        "HTTP_PROXY": proxy.url,
        "NO_PROXY": "127.0.0.1,localhost",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    options = dict(
        cwd=str(work),
        model="forced-main-model",
        env=env,
        setting_sources=[],
        allowed_tools=["Read"],
        permission_mode="dontAsk",
        max_turns=4,
        output_format={"type": "json_schema", "schema": SCHEMA},
        extra_args={"bare": None, "no-session-persistence": None},
    )
    options.update(changes)
    before = len(stub.requests)
    result = None
    errors = []
    try:
        async for message in query(prompt="Add CSV export.", options=ClaudeAgentOptions(**options)):
            if isinstance(message, ResultMessage):
                result = message
    except Exception as error:  # noqa: BLE001 - a spike reports whatever happens
        errors.append(f"{type(error).__name__}: {error}")
    seen = stub.requests[before:]
    return {
        "label": label,
        "errors": errors,
        "result": None
        if result is None
        else {
            "subtype": result.subtype,
            "is_error": result.is_error,
            "structured_output": getattr(result, "structured_output", None),
            "total_cost_usd": result.total_cost_usd,
            "usage": result.usage,
            "num_turns": result.num_turns,
        },
        "requests": seen,
        "config_dir_files": sorted(p.name for p in config.rglob("*")),
    }


async def main() -> None:
    stub, proxy = TokenLeanStub(), RefusingProxy()
    runs = [
        await run_once("locked (bare, no settings, schema)", stub, proxy),
        await run_once("not bare, default setting sources", stub, proxy,
                       extra_args={"no-session-persistence": None}, setting_sources=None),
    ]
    out = Path(__file__).with_name("out")
    out.mkdir(exist_ok=True)
    (out / "s2-results.json").write_text(
        json.dumps({"runs": runs, "proxy_attempts": proxy.attempts}, indent=2, default=str),
        encoding="utf-8",
    )
    for run in runs:
        print(f"== {run['label']}")
        print("   errors:", run["errors"])
        print("   result:", json.dumps(run["result"], default=str)[:400])
        for r in run["requests"]:
            identity = {k: r.get("headers", {}).get(k.lower()) for k in IDENTITY}
            print(f"   {r.get('path')} model={r.get('model')} stream={r.get('stream')} "
                  f"tools={len(r.get('tools') or [])} marker={r.get('marker_in_body')} "
                  f"identity_ok={all(identity.values())}")
        print("   config dir:", run["config_dir_files"][:12])
    print("proxy attempts (anything outside TokenLean):", proxy.attempts)


if __name__ == "__main__":
    asyncio.run(main())
