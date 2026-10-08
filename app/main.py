# HTTP application factory: builds the FastAPI app from validated Settings.
#
# Owns the lifetime of the shared outbound HTTP client and the corpus store, and
# exposes the health endpoints (REQ-017). Chat and streaming routes are added by
# later features; each chat request will call corpus.refresh() first (ADR-005).
#
# Settings are passed in rather than read here, so this module never touches the
# environment (REQ-013) and tests can build an app with exact settings.

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import Settings
from app.corpus import Corpus
from app.health import check_llm


def create_app(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """Build the app. `transport` lets tests replace the network with a fake endpoint."""

    corpus = Corpus(
        root=settings.corpus_dir,
        max_file_bytes=settings.corpus_max_file_bytes,
        max_files=settings.corpus_max_files,
        settle_seconds=settings.corpus_settle_seconds,
    )

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
            yield

    app = FastAPI(title="ELENTA local chat service", lifespan=lifespan)

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
