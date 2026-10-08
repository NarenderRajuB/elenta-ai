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
| Container | Pinned slim base, non-root UID 10001, read-only root FS and `/data`, no capabilities, loopback-only port | ADR-012 |
| Live corpus | Re-scan `/data` at the start of each request; reuse unchanged files; one immutable snapshot per refresh | ADR-005 |

## Prerequisites (so far)

- [uv](https://docs.astral.sh/uv/) 0.11+ (it provides Python 3.12 from `.python-version`)
- Ollama running on the host with `qwen2.5:0.5b` pulled (`ollama pull qwen2.5:0.5b`). Needed to see `/readyz` report *ready*. The automated tests do **not** need it.

- Docker Desktop (tested: 4.94, Compose v5.5.1) for the containerised run.

Internet is needed once, for `uv sync`, `ollama pull` and the first `docker compose build`, which pulls the pinned `python:3.12-slim` base. After that everything runs offline.

## Running with Docker Compose

```bash
cp .env.example .env          # first time only; adjust if needed
docker compose build          # first time only (online: base image + dependencies)
docker compose up -d          # the single startup command
docker compose ps             # STATUS shows (healthy) once /healthz answers
curl -i http://127.0.0.1:8000/readyz
docker compose logs -f app
docker compose down
```

- `LLM_URL` in `.env` uses `host.docker.internal`, which is how the container reaches Ollama on the host. This was verified on Docker Desktop for Mac (ADR-013).
- Inside the container `APP_HOST`/`APP_PORT` are fixed to `0.0.0.0:8000`. The port is published on **host loopback only** (`127.0.0.1:8000`).
- `./data` is mounted read-only at `/data`. Files added on the host are visible in the container immediately.
- If `.env` is missing or incomplete, the container exits with code **2** and lists the missing variables (`docker compose logs app`).
- The container health status reflects **liveness** only. Check `/readyz` for model availability.

Docker / Compose prerequisites will be listed when the container feature lands.

## Running the service locally (no Docker yet)

```bash
uv sync
LLM_URL=http://localhost:11434/v1 LLM_MODEL=qwen2.5:0.5b CORPUS_DIR=./data uv run python -m app
```

`CORPUS_DIR=./data` is needed outside Docker because the default `/data` only exists in the container.

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

Container tests build the image and start real containers (about 80 s, need Docker). They are excluded from the default run:

```bash
uv run pytest -m container -v
```

Run a single requirement's tests, for example:

```bash
uv run pytest -v tests/test_config.py::TestReq016FailFast
```

Current result: **208 passed, 0 warnings** (default run) and **15 passed** (`-m container`). Dev-only dependencies: `pytest`, and `httpx2` for FastAPI's test client (TS-003). The tests start local servers on `127.0.0.1` only and need neither Ollama nor internet.

## Configuration

All configuration is via environment variables (REQ-013). [`.env.example`](.env.example) is the reference; copy it to `.env` and adjust. `.env` is git-ignored.

| Variable | Required | Example | Effect |
|---|---|---|---|
| `LLM_URL` | Yes | `http://host.docker.internal:11434/v1` | Base URL of any OpenAI-compatible endpoint. The client appends `/chat/completions`. Must be `http`/`https` with a host, a valid port if given, and no query string or fragment. Trailing `/` is removed. |
| `LLM_MODEL` | Yes | `qwen2.5:0.5b` | Model identifier, passed to the endpoint unchanged. `/readyz` checks it is listed by the endpoint. |
| `APP_HOST` | No (default `127.0.0.1`) | `127.0.0.1` | Interface the server binds to. Loopback keeps a local run off the network; a container must use `0.0.0.0`. |
| `APP_PORT` | No (default `8000`) | `8000` | Server port, whole number 1–65535. |
| `LLM_HEALTH_TIMEOUT_SECONDS` | No (default `3`) | `3` | Max wait for the `/readyz` model probe; positive, finite seconds. |
| `CORPUS_DIR` | No (default `/data`) | `/data` | Corpus root; nothing outside it is read. A relative path is made absolute. Must exist at startup (exit 2 otherwise). Fixed to `/data` in Compose. |
| `CORPUS_MAX_FILE_BYTES` | No (default `104857600` = 100 MB) | `104857600` | Larger files are skipped (`too_large`). Whole number ≥ 1. |
| `CORPUS_MAX_FILES` | No (default `500`) | `500` | Supported files beyond this (in sorted path order) are skipped (`file_limit_exceeded`). Whole number ≥ 1. |
| `CORPUS_SETTLE_SECONDS` | No (default `0.5`) | `0.5` | A file modified more recently is treated as still being written and picked up later. `0` disables. |

A blank optional value means "use the default".

If anything is missing or invalid, loading fails with **one** error that lists every problem. The error never repeats the URL value, because it may contain credentials.

## Live corpus

**Supported formats:** UTF-8 text files with extension `.txt` or `.md` (case-insensitive), in `CORPUS_DIR` and its subdirectories. A leading UTF-8 BOM is removed. Markdown is read as plain text; it is never rendered or executed.

**Limits:** up to 100 MB per file and 500 files (both configurable). Anything over a limit is skipped and reported, never silently dropped.

**How changes are picked up (ADR-005):** every chat request (the chat feature comes next) starts with a refresh. The refresh re-scans the directory, reuses files whose `(size, mtime, ctime, inode)` is unchanged, re-reads new or changed files, and drops anything no longer present. Each request sees exactly one complete snapshot.

**When a change is ready to serve:** on the first request that starts after the file has been unmodified for `CORPUS_SETTLE_SECONDS` (0.5 s by default) and is then read without changing. Until then the file is **not served at all**, not even its previous version.

**Skip reasons** (logged as `corpus file skipped: path=… reason=…`; content is never logged):

| Reason | Severity | Meaning |
|---|---|---|
| `hidden` | info | Name (or a parent directory) starts with `.` |
| `symlink` | info | Symbolic link: never followed, even if the target is inside the corpus |
| `unsupported_type` | info | Not `.txt` / `.md` |
| `not_regular_file` | info | Pipe, socket or device: never opened |
| `empty` | info | Zero bytes, whitespace only, or BOM only |
| `settling` | info | Modified within the settle window; retried next request |
| `too_large` | error | Over `CORPUS_MAX_FILE_BYTES` |
| `file_limit_exceeded` | error | Beyond `CORPUS_MAX_FILES` |
| `changing` | error | Changed while being read; retried next request |
| `not_utf8` | error | Not valid UTF-8 |
| `binary_content` | error | Contains NUL bytes |
| `unreadable` | error | Permission denied or I/O error (file or directory) |
| `invalid_filename` | error | Name is not valid UTF-8 |
| `outside_root` | error | Real path resolves outside the corpus root |
| `corpus_dir_missing` | error | Corpus root vanished at runtime; empty corpus served |

`error` reasons log at WARNING; `info` reasons at DEBUG.

**Platform notes (Docker Desktop for Mac, verified):** the bind mount passes size, mtime, ctime and inode through, so every change type is detected. No file watcher is used, so inotify limitations of Docker Desktop don't apply. The VM clock trails macOS by under 1 ms; this only delays readiness by that amount (TS-004). Native Linux is not tested here.

**Rebuild after code changes:** `docker compose up -d --build`. Without `--build`, Compose reuses the old image (TS-004).

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

### REQ-011 / REQ-040 / REQ-066 / REQ-067 / REQ-012: Containerised, read-only corpus, non-root, offline
Code: `Dockerfile`, `compose.yaml`, `.dockerignore` · Tests: `tests/test_container_config.py` (static, default run), `tests/test_container_runtime.py` (`-m container`)

| Type | Scenario | Expected | REQ |
|---|---|---|---|
| ✅ | `./data` bind-mounted at `/data` | Mount exists, `read_only: true` | 040, 067 |
| ✅ | File created in `./data` on the host | Readable at `/data/…` in the running container | 040 |
| ✅ | Container starts via Compose | Health status becomes `healthy` | 011 |
| ✅ | `id -u` inside the container | `10001` (Dockerfile's last `USER` is numeric, non-zero) | 066 |
| ✅ | Host Ollama with the model available | `/readyz` 200 from inside the container (skipped if Ollama is absent) | 011 |
| ✅ | Image built from `uv.lock` | `uv sync --frozen --no-dev` | 012 |
| ✅ | Hardening | `read_only`, `cap_drop: [ALL]`, `no-new-privileges` | 066 |
| ❌ | Config missing (`LLM_URL=`, `LLM_MODEL=`) | Container exits 2, both named, no traceback | 016 |
| ❌ | Write to `/data` or `/app` | `Read-only file system` | 067 |
| ❌ | Model endpoint unreachable (dead port) | `/readyz` 503 `unreachable` | 017 |
| ❌ | Port published on all host interfaces | Test fails: every port must bind `127.0.0.1` | — |
| ❌ | Healthcheck uses `/readyz` | Test fails: must be liveness only (ADR-015) | 017 |
| ❌ | `.env`, `data/`, `.git/` or `tests/` in build context | Test fails: listed in `.dockerignore` | 068 |
| ❌ | Unpinned base image | Test fails: must be `@sha256:` digest | 012 |
| ⚠️ | Compose rendered with empty `LLM_URL`/`LLM_MODEL` | Still valid; the app reports the problem | 016 |
| ⚠️ | `/tmp` | Writable (tmpfs), the only writable path | 066 |
| ⚠️ | Linux capabilities | `CapEff` all zeros | 066 |
| ⚠️ | `pytest` / `httpx2` in image | Absent | 012 |
| ⚠️ | Started with `--network none` | `/healthz` 200, `/readyz` 503 `unreachable`, so no internet is needed to start | 012 |
| ⚠️ | Native Linux Docker | `host.docker.internal:host-gateway` alias present (not tested on Linux) | 046 |

### REQ-041: Supported formats (and limits)
Code: `app/ingestion.py` · Tests: `tests/test_corpus.py::TestReq041Formats`, `::TestLimits`

| Type | Scenario | Expected |
|---|---|---|
| ✅ | `.txt` and `.md` files | Served with exact text |
| ✅ | Nested directories | Served with POSIX relative path (`policies/hr/leave.md`) |
| ✅ | File exactly at the size limit | Served |
| ❌ | `.pdf`, `.png`, `.json`, no extension, `.txt.gz` | Skipped `unsupported_type` |
| ❌ | File over the size limit | Skipped `too_large` **without being read** |
| ❌ | More supported files than `CORPUS_MAX_FILES` | First N in sorted order served; rest `file_limit_exceeded` |
| ⚠️ | `UPPER.TXT`, `Mixed.Md` | Served (case-insensitive) |
| ⚠️ | UTF-8 BOM | Removed |
| ⚠️ | Unicode / spaces in filename; non-Latin content and emoji | Served unchanged |
| ⚠️ | Hidden/unsupported files | Don't count towards the file limit |
| ⚠️ | File grows past the limit between scan and read | Not served |

### REQ-042 / REQ-003: Add, modify, rename, remove reflected without restart
Code: `app/corpus.py` · Tests: `tests/test_corpus.py::TestReq042LiveChanges`, container `test_positive_live_changes_visible_through_bind_mount`

| Type | Scenario | Expected |
|---|---|---|
| ✅ | File added | Served on next refresh (`added=1`) |
| ✅ | File modified | Only the new content served (`modified=1`) |
| ✅ | File renamed | Old path gone, new path served (`removed=1`, `added=1`) |
| ✅ | Nothing changed | No file re-read (`unchanged`); version unchanged |
| ✅ | Same changes made on the Mac, seen in the container | Each reflected through the bind mount |
| ⚠️ | Same-size edit with mtime forced back | Detected via ctime |
| ⚠️ | Atomic save (write temp file, rename over original) | Detected via inode |
| ⚠️ | 20 rapid successive edits | Each refresh shows the latest |
| ⚠️ | 200 write-then-refresh cycles through the bind mount (manual stress) | 200/200 correct (TS-004) |

### REQ-044: Removed documents stop contributing
Tests: `tests/test_corpus.py::TestReq044Deletion`

| Type | Scenario | Expected |
|---|---|---|
| ✅ | File deleted | Gone from the next snapshot (`removed=1`) |
| ✅ | Deleted content | Appears nowhere in the snapshot or store |
| ❌ | Directory with files deleted | All its documents gone |
| ⚠️ | Served file becomes corrupt | Not served from cache; skipped `not_utf8` |
| ⚠️ | Corpus root removed while running | Empty corpus, `corpus_dir_missing`; no crash |

### REQ-045: Rapid changes and partial writes never served mixed
Tests: `tests/test_corpus.py::TestReq045PartialWrites`

| Type | Scenario | Expected |
|---|---|---|
| ✅ | File within the settle window, then settled | `settling` first, then served |
| ❌ | File appended to while being read | Skipped `changing`; not served |
| ❌ | Previously served file is mid-change | Neither old nor new version served until stable |
| ⚠️ | mtime an hour in the future | Waits (`settling`), never served early |
| ⚠️ | `CORPUS_SETTLE_SECONDS=0` with mtime 50 ms ahead (VM clock skew) | Served (regression for TS-004) |
| ⚠️ | 4 threads refreshing concurrently | No errors; consistent snapshot |

### REQ-056 / REQ-073: Unreadable or corrupt files don't take down the service
Tests: `tests/test_corpus.py::TestReq056CorruptFiles`

| Type | Scenario | Expected |
|---|---|---|
| ❌ | Invalid UTF-8 / NUL byte / zero bytes / whitespace only / BOM only | Skipped (`not_utf8` / `binary_content` / `empty`); valid files still served |
| ❌ | Permission-denied file | `unreadable`; other files served |
| ❌ | Permission-denied subdirectory | `unreadable` for the directory; other files served |
| ⚠️ | Logging | One WARNING per error skip, with path and reason only; no content |
| ⚠️ | Policy skips (`.gitkeep`, `.pdf`) | Not logged as warnings |
| ⚠️ | Non-UTF-8 filename | Detected (`invalid_filename`) |

### REQ-064: Reads restricted to the corpus root
Tests: `tests/test_corpus.py::TestReq064Boundary`

| Type | Scenario | Expected |
|---|---|---|
| ✅ | Files under the root | Served; sibling files outside the root never read |
| ❌ | Symlinked file (target inside the root) | Skipped `symlink` |
| ❌ | Symlink to a file outside the root | Skipped; target content never read |
| ❌ | Symlinked directory | Not traversed |
| ❌ | Hidden file or hidden directory | Skipped `hidden`; not descended |
| ❌ | FIFO named `pipe.txt` | Skipped `not_regular_file` immediately (never blocks) |
| ⚠️ | File swapped for a symlink between scan and read | Not followed (`O_NOFOLLOW`) |
| ⚠️ | Directory swapped for a symlink between scan and read | `outside_root` |
| ⚠️ | Hand-built path `../x.txt` | `outside_root` |
| ⚠️ | Root missing, or root is a file | `corpus_dir_missing`; no crash |

### Corpus settings (REQ-013 / REQ-016)
Tests: `tests/test_config.py::TestCorpusSettings`, `tests/test_entrypoint.py`, container tests

| Type | Scenario | Expected |
|---|---|---|
| ✅ | Defaults | `/data`, 100 MB, 500 files, 0.5 s |
| ✅ | Overrides; relative `./data` | Applied; made absolute |
| ❌ | Limits `0`, `-1`, `abc`, `1.5`, `1e6` | "must be a whole number of at least 1" |
| ❌ | Settle `-0.1`, `abc`, `nan`, `inf` | "must be zero or a positive number of seconds" |
| ❌ | `CORPUS_DIR` missing, or a file | Exit code 2, clear message |
| ❌ | Container run without the `/data` mount | Exit code 2 |
| ⚠️ | Settle `0`; limits of `1` | Accepted |
| ⚠️ | Blank value | Default |
| ⚠️ | Startup log | `corpus refreshed: version=1 documents=1 added=1 …`; no content |

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
  corpus.py          corpus state: refresh, snapshot, change stats (ADR-005)
  ingestion.py       safe file discovery and reading under the corpus root
  health.py          model endpoint readiness probe (REQ-017)
  main.py            FastAPI app factory, /healthz and /readyz (REQ-017)
data/                corpus, mounted read-only at /data (only .gitkeep so far)
tests/
  test_config.py     config tests by requirement
  test_corpus.py     ingestion and live-corpus tests by requirement
  test_container_config.py   static checks of compose.yaml / Dockerfile
  test_container_runtime.py  real containers (-m container)
  test_health.py     health/readiness with a fake model endpoint
  test_entrypoint.py real subprocess: startup exit codes, live server
  test_file_headers.py
docs/                spec, requirements, acceptance criteria, ADRs, troubleshooting log
.env.example         configuration reference
Dockerfile           two-stage image build (pinned base, non-root)
compose.yaml         single-command startup
.dockerignore        keeps .env, data/, tests/ out of the image
pyproject.toml       project metadata, pytest settings
uv.lock              pinned dependency versions
```
