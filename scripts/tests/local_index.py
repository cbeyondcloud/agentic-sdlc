"""A tiny package index for the release tests (M3-21): the legacy upload API that
`uv publish` speaks (POST multipart with `:action=file_upload`, Basic auth) and PEP 503
simple pages that `uv pip install --index-url` reads. Loopback only, files on disk, no
network beyond 127.0.0.1. Not for production: the org runs devpi, Artifactory, Nexus,
pypiserver or GitLab."""

from __future__ import annotations

import base64
import email.policy
import hashlib
import html
import re
import threading
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class LocalIndex:
    def __init__(self, root: Path, username: str = "publisher", password: str = "") -> None:
        self.root = root
        self.username = username
        self.password = password
        self.uploads: list[str] = []
        self.refused: list[str] = []
        self.server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}/"

    @property
    def simple_url(self) -> str:
        return self.url + "simple/"

    def start(self) -> LocalIndex:
        self.root.mkdir(parents=True, exist_ok=True)
        index = self

        class Handler(_Handler):
            owner = index

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    def files(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}


class _Handler(BaseHTTPRequestHandler):
    owner: LocalIndex

    def log_message(self, *args) -> None:  # quiet test output
        pass

    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip(), 16)
                if size == 0:
                    self.rfile.readline()
                    return b"".join(chunks)
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def do_POST(self) -> None:  # noqa: N802 (http.server's naming)
        owner = self.owner
        body = self._body()
        wanted = base64.b64encode(f"{owner.username}:{owner.password}".encode()).decode()
        if self.headers.get("Authorization", "") != f"Basic {wanted}":
            owner.refused.append("auth")
            self._send(403, b"invalid credentials")
            return
        ctype = self.headers.get("Content-Type", "")
        message = BytesParser(policy=email.policy.HTTP).parsebytes(
            f"Content-Type: {ctype}\r\n\r\n".encode() + body
        )
        fields: dict[str, str] = {}
        content: tuple[str, bytes] | None = None
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True) or b""
            if part.get_filename():
                content = (part.get_filename(), payload)
            else:
                fields[name] = payload.decode("utf-8")
        if fields.get(":action") != "file_upload" or content is None:
            self._send(400, b"not a file upload")
            return
        filename, data = content
        if fields.get("sha256_digest") != hashlib.sha256(data).hexdigest():
            self._send(400, b"sha256 mismatch")
            return
        target = owner.root / normalize(fields["name"]) / filename
        if target.exists():
            owner.refused.append(filename)
            self._send(400, b"File already exists")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        owner.uploads.append(filename)
        self._send(200, b"OK")

    def do_GET(self) -> None:  # noqa: N802
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        root = self.owner.root
        if parts == ["simple"]:
            links = "".join(
                f'<a href="/simple/{p.name}/">{p.name}</a>\n'
                for p in sorted(root.iterdir())
                if p.is_dir()
            )
            self._send(200, f"<html><body>\n{links}</body></html>".encode(), "text/html")
        elif len(parts) == 2 and parts[0] == "simple":
            folder = root / normalize(parts[1])
            if not folder.is_dir():
                self._send(404, b"no such project")
                return
            links = ""
            for path in sorted(folder.iterdir()):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                href = f"/files/{folder.name}/{path.name}#sha256={digest}"
                links += f'<a href="{html.escape(href)}">{html.escape(path.name)}</a>\n'
            self._send(200, f"<html><body>\n{links}</body></html>".encode(), "text/html")
        elif len(parts) == 3 and parts[0] == "files":
            path = root / parts[1] / parts[2]
            if path.is_file() and path.resolve().is_relative_to(root.resolve()):
                self._send(200, path.read_bytes(), "application/octet-stream")
            else:
                self._send(404, b"no such file")
        else:
            self._send(404, b"not found")
