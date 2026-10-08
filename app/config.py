# Configuration boundary: the only place the application reads environment variables.
#
# Every other module receives a Settings object, so configuration is explicit and
# testable (REQ-013). Validation runs once at startup and reports every problem in a
# single error, so a misconfigured container fails immediately instead of at the
# first chat request (REQ-016). See ADR-011.
#
# Settings are added here only when a feature needs them; .env.example must list
# exactly the same variables (enforced by tests/test_config.py).

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

# Defaults for optional settings. Each is documented in .env.example and README.md.
# Loopback by default so a local run is not exposed to the network; the container
# sets APP_HOST=0.0.0.0 explicitly because it must accept traffic from the host.
DEFAULT_APP_HOST = "127.0.0.1"
DEFAULT_APP_PORT = 8000
# Readiness probes must answer quickly; a slow model endpoint should read as
# "not ready" rather than hang the health check.
DEFAULT_LLM_HEALTH_TIMEOUT_SECONDS = 3.0


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        lines = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"Invalid configuration ({len(problems)} problem(s)):\n{lines}")


@dataclass(frozen=True)
class Settings:
    # Base URL of any OpenAI-compatible endpoint, e.g. http://host.docker.internal:11434/v1.
    # The inference client appends /chat/completions, so no provider path is hard-coded (REQ-015, REQ-022).
    llm_url: str
    # Model identifier passed verbatim to the endpoint, e.g. qwen2.5:0.5b.
    llm_model: str
    # Interface and port the HTTP server binds to.
    app_host: str = DEFAULT_APP_HOST
    app_port: int = DEFAULT_APP_PORT
    # Upper bound on the readiness probe to the model endpoint (REQ-017).
    llm_health_timeout_seconds: float = DEFAULT_LLM_HEALTH_TIMEOUT_SECONDS


def _require(environ: Mapping[str, str], name: str, problems: list[str]) -> str:
    # Whitespace-only values are treated as missing: they are almost always a
    # copy/paste mistake in an env file, never an intended setting.
    value = environ.get(name, "").strip()
    if not value:
        problems.append(f"{name} is required but is missing or empty")
    return value


def _validate_url(name: str, value: str, problems: list[str]) -> str:
    # The value itself is deliberately not echoed in the error: a URL may carry
    # credentials, and errors end up in logs (REQ-068).
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        problems.append(f"{name} must be an http:// or https:// URL with a host")
        return value
    # urlsplit does not validate the port until .port is read; reading it here
    # turns a bad port into a startup error instead of a failure on the first request.
    try:
        parts.port
    except ValueError:
        problems.append(f"{name} has an invalid port (must be a number from 0 to 65535)")
        return value
    # The client builds request URLs by appending a path, which would land after a
    # query string or fragment and silently produce a wrong URL.
    if parts.query or parts.fragment:
        problems.append(f"{name} must be a base URL without a query string or fragment")
        return value
    # Normalise so the client can always join with "/chat/completions".
    return value.rstrip("/")


def _optional(environ: Mapping[str, str], name: str) -> str | None:
    # Unset and blank are both "use the default"; blank is common when a variable
    # is listed in an env file with no value.
    value = environ.get(name, "").strip()
    return value or None


def _parse_port(name: str, raw: str, problems: list[str]) -> int:
    # Port 0 would ask the OS for a random port, which makes the service unreachable
    # at a documented address, so it is rejected for the server port.
    if raw.isdigit() and 1 <= int(raw) <= 65535:
        return int(raw)
    problems.append(f"{name} must be a whole number from 1 to 65535")
    return DEFAULT_APP_PORT


def _parse_positive_seconds(name: str, raw: str, default: float, problems: list[str]) -> float:
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    # float() accepts "nan" and "inf"; neither is a usable timeout.
    if math.isfinite(value) and value > 0:
        return value
    problems.append(f"{name} must be a positive number of seconds")
    return default


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Build Settings from an environment mapping, or raise ConfigError listing all problems.

    Takes the mapping as a parameter (rather than reading os.environ directly) so
    tests can supply exact inputs without mutating process state.
    """
    problems: list[str] = []

    llm_url = _require(environ, "LLM_URL", problems)
    if llm_url:
        llm_url = _validate_url("LLM_URL", llm_url, problems)
    llm_model = _require(environ, "LLM_MODEL", problems)

    app_host = _optional(environ, "APP_HOST") or DEFAULT_APP_HOST

    raw_port = _optional(environ, "APP_PORT")
    app_port = _parse_port("APP_PORT", raw_port, problems) if raw_port else DEFAULT_APP_PORT

    raw_timeout = _optional(environ, "LLM_HEALTH_TIMEOUT_SECONDS")
    health_timeout = (
        _parse_positive_seconds(
            "LLM_HEALTH_TIMEOUT_SECONDS", raw_timeout, DEFAULT_LLM_HEALTH_TIMEOUT_SECONDS, problems
        )
        if raw_timeout
        else DEFAULT_LLM_HEALTH_TIMEOUT_SECONDS
    )

    if problems:
        raise ConfigError(problems)
    return Settings(
        llm_url=llm_url,
        llm_model=llm_model,
        app_host=app_host,
        app_port=app_port,
        llm_health_timeout_seconds=health_timeout,
    )


def load_settings_from_process_env() -> Settings:
    """The single place the real process environment is read (used by the entry point)."""
    return load_settings(os.environ)
