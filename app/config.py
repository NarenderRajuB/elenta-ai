# Configuration boundary: the only place the application reads environment variables.
#
# Every other module receives a Settings object, so configuration is explicit and
# testable (REQ-013). Validation runs once at startup and reports every problem in a
# single error, so a misconfigured container fails immediately instead of at the
# first chat request (REQ-016). See ADR-011.
#
# Settings are added here only when a feature needs them; .env.example must list
# exactly the same variables (enforced by tests/test_config.py).

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit


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

    if problems:
        raise ConfigError(problems)
    return Settings(llm_url=llm_url, llm_model=llm_model)
