"""Recording spans: needs the OpenTelemetry SDK (extra `agentic-core[telemetry]`).

  * `install_tracing(exporter)` - the process's tracer provider, sending every finished
    span to `exporter` (call once, early; a global provider can be set only once);
  * `StrippingSpanExporter(inner)` - what `install_tracing` puts in front of every
    exporter: error text never leaves the process (below);
  * `JsonlSpanExporter(path)` - one JSON object per finished span, appended to a local
    file (e.g. `traces.jsonl` in the run folder), so a run's trace can be read offline.

Span attributes carry no prompt or answer text (content capture is off), but an error
can: an exception's message and stack trace, or a status description, may echo a
provider's response body, a prompt fragment, a URL or a key. The framework's own step
span records only the error's type; spans from libraries (Pydantic AI's `invoke_agent`
and `chat`) record exceptions in full, so the stripping happens at export, for every
span: an `exception` event keeps only `exception.type` and `exception.escaped`, and a
status description is kept only when it is a bare name (an error type or reason code).

A network exporter (OTLP to a collector) is the owner's choice and is not bundled.
"""

import json
import re
import threading
from collections.abc import Sequence
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Status

#: The `exception` event attributes that leave the process (no message, no stack trace).
EXCEPTION_KEPT = ("exception.type", "exception.escaped")
_BARE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,99}")


def install_tracing(exporter: SpanExporter) -> TracerProvider:
    """Make the global tracer provider export every finished span to `exporter`, error
    text stripped."""
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(StrippingSpanExporter(exporter)))
    trace.set_tracer_provider(provider)
    return provider


class StrippingSpanExporter(SpanExporter):
    """Hands `inner` copies of the spans with exception messages, stack traces and
    free-text status descriptions removed; everything else is passed through."""

    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self.inner.export([_stripped(span) for span in spans])

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


def _stripped(span: ReadableSpan) -> ReadableSpan:
    status = span.status
    if status.description is not None and not _BARE_NAME.fullmatch(status.description):
        status = Status(status.status_code)
    events = [_stripped_event(event) for event in span.events]
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=span.attributes,
        events=events,
        links=span.links,
        kind=span.kind,
        status=status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


def _stripped_event(event: Event) -> Event:
    if event.name != "exception":
        return event
    attributes = {k: v for k, v in (event.attributes or {}).items() if k in EXCEPTION_KEPT}
    return Event(event.name, attributes, timestamp=event.timestamp)


class JsonlSpanExporter(SpanExporter):
    """Appends one line per span: names, ids (hex), times (ns), status and attributes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        lines = "".join(json.dumps(_record(span), ensure_ascii=False) + "\n" for span in spans)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(lines)
        except OSError:
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS


def _record(span: ReadableSpan) -> dict:
    context, parent = span.context, span.parent
    return {
        "name": span.name,
        "trace_id": format(context.trace_id, "032x"),
        "span_id": format(context.span_id, "016x"),
        "parent_span_id": format(parent.span_id, "016x") if parent else None,
        "start_ns": span.start_time,
        "end_ns": span.end_time,
        "status": span.status.status_code.name,
        "attributes": {key: _plain(value) for key, value in (span.attributes or {}).items()},
    }


def _plain(value: object) -> object:
    return list(value) if isinstance(value, tuple) else value
