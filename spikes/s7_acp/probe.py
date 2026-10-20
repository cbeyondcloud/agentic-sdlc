"""Throwaway S7 probe: drive each agent over ACP (Python SDK `agent-client-protocol` 0.12.1)
against the local model stub, inside a `--network none` container.

For each agent it records:
  * initialize: protocol version, agent capabilities, auth methods, agent info;
  * session/new: modes, config options (model selector?), errors;
  * one prompt asking for `echo s7-tool-ran`: every session/update kind, every
    session/request_permission (always answered with a reject option, so a tool only runs
    if the agent never asked), the stop reason;
  * the stub's request log (paths, headers, model, tools offered, whether the planted
    AGENTS.md/GEMINI.md marker reached the prompt) and the refusing proxy's attempts.

Usage: python probe.py [agent ...]   (default: all). Results land in /out/<agent>.json.
"""

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

import acp
from acp import schema

sys.path.insert(0, os.path.dirname(__file__))
from stub import MARKER, ModelStub, RefusingProxy  # noqa: E402

OUT = Path(os.environ.get("S7_OUT", "/out"))
PROMPT = "Run exactly this shell command: " + os.environ.get("S7_CMD", "touch s7-tool-ran.txt")
HEADER_ENV = "X-Agentic-Run: s7-run,X-Agentic-Step: s7-step"


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=2), encoding="utf-8")


def agents(stub: str) -> dict:
    """Launch profile per agent: command, env, files to plant in the per-agent HOME."""
    return {
        "gemini": {
            "cmd": ["gemini", "--acp", *os.environ.get("S7_GEMINI_ARGS", "").split()],
            "env": {
                "GEMINI_API_KEY": "s7-fake-key",
                "GOOGLE_GEMINI_BASE_URL": stub,
                "GEMINI_CLI_CUSTOM_HEADERS": HEADER_ENV,
                "GEMINI_CLI_SYSTEM_SETTINGS_PATH": "{home}/system-settings.json",
                "GEMINI_MODEL": "gemini-s7",
            },
            "files": {
                "admin-policy.toml": (
                    '[[rule]]\ntoolName = "run_shell_command"\ndecision = "ask_user"\npriority = 999\n'
                ),
                "system-settings.json": {
                    "security": {"auth": {"selectedType": "gemini-api-key"}, "folderTrust": {"enabled": False}},
                    "privacy": {"usageStatisticsEnabled": False},
                    "telemetry": {"enabled": False},
                    "general": {"disableAutoUpdate": True, "disableUpdateNag": True},
                    "model": {"name": "gemini-s7"},
                },
            },
            "auth": "gemini-api-key",
        },
        "opencode": {
            "cmd": ["opencode", "acp", "--pure"],
            "env": {
                "OPENCODE_CONFIG_CONTENT": json.dumps({
                    "$schema": "https://opencode.ai/config.json",
                    "provider": {
                        "tokenlean": {
                            "npm": "@ai-sdk/openai-compatible",
                            "name": "TokenLean",
                            "options": {
                                "baseURL": stub + "/v1",
                                "apiKey": "s7-fake-key",
                                "headers": {"X-Agentic-Run": "s7-run", "X-Agentic-Step": "s7-step"},
                            },
                            "models": {"s7-model": {"name": "s7-model", "tool_call": True}},
                        }
                    },
                    "model": "tokenlean/s7-model",
                    "small_model": "tokenlean/s7-model",
                    "enabled_providers": ["tokenlean"],
                    "autoupdate": False,
                    "share": "disabled",
                    "permission": {"*": "ask", "read": "allow", "bash": {"*": "ask"}},
                }),
                "OPENCODE_DISABLE_AUTOUPDATE": "1",
                "OPENCODE_DISABLE_MODELS_FETCH": "1",
                "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
                "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
                "OPENCODE_DISABLE_CLAUDE_CODE": "1",
            },
            "files": {},
        },
        "goose": {
            "cmd": ["goose", "acp"],
            "env": {
                "GOOSE_PROVIDER": "openai",
                "GOOSE_MODEL": "s7-model",
                "OPENAI_HOST": stub,
                "OPENAI_BASE_PATH": "v1/chat/completions",
                "OPENAI_API_KEY": "s7-fake-key",
                "OPENAI_CUSTOM_HEADERS": "X-Agentic-Run=s7-run,X-Agentic-Step=s7-step",
                "GOOSE_MODE": "approve",
                "GOOSE_TELEMETRY_ENABLED": "false",
                "GOOSE_DISABLE_KEYRING": "1",
                "GOOSE_PATH_ROOT": "{home}/goose-root",
                "GOOSE_DISABLE_SESSION_NAMING": "1",
            },
            "files": {},
        },
        "copilot": {
            "cmd": ["copilot", "--acp", "--stdio"],
            "env": {
                "COPILOT_PROVIDER_BASE_URL": stub + "/v1",
                "COPILOT_PROVIDER_TYPE": "openai",
                "COPILOT_PROVIDER_API_KEY": "s7-fake-key",
                "COPILOT_MODEL": "s7-model",
                "COPILOT_OFFLINE": "true",
            },
            "files": {},
        },
        "openhands": {
            "cmd": ["/out/openhands-1.16.0", "acp", "--override-with-envs"],
            "env": {
                "LLM_API_KEY": "s7-fake-key",
                "LLM_BASE_URL": stub + "/v1",
                "LLM_MODEL": "openai/s7-model",
                "OPENHANDS_SUPPRESS_BANNER": "1",
            },
            "files": {},
        },
        "agy-acp": {
            "cmd": ["/opt/agy-acp/agy_acp_server.par", "--uid="],
            "env": {
                "GEMINI_API_KEY": "s7-fake-key",
                "GOOGLE_GEMINI_BASE_URL": stub,
            },
            "files": {".gemini/antigravity-cli/settings.json": {"modelProvider": "gemini"}},
            "auth": "gemini-api-key",
        },
    }


class Recorder:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.permissions: list[dict] = []
        self.other: list[dict] = []

    async def request_permission(self, session_id, tool_call, options, **kw):
        opts = [{"id": o.option_id, "kind": o.kind, "name": o.name} for o in options]
        self.permissions.append({"tool_call": tool_call.model_dump(mode="json", exclude_none=True), "options": opts})
        reject = next((o for o in options if o.kind in ("reject_once", "reject_always")), None)
        if reject is None:
            return schema.RequestPermissionResponse(outcome=schema.DeniedOutcome(outcome="cancelled"))
        return schema.RequestPermissionResponse(
            outcome=schema.AllowedOutcome(outcome="selected", option_id=reject.option_id)
        )

    async def session_update(self, session_id, update, **kw):
        d = update.model_dump(mode="json", exclude_none=True, by_alias=True)
        self.updates.append({"t": round(time.time(), 3), **d})

    async def write_text_file(self, *a, **kw):
        self.other.append({"write_text_file": kw})

    async def read_text_file(self, *a, **kw):
        self.other.append({"read_text_file": kw})
        raise acp.RequestError.method_not_found("fs/read_text_file")

    async def create_terminal(self, *a, **kw):
        self.other.append({"create_terminal": kw})
        raise acp.RequestError.method_not_found("terminal/create")

    async def ext_method(self, method, params):
        self.other.append({"ext_method": method})
        return {}

    async def ext_notification(self, method, params):
        self.other.append({"ext_notification": method})

    def on_connect(self, conn):
        pass


def summarise_updates(updates: list[dict]) -> dict:
    kinds: dict[str, int] = {}
    text = []
    tools = []
    usage = []
    for u in updates:
        k = u.get("sessionUpdate", "?")
        kinds[k] = kinds.get(k, 0) + 1
        if k == "agent_message_chunk":
            text.append((u.get("content") or {}).get("text", ""))
        elif k in ("tool_call", "tool_call_update"):
            tools.append({x: u.get(x) for x in ("toolCallId", "title", "kind", "status") if u.get(x)})
        elif k == "usage_update":
            usage.append(u)
    return {"kinds": kinds, "agent_text": "".join(text)[:400], "tool_events": tools[:12], "usage": usage[:3]}


async def run_agent(name: str, prof: dict, stub: ModelStub, proxy: RefusingProxy) -> dict:
    root = Path(f"/s7/{name}")
    shutil.rmtree(root, ignore_errors=True)
    home, work = root / "home", root / "work"
    prof = {**prof, "cmd": [c.replace("{home}", str(home)) for c in prof["cmd"]]}
    for rel, data in prof["files"].items():
        write(home / rel, data)
    home.mkdir(parents=True, exist_ok=True)
    write(work / "AGENTS.md", f"Project rule: {MARKER}\n")
    write(work / "GEMINI.md", f"Project rule: {MARKER}\n")
    write(work / ".goosehints", f"Project rule: {MARKER}\n")
    for rel, data in json.loads(os.environ.get("S7_PLANT", "{}")).items():
        write(work / rel, data)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "HTTPS_PROXY": proxy.url, "HTTP_PROXY": proxy.url,
        "https_proxy": proxy.url, "http_proxy": proxy.url,
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        "TERM": "dumb",
    }
    env.update({k: v.replace("{home}", str(home)) for k, v in prof["env"].items()})
    env.update(json.loads(os.environ.get("S7_EXTRA_ENV", "{}")))
    log = Path(stub.log_path)
    start_line = len(log.read_text().splitlines()) if log.exists() else 0
    proxy_start = len(proxy.attempts)
    rec = Recorder()
    result: dict = {"agent": name, "cmd": prof["cmd"]}
    stderr_path = root / "stderr.txt"
    t0 = time.time()
    try:
        async with acp.spawn_agent_process(
            rec, prof["cmd"][0], *prof["cmd"][1:], env=env, cwd=str(work),
            transport_kwargs={"stderr": open(stderr_path, "wb"), "limit": 16 * 1024 * 1024},
        ) as (conn, proc):
            init = await asyncio.wait_for(
                conn.initialize(
                    protocol_version=acp.PROTOCOL_VERSION,
                    client_capabilities=schema.ClientCapabilities(
                        fs=schema.FileSystemCapabilities(read_text_file=False, write_text_file=False),
                        terminal=False,
                    ),
                    client_info=schema.Implementation(name="s7-probe", version="0"),
                ),
                timeout=90,
            )
            result["initialize"] = init.model_dump(mode="json", exclude_none=True, by_alias=True)
            if prof.get("auth"):
                try:
                    await asyncio.wait_for(conn.authenticate(method_id=prof["auth"]), timeout=30)
                    result["authenticate"] = "ok"
                except Exception as exc:  # noqa: BLE001
                    result["authenticate"] = repr(exc)[:300]
            sess = await asyncio.wait_for(conn.new_session(cwd=str(work), mcp_servers=[]), timeout=90)
            result["new_session"] = sess.model_dump(mode="json", exclude_none=True, by_alias=True)
            if os.environ.get("S7_SET_MODEL"):
                try:
                    r = await asyncio.wait_for(
                        conn.set_config_option(config_id="model", session_id=sess.session_id,
                                               value=os.environ["S7_SET_MODEL"]), timeout=30)
                    result["set_model"] = "ok"
                except Exception as exc:  # noqa: BLE001
                    result["set_model"] = repr(exc)[:300]
            resp = await asyncio.wait_for(
                conn.prompt(session_id=sess.session_id, prompt=[acp.text_block(PROMPT)]), timeout=120
            )
            result["prompt_response"] = resp.model_dump(mode="json", exclude_none=True, by_alias=True)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"[:800]
    result["seconds"] = round(time.time() - t0, 1)
    result["tool_side_effect_file_exists"] = (work / "s7-tool-ran.txt").exists()
    result["permissions"] = rec.permissions
    result["updates"] = summarise_updates(rec.updates)
    result["client_calls"] = rec.other
    lines = log.read_text().splitlines()[start_line:] if log.exists() else []
    reqs = [json.loads(x) for x in lines]
    result["model_requests"] = [
        {k: r.get(k) for k in ("method", "path", "model", "tools", "answered_with", "marker_in_prompt", "stream")}
        | {"agentic_headers": {h: v for h, v in r["headers"].items() if h.startswith("x-agentic")},
           "auth_header": "authorization" in r["headers"] or "x-goog-api-key" in r["headers"],
           "user_agent": r["headers"].get("user-agent", "")[:80]}
        for r in reqs
    ]
    result["proxy_attempts"] = proxy.attempts[proxy_start:]
    result["stderr_tail"] = stderr_path.read_text(errors="replace")[-1500:] if stderr_path.exists() else ""
    return result


async def main(selected: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    stub = ModelStub(str(OUT / "stub_requests.jsonl"))
    proxy = RefusingProxy()
    profiles = agents(stub.url + os.environ.get("S7_PREFIX", ""))
    for name in selected or list(profiles):
        res = await run_agent(name, profiles[name], stub, proxy)
        write(OUT / f"{name}.json", res)
        print(json.dumps({k: res.get(k) for k in ("agent", "error", "seconds")}), flush=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
