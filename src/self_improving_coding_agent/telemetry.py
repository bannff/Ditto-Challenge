"""Telemetry: console spans for observability + in-memory span capture for evals.

Two consumers off one OTEL pipe. The console exporter prints spans during a run; the
in-memory exporter feeds StrandsInMemorySessionMapper, which produces the Session that
trace-level evaluators and detectors need. The tracer provider is global and set once, so
setup must run before any agent is constructed, or those agents emit no spans.
"""

from __future__ import annotations

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from strands.telemetry import StrandsTelemetry
from strands_evals.mappers import StrandsInMemorySessionMapper
from strands_evals.types.trace import Session

_exporter: InMemorySpanExporter | None = None


def setup_telemetry(*, console: bool = True) -> InMemorySpanExporter:
    """Configure telemetry once and return the in-memory exporter. Safe to call again;
    the second call is a no-op that returns the same exporter."""
    global _exporter
    if _exporter is not None:
        return _exporter
    telemetry = StrandsTelemetry()
    if console:
        telemetry.setup_console_exporter()
    exporter = InMemorySpanExporter()
    # Simple (synchronous) processor so finished spans are available without a force flush.
    telemetry.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    _exporter = exporter
    return _exporter


def finished_spans() -> list[ReadableSpan]:
    return list(_exporter.get_finished_spans()) if _exporter else []


def build_session(session_id: str) -> Session:
    return StrandsInMemorySessionMapper().map_to_session(finished_spans(), session_id)


def clear_spans() -> None:
    """Drop captured spans so a run's Session isn't polluted by an earlier run."""
    if _exporter is not None:
        _exporter.clear()
