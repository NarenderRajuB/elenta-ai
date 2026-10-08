# Process entry point: `python -m app`.
#
# Loads configuration first and exits with a clear message and non-zero status if it
# is invalid (REQ-016), before any server or socket is created. Only then is the app
# built and served.

import logging
import os
import sys

import uvicorn

from app.config import ConfigError, load_settings_from_process_env
from app.main import create_app
from app.observability import configure_logging
from app.prompt import fixed_overhead_tokens, prompt_limit

# Distinct from 1 (generic crash) so scripts and logs can tell "bad config" apart.
EXIT_CONFIG_ERROR = 2

# Room guaranteed for the question at the full evidence budget (~400 characters).
# Longer questions still work whenever less evidence is selected; otherwise the
# request is rejected as too long instead of overflowing.
MIN_QUESTION_TOKENS = 100


def main() -> int:
    try:
        settings = load_settings_from_process_env()
    except ConfigError as exc:
        print(f"elenta: startup aborted.\n{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    # A missing corpus root at startup is a deployment mistake (e.g. the ./data mount
    # is absent), so it fails fast. If it disappears later, refresh reports it and the
    # service keeps running with an empty corpus.
    if not os.path.isdir(settings.corpus_dir):
        print(
            f"elenta: startup aborted.\nCORPUS_DIR is not an existing directory: {settings.corpus_dir}", file=sys.stderr
        )
        return EXIT_CONFIG_ERROR

    # The worst case must fit the model's context: instructions + a full evidence budget
    # + the answer allowance, plus at least MIN_QUESTION_TOKENS for the question itself.
    # Checked once at startup so a misconfigured budget fails fast, not per request.
    worst_case = fixed_overhead_tokens() + settings.context_token_budget + MIN_QUESTION_TOKENS
    available = prompt_limit(settings.llm_context_tokens, settings.llm_max_tokens)
    if worst_case > available:
        print(
            "elenta: startup aborted.\n"
            f"Context budget does not fit: instructions (~{fixed_overhead_tokens()}) + CONTEXT_TOKEN_BUDGET "
            f"({settings.context_token_budget}) + question allowance ({MIN_QUESTION_TOKENS}) = ~{worst_case} tokens, "
            f"but LLM_CONTEXT_TOKENS ({settings.llm_context_tokens}) - LLM_MAX_TOKENS ({settings.llm_max_tokens}) "
            f"leaves {available}. Lower CONTEXT_TOKEN_BUDGET or LLM_MAX_TOKENS.",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR

    # Structured JSON logs on stderr for every logger, including uvicorn's (ADR-008).
    configure_logging(logging.INFO)

    # log_config=None: keep uvicorn from installing its own text handlers, so its
    # records go through the JSON handler above.
    uvicorn.run(create_app(settings), host=settings.app_host, port=settings.app_port, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
