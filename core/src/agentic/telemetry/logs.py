"""Structured per-step logs (M1-47): JSON lines, levels, secrets redacted.

`StepLogs.step(run_dir, run_id, step, iteration)` is a context for one step: while it
is open, every record of the `agentic` loggers goes to
`<run_dir>/logs/<step>-<iteration>.jsonl`, one JSON object per line:

    {"at", "seq", "level", "logger", "run_id", "step", "iteration", "message", "fields",
     "error"}

`fields` comes from `extra={"fields": {...}}`; `error` is the formatted exception.
`seq` counts the process's records, so records written within one clock tick still
read back in the order they were written.

A step id that is not a plain file name (letters, digits, `.`, `_`, `-`, at most 100
characters, not starting with `.`) gets no log file: the step runs, nothing is written
outside `<run_dir>/logs`, and a warning says why.

Every string written - message, field values, exception text - goes through the
redactor first, and a `StepLogs` cannot be made without one (`sdlc_commons.redaction`
composes Zyroflo's masking; core does not import it). If the redactor itself fails,
the text is replaced, never written raw.

Records are matched to their step by a context variable, so two runs advanced
concurrently in one process (or a step's child agents, which inherit the context)
never write into each other's logs.

`read_logs` / `render_logs` read them back for `agentic logs <run> [--step]`, `LogTail`
reads what is new for `--follow`, and `redact_record` redacts a record once more on read.
"""

import itertools
import json
import logging
import re
import threading
import traceback
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

ROOT_LOGGER = "agentic"
LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}
UNREDACTABLE = "<not written: redaction failed>"
_LOG = logging.getLogger(__name__)
_PLAIN_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}")
_SEQ = itertools.count(1)  # next() is atomic under the GIL
_CURRENT: ContextVar[tuple[str, str, int] | None] = ContextVar("agentic_step_log", default=None)
_LEVEL_LOCK = threading.Lock()
_OPEN: list[int] = []  # the levels of the step logs now open
_SAVED: list[int] = []  # the `agentic` logger's own level before the first opened


class StepLogs:
    def __init__(self, redact: Callable[[str], str], level: int | str = logging.INFO) -> None:
        if not callable(redact):
            raise TypeError("step logs need a redactor: a function str -> str")
        self.redact = redact
        self.level = LEVELS[level.lower()] if isinstance(level, str) else int(level)

    @contextmanager
    def step(self, run_dir: Path, run_id: str, step: str, iteration: int) -> Iterator[Path | None]:
        """Log this step's records to its own file while the context is open (yields
        None, and logs nothing, for a step id that is not a plain file name)."""
        if not _PLAIN_NAME.fullmatch(step):
            _LOG.warning("no step log: step id %r is not a plain file name", step[:120])
            yield None
            return
        key = (run_id, step, iteration)
        path = Path(run_dir) / "logs" / f"{step}-{int(iteration)}.jsonl"
        handler = _StepHandler(path, key, self.redact, self.level)
        logger = logging.getLogger(ROOT_LOGGER)
        token = _CURRENT.set(key)
        _open_level(logger, self.level)
        logger.addHandler(handler)
        try:
            yield path
        finally:
            logger.removeHandler(handler)
            _close_level(logger, self.level)
            _CURRENT.reset(token)
            handler.close()


class _StepHandler(logging.Handler):
    def __init__(self, path: Path, key: tuple, redact: Callable[[str], str], level: int) -> None:
        super().__init__(level)
        self.path, self.key, self.redact = path, key, redact

    def emit(self, record: logging.LogRecord) -> None:
        if _CURRENT.get() != self.key:
            return  # another run's or step's record
        try:
            line = json.dumps(self._entry(record), ensure_ascii=False) + "\n"
            self.acquire()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
            finally:
                self.release()
        except Exception:
            self.handleError(record)

    def _entry(self, record: logging.LogRecord) -> dict:
        run_id, step, iteration = self.key
        error = None
        if record.exc_info:
            error = self._clean("".join(traceback.format_exception(*record.exc_info)))
        fields = getattr(record, "fields", None)
        return {
            "at": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="microseconds"),
            "seq": next(_SEQ),
            "level": record.levelname.lower(),
            "logger": record.name,
            "run_id": run_id,
            "step": step,
            "iteration": iteration,
            "message": self._clean(_message(record)),
            "fields": self._value(fields) if isinstance(fields, Mapping) else {},
            "error": error,
        }

    def _clean(self, text: str) -> str:
        return _clean(self.redact, text)

    def _value(self, value: object) -> object:
        return _redacted(self.redact, value)


def _clean(redact: Callable[[str], str], text: str) -> str:
    try:
        cleaned = redact(text)
    except Exception:
        return UNREDACTABLE
    return cleaned if isinstance(cleaned, str) else UNREDACTABLE


def _redacted(redact: Callable[[str], str], value: object) -> object:
    if isinstance(value, Mapping):
        return {_clean(redact, str(k)): _redacted(redact, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redacted(redact, item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _clean(redact, str(value))


def redact_record(record: Mapping, redact: Callable[[str], str]) -> dict:
    """A copy of a read-back record with its text redacted once more (the message, the
    error, field names and values; numbers stay). Records are redacted when written;
    this masks what an older writer, or a redactor that did not know a secret, let by."""
    return {
        key: _redacted(redact, value) if key in ("message", "error", "fields") else value
        for key, value in record.items()
    }


class LogTail:
    """A run's step logs read incrementally: each `poll()` returns the records written
    since the previous one, oldest first (for `agentic logs --follow`).

    Files are read in binary from a byte offset per file, and only whole lines (ending
    in a newline) are taken, so a record being written is never half-read; a file that
    got shorter than its offset is read again from the start. Opening a file for
    reading never blocks the writer (Python opens files shared on Windows too).
    """

    def __init__(
        self, run_dir: Path, step: str | None = None, min_level: str | None = None
    ) -> None:
        self.folder = Path(run_dir) / "logs"
        self.step = step
        self.floor = LEVELS[min_level.lower()] if min_level else 0
        self._offsets: dict[Path, int] = {}
        self._lines: dict[Path, int] = {}

    def poll(self, final: bool = False) -> list[dict]:
        """The new records. `final` also takes a last line with no newline (a writer
        that stopped mid-record), which then reads as a "not JSON" error."""
        if not self.folder.is_dir():
            return []
        records = []
        for path in sorted(self.folder.glob("*.jsonl")):
            if self._wanted(path.stem):
                records += self._read(path, final)
        return sorted(records, key=_order)

    def _wanted(self, stem: str) -> bool:
        step = self.step
        return not step or stem == step or bool(re.fullmatch(re.escape(step) + r"-\d+", stem))

    def _read(self, path: Path, final: bool) -> list[dict]:
        offset = self._offsets.get(path, 0)
        try:
            with path.open("rb") as handle:
                if handle.seek(0, 2) < offset:  # truncated or replaced: start again
                    offset, self._lines[path] = 0, 0
                handle.seek(offset)
                data = handle.read()
        except OSError:
            return []  # removed or unreadable for now: the next poll tries again
        end = len(data) if final else data.rfind(b"\n") + 1
        self._offsets[path] = offset + end
        records = []
        for raw in data[:end].splitlines():
            self._lines[path] = number = self._lines.get(path, 0) + 1
            record = _parse(raw, path.name, number)
            if LEVELS.get(str(record.get("level")), logging.ERROR) >= self.floor:
                records.append(record)
        return records


def _parse(raw: bytes, name: str, number: int) -> dict:
    try:
        record = json.loads(raw.decode("utf-8"))
    except ValueError:  # UnicodeDecodeError is a ValueError
        record = None
    if not isinstance(record, dict):
        return {"level": "error", "message": f"{name}:{number}: not JSON", "at": ""}
    return record


def read_logs(run_dir: Path, step: str | None = None, min_level: str | None = None) -> list[dict]:
    """A run's step logs, oldest first (by time, then by write order within a process).
    `step` is a step id (every iteration) or `<step>-<iteration>`; `min_level` drops
    records below it."""
    return LogTail(run_dir, step, min_level).poll(final=True)


def _order(record: dict) -> tuple[str, int]:
    seq = record.get("seq")
    return str(record.get("at", "")), seq if isinstance(seq, int) else 0


def render_logs(records: list[dict]) -> str:
    """One line per record (and the error under it), for a terminal."""
    out = []
    for r in records:
        where = f"{r.get('step', '?')}-{r.get('iteration', '?')}"
        fields = " ".join(f"{k}={v}" for k, v in (r.get("fields") or {}).items())
        level = str(r.get("level", "")).upper()
        out.append(f"{r.get('at', '')} {level:<8} {where} {r.get('message', '')} {fields}".rstrip())
        if r.get("error"):
            out += [f"    {line}" for line in str(r["error"]).rstrip().splitlines()]
    return "\n".join(out) + ("\n" if out else "")


def _message(record: logging.LogRecord) -> str:
    try:
        return record.getMessage()
    except Exception:  # bad %-arguments: keep the template, never crash the step
        return str(record.msg)


def _open_level(logger: logging.Logger, level: int) -> None:
    """Let records at `level` reach the handlers while any step log wants them."""
    with _LEVEL_LOCK:
        if not _OPEN:
            _SAVED[:] = [logger.level]
        _OPEN.append(level)
        if logger.getEffectiveLevel() > level:
            logger.setLevel(level)


def _close_level(logger: logging.Logger, level: int) -> None:
    with _LEVEL_LOCK:
        _OPEN.remove(level)
        if not _OPEN:
            logger.setLevel(_SAVED[0] if _SAVED else logging.NOTSET)
