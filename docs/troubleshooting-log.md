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
