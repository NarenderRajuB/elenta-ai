# Tests for the minimal browser UI (ADR-010): REQ-005, REQ-030, REQ-065.
#
# Rendering safety is enforced structurally: the client code never uses an HTML sink,
# and the Content-Security-Policy forbids inline and third-party script, so untrusted
# text can only ever be displayed as text. A browser-level demo (HTML/script inside a
# document) is part of review scenario 5.

import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import ALLOWED_HOSTS, SECURITY_HEADERS, create_app

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
# Code only: app.js's header comment names the forbidden APIs to document the rule.
APP_JS_CODE = "\n".join(line for line in APP_JS.splitlines() if not line.lstrip().startswith("//"))
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def client(tmp_path):
    settings = load_settings({"LLM_URL": "http://llm.test/v1", "LLM_MODEL": "gemma3:1b", "CORPUS_DIR": str(tmp_path)})
    app = create_app(settings, transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c


# --- Positive ----------------------------------------------------------------------


def test_positive_index_page_served(client):
    response = client.get("/")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
    assert 'id="question"' in response.text and 'id="answer"' in response.text and 'id="sources"' in response.text


@pytest.mark.parametrize("path, kind", [("/static/app.js", "javascript"), ("/static/style.css", "text/css")])
def test_positive_assets_served(client, path, kind):
    response = client.get(path)
    assert response.status_code == 200 and kind in response.headers["content-type"]


@pytest.mark.parametrize("path", ["/", "/static/app.js", "/healthz", "/readyz"])
def test_positive_security_headers_on_every_response(client, path):
    response = client.get(path)
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_positive_untrusted_text_written_with_text_apis():
    assert "textContent" in APP_JS and "createTextNode" in APP_JS


def test_positive_competing_sources_notice_shown_as_text():
    # REQ-054 / ADR-019: the notice names file names, which are untrusted.
    assert 'id="notice"' in INDEX
    assert '$("notice").textContent = data.message' in APP_JS_CODE


# --- Negative ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "sink",
    [
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        'setTimeout("',
        "DOMParser",
    ],
)
def test_negative_no_html_sinks_or_code_evaluation(sink):
    assert sink not in APP_JS_CODE


def test_edge_comment_stripping_does_not_hide_code():
    assert "textContent" in APP_JS_CODE and "fetch(" in APP_JS_CODE


def test_negative_csp_forbids_inline_and_third_party_script():
    csp = SECURITY_HEADERS["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp


def test_negative_no_inline_script_or_style_in_page():
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", INDEX)
    assert "<style" not in INDEX and " style=" not in INDEX and "onclick" not in INDEX.lower()


def test_negative_no_external_resources():
    # REQ-012: the page must work offline; everything is served by the app itself.
    for text in (INDEX, APP_JS, (STATIC / "style.css").read_text(encoding="utf-8")):
        assert "http://" not in text and "https://" not in text


def test_negative_api_docs_disabled(client):
    # FastAPI's docs pages load scripts from a CDN (offline + CSP conflict).
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


# --- Edge --------------------------------------------------------------------------


def test_edge_path_traversal_under_static_refused(client):
    assert client.get("/static/../config.py").status_code == 404
    assert client.get("/static/%2e%2e/config.py").status_code == 404


def test_edge_stop_button_aborts_request():
    assert "AbortController" in APP_JS and "controller.abort()" in APP_JS


def test_edge_answer_box_preserves_line_breaks_without_markup():
    assert "white-space: pre-wrap" in (STATIC / "style.css").read_text(encoding="utf-8")


# --- Host header: DNS rebinding (TS-015) ---------------------------------------------


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "localhost:8000", "127.0.0.1:8000"])
def test_positive_local_host_names_served(client, host):
    assert client.get("/healthz", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("path", ["/", "/healthz", "/static/app.js"])
def test_negative_foreign_host_refused(client, path):
    # A rebinding attack reaches 127.0.0.1 under the attacker's own domain name.
    assert client.get(path, headers={"Host": "attacker.example"}).status_code == 400


def test_negative_foreign_host_cannot_ask_questions(client):
    response = client.post("/chat", json={"question": "annual leave"}, headers={"Host": "attacker.example"})
    assert response.status_code == 400 and "event:" not in response.text


@pytest.mark.parametrize("host", ["localhost.attacker.example", "127.0.0.1.nip.io", "0.0.0.0", ""])
def test_edge_look_alike_and_empty_hosts_refused(client, host):
    assert client.get("/healthz", headers={"Host": host}).status_code == 400


def test_edge_allowed_hosts_are_exactly_the_loopback_names():
    assert ALLOWED_HOSTS == ["localhost", "127.0.0.1"]
