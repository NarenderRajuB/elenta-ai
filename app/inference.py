# Inference access boundary: streams a chat completion from any OpenAI-compatible
# endpoint (ADR-002). The only provider-facing inputs are LLM_URL and LLM_MODEL (REQ-015,
# REQ-022); the request uses standard fields only (model, messages, stream, max_tokens,
# temperature, stream_options.include_usage).
#
# Failure handling (REQ-076, REQ-033): every way the call can fail becomes an
# InferenceError with a fixed code, so the transport layer can tell the user exactly what
# happened. Three limits bound the call: connect timeout, read timeout (gap between
# streamed pieces), and a whole-request deadline checked after every piece. The deadline
# is checked explicitly rather than with a cancel scope, because a cancel scope must not
# stay open across `yield` in an async generator. Worst case is therefore
# request_timeout + read_timeout. If the consumer stops iterating (client disconnected),
# the `async with` closes the HTTP response, which closes the connection, and the server
# stops generating.

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import anyio
import httpx


class InferenceError(Exception):
    # Codes: model_unavailable, model_timeout, model_http_error, model_stream_failed.
    # `started` tells the caller whether any answer text had already been produced.
    def __init__(self, code: str, started: bool) -> None:
        self.code = code
        self.started = started
        super().__init__(code)


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Usage:
    # Token counts as reported by the endpoint (labelled "reported" downstream).
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class Finish:
    reason: str  # e.g. "stop", "length"; "unknown" if the endpoint did not say


async def stream_chat(
    client: httpx.AsyncClient,
    *,
    url: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    connect_timeout: float,
    read_timeout: float,
    request_timeout: float,
) -> AsyncIterator[TextDelta | Usage | Finish]:
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Standard OpenAI field; endpoints that ignore it simply send no usage, and the
        # caller falls back to labelled estimates.
        "stream_options": {"include_usage": True},
    }
    timeout = httpx.Timeout(connect=connect_timeout, read=read_timeout, write=connect_timeout, pool=connect_timeout)
    started = False
    finish_reason = "unknown"
    deadline = anyio.current_time() + request_timeout
    try:
        async with client.stream("POST", f"{url}/chat/completions", json=body, timeout=timeout) as response:
            if response.status_code != 200:
                raise InferenceError("model_http_error", started=False)
            async for line in response.aiter_lines():
                if anyio.current_time() > deadline:
                    raise InferenceError("model_timeout", started=started)
                # SSE framing: payload lines start with "data:"; comments, blank lines
                # and other fields are ignored.
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    raise InferenceError("model_stream_failed", started=started) from None
                usage = chunk.get("usage")
                if isinstance(usage, dict) and "prompt_tokens" in usage:
                    yield Usage(int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))
                for choice in chunk.get("choices") or []:
                    text = (choice.get("delta") or {}).get("content")
                    if text:
                        started = True
                        yield TextDelta(text)
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
    except InferenceError:
        raise
    except httpx.TimeoutException:
        raise InferenceError("model_timeout", started=started) from None
    except httpx.ConnectError:
        raise InferenceError("model_unavailable", started=False) from None
    except httpx.HTTPError:
        # Connection dropped or protocol error mid-stream.
        raise InferenceError("model_stream_failed", started=started) from None
    yield Finish(finish_reason)
