# Configuration

Every environment variable the service reads, whether it is required, a safe example value, what it changes and what values are accepted (REQ-105). The reference file is [`.env.example`](../.env.example): a test checks it lists exactly the app's settings plus `COMPOSE_PROFILES` (which Compose reads itself), and another that Compose passes every app setting to the container (TS-011).

## How configuration is loaded

- **Environment variables only** (REQ-013). `app/config.py` is the only module that reads them; there are no configuration files and no command-line options.
- **With Docker Compose**, copy `.env.example` to `.env` and edit it. Compose reads `.env` and passes the values to the app container. Changes take effect on `docker compose up -d` (the container is recreated); no rebuild is needed.
- **Blank means default.** An optional variable that is unset or empty uses the default below.
- **Fail fast** (REQ-016). Every value is checked once at startup. If anything is missing or invalid, the app prints **one** error listing every problem and exits with code **2** before opening a port. The error never repeats a URL value, because a URL could contain a password.
- `.env` is git-ignored and excluded from the image build. The example values contain no secrets.

```text
$ uv run python -m app
elenta: startup aborted.
Invalid configuration (2 problem(s)):
  - LLM_URL is required but is missing or empty
  - LLM_MODEL is required but is missing or empty
```

## Model server choice (read by Docker Compose)

| Variable | Required | Example (safe) | Default | Accepted values | Effect |
|---|---|---|---|---|---|
| `COMPOSE_PROFILES` | No | `ollama` | *(empty)* | `ollama` or empty | Read by **Docker Compose**, not by the app. `ollama` makes `docker compose up` also start the Ollama model server (the Intel Mac setup, ADR-020); then `LLM_URL` must be `http://ollama:11434/v1`. Leave it out when using Docker Model Runner or a natively installed Ollama |

The three setups, all chosen in `.env` only:

| Setup | `COMPOSE_PROFILES` | `LLM_URL` |
|---|---|---|
| A. Ollama in Compose (Intel Mac; the default in `.env.example`) | `ollama` | `http://ollama:11434/v1` |
| B. Docker Model Runner | *(not set)* | `http://model-runner.docker.internal/engines/v1` |
| C. Ollama installed on the host | *(not set)* | `http://host.docker.internal:11434/v1` |

## Model endpoint

| Variable | Required | Example (safe) | Default | Accepted values | Effect |
|---|---|---|---|---|---|
| `LLM_URL` | **Yes** | `http://ollama:11434/v1` | — | `http`/`https` URL with a host; valid port if given; no query string or fragment. A trailing `/` is removed | Base URL of any OpenAI-compatible endpoint. The app calls `{LLM_URL}/chat/completions` and, for readiness, `{LLM_URL}/models`. Choosing this URL is how the backend is chosen (Ollama, Docker Model Runner, other); the trace labels it `llm.backend` |
| `LLM_MODEL` | **Yes** | `gemma3:1b` | — | Any non-blank text | Model name sent to the endpoint unchanged. `/readyz` checks the endpoint lists it exactly |
| `LLM_TEMPERATURE` | No | `0` | `0` | 0 to 2 | Sampling temperature. `0` makes answers as repeatable as the server allows; the injection evaluation was run at 0 (TS-006) |
| `LLM_CONTEXT_TOKENS` | No | `4096` | `4096` | Whole number ≥ 1 | The model's context window. **Must match the server** (Ollama is pinned to 4096, TS-005); the app can't read it from the server through the standard API |
| `LLM_MAX_TOKENS` | No | `512` | `512` | Whole number ≥ 1, smaller than `LLM_CONTEXT_TOKENS` | Longest answer, sent as `max_tokens`. The prompt may use the rest of the context window |
| `LLM_HEALTH_TIMEOUT_SECONDS` | No | `3` | `3` | Positive number | How long `/readyz` waits for the model server |
| `LLM_CONNECT_TIMEOUT_SECONDS` | No | `5` | `5` | Positive number | How long to wait to connect to the model server. Exceeded: `model_timeout`; connection refused: `model_unavailable` |
| `LLM_READ_TIMEOUT_SECONDS` | No | `60` | `60` | Positive number | Longest gap between streamed pieces, **including the wait for the first one**, which covers prompt processing on CPU. Exceeded: `model_timeout` |
| `LLM_REQUEST_TIMEOUT_SECONDS` | No | `180` | `180` | Positive number | Limit on one whole model call, checked after each streamed piece. A call can therefore run at most this plus one read timeout (240 s with the defaults) |

## Corpus

| Variable | Required | Example (safe) | Default | Accepted values | Effect |
|---|---|---|---|---|---|
| `CORPUS_DIR` | No | `/data` | `/data` | A path; relative paths are made absolute against the working directory | Corpus root; nothing outside it is read (REQ-064). Must exist at startup, or the app exits with code 2. **Fixed to `/data` under Compose** |
| `CORPUS_MAX_FILE_BYTES` | No | `52428800` | `52428800` (50 MB) | Whole number ≥ 1 | Larger files are skipped without being read and reported as `too_large` |
| `CORPUS_MAX_FILES` | No | `500` | `500` | Whole number ≥ 1 | Supported files beyond this number, in sorted path order, are skipped as `file_limit_exceeded`. Hidden and unsupported files don't count |
| `CORPUS_SETTLE_SECONDS` | No | `0.5` | `0.5` | Zero or a positive number | A file modified more recently than this is treated as still being written and served on a later question. `0` turns the wait off (a file is then only protected by the changed-during-read check) |

## Evidence selection

| Variable | Required | Example (safe) | Default | Accepted values | Effect |
|---|---|---|---|---|---|
| `CHUNK_MAX_CHARS` | No | `800` | `800` (about 200 estimated tokens) | Whole number ≥ 1 | Target size of an evidence chunk. Smaller chunks give more precise sources and fit more files into the budget; larger chunks keep more context together |
| `CONTEXT_TOKEN_BUDGET` | No | `1500` | `1500` | Whole number ≥ 1, smaller than `LLM_CONTEXT_TOKENS`, and must pass the startup fit check below | Most evidence (estimated tokens) placed in the prompt for one question. Chunks are added in rank order until it is full; what was dropped is shown in the `sources` event and the trace |
| `SELECTION_MIN_SCORE` | No | `0` | `0` | Zero or a positive number | BM25 score a chunk must exceed to count as evidence. `0` means "shares at least one meaningful word with the question". Raising it makes the "not enough information" reply more likely |

## Server and tracing

| Variable | Required | Example (safe) | Default | Accepted values | Effect |
|---|---|---|---|---|---|
| `APP_HOST` | No | `127.0.0.1` | `127.0.0.1` | An interface address | Where the server listens. The default keeps a local run off the network. **Fixed to `0.0.0.0` under Compose**, where exposure is controlled by publishing the port on `127.0.0.1` only |
| `APP_PORT` | No | `8000` | `8000` | Whole number 1–65535 | Server port. **Fixed to `8000` under Compose** (published on host `127.0.0.1:8000`) |
| `OTLP_TRACES_URL` | No | *(empty)* | *(empty)* | `http`/`https` URL with a host | Where traces are sent (OTLP over HTTP). Empty: traces are still created, and their ids are still the request ids, but not exported. Must be a **local** collector (REQ-012, REQ-085). **Fixed to the bundled Jaeger (`http://jaeger:4318/v1/traces`) under Compose** |

## Fixed under Compose

`compose.yaml` sets four variables itself, because they must match the container's port mapping, volume and network. Values for them in `.env` are ignored under Compose (a test checks this); they matter only when running without Docker.

| Variable | Value in the container | Why |
|---|---|---|
| `APP_HOST` | `0.0.0.0` | A server listening on loopback inside a container can't be reached through the published port |
| `APP_PORT` | `8000` | Must match the port mapping `127.0.0.1:8000:8000` |
| `CORPUS_DIR` | `/data` | Must match the read-only mount of `./data` |
| `OTLP_TRACES_URL` | `http://jaeger:4318/v1/traces` | The bundled Jaeger on the Compose network |

## Rules that combine settings

Checked at startup; breaking one stops the app with exit code 2 and a message saying which values to change.

1. `LLM_MAX_TOKENS` < `LLM_CONTEXT_TOKENS`.
2. `CONTEXT_TOKEN_BUDGET` < `LLM_CONTEXT_TOKENS`.
3. **The worst-case prompt must fit:** the fixed instructions (about 293 estimated tokens) + `CONTEXT_TOKEN_BUDGET` + 100 tokens for the question must not exceed `LLM_CONTEXT_TOKENS` − `LLM_MAX_TOKENS`. With the defaults: 293 + 1,500 + 100 = 1,893 ≤ 4,096 − 512 = 3,584. So with the other defaults, `CONTEXT_TOKEN_BUDGET` can go up to about 3,190.

Even when these pass, a very long question can still be too big for one request; it is then rejected with `question_too_long`, never truncated (REQ-055).

## Common changes

| Goal | Change |
|---|---|
| Use Docker Model Runner | Remove `COMPOSE_PROFILES`; `LLM_URL=http://model-runner.docker.internal/engines/v1` and `LLM_MODEL` = its model name ([Setup](setup.md#docker-model-runner-not-tested)) |
| Use an Ollama installed on the host | Remove `COMPOSE_PROFILES`; `LLM_URL=http://host.docker.internal:11434/v1` ([Setup](setup.md#ollama-installed-natively-on-the-host)) |
| Use another model | `LLM_MODEL`; keep it at most 1B parameters (REQ-020), and set `LLM_CONTEXT_TOKENS` to the server's context window |
| Slow machine, model times out before the first word | Raise `LLM_READ_TIMEOUT_SECONDS` (and `LLM_REQUEST_TIMEOUT_SECONDS` if long answers are cut off) |
| Longer answers | Raise `LLM_MAX_TOKENS`; check rule 3 still holds, lowering `CONTEXT_TOKEN_BUDGET` if needed |
| More evidence per question | Raise `CONTEXT_TOKEN_BUDGET` within rule 3 |
| Larger files or more of them | `CORPUS_MAX_FILE_BYTES`, `CORPUS_MAX_FILES`. Every question re-scans the corpus, so very large corpora make questions slower |
| Files copied slowly over a network share | Raise `CORPUS_SETTLE_SECONDS` |
| Run outside Docker | `LLM_URL=http://localhost:11434/v1` (an Ollama on the host) and `CORPUS_DIR=./data` ([Setup](setup.md#without-docker-development)) |
