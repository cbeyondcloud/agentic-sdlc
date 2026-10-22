"""Test helper: a scripted ACP agent, run as a subprocess over stdio exactly as a real agent
CLI is (M2-57). Built on the ACP SDK's agent side, so the backend's client, transport and
process handling are exercised on every OS without any agent binary.

    python acp_fake_agent.py <script.json>

The script (JSON) holds:
  * "log": a JSONL file where every call the client made is recorded;
  * "auth_methods" / "modes": what initialize and session/new advertise;
  * "watch": home-relative files whose existence session/new and every prompt log;
  * "turns": one list of actions per prompt (later prompts answer "VALID default"):
      {"say": text}                       an agent message chunk
      {"tool": title, "command": c}       a tool call; with "ask": true it first asks the
                                          client's permission ("options": "no_reject"
                                          offers no reject option) and logs the answer
      {"cost": usd}                       a usage_update with the session's cost
      {"usage": {"input": n, "output": n, "cache_read": n}}   the turn's PromptResponse usage
      {"spawn": true}                     a background process left running (its pid logged)
      {"sleep": seconds}
      {"stop": reason}                    the turn's stop reason (default end_turn)
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import acp
from acp import schema

LOGGED_ENV = (
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "FAKE_QUIET",
    "FAKE_ROUTE",
    "FAKE_INHERITED",
    "OPENAI_API_KEY",
    "NO_PROXY",
    "no_proxy",
)


class FakeAgent:
    def __init__(self, script: dict) -> None:
        self.script = script
        self.log_path = Path(script["log"])
        self.turn = 0
        self.conn = None
        self.calls = 0

    def log(self, event: str, **data) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": event, **data}) + "\n")

    def watched(self) -> dict[str, bool]:
        home = Path(os.environ["HOME"])
        return {name: (home / name).exists() for name in self.script.get("watch", [])}

    def on_connect(self, conn) -> None:
        self.conn = conn

    async def initialize(self, protocol_version, client_capabilities=None, client_info=None, **kw):
        caps = client_capabilities.model_dump(mode="json", by_alias=True, exclude_none=True)
        env = {name: os.environ.get(name) for name in LOGGED_ENV}
        self.log(
            "initialize", protocol=protocol_version, capabilities=caps, env=env, argv=sys.argv[2:]
        )
        self.log("cwd", cwd=os.getcwd())
        methods = [
            schema.AuthMethodAgent(id=name, name=name)
            for name in self.script.get("auth_methods", [])
        ]
        return schema.InitializeResponse(
            protocol_version=self.script.get("protocol", 1),
            agent_capabilities=schema.AgentCapabilities(),
            auth_methods=methods,
        )

    async def authenticate(self, method_id, **kw):
        self.log("authenticate", method=method_id)
        return schema.AuthenticateResponse()

    async def new_session(self, cwd, mcp_servers=None, **kw):
        self.log("new_session", cwd=cwd, mcp_servers=len(mcp_servers or []), watched=self.watched())
        modes = None
        if self.script.get("modes"):
            available = [schema.SessionMode(id=m, name=m) for m in self.script["modes"]]
            modes = schema.SessionModeState(
                current_mode_id=available[0].id, available_modes=available
            )
        return schema.NewSessionResponse(session_id="s-1", modes=modes)

    async def set_session_mode(self, session_id, mode_id, **kw):
        self.log("set_mode", mode=mode_id)
        return schema.SetSessionModeResponse()

    async def prompt(self, session_id, prompt, **kw):
        text = "\n".join(getattr(block, "text", "") for block in prompt)
        self.log("prompt", text=text, blocks=len(prompt), watched=self.watched())
        turns = self.script.get("turns", [])
        actions = turns[self.turn] if self.turn < len(turns) else [{"say": "VALID default"}]
        self.turn += 1
        usage, stop = None, "end_turn"
        for action in actions:
            if "say" in action:
                update = acp.update_agent_message_text(action["say"])
                await self.conn.session_update(session_id=session_id, update=update)
            elif "tool" in action:
                await self._tool(session_id, action)
            elif "cost" in action:
                cost = schema.Cost(amount=action["cost"], currency=action.get("currency", "USD"))
                update = schema.UsageUpdate(
                    session_update="usage_update", used=100, size=1000, cost=cost
                )
                await self.conn.session_update(session_id=session_id, update=update)
            elif "usage" in action:
                tokens = action["usage"]
                usage = schema.Usage(
                    input_tokens=tokens.get("input", 0),
                    output_tokens=tokens.get("output", 0),
                    cached_read_tokens=tokens.get("cache_read"),
                    total_tokens=tokens.get("input", 0) + tokens.get("output", 0),
                )
            elif "spawn" in action:  # a background process the agent leaves running
                child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.log("spawned", pid=child.pid)
            elif "sleep" in action:
                await asyncio.sleep(action["sleep"])
            elif "stop" in action:
                stop = action["stop"]
        return schema.PromptResponse(stop_reason=stop, usage=usage)

    async def _tool(self, session_id, action) -> None:
        self.calls += 1
        call_id = f"call-{self.calls}"
        raw = {"command": action["command"]}
        start = acp.start_tool_call(
            call_id, action["tool"], kind="execute", status="pending", raw_input=raw
        )
        await self.conn.session_update(session_id=session_id, update=start)
        status = "completed"
        if action.get("ask"):
            options = [
                schema.PermissionOption(option_id="once", name="Allow once", kind="allow_once"),
                schema.PermissionOption(option_id="always", name="Always", kind="allow_always"),
            ]
            if action.get("options") != "no_reject":
                options.append(
                    schema.PermissionOption(option_id="reject", name="Reject", kind="reject_once")
                )
            call = schema.ToolCallUpdate(
                tool_call_id=call_id, title=action["tool"], kind="execute", raw_input=raw
            )
            answer = await self.conn.request_permission(
                session_id=session_id, tool_call=call, options=options
            )
            outcome = answer.outcome.model_dump(mode="json", by_alias=True, exclude_none=True)
            self.log("permission", command=action["command"], outcome=outcome)
            if outcome.get("optionId") not in ("once", "always"):
                status = "failed"
        update = acp.update_tool_call(call_id, status=status)
        await self.conn.session_update(session_id=session_id, update=update)

    async def cancel(self, session_id, **kw):
        self.log("cancel")


def main() -> None:
    script = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    asyncio.run(acp.run_agent(FakeAgent(script)))


if __name__ == "__main__":
    main()
