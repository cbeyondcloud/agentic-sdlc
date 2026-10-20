"""Shared pytest fixtures for the core package tests."""

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    """Folder holding sample agent, pipeline, policy and handoff files."""
    return FIXTURES_DIR


_EXPORTER = None


@pytest.fixture
def spans():
    """The OpenTelemetry spans this test finished. One in-memory SDK provider is installed
    globally for the session (a global provider can be set only once)."""
    global _EXPORTER
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    if _EXPORTER is None:
        _EXPORTER = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
        trace.set_tracer_provider(provider)
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()
