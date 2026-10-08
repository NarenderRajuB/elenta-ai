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
# Corpus defaults (ADR-005). /data is where the brief mounts the corpus. The size and
# count limits were chosen by the candidate; files beyond them are skipped and reported.
DEFAULT_CORPUS_DIR = "/data"
DEFAULT_CORPUS_MAX_FILE_BYTES = 50 * 1024 * 1024
DEFAULT_CORPUS_MAX_FILES = 500
# A file modified more recently than this may still be being written; it is picked up
# on a later request instead of risking a half-written read (REQ-045).
DEFAULT_CORPUS_SETTLE_SECONDS = 0.5
# Evidence selection defaults (ADR-007), chosen by the candidate. A chunk of about 800
# characters is roughly 200 estimated tokens; a 1500-token evidence budget fits
# comfortably in the 4096-token context Ollama is pinned to (TS-005), leaving room for
# instructions, the question and the answer.
DEFAULT_CHUNK_MAX_CHARS = 800
DEFAULT_CONTEXT_TOKEN_BUDGET = 1500
# BM25 score a chunk must exceed to count as evidence. 0 means "shares at least one
# meaningful word with the question"; raise it to demand stronger matches.
DEFAULT_SELECTION_MIN_SCORE = 0.0
# Model context window (must match the server: Ollama is pinned to 4096, TS-005) and the
# answer allowance sent as max_tokens. The prompt may use context minus answer allowance;
# anything larger is rejected rather than silently truncated by the server (REQ-055).
DEFAULT_LLM_CONTEXT_TOKENS = 4096
DEFAULT_LLM_MAX_TOKENS = 512
# Sampling temperature sent with every chat request. 0 makes answers as repeatable as
# the server allows, so behaviour can be tested and demonstrated (TS-006). The upper
# bound 2 is the range defined by the OpenAI-compatible API.
DEFAULT_LLM_TEMPERATURE = 0.0
MAX_LLM_TEMPERATURE = 2.0


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
    # Model identifier passed verbatim to the endpoint, e.g. gemma3:1b.
    llm_model: str
    # Interface and port the HTTP server binds to.
    app_host: str = DEFAULT_APP_HOST
    app_port: int = DEFAULT_APP_PORT
    # Upper bound on the readiness probe to the model endpoint (REQ-017).
    llm_health_timeout_seconds: float = DEFAULT_LLM_HEALTH_TIMEOUT_SECONDS
    # Root of the document corpus; nothing outside it is ever read (REQ-064).
    corpus_dir: str = DEFAULT_CORPUS_DIR
    corpus_max_file_bytes: int = DEFAULT_CORPUS_MAX_FILE_BYTES
    corpus_max_files: int = DEFAULT_CORPUS_MAX_FILES
    corpus_settle_seconds: float = DEFAULT_CORPUS_SETTLE_SECONDS
    # Evidence selection (REQ-050, REQ-055).
    chunk_max_chars: int = DEFAULT_CHUNK_MAX_CHARS
    context_token_budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET
    selection_min_score: float = DEFAULT_SELECTION_MIN_SCORE
    # Context accounting (REQ-055, REQ-071).
    llm_context_tokens: int = DEFAULT_LLM_CONTEXT_TOKENS
    llm_max_tokens: int = DEFAULT_LLM_MAX_TOKENS
    llm_temperature: float = DEFAULT_LLM_TEMPERATURE


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


def _parse_positive_int(name: str, raw: str, default: int, problems: list[str]) -> int:
    if raw.isdigit() and int(raw) >= 1:
        return int(raw)
    problems.append(f"{name} must be a whole number of at least 1")
    return default


def _parse_seconds(name: str, raw: str, default: float, problems: list[str], allow_zero: bool = False) -> float:
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    # float() accepts "nan" and "inf"; neither is a usable duration.
    if math.isfinite(value) and (value > 0 or (allow_zero and value == 0)):
        return value
    kind = "zero or a positive" if allow_zero else "a positive"
    problems.append(f"{name} must be {kind} number of seconds")
    return default


def _parse_non_negative_float(name: str, raw: str, default: float, problems: list[str]) -> float:
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if math.isfinite(value) and value >= 0:
        return value
    problems.append(f"{name} must be zero or a positive number")
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
        _parse_seconds("LLM_HEALTH_TIMEOUT_SECONDS", raw_timeout, DEFAULT_LLM_HEALTH_TIMEOUT_SECONDS, problems)
        if raw_timeout
        else DEFAULT_LLM_HEALTH_TIMEOUT_SECONDS
    )

    # A relative CORPUS_DIR (handy for local runs, e.g. ./data) is made absolute against
    # the working directory once, here, so the rest of the app sees one fixed root.
    # Existence is checked at startup by the entry point, not here, so that loading
    # settings never touches the filesystem.
    corpus_dir = os.path.abspath(_optional(environ, "CORPUS_DIR") or DEFAULT_CORPUS_DIR)

    raw_max_bytes = _optional(environ, "CORPUS_MAX_FILE_BYTES")
    max_file_bytes = (
        _parse_positive_int("CORPUS_MAX_FILE_BYTES", raw_max_bytes, DEFAULT_CORPUS_MAX_FILE_BYTES, problems)
        if raw_max_bytes
        else DEFAULT_CORPUS_MAX_FILE_BYTES
    )
    raw_max_files = _optional(environ, "CORPUS_MAX_FILES")
    max_files = (
        _parse_positive_int("CORPUS_MAX_FILES", raw_max_files, DEFAULT_CORPUS_MAX_FILES, problems)
        if raw_max_files
        else DEFAULT_CORPUS_MAX_FILES
    )
    raw_settle = _optional(environ, "CORPUS_SETTLE_SECONDS")
    settle = (
        _parse_seconds("CORPUS_SETTLE_SECONDS", raw_settle, DEFAULT_CORPUS_SETTLE_SECONDS, problems, allow_zero=True)
        if raw_settle
        else DEFAULT_CORPUS_SETTLE_SECONDS
    )

    def optional_int(name: str, default: int) -> int:
        raw = _optional(environ, name)
        return _parse_positive_int(name, raw, default, problems) if raw else default

    chunk_max_chars = optional_int("CHUNK_MAX_CHARS", DEFAULT_CHUNK_MAX_CHARS)
    context_token_budget = optional_int("CONTEXT_TOKEN_BUDGET", DEFAULT_CONTEXT_TOKEN_BUDGET)
    raw_min_score = _optional(environ, "SELECTION_MIN_SCORE")
    min_score = (
        _parse_non_negative_float("SELECTION_MIN_SCORE", raw_min_score, DEFAULT_SELECTION_MIN_SCORE, problems)
        if raw_min_score
        else DEFAULT_SELECTION_MIN_SCORE
    )

    llm_context_tokens = optional_int("LLM_CONTEXT_TOKENS", DEFAULT_LLM_CONTEXT_TOKENS)
    llm_max_tokens = optional_int("LLM_MAX_TOKENS", DEFAULT_LLM_MAX_TOKENS)
    raw_temperature = _optional(environ, "LLM_TEMPERATURE")
    llm_temperature = DEFAULT_LLM_TEMPERATURE
    if raw_temperature:
        llm_temperature = _parse_non_negative_float("LLM_TEMPERATURE", raw_temperature, DEFAULT_LLM_TEMPERATURE, problems)
        if llm_temperature > MAX_LLM_TEMPERATURE:
            problems.append(f"LLM_TEMPERATURE must be at most {MAX_LLM_TEMPERATURE:g}")
            llm_temperature = DEFAULT_LLM_TEMPERATURE
    # Cross-field rule checked here because both values are plain numbers. Whether the
    # fixed prompt text also fits is checked at startup (app/__main__.py), because that
    # depends on the prompt module, not on configuration alone.
    if llm_max_tokens >= llm_context_tokens:
        problems.append("LLM_MAX_TOKENS must be smaller than LLM_CONTEXT_TOKENS")
    if context_token_budget >= llm_context_tokens:
        problems.append("CONTEXT_TOKEN_BUDGET must be smaller than LLM_CONTEXT_TOKENS")

    if problems:
        raise ConfigError(problems)
    return Settings(
        llm_url=llm_url,
        llm_model=llm_model,
        app_host=app_host,
        app_port=app_port,
        llm_health_timeout_seconds=health_timeout,
        corpus_dir=corpus_dir,
        corpus_max_file_bytes=max_file_bytes,
        corpus_max_files=max_files,
        corpus_settle_seconds=settle,
        chunk_max_chars=chunk_max_chars,
        context_token_budget=context_token_budget,
        selection_min_score=min_score,
        llm_context_tokens=llm_context_tokens,
        llm_max_tokens=llm_max_tokens,
        llm_temperature=llm_temperature,
    )


def load_settings_from_process_env() -> Settings:
    """The single place the real process environment is read (used by the entry point)."""
    return load_settings(os.environ)
