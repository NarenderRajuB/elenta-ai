# Chat pipeline: one question in, an ordered sequence of events out.
#
# Orchestrates the boundaries for a single request, in this order:
#   corpus refresh (ADR-005) -> evidence selection (ADR-007) -> prompt assembly
#   (ADR-006 C1/C2/C7) -> inference stream (ADR-002) -> output guard (ADR-006 C4/C5)
# and reports each step as a plain event dict. It knows nothing about HTTP or SSE
# framing; app/sse.py and app/main.py turn the events into a response (ADR-004).
#
# Event sequence (always exactly one terminal event, `done` or `error`):
#   meta -> sources -> token* -> [refusal] -> done
#   meta -> [sources] -> token* -> error
#
# Deterministic guards that do not depend on the model:
#   - no qualifying evidence: fixed reply, the model is not called (ADR-006 C6)
#   - sources come from selection, not from model text (ADR-006 C3)
#   - prompt too large: rejected, never silently truncated (REQ-055)
#
# Logs carry the request id, stage timings and counts, never the question text, the
# prompt or document content (REQ-068).

import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import anyio
import httpx

from app.config import Settings
from app.corpus import Corpus
from app.index import IndexCache
from app.inference import Finish, InferenceError, TextDelta, Usage, stream_chat
from app.output_guard import REFUSAL, OutputGuard
from app.prompt import PromptTooLarge, assemble
from app.selection import select
from app.tokens import METHOD as TOKEN_METHOD
from app.tokens import estimate_tokens

log = logging.getLogger(__name__)

# Fixed user-visible texts. Kept here so behaviour and docs/tests refer to one source.
INSUFFICIENT_REPLIES = {
    "empty_corpus": "There are no readable documents in the corpus, so I can't answer this.",
    "no_meaningful_terms": "Please ask a question about the documents. I couldn't find any searchable words in it.",
    "no_relevant_evidence": "The documents do not contain enough information to answer this question.",
}
ERROR_MESSAGES = {
    "model_unavailable": "The language model is not reachable. Please try again shortly.",
    "model_timeout": "The language model took too long to respond.",
    "model_http_error": "The language model returned an error.",
    "model_stream_failed": "The answer was interrupted because the language model stream failed.",
    "question_too_long": "The question is too long to answer within the model's context window.",
    "internal_error": "Something went wrong while answering. Please try again.",
}


@dataclass
class ChatDeps:
    settings: Settings
    corpus: Corpus
    index_cache: IndexCache
    http_client: httpx.AsyncClient


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def _error(request_id: str, code: str, partial: bool) -> dict:
    return {"event": "error", "data": {"request_id": request_id, "code": code,
                                       "message": ERROR_MESSAGES[code], "partial": partial}}


async def answer(question: str, deps: ChatDeps, request_id: str | None = None) -> AsyncIterator[dict]:
    s = deps.settings
    request_id = request_id or uuid.uuid4().hex
    timings: dict[str, float] = {}
    started_at = time.perf_counter()
    outcome = "client_disconnected"  # overwritten on every normal or error exit
    log.info("chat start: request_id=%s question_chars=%d", request_id, len(question))
    try:
        yield {"event": "meta", "data": {"request_id": request_id}}

        # Blocking file I/O and CPU-bound indexing run in a worker thread.
        t = time.perf_counter()
        snapshot = await anyio.to_thread.run_sync(deps.corpus.refresh)
        timings["corpus_refresh_ms"] = _ms(t)

        t = time.perf_counter()
        index = await anyio.to_thread.run_sync(deps.index_cache.get, snapshot)
        selection = select(index, question, s.context_token_budget, s.selection_min_score)
        timings["selection_ms"] = _ms(t)

        yield {"event": "sources", "data": {
            "request_id": request_id,
            "corpus_version": snapshot.version,
            "documents": len(snapshot.documents),
            # Unusable files are shown, not silently omitted (REQ-056, REQ-070).
            "skipped_files": [{"path": k.rel_path, "reason": k.reason} for k in snapshot.skips if k.severity == "error"],
            "chunks": [{"id": c.chunk_id, "source": c.rel_path, "score": c.score,
                        "estimated_tokens": c.estimated_tokens, "truncated": c.truncated} for c in selection.chunks],
            "files": list(selection.source_files),
            "budget_tokens": selection.budget_tokens,
            "used_tokens": selection.used_tokens,
            "dropped_chunks": selection.dropped_count,
            "truncated": selection.truncated,
            "insufficient_reason": selection.insufficient_reason,
            "token_count_method": TOKEN_METHOD,
        }}

        if not selection.sufficient:
            text = INSUFFICIENT_REPLIES[selection.insufficient_reason]
            yield {"event": "token", "data": {"text": text}}
            outcome = "insufficient_evidence"
            yield {"event": "done", "data": {"request_id": request_id, "finish_reason": outcome,
                                             "model_called": False, "timings_ms": timings}}
            return

        t = time.perf_counter()
        try:
            prompt = assemble(question, selection, s.llm_context_tokens, s.llm_max_tokens)
        except PromptTooLarge:
            outcome = "question_too_long"
            yield _error(request_id, outcome, partial=False)
            return
        timings["prompt_assembly_ms"] = _ms(t)

        guard = OutputGuard(prompt.messages[0]["content"])
        usage: Usage | None = None
        finish_reason = "unknown"
        emitted_chars = 0
        t = time.perf_counter()
        try:
            async for item in stream_chat(
                deps.http_client, url=s.llm_url, model=s.llm_model, messages=list(prompt.messages),
                max_tokens=s.llm_max_tokens, temperature=s.llm_temperature,
                connect_timeout=s.llm_connect_timeout_seconds, read_timeout=s.llm_read_timeout_seconds,
                request_timeout=s.llm_request_timeout_seconds,
            ):
                if isinstance(item, TextDelta):
                    released = guard.feed(item.text)
                    if guard.blocked:
                        break  # leaving the loop closes the upstream stream (ADR-006 C4)
                    if released:
                        if "first_token_ms" not in timings:
                            timings["first_token_ms"] = _ms(started_at)
                        emitted_chars += len(released)
                        yield {"event": "token", "data": {"text": released}}
                elif isinstance(item, Usage):
                    usage = item
                elif isinstance(item, Finish):
                    finish_reason = item.reason
        except InferenceError as exc:
            timings["inference_ms"] = _ms(t)
            outcome = exc.code
            yield _error(request_id, exc.code, partial=emitted_chars > 0)
            return
        timings["inference_ms"] = _ms(t)

        if guard.blocked:
            outcome = "instruction_leak_blocked"
            log.warning("output guard blocked instruction leak: request_id=%s", request_id)
            yield {"event": "refusal", "data": {"text": REFUSAL}}
        else:
            tail = guard.finish()
            if tail:
                emitted_chars += len(tail)
                yield {"event": "token", "data": {"text": tail}}
            outcome = finish_reason
        if guard.events.reasoning_removed:
            log.info("output guard removed reasoning: request_id=%s blocks=%d", request_id, guard.events.reasoning_removed)

        # Token counts: reported by the endpoint when available, otherwise estimated
        # with the documented method; always labelled (REQ-083).
        if usage is not None:
            tokens = {"prompt": usage.prompt_tokens, "completion": usage.completion_tokens, "source": "reported"}
        else:
            tokens = {"prompt": prompt.estimated_prompt_tokens, "completion": estimate_tokens("x" * emitted_chars),
                      "source": f"estimated ({TOKEN_METHOD})"}
        yield {"event": "done", "data": {
            "request_id": request_id, "finish_reason": outcome, "model_called": True,
            "tokens": tokens, "prompt_tokens_estimated": prompt.estimated_prompt_tokens,
            "reasoning_blocks_removed": guard.events.reasoning_removed, "timings_ms": timings,
        }}
    except Exception:
        # Unexpected bug: still end the stream with a terminal event carrying the id.
        log.exception("chat failed: request_id=%s", request_id)
        outcome = "internal_error"
        yield _error(request_id, outcome, partial=False)
    finally:
        # Runs on normal completion, on errors and when the client disconnects
        # (the generator is closed; outcome stays "client_disconnected").
        log.info("chat end: request_id=%s outcome=%s total_ms=%.1f timings=%s",
                 request_id, outcome, _ms(started_at), timings)
