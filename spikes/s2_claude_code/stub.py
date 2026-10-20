"""Throwaway S2 stand-ins (never imported by product code).

* TokenLeanStub - TokenLean's Anthropic route (`/v1/messages`, streamed or not, and
  `/v1/messages/count_tokens`): logs every request, answers from a tiny script, and
  sets TokenLean's cost header on every answer.
* RefusingProxy - an HTTP(S) proxy that refuses everything and logs where the client
  tried to go: anything Claude Code sends outside TokenLean shows up here.
"""

import json
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STRUCTURED = {"summary": "added CSV export", "files": ["src/export.py"]}
MARKER = "S2-MARKER-FROM-CLAUDE-MD"


def _events(model: str, blocks: list[dict], stop: str) -> bytes:
    out = []

    def send(event: str, data: dict) -> None:
        out.append(f"event: {event}\ndata: {json.dumps(data)}\n\n")

    send(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_s2",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 120, "output_tokens": 1},
            },
        },
    )
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            send("content_block_start", {"type": "content_block_start", "index": index,
                                         "content_block": {"type": "text", "text": ""}})
            send("content_block_delta", {"type": "content_block_delta", "index": index,
                                         "delta": {"type": "text_delta", "text": block["text"]}})
        else:
            start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            send("content_block_start", {"type": "content_block_start", "index": index,
                                         "content_block": start})
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            send("content_block_delta", {"type": "content_block_delta", "index": index,
                                         "delta": delta})
        send("content_block_stop", {"type": "content_block_stop", "index": index})
    send("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": stop, "stop_sequence": None},
                           "usage": {"output_tokens": 30}})
    send("message_stop", {"type": "message_stop"})
    return "".join(out).encode("utf-8")


class TokenLeanStub:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.queue: list[list[dict]] = []  # scripted answers, used first, one per request
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                data = json.loads(body or b"{}")
                headers = {k.lower(): v for k, v in self.headers.items()}
                for secret in ("x-api-key", "authorization"):
                    if secret in headers:
                        headers[secret] = f"<{len(headers[secret])} chars>"
                tools = [t.get("name") for t in data.get("tools", [])]
                stub.requests.append({
                    "path": self.path,
                    "headers": headers,
                    "model": data.get("model"),
                    "stream": data.get("stream"),
                    "tools": tools,
                    "temperature": data.get("temperature"),
                    "marker_in_body": MARKER in body.decode("utf-8", "replace"),
                    "tool_results": [
                        str(part.get("content"))[:200]
                        for m in data.get("messages", [])[-1:]
                        if isinstance(m.get("content"), list)
                        for part in m["content"]
                        if part.get("type") == "tool_result"
                    ],
                    "body_bytes": len(body),
                })
                if self.path.startswith("/v1/messages/count_tokens"):
                    return self._json({"input_tokens": 42})
                if not self.path.startswith("/v1/messages"):
                    self.send_response(404)
                    self.end_headers()
                    return
                model = data.get("model", "?")
                last = (data.get("messages") or [{}])[-1]
                answered = isinstance(last.get("content"), list) and any(
                    part.get("type") == "tool_result" for part in last["content"]
                )
                structured = next((t for t in tools if "structured" in (t or "").lower()), None)
                if stub.queue and not self.path.startswith("/v1/messages/count_tokens"):
                    blocks = stub.queue.pop(0)
                    stop = "tool_use" if blocks[0]["type"] == "tool_use" else "end_turn"
                elif structured and not answered:
                    blocks = [{"type": "tool_use", "id": "toolu_s2", "name": structured,
                               "input": STRUCTURED}]
                    stop = "tool_use"
                else:
                    blocks, stop = [{"type": "text", "text": "done"}], "end_turn"
                if data.get("stream"):
                    payload = _events(model, blocks, stop)
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("x-tokenlean-cost-usd", "0.0123")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    content = [
                        {"type": "text", "text": b["text"]} if b["type"] == "text"
                        else {"type": "tool_use", "id": b["id"], "name": b["name"],
                              "input": b["input"]}
                        for b in blocks
                    ]
                    self._json({"id": "msg_s2", "type": "message", "role": "assistant",
                                "model": model, "content": content, "stop_reason": stop,
                                "stop_sequence": None,
                                "usage": {"input_tokens": 120, "output_tokens": 30}})

            def do_GET(self):
                stub.requests.append({"path": self.path, "method": "GET"})
                self.send_response(404)
                self.end_headers()

            def _json(self, data: dict) -> None:
                payload = json.dumps(data).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("x-tokenlean-cost-usd", "0.0123")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
