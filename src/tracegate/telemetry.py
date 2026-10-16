from __future__ import annotations

import sys
import threading
from collections import defaultdict

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace import StatusCode

from tracegate.config import Settings
from tracegate.models import SpanRecord
from tracegate.semconv import SPAN_KIND


class TraceCollector(SpanProcessor):
    def __init__(self) -> None:
        self._spans: dict[int, list[ReadableSpan]] = defaultdict(list)
        self._lock = threading.Lock()

    def on_start(self, span, parent_context=None) -> None:
        return None

    def on_end(self, span: ReadableSpan) -> None:
        with self._lock:
            self._spans[span.get_span_context().trace_id].append(span)

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def take(self, trace_id: int) -> list[ReadableSpan]:
        with self._lock:
            spans = self._spans.pop(trace_id, [])
        return sorted(spans, key=lambda span: span.start_time or 0)


def to_record(span: ReadableSpan) -> SpanRecord:
    context = span.get_span_context()
    attributes = dict(span.attributes or {})
    status = span.status
    return SpanRecord(
        trace_id=format(context.trace_id, "032x"),
        span_id=format(context.span_id, "016x"),
        parent_id=format(span.parent.span_id, "016x") if span.parent else None,
        name=span.name,
        kind=str(attributes.get(SPAN_KIND, "UNKNOWN")),
        status=status.status_code.name,
        start_ns=span.start_time or 0,
        end_ns=span.end_time or 0,
        attributes=attributes,
        error=status.description if status.status_code is StatusCode.ERROR else None,
    )


class Telemetry:
    def __init__(self, settings: Settings):
        resource = Resource.create(
            {
                "service.name": settings.service_name,
                "openinference.project.name": settings.project_name,
            }
        )
        self.provider = TracerProvider(resource=resource)
        self.collector = TraceCollector()
        self.provider.add_span_processor(self.collector)
        if settings.otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )
            except ImportError as exc:
                raise RuntimeError(
                    "OTLP export requires the extra: pip install 'tracegate[otlp]'"
                ) from exc
            exporter = OTLPSpanExporter(
                endpoint=settings.otlp_endpoint,
                headers=settings.otlp_header_map or None,
            )
            self.provider.add_span_processor(BatchSpanProcessor(exporter))
        if settings.console_export:
            self.provider.add_span_processor(
                SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr))
            )
        self.tracer = self.provider.get_tracer("tracegate")

    def flush(self, timeout_ms: int = 10000) -> bool:
        return self.provider.force_flush(timeout_ms)

    def shutdown(self) -> None:
        self.provider.shutdown()
