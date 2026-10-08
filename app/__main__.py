# Process entry point: `python -m app`.
#
# Loads configuration first and exits with a clear message and non-zero status if it
# is invalid (REQ-016), before any server or socket is created. Only then is the app
# built and served.

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

    uvicorn.run(create_app(settings), host=settings.app_host, port=settings.app_port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
