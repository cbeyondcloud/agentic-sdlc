"""Shared pytest fixtures for the core package tests."""

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    """Folder holding sample agent, pipeline, policy and handoff files."""
    return FIXTURES_DIR


_EXPORTERS: dict = {}


def _span_exporters() -> dict:
    """One in-memory SDK provider, installed globally for the session (a global provider
    can be set only once), feeding two exporters: `raw` gets the spans as recorded,
    `exported` what an exporter given to `install_tracing` would get."""
    if not _EXPORTERS:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        from agentic.telemetry.sdk import StrippingSpanExporter

        raw, exported = InMemorySpanExporter(), InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(raw))
        provider.add_span_processor(SimpleSpanProcessor(StrippingSpanExporter(exported)))
        trace.set_tracer_provider(provider)
        _EXPORTERS.update(raw=raw, exported=exported)
    return _EXPORTERS


@pytest.fixture
def spans():
    """The OpenTelemetry spans this test finished, as recorded."""
    exporters = _span_exporters()
    for exporter in exporters.values():
        exporter.clear()
    yield exporters["raw"]
    for exporter in exporters.values():
        exporter.clear()


@pytest.fixture
def exported_spans(spans):
    """The spans this test finished, as an exporter installed by `install_tracing` gets them."""
    return _EXPORTERS["exported"]
