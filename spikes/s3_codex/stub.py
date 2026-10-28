"""Throwaway S3 stand-ins (never imported by product code).

* ResponsesStub - a stand-in for a TokenLean OpenAI Responses route (`POST /v1/responses`,
  streamed). It logs every request it gets (method, path, headers, body) to a JSONL file
  and answers from a tiny script:
    - first turn: a reasoning item (with `encrypted_content`) plus a function call to the
      shell-like tool Codex offered, when the scenario asks for a tool call;
    - a turn that carries a tool result: the final answer - JSON matching the schema when
      the request has `text.format` of type `json_schema`, plain text otherwise.
  The event set per answer is chosen by `SCENARIO`:
    - `full`      - every event a real Responses stream sends (created, in_progress,
                    output_item.added, content_part.*, output_text.delta/done,
                    output_item.done, completed with usage);
    - `minimal`   - only `response.output_item.done` items and `response.completed`;
    - `nocomplete`- items but no `response.completed` (the stream just ends);
    - `nousage`   - `response.completed` without a `usage` object;
  Any other path answers 404 and is logged, so endpoints Codex tries show up.
* RefusingProxy - an HTTP(S) proxy that refuses everything and logs where the client tried
  to go: anything Codex sends outside the stub shows up here.
"""

import json
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STRUCTURED = {"summary": "added CSV export", "files": ["src/export.py"]}
COST_HEADER = "x-tokenlean-cost-usd"
SHELL_TOOL_NAMES = ("exec_command", "shell_command", "shell", "local_shell")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _usage() -> dict:
    return {
        "input_tokens": 1200,
        "input_tokens_details": {"cached_tokens": 800},
        "output_tokens": 40,
        "output_tokens_details": {"reasoning_tokens": 12},
        "total_tokens": 1240,
    }


def _shell_call(tools: list, cmd: str) -> tuple[dict | None, str]:
    """A function_call item for the first shell-like tool offered, and its tool name."""
    for wanted in SHELL_TOOL_NAMES:
        for tool in tools or []:
            name = tool.get("name") or (tool.get("function") or {}).get("name")
            if name != wanted:
                continue
            props = (tool.get("parameters") or {}).get("properties") or {}
            if "cmd" in props:
                args = {"cmd": cmd}
            elif (props.get("command") or {}).get("type") == "array":
                args = {"command": ["bash", "-lc", cmd]}
            else:
                args = {"command": cmd}
            item = {
                "type": "function_call",
                "id": "fc_s3_1",
                "call_id": "call_s3_1",
                "name": name,
                "arguments": json.dumps(args),
                "status": "completed",
            }
            return item, name
    return None, ""


def _reasoning_item() -> dict:
    return {
        "type": "reasoning",
        "id": "rs_s3_1",
        "summary": [{"type": "summary_text", "text": "S3 plans to run one command."}],
        "encrypted_content": "S3-ENCRYPTED-REASONING-BLOB",
    }


def _message_item(text: str) -> dict:
    return {
        "type": "message",
        "id": "msg_s3_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def build_stream(
    body: dict, scenario: str, want_tool: bool, tool_cmd: str = "echo s3-tool-ran"
) -> tuple[str, dict]:
    """The SSE body for one request, and a note of what was decided (for the log)."""
    model = body.get("model", "")
    inputs = body.get("input") or []
    has_tool_result = any(
        isinstance(i, dict) and i.get("type") in ("function_call_output", "custom_tool_call_output")
        for i in inputs
    )
    items: list[dict] = []
    note = {}
    if want_tool and not has_tool_result:
        call, name = _shell_call(body.get("tools") or [], tool_cmd)
        if call:
            items = [_reasoning_item(), call]
            note["answered_with"] = f"function_call:{name}"
    if not items:
        fmt = (body.get("text") or {}).get("format") or {}
        if fmt.get("type") == "json_schema":
            text = json.dumps(STRUCTURED)
            note["answered_with"] = "json_schema_message"
        else:
            text = "S3 done."
            note["answered_with"] = "text_message"
        items = [_message_item(text)]

    rid = f"resp_s3_{int(time.time() * 1000)}"
    response = {
        "id": rid,
        "object": "response",
        "created_at": int(time.time()),
        "status": "in_progress",
        "model": model,
        "output": [],
    }
    out: list[str] = []
    seq = 0

    def send(event: str, data: dict) -> None:
        nonlocal seq
        data = {"type": event, "sequence_number": seq, **data}
        seq += 1
        out.append(_sse(event, data))

    full = scenario == "full"
    if full:
        send("response.created", {"response": response})
        send("response.in_progress", {"response": response})
    for index, item in enumerate(items):
        if full:
            added = dict(item)
            if item["type"] == "message":
                added = {**item, "content": [], "status": "in_progress"}
            send("response.output_item.added", {"output_index": index, "item": added})
            if item["type"] == "message":
                text = item["content"][0]["text"]
                part = {"type": "output_text", "text": "", "annotations": []}
                base = {"item_id": item["id"], "output_index": index, "content_index": 0}
                send("response.content_part.added", {**base, "part": part})
                for piece in (text[: len(text) // 2], text[len(text) // 2 :]):
                    send("response.output_text.delta", {**base, "delta": piece})
                send("response.output_text.done", {**base, "text": text})
                send("response.content_part.done", {**base, "part": item["content"][0]})
            elif item["type"] == "function_call":
                base = {"item_id": item["id"], "output_index": index}
                send("response.function_call_arguments.delta", {**base, "delta": item["arguments"]})
                send(
                    "response.function_call_arguments.done",
                    {**base, "arguments": item["arguments"]},
                )
        send("response.output_item.done", {"output_index": index, "item": item})
    if scenario != "nocomplete":
        done = {**response, "status": "completed", "output": items}
        if scenario != "nousage":
            done["usage"] = _usage()
        send("response.completed", {"response": done})
    return "".join(out), note


class ResponsesStub:
    def __init__(
        self,
        log_path: str,
        scenario: str = "full",
        want_tool: bool = False,
        tool_cmd: str = "echo s3-tool-ran",
    ) -> None:
        self.log_path = log_path
        self.scenario = scenario
        self.want_tool = want_tool
        self.tool_cmd = tool_cmd
        self.lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # quiet
                pass

            def _record(self, body_text: str, note: dict) -> None:
                try:
                    body = json.loads(body_text) if body_text else None
                except ValueError:
                    body = {"_unparsed": body_text[:2000]}
                entry = {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                    **note,
                }
                with stub.lock, open(stub.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry) + "\n")

            def _read_body(self) -> str:
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                if self.headers.get("content-encoding", "").lower() == "zstd":
                    return f"<zstd-compressed body, {len(raw)} bytes>"
                return raw.decode("utf-8", "replace")

            def do_GET(self):
                self._record("", {})
                self.send_response(404)
                self.send_header("content-length", "0")
                self.end_headers()

            def do_POST(self):
                text = self._read_body()
                if self.path.rstrip("/").split("?")[0].endswith("/responses"):
                    try:
                        body = json.loads(text)
                    except ValueError:
                        body = {}
                    sse, note = build_stream(body, stub.scenario, stub.want_tool, stub.tool_cmd)
                    self._record(text, note)
                    payload = sse.encode("utf-8")
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("content-length", str(len(payload)))
                    self.send_header("x-request-id", "req_s3")
                    self.send_header(COST_HEADER, "0.000123")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self._record(text, {})
                self.send_response(404)
                self.send_header("content-length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


class RefusingProxy:
    def __init__(self) -> None:
        self.attempts: list[str] = []
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(5)
                try:
                    first = self.request.recv(4096).split(b"\r\n", 1)[0].decode("latin-1")
                except (TimeoutError, OSError):
                    first = "(no request line)"
                proxy.attempts.append(first)
                try:
                    self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    pass

        class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
