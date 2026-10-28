"""A run's spans go to its own `traces.jsonl` (DF-165): the process-wide exporter hands
them to the run the command is driving, and drops nothing into another run's folder."""

import json

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from agentic.telemetry.sdk import JsonlSpanExporter
from agentic_cli.driving import _RunExporter


def test_spans_go_to_the_current_run_only(tmp_path):
    exporter = _RunExporter()
    provider = TracerProvider()  # local: the global provider is left alone
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("before any run"):
        pass
    exporter.target = JsonlSpanExporter(tmp_path / "r-1" / "traces.jsonl")
    with tracer.start_as_current_span("step requirements"):
        pass
    exporter.target = JsonlSpanExporter(tmp_path / "r-2" / "traces.jsonl")
    with tracer.start_as_current_span("step design"):
        pass

    first = (tmp_path / "r-1" / "traces.jsonl").read_text("utf-8").splitlines()
    second = (tmp_path / "r-2" / "traces.jsonl").read_text("utf-8").splitlines()
    assert [json.loads(line)["name"] for line in first] == ["step requirements"]
    assert [json.loads(line)["name"] for line in second] == ["step design"]
