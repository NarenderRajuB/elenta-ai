# Tests for the inference client (app/inference.py): OpenAI-compatible streaming,
# failure codes and timeouts (REQ-015, REQ-022, REQ-031, REQ-033, REQ-076, REQ-083).
#
# The model endpoint is an httpx.MockTransport, so every failure can be produced on
# demand without a running model.

import json

import anyio
import httpx
import pytest

from app.inference import Finish, InferenceError, TextDelta, Usage, stream_chat

URL = "http://llm.test/v1"


def sse(*chunks: dict, done: bool = True) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + ("data: [DONE]\n\n" if done else "")).encode()


def delta(text: str, finish: str | None = None) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}]}


async def collect(handler, **overrides) -> list:
    options = dict(url=URL, model="gemma3:1b", messages=[{"role": "user", "content": "q"}], max_tokens=64,
                   temperature=0.0, connect_timeout=1, read_timeout=1, request_timeout=5)
    options.update(overrides)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return [item async for item in stream_chat(client, **options)]


def run(handler, **overrides) -> list:
    return anyio.run(lambda: collect(handler, **overrides))


class SlowStream(httpx.AsyncByteStream):
    """Yields pieces with a delay; records whether the consumer closed it early."""

    def __init__(self, pieces: list[bytes], delay: float = 0.0, fail_after: int | None = None):
        self.pieces, self.delay, self.fail_after, self.closed = pieces, delay, fail_after, False

    async def __aiter__(self):
        for i, piece in enumerate(self.pieces):
            if self.fail_after is not None and i == self.fail_after:
                raise httpx.ReadError("connection dropped")
            await anyio.sleep(self.delay)
            yield piece

    async def aclose(self):
        self.closed = True


# --- Positive ----------------------------------------------------------------------

def test_positive_deltas_usage_and_finish_in_order():
    body = sse(delta("Leave "), delta("is 25 days", "stop"),
               {"choices": [], "usage": {"prompt_tokens": 300, "completion_tokens": 5}})
    items = run(lambda r: httpx.Response(200, content=body))
    assert items == [TextDelta("Leave "), TextDelta("is 25 days"), Usage(300, 5), Finish("stop")]


def test_positive_request_uses_only_standard_fields():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=sse(delta("x")))

    run(handler, temperature=0.0, max_tokens=64)
    assert seen["url"] == f"{URL}/chat/completions"
    assert set(seen["body"]) == {"model", "messages", "stream", "max_tokens", "temperature", "stream_options"}
    assert seen["body"]["stream"] is True and seen["body"]["model"] == "gemma3:1b"
    assert seen["body"]["temperature"] == 0.0 and seen["body"]["stream_options"] == {"include_usage": True}


def test_positive_many_small_deltas_are_yielded_individually():
    body = sse(*[delta(c) for c in "streaming"])
    assert [i.text for i in run(lambda r: httpx.Response(200, content=body)) if isinstance(i, TextDelta)] == list("streaming")


# --- Negative: each failure has its own code ---------------------------------------

@pytest.mark.parametrize("exc, code", [
    (httpx.ConnectError("refused"), "model_unavailable"),
    (httpx.ConnectTimeout("slow"), "model_timeout"),
    (httpx.ReadTimeout("slow"), "model_timeout"),
])
def test_negative_transport_failures_before_stream(exc, code):
    def handler(request):
        raise exc
    with pytest.raises(InferenceError) as err:
        run(handler)
    assert err.value.code == code and err.value.started is False


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_negative_http_error_status(status):
    with pytest.raises(InferenceError) as err:
        run(lambda r: httpx.Response(status, text="error details not exposed"))
    assert err.value.code == "model_http_error"


def test_negative_stream_drops_after_text_started():
    stream = SlowStream([f"data: {json.dumps(delta('partial '))}\n\n".encode(), b"never"], fail_after=1)
    with pytest.raises(InferenceError) as err:
        run(lambda r: httpx.Response(200, stream=stream))
    assert err.value.code == "model_stream_failed" and err.value.started is True


def test_negative_malformed_json_payload():
    with pytest.raises(InferenceError) as err:
        run(lambda r: httpx.Response(200, content=b"data: {not json}\n\n"))
    assert err.value.code == "model_stream_failed"


def test_negative_stalled_stream_hits_read_timeout():
    # A gap longer than read_timeout between pieces is a timeout, not an endless wait.
    def handler(request):
        raise httpx.ReadTimeout("no data")
    with pytest.raises(InferenceError) as err:
        run(handler, read_timeout=0.1)
    assert err.value.code == "model_timeout"


def test_negative_whole_request_deadline():
    pieces = [f"data: {json.dumps(delta('x'))}\n\n".encode()] * 20
    stream = SlowStream(pieces, delay=0.05)
    with pytest.raises(InferenceError) as err:
        run(lambda r: httpx.Response(200, stream=stream), request_timeout=0.2)
    assert err.value.code == "model_timeout" and err.value.started is True


# --- Edge --------------------------------------------------------------------------

def test_edge_no_usage_reported_is_fine():
    items = run(lambda r: httpx.Response(200, content=sse(delta("ok", "stop"))))
    assert not any(isinstance(i, Usage) for i in items) and items[-1] == Finish("stop")


def test_edge_missing_done_marker_still_finishes():
    items = run(lambda r: httpx.Response(200, content=sse(delta("ok"), done=False)))
    assert items == [TextDelta("ok"), Finish("unknown")]


def test_edge_comments_blank_lines_and_empty_deltas_ignored():
    body = b": keep-alive\n\nevent: ping\n\n" + sse({"choices": [{"delta": {}}]}, {"choices": [{"delta": {"role": "assistant"}}]}, delta("hi"))
    assert [i for i in run(lambda r: httpx.Response(200, content=body)) if isinstance(i, TextDelta)] == [TextDelta("hi")]


def test_edge_length_finish_reason_reported():
    assert run(lambda r: httpx.Response(200, content=sse(delta("cut", "length"))))[-1] == Finish("length")


def test_edge_consumer_stopping_early_closes_upstream():
    # REQ-033: when the client goes away, iteration stops and the upstream response
    # must be closed, which tells the model server to stop generating.
    stream = SlowStream([f"data: {json.dumps(delta(str(i)))}\n\n".encode() for i in range(50)], delay=0.01)

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))) as client:
            gen = stream_chat(client, url=URL, model="m", messages=[], max_tokens=1, temperature=0,
                              connect_timeout=1, read_timeout=1, request_timeout=5)
            async for _ in gen:
                break
            await gen.aclose()

    anyio.run(main)
    assert stream.closed is True
