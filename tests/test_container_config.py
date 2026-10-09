# Static checks of the container definition: compose.yaml, Dockerfile, .dockerignore.
#
# Covers REQ-011 (compose brings up the app), REQ-040/REQ-067 (read-only /data mount),
# REQ-066 (non-root), REQ-012 (pinned, reproducible build), REQ-013/REQ-014 (every
# .env.example setting reaches the container) and ADR-012/ADR-015 choices.
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


def _compose_config(env_file: str | None = None, **env_overrides: str) -> dict:
    # env_file replaces the project's .env (Compose reads it for ${...} substitution).
    result = subprocess.run(
        ["docker", "compose", *(["--env-file", env_file] if env_file else []), "config", "--format", "json"],
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


# --- Every .env.example setting reaches the container (REQ-013, REQ-014) -----------

# Set in compose.yaml itself, not from .env: they must match the container's port,
# mount and Compose network (see the comments there).
FIXED_IN_COMPOSE = {
    "APP_HOST": "0.0.0.0",
    "APP_PORT": "8000",
    "CORPUS_DIR": "/data",
    "OTLP_TRACES_URL": "http://jaeger:4318/v1/traces",
}


def _env_example_names() -> list[str]:
    # COMPOSE_PROFILES is read by Compose itself, not passed to the app (ADR-020).
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    names = [line.split("=", 1)[0] for line in lines if line and not line.startswith("#") and "=" in line]
    return [n for n in names if n != "COMPOSE_PROFILES"]


def test_positive_every_env_example_setting_is_passed_from_env():
    # A distinct value per variable proves each one is wired to its own name.
    passed = [n for n in _env_example_names() if n not in FIXED_IN_COMPOSE]
    environment = _compose_config(**{name: f"value-of-{name}" for name in passed})["environment"]
    for name in passed:
        assert environment.get(name) == f"value-of-{name}", f"{name} in .env would be ignored by Compose"


def test_negative_fixed_settings_cannot_be_overridden_from_env():
    overrides = dict.fromkeys(FIXED_IN_COMPOSE, "from-env")
    environment = _compose_config(LLM_URL="http://h/v1", LLM_MODEL="m", **overrides)["environment"]
    for name, value in FIXED_IN_COMPOSE.items():
        assert environment[name] == value


def test_negative_compose_passes_nothing_that_env_example_does_not_document(app_service):
    assert set(app_service["environment"]) == set(_env_example_names())


def test_edge_unset_optional_settings_render_empty_so_app_defaults_apply(tmp_path):
    # Without a .env, optional settings must render as "" (the app treats blank as
    # "use the default"), not be dropped or given a value Compose invented.
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    optional = [n for n in _env_example_names() if n not in FIXED_IN_COMPOSE and n not in ("LLM_URL", "LLM_MODEL")]
    environment = _compose_config(
        env_file=str(empty), LLM_URL="http://h/v1", LLM_MODEL="m", **dict.fromkeys(optional, "")
    )["environment"]
    for name in optional:
        assert environment[name] == "", name


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


# --- Model server in Compose for Intel Macs (ADR-020, REQ-011) -------------------------


def _all_services(**env: str) -> dict:
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        env={**os.environ, "LLM_URL": "http://h/v1", "LLM_MODEL": "m", **env},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["services"]


@pytest.fixture(scope="module")
def ollama_service() -> dict:
    return _all_services(COMPOSE_PROFILES="ollama")["ollama"]


def test_positive_ollama_profile_adds_the_model_server():
    assert sorted(_all_services(COMPOSE_PROFILES="ollama")) == ["app", "jaeger", "ollama"]


def test_positive_app_waits_for_a_healthy_model_server_when_enabled():
    ollama = _all_services(COMPOSE_PROFILES="ollama")["app"]["depends_on"]["ollama"]
    assert ollama["condition"] == "service_healthy" and ollama["required"] is False


def test_positive_ollama_pinned_offline_and_context_matches_the_app(ollama_service):
    from app.config import DEFAULT_LLM_CONTEXT_TOKENS

    assert ollama_service["image"].startswith("ollama/ollama:0.40.1@sha256:")
    assert ollama_service["environment"]["OLLAMA_NO_CLOUD"] == "1"
    assert int(ollama_service["environment"]["OLLAMA_CONTEXT_LENGTH"]) == DEFAULT_LLM_CONTEXT_TOKENS
    assert ollama_service["healthcheck"]["test"] == ["CMD", "ollama", "list"]


def test_negative_no_profile_no_model_server():
    # Docker Model Runner or native Ollama: Compose must not start a second server.
    services = _all_services(COMPOSE_PROFILES="")
    assert "ollama" not in services and sorted(services) == ["app", "jaeger"]


def test_negative_ollama_not_published_and_hardened(ollama_service):
    assert not ollama_service.get("ports")  # reachable only on the Compose network
    assert ollama_service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in ollama_service["security_opt"]


def test_edge_models_kept_in_a_named_volume(ollama_service):
    mounts = ollama_service["volumes"]
    assert [(m["type"], m["source"], m["target"]) for m in mounts] == [("volume", "ollama-models", "/root/.ollama")]
