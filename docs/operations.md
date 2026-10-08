# Operations

Running the service day to day: logs and traces, how corpus changes are picked up, restart and recovery, supported formats and limits, and platform notes (REQ-106). Startup is in [Setup](setup.md); settings in [Configuration](configuration.md); behaviour on each failure in Failure handling (to be written, REQ-108).

## Everyday commands

```bash
docker compose up -d                 # start app + Jaeger (Ollama must already be running)
docker compose up -d --build         # after changing code or the Dockerfile; without --build the old image is reused (TS-004)
docker compose ps                    # app shows (healthy) once /healthz answers
curl -s http://127.0.0.1:8000/readyz # is the model reachable?
docker compose logs -f app           # follow the app's logs
docker compose down                  # stop everything; traces are discarded, ./data is untouched
```

## Logs

The app writes **one JSON object per line** to stderr, for every logger including the web server's. Docker keeps them; read them with `docker compose logs app`.

```json
{"ts": "...", "level": "INFO", "logger": "app.chat", "message": "chat start", "request_id": "8dbc…ed", "question_chars": 30}
{"ts": "...", "level": "INFO", "logger": "app.corpus", "message": "corpus refreshed: version=1 documents=2 added=2 modified=0 removed=0 unchanged=0 skipped=0 duration_ms=1.4", "request_id": "8dbc…ed"}
{"ts": "...", "level": "INFO", "logger": "app.chat", "message": "evidence selected", "request_id": "8dbc…ed", "chunk_ids": ["leave.md#0:00960ef0"], "used_tokens": 10, "dropped_chunks": 0}
{"ts": "...", "level": "INFO", "logger": "app.chat", "message": "chat end", "request_id": "8dbc…ed", "outcome": "stop", "total_ms": 5817.5, "timings_ms": {…}}
```

| Message | Level | When |
|---|---|---|
| `chat start` / `chat end` | INFO | Every request; `chat end` has the outcome, total time and stage timings |
| `corpus refreshed: …` | INFO | Every request: snapshot version and counts of documents added, modified, removed, unchanged, skipped |
| `corpus file skipped: path=… reason=…` | WARNING for problems (`not_utf8`, `too_large`, `changing`, …); DEBUG for policy skips (`hidden`, `unsupported_type`, …) | Each file not served, with its reason |
| `evidence selected` | INFO | Chunk ids chosen, tokens used, chunks dropped, or why there was no evidence |
| `model call failed` | WARNING | The model was unreachable, timed out or failed; has the error code and whether text was already sent |
| `output guard blocked instruction leak` | WARNING | The answer started reproducing the system instructions and was replaced by a refusal |
| `output guard removed reasoning` | INFO | Reasoning blocks were removed from an answer |
| `chat failed` | ERROR | An unexpected bug; includes the stack trace |
| `127.0.0.1:… - "GET /healthz HTTP/1.1" 200` (logger `uvicorn.access`) | INFO | Every HTTP request, including the Compose health check every 10 seconds; method, path and status only |
| `Transient error …` / `Failed to export spans batch …` (logger `opentelemetry…`) | WARNING / ERROR | Jaeger is not reachable; answers are unaffected |

**Every line logged while a request is handled carries its `request_id`**, from every module, so one request can be pulled out with:

```bash
docker compose logs --no-log-prefix app | grep <request id>
```

**Never logged:** the question text (only its length), prompts, document content, answer text, and `LLM_URL` (it could contain a password; the HTTP client's own request logging is switched off for this reason, TS-007). Logs record paths, counts, chunk ids, timings and codes.

## Traces

Each chat request is **one trace** in Jaeger, and **its trace id is the request id** shown under the answer, in every event and on every log line.

- **Open one:** http://127.0.0.1:16686, paste the id into "Lookup by Trace ID", or go to `http://127.0.0.1:16686/trace/<request id>`. Spans arrive about one second after the answer finishes.
- **Spans:** `chat.request` (whole request) with `corpus.refresh`, `evidence.selection`, `prompt.assembly` and `inference.stream` below it. Their durations are the per-stage latency; `inference.stream` is the model time.
- **What they record:** counts, timings, chunk ids and files, token counts labelled `reported` or `estimated (chars/4)`, model and backend (`llm.backend`, `server.address`, `server.port`), route, outcome and error codes. Never the question, prompt, document text, answer text, exception messages or `LLM_URL`. The full attribute list is in the [README](../README.md#logs-and-traces-observability).
- **Failed requests** have ERROR status on the failing stage and on the root, with `error.code`; an unexpected bug adds `error.type` (the exception class).
- **Retention:** Jaeger keeps traces **in memory only**. They are lost when the Jaeger container stops or restarts (ADR-008).
- **Running without Docker:** traces are created but not exported unless `OTLP_TRACES_URL` points at a local collector.

## Corpus refresh while running

There is no file watcher or background job: **every question starts by re-scanning `./data`** (ADR-005). Step by step in [Request and data flow](request-flow.md#1-corpus-refresh).

- **Add, edit, rename or delete files at any time.** The next question uses the current files; no restart, rebuild or re-index. A deleted file's text and chunks are gone from that question on.
- **When a change becomes visible:** on the first question that starts after the file has been unchanged for `CORPUS_SETTLE_SECONDS` (0.5 s) and is then read without changing. Until then the file is **not served at all**, not even its previous version, so an answer never mixes old and new text.
- **What was skipped, and why,** is shown in three places: the `skipped_files` list in the answer's sources (problems only), the `corpus file skipped` log lines, and `corpus.skipped_errors` on the trace.
- **Cost:** unchanged files are recognised by size, modification time, change time and inode, and not re-read. The search index is rebuilt only when something changed. This suits hundreds of files; it is not designed for very large corpora.

## Supported formats and limits

| | |
|---|---|
| **Formats** | UTF-8 text with extension `.txt` or `.md` (any letter case), in `./data` and its subfolders. A leading byte-order mark is removed. Markdown is read as plain text, never rendered |
| **Not supported** | Every other extension (PDF, Word, HTML, JSON, images, archives…): skipped as `unsupported_type`. Text that isn't valid UTF-8: `not_utf8`. Files containing NUL bytes: `binary_content` |
| **Never read** | Hidden files and folders (name starts with `.`), symbolic links, pipes, sockets and devices, anything resolving outside `/data` |
| **File size** | Up to 50 MB (`CORPUS_MAX_FILE_BYTES`); larger files are skipped without being read (`too_large`) |
| **File count** | Up to 500 supported files (`CORPUS_MAX_FILES`), the first 500 in sorted path order; the rest are `file_limit_exceeded` |
| **Empty files** | Zero bytes or whitespace only: skipped as `empty` |
| **Evidence per question** | 1,500 estimated tokens (`CONTEXT_TOKEN_BUDGET`), in chunks of about 800 characters |
| **Question** | Up to 8,000 characters, and it must fit the context window with the evidence |

Every skip reason, with its severity, is listed in the [README](../README.md#live-corpus).

## Restart and recovery

All app state (corpus snapshot, search index) is in memory and rebuilt from `./data` on demand, so **no restart needs a recovery step**.

| Situation | What happens | What to do |
|---|---|---|
| App restarted (`docker compose restart app`, or `down` then `up -d`) | The first question re-reads `./data` and rebuilds the index; later questions are fast again | Nothing |
| App exits at startup | Invalid configuration (exit code 2): every problem is listed in `docker compose logs app` | Fix `.env`, then `docker compose up -d` |
| App container crashes or is killed | Compose sets **no restart policy**, so it stays stopped | `docker compose up -d` |
| Ollama stopped or not started | `/readyz` returns 503 `unreachable`; questions get an `error` event `model_unavailable` (questions with no evidence are still answered, without the model). The app stays up and healthy | Start Ollama. The next question works: the app's HTTP client connects again on the next call, so no app restart is needed |
| Mac rebooted | Ollama's `OLLAMA_CONTEXT_LENGTH` and `OLLAMA_NO_CLOUD` settings are lost (TS-005) | Run the two `launchctl setenv` lines from [Setup](setup.md#3-prepare-the-model-server) again, then restart Ollama |
| Model removed from Ollama | `/readyz` returns 503 `model_not_found` | `ollama pull gemma3:1b` |
| Jaeger stopped or restarted | Answers keep working. While Jaeger is down, the exporter logs `Transient error …` (WARNING) and `Failed to export spans batch …` (ERROR), and those requests' traces are lost (verified 2026-10-09). After a restart, earlier traces are gone too (in memory only) | `docker compose up -d` starts it again |
| `./data` disappears while running | Empty corpus served, logged as `corpus_dir_missing`; the app keeps running | Restore the folder; the next question uses it |
| Code or Dockerfile changed | The running container still has the old code | `docker compose up -d --build` |
| `.env` changed | The container still has the old values | `docker compose up -d` (recreates the container; no rebuild needed) |

## Checking the system

```bash
scripts/verify.sh --container   # tests, lint, types, security scans of code, secrets, dependencies and both images
```

Each check's full output and a summary are saved under `docs/evidence/verify/` (REQ-120 – REQ-123). Re-run it after changes and before a review; pip-audit and Trivy need internet to fetch current vulnerability data (`--offline` skips them).

## Platform notes

| Platform | Status | Notes |
|---|---|---|
| **macOS, Docker Desktop (Intel)** | Verified | The bind mount passes size, modification time, change time and inode through, so every kind of change is detected. No file watcher is used, so Docker Desktop's file-event limitations don't apply. The Docker VM clock trails macOS by under 1 ms, which only delays when a new file is served by that much (TS-004) |
| **macOS, Apple Silicon** | Not tested | Same as Intel for the app. Docker Model Runner is available there and can replace Ollama with a `.env` change ([Setup](setup.md#docker-model-runner-not-tested)) |
| **Native Linux** | Not tested | `compose.yaml` maps `host.docker.internal` to the host. Ollama must listen on an address the container can reach (`OLLAMA_HOST=0.0.0.0`) and have `OLLAMA_CONTEXT_LENGTH=4096`. Change detection uses file metadata, not file events, so it should behave as on macOS (ADR-005) |
| **Windows, Docker Desktop** | Not tested | Expected to behave like macOS Docker Desktop; files in `./data` on the Windows file system are reached through Docker Desktop's file sharing |
