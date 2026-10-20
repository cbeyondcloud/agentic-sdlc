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

`read_logs` / `render_logs` read them back for `agentic logs <run> [--step]` (M1-54).
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
        try:
            cleaned = self.redact(text)
        except Exception:
            return UNREDACTABLE
        return cleaned if isinstance(cleaned, str) else UNREDACTABLE

    def _value(self, value: object) -> object:
        if isinstance(value, Mapping):
            return {self._clean(str(k)): self._value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._value(item) for item in value]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self._clean(str(value))


def read_logs(run_dir: Path, step: str | None = None, min_level: str | None = None) -> list[dict]:
    """A run's step logs, oldest first (by time, then by write order within a process).
    `step` is a step id (every iteration) or `<step>-<iteration>`; `min_level` drops
    records below it."""
    folder = Path(run_dir) / "logs"
    if not folder.is_dir():
        return []
    floor = LEVELS[min_level.lower()] if min_level else 0
    records = []
    for path in sorted(folder.glob("*.jsonl")):
        if step and not (path.stem == step or re.fullmatch(re.escape(step) + r"-\d+", path.stem)):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                record = json.loads(line)
            except ValueError:
                record = {"level": "error", "message": f"{path.name}:{number}: not JSON", "at": ""}
            if LEVELS.get(str(record.get("level")), logging.ERROR) >= floor:
                records.append(record)
    return sorted(records, key=_order)


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
