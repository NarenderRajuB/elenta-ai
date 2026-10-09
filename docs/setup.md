# Setup

From a fresh machine to a first streamed answer (REQ-104). Verified on macOS 15 (Intel) with Docker Desktop; other platforms are covered in [Other platforms](#other-platforms) with what is and isn't tested.

Internet is needed **once**: to install the tools, pull the model and build the image. After that the system runs with no internet (REQ-012).

## 1. Prerequisites

| Tool | Version tested | Needed for |
|---|---|---|
| Docker Desktop | 4.94 (Engine 29.8.2, Compose v5.5.1) | Running the service and the trace viewer |
| Ollama | 0.40.1 | Serving the model on the host |
| Git | any | Getting the code |
| [uv](https://docs.astral.sh/uv/) | 0.11.24 | Only for running the tests or the service outside Docker; it provides Python 3.12 |

Hardware: the model needs about 1 GB of memory; it runs on CPU. The first token of an answer takes a few seconds on the tested Intel MacBook Pro.

**Why Ollama and not Docker Model Runner?** The brief prefers Docker Model Runner, but it is not available on Intel Macs (TS-001), so this setup uses Ollama, which the brief permits. The app works with either; see [Docker Model Runner](#docker-model-runner-not-tested).

## 2. Get the code

```bash
git clone https://github.com/NarenderRajuB/elenta-ai.git
cd elenta-ai
```

## 3. Prepare the model server

Ollama must use a **4,096-token context window**, which the app's token budget assumes (TS-005); otherwise Ollama picks a size from available memory and results vary between machines. Ollama's cloud features are switched off so the model server, like the app, has no internet-dependent features (ADR-013).

For the macOS Ollama app:

```bash
launchctl setenv OLLAMA_CONTEXT_LENGTH 4096   # pin the context window
launchctl setenv OLLAMA_NO_CLOUD 1            # disable Ollama's cloud features
# Quit Ollama from the menu bar and open it again so it picks these up.
# Both settings are lost on reboot: run the two lines again after restarting the Mac.

ollama pull gemma3:1b                          # one-time download, about 815 MB
ollama run gemma3:1b "hi" >/dev/null && ollama ps   # CONTEXT column must show 4096
```

`gemma3:1b` is a quantised GGUF model (Q4_K_M) with 999.89M parameters, inside the brief's 1-billion limit. It was chosen after an evaluation against two other small models (ADR-016, [injection evaluation](evidence/injection-eval.md)).

To confirm the running server picked up both settings:

```bash
ps eww -o command= -p "$(pgrep -f 'ollama serve' | head -1)" | tr ' ' '\n' | grep -E 'OLLAMA_(CONTEXT_LENGTH|NO_CLOUD)'
# expected: OLLAMA_CONTEXT_LENGTH=4096 and OLLAMA_NO_CLOUD=1
```

## 4. Configure

```bash
cp .env.example .env
```

The example values work as they are for this setup: `LLM_URL=http://host.docker.internal:11434/v1` (Ollama on the host, as seen from the container) and `LLM_MODEL=gemma3:1b`. Every other setting is optional; see [Configuration](configuration.md). `.env` is git-ignored.

## 5. Add documents

Put `.txt` or `.md` files (UTF-8) in `./data`, in subfolders if you like. For a first test:

```bash
printf '# Leave policy\n\nEmployees receive 25 days of paid annual leave per calendar year.\n' > data/leave-policy.md
```

Files can be added, changed or removed at any time while the service runs; the next question uses them.

## 6. Start

```bash
docker compose up -d --build
```

The first build downloads the pinned Python base image and the dependencies (internet needed). After that, **`docker compose up -d` alone starts the complete system** (REQ-011): the app on http://127.0.0.1:8000 and the Jaeger trace viewer on http://127.0.0.1:16686. Both are reachable from this machine only.

Ollama is not started by Compose; it must already be running (step 3).

## 7. Check health

```bash
docker compose ps                         # app shows (healthy) after a few seconds
curl -i http://127.0.0.1:8000/healthz     # 200 {"status":"ok"}: the app is up
curl -i http://127.0.0.1:8000/readyz      # 200 {"status":"ready",...}: the model is reachable
```

`/healthz` only says the app is running. `/readyz` also checks that the model server answers and lists `LLM_MODEL`. If it returns **503**, the `reason` says why:

| `/readyz` reason | Meaning | Fix |
|---|---|---|
| `unreachable` | Nothing answering at `LLM_URL` | Start the Ollama app (or `ollama serve`); check `LLM_URL` |
| `timeout` | Model server slower than `LLM_HEALTH_TIMEOUT_SECONDS` (3 s) | Check the host's load; raise the timeout |
| `http_error` | Model server answered with an error status | Check that `LLM_URL` ends in `/v1` |
| `invalid_response` | The answer isn't an OpenAI-style model list | `LLM_URL` points at something that isn't OpenAI-compatible |
| `model_not_found` | Server is up but `LLM_MODEL` isn't listed (exact match) | `ollama pull gemma3:1b`, or fix the name |

If `docker compose ps` doesn't list the app, it exited at startup because its configuration is invalid: `docker compose logs app` lists every problem (exit code 2).

## 8. First request

**Browser:** open http://127.0.0.1:8000, type *How many days of annual leave do employees get?* and press **Ask**. The answer appears as it is generated, followed by its sources and the request id.

**Command line** (`-N` prints events as they arrive):

```bash
curl -N -X POST http://127.0.0.1:8000/chat -H 'Content-Type: application/json' \
     -d '{"question":"How many days of annual leave do employees get?"}'
```

Output from a run on 2026-10-09 with the document from step 5 (the request id differs every time; some `sources` fields left out):

```text
event: meta
data: {"request_id":"9e1b8ba4202a5f7c273603fbd59e3db6"}

event: sources
data: {"request_id":"9e1b…db6","corpus_version":1,"documents":1,"skipped_files":[],"chunks":[{"id":"leave-policy.md#0:c84477d7","source":"leave-policy.md","score":1.274,"estimated_tokens":21,"truncated":false}],"files":["leave-policy.md"],"budget_tokens":1500,"used_tokens":21, …}

event: token
data: {"text":"2"}

event: token
data: {"text":"5 d"}

event: token
data: {"text":"ays"}

event: done
data: {"request_id":"9e1b…db6","finish_reason":"stop","model_called":true,"tokens":{"prompt":338,"completion":4,"source":"reported"},"prompt_tokens_estimated":350,"reasoning_blocks_removed":0,"timings_ms":{"corpus_refresh_ms":1.5,"selection_ms":0.8,"prompt_assembly_ms":0.0,"first_token_ms":4066.9,"inference_ms":4142.4}}
```

The several `token` events are the progressive stream. To see the same request in the trace viewer, open `http://127.0.0.1:16686/trace/<request id>`.

## 9. Stop

```bash
docker compose down     # stops the app and Jaeger; traces are discarded, ./data is untouched
```

## Other platforms

### Native Linux (not tested)

- `compose.yaml` maps `host.docker.internal` to the host (`host-gateway`), so the same `LLM_URL` works.
- Ollama listens on `127.0.0.1` by default, which a container can't reach on Linux. Start it with `OLLAMA_HOST=0.0.0.0` (or the Docker bridge address), and `OLLAMA_CONTEXT_LENGTH=4096` (ADR-013).
- File-change detection doesn't depend on file-system events, so it should behave the same as on macOS (ADR-005).

### Docker Model Runner (not tested)

On a machine where Docker Model Runner is available (for example Apple Silicon with it enabled in Docker Desktop), pull a quantised model of at most 1B parameters from Docker Hub's `ai/` namespace, then set in `.env`:

```bash
LLM_URL=http://model-runner.docker.internal/engines/v1
LLM_MODEL=<the model name as listed by `docker model ls`>
```

No code change is needed (REQ-015). The context-window note in step 3 applies here too: the app assumes 4,096 tokens (`LLM_CONTEXT_TOKENS`). Each trace records which backend served the request (`llm.backend`).

## Without Docker (development)

```bash
uv sync
LLM_URL=http://localhost:11434/v1 LLM_MODEL=gemma3:1b CORPUS_DIR=./data uv run python -m app
```

`localhost` here because the app runs on the host itself. Tests: `uv run pytest` (no Ollama or internet needed); the full verification run is `scripts/verify.sh`, described in the [README](../README.md#code-quality-and-security-checks-req-120123).
