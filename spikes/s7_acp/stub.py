"""Throwaway S7 stand-ins (never imported by product code).

* ModelStub - a stand-in for TokenLean's model routes. It logs every request (method, path,
  headers, model, tool names offered, whether a planted marker reached the prompt) to a
  JSONL file and answers from a tiny script:
    - first turn that offers a shell-like tool: a tool call running `echo s7-tool-ran`;
    - any later turn (a tool result is present) or no shell tool: the text "S7 done.".
  Routes: OpenAI chat-completions (`.../chat/completions`, streamed or not), OpenAI
  Responses (`.../responses`, text only), Gemini API (`...:generateContent`,
  `...:streamGenerateContent`, `...:countTokens`). Anything else answers 404 and is logged,
  so endpoints an agent tries show up.
* RefusingProxy - an HTTP(S) proxy that refuses everything and logs where the client tried
  to go (same as S3).
"""

import json
import os
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MARKER = "S7-PLANTED-MARKER"
SHELL_HINTS = ("bash", "shell", "run_shell_command", "execute", "terminal", "command")
COMMAND = os.environ.get("S7_CMD", "touch s7-tool-ran.txt")


def _args_for(params: dict) -> dict:
    props = (params or {}).get("properties") or {}
    required = (params or {}).get("required") or list(props)
    args: dict = {}
    for name in required:
        spec = props.get(name) or {}
        kind = spec.get("type")
        if name in ("command", "cmd", "script", "commandLine"):
            args[name] = ["bash", "-lc", COMMAND] if kind == "array" else COMMAND
        elif kind == "array":
            args[name] = []
        elif kind in ("integer", "number"):
            args[name] = 30000 if "timeout" in name else 1
        elif kind == "boolean":
            args[name] = False
        else:
            args[name] = "s7 probe"
    if not any(k in args for k in ("command", "cmd", "script", "commandLine")):
        args["command"] = COMMAND
    return args


def _pick_shell(tools: list[tuple[str, dict]]) -> tuple[str, dict] | None:
    for hint in SHELL_HINTS:
        for name, params in tools:
            if hint in name.lower():
                return name, params
    return None


class ModelStub:
    def __init__(self, log_path: str) -> None:
        self.log_path = log_path
        self.lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _record(self, text: str, note: dict) -> None:
                try:
                    body = json.loads(text) if text else None
                except ValueError:
                    body = {"_unparsed": text[:500]}
                entry = {
                    "t": time.time(),
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "model": (body or {}).get("model") if isinstance(body, dict) else None,
                    "marker_in_prompt": MARKER in text,
                    "body_bytes": len(text),
                    **note,
                }
                with stub.lock, open(stub.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry) + "\n")

            def _send(self, code: int, payload: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._record("", {})
                if self.path.rstrip("/").endswith("/models"):
                    data = {"object": "list", "data": [{"id": "s7-model", "object": "model"}]}
                    self._send(200, json.dumps(data).encode(), "application/json")
                    return
                self._send(404, b"", "text/plain")

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                text = (self.rfile.read(length) if length else b"").decode("utf-8", "replace")
                try:
                    body = json.loads(text) if text else {}
                except ValueError:
                    body = {}
                path = self.path.split("?")[0]
                if path.endswith("/chat/completions"):
                    self._chat(text, body)
                elif path.endswith("/responses"):
                    self._responses(text, body)
                elif ":countTokens" in path:
                    self._record(text, {"answered_with": "count"})
                    self._send(200, b'{"totalTokens": 10}', "application/json")
                elif ":generateContent" in path or ":streamGenerateContent" in path:
                    self._gemini(text, body, stream=":streamGenerateContent" in path)
                else:
                    self._record(text, {"answered_with": "404"})
                    self._send(404, b"", "text/plain")

            # ---- OpenAI chat-completions -------------------------------------------------
            def _chat(self, text: str, body: dict) -> None:
                tools = [
                    (t.get("function", {}).get("name", ""), t.get("function", {}).get("parameters") or {})
                    for t in body.get("tools") or []
                ]
                done = any(m.get("role") == "tool" for m in body.get("messages") or [])
                pick = None if done else _pick_shell(tools)
                note = {"tools": [n for n, _ in tools], "stream": bool(body.get("stream"))}
                usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
                if pick:
                    call = {
                        "id": "call_s7_1",
                        "type": "function",
                        "function": {"name": pick[0], "arguments": json.dumps(_args_for(pick[1]))},
                    }
                    msg, finish = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
                    note["answered_with"] = f"tool_call:{pick[0]}"
                else:
                    msg, finish = {"role": "assistant", "content": "S7 done."}, "stop"
                    note["answered_with"] = "text"
                self._record(text, note)
                base = {"id": "chatcmpl-s7", "created": int(time.time()), "model": body.get("model", "")}
                if not body.get("stream"):
                    data = {**base, "object": "chat.completion", "usage": usage,
                            "choices": [{"index": 0, "message": msg, "finish_reason": finish}]}
                    self._send(200, json.dumps(data).encode(), "application/json")
                    return
                delta = dict(msg)
                if "tool_calls" in delta:
                    delta["tool_calls"] = [{**c, "index": 0} for c in delta["tool_calls"]]
                chunks = [
                    {**base, "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {**base, "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                    {**base, "object": "chat.completion.chunk", "choices": [], "usage": usage},
                ]
                sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
                self._send(200, sse.encode(), "text/event-stream")

            # ---- OpenAI Responses (text only) --------------------------------------------
            def _responses(self, text: str, body: dict) -> None:
                self._record(text, {"answered_with": "responses_text"})
                item = {"type": "message", "id": "msg_s7", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "S7 done.", "annotations": []}]}
                resp = {"id": "resp_s7", "object": "response", "status": "completed",
                        "model": body.get("model", ""), "output": [item],
                        "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}}
                if not body.get("stream"):
                    self._send(200, json.dumps(resp).encode(), "application/json")
                    return
                ev = [("response.output_item.done", {"output_index": 0, "item": item}),
                      ("response.completed", {"response": resp})]
                sse = "".join(f"event: {e}\ndata: {json.dumps({'type': e, **d})}\n\n" for e, d in ev)
                self._send(200, sse.encode(), "text/event-stream")

            # ---- Gemini API ---------------------------------------------------------------
            def _gemini(self, text: str, body: dict, stream: bool) -> None:
                tools = [
                    (d.get("name", ""), d.get("parameters") or d.get("parametersJsonSchema") or {})
                    for t in body.get("tools") or []
                    for d in t.get("functionDeclarations") or []
                ]
                done = any(
                    "functionResponse" in p
                    for c in body.get("contents") or []
                    for p in c.get("parts") or []
                )
                pick = None if done else _pick_shell(tools)
                note = {"tools": [n for n, _ in tools], "stream": stream, "model": self.path.split("/models/")[-1].split(":")[0]}
                if pick:
                    parts = [{"functionCall": {"name": pick[0], "args": _args_for(pick[1])}}]
                    note["answered_with"] = f"tool_call:{pick[0]}"
                else:
                    parts = [{"text": "S7 done."}]
                    note["answered_with"] = "text"
                self._record(text, note)
                data = {
                    "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP", "index": 0}],
                    "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 10, "totalTokenCount": 110},
                    "modelVersion": note["model"],
                }
                if stream:
                    self._send(200, f"data: {json.dumps(data)}\r\n\r\n".encode(), "text/event-stream")
                else:
                    self._send(200, json.dumps(data).encode(), "application/json")

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
