# Observability boundary: structured JSON logs and OpenTelemetry tracing (ADR-008).
#
# Logs: one JSON object per line on stderr. Every record made while a chat request is
# being handled carries that request's id, taken from a context variable, so
# `docker compose logs app | grep <id>` returns the whole request (REQ-082). Fields are
# passed as `extra={...}`; message text never contains document content, prompts or the
# question (REQ-068).
#
# Traces: each app instance builds its own TracerProvider (no process-wide global), so
# tests stay isolated and nothing is configured implicitly. All SDK settings that could
# otherwise come from OTEL_* environment variables (sampler, resource, span limits,
# exporter endpoint) are passed explicitly; configuration stays in app/config.py
# (REQ-013). Spans are exported over OTLP/HTTP to a local collector (Jaeger in Compose)
# only when OTLP_TRACES_URL is set; the exporter's HTTP session ignores proxy
# environment variables, so traces can only go to that address (REQ-012, REQ-085).

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

import requests
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

SERVICE_NAME = "elenta-chat"

# Set by app/chat.py for the duration of one request; copied into worker threads by
# anyio.to_thread, so corpus and index logs also carry it.
REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

# Attributes every LogRecord has; anything else on a record came from `extra=`.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or REQUEST_ID.get()
        if request_id:
            entry["request_id"] = request_id
        for key, value in vars(record).items():
            if key not in _STANDARD and key not in entry:
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs every request URL at INFO; LLM_URL could carry credentials (TS-007).
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_tracer_provider(otlp_traces_url: str | None, extra_exporter: SpanExporter | None = None) -> TracerProvider:
    """Tracer provider for one app instance. `extra_exporter` lets tests capture spans."""
    provider = TracerProvider(
        resource=Resource({"service.name": SERVICE_NAME}),
        sampler=ALWAYS_ON,  # one trace per chat request, every request (REQ-080)
        span_limits=SpanLimits(max_attributes=64, max_events=16, max_links=0, max_attribute_length=2048),
    )
    if otlp_traces_url:
        session = requests.Session()
        session.trust_env = False  # no HTTP(S)_PROXY / NETRC redirection of traces
        # Export every second (SDK default: 5 s) so a trace is viewable almost as soon as
        # its request ends, e.g. during a live demo.
        exporter = OTLPSpanExporter(endpoint=otlp_traces_url, session=session, timeout=5)
        provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=1000))
    if extra_exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(extra_exporter, schedule_delay_millis=50))
    return provider
