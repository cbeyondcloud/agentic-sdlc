"""Test helper: stand-ins for TokenLean's Anthropic route and for "the internet"
(from spike S2). Used by the claude-code contract tests with the real, pinned
Claude Code binary.

* TokenLeanStub - `/v1/messages` (streamed or not) and `/v1/messages/count_tokens`.
  Logs every request; answers from `queue` (text, or a tool call), then "done".
* RefusingProxy - an HTTP(S) proxy that refuses everything and logs where the
  client tried to go: anything sent outside TokenLean shows up in `attempts`.
"""

import itertools
import json
import socketserver
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MARKER = "AGENTIC-TEST-MARKER-FROM-USER-SETTINGS"
_TOOL_IDS = itertools.count(1)  # every tool call needs its own id: a repeated one is ignored


@dataclass
class Reply:
    text: str | None = None  # a text answer, or
    tool: str | None = None  # a call to this tool
    tool_input: dict = field(default_factory=dict)
    tool_id: str = field(default_factory=lambda: f"toolu_{next(_TOOL_IDS)}")
    input_tokens: int = 100
    output_tokens: int = 20


def _sse(model: str, reply: Reply) -> bytes:
    out: list[str] = []

    def send(event: str, data: dict) -> None:
        out.append(f"event: {event}\ndata: {json.dumps(data)}\n\n")

    usage = {"input_tokens": reply.input_tokens, "output_tokens": 1}
    message = {
        "id": "msg_t",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": usage,
    }
    send("message_start", {"type": "message_start", "message": message})
    if reply.tool:
        block = {"type": "tool_use", "id": reply.tool_id, "name": reply.tool, "input": {}}
        delta = {"type": "input_json_delta", "partial_json": json.dumps(reply.tool_input)}
        stop = "tool_use"
    else:
        block = {"type": "text", "text": ""}
        delta = {"type": "text_delta", "text": reply.text or ""}
        stop = "end_turn"
    send("content_block_start", {"type": "content_block_start", "index": 0, "content_block": block})
    send("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta})
    send("content_block_stop", {"type": "content_block_stop", "index": 0})
    send(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": reply.output_tokens},
        },
    )
    send("message_stop", {"type": "message_stop"})
    return "".join(out).encode("utf-8")


def _last_user_text(data: dict) -> str:
    for message in reversed(data.get("messages", [])):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        return "\n".join(
            str(part.get("text") or part.get("content") or "")
            for part in content or []
            if isinstance(part, dict)
        )
    return ""


class TokenLeanStub:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.queue: list[Reply] = []
        self.after: Reply | None = None  # the answer once `queue` is used up (default "done")
        self.delay_s = 0.0
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                data = json.loads(body or b"{}")
                headers = {k.lower(): v for k, v in self.headers.items()}
                for secret in ("x-api-key", "authorization"):
                    if secret in headers:
                        headers[secret] = "<redacted>"
                stub.requests.append(
                    {
                        "path": self.path,
                        "headers": headers,
                        "model": data.get("model"),
                        "tools": [t.get("name") for t in data.get("tools", [])],
                        "temperature": data.get("temperature"),
                        "marker": MARKER in body.decode("utf-8", "replace"),
                        "last_user_text": _last_user_text(data),
                    }
                )
                if self.path.startswith("/v1/messages/count_tokens"):
                    return self._send(b'{"input_tokens": 42}', "application/json")
                if not self.path.startswith("/v1/messages"):
                    self.send_response(404)
                    self.end_headers()
                    return
                time.sleep(stub.delay_s)
                reply = stub.queue.pop(0) if stub.queue else (stub.after or Reply(text="done"))
                model = data.get("model", "?")
                if data.get("stream"):
                    return self._send(_sse(model, reply), "text/event-stream")
                content = (
                    [
                        {
                            "type": "tool_use",
                            "id": reply.tool_id,
                            "name": reply.tool,
                            "input": reply.tool_input,
                        }
                    ]
                    if reply.tool
                    else [{"type": "text", "text": reply.text or ""}]
                )
                message = {
                    "id": "msg_t",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": content,
                    "stop_reason": "tool_use" if reply.tool else "end_turn",
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": reply.input_tokens,
                        "output_tokens": reply.output_tokens,
                    },
                }
                self._send(json.dumps(message).encode("utf-8"), "application/json")

            def _send(self, payload: bytes, kind: str) -> None:
                try:
                    self.send_response(200)
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
        return [
            r
            for r in self.requests
            if r["path"].startswith("/v1/messages?") or r["path"] == "/v1/messages"
        ]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class RefusingProxy:
    def __init__(self) -> None:
        self.attempts: list[str] = []
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(5)
                try:
                    first = self.request.recv(4096).split(b"\r\n", 1)[0].decode("latin-1")
                except OSError:
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

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
