"""A capture SMTP server for the mail tests (aiosmtpd, Apache-2.0): it records what it
accepts, can refuse recipients or data with a chosen reply, and offers STARTTLS with a
throw-away certificate (trustme) when asked. It binds 127.0.0.1 on a free port."""

from __future__ import annotations

import socket
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import EmailMessage

import trustme
from aiosmtpd.controller import Controller


@dataclass
class Received:
    mail_from: str
    rcpt_tos: list[str]
    content: bytes
    tls: bool

    @property
    def message(self) -> EmailMessage:
        return message_from_bytes(self.content, policy=policy.default)


@dataclass
class Capture:
    """The handler: what arrived, and the replies to give instead of accepting."""

    received: list[Received] = field(default_factory=list)
    rcpt_reply: str | None = None  # e.g. "450 try later" or "550 no such user"
    data_replies: list[str] = field(default_factory=list)  # consumed one per DATA

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if self.rcpt_reply:
            return self.rcpt_reply
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        if self.data_replies:
            return self.data_replies.pop(0)
        self.received.append(
            Received(
                mail_from=envelope.mail_from,
                rcpt_tos=list(envelope.rcpt_tos),
                content=envelope.original_content or envelope.content,
                tls=session.ssl is not None,
            )
        )
        return "250 Message accepted for delivery"


@dataclass(frozen=True)
class Relay:
    capture: Capture
    port: int
    client_context: ssl.SSLContext  # trusts the server's throw-away CA


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def capture_relay(*, starttls: bool = True) -> Iterator[Relay]:
    """A relay on 127.0.0.1 that offers STARTTLS (and requires it) unless `starttls` is
    False, in which case it speaks only in clear."""
    ca = trustme.CA()
    server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("127.0.0.1").configure_cert(server_context)
    client_context = ssl.create_default_context()
    ca.configure_trust(client_context)
    capture = Capture()
    options = {"tls_context": server_context, "require_starttls": True} if starttls else {}
    port = _free_port()
    controller = Controller(capture, hostname="127.0.0.1", port=port, **options)
    controller.start()
    try:
        yield Relay(capture=capture, port=port, client_context=client_context)
    finally:
        controller.stop()
