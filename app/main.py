# HTTP application factory: builds the FastAPI app from validated Settings.
#
# Routes:
#   GET  /         minimal browser UI (ADR-010), served from app/static/
#   POST /chat     streamed answer as Server-Sent Events (ADR-004)
#   GET  /healthz  liveness;  GET /readyz  readiness (ADR-015)
#
# Owns the lifetime of the shared outbound HTTP client, the corpus store and the index
# cache. Every response carries security headers; the Content-Security-Policy allows
# scripts, styles and connections only from this origin and no inline script, so even
# if untrusted text reached the page as markup it could not run (REQ-065).
#
# Settings are passed in rather than read here, so this module never touches the
# environment (REQ-013) and tests can build an app with exact settings.

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from opentelemetry.sdk.trace.export import SpanExporter
from pydantic import BaseModel, Field

from app.chat import ChatDeps, answer
from app.config import Settings
from app.corpus import Corpus
from app.health import check_llm
from app.index import IndexCache
from app.observability import build_tracer_provider
from app.sse import SSE_HEADERS, frame

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Transport-level cap on request size, so an oversized body is refused before any work.
# The real limit is the context window: prompt assembly rejects questions that don't fit.
MAX_QUESTION_CHARS = 8000

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)


def create_app(
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    span_exporter: SpanExporter | None = None,
) -> FastAPI:
    """Build the app. Tests can replace the model endpoint (`transport`) and capture
    spans in memory (`span_exporter`)."""

    corpus = Corpus(
        root=settings.corpus_dir,
        max_file_bytes=settings.corpus_max_file_bytes,
        max_files=settings.corpus_max_files,
        settle_seconds=settings.corpus_settle_seconds,
    )
    index_cache = IndexCache(settings.chunk_max_chars)
    tracer_provider = build_tracer_provider(settings.otlp_traces_url, span_exporter)
    tracer = tracer_provider.get_tracer("app.chat")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.corpus = corpus
        # One refresh at startup so the logs show what the corpus contains before the
        # first question. Not required for correctness: every request refreshes anyway.
        # File I/O is blocking, so it runs in a worker thread, off the event loop.
        await anyio.to_thread.run_sync(corpus.refresh)
        # trust_env=False: ignore HTTP(S)_PROXY / NETRC etc. from the environment, so
        # the configured LLM_URL is the only outbound destination (REQ-012, REQ-022).
        async with httpx.AsyncClient(trust_env=False, transport=transport) as client:
            app.state.http_client = client
            app.state.chat_deps = ChatDeps(settings, corpus, index_cache, client, tracer)
            app.state.tracer_provider = tracer_provider
            yield
        # Flush spans still queued for export before the process exits.
        tracer_provider.shutdown()

    # No interactive API docs: they load scripts from a CDN, which breaks offline
    # operation (REQ-012) and the Content-Security-Policy.
    app = FastAPI(title="ELENTA local chat service", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.post("/chat")
    async def chat(body: ChatRequest) -> StreamingResponse:
        # Validation failures (empty, too long, wrong type) are answered as plain JSON
        # (422 by FastAPI, 400 below) before any stream starts; once the stream has
        # started, failures arrive as an `error` event (ADR-004).
        question = body.question.strip()
        if not question:
            return JSONResponse({"error": "question must not be blank"}, status_code=400)

        async def events() -> AsyncIterator[str]:
            async for event in answer(question, app.state.chat_deps):
                yield frame(event)

        # When the client disconnects, Starlette cancels this response; the generator
        # chain closes and the upstream model request is closed with it (REQ-033).
        return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        # Liveness: the process is up and serving HTTP. Deliberately checks nothing
        # else, so a model outage never makes an orchestrator restart a healthy app.
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        # Readiness: can this instance answer a chat request right now?
        llm = await check_llm(
            app.state.http_client,
            settings.llm_url,
            settings.llm_model,
            settings.llm_health_timeout_seconds,
        )
        llm_body = {"ok": llm.ok} if llm.ok else {"ok": False, "reason": llm.reason}
        if llm.ok:
            return JSONResponse({"status": "ready", "checks": {"llm": llm_body}})
        return JSONResponse({"status": "not_ready", "checks": {"llm": llm_body}}, status_code=503)

    return app
