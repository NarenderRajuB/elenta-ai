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
| Language / web stack | Python 3.12, FastAPI + Uvicorn (not yet added; arrives with the HTTP feature) | ADR-001 |
| Model backend | Ollama running **natively on the host** (Docker Model Runner is unavailable on Intel Macs, see TS-001) | ADR-013 |
| Model | `qwen2.5:0.5b`, 494M parameters, Q4_K_M GGUF | ADR-013 |
| Configuration | Environment variables only, read in one module, fail fast | ADR-011 |
| Python tooling | `uv` with a committed `uv.lock` | ADR-014 |

## Prerequisites (so far)

- [uv](https://docs.astral.sh/uv/) 0.11+ (it provides Python 3.12 from `.python-version`)
- Ollama running on the host with `qwen2.5:0.5b` pulled (`ollama pull qwen2.5:0.5b`). Not needed by the current tests.

Docker / Compose prerequisites will be listed when the container feature lands.

## Running the tests

```bash
uv sync            # create .venv from uv.lock (first time only)
uv run pytest -v   # run all tests, one line per test
```

Run a single requirement's tests, for example:

```bash
uv run pytest -v tests/test_config.py::TestReq016FailFast
```

Current result: **48 passed**.

## Configuration

All configuration is via environment variables (REQ-013). [`.env.example`](.env.example) is the reference; copy it to `.env` and adjust. `.env` is git-ignored.

| Variable | Required | Example | Effect |
|---|---|---|---|
| `LLM_URL` | Yes | `http://host.docker.internal:11434/v1` | Base URL of any OpenAI-compatible endpoint. The client appends `/chat/completions`. Must be `http`/`https` with a host, a valid port if given, and no query string or fragment. Trailing `/` is removed. |
| `LLM_MODEL` | Yes | `qwen2.5:0.5b` | Model identifier, passed to the endpoint unchanged. |

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

**Not yet done for REQ-016:** making the service *exit* at startup on a config error. This needs the application entry point, which arrives with the HTTP feature.

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
  config.py          configuration boundary (REQ-013..016)
tests/
  test_config.py     config tests by requirement
  test_file_headers.py
docs/                spec, requirements, acceptance criteria, ADRs, troubleshooting log
.env.example         configuration reference
pyproject.toml       project metadata, pytest settings
uv.lock              pinned dependency versions
```
