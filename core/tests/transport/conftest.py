"""Local servers for the network tests: an HTTPS server signed by a test CA,
and a CONNECT proxy that records where it tunnelled. Both live on loopback, so
the tests need no network."""

import http.server
import select
import socket
import socketserver
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import trustme


@pytest.fixture
def ca() -> trustme.CA:
    return trustme.CA()


@pytest.fixture
def ca_bundle(ca: trustme.CA, tmp_path: Path) -> Path:
    bundle = tmp_path / "corporate-ca.pem"
    ca.cert_pem.write_to_path(str(bundle))
    return bundle


class _Ok(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def https_server(ca: trustme.CA) -> Iterator[str]:
    """`https://localhost:<port>`, serving `ok` with a certificate from `ca`."""
    server = http.server.ThreadingHTTPServer(("localhost", 0), _Ok)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert("localhost").configure_cert(context)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"https://localhost:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


class _Tunnel(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            head += chunk
        method, target, _ = head.split(b"\r\n", 1)[0].decode().split(" ", 2)
        if method != "CONNECT":
            self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        self.server.seen.append(target)
        host, port = target.rsplit(":", 1)
        with socket.create_connection((host, int(port))) as upstream:
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            pair = {self.request: upstream, upstream: self.request}
            while True:
                ready, _, _ = select.select(list(pair), [], [], 5)
                if not ready:
                    return
                for sock in ready:
                    data = sock.recv(65536)
                    if not data:
                        return
                    pair[sock].sendall(data)


@contextmanager
def _proxy() -> Iterator[tuple[str, list[str]]]:
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Tunnel)
    server.daemon_threads = True
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server.seen
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def connect_proxy() -> Iterator[tuple[str, list[str]]]:
    """(proxy URL, the `host:port` targets it tunnelled to)."""
    with _proxy() as proxy:
        yield proxy
