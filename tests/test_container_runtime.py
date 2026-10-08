# Runtime checks of the real container, started through Compose (REQ-011, REQ-012,
# REQ-016, REQ-017, REQ-040, REQ-066, REQ-067; ADR-012).
#
# Slow (builds the image, starts containers), so excluded from the default run.
# Run with:  uv run pytest -m container
#
# Uses `docker compose run` under a separate project name with no published ports,
# so it never collides with a stack the developer already has running on port 8000.
# Checks are made from inside the container with Python's urllib (the image has no curl).
# The real host Ollama is never stopped; "model down" is simulated with a dead port.

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "elenta-pytest"
VALID_ENV = {"LLM_URL": "http://host.docker.internal:11434/v1", "LLM_MODEL": "qwen2.5:0.5b"}

pytestmark = [
    pytest.mark.container,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed"),
]

# Prints the HTTP status and body of a URL from inside the container.
PROBE = (
    "import sys, urllib.request, urllib.error\n"
    "try:\n"
    "    r = urllib.request.urlopen(sys.argv[1], timeout=5); print(r.status, r.read().decode())\n"
    "except urllib.error.HTTPError as e:\n"
    "    print(e.code, e.read().decode())\n"
)


def _docker(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def _compose(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return _docker("compose", "-p", PROJECT, *args, timeout=timeout)


@pytest.fixture(scope="session", autouse=True)
def built_image():
    if _docker("info").returncode != 0:
        pytest.skip("docker daemon not running")
    result = _compose("build", timeout=600)
    assert result.returncode == 0, result.stderr
    yield
    _compose("down", "--remove-orphans")


def _start(env: dict[str, str]) -> str:
    """Start the app service detached via compose run; return the container name."""
    name = f"{PROJECT}-{uuid.uuid4().hex[:8]}"
    env_args = [arg for k, v in env.items() for arg in ("-e", f"{k}={v}")]
    result = _compose("run", "-d", "--name", name, *env_args, "app")
    assert result.returncode == 0, result.stderr
    return name


def _exec(name: str, *cmd: str) -> subprocess.CompletedProcess:
    return _docker("exec", name, *cmd, timeout=30)


def _get(name: str, path: str) -> tuple[int, str]:
    out = _exec(name, "python", "-c", PROBE, f"http://127.0.0.1:8000{path}").stdout.strip()
    code, _, body = out.partition(" ")
    return int(code), body


def _wait_live(name: str, deadline_s: float = 30) -> None:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        result = _exec(name, "python", "-c", PROBE, "http://127.0.0.1:8000/healthz")
        if result.returncode == 0 and result.stdout.startswith("200"):
            return
        time.sleep(0.5)
    raise AssertionError(f"container {name} never became live:\n{_docker('logs', name).stdout}")


@pytest.fixture
def running():
    """Start a container with valid config; always removed afterwards."""
    names: list[str] = []

    def start(env: dict[str, str] | None = None) -> str:
        name = _start({**VALID_ENV, **(env or {})})
        names.append(name)
        _wait_live(name)
        return name

    yield start
    for name in names:
        _docker("rm", "-f", name)


def _host_ollama_has_model() -> bool:
    try:
        r = httpx.get("http://localhost:11434/v1/models", timeout=2, trust_env=False)
        return VALID_ENV["LLM_MODEL"] in {m["id"] for m in r.json()["data"]}
    except Exception:
        return False


# --- Positive --------------------------------------------------------------------

def test_positive_container_becomes_healthy(running):
    name = running()
    end = time.monotonic() + 40
    status = ""
    while time.monotonic() < end:
        status = _docker("inspect", "--format", "{{.State.Health.Status}}", name).stdout.strip()
        if status == "healthy":
            break
        time.sleep(1)
    assert status == "healthy"


def test_positive_runs_as_non_root_uid_10001(running):
    assert _exec(running(), "id", "-u").stdout.strip() == "10001"


@pytest.mark.skipif(not _host_ollama_has_model(), reason="host Ollama with qwen2.5:0.5b not available")
def test_positive_ready_against_host_ollama(running):
    # Confirms the ADR-013 route: container -> host.docker.internal -> native Ollama.
    code, body = _get(running(), "/readyz")
    assert code == 200 and '"ready"' in body


def test_positive_host_files_visible_under_data(running):
    name = running()
    probe = ROOT / "data" / f"_pytest_probe_{uuid.uuid4().hex[:6]}.txt"
    try:
        probe.write_text("visible", encoding="utf-8")
        assert _exec(name, "cat", f"/data/{probe.name}").stdout == "visible"
    finally:
        probe.unlink(missing_ok=True)


# --- Negative --------------------------------------------------------------------

def test_negative_missing_config_exits_2_with_clear_message():
    result = _compose("run", "--rm", "-e", "LLM_URL=", "-e", "LLM_MODEL=", "app")
    assert result.returncode == 2
    output = result.stdout + result.stderr
    assert "LLM_URL is required" in output and "LLM_MODEL is required" in output
    assert "Traceback" not in output


def test_negative_corpus_mount_is_read_only(running):
    result = _exec(running(), "touch", "/data/should_fail")
    assert result.returncode != 0 and "Read-only file system" in result.stderr


def test_negative_app_filesystem_is_read_only(running):
    result = _exec(running(), "touch", "/app/should_fail")
    assert result.returncode != 0 and "Read-only file system" in result.stderr


def test_negative_model_unreachable_reports_not_ready(running):
    code, body = _get(running({"LLM_URL": "http://host.docker.internal:1/v1"}), "/readyz")
    assert code == 503 and '"unreachable"' in body


# --- Edge ------------------------------------------------------------------------

def test_edge_tmp_is_writable(running):
    assert _exec(running(), "sh", "-c", "touch /tmp/ok && echo ok").stdout.strip() == "ok"


def test_edge_no_linux_capabilities(running):
    out = _exec(running(), "grep", "CapEff", "/proc/self/status").stdout
    assert out.split()[-1] == "0000000000000000"


def test_edge_dev_tools_not_in_image(running):
    code = "import importlib.util as u; print([m for m in ('pytest', 'httpx2') if u.find_spec(m)])"
    assert _exec(running(), "python", "-c", code).stdout.strip() == "[]"


def test_edge_starts_with_no_network_at_all():
    # REQ-012: nothing at startup needs the internet. --network none removes even
    # the host route, so readiness must report the model unreachable, not hang.
    name = f"{PROJECT}-offline-{uuid.uuid4().hex[:6]}"
    env_args = [arg for k, v in VALID_ENV.items() for arg in ("-e", f"{k}={v}")]
    result = _docker("run", "-d", "--name", name, "--network", "none", "--read-only",
                     *env_args, "-e", "APP_HOST=127.0.0.1", "elenta-ai:local")
    assert result.returncode == 0, result.stderr
    try:
        _wait_live(name)
        code, body = _get(name, "/readyz")
        assert code == 503 and '"unreachable"' in body
    finally:
        _docker("rm", "-f", name)
