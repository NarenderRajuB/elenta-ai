# Tests for observability (app/observability.py and the spans emitted by app/chat.py):
# REQ-080 one trace per request, REQ-081 stage spans, REQ-082 id in logs and error events,
# REQ-083 per-stage latency and labelled token counts, REQ-084 chunk ids without document
# text, REQ-068 nothing sensitive in logs or span attributes.
#
# Spans are captured with the SDK's in-memory exporter; no collector is needed.

import json
import logging
from pathlib import Path

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.chat import ChatDeps, answer
from app.config import load_settings
from app.corpus import Corpus
from app.index import IndexCache
from app.main import create_app
from app.observability import REQUEST_ID, JsonFormatter, build_tracer_provider, configure_logging

SECRET_DOC = "Employees receive 25 days of annual leave. CONFIDENTIAL-DOC-MARKER-7731"
SECRET_QUESTION = "annual leave QUESTION-MARKER-4410"
STAGES = ["corpus.refresh", "evidence.selection", "prompt.assembly", "inference.stream"]


def sse(*texts, usage=(200, 5)):
    out = "".join(f"data: {json.dumps({'choices': [{'delta': {'content': t}}]})}\n\n" for t in texts)
    if usage:
        counts = {"prompt_tokens": usage[0], "completion_tokens": usage[1]}
        out += f"data: {json.dumps({'choices': [], 'usage': counts})}\n\n"
    return (out + "data: [DONE]\n\n").encode()


def run_chat(tmp_path: Path, question: str, respond, files=None):
    for name, text in (files if files is not None else {"leave.md": SECRET_DOC}).items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    settings = load_settings(
        {
            "LLM_URL": "http://llm.test/v1",
            "LLM_MODEL": "gemma3:1b",
            "CORPUS_DIR": str(tmp_path),
            "CORPUS_SETTLE_SECONDS": "0",
        }
    )
    exporter = InMemorySpanExporter()

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gemma3:1b"}]})
        return respond(request)

    app = create_app(settings, transport=httpx.MockTransport(handler), span_exporter=exporter)
    with TestClient(app) as client:
        body = client.post("/chat", json={"question": question}).text
    # Leaving the TestClient shuts the provider down, which flushes all spans.
    spans = {s.name: s for s in exporter.get_finished_spans()}
    events = [(f.split("\n")[0][7:], json.loads(f.split("\n")[1][6:])) for f in body.strip().split("\n\n")]
    return spans, dict(events), exporter.get_finished_spans()


# ---------------------------------------------------------------------------
# REQ-080 / REQ-081: one trace per request with a span per stage
# ---------------------------------------------------------------------------


class TestTraceStructure:
    def test_positive_one_trace_root_and_four_stage_spans(self, tmp_path):
        spans, _, all_spans = run_chat(tmp_path, "annual leave", lambda r: httpx.Response(200, content=sse("25 days")))
        assert {s.context.trace_id for s in all_spans} == {spans["chat.request"].context.trace_id}
        assert sorted(spans) == sorted(["chat.request", *STAGES])
        for name in STAGES:
            assert spans[name].parent.span_id == spans["chat.request"].context.span_id

    def test_positive_trace_id_is_the_request_id_shown_to_user(self, tmp_path):
        spans, events, _ = run_chat(tmp_path, "annual leave", lambda r: httpx.Response(200, content=sse("ok")))
        assert format(spans["chat.request"].context.trace_id, "032x") == events["meta"]["request_id"]

    def test_positive_stages_in_pipeline_order(self, tmp_path):
        spans, _, _ = run_chat(tmp_path, "annual leave", lambda r: httpx.Response(200, content=sse("ok")))
        starts = [spans[n].start_time for n in STAGES]
        assert starts == sorted(starts)

    def test_negative_two_requests_two_separate_traces(self, tmp_path):
        (tmp_path / "leave.md").write_text(SECRET_DOC, encoding="utf-8")
        settings = load_settings(
            {
                "LLM_URL": "http://llm.test/v1",
                "LLM_MODEL": "m",
                "CORPUS_DIR": str(tmp_path),
                "CORPUS_SETTLE_SECONDS": "0",
            }
        )
        exporter = InMemorySpanExporter()
        app = create_app(
            settings,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=sse("ok"))),
            span_exporter=exporter,
        )
        with TestClient(app) as client:
            client.post("/chat", json={"question": "annual leave"})
            client.post("/chat", json={"question": "annual leave"})
        roots = [s for s in exporter.get_finished_spans() if s.name == "chat.request"]
        assert len(roots) == 2 and roots[0].context.trace_id != roots[1].context.trace_id

    def test_edge_no_evidence_trace_has_no_prompt_or_inference_span(self, tmp_path):
        spans, _, _ = run_chat(tmp_path, "capital of France", lambda r: httpx.Response(200, content=sse("x")))
        assert sorted(spans) == ["chat.request", "corpus.refresh", "evidence.selection"]
        assert spans["chat.request"].attributes["chat.model_called"] is False
        assert spans["evidence.selection"].attributes["selection.insufficient_reason"] == "no_relevant_evidence"


# ---------------------------------------------------------------------------
# REQ-083 / REQ-084: latency, labelled token counts, chunk ids
# ---------------------------------------------------------------------------


class TestSpanContent:
    def test_positive_reported_token_counts_on_inference_span(self, tmp_path):
        spans, _, _ = run_chat(
            tmp_path, "annual leave", lambda r: httpx.Response(200, content=sse("ok", usage=(321, 9)))
        )
        a = spans["inference.stream"].attributes
        assert (a["llm.prompt_tokens"], a["llm.completion_tokens"], a["llm.token_count_source"]) == (321, 9, "reported")
        assert a["llm.model"] == "gemma3:1b" and a["llm.first_token_ms"] >= 0

    def test_positive_estimated_counts_labelled(self, tmp_path):
        spans, _, _ = run_chat(
            tmp_path, "annual leave", lambda r: httpx.Response(200, content=sse("abcdefgh", usage=None))
        )
        assert spans["inference.stream"].attributes["llm.token_count_source"] == "estimated (chars/4)"
        assert spans["prompt.assembly"].attributes["prompt.token_count_method"] == "chars/4"

    def test_positive_selected_chunk_ids_match_sources_event(self, tmp_path):
        spans, events, _ = run_chat(tmp_path, "annual leave", lambda r: httpx.Response(200, content=sse("ok")))
        assert list(spans["evidence.selection"].attributes["selection.chunk_ids"]) == [
            c["id"] for c in events["sources"]["chunks"]
        ]

    def test_positive_every_span_has_a_duration(self, tmp_path):
        _, _, all_spans = run_chat(tmp_path, "annual leave", lambda r: httpx.Response(200, content=sse("ok")))
        assert all(s.end_time >= s.start_time for s in all_spans)

    def test_negative_no_question_prompt_or_document_text_in_any_span(self, tmp_path):
        _, _, all_spans = run_chat(tmp_path, SECRET_QUESTION, lambda r: httpx.Response(200, content=sse("answer text")))
        dump = repr([(s.name, dict(s.attributes), [e.attributes for e in s.events]) for s in all_spans])
        assert "CONFIDENTIAL-DOC-MARKER" not in dump and "QUESTION-MARKER" not in dump
        assert "You are a document question-answering assistant" not in dump

    def test_edge_corrupt_file_recorded_on_refresh_span(self, tmp_path):
        (tmp_path / "bad.txt").write_bytes(b"\xff\xfe")
        spans, _, _ = run_chat(tmp_path, "annual leave", lambda r: httpx.Response(200, content=sse("ok")))
        assert list(spans["corpus.refresh"].attributes["corpus.skipped_errors"]) == ["bad.txt:not_utf8"]


# ---------------------------------------------------------------------------
# Failure paths are visible in the trace (REQ-076, REQ-033)
# ---------------------------------------------------------------------------


class TestFailureTraces:
    def test_negative_model_unavailable_marks_spans_as_errors(self, tmp_path):
        def refuse(request):
            raise httpx.ConnectError("refused")

        spans, events, _ = run_chat(tmp_path, "annual leave", refuse)
        assert spans["inference.stream"].status.status_code == StatusCode.ERROR
        assert spans["inference.stream"].attributes["error.code"] == "model_unavailable"
        assert spans["chat.request"].status.status_code == StatusCode.ERROR
        assert spans["chat.request"].attributes["chat.outcome"] == "model_unavailable"
        assert events["error"]["request_id"] == format(spans["chat.request"].context.trace_id, "032x")

    def test_negative_prompt_too_large_recorded(self, tmp_path):
        (tmp_path / "leave.md").write_text(SECRET_DOC, encoding="utf-8")
        settings = load_settings(
            {
                "LLM_URL": "http://llm.test/v1",
                "LLM_MODEL": "m",
                "CORPUS_DIR": str(tmp_path),
                "CORPUS_SETTLE_SECONDS": "0",
                "LLM_CONTEXT_TOKENS": "2000",
                "LLM_MAX_TOKENS": "300",
                "CONTEXT_TOKEN_BUDGET": "1000",
            }
        )
        exporter = InMemorySpanExporter()
        app = create_app(
            settings,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=sse("x"))),
            span_exporter=exporter,
        )
        with TestClient(app) as client:
            client.post("/chat", json={"question": "annual leave " + "why " * 1700})
        spans = {s.name: s for s in exporter.get_finished_spans()}
        assert spans["prompt.assembly"].attributes["error.code"] == "question_too_long"
        assert "inference.stream" not in spans

    def test_edge_client_disconnect_still_ends_every_span(self, tmp_path):
        (tmp_path / "leave.md").write_text(SECRET_DOC, encoding="utf-8")
        settings = load_settings(
            {
                "LLM_URL": "http://llm.test/v1",
                "LLM_MODEL": "m",
                "CORPUS_DIR": str(tmp_path),
                "CORPUS_SETTLE_SECONDS": "0",
            }
        )
        exporter = InMemorySpanExporter()
        provider = build_tracer_provider(None, exporter)
        body = sse(*[f"w{i} " for i in range(100)])

        async def main():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))
            ) as client:
                deps = ChatDeps(
                    settings, Corpus(str(tmp_path), 10**6, 500, 0.0), IndexCache(800), client, provider.get_tracer("t")
                )
                gen = answer("annual leave", deps)
                async for event in gen:
                    if event["event"] == "token":
                        break
                await gen.aclose()

        anyio.run(main)
        provider.shutdown()
        spans = {s.name: s for s in exporter.get_finished_spans()}
        assert spans["chat.request"].attributes["chat.outcome"] == "client_disconnected"
        assert spans["inference.stream"].attributes["error.code"] == "client_disconnected"


# ---------------------------------------------------------------------------
# REQ-082 / REQ-068: structured logs carry the request id, never sensitive text
# ---------------------------------------------------------------------------


def _record(msg="hello", **extra):
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, msg, None, None)
    for k, v in extra.items():
        setattr(record, k, v)
    return record


class TestJsonLogs:
    def test_positive_each_line_is_json_with_core_fields(self):
        entry = json.loads(JsonFormatter().format(_record("chat end", outcome="stop")))
        assert {"ts", "level", "logger", "message"} <= set(entry) and entry["outcome"] == "stop"

    def test_positive_request_id_from_context(self):
        token = REQUEST_ID.set("abc123")
        try:
            assert json.loads(JsonFormatter().format(_record()))["request_id"] == "abc123"
        finally:
            REQUEST_ID.reset(token)

    def test_positive_all_logs_of_a_request_share_its_id(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        handler_records = []

        class Capture(logging.Handler):
            def emit(self, record):
                handler_records.append(json.loads(JsonFormatter().format(record)))

        capture = Capture()
        logging.getLogger().addHandler(capture)
        try:
            _, events, _ = run_chat(tmp_path, SECRET_QUESTION, lambda r: httpx.Response(200, content=sse("ok")))
        finally:
            logging.getLogger().removeHandler(capture)
        rid = events["meta"]["request_id"]
        mine = [r for r in handler_records if r.get("request_id") == rid]
        messages = {r["message"].split(":")[0] for r in mine}
        assert {"chat start", "corpus refreshed", "evidence selected", "chat end"} <= messages
        text = json.dumps(handler_records)
        assert "QUESTION-MARKER" not in text and "CONFIDENTIAL-DOC-MARKER" not in text

    def test_negative_no_request_id_outside_a_request(self):
        assert "request_id" not in json.loads(JsonFormatter().format(_record()))

    def test_edge_non_serialisable_extra_and_exceptions(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = _record("failed", obj=object())
            record.exc_info = sys.exc_info()
        entry = json.loads(JsonFormatter().format(record))
        assert "ValueError: boom" in entry["exception"] and "object" in entry["obj"]

    def test_edge_configure_logging_silences_httpx_info(self):
        root = logging.getLogger()
        saved = root.handlers[:], root.level
        try:
            configure_logging()
            assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
            assert isinstance(root.handlers[0].formatter, JsonFormatter)
        finally:
            root.handlers[:], level = saved[0], saved[1]
            root.setLevel(level)


# ---------------------------------------------------------------------------
# Exporter configuration (REQ-085, REQ-012)
# ---------------------------------------------------------------------------


class TestExporterConfig:
    def test_positive_spans_exported_directly_even_with_proxy_env(self, monkeypatch):
        # Behavioural check of trust_env=False: with HTTP_PROXY pointing at a dead port,
        # spans must still reach the configured local collector directly.
        import socket
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        received = []

        class Collector(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            dead_port = sock.getsockname()[1]
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{dead_port}")
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{dead_port}")
        server = HTTPServer(("127.0.0.1", 0), Collector)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            provider = build_tracer_provider(f"http://127.0.0.1:{server.server_port}/v1/traces")
            provider.get_tracer("t").start_span("probe").end()
            assert provider.force_flush(5000)
            provider.shutdown()
        finally:
            server.shutdown()
        assert received and received[0][0] == "/v1/traces" and b"probe" in received[0][1]

    def test_negative_no_exporter_without_url(self):
        provider = build_tracer_provider(None)
        assert provider._active_span_processor._span_processors == ()
        provider.shutdown()

    @pytest.mark.parametrize(
        "value, ok", [("http://jaeger:4318/v1/traces", True), ("ftp://x", False), ("jaeger:4318", False)]
    )
    def test_edge_otlp_url_validated(self, value, ok):
        env = {"LLM_URL": "http://h/v1", "LLM_MODEL": "m", "OTLP_TRACES_URL": value}
        if ok:
            assert load_settings(env).otlp_traces_url == value
        else:
            with pytest.raises(Exception, match="OTLP_TRACES_URL"):
                load_settings(env)
