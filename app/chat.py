# Chat pipeline: one question in, an ordered sequence of events out.
#
# Orchestrates the boundaries for a single request, in this order:
#   corpus refresh (ADR-005) -> evidence selection (ADR-007) -> prompt assembly
#   (ADR-006 C1/C2/C7) -> inference stream (ADR-002) -> output guard (ADR-006 C4/C5)
# and reports each step as a plain event dict. It knows nothing about HTTP or SSE
# framing; app/sse.py and app/main.py turn the events into a response (ADR-004).
#
# Event sequence (always exactly one terminal event, `done` or `error`):
#   meta -> sources -> [notice] -> token* -> [refusal] -> done
#   meta -> [sources] -> [notice] -> token* -> error
#
# Deterministic guards that do not depend on the model:
#   - no qualifying evidence: fixed reply, the model is not called (ADR-006 C6)
#   - sources come from selection, not from model text (ADR-006 C3)
#   - documents that match the question about equally well are named in a `notice`
#     event, so a possible conflict is surfaced even if the model picks one (ADR-019)
#   - prompt too large: rejected, never silently truncated (REQ-055)
#
# Observability (ADR-008, REQ-080..084): one trace per request. The root span
# `chat.request` has a child span per stage: `corpus.refresh`, `evidence.selection`,
# `prompt.assembly`, `inference.stream`. The trace id is the request id, shown to the
# user, carried on every event and log line. Spans record counts, timings, chunk ids
# and token counts (labelled reported/estimated), the model backend (derived from
# LLM_URL) and error codes; never the question, the prompt, document text, the answer
# text or the LLM_URL itself (REQ-068). Spans are started and ended explicitly instead of being made
# "current", because a context attached inside an async generator cannot be detached
# safely once the generator is closed from another task (client disconnect).

import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import anyio
import httpx
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode, Tracer

from app.config import Settings
from app.corpus import Corpus
from app.index import IndexCache
from app.inference import Finish, InferenceError, TextDelta, Usage, stream_chat
from app.observability import REQUEST_ID, llm_backend_attributes
from app.output_guard import APPROVAL_REFUSAL, REFUSAL, ApprovalGuard, OutputGuard
from app.prompt import QUOTABLE_RULES, PromptTooLarge, assemble
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
    tracer: Tracer


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def _error(request_id: str, code: str, partial: bool) -> dict:
    return {
        "event": "error",
        "data": {"request_id": request_id, "code": code, "message": ERROR_MESSAGES[code], "partial": partial},
    }


def competing_sources_message(files: tuple[str, ...]) -> str:
    return (
        f"This answer draws on {len(files)} documents that match your question about equally well: "
        f"{', '.join(files)}. They may disagree, and the answer may reflect only one of them. Check each one."
    )


def _request_id(span: trace.Span) -> str:
    ctx = span.get_span_context()
    # A non-recording tracer has no trace id; fall back so ids are always unique.
    return format(ctx.trace_id, "032x") if ctx.is_valid else uuid.uuid4().hex


async def answer(question: str, deps: ChatDeps) -> AsyncIterator[dict]:
    s = deps.settings
    tracer = deps.tracer
    root = tracer.start_span(
        "chat.request",
        attributes={"http.request.method": "POST", "http.route": "/chat", "chat.question_chars": len(question)},
    )
    parent = trace.set_span_in_context(root)
    request_id = _request_id(root)
    root.set_attribute("chat.request_id", request_id)
    id_token = REQUEST_ID.set(request_id)
    timings: dict[str, float] = {}
    started_at = time.perf_counter()
    outcome = "client_disconnected"  # overwritten on every normal or error exit
    open_spans: list[trace.Span] = []

    def stage(name: str) -> trace.Span:
        span = tracer.start_span(name, context=parent)
        open_spans.append(span)
        return span

    def end(span: trace.Span, error_code: str | None = None) -> None:
        if error_code:
            span.set_status(Status(StatusCode.ERROR, error_code))
            span.set_attribute("error.code", error_code)
        span.end()
        open_spans.remove(span)

    log.info("chat start", extra={"question_chars": len(question)})
    try:
        yield {"event": "meta", "data": {"request_id": request_id}}

        # Blocking file I/O and CPU-bound indexing run in a worker thread.
        span = stage("corpus.refresh")
        t = time.perf_counter()
        snapshot = await anyio.to_thread.run_sync(deps.corpus.refresh)
        timings["corpus_refresh_ms"] = _ms(t)
        st = snapshot.stats
        span.set_attributes(
            {
                "corpus.version": snapshot.version,
                "corpus.documents": len(snapshot.documents),
                "corpus.added": st.added,
                "corpus.modified": st.modified,
                "corpus.removed": st.removed,
                "corpus.unchanged": st.unchanged,
                "corpus.skipped": st.skipped,
                "corpus.skipped_errors": [f"{k.rel_path}:{k.reason}" for k in snapshot.skips if k.severity == "error"],
            }
        )
        end(span)

        span = stage("evidence.selection")
        t = time.perf_counter()
        index = await anyio.to_thread.run_sync(deps.index_cache.get, snapshot)
        selection = select(index, question, s.context_token_budget, s.selection_min_score)
        timings["selection_ms"] = _ms(t)
        span.set_attributes(
            {
                "selection.index_chunks": len(index),
                "selection.query_terms": len(selection.query_terms),
                "selection.candidates": selection.candidates_considered,
                "selection.chunk_ids": [c.chunk_id for c in selection.chunks],
                "selection.files": list(selection.source_files),
                "selection.budget_tokens": selection.budget_tokens,
                "selection.used_tokens": selection.used_tokens,
                "selection.dropped_chunks": selection.dropped_count,
                # The first DROPPED_DETAIL_LIMIT dropped chunks in rank order, as two
                # parallel lists (span attributes can't hold objects). Ids and scores
                # only, never chunk text (REQ-084).
                "selection.dropped_chunk_ids": [d.chunk_id for d in selection.dropped_top],
                "selection.dropped_scores": [d.score for d in selection.dropped_top],
                "selection.competing_files": list(selection.competing_files),
                "selection.truncated": selection.truncated,
                "selection.token_count_method": TOKEN_METHOD,
                "selection.insufficient_reason": selection.insufficient_reason or "",
            }
        )
        end(span)
        log.info(
            "evidence selected",
            extra={
                "chunk_ids": [c.chunk_id for c in selection.chunks],
                "used_tokens": selection.used_tokens,
                "dropped_chunks": selection.dropped_count,
                "insufficient_reason": selection.insufficient_reason,
                "competing_files": list(selection.competing_files),
            },
        )

        yield {
            "event": "sources",
            "data": {
                "request_id": request_id,
                "corpus_version": snapshot.version,
                "documents": len(snapshot.documents),
                # Unusable files are shown, not silently omitted (REQ-056, REQ-070).
                "skipped_files": [
                    {"path": k.rel_path, "reason": k.reason} for k in snapshot.skips if k.severity == "error"
                ],
                "chunks": [
                    {
                        "id": c.chunk_id,
                        "source": c.rel_path,
                        "score": c.score,
                        "estimated_tokens": c.estimated_tokens,
                        "truncated": c.truncated,
                    }
                    for c in selection.chunks
                ],
                "files": list(selection.source_files),
                "budget_tokens": selection.budget_tokens,
                "used_tokens": selection.used_tokens,
                "dropped_chunks": selection.dropped_count,
                "truncated": selection.truncated,
                "insufficient_reason": selection.insufficient_reason,
                "token_count_method": TOKEN_METHOD,
            },
        }

        if selection.competing_files:
            # From selection code, not the model: shown whatever the model answers (REQ-054).
            yield {
                "event": "notice",
                "data": {
                    "code": "competing_sources",
                    "files": list(selection.competing_files),
                    "message": competing_sources_message(selection.competing_files),
                },
            }

        # Testing the reason itself (not `selection.sufficient`) lets the type checker
        # see it is a str here.
        if selection.insufficient_reason is not None:
            text = INSUFFICIENT_REPLIES[selection.insufficient_reason]
            yield {"event": "token", "data": {"text": text}}
            outcome = "insufficient_evidence"
            root.set_attribute("chat.model_called", False)
            yield {
                "event": "done",
                "data": {
                    "request_id": request_id,
                    "finish_reason": outcome,
                    "model_called": False,
                    "timings_ms": timings,
                },
            }
            return

        span = stage("prompt.assembly")
        t = time.perf_counter()
        try:
            prompt = assemble(question, selection, s.llm_context_tokens, s.llm_max_tokens)
        except PromptTooLarge as exc:
            span.set_attributes({"prompt.estimated_tokens": exc.prompt_tokens, "prompt.limit_tokens": exc.limit})
            end(span, "question_too_long")
            outcome = "question_too_long"
            yield _error(request_id, outcome, partial=False)
            return
        timings["prompt_assembly_ms"] = _ms(t)
        span.set_attributes(
            {
                "prompt.estimated_tokens": prompt.estimated_prompt_tokens,
                "prompt.limit_tokens": s.llm_context_tokens - s.llm_max_tokens,
                "prompt.evidence_chunks": len(prompt.evidence_chunk_ids),
                "prompt.token_count_method": TOKEN_METHOD,
            }
        )
        end(span)

        guard = OutputGuard(prompt.messages[0]["content"], QUOTABLE_RULES)
        approval = ApprovalGuard(question)  # C9, on the text the leak/reasoning guard releases
        usage: Usage | None = None
        finish_reason = "unknown"
        emitted_chars = 0
        span = stage("inference.stream")
        span.set_attributes(
            {
                "llm.model": s.llm_model,
                "llm.temperature": s.llm_temperature,
                "llm.max_tokens": s.llm_max_tokens,
                **llm_backend_attributes(s.llm_url),
            }
        )
        root.set_attribute("chat.model_called", True)
        t = time.perf_counter()
        try:
            async for item in stream_chat(
                deps.http_client,
                url=s.llm_url,
                model=s.llm_model,
                messages=list(prompt.messages),
                max_tokens=s.llm_max_tokens,
                temperature=s.llm_temperature,
                connect_timeout=s.llm_connect_timeout_seconds,
                read_timeout=s.llm_read_timeout_seconds,
                request_timeout=s.llm_request_timeout_seconds,
            ):
                if isinstance(item, TextDelta):
                    released = approval.feed(guard.feed(item.text))
                    if guard.blocked or approval.blocked:
                        break  # leaving the loop closes the upstream stream (ADR-006 C4, ADR-021)
                    if released:
                        if "first_token_ms" not in timings:
                            timings["first_token_ms"] = _ms(started_at)
                            span.add_event("first_token")
                        emitted_chars += len(released)
                        yield {"event": "token", "data": {"text": released}}
                elif isinstance(item, Usage):
                    usage = item
                elif isinstance(item, Finish):
                    finish_reason = item.reason
        except InferenceError as exc:
            timings["inference_ms"] = _ms(t)
            span.set_attribute("llm.partial", emitted_chars > 0)
            end(span, exc.code)
            outcome = exc.code
            log.warning("model call failed", extra={"code": exc.code, "partial": emitted_chars > 0})
            yield _error(request_id, exc.code, partial=emitted_chars > 0)
            return
        timings["inference_ms"] = _ms(t)

        tail = "" if guard.blocked or approval.blocked else approval.feed(guard.finish())
        tail += "" if guard.blocked or approval.blocked else approval.finish()
        if guard.blocked:
            outcome = "instruction_leak_blocked"
            log.warning("output guard blocked instruction leak")
            yield {"event": "refusal", "data": {"text": REFUSAL}}
        elif approval.blocked:
            # The refusal replaces anything already shown; the sources stay visible.
            outcome = "unsupported_approval_blocked"
            log.warning("output guard blocked an unsupported approval")
            yield {"event": "refusal", "data": {"text": APPROVAL_REFUSAL}}
        else:
            if tail:
                # A short answer can be released only here (the last word is held, C9).
                if "first_token_ms" not in timings:
                    timings["first_token_ms"] = _ms(started_at)
                    span.add_event("first_token")
                emitted_chars += len(tail)
                yield {"event": "token", "data": {"text": tail}}
            outcome = finish_reason
        if guard.events.reasoning_removed:
            log.info("output guard removed reasoning", extra={"blocks": guard.events.reasoning_removed})

        # Token counts: reported by the endpoint when available, otherwise estimated
        # with the documented method; always labelled (REQ-083).
        if usage is not None:
            prompt_tokens, completion_tokens, token_source = usage.prompt_tokens, usage.completion_tokens, "reported"
        else:
            prompt_tokens = prompt.estimated_prompt_tokens
            completion_tokens = estimate_tokens("x" * emitted_chars)
            token_source = f"estimated ({TOKEN_METHOD})"
        tokens = {"prompt": prompt_tokens, "completion": completion_tokens, "source": token_source}
        span.set_attributes(
            {
                "llm.finish_reason": outcome,
                "llm.prompt_tokens": prompt_tokens,
                "llm.completion_tokens": completion_tokens,
                "llm.token_count_source": token_source,
                "llm.first_token_ms": timings.get("first_token_ms", -1.0),
                "llm.answer_chars": emitted_chars,
                "guard.reasoning_blocks_removed": guard.events.reasoning_removed,
                "guard.leak_blocked": guard.blocked,
                "guard.approval_blocked": approval.blocked,
            }
        )
        end(span)
        yield {
            "event": "done",
            "data": {
                "request_id": request_id,
                "finish_reason": outcome,
                "model_called": True,
                "tokens": tokens,
                "prompt_tokens_estimated": prompt.estimated_prompt_tokens,
                "reasoning_blocks_removed": guard.events.reasoning_removed,
                "timings_ms": timings,
            },
        }
    except Exception as exc:
        # Unexpected bug: still end the stream with a terminal event carrying the id.
        # The trace gets the exception class only; its message could contain data.
        log.exception("chat failed")
        outcome = "internal_error"
        root.set_attribute("error.type", type(exc).__name__)
        yield _error(request_id, outcome, partial=False)
    finally:
        # Runs on normal completion, on errors and when the client disconnects (the
        # generator is closed; outcome stays "client_disconnected"). Every span is
        # ended so a cut-off request still appears as a complete trace.
        for span in open_spans:
            end(span, outcome if outcome == "client_disconnected" else None)
        total_ms = _ms(started_at)
        root.set_attributes({"chat.outcome": outcome, "chat.total_ms": total_ms})
        if outcome in ERROR_MESSAGES or outcome == "client_disconnected":
            root.set_status(Status(StatusCode.ERROR, outcome))
        root.end()
        log.info("chat end", extra={"outcome": outcome, "total_ms": total_ms, "timings_ms": timings})
        try:
            REQUEST_ID.reset(id_token)
        except ValueError:
            # Closed from a different context (client disconnect): nothing to restore.
            pass
