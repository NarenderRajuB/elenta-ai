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

# Distinct from 1 (generic crash) so scripts and logs can tell "bad config" apart.
EXIT_CONFIG_ERROR = 2


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
        print(f"elenta: startup aborted.\nCORPUS_DIR is not an existing directory: {settings.corpus_dir}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    # Plain-text logs to stderr for now; structured JSON logging arrives with the
    # observability feature (REQ-082).
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    uvicorn.run(create_app(settings), host=settings.app_host, port=settings.app_port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
