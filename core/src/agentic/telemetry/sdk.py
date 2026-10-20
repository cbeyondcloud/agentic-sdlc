"""Recording spans: needs the OpenTelemetry SDK (extra `agentic-core[telemetry]`).

  * `install_tracing(exporter)` - the process's tracer provider, sending every finished
    span to `exporter` (call once, early; a global provider can be set only once);
  * `JsonlSpanExporter(path)` - one JSON object per finished span, appended to a local
    file (e.g. `traces.jsonl` in the run folder), so a run's trace can be read offline.

A network exporter (OTLP to a collector) is the owner's choice and is not bundled.
"""

import json
import threading
from collections.abc import Sequence
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult


def install_tracing(exporter: SpanExporter) -> TracerProvider:
    """Make the global tracer provider export every finished span to `exporter`."""
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider


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
