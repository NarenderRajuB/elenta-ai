# Tests for REQ-017: liveness (/healthz) and readiness (/readyz) signals.
#
# The model endpoint is replaced by httpx.MockTransport, so these tests are
# deterministic and need no running Ollama. The real-network path is covered by
# tests/test_entrypoint.py.

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

SETTINGS = Settings(llm_url="http://llm.test:11434/v1", llm_model="qwen2.5:0.5b")
MODELS_OK = {"object": "list", "data": [{"id": "qwen2.5:0.5b"}, {"id": "other:1b"}]}


def _client(handler, settings: Settings = SETTINGS) -> TestClient:
    return TestClient(create_app(settings, transport=httpx.MockTransport(handler)))


def _readyz(handler, settings: Settings = SETTINGS) -> httpx.Response:
    with _client(handler, settings) as client:
        return client.get("/readyz")


def _raise(exc: Exception):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc
    return handler


# --- Positive ----------------------------------------------------------------

def test_positive_healthz_ok():
    with _client(lambda r: httpx.Response(200, json=MODELS_OK)) as client:
        response = client.get("/healthz")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_positive_ready_when_endpoint_lists_model():
    response = _readyz(lambda r: httpx.Response(200, json=MODELS_OK))
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"llm": {"ok": True}}}


def test_positive_probe_uses_standard_models_path():
    # REQ-022: only the OpenAI-compatible contract under the configured base URL.
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        return httpx.Response(200, json=MODELS_OK)

    _readyz(handler)
    assert seen == [("GET", "http://llm.test:11434/v1/models")]


# --- Negative: each failure maps to 503 with a fixed reason code ---------------

@pytest.mark.parametrize(
    "handler, reason",
    [
        (_raise(httpx.ConnectError("refused")), "unreachable"),
        (_raise(httpx.ReadTimeout("slow")), "timeout"),
        (_raise(httpx.ConnectTimeout("slow")), "timeout"),
        (lambda r: httpx.Response(500, text="boom"), "http_error"),
        (lambda r: httpx.Response(404, text="not found"), "http_error"),
        (lambda r: httpx.Response(200, text="<html>not json</html>"), "invalid_response"),
        (lambda r: httpx.Response(200, json={"models": []}), "invalid_response"),
        (lambda r: httpx.Response(200, json={"data": [{"name": "x"}]}), "invalid_response"),
        (lambda r: httpx.Response(200, json={"data": "nope"}), "invalid_response"),
        (lambda r: httpx.Response(200, json={"data": [{"id": "other:1b"}]}), "model_not_found"),
    ],
    ids=["refused", "read-timeout", "connect-timeout", "500", "404", "not-json",
         "no-data-key", "items-without-id", "data-not-list", "model-missing"],
)
def test_negative_not_ready(handler, reason):
    response = _readyz(handler)
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "checks": {"llm": {"ok": False, "reason": reason}}}


# --- Edge cases ----------------------------------------------------------------

def test_edge_liveness_stays_ok_when_model_is_down():
    # A model outage must not make liveness fail (it would trigger needless restarts).
    with _client(_raise(httpx.ConnectError("refused"))) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503


def test_edge_empty_model_list_is_model_not_found():
    assert _readyz(lambda r: httpx.Response(200, json={"data": []})).json()["checks"]["llm"]["reason"] == "model_not_found"


def test_edge_model_match_is_exact():
    # Same string is sent on chat requests, so a case-only difference must not pass.
    response = _readyz(lambda r: httpx.Response(200, json={"data": [{"id": "Qwen2.5:0.5B"}]}))
    assert response.json()["checks"]["llm"]["reason"] == "model_not_found"


def test_edge_readiness_recovers_without_restart():
    # Ollama started after the app: the next probe must report ready.
    state = {"up": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if not state["up"]:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json=MODELS_OK)

    with _client(handler) as client:
        assert client.get("/readyz").status_code == 503
        state["up"] = True
        assert client.get("/readyz").status_code == 200


def test_edge_response_never_leaks_url_or_credentials():
    # REQ-068: the probe response is visible to any caller.
    settings = Settings(llm_url="http://user:s3cret@llm.test/v1", llm_model="qwen2.5:0.5b")
    body = _readyz(_raise(httpx.ConnectError("refused to http://user:s3cret@llm.test")), settings).text
    assert "s3cret" not in body and "llm.test" not in body


def test_edge_configured_timeout_is_applied():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.extensions["timeout"])
        return httpx.Response(200, json=MODELS_OK)

    _readyz(handler, Settings(llm_url=SETTINGS.llm_url, llm_model=SETTINGS.llm_model, llm_health_timeout_seconds=0.5))
    assert seen["read"] == 0.5 and seen["connect"] == 0.5


def test_edge_outbound_client_ignores_proxy_environment(monkeypatch):
    # REQ-012: a stray HTTP(S)_PROXY must not redirect traffic away from LLM_URL.
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    app = create_app(SETTINGS, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=MODELS_OK)))
    with TestClient(app):
        assert app.state.http_client.trust_env is False
