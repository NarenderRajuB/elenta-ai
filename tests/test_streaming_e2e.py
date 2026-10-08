# End-to-end streaming over real sockets: REQ-031 (genuine progressive streaming) and
# REQ-033 (a client disconnect stops the model call).
#
# TestClient buffers whole responses, so it cannot prove either property. Here a fake
# OpenAI-compatible model server emits one token every DELAY seconds, the real app runs
# in uvicorn in front of it, and the client reads /chat over TCP, timing each event.
# Everything binds to 127.0.0.1; no model and no internet are needed.

import json
import socket
import threading
import time

import anyio
import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from app.config import load_settings
from app.main import create_app

TOKENS = 12
DELAY = 0.15  # seconds between fake tokens: full answer takes ~1.8 s


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeModel:
    """Fake model server that records how many tokens it produced and whether it was cut off."""

    def __init__(self) -> None:
        self.produced = 0
        self.finished = False
        self.cancelled = threading.Event()
        app = FastAPI()

        @app.get("/v1/models")
        async def models():
            return {"data": [{"id": "gemma3:1b"}]}

        @app.post("/v1/chat/completions")
        async def completions():
            async def gen():
                try:
                    for i in range(TOKENS):
                        await anyio.sleep(DELAY)
                        self.produced += 1
                        yield f'data: {json.dumps({"choices": [{"delta": {"content": f"word{i} "}}]})}\n\n'
                    yield "data: [DONE]\n\n"
                    self.finished = True
                finally:
                    if not self.finished:
                        self.cancelled.set()
            return StreamingResponse(gen(), media_type="text/event-stream")

        self.app = app


class Server:
    def __init__(self, app) -> None:
        self.port = _free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.02)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def stack(tmp_path):
    (tmp_path / "leave.md").write_text("Employees receive 25 days of annual leave.", encoding="utf-8")
    model = FakeModel()
    with Server(model.app) as upstream:
        settings = load_settings({"LLM_URL": f"http://127.0.0.1:{upstream.port}/v1", "LLM_MODEL": "gemma3:1b",
                                  "CORPUS_DIR": str(tmp_path), "CORPUS_SETTLE_SECONDS": "0"})
        with Server(create_app(settings)) as app:
            yield model, f"http://127.0.0.1:{app.port}"


def _events(response: httpx.Response):
    """Yield (event name, data, arrival time) as frames arrive."""
    buffer = ""
    for text in response.iter_text():
        buffer += text
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            name, data = frame.split("\n")
            yield name[7:], json.loads(data[6:]), time.monotonic()


def test_positive_answer_arrives_progressively(stack):
    _, base = stack
    start = time.monotonic()
    token_times, final = [], None
    with httpx.stream("POST", f"{base}/chat", json={"question": "annual leave"}, timeout=30, trust_env=False) as r:
        for name, data, at in _events(r):
            if name == "token":
                token_times.append(at - start)
            elif name in ("done", "error"):
                final = name
    assert final == "done"
    # Many separate token events, the first long before the last: genuine streaming,
    # not one flush at the end.
    assert len(token_times) >= 5
    assert token_times[-1] - token_times[0] > DELAY * (TOKENS / 2)


def test_negative_client_disconnect_stops_model_generation(stack):
    model, base = stack
    with httpx.stream("POST", f"{base}/chat", json={"question": "annual leave"}, timeout=30, trust_env=False) as r:
        for name, _, _ in _events(r):
            if name == "token":
                break  # client goes away mid-answer; leaving the block closes the connection
    # The app must cancel its upstream call, so the fake model sees its stream cut off
    # well before it would have finished on its own.
    assert model.cancelled.wait(timeout=5), "model stream was not cancelled after client disconnect"
    assert model.produced < TOKENS and not model.finished
