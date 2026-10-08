# Troubleshooting Log

Required by brief §5.11 (REQ-111, REQ-112). Entries are appended **in the order issues occur** and are never rewritten into a tidy narrative afterwards. Dead ends are recorded too.

Each entry has: **Symptom**, **Diagnosis**, **Attempted actions**, and **Resolution or remaining limitation**.

---

## TS-001: Docker Model Runner unavailable on the development machine

- **Date:** 2026-10-08
- **Phase:** Pre-implementation environment check
- **Related:** REQ-001, REQ-021, ADR-003 (rejected), ADR-013

**Symptom.**
```
$ docker model version
docker: unknown command: docker model
```
The same error appears for `docker model status` and `docker model ls`.

**Environment.**
- MacBook Pro, Intel Core i7-8850H, `uname -m` = `x86_64`
- macOS 15.7.9 (24G830)
- Docker Desktop 4.94.0, Docker Engine 29.8.2, Compose v5.5.1

**Diagnosis.**
- `docker-model` is missing from both `~/.docker/cli-plugins/` and `/Applications/Docker.app/Contents/Resources/cli-plugins/`. Other Docker Desktop plugins (`docker-ai`, `docker-mcp`, `docker-scout`, etc.) are present, so the installation itself is otherwise complete.
- On macOS, Docker Model Runner is supported on Apple Silicon only. Docker Desktop does not ship the plugin for Intel Macs, and no settings toggle can enable it.

**Attempted actions.**
1. Ran `docker model version` / `status` / `ls`: unknown command.
2. Searched the plugin directories: no `docker-model` binary.
3. Checked whether a local OpenAI-compatible alternative was available: Ollama 0.35.1 is installed and running at `localhost:11434`.
4. Ran `ollama show qwen2.5:0.5b`: 494.03M parameters, Q4_K_M, context 32768.
5. Sent a streaming request to `POST /v1/chat/completions`: received progressive `chat.completion.chunk` frames and a final `usage` chunk when `stream_options.include_usage` was set.

**Resolution.**
Using Ollama as the OpenAI-compatible local endpoint, as the brief §5.2 permits when DMR is unavailable on the candidate hardware. Recorded in ADR-013. The application remains endpoint-agnostic, so DMR can be used on Apple Silicon by changing only `LLM_URL` and `LLM_MODEL`.

**Remaining limitation.**
- The DMR path cannot be demonstrated on this machine. Docs must state this honestly.
- Ollama sets its effective context window server-side, so there is a risk of silent truncation if our prompt exceeds it (see ADR-013 consequences).

---

## TS-002: LLM_URL validation accepted invalid ports and query/fragment parts

- **Date:** 2026-10-08
- **Phase:** Feature 1 (configuration), while writing edge-case tests
- **Related:** REQ-016, ADR-011, `app/config.py`

**Symptom.** Probing `load_settings` with edge-case URLs showed these were all accepted as valid:
```
'http://host:99999/v1' -> ACCEPTED
'http://host:abc/v1'   -> ACCEPTED
'http://h/v1?x=1'      -> ACCEPTED
'http://h/v1#frag'     -> ACCEPTED
```

**Diagnosis.**
- `urllib.parse.urlsplit` parses lazily. `.hostname` is set even when the port is garbage, and the port is only validated when `.port` is accessed (it raises `ValueError`). The original check only looked at scheme and hostname.
- The inference client will build request URLs by appending `/chat/completions` to `LLM_URL`. With a query string or fragment present, the path would land after `?…` or `#…` and produce a wrong URL.
- In both cases the service would start and then fail at the first chat request, which breaks the "fail fast" requirement (REQ-016).

**Attempted actions.** Wrote a probe script over 8 edge-case URLs. Upper-case scheme, IPv6 host and repeated trailing slashes already behaved correctly (`urlsplit` lower-cases the scheme).

**Resolution.** `_validate_url` now reads `parts.port` inside `try/except ValueError`, and rejects URLs with a query string or fragment. Each case gets its own clear error message. Regression tests were added in `tests/test_config.py`.

**Remaining limitation.** Validation checks only that the URL is well-formed, not that it is reachable. Reachability is the job of the readiness signal (REQ-017), which is a later feature.

---

## TS-003: Starlette deprecation warning for `httpx` in the test client

- **Date:** 2026-10-08
- **Phase:** Feature 2 (HTTP service and health signals), first test run
- **Related:** REQ-017, REQ-121, ADR-001, ADR-002

**Symptom.** The test suite passes (102 tests) but pytest prints:
```
.venv/lib/python3.12/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning:
Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
```

**Diagnosis.** The warning comes from `starlette.testclient` (Starlette 1.7.0, via FastAPI 0.143.0) and is raised only when `fastapi.testclient` is imported, i.e. in tests. The application's runtime use of `httpx` (outbound client, ADR-002) does not involve `starlette.testclient` and is unaffected.

**Attempted actions.** None yet. The suggested `httpx2` package has not been reviewed, and adding a dependency needs the candidate's approval (working rules).

**Resolution / remaining limitation.** Open. Options:
1. Review `httpx2` and, if acceptable, add it as a **dev-only** dependency for the test client.
2. Keep `httpx` and record the warning as accepted until Starlette removes support.
3. Pin Starlette below the version that deprecates it (rejected unless needed: it holds back security fixes).

**Investigation (2026-10-08, after the candidate asked for option 1 to be explored).**
- Starlette's source (`starlette/testclient.py`) tries `import httpx2 as httpx` first and falls back to `httpx` with this warning. Its package metadata says "Test client built on `httpx2`" and lists `httpx2` under the `full` extra. This is Starlette's supported path, not a third-party suggestion.
- PyPI (`https://pypi.org/pypi/httpx2/json`): `httpx2` 2.13.1, BSD-3-Clause, author Tom Christie (also the author of `httpx` and Starlette), source `github.com/pydantic/httpx2`. 18 releases, first 2026-05-11, latest 2026-09-23. Python >=3.10.
- Base dependencies it would add: `httpcore2`, `truststore` (uses the OS certificate store for TLS), `anyio`, `idna` and `typing-extensions` (the last three are already installed).
- Impact if added as **dev-only**: only `TestClient` would use it. The app's runtime client stays `httpx` (ADR-002), and so do the tests that inject `httpx.MockTransport` into the app or call the live server with `httpx.get`. The runtime image is unchanged.
- Open point: the ecosystem appears to be moving from `httpx` to `httpx2`. Whether the **runtime** client should move as well is a separate decision for ADR-002 and is not part of this fix.

**Resolution (2026-10-08).** The candidate approved adding `httpx2` as a dev-only dependency. Ran `uv add --dev httpx2`, which installed `httpx2` 2.13.1, `httpcore2` 2.13.1 and `truststore` 0.10.4. Re-ran the suite: **102 passed, 0 warnings**. Runtime dependencies are unchanged (`fastapi`, `httpx`, `uvicorn`).

**Remaining limitation.** The dev environment now contains two HTTP client stacks: `httpx2` for `TestClient`, `httpx` for the app. Moving the runtime client to `httpx2` stays an open question for ADR-002.
