"""Test helper: a stand-in for TokenLean's OpenAI Responses route (`POST /v1/responses`,
streamed), grown from spike S3's `spikes/s3_codex/stub.py`. TokenLean's real route is
ST-4 (M2-32), not built yet; this answers the way S3 proved Codex needs (Q8): one
`response.output_item.done` per item and a terminal `response.completed` with an id and
usage. Used by the codex contract tests with the real, pinned Codex binary.

Each request is logged (path, headers with secrets redacted, parsed body) and answered
from `queue`: a text message, a call to the shell tool Codex offered, or an
`apply_patch` custom tool call. An empty queue answers "done". Any other path is a 404.
"""

import itertools
import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SHELL_TOOLS = ("exec_command", "shell_command", "shell", "local_shell")
_IDS = itertools.count(1)


@dataclass
class Reply:
    text: str | None = None  # a final message, or
    shell: str | None = None  # a call to the shell tool with this command, or
    patch: str | None = None  # an apply_patch call with this patch
    input_tokens: int = 100
    cached_tokens: int = 0
    output_tokens: int = 20


def _shell_item(tools: list, command: str) -> dict:
    """A function_call to the first shell-like tool offered, shaped to its parameters."""
    for wanted in SHELL_TOOLS:
        for tool in tools or []:
            if tool.get("name") != wanted:
                continue
            props = (tool.get("parameters") or {}).get("properties") or {}
            if "cmd" in props:
                args = {"cmd": command}
            elif (props.get("command") or {}).get("type") == "array":
                args = {"command": ["bash", "-lc", command]}
            else:
                args = {"command": command}
            n = next(_IDS)
            return {
                "type": "function_call",
                "id": f"fc_{n}",
                "call_id": f"call_{n}",
                "name": wanted,
                "arguments": json.dumps(args),
                "status": "completed",
            }
    raise AssertionError(f"no shell tool offered: {[t.get('name') for t in tools or []]}")


def _patch_item(patch: str) -> dict:
    n = next(_IDS)  # Codex takes apply_patch only as a custom_tool_call (S3 run T)
    return {
        "type": "custom_tool_call",
        "id": f"ctc_{n}",
        "call_id": f"call_{n}",
        "name": "apply_patch",
        "input": patch,
        "status": "completed",
    }


def _message_item(text: str) -> dict:
    return {
        "type": "message",
        "id": f"msg_{next(_IDS)}",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def stream(body: dict, reply: Reply) -> bytes:
    """The SSE body answering one request with `reply`."""
    if reply.shell is not None:
        item = _shell_item(body.get("tools") or [], reply.shell)
    elif reply.patch is not None:
        item = _patch_item(reply.patch)
    else:
        item = _message_item(reply.text if reply.text is not None else "done")
    response = {
        "id": f"resp_{next(_IDS)}",
        "object": "response",
        "created_at": int(time.time()),
        "model": body.get("model", ""),
        "output": [],
    }
    usage = {
        "input_tokens": reply.input_tokens,
        "input_tokens_details": {"cached_tokens": reply.cached_tokens},
        "output_tokens": reply.output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": reply.input_tokens + reply.output_tokens,
    }
    events = [
        ("response.created", {"response": {**response, "status": "in_progress"}}),
        ("response.output_item.done", {"output_index": 0, "item": item}),
        (
            "response.completed",
            {"response": {**response, "status": "completed", "output": [item], "usage": usage}},
        ),
    ]
    out = [
        f"event: {kind}\ndata: {json.dumps({'type': kind, 'sequence_number': n, **data})}\n\n"
        for n, (kind, data) in enumerate(events)
    ]
    return "".join(out).encode("utf-8")


class ResponsesStub:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.queue: list[Reply] = []
        self.delay_s = 0.0
        self.lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                self._record({}, "")
                self._send(404, b"", "text/plain")

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                text = raw.decode("utf-8", "replace")
                try:
                    body = json.loads(text) if text else {}
                except ValueError:
                    body = {}
                self._record(body, text)
                if not self.path.split("?")[0].rstrip("/").endswith("/v1/responses"):
                    return self._send(404, b"", "text/plain")
                time.sleep(stub.delay_s)
                with stub.lock:
                    reply = stub.queue.pop(0) if stub.queue else Reply(text="done")
                self._send(200, stream(body, reply), "text/event-stream")

            def _record(self, body: dict, text: str) -> None:
                headers = {k.lower(): v for k, v in self.headers.items()}
                if "authorization" in headers:  # kept only as its scheme and length
                    value = headers["authorization"]
                    headers["authorization"] = f"{value.split(' ')[0]} <{len(value)} chars>"
                with stub.lock:
                    stub.requests.append(
                        {"path": self.path, "headers": headers, "body": body, "text": text}
                    )

            def _send(self, status: int, payload: bytes, kind: str) -> None:
                try:
                    self.send_response(status)
                    self.send_header("content-type", kind)
                    self.send_header("x-tokenlean-cost-usd", "9.99")  # must be ignored
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass  # the client went away (a time limit)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def model_calls(self) -> list[dict]:
        with self.lock:
            return [r for r in self.requests if r["path"].split("?")[0] == "/v1/responses"]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def tool_outputs(call: dict) -> list[str]:
    """The tool results Codex sent back in one request (function or custom tool calls)."""
    kinds = ("function_call_output", "custom_tool_call_output")
    return [
        str(item.get("output"))
        for item in call["body"].get("input") or []
        if isinstance(item, dict) and item.get("type") in kinds
    ]
