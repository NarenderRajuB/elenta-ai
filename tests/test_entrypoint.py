# Tests for the process entry point (`python -m app`): REQ-016 startup behaviour and
# REQ-017 signals on a real running server.
#
# These start real subprocesses bound to 127.0.0.1 only. "Model down" is simulated
# by pointing LLM_URL at a local port with nothing listening, so no Ollama and no
# internet are needed.

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_app(env: dict[str, str]) -> subprocess.Popen:
    # Minimal explicit environment: proves the app needs nothing beyond what is passed.
    return subprocess.Popen(
        [sys.executable, "-m", "app"],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT), **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _exit_of(env: dict[str, str]) -> tuple[int, str]:
    proc = _run_app(env)
    _, stderr = proc.communicate(timeout=20)
    return proc.returncode, stderr


# --- Negative: bad configuration aborts startup ----------------------------------

def test_negative_missing_config_exits_with_code_2_and_clear_message():
    code, stderr = _exit_of({})
    assert code == 2
    assert "startup aborted" in stderr
    assert "LLM_URL is required" in stderr and "LLM_MODEL is required" in stderr
    # A clear message, not a Python stack trace.
    assert "Traceback" not in stderr


def test_negative_invalid_optional_setting_also_aborts():
    code, stderr = _exit_of({"LLM_URL": "http://127.0.0.1:1/v1", "LLM_MODEL": "m", "APP_PORT": "70000"})
    assert code == 2 and "APP_PORT" in stderr


def test_negative_missing_corpus_dir_aborts(tmp_path):
    missing = tmp_path / "no-such-dir"
    code, stderr = _exit_of({"LLM_URL": "http://127.0.0.1:1/v1", "LLM_MODEL": "m", "CORPUS_DIR": str(missing)})
    assert code == 2
    assert "CORPUS_DIR is not an existing directory" in stderr and "Traceback" not in stderr


def test_negative_corpus_dir_that_is_a_file_aborts(tmp_path):
    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")
    code, stderr = _exit_of({"LLM_URL": "http://127.0.0.1:1/v1", "LLM_MODEL": "m", "CORPUS_DIR": str(a_file)})
    assert code == 2 and "CORPUS_DIR" in stderr


def test_negative_budget_that_cannot_fit_context_aborts(tmp_path):
    # Each value is individually valid, but instructions + 3000 evidence + 100 question
    # cannot fit in 4096 - 1000: startup must refuse rather than overflow per request.
    code, stderr = _exit_of({
        "LLM_URL": "http://127.0.0.1:1/v1", "LLM_MODEL": "m", "CORPUS_DIR": str(tmp_path),
        "CONTEXT_TOKEN_BUDGET": "3000", "LLM_MAX_TOKENS": "1000",
    })
    assert code == 2
    assert "Context budget does not fit" in stderr and "Traceback" not in stderr


def test_edge_secret_in_bad_url_not_printed():
    code, stderr = _exit_of({"LLM_URL": "ftp://user:s3cret@host", "LLM_MODEL": "m"})
    assert code == 2 and "s3cret" not in stderr


# --- Positive: valid config starts a server with working health signals -----------

def test_positive_server_starts_live_and_reports_model_unreachable(tmp_path):
    app_port, dead_llm_port = _free_port(), _free_port()
    (tmp_path / "doc.txt").write_text("hello", encoding="utf-8")
    proc = _run_app({
        "CORPUS_DIR": str(tmp_path),
        "CORPUS_SETTLE_SECONDS": "0",
        "LLM_URL": f"http://127.0.0.1:{dead_llm_port}/v1",
        "LLM_MODEL": "qwen2.5:0.5b",
        "APP_PORT": str(app_port),
        "LLM_HEALTH_TIMEOUT_SECONDS": "1",
    })
    try:
        base = f"http://127.0.0.1:{app_port}"
        deadline = time.monotonic() + 15
        while True:
            try:
                live = httpx.get(f"{base}/healthz", timeout=1)
                break
            except httpx.TransportError:
                assert proc.poll() is None, proc.stderr.read()
                assert time.monotonic() < deadline, "server did not start in time"
                time.sleep(0.1)
        assert live.status_code == 200

        ready = httpx.get(f"{base}/readyz", timeout=5)
        assert ready.status_code == 503
        assert ready.json()["checks"]["llm"]["reason"] == "unreachable"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    # Startup refresh is visible in the logs: counts and names only, never content.
    logs = proc.stderr.read()
    assert "corpus refreshed: version=1 documents=1 added=1" in logs
    assert "hello" not in logs
