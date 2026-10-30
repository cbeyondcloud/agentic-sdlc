"""Sending one mail through the org's SMTP relay (M2-07, ruling P1).

stdlib `smtplib` on a worker thread: one connection per mail, from the background worker
only. `starttls` (the default) refuses a relay that does not offer STARTTLS - the mail is
never sent in clear and stays queued; `tls` connects with implicit TLS (port 465); `none`
is for a relay on the same host or cluster. A refusal of the mail itself (5xx for the
recipient or the data) is permanent; anything else - a 4xx, a connection problem, a
relay misconfiguration such as a failed login - is transient and retried."""

from __future__ import annotations

import asyncio
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Protocol
from uuid import UUID

from agentic_cp.settings import Settings

#: The Message-ID domain when the sender's is not a dotted host name (DF-577); the id's
#: local part is the outbox row's, so a resend is recognisable (P2).
MESSAGE_ID_DOMAIN = "agentic-control-plane.invalid"
#: One label of a host name (letters, digits, inner hyphens), after IDNA encoding.
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class DeliveryFailure(Exception):
    """A mail was not accepted; the message never holds a secret."""


class TransientFailure(DeliveryFailure):
    """Try again later (backoff)."""


class RelayUnusable(TransientFailure):
    """The relay itself cannot be used now (unreachable, no STARTTLS, a refused login):
    no other mail would get through either, so delivery stops for this pass."""


class PermanentFailure(DeliveryFailure):
    """The relay refused this mail for good."""


@dataclass(frozen=True)
class Outgoing:
    id: UUID
    sender: str
    sender_name: str
    recipient: str
    subject: str
    text: str
    html: str


class Notifier(Protocol):
    async def send(self, message: Outgoing) -> None:
        """Hand `message` to the relay; `TransientFailure` / `PermanentFailure` if not."""
        ...


def message_id_domain(sender: str) -> str:
    """The sender's domain, lower case (DF-577: some spam filters score down a dotless
    Message-ID domain), ASCII; MESSAGE_ID_DOMAIN when it is not a dotted host name."""
    domain = sender.rpartition("@")[2].strip().lower().removesuffix(".")
    try:
        domain = domain.encode("idna").decode("ascii")  # an international name as ASCII
    except UnicodeError:
        return MESSAGE_ID_DOMAIN
    labels = domain.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        return MESSAGE_ID_DOMAIN
    return domain


def build_message(message: Outgoing) -> EmailMessage:
    """multipart/alternative: the plain text first, then the HTML part."""
    mail = EmailMessage()
    mail["From"] = formataddr((message.sender_name, message.sender))
    mail["To"] = message.recipient
    mail["Subject"] = message.subject
    mail["Date"] = formatdate(usegmt=True)
    mail["Message-ID"] = f"<{message.id}@{message_id_domain(message.sender)}>"
    mail["Auto-Submitted"] = "auto-generated"  # RFC 3834: no auto-replies to it
    mail.set_content(message.text)
    mail.add_alternative(message.html, subtype="html")
    return mail


class SmtpNotifier:
    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        security: str = "starttls",
        username: str | None = None,
        password: str | None = None,
        timeout: float = 30.0,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if security not in ("starttls", "tls", "none"):
            raise ValueError("security is starttls, tls or none")
        self.host, self.port, self.security = host, port, security
        self.username, self._password, self.timeout = username, password, timeout
        self.ssl_context = ssl_context

    def __repr__(self) -> str:  # never the password
        return f"SmtpNotifier({self.host!r}, {self.port}, {self.security!r})"

    @classmethod
    def from_settings(cls, settings: Settings) -> SmtpNotifier:
        password = settings.smtp_password.get_secret_value() if settings.smtp_password else None
        return cls(
            host=settings.smtp_host or "",
            port=settings.smtp_port,
            security=settings.smtp_security,
            username=settings.smtp_username,
            password=password,
            timeout=settings.smtp_timeout_seconds,
        )

    async def send(self, message: Outgoing) -> None:
        await asyncio.to_thread(self._send, build_message(message))

    def _context(self) -> ssl.SSLContext:
        return self.ssl_context or ssl.create_default_context()

    def _send(self, mail: EmailMessage) -> None:
        try:
            if self.security == "tls":
                smtp = smtplib.SMTP_SSL(
                    self.host, self.port, timeout=self.timeout, context=self._context()
                )
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            with smtp:
                smtp.ehlo()
                if self.security == "starttls":
                    if not smtp.has_extn("starttls"):
                        raise RelayUnusable(
                            f"the relay {self.host}:{self.port} does not offer STARTTLS; "
                            "the mail is not sent in clear"
                        )
                    smtp.starttls(context=self._context())
                    smtp.ehlo()
                if self.username:
                    smtp.login(self.username, self._password or "")
                smtp.send_message(mail)
        except DeliveryFailure:
            raise
        except smtplib.SMTPRecipientsRefused as error:
            codes = [code for code, _ in error.recipients.values()]
            replies = "; ".join(
                f"{code} {_text(reply)}" for code, reply in error.recipients.values()
            )
            kind = PermanentFailure if codes and min(codes) >= 500 else TransientFailure
            raise kind(f"recipient refused: {replies}") from None
        except smtplib.SMTPDataError as error:
            kind = PermanentFailure if error.smtp_code >= 500 else TransientFailure
            raise kind(f"mail refused: {error.smtp_code} {_text(error.smtp_error)}") from None
        except smtplib.SMTPResponseException as error:
            raise RelayUnusable(  # a failed login or a refused sender: the relay's set-up
                f"the relay answered {error.smtp_code} {_text(error.smtp_error)}"
            ) from None
        except (smtplib.SMTPException, OSError) as error:
            raise RelayUnusable(
                f"the relay {self.host}:{self.port} could not be used ({type(error).__name__})"
            ) from None


def _text(reply: bytes | str) -> str:
    text = reply.decode("utf-8", "replace") if isinstance(reply, bytes) else str(reply)
    return " ".join(text.split())[:200]


def notifier_for(settings: Settings) -> Notifier | None:
    """The deployment's relay, or None when no relay is configured (mails stay queued)."""
    return SmtpNotifier.from_settings(settings) if settings.smtp_host else None
