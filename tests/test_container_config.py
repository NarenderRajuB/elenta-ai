# Static checks of the container definition: compose.yaml, Dockerfile, .dockerignore.
#
# Covers REQ-011 (compose brings up the app), REQ-040/REQ-067 (read-only /data mount),
# REQ-066 (non-root), REQ-012 (pinned, reproducible build) and ADR-012/ADR-015 choices.
# Uses `docker compose config` so the file is checked exactly as Compose interprets it.
# Fast (no build, no containers); skipped if the docker CLI is unavailable.

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")


def _compose_config(**env_overrides: str) -> dict:
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        env={**os.environ, **env_overrides},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["services"]["app"]


@pytest.fixture(scope="module")
def app_service() -> dict:
    return _compose_config(LLM_URL="http://host.docker.internal:11434/v1", LLM_MODEL="gemma3:1b")


# --- Positive --------------------------------------------------------------------


def test_positive_corpus_mounted_read_only_at_data(app_service):
    mounts = [v for v in app_service["volumes"] if v["target"] == "/data"]
    assert len(mounts) == 1
    assert mounts[0]["type"] == "bind"
    assert Path(mounts[0]["source"]) == ROOT / "data"
    assert mounts[0]["read_only"] is True


def test_positive_app_listens_on_all_interfaces_inside_container(app_service):
    assert app_service["environment"]["APP_HOST"] == "0.0.0.0"
    assert app_service["environment"]["APP_PORT"] == "8000"


def test_positive_required_config_passed_through(app_service):
    assert app_service["environment"]["LLM_URL"] == "http://host.docker.internal:11434/v1"
    assert app_service["environment"]["LLM_MODEL"] == "gemma3:1b"


def test_positive_hardening_flags(app_service):
    assert app_service["read_only"] is True
    assert app_service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in app_service["security_opt"]


def test_positive_dockerfile_runs_as_fixed_non_root_user():
    lines = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    user_lines = [line.split()[1] for line in lines if line.startswith("USER ")]
    # The last USER wins; it must be numeric and non-zero.
    uid = user_lines[-1].split(":")[0]
    assert uid.isdigit() and int(uid) != 0


def test_positive_dockerfile_builds_from_lockfile_without_dev_tools():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "uv sync --frozen --no-dev" in text


# --- Negative --------------------------------------------------------------------


def test_negative_port_not_published_on_all_host_interfaces(app_service):
    # Must not be reachable from the LAN: every published port binds host loopback.
    assert app_service["ports"], "app port must be published"
    for port in app_service["ports"]:
        assert port.get("host_ip") == "127.0.0.1"


def test_negative_healthcheck_does_not_depend_on_model(app_service):
    # ADR-015: container health is liveness; a model outage must not mark it unhealthy.
    test_cmd = " ".join(app_service["healthcheck"]["test"])
    assert "/healthz" in test_cmd and "/readyz" not in test_cmd


@pytest.mark.parametrize("entry", [".env", "data/", ".git/", "tests/"])
def test_negative_secrets_corpus_and_tests_excluded_from_build_context(entry):
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert entry in ignored


def test_negative_base_image_is_pinned_by_digest():
    # A floating tag would make "after the pull, it works offline" unrepeatable.
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "python:3.12-slim@sha256:" in text


# --- Edge ------------------------------------------------------------------------


def test_edge_compose_renders_without_env_file_values():
    # No .env values: Compose must still render (empty strings), so the app itself
    # reports the missing configuration with its clear error (REQ-016).
    service = _compose_config(LLM_URL="", LLM_MODEL="")
    assert service["environment"]["LLM_URL"] == ""
    assert service["environment"]["LLM_MODEL"] == ""


def test_edge_host_gateway_alias_for_native_linux(app_service):
    # Compose v5 renders extra_hosts as "name=target" strings.
    assert app_service["extra_hosts"] == ["host.docker.internal=host-gateway"]


def test_edge_tmpfs_is_the_only_writable_path(app_service):
    assert app_service["tmpfs"] == ["/tmp"]


# --- Trace viewer (ADR-008, REQ-085, REQ-012) ----------------------------------------


@pytest.fixture(scope="module")
def jaeger_service() -> dict:
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        env={**os.environ, "LLM_URL": "http://h/v1", "LLM_MODEL": "m"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(result.stdout)["services"]["jaeger"]


def test_positive_app_exports_traces_to_local_jaeger(app_service):
    assert app_service["environment"]["OTLP_TRACES_URL"] == "http://jaeger:4318/v1/traces"
    assert "jaeger" in app_service["depends_on"]


def test_positive_jaeger_image_pinned_by_digest(jaeger_service):
    assert jaeger_service["image"].startswith("jaegertracing/jaeger:2.22.0@sha256:")


def test_negative_jaeger_ui_only_on_loopback_and_otlp_not_published(jaeger_service):
    published = {(p.get("host_ip"), int(p["target"])) for p in jaeger_service["ports"]}
    assert published == {("127.0.0.1", 16686)}


def test_edge_jaeger_hardening(jaeger_service):
    assert jaeger_service["cap_drop"] == ["ALL"] and "no-new-privileges:true" in jaeger_service["security_opt"]
