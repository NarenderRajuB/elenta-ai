# ELENTA AI: Local Grounded Chat Service

A small, local, containerised chat service that answers questions from a live document corpus in `./data`, streams its answer, treats documents and model output as untrusted, and can be traced end to end. It is built for the ELENTA AI Developer Practical Assessment (ELN-TA-BTR-003).

> **Status: in progress.** This README is updated in the same change as each requirement is implemented and tested. Anything not listed under [Implemented requirements](#implemented-requirements) does **not** exist yet.

## Documentation

| Document | Contents |
|---|---|
| [docs/spec.md](docs/spec.md) | Structured summary of the brief, interpretations, open questions |
| [docs/requirements.md](docs/requirements.md) | Requirements register (REQ-001 … REQ-141), each traced to a brief section (`§` = section of the brief) |
| [docs/acceptance-criteria.md](docs/acceptance-criteria.md) | Live review scenarios and per-requirement acceptance criteria |
| [docs/architecture-decisions.md](docs/architecture-decisions.md) | Decision log (ADRs) |
| [docs/troubleshooting-log.md](docs/troubleshooting-log.md) | Issues and dead ends, in the order they occurred |

The full operator guide (architecture, setup, configuration, operations, security, failure handling) will be added under `docs/` as features land.

## Current architecture decisions

| Area | Decision | ADR |
|---|---|---|
| Language / web stack | Python 3.12, FastAPI + Uvicorn | ADR-001 |
| Outbound HTTP client | `httpx`, ignoring proxy env vars (`trust_env=False`); no internet needed after install | ADR-002 |
| Model backend | Ollama running **natively on the host** (Docker Model Runner is unavailable on Intel Macs, see TS-001) | ADR-013 |
| Model | `qwen2.5:0.5b`, 494M parameters, Q4_K_M GGUF | ADR-013 |
| Configuration | Environment variables only, read in one module, fail fast | ADR-011 |
| Python tooling | `uv` with a committed `uv.lock` | ADR-014 |
| Health signals | `/healthz` liveness, `/readyz` readiness (probes the model endpoint) | ADR-015 |

## Prerequisites (so far)

- [uv](https://docs.astral.sh/uv/) 0.11+ (it provides Python 3.12 from `.python-version`)
- Ollama running on the host with `qwen2.5:0.5b` pulled (`ollama pull qwen2.5:0.5b`). Needed to see `/readyz` report *ready*. The automated tests do **not** need it.

Internet is needed once, for `uv sync` and `ollama pull`. After that everything runs offline.

Docker / Compose prerequisites will be listed when the container feature lands.

## Running the service locally (no Docker yet)

```bash
uv sync
LLM_URL=http://localhost:11434/v1 LLM_MODEL=qwen2.5:0.5b uv run python -m app
```

When running directly on the Mac, `LLM_URL` uses `localhost`. `host.docker.internal` (as in `.env.example`) only resolves from inside a container.

Health check, in another terminal:

```bash
curl -i http://127.0.0.1:8000/healthz   # 200 {"status":"ok"}: process is up
curl -i http://127.0.0.1:8000/readyz    # 200 ready, or 503 not_ready with a reason
```

| `/readyz` reason | Meaning | Typical fix |
|---|---|---|
| `unreachable` | Nothing answering at `LLM_URL` | Start Ollama (`ollama serve` or the Ollama app); check the URL |
| `timeout` | Endpoint slower than `LLM_HEALTH_TIMEOUT_SECONDS` | Check host load; raise the timeout |
| `http_error` | Endpoint answered with a non-200 status | Check `LLM_URL` path (should end in `/v1` for Ollama) |
| `invalid_response` | Answer is not an OpenAI-style model list | `LLM_URL` points at something that is not OpenAI-compatible |
| `model_not_found` | Endpoint is up but `LLM_MODEL` isn't listed (exact match) | `ollama pull qwen2.5:0.5b`, or fix the spelling |

With missing or invalid configuration, the process prints every problem and exits with code **2** before opening any port:

```text
$ uv run python -m app
elenta: startup aborted.
Invalid configuration (2 problem(s)):
  - LLM_URL is required but is missing or empty
  - LLM_MODEL is required but is missing or empty
```

## Running the tests

```bash
uv sync            # create .venv from uv.lock (first time only)
uv run pytest -v   # run all tests, one line per test
```

Run a single requirement's tests, for example:

```bash
uv run pytest -v tests/test_config.py::TestReq016FailFast
```

Current result: **102 passed, 0 warnings**. Dev-only dependencies: `pytest`, and `httpx2` for FastAPI's test client (TS-003). The tests start local servers on `127.0.0.1` only and need neither Ollama nor internet.

## Configuration

All configuration is via environment variables (REQ-013). [`.env.example`](.env.example) is the reference; copy it to `.env` and adjust. `.env` is git-ignored.

| Variable | Required | Example | Effect |
|---|---|---|---|
| `LLM_URL` | Yes | `http://host.docker.internal:11434/v1` | Base URL of any OpenAI-compatible endpoint. The client appends `/chat/completions`. Must be `http`/`https` with a host, a valid port if given, and no query string or fragment. Trailing `/` is removed. |
| `LLM_MODEL` | Yes | `qwen2.5:0.5b` | Model identifier, passed to the endpoint unchanged. `/readyz` checks it is listed by the endpoint. |
| `APP_HOST` | No (default `127.0.0.1`) | `127.0.0.1` | Interface the server binds to. Loopback keeps a local run off the network; a container must use `0.0.0.0`. |
| `APP_PORT` | No (default `8000`) | `8000` | Server port, whole number 1–65535. |
| `LLM_HEALTH_TIMEOUT_SECONDS` | No (default `3`) | `3` | Max wait for the `/readyz` model probe; positive, finite seconds. |

A blank optional value means "use the default".

If anything is missing or invalid, loading fails with **one** error that lists every problem. The error never repeats the URL value, because it may contain credentials.

## Implemented requirements

Each requirement lists its test scenarios: ✅ positive (valid input accepted), ❌ negative (invalid input rejected), ⚠️ edge (boundary or unusual input with deliberately chosen behaviour).

### REQ-013: All configuration via environment variables
Code: `app/config.py` · Tests: `tests/test_config.py::TestReq013EnvOnly`

| Type | Scenario | Expected |
|---|---|---|
| ✅ | Valid env mapping | `Settings` built with exact values |
| ❌ | Process env is valid but an empty mapping is passed | Fails, so there is no hidden read of `os.environ` |
| ❌ | Any module other than `config.py` reads `os.environ` / `getenv` | Test fails (single config boundary) |
| ⚠️ | Unrelated variables present (`PATH`, `LLM_UNKNOWN`) | Ignored |
| ⚠️ | Attempt to modify `Settings` after load | Rejected (immutable) |
| ✅ | `load_settings_from_process_env()` (entry point) | Reads the real process env; it's the only `os.environ` access |
| ✅ | Optional settings unset / set | Documented defaults / overridden values (`TestOptionalSettings`) |
| ⚠️ | Optional setting present but blank | Default used |

### REQ-014: `.env.example` with safe example values
Code: `.env.example` · Tests: `tests/test_config.py::TestReq014EnvExample`

| Type | Scenario | Expected |
|---|---|---|
| ✅ | Keys in `.env.example` vs `Settings` fields | Exactly equal (no drift) |
| ✅ | Load `.env.example` as configuration | Valid as-is |
| ❌ | Example `LLM_URL` contains credentials (`@`) | Test fails |
| ⚠️ | Same key listed twice | Test fails |

### REQ-015: Endpoint and model from `LLM_URL` / `LLM_MODEL`; switching needs no code change
Code: `app/config.py` · Tests: `tests/test_config.py::TestReq015EndpointSwitching`

| Type | Scenario | Expected |
|---|---|---|
| ✅ | Ollama on host, Docker Model Runner, generic HTTPS endpoint | All accepted by env change only |
| ⚠️ | Model ID with `/`, `:`, mixed case | Kept unchanged |
| ⚠️ | Leading/trailing whitespace in values | Trimmed |
| ⚠️ | `…/v1/` or `…/v1//` | Normalised to `…/v1` |
| ⚠️ | IPv6 host `http://[::1]:11434/v1` | Accepted |
| ⚠️ | Upper-case scheme `HTTP://` | Accepted |

### REQ-016: Fail fast with a clear error on missing or invalid configuration
Code: `app/config.py` · Tests: `tests/test_config.py::TestReq016FailFast`

| Type | Scenario | Expected |
|---|---|---|
| ✅ | Valid configuration | No error |
| ❌ | `LLM_URL` or `LLM_MODEL` missing | `ConfigError` naming the variable |
| ❌ | Empty or whitespace-only value | Treated as missing |
| ❌ | No scheme, `ftp://`, no host, free text, `//host` | "must be an http:// or https:// URL with a host" |
| ❌ | Port `99999`, `abc`, `-1` | "invalid port" (regression for TS-002) |
| ❌ | Query string or fragment in `LLM_URL` | Rejected (regression for TS-002) |
| ❌ | Several problems at once | One error listing all of them, with a count |
| ⚠️ | Invalid URL containing a password | Password never appears in the error (REQ-068) |
| ⚠️ | Port `0` and `65535` | Accepted (range boundaries) |
| ❌ | `APP_PORT` = `0`, `65536`, `-1`, `abc`, `8000.5`, `80 80` | "must be a whole number from 1 to 65535" |
| ⚠️ | `APP_PORT` = `1`, `65535`, `" 8080 "` | Accepted (boundaries, whitespace trimmed) |
| ❌ | `LLM_HEALTH_TIMEOUT_SECONDS` = `0`, `-1`, `abc`, `nan`, `inf`, `-inf` | "must be a positive number of seconds" |
| ⚠️ | `LLM_HEALTH_TIMEOUT_SECONDS` = `0.001`, `1e1` | Accepted |
| ❌ | Required and optional problems together | All 4 reported in one error |

**Startup exit** (`tests/test_entrypoint.py`, real subprocess):

| Type | Scenario | Expected |
|---|---|---|
| ❌ | No configuration at all | Exit code 2, both variables named, no Python traceback |
| ❌ | Required vars valid, `APP_PORT=70000` | Exit code 2, `APP_PORT` named |
| ⚠️ | Bad URL containing a password | Exit code 2, password not printed |

### REQ-017: Practical health / readiness signal
Code: `app/main.py`, `app/health.py`, `app/__main__.py` · Tests: `tests/test_health.py`, `tests/test_entrypoint.py`

| Type | Scenario | Expected |
|---|---|---|
| ✅ | `GET /healthz` | 200 `{"status":"ok"}` |
| ✅ | Endpoint lists `LLM_MODEL` | `/readyz` 200 `ready` |
| ✅ | Probe request | Exactly one `GET {LLM_URL}/models` (standard OpenAI-compatible path) |
| ✅ | Real server via `python -m app`, model port closed | `/healthz` 200, `/readyz` 503 `unreachable` |
| ❌ | Connection refused | 503 `unreachable` |
| ❌ | Read or connect timeout | 503 `timeout` |
| ❌ | Endpoint returns 500 or 404 | 503 `http_error` |
| ❌ | Not JSON; no `data`; items without `id`; `data` not a list | 503 `invalid_response` |
| ❌ | Model not in list | 503 `model_not_found` |
| ⚠️ | Model down | `/healthz` still 200 (liveness independent of the model) |
| ⚠️ | Empty model list | 503 `model_not_found` |
| ⚠️ | Case-only difference in model name | 503 `model_not_found` (exact match) |
| ⚠️ | Ollama starts after the app | Next `/readyz` turns 200 without restart |
| ⚠️ | `LLM_URL` contains a password; error mentions it | Response contains neither the password nor the host |
| ⚠️ | `LLM_HEALTH_TIMEOUT_SECONDS=0.5` | Probe uses 0.5 s connect/read timeout |
| ⚠️ | `HTTP_PROXY`/`HTTPS_PROXY` set | Ignored (`trust_env=False`), so traffic goes only to `LLM_URL` |

Manually verified against real Ollama on the host (2026-10-08): ready → 200; `LLM_MODEL=qwen2.5:7b-typo` → 503 `model_not_found`.

### REQ-091: Every source file opens with a header comment block
Tests: `tests/test_file_headers.py`

| Type | Scenario | Expected |
|---|---|---|
| ✅ | Every `.py` file under `app/` and `tests/` | Starts with a `#` comment |
| ❌ | Code on the first line | Rejected |
| ❌ | Docstring instead of comment block | Rejected |
| ⚠️ | Empty file, or blank first line | Rejected |
| ⚠️ | UTF-8 BOM before the comment | Accepted |

## Project layout (current)

```
app/
  __init__.py        package marker
  __main__.py        entry point: validate config, then serve (REQ-016)
  config.py          configuration boundary (REQ-013..016)
  health.py          model endpoint readiness probe (REQ-017)
  main.py            FastAPI app factory, /healthz and /readyz (REQ-017)
tests/
  test_config.py     config tests by requirement
  test_health.py     health/readiness with a fake model endpoint
  test_entrypoint.py real subprocess: startup exit codes, live server
  test_file_headers.py
docs/                spec, requirements, acceptance criteria, ADRs, troubleshooting log
.env.example         configuration reference
pyproject.toml       project metadata, pytest settings
uv.lock              pinned dependency versions
```
