"""The cassette server (M1-63, spike S10 "CI fixture-corpus mode").

A stdlib HTTP server that stands where TokenLean stands. It speaks the three model
routes - `/v1/chat/completions`, `/v1/messages` (with `count_tokens`) and `/v1/responses`
- so it serves the native backend and agent CLIs alike: whatever is pointed at its
`base_url` is recorded or replayed, in process or in a subprocess. Grown from the
`TokenLeanStub` test helper of spike S2.

Each run gets its own base URL, `<server>/c/<case>/<label>/<repeat>`, so its calls are
keyed by (case, variant label, repeat, call index) and stored in
`<root>/<case>/<label>/<repeat>.json`.

  * **record** - each call is forwarded to the upstream (local TokenLean, M1-67, or a
    scripted stand-in) and saved with the sha256 of its canonical request. Request
    headers are never saved, and of the response headers only the content type and
    TokenLean's `x-tokenlean-*` (cost, routed model) are kept - no secret reaches a file.
  * **replay** - each call is answered from its cassette, in order. A missing cassette
    or a call beyond the recording FAILS (the plumbing broke); `finish` also fails a run
    that made fewer calls than recorded. A request whose hash differs from the recording
    is still answered and counted in `stale` - an agent prompt changed, so a re-record
    is due, but prompt work never breaks CI.
"""

import hashlib
import json
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal
from urllib.parse import quote, unquote

ROUTES = ("/v1/chat/completions", "/v1/messages", "/v1/responses")
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
VOLATILE_FIELDS = ("metadata", "user")  # ids that differ per run, not part of the prompt
KEPT_HEADERS = ("content-type",)
KEPT_PREFIX = "x-tokenlean-"


@dataclass(frozen=True)
class UpstreamRequest:
    key: str  # case/label/repeat
    path: str  # the route, e.g. /v1/messages?beta=true
    body: bytes
    headers: dict[str, str]  # as sent by the client (auth included): forwarded, never saved


@dataclass(frozen=True)
class UpstreamReply:
    status: int
    body: bytes
    content_type: str
    headers: dict[str, str] = field(default_factory=dict)


Upstream = Callable[[UpstreamRequest], UpstreamReply]


def request_sha(body: bytes) -> str:
    """The canonical request's sha256: JSON with sorted keys, minus per-run ids."""
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return "sha256:" + hashlib.sha256(body).hexdigest()
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if k not in VOLATILE_FIELDS}
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def http_upstream(base_url: str, timeout_s: float = 600.0) -> Upstream:
    """Forward to a real gateway (record mode against local TokenLean)."""
    root = base_url.rstrip("/")

    def forward(request: UpstreamRequest) -> UpstreamReply:
        headers = {k: v for k, v in request.headers.items() if k not in ("host", "content-length")}
        outgoing = urllib.request.Request(
            root + request.path, data=request.body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(outgoing, timeout=timeout_s) as reply:  # noqa: S310
                return _reply(reply.status, reply.read(), dict(reply.headers.items()))
        except urllib.error.HTTPError as error:
            return _reply(error.code, error.read(), dict(error.headers.items()))

    return forward


def _reply(status: int, body: bytes, headers: dict[str, str]) -> UpstreamReply:
    lowered = {k.lower(): v for k, v in headers.items()}
    return UpstreamReply(status, body, lowered.get("content-type", "application/json"), lowered)


def check_key(key: str) -> list[str]:
    parts = key.split("/")
    if not parts or not all(_SEGMENT.fullmatch(p) and p not in (".", "..") for p in parts):
        raise ValueError(f"a cassette key is slash-separated ids, not {key!r}")
    return parts


class CassetteServer:
    def __init__(
        self,
        root: Path,
        mode: Literal["replay", "record"],
        upstream: Upstream | None = None,
    ) -> None:
        if mode == "record" and upstream is None:
            raise ValueError("record mode needs an upstream to forward to")
        self.root, self.mode, self.upstream = Path(root), mode, upstream
        self.problems: list[str] = []
        self.stale = 0
        self._lock = threading.Lock()
        self._loaded: dict[str, list[dict] | None] = {}
        self._served: dict[str, int] = {}
        self._recorded: dict[str, list[dict]] = {}
        self._server: ThreadingHTTPServer | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> "CassetteServer":
        handler = self._handler()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> "CassetteServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def url(self) -> str:
        assert self._server is not None, "start the server first"
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def base_url(self, key: str) -> str:
        """The base URL a run's model client uses: its calls are keyed by `key`."""
        check_key(key)
        return f"{self.url}/c/{quote(key, safe='/')}"

    def expect(self, keys: Iterable[str]) -> None:
        """Keys a run was meant to use: `finish` fails one that made fewer calls than
        recorded, even if it made none (a run that crashed before its first call)."""
        with self._lock:
            for key in keys:
                self._served.setdefault(key, 0)

    def finish(self) -> list[str]:
        """Record: write the cassettes. Replay: check every key used all its calls.
        Returns every problem (empty: the replay was faithful)."""
        with self._lock:
            if self.mode == "record":
                for key, calls in sorted(self._recorded.items()):
                    self._write(key, calls)
            else:
                for key, used in sorted(self._served.items()):
                    calls = self._cassette(key)
                    if calls is not None and used < len(calls):
                        self.problems.append(
                            f"{key}: recorded {len(calls)} calls, the run made {used}"
                        )
            return list(self.problems)

    # ── serving ───────────────────────────────────────────────────────────────

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                headers = {k.lower(): v for k, v in self.headers.items()}
                status, reply = server._answer(self.path, body, headers)
                try:
                    self.send_response(status)
                    self.send_header("content-type", reply.content_type)
                    for name, value in reply.headers.items():
                        if name.startswith(KEPT_PREFIX):
                            self.send_header(name, value)
                    self.send_header("content-length", str(len(reply.body)))
                    self.end_headers()
                    self.wfile.write(reply.body)
                except OSError:
                    pass  # the client went away

        return Handler

    def _answer(self, path: str, body: bytes, headers: dict[str, str]) -> tuple[int, UpstreamReply]:
        key, route = _split(path)
        if key is None or route is None:
            return 400, _error(f"not a cassette path: {path}", "bad_path")
        try:
            check_key(key)
        except ValueError as error:
            return 400, _error(str(error), "bad_path")
        if not route.split("?", 1)[0].startswith(ROUTES):
            with self._lock:
                self.problems.append(f"{key}: unsupported route {route}")
            return 404, _error(f"unsupported route {route}", "bad_route")
        if self.mode == "record":
            return self._record(key, route, body, headers)
        return self._replay(key, route, body)

    def _record(
        self, key: str, route: str, body: bytes, headers: dict[str, str]
    ) -> tuple[int, UpstreamReply]:
        assert self.upstream is not None
        reply = self.upstream(UpstreamRequest(key, route, body, headers))
        kept = {
            k.lower(): v
            for k, v in reply.headers.items()
            if k.lower().startswith(KEPT_PREFIX) or k.lower() in KEPT_HEADERS
        }
        call = {
            "path": route,
            "request_sha": request_sha(body),
            "status": reply.status,
            "content_type": reply.content_type,
            "headers": dict(sorted(kept.items())),
            "body": reply.body.decode("utf-8"),
        }
        with self._lock:
            self._recorded.setdefault(key, []).append(call)
        return reply.status, UpstreamReply(reply.status, reply.body, reply.content_type, kept)

    def _replay(self, key: str, route: str, body: bytes) -> tuple[int, UpstreamReply]:
        with self._lock:
            calls = self._cassette(key)
            index = self._served.get(key, 0)
            self._served[key] = index + 1
            if calls is None:
                self.problems.append(f"no cassette for {key}: record it with --mode record")
                return 404, _error(f"no cassette for {key}", "cassette_missing")
            if index >= len(calls):
                self.problems.append(f"{key}: the run made more calls than recorded ({len(calls)})")
                return 409, _error(f"{key}: no recorded call {index + 1}", "cassette_exhausted")
            call = calls[index]
            if call["path"].split("?", 1)[0] != route.split("?", 1)[0]:
                self.problems.append(f"{key}: call {index + 1} went to {route}, not {call['path']}")
                return 409, _error("the route differs from the recording", "cassette_mismatch")
            if call["request_sha"] != request_sha(body):
                self.stale += 1
        reply = UpstreamReply(
            call["status"], call["body"].encode("utf-8"), call["content_type"], call["headers"]
        )
        return reply.status, reply

    def _cassette(self, key: str) -> list[dict] | None:
        if key not in self._loaded:
            path = self._path(key)
            self._loaded[key] = json.loads(path.read_bytes())["calls"] if path.is_file() else None
        return self._loaded[key]

    def _path(self, key: str) -> Path:
        *folders, name = check_key(key)
        return self.root.joinpath(*folders, f"{name}.json")

    def _write(self, key: str, calls: list[dict]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"key": key, "calls": calls}, indent=1, ensure_ascii=False) + "\n"
        path.write_bytes(text.encode("utf-8"))


def _split(path: str) -> tuple[str | None, str | None]:
    if not path.startswith("/c/") or "/v1/" not in path:
        return None, None
    key, _, rest = path[3:].partition("/v1/")
    return unquote(key), "/v1/" + rest


def _error(message: str, kind: str) -> UpstreamReply:
    body = json.dumps({"error": {"type": kind, "message": message}}).encode("utf-8")
    return UpstreamReply(0, body, "application/json")
