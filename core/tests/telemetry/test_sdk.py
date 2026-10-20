"""What leaves the process: an exporter given to `install_tracing` never receives error
text - exception messages, stack traces or free-text status descriptions - from any
span, the framework's or a library's (M1-43; D14, OR-01)."""

from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from agentic.telemetry import sdk

SENTINEL = "SENTINEL-ERROR-TEXT-4410"


@pytest.fixture
def installed(monkeypatch):
    """install_tracing's provider, without making it the process's global one."""
    providers = []
    monkeypatch.setattr(sdk.trace, "set_tracer_provider", providers.append)

    def install(exporter):
        provider = sdk.install_tracing(exporter)
        assert providers == [provider]
        return provider

    return install


def test_an_exported_span_carries_no_error_text(installed):
    exporter = InMemorySpanExporter()
    provider = installed(exporter)
    tracer = provider.get_tracer("a-library")
    with pytest.raises(RuntimeError):
        with tracer.start_as_current_span("chat model", attributes={"keep": "me"}):
            raise RuntimeError(f"401 from provider, body: {SENTINEL}")
    with tracer.start_as_current_span("tool") as span:
        span.set_status(Status(StatusCode.ERROR, f"tool output: {SENTINEL}"))
    with tracer.start_as_current_span("step x") as span:
        span.set_status(Status(StatusCode.ERROR, "RuntimeError"))
    provider.force_flush()

    chat, tool, step = exporter.get_finished_spans()
    for span in (chat, tool, step):
        events = [(e.name, dict(e.attributes or {})) for e in span.events]
        assert SENTINEL not in repr((dict(span.attributes), events, span.status.description))
    assert chat.attributes["keep"] == "me"
    assert chat.status.status_code is StatusCode.ERROR
    (event,) = chat.events
    assert event.name == "exception"
    assert set(event.attributes) <= {"exception.type", "exception.escaped"}
    assert event.attributes["exception.type"].endswith("RuntimeError")
    assert tool.status.status_code is StatusCode.ERROR and tool.status.description is None
    assert step.status.description == "RuntimeError"  # a bare type or code name is kept
    assert chat.context.span_id and chat.start_time and chat.end_time


def test_the_jsonl_exporter_gets_stripped_spans_too(installed, tmp_path: Path):
    path = tmp_path / "traces.jsonl"
    provider = installed(sdk.JsonlSpanExporter(path))
    with pytest.raises(ValueError):
        with provider.get_tracer("t").start_as_current_span("work"):
            raise ValueError(SENTINEL)
    provider.force_flush()
    text = path.read_text(encoding="utf-8")
    assert '"name": "work"' in text and SENTINEL not in text
