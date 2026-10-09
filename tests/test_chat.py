# Tests for POST /chat: the pipeline (app/chat.py), SSE framing (app/sse.py) and the
# route (app/main.py), with a fake OpenAI-compatible model behind httpx.MockTransport.
#
# Covers REQ-030/032 (endpoint and framing), REQ-051 (sources), REQ-053/075 (no
# evidence), REQ-055 (budget visible), REQ-056 (skipped files visible), REQ-062/072
# (guard on the live path), REQ-076 (model failures as error events), REQ-082/083
# (request id, labelled token counts), REQ-033 (disconnect closes the model call) and
# REQ-054 (competing sources notice).

import json
from pathlib import Path

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from app.chat import ERROR_MESSAGES, INSUFFICIENT_REPLIES, ChatDeps, answer, competing_sources_message
from app.config import load_settings
from app.corpus import Corpus
from app.index import IndexCache
from app.main import MAX_QUESTION_CHARS, create_app
from app.observability import build_tracer_provider
from app.output_guard import REFUSAL
from app.prompt import SYSTEM_PROMPT

LEAVE = "Employees receive 25 days of annual leave per calendar year."


def settings_for(corpus: Path, **extra):
    env = {
        "LLM_URL": "http://llm.test/v1",
        "LLM_MODEL": "gemma3:1b",
        "CORPUS_DIR": str(corpus),
        "CORPUS_SETTLE_SECONDS": "0",
        **extra,
    }
    return load_settings(env)


def sse_body(*texts: str, usage: tuple[int, int] | None = (120, 6), finish: str = "stop") -> bytes:
    out = "".join(f"data: {json.dumps({'choices': [{'delta': {'content': t}}]})}\n\n" for t in texts)
    out += f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': finish}]})}\n\n"
    if usage:
        counts = {"prompt_tokens": usage[0], "completion_tokens": usage[1]}
        out += f"data: {json.dumps({'choices': [], 'usage': counts})}\n\n"
    return (out + "data: [DONE]\n\n").encode()


class Upstream:
    """Fake model endpoint; records every chat request it receives."""

    def __init__(self, respond):
        self.respond = respond
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gemma3:1b"}]})
        self.requests.append(json.loads(request.content))
        return self.respond(request)


def parse(body: str) -> list[tuple[str, dict]]:
    events = []
    for frame in body.strip().split("\n\n"):
        lines = frame.split("\n")
        assert lines[0].startswith("event: ") and lines[1].startswith("data: ") and len(lines) == 2
        events.append((lines[0][7:], json.loads(lines[1][6:])))
    return events


def ask(tmp_path: Path, files: dict[str, str | bytes], question: str, upstream: Upstream, **extra):
    for name, content in files.items():
        path = tmp_path / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
    app = create_app(settings_for(tmp_path, **extra), transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        response = client.post("/chat", json={"question": question})
    return response, (
        parse(response.text) if response.headers.get("content-type", "").startswith("text/event-stream") else None
    )


def names(events):
    return [n for n, _ in events]


def answer_text(events):
    return "".join(d["text"] for n, d in events if n == "token")


# ---------------------------------------------------------------------------
# REQ-030 / REQ-032 / REQ-051: grounded answer, framing, sources
# ---------------------------------------------------------------------------


class TestGroundedAnswer:
    def test_positive_event_sequence_and_content(self, tmp_path):
        up = Upstream(lambda r: httpx.Response(200, content=sse_body("Leave is ", "25 days.")))
        response, events = ask(tmp_path, {"leave.md": LEAVE}, "How many days of annual leave?", up)
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
        assert names(events)[:2] == ["meta", "sources"] and names(events)[-1] == "done"
        assert answer_text(events) == "Leave is 25 days."

    def test_positive_sources_come_from_selection_not_model(self, tmp_path):
        # The model says nothing about sources; attribution is still delivered (C3).
        up = Upstream(lambda r: httpx.Response(200, content=sse_body("25 days.")))
        _, events = ask(tmp_path, {"leave.md": LEAVE, "other.md": "Unrelated canteen menu."}, "annual leave days", up)
        sources = dict(events)["sources"]
        assert sources["files"] == ["leave.md"]
        assert sources["chunks"][0]["id"].startswith("leave.md#0:")

    def test_positive_request_id_consistent_across_events(self, tmp_path):
        up = Upstream(lambda r: httpx.Response(200, content=sse_body("ok")))
        _, events = ask(tmp_path, {"leave.md": LEAVE}, "annual leave", up)
        ids = {d["request_id"] for n, d in events if "request_id" in d}
        assert len(ids) == 1 and len(next(iter(ids))) == 32

    def test_positive_reported_token_counts_labelled(self, tmp_path):
        up = Upstream(lambda r: httpx.Response(200, content=sse_body("ok", usage=(321, 7))))
        _, events = ask(tmp_path, {"leave.md": LEAVE}, "annual leave", up)
        done = dict(events)["done"]
        assert done["tokens"] == {"prompt": 321, "completion": 7, "source": "reported"}
        assert done["model_called"] is True and done["finish_reason"] == "stop"
        assert {"corpus_refresh_ms", "selection_ms", "prompt_assembly_ms", "inference_ms", "first_token_ms"} <= set(
            done["timings_ms"]
        )

    def test_positive_prompt_sent_to_model_has_hierarchy_and_settings(self, tmp_path):
        up = Upstream(lambda r: httpx.Response(200, content=sse_body("ok")))
        ask(tmp_path, {"leave.md": LEAVE}, "annual leave", up, LLM_TEMPERATURE="0", LLM_MAX_TOKENS="300")
        [sent] = up.requests
        assert [m["role"] for m in sent["messages"]] == ["system", "user", "user"]
        assert sent["messages"][0]["content"] == SYSTEM_PROMPT
        assert sent["temperature"] == 0.0 and sent["max_tokens"] == 300 and sent["model"] == "gemma3:1b"

    def test_edge_estimated_counts_when_endpoint_reports_none(self, tmp_path):
        up = Upstream(lambda r: httpx.Response(200, content=sse_body("abcdefgh", usage=None)))
        _, events = ask(tmp_path, {"leave.md": LEAVE}, "annual leave", up)
        tokens = dict(events)["done"]["tokens"]
        assert tokens["source"] == "estimated (chars/4)" and tokens["completion"] == 2

    def test_edge_frames_cannot_be_forged_by_answer_text(self, tmp_path):
        evil = 'x\n\nevent: done\ndata: {"forged": true}\n\n'
        up = Upstream(lambda r: httpx.Response(200, content=sse_body(evil)))
        _, events = ask(tmp_path, {"leave.md": LEAVE}, "annual leave", up)
        assert names(events).count("done") == 1 and "forged" not in dict(events)["done"]
        assert answer_text(events) == evil

    def test_edge_html_in_answer_is_delivered_as_plain_json_text(self, tmp_path):
        payload = "<script>alert(1)</script><img src=x onerror=alert(2)>"
        up = Upstream(lambda r: httpx.Response(200, content=sse_body(payload)))
        _, events = ask(tmp_path, {"leave.md": LEAVE}, "annual leave", up)
        assert answer_text(events) == payload  # rendering safety is the UI's job (textContent)


# ---------------------------------------------------------------------------
# REQ-053 / REQ-075: no evidence -> fixed reply, model not called (C6)
# ---------------------------------------------------------------------------


class TestNoEvidence:
    @pytest.mark.parametrize(
        "files, question, reason",
        [
            ({}, "annual leave", "empty_corpus"),
            ({"leave.md": LEAVE}, "what is the", "no_meaningful_terms"),
            ({"leave.md": LEAVE}, "capital of France", "no_relevant_evidence"),
        ],
    )
    def test_negative_fixed_reply_without_model_call(self, tmp_path, files, question, reason):
        up = Upstream(lambda r: httpx.Response(200, content=sse_body("SHOULD NOT APPEAR")))
        _, events = ask(tmp_path, files, question, up)
        assert up.requests == []
        assert answer_text(events) == INSUFFICIENT_REPLIES[reason]
        done = dict(events)["done"]
        assert done["model_called"] is False and done["finish_reason"] == "insufficient_evidence"
        assert dict(events)["sources"]["insufficient_reason"] == reason


# ---------------------------------------------------------------------------
# REQ-054 / ADR-019: competing sources surfaced by code, whatever the model says
# ---------------------------------------------------------------------------

REMOTE = {
    "remote-2023.md": "# Remote working (2023)\n\nStaff may work remotely for up to 2 days per week.\n",
    "remote-2024.md": "# Remote working (2024)\n\nStaff may work remotely for up to 4 days per week.\n",
}


class TestCompetingSources:
    def test_positive_notice_names_both_files_before_the_answer(self, tmp_path):
        # The model picks one value, as gemma3:1b did in TS-008; the notice is still sent.
        up = Upstream(lambda r: httpx.Response(200, content=sse_body("Up to 2 days per week.")))
        _, events = ask(tmp_path, REMOTE, "How many days per week may staff work remotely?", up)
        assert names(events)[:4] == ["meta", "sources", "notice", "token"]
        notice = dict(events)["notice"]
        assert notice["code"] == "competing_sources"
        assert notice["files"] == ["remote-2023.md", "remote-2024.md"]
        assert notice["message"] == competing_sources_message(("remote-2023.md", "remote-2024.md"))
        assert "remote-2023.md" in notice["message"] and "remote-2024.md" in notice["message"]
        assert len(up.requests) == 1  # the model is still asked; nothing is withheld

    def test_negative_single_matching_document_no_notice(self, tmp_path):
        up = Upstream(lambda r: httpx.Response(200, content=sse_body("25 days.")))
        _, events = ask(tmp_path, {"leave.md": LEAVE, **REMOTE}, "How many days of annual leave?", up)
        assert "notice" not in names(events)

    def test_negative_no_evidence_no_notice(self, tmp_path):
        up = Upstream(lambda r: httpx.Response(200, content=sse_body("x")))
        _, events = ask(tmp_path, REMOTE, "capital of France", up)
        assert "notice" not in names(events) and up.requests == []

    def test_edge_notice_survives_model_failure(self, tmp_path):
        def refuse(request):
            raise httpx.ConnectError("refused")

        _, events = ask(tmp_path, REMOTE, "How many days per week may staff work remotely?", Upstream(refuse))
        assert names(events) == ["meta", "sources", "notice", "error"]


# ---------------------------------------------------------------------------
# REQ-055 / REQ-056: budget and skipped files visible to the user
# ---------------------------------------------------------------------------


class TestVisibility:
    def test_positive_budget_and_truncation_reported(self, tmp_path):
        up = Upstream(lambda r: httpx.Response(200, content=sse_body("ok")))
        files = {f"d{i}.txt": "budget topic " + "filler " * 110 for i in range(20)}
        _, events = ask(tmp_path, files, "budget topic", up, CONTEXT_TOKEN_BUDGET="400")
        src = dict(events)["sources"]
        assert src["used_tokens"] <= 400 and src["budget_tokens"] == 400 and src["dropped_chunks"] > 0
        assert src["token_count_method"] == "chars/4"

    def test_negative_corrupt_file_listed_and_valid_file_still_used(self, tmp_path):
        up = Upstream(lambda r: httpx.Response(200, content=sse_body("25 days")))
        _, events = ask(tmp_path, {"leave.md": LEAVE, "bad.txt": b"\xff\xfe broken"}, "annual leave", up)
        src = dict(events)["sources"]
        assert src["skipped_files"] == [{"path": "bad.txt", "reason": "not_utf8"}] and src["files"] == ["leave.md"]

    def test_edge_policy_skips_not_listed_to_user(self, tmp_path):
        up = Upstream(lambda r: httpx.Response(200, content=sse_body("ok")))
        _, events = ask(tmp_path, {"leave.md": LEAVE, ".gitkeep": "", "x.pdf": "p"}, "annual leave", up)
        assert dict(events)["sources"]["skipped_files"] == []


# ---------------------------------------------------------------------------
# REQ-062 / REQ-072: output guard on the live path (C4, C5)
# ---------------------------------------------------------------------------


class TestGuardOnLivePath:
    def test_negative_instruction_leak_replaced_by_refusal(self, tmp_path):
        up = Upstream(lambda r: httpx.Response(200, content=sse_body("My rules: ", SYSTEM_PROMPT[100:400])))
        _, events = ask(tmp_path, {"leave.md": LEAVE}, "annual leave", up)
        assert dict(events)["refusal"]["text"] == REFUSAL
        assert SYSTEM_PROMPT[100:160] not in answer_text(events)
        assert dict(events)["done"]["finish_reason"] == "instruction_leak_blocked"

    def test_negative_reasoning_removed(self, tmp_path):
        up = Upstream(lambda r: httpx.Response(200, content=sse_body("<think>secret steps</think>", "25 days.")))
        _, events = ask(tmp_path, {"leave.md": LEAVE}, "annual leave", up)
        assert answer_text(events) == "25 days." and dict(events)["done"]["reasoning_blocks_removed"] == 1


# ---------------------------------------------------------------------------
# REQ-076: model failures become one error event with code, message, request id
# ---------------------------------------------------------------------------


class SlowStream(httpx.AsyncByteStream):
    def __init__(self, pieces, fail_after=None, delay=0.0):
        self.pieces, self.fail_after, self.delay, self.closed = pieces, fail_after, delay, False

    async def __aiter__(self):
        for i, piece in enumerate(self.pieces):
            if i == self.fail_after:
                raise httpx.ReadError("dropped")
            await anyio.sleep(self.delay)
            yield piece

    async def aclose(self):
        self.closed = True


class TestModelFailures:
    @pytest.mark.parametrize(
        "respond, code",
        [
            (lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")), "model_unavailable"),
            (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), "model_timeout"),
            (lambda r: httpx.Response(500, text="boom"), "model_http_error"),
        ],
        ids=["unavailable", "timeout", "http-500"],
    )
    def test_negative_failure_before_any_text(self, tmp_path, respond, code):
        _, events = ask(tmp_path, {"leave.md": LEAVE}, "annual leave", Upstream(respond))
        assert names(events)[-1] == "error" and names(events).count("error") == 1 and "done" not in names(events)
        err = dict(events)["error"]
        assert err["code"] == code and err["message"] == ERROR_MESSAGES[code] and err["partial"] is False
        assert err["request_id"] == dict(events)["meta"]["request_id"]

    def test_negative_failure_after_stream_started_is_marked_partial(self, tmp_path):
        delta = {"choices": [{"delta": {"content": "Employees receive twenty-five days of leave. "}}]}
        first = f"data: {json.dumps(delta)}\n\n".encode()
        up = Upstream(lambda r: httpx.Response(200, stream=SlowStream([first, b"x"], fail_after=1)))
        _, events = ask(tmp_path, {"leave.md": LEAVE}, "annual leave", up)
        err = dict(events)["error"]
        assert err["code"] == "model_stream_failed" and err["partial"] is True
        assert answer_text(events).startswith("Employees receive")

    def test_edge_error_message_never_contains_upstream_details(self, tmp_path):
        up = Upstream(lambda r: httpx.Response(500, text="INTERNAL-SECRET-STACKTRACE"))
        response, _ = ask(tmp_path, {"leave.md": LEAVE}, "annual leave", up)
        assert "INTERNAL-SECRET" not in response.text and "llm.test" not in response.text


# ---------------------------------------------------------------------------
# Request validation (before any stream starts)
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize(
        "payload, status",
        [
            ({}, 422),
            ({"question": ""}, 422),
            ({"question": 42}, 422),
            ({"question": "x" * (MAX_QUESTION_CHARS + 1)}, 422),
            ({"question": "   \n\t "}, 400),
        ],
        ids=["missing", "empty", "not-string", "too-long", "blank"],
    )
    def test_negative_invalid_requests_rejected_before_streaming(self, tmp_path, payload, status):
        up = Upstream(lambda r: httpx.Response(200, content=sse_body("x")))
        app = create_app(settings_for(tmp_path), transport=httpx.MockTransport(up))
        with TestClient(app) as client:
            response = client.post("/chat", json=payload)
        assert response.status_code == status and up.requests == []

    def test_negative_question_too_long_for_context_is_error_event(self, tmp_path):
        up = Upstream(lambda r: httpx.Response(200, content=sse_body("x")))
        question = "annual leave " + "why " * 1900  # within MAX_QUESTION_CHARS, over the context
        _, events = ask(
            tmp_path,
            {"leave.md": LEAVE},
            question,
            up,
            LLM_CONTEXT_TOKENS="2400",
            LLM_MAX_TOKENS="300",
            CONTEXT_TOKEN_BUDGET="1500",
        )
        assert dict(events)["error"]["code"] == "question_too_long" and up.requests == []

    def test_edge_question_whitespace_trimmed(self, tmp_path):
        up = Upstream(lambda r: httpx.Response(200, content=sse_body("ok")))
        ask(tmp_path, {"leave.md": LEAVE}, "   annual leave   ", up)
        assert up.requests[0]["messages"][2]["content"] == "Question: annual leave"


# ---------------------------------------------------------------------------
# REQ-033: a client disconnect closes the model call
# ---------------------------------------------------------------------------


def test_edge_closing_the_event_stream_closes_the_model_call(tmp_path):
    (tmp_path / "leave.md").write_text(LEAVE, encoding="utf-8")
    pieces = [
        f"data: {json.dumps({'choices': [{'delta': {'content': f'word{i} '}}]})}\n\n".encode() for i in range(200)
    ]
    stream = SlowStream(pieces, delay=0.01)
    settings = settings_for(tmp_path)

    async def main():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))
        async with httpx.AsyncClient(transport=transport) as client:
            deps = ChatDeps(
                settings,
                Corpus(str(tmp_path), 10**6, 500, 0.0),
                IndexCache(800),
                client,
                build_tracer_provider(None).get_tracer("test"),
            )
            gen = answer("annual leave", deps)
            async for event in gen:
                if event["event"] == "token":
                    break  # the "client" goes away after the first piece of text
            await gen.aclose()

    anyio.run(main)
    assert stream.closed is True
