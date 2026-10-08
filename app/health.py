# Readiness probe for the model endpoint (REQ-017).
#
# Ollama runs natively on the host (ADR-013), so Compose cannot start or health-check
# it; this probe is the only place a stopped or misconfigured model server becomes
# visible. It uses only the standard OpenAI-compatible GET {LLM_URL}/models call, so
# it works for any compatible endpoint (REQ-022).
#
# Failure reasons are short fixed codes. Exception text and the URL are never returned,
# because the URL may carry credentials and the response is visible to any caller (REQ-068).

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class LlmCheck:
    ok: bool
    # None when ok; otherwise one of: timeout, unreachable, http_error,
    # invalid_response, model_not_found.
    reason: str | None = None


async def check_llm(client: httpx.AsyncClient, llm_url: str, llm_model: str, timeout: float) -> LlmCheck:
    """Report whether the endpoint answers and lists the configured model."""
    try:
        response = await client.get(f"{llm_url}/models", timeout=timeout)
    except httpx.TimeoutException:
        return LlmCheck(ok=False, reason="timeout")
    except httpx.HTTPError:
        # Connection refused, DNS failure, protocol error: all mean "cannot talk to it".
        return LlmCheck(ok=False, reason="unreachable")

    if response.status_code != 200:
        return LlmCheck(ok=False, reason="http_error")

    # The OpenAI list format is {"data": [{"id": "..."}, ...]}. Anything else is
    # reported rather than guessed at, so a non-compatible endpoint is caught here.
    try:
        body = response.json()
        model_ids = {item["id"] for item in body["data"]}
    except (ValueError, KeyError, TypeError):
        return LlmCheck(ok=False, reason="invalid_response")

    # Exact match: the same string is sent verbatim on chat requests, so a near
    # match here would still fail later.
    if llm_model not in model_ids:
        return LlmCheck(ok=False, reason="model_not_found")
    return LlmCheck(ok=True)
