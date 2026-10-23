"""Keep credentials that travel in a URL path out of the server's logs (review F2).

The policy-repo webhook carries its tenant's hook token in the path (ND-14), so an access
log line would hold it in clear. `install_log_redaction` adds a filter to the uvicorn access
logger (idempotent) that masks every webhook path segment before a line is written."""

from __future__ import annotations

import logging
import re

from agentic.orgapi import API_PREFIX

_WEBHOOK = re.compile(re.escape(f"{API_PREFIX}/policy/webhook/") + r"[^/?#\s\"]+")
MASKED = f"{API_PREFIX}/policy/webhook/<redacted>"
ACCESS_LOGGERS = ("uvicorn.access",)


def redact(text: str) -> str:
    return _WEBHOOK.sub(MASKED, text)


class RedactWebhookTokens(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()
            }
        return True


def install_log_redaction() -> None:
    for name in ACCESS_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactWebhookTokens) for f in logger.filters):
            logger.addFilter(RedactWebhookTokens())
