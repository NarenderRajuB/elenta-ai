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
from typing import TypeGuard

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
    # The stream must say it is complete, with [DONE] or a finish reason: a connection
    # that simply ends after some text is an interrupted answer, not a finished one (TS-018).
    completed = False
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
                    completed = True
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    raise InferenceError("model_stream_failed", started=started) from None
                # Every field is type-checked before use: a malformed payload becomes a
                # fixed error code, never an exception carrying upstream values (TS-018).
                if not isinstance(chunk, dict):
                    raise InferenceError("model_stream_failed", started=started)
                choices = chunk.get("choices") or []
                if not isinstance(choices, list):
                    raise InferenceError("model_stream_failed", started=started)
                usage = _usage(chunk.get("usage"))
                if usage is not None:
                    yield usage
                for choice in choices:
                    if not isinstance(choice, dict):
                        raise InferenceError("model_stream_failed", started=started)
                    delta = choice.get("delta")
                    text = delta.get("content") if isinstance(delta, dict) else None
                    if isinstance(text, str) and text:
                        started = True
                        yield TextDelta(text)
                    reason = choice.get("finish_reason")
                    if isinstance(reason, str) and reason:
                        finish_reason = reason
                        completed = True
    except InferenceError:
        raise
    except httpx.TimeoutException:
        raise InferenceError("model_timeout", started=started) from None
    except httpx.ConnectError:
        raise InferenceError("model_unavailable", started=False) from None
    except httpx.HTTPError:
        # Connection dropped or protocol error mid-stream.
        raise InferenceError("model_stream_failed", started=started) from None
    if not completed:
        raise InferenceError("model_stream_failed", started=started)
    yield Finish(finish_reason)


def _is_count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _usage(value: object) -> Usage | None:
    """Reported token counts, or None if absent or malformed (counts are then
    estimated and labelled as such downstream)."""
    if not isinstance(value, dict):
        return None
    prompt, completion = value.get("prompt_tokens"), value.get("completion_tokens", 0)
    if _is_count(prompt) and _is_count(completion):
        return Usage(prompt, completion)
    return None
