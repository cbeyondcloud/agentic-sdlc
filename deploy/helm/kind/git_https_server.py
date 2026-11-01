"""A local git host for the day-0 bootstrap proof on kind (M3-05): smart HTTP over TLS.

`git http-backend` (git's own CGI) behind the standard library's HTTP server, so the
control plane fetches the policy repo exactly as from a real git host: https, a private
CA it trusts through the tenant's gitconfig, and a token sent by its credential helper.
It runs in the control-plane image (git and Python, nothing else) as a container on the
kind network; the kind proof (bootstrap_proof.py) starts it. Not for production.

    python git_https_server.py --root /srv/git --cert server.pem --key server.key \\
        --token-file /srv/token [--port 8443]

Every request needs HTTP Basic auth whose password is the token; repositories are bare
folders under --root."""

from __future__ import annotations

import argparse
import base64
import hmac
import os
import ssl
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def make_handler(root: Path, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"  # one request per connection: the CGI ends the body

        def _authorised(self) -> bool:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                _, _, password = base64.b64decode(header[6:]).decode().partition(":")
            except ValueError:
                return False
            return hmac.compare_digest(password.encode(), token.encode())

        def _backend(self) -> None:
            if not self._authorised():
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="git"')
                self.end_headers()
                return
            parts = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "GIT_PROJECT_ROOT": str(root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "REQUEST_METHOD": self.command,
                "PATH_INFO": parts.path,
                "QUERY_STRING": parts.query,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(len(body)),
                "REMOTE_USER": "git",
                "REMOTE_ADDR": self.client_address[0],
                "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
                "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
                # the repositories belong to the user who copied them in, not to this one
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": "*",
            }
            done = subprocess.run(
                ["git", "http-backend"], input=body, env=env, capture_output=True, timeout=120
            )
            head, _, payload = done.stdout.partition(b"\r\n\r\n")
            if not _:
                head, _, payload = done.stdout.partition(b"\n\n")
            status, headers = 200, []
            for line in head.decode("latin-1").splitlines():
                name, _, value = line.partition(":")
                if name.lower() == "status":
                    status = int(value.split()[0])
                elif name:
                    headers.append((name, value.strip()))
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            # the whole body is here: its length, so the client never reads to EOF
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def finish(self) -> None:
            super().finish()
            try:  # close TLS with close_notify: GnuTLS refuses a bare TCP close
                self.connection.unwrap()
            except (OSError, ValueError):
                pass

        do_GET = _backend  # noqa: N815
        do_POST = _backend  # noqa: N815

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            print(f"git-https: {self.command} {urlsplit(self.path).path} {args[1:2]}", flush=True)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--port", type=int, default=8443)
    args = parser.parse_args()
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(Path(args.root), token))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(f"git-https: serving {args.root} on {args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
