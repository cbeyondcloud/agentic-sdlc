"""A local fake of the Jira REST API v2 (stdlib `http.server`) for the M2-50 end-to-end tests.

It serves only what the adapter calls: an issue, its comments and its transitions, under a
context path like a Data Center install (`/jira`). It checks the Bearer token like Jira
does, and records every request (method, path, body, auth) so tests can assert what was -
and was never - called. Errors can echo the request's Authorization header back, so a
test can prove the client scrubs it.
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

CONTEXT = "/jira"
_ISSUE = re.compile(CONTEXT + r"/rest/api/2/issue/(?P<key>[A-Z][A-Z0-9_]*-\d+)(?P<rest>/.*)?")
TRANSITIONS = [
    {"id": "11", "name": "To Do", "to": {"name": "To Do"}},
    {"id": "21", "name": "Start work", "to": {"name": "In Progress"}},
    {"id": "31", "name": "Done", "to": {"name": "Done"}},
]


def issue(key: str, summary: str, description: str | None) -> dict:
    """An issue as Jira Data Center returns it, cut down to what the mapping reads."""
    return {
        "id": "203117",
        "key": key,
        "self": f"https://jira.acme.example/jira/rest/api/2/issue/{key}",
        "fields": {
            "summary": summary,
            "description": description,
            "issuetype": {"name": "Story"},
            "status": {"name": "To Do"},
        },
    }


class FakeJira:
    def __init__(self, token: str) -> None:
        self.token = token
        self.issues: dict[str, dict] = {}
        self.comments: dict[str, list[str]] = {}
        self.status: dict[str, str] = {}
        self.requests: list[dict] = []
        self.fail: dict[str, int] = {}  # "POST comment" / "GET transitions" -> HTTP status
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}{CONTEXT}"

    def start(self) -> "FakeJira":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def add(self, key: str, summary: str, description: str | None = "") -> None:
        self.issues[key] = issue(key, summary, description)
        self.status[key] = "To Do"

    def calls(self) -> list[str]:
        return [f"{r['method']} {r['path']}" for r in self.requests]

    # ── the API ──────────────────────────────────────────────────────────────

    def handle(self, method: str, path: str, body: dict | None, auth: str):
        with self._lock:
            self.requests.append({"method": method, "path": path, "body": body, "auth": auth})
            if auth != f"Bearer {self.token}":
                message = f"You are not authenticated (got {auth})"
                return 401, {"errorMessages": [message], "errors": {}}
            found = _ISSUE.fullmatch(path)
            if found is None or found["key"] not in self.issues:
                missing = "Issue does not exist or you do not have permission to see it."
                return 404, {"errorMessages": [missing], "errors": {}}
            key, rest = found["key"], found["rest"] or ""
            verb = f"{method} {rest.strip('/') or 'issue'}"
            if verb in self.fail:
                said = f"failed on purpose (request had {auth})"
                return self.fail[verb], {"errorMessages": [said], "errors": {}}
            return self._route(verb, key, body or {})

    def _route(self, verb: str, key: str, body: dict):
        if verb == "GET issue":
            return 200, self.issues[key]
        if verb == "POST comment":
            self.comments.setdefault(key, []).append(body.get("body"))
            return 201, {"id": str(10300 + len(self.comments[key])), "body": body.get("body")}
        if verb == "GET transitions":
            return 200, {"transitions": TRANSITIONS}
        if verb == "POST transitions":
            wanted = (body.get("transition") or {}).get("id")
            chosen = next((t for t in TRANSITIONS if t["id"] == wanted), None)
            if chosen is None:
                return 400, {"errorMessages": ["Transition id is not valid."], "errors": {}}
            self.status[key] = chosen["to"]["name"]
            return 204, None
        return 404, {"errorMessages": ["Not found"], "errors": {}}

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                parts = urlsplit(self.path)
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                status, data = fake.handle(
                    self.command, parts.path, body, self.headers.get("authorization", "")
                )
                payload = b"" if data is None else json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = _serve

            def log_message(self, format, *args) -> None:  # noqa: A002 - stdlib signature
                return

        return Handler
