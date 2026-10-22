"""Test helper: a stand-in for TokenLean's OpenAI-compatible route (grown from spike S7's
`spikes/s7_acp/stub.py`). Used by the ACP contract tests with the real, pinned agent CLI.

* `POST .../chat/completions`, streamed or not: logs every request (path, headers with
  the key redacted, model, tools offered, temperature, whether a planted marker reached
  the prompt, the last tool result) and answers from `queue` - a text or a tool call,
  then "done". A request that offers no tools (an agent's title or summary call) gets
  a short text without consuming the queue.
* `GET .../models`: one model. Anything else: 404, logged.

The refusing proxy for "the internet" is `anthropic_stub.RefusingProxy`.
"""

import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MARKER = "AGENTIC-TEST-MARKER-FROM-THE-REPOSITORY"
_CALL_IDS = itertools.count(1)


@dataclass
class Reply:
    text: str | None = None  # a text answer, or
    tool: str | None = None  # a call to this tool
    arguments: dict = field(default_factory=dict)
    call_id: str = field(default_factory=lambda: f"call_{next(_CALL_IDS)}")
    input_tokens: int = 100
    output_tokens: int = 20


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return ""


class OpenAIStub:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.queue: list[Reply] = []
        self.delay_s = 0.0
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _headers(self) -> dict:
                headers = {k.lower(): v for k, v in self.headers.items()}
                for secret in ("authorization", "api-key", "x-api-key"):
                    if secret in headers:
                        headers[secret] = "<redacted>"
                return headers

            def do_GET(self):
                stub.requests.append({"method": "GET", "path": self.path, "headers": {}})
                if self.path.rstrip("/").endswith("/models"):
                    data = {"object": "list", "data": [{"id": "model", "object": "model"}]}
                    return self._send(200, json.dumps(data).encode(), "application/json")
                self._send(404, b"", "text/plain")

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                text = raw.decode("utf-8", "replace")
                try:
                    body = json.loads(text or "{}")
                except ValueError:
                    body = {}
                if not self.path.split("?")[0].endswith("/chat/completions"):
                    stub.requests.append({"method": "POST", "path": self.path, "headers": {}})
                    return self._send(404, b"", "text/plain")
                messages = body.get("messages") or []
                tools = [(t.get("function") or {}).get("name") for t in body.get("tools") or []]
                results = [_text_of(m.get("content")) for m in messages if m.get("role") == "tool"]
                aux = not tools
                stub.requests.append(
                    {
                        "method": "POST",
                        "path": self.path,
                        "headers": self._headers(),
                        "auth": self.headers.get("authorization"),  # a test key only
                        "model": body.get("model"),
                        "tools": tools,
                        "temperature": body.get("temperature"),
                        "stream": bool(body.get("stream")),
                        "marker": MARKER in text,
                        "last_tool_result": results[-1] if results else None,
                        "last_user_text": next(
                            (
                                _text_of(m.get("content"))
                                for m in reversed(messages)
                                if m.get("role") == "user"
                            ),
                            "",
                        ),
                        "aux": aux,
                    }
                )
                if aux:
                    reply = Reply(text="Agentic step", input_tokens=10, output_tokens=3)
                else:
                    time.sleep(stub.delay_s)
                    reply = stub.queue.pop(0) if stub.queue else Reply(text="done")
                self._answer(body, reply)

            def _answer(self, body: dict, reply: Reply) -> None:
                usage = {
                    "prompt_tokens": reply.input_tokens,
                    "completion_tokens": reply.output_tokens,
                    "total_tokens": reply.input_tokens + reply.output_tokens,
                }
                if reply.tool:
                    call = {
                        "id": reply.call_id,
                        "type": "function",
                        "function": {"name": reply.tool, "arguments": json.dumps(reply.arguments)},
                    }
                    message = {"role": "assistant", "content": None, "tool_calls": [call]}
                    finish = "tool_calls"
                else:
                    message = {"role": "assistant", "content": reply.text or ""}
                    finish = "stop"
                base = {"id": "chatcmpl-t", "created": int(time.time()), "model": body.get("model")}
                if not body.get("stream"):
                    data = {
                        **base,
                        "object": "chat.completion",
                        "usage": usage,
                        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    }
                    return self._send(200, json.dumps(data).encode(), "application/json")
                delta = dict(message)
                if "tool_calls" in delta:
                    delta["tool_calls"] = [{**c, "index": 0} for c in delta["tool_calls"]]
                chunk = {**base, "object": "chat.completion.chunk"}
                chunks = [
                    {**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                    {**chunk, "choices": [], "usage": usage},
                ]
                sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
                self._send(200, sse.encode(), "text/event-stream")

            def _send(self, code: int, payload: bytes, kind: str) -> None:
                try:
                    self.send_response(code)
                    self.send_header("content-type", kind)
                    self.send_header("content-length", str(len(payload)))
                    self.send_header("x-tokenlean-cost-usd", "9.99")  # must be ignored
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass  # the client went away (a time limit)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def model_calls(self, *, aux: bool | None = None) -> list[dict]:
        calls = [r for r in self.requests if r.get("method") == "POST" and "model" in r]
        return calls if aux is None else [r for r in calls if r["aux"] is aux]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
