"""A local fake of the GitHub REST API (stdlib `http.server`) for the M1-58 contract tests.

It serves only what the adapter calls: the repository, pull requests (list, create,
update, their files with renames, reviews), a file's contents at a commit, a comparison's
common ancestor and a commit's check runs and statuses. It checks the bearer token like GitHub
does and reads branch heads from a local bare remote, so a pull request can only be
opened for a branch that was really pushed. Every request is recorded (method, path,
query, body), so tests can assert what was - and was never - called. Errors echo the
request's Authorization header back, so a test can prove the client scrubs it.
"""

import base64
import json
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

_REPO = r"/repos/(?P<owner>[^/]+)/(?P<name>[^/]+)"


class FakeGitHub:
    def __init__(self, token: str, bare: Path | None = None, default_branch: str = "main"):
        self.token = token
        self.bare = bare
        self.default_branch = default_branch
        self.login = "dev"  # who opens pull requests
        self.pulls: list[dict] = []
        self.check_runs: list[list[dict]] = [[]]  # one list per read; the last one repeats
        self.statuses: list[dict] = []
        self.requests: list[dict] = []
        self.fail: dict[str, tuple[int, str]] = {}  # "POST pulls" -> (status, message)
        #: like `fail`, but each matching request uses up the first answer in the list
        self.fail_next: dict[str, list[tuple[int, str]]] = {}
        #: list at most this many files of a pull request (GitHub stops at 3000)
        self.files_cap: int | None = None
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "FakeGitHub":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def paths(self) -> list[str]:
        return [f"{r['method']} {r['path']}" for r in self.requests]

    # ── the API ──────────────────────────────────────────────────────────────

    def handle(self, method: str, path: str, query: dict, body: dict | None, auth: str):
        with self._lock:
            self.requests.append(
                {"method": method, "path": path, "query": query, "body": body, "auth": auth}
            )
            if auth != f"Bearer {self.token}":
                return 401, {"message": f"Bad credentials (got {auth})"}
            for key, answers in self.fail_next.items():
                verb, _, tail = key.partition(" ")
                if verb == method and path.endswith(tail) and answers:
                    status, message = answers.pop(0)
                    return status, {"message": f"{message} (request had {auth})"}
            for key, (status, message) in self.fail.items():
                verb, _, tail = key.partition(" ")
                if verb == method and path.endswith(tail):
                    return status, {"message": f"{message} (request had {auth})"}
            return self._route(method, path, query, body or {})

    def _route(self, method, path, query, body):
        if method == "GET" and re.fullmatch(_REPO, path):
            return 200, {"default_branch": self.default_branch}
        if re.fullmatch(_REPO + "/pulls", path):
            if method == "GET":
                owner = re.fullmatch(_REPO + "/pulls", path).group("owner")
                state = query.get("state", ["open"])[0]
                pulls = [p for p in self.pulls if state == "all" or p["state"] == state]
                if "head" not in query:  # a listing (M2-46): every pull in that state
                    return 200, pulls
                head = query["head"][0].removeprefix(f"{owner}:")
                return 200, [p for p in pulls if p["head"]["ref"] == head]
            if method == "POST":
                return self._create(body)
        changed = re.fullmatch(_REPO + r"/pulls/(?P<n>\d+)/files", path)
        if changed and method == "GET":
            pull = next((p for p in self.pulls if p["number"] == int(changed["n"])), None)
            if pull is None:
                return 404, {"message": "Not Found"}
            rows = self._changed(pull)[: self.files_cap]
            size = int(query.get("per_page", ["30"])[0])
            page = int(query.get("page", ["1"])[0])
            return 200, rows[(page - 1) * size : page * size]
        contents = re.fullmatch(_REPO + r"/contents/(?P<path>.+)", path)
        if contents and method == "GET":
            return self._contents(contents["path"], query.get("ref", ["HEAD"])[0])
        compare = re.fullmatch(_REPO + r"/compare/(?P<base>.+)\.\.\.(?P<head>[^/]+)", path)
        if compare and method == "GET":
            sha = self._git("merge-base", compare["base"], compare["head"]).strip()
            return (200, {"merge_base_commit": {"sha": sha}}) if sha else (404, {})
        reviews = re.fullmatch(_REPO + r"/pulls/(?P<n>\d+)/reviews", path)
        if reviews and method == "POST":
            pull = next((p for p in self.pulls if p["number"] == int(reviews["n"])), None)
            if pull is None:
                return 404, {"message": "Not Found"}
            pull.setdefault("reviews", []).append(body)
            return 200, {"id": len(pull["reviews"]), "state": body.get("event")}
        said = re.fullmatch(_REPO + r"/pulls/(?P<n>\d+)/(?P<what>reviews|comments)", path)
        if said and method == "GET":  # M3-11: what people said, as a test recorded it
            pull = next((p for p in self.pulls if p["number"] == int(said["n"])), None)
            if pull is None:
                return 404, {"message": "Not Found"}
            return 200, pull.get(f"recorded_{said['what']}", [])
        found = re.fullmatch(_REPO + r"/pulls/(?P<n>\d+)", path)
        if found and method in ("GET", "PATCH"):  # an update reads the body first (DF-296)
            pull = next((p for p in self.pulls if p["number"] == int(found["n"])), None)
            if pull is None:
                return 404, {"message": "Not Found"}
            if method == "GET":
                if self.bare is not None:  # the branch as pushed now
                    pull["head"]["sha"] = self._head(pull["head"]["ref"]) or pull["head"]["sha"]
                return 200, {**pull, "changed_files": len(self._changed(pull))}
            pull["body"] = body.get("body", pull["body"])
            pull["head"]["sha"] = self._head(pull["head"]["ref"]) or pull["head"]["sha"]
            return 200, pull
        if method == "GET" and re.fullmatch(_REPO + r"/commits/[^/]+/check-runs", path):
            runs = self.check_runs.pop(0) if len(self.check_runs) > 1 else self.check_runs[0]
            return 200, {"total_count": len(runs), "check_runs": runs}
        if method == "GET" and re.fullmatch(_REPO + r"/commits/[^/]+/status", path):
            return 200, {"state": "pending", "statuses": self.statuses}
        return 404, {"message": "Not Found"}

    def _create(self, body: dict):
        head = body.get("head", "")
        sha = self._head(head)
        if sha is None:
            return 422, {"message": "Validation Failed", "errors": [{"message": "head invalid"}]}
        if any(p["head"]["ref"] == head for p in self.pulls):
            message = f"A pull request already exists for acme:{head}."
            return 422, {"message": "Validation Failed", "errors": [{"message": message}]}
        number = len(self.pulls) + 1
        pull = {
            "number": number,
            "html_url": f"https://github.example.test/acme/app/pull/{number}",
            "state": "open",
            "title": body.get("title", ""),
            "body": body.get("body", ""),
            "head": {"ref": head, "sha": sha},
            "base": {"ref": body.get("base", "")},
            "user": {"login": self.login},
            "merged": False,
        }
        self.pulls.append(pull)
        return 201, pull

    def open_pull(self, head: str, body: str, base: str = "main") -> dict:
        """Someone opened a pull request by hand (not through the adapter)."""
        status, pull = self._create({"head": head, "base": base, "title": "by hand", "body": body})
        assert status == 201, pull
        return pull

    def merge(self, number: int) -> None:
        """A person merged the pull request on the host."""
        pull = next(p for p in self.pulls if p["number"] == number)
        pull.update(state="closed", merged=True)

    def close(self, number: int) -> None:
        """A person closed the pull request without merging it."""
        pull = next(p for p in self.pulls if p["number"] == number)
        pull.update(state="closed", merged=False)

    def _changed(self, pull: dict) -> list[dict]:
        """The files a pull request changes, as GitHub lists them: its branch against its
        base in the remote, a rename with its `previous_filename`."""
        if self.bare is None:
            return []
        base, head = pull["base"]["ref"], pull["head"]["ref"]
        rows = []
        for line in self._git("diff", "--name-status", "-M", f"{base}...{head}").splitlines():
            status, *names = line.split("\t")
            if status.startswith("R"):
                rows.append(
                    {"filename": names[1], "status": "renamed", "previous_filename": names[0]}
                )
            elif names:
                word = {"A": "added", "D": "removed"}.get(status[:1], "modified")
                rows.append({"filename": names[0], "status": word})
        return rows

    def _contents(self, path: str, ref: str):
        """GET contents/<path>?ref=: a file as base64, a folder as a listing, else 404."""
        path = unquote(path)
        kind = self._git("cat-file", "-t", f"{ref}:{path}").strip()
        if kind == "tree":
            names = self._git("ls-tree", "--name-only", f"{ref}:{path}").splitlines()
            return 200, [{"type": "file", "name": name} for name in names]
        if kind != "blob":
            return 404, {"message": "Not Found"}
        done = subprocess.run(
            ["git", "-C", str(self.bare), "cat-file", "blob", f"{ref}:{path}"], capture_output=True
        )
        content = base64.b64encode(done.stdout).decode("ascii")
        return 200, {"type": "file", "encoding": "base64", "content": content}

    def _git(self, *args: str) -> str:
        if self.bare is None:
            return ""
        done = subprocess.run(
            ["git", "-C", str(self.bare), *args], capture_output=True, text=True, encoding="utf-8"
        )
        return done.stdout if done.returncode == 0 else ""

    def _head(self, branch: str) -> str | None:
        if self.bare is None:
            return "f" * 40
        done = subprocess.run(
            ["git", "-C", str(self.bare), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
        )
        return done.stdout.strip() if done.returncode == 0 else None

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                parts = urlsplit(self.path)
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                status, data = fake.handle(
                    self.command,
                    parts.path,
                    parse_qs(parts.query),
                    body,
                    self.headers.get("authorization", ""),
                )
                payload = json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _serve

            def log_message(self, format, *args) -> None:  # noqa: A002 - stdlib signature
                return

        return Handler
