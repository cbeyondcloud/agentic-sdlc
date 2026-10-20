"""Where the library's log records go while a command runs (DF-175).

The `agentic` loggers write each step's records to its log file (M1-47). On the console
the CLI shows only warnings and worse, through the same redactor as the step logs, and
stops the records at the `agentic` logger: no root handler (a developer's own logging
setup, a test harness) ever sees an unredacted message or traceback, and the step logs'
lower level never reaches the console. Everything is put back when the command ends.
"""

import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TextIO

ROOT = "agentic"


class _RedactingFormatter(logging.Formatter):
    def __init__(self, redact: Callable[[str], str]) -> None:
        super().__init__("%(levelname)s %(name)s: %(message)s")
        self.redact = redact

    def format(self, record: logging.LogRecord) -> str:
        try:
            return self.redact(super().format(record))
        except Exception:  # never print what could not be cleaned
            return f"{record.levelname} {record.name}: <not shown: redaction failed>"


@contextmanager
def console_logging(
    redact: Callable[[str], str], *, stream: TextIO | None = None, level: int = logging.WARNING
) -> Iterator[None]:
    logger = logging.getLogger(ROOT)
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(_RedactingFormatter(redact))
    propagate = logger.propagate
    logger.addHandler(handler)
    logger.propagate = False
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        handler.close()
