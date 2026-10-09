# Setup

From a fresh machine to a first streamed answer (REQ-104). Verified on macOS 15 (Intel) with Docker Desktop, with Ollama running inside Compose; other platforms are covered in [Other platforms](#other-platforms) with what is and isn't tested.

Internet is needed **once**: to install the tools, download the images and the model, and build the app image. After that the system runs with no internet (REQ-012).

## 1. Prerequisites

| Tool | Version tested | Needed for |
|---|---|---|
| Docker Desktop | 4.94 (Engine 29.8.2, Compose v5.5.1) | Everything: the service, the trace viewer and, on Intel Macs, the model server |
| Git | any | Getting the code |
| [uv](https://docs.astral.sh/uv/) | 0.11.24 | Only for running the tests or the service outside Docker; it provides Python 3.12 |

Hardware: the model runs on the CPU and needs about 1.5 GB of memory (the tested Docker VM has 8 GB and 12 CPUs). Disk: about 10 GB for the Ollama image and 0.8 GB for the model. The first answer after a start takes about 4 seconds while the model loads; later answers start in well under a second.

**Which model server?** The brief prefers Docker Model Runner, but it isn't available on Intel Macs (TS-001), so on an Intel Mac **Ollama runs inside Compose** (ADR-020), which the brief permits. Other machines can use Docker Model Runner or a natively installed Ollama instead, by changing `.env` only; see [Other platforms](#other-platforms).

## 2. Get the code

```bash
git clone https://github.com/NarenderRajuB/elenta-ai.git
cd elenta-ai
```

## 3. Configure

```bash
cp .env.example .env
```

The example values are the Intel Mac setup and work as they are:

```bash
COMPOSE_PROFILES=ollama            # read by Docker Compose: also start the ollama service
LLM_URL=http://ollama:11434/v1     # the ollama service on the Compose network
LLM_MODEL=gemma3:1b
```

Every other setting is optional; see [Configuration](configuration.md). `.env` is git-ignored.

## 4. Start

```bash
docker compose up -d --build
```

The first run downloads the pinned images (Python base, Jaeger, and Ollama at 3.8 GB) and the Python dependencies, so it needs internet and takes a while. After that, **`docker compose up -d` alone starts the complete system** (REQ-011): the app on http://127.0.0.1:8000, the Jaeger trace viewer on http://127.0.0.1:16686 (both reachable from this machine only), and the Ollama model server, which is reachable only by the app.

## 5. Pull the model (once)

```bash
docker compose exec ollama ollama pull gemma3:1b   # about 800 MB, kept in the ollama-models volume
docker compose exec ollama ollama list             # gemma3:1b listed
```

`gemma3:1b` is a quantised GGUF model (Q4_K_M) with 999.89M parameters, inside the brief's 1-billion limit. It was chosen after an evaluation against two other small models (ADR-016, [injection evaluation](evidence/injection-eval.md)). The model stays in the volume across restarts and `docker compose down`; from now on nothing needs the internet (REQ-012).

The Ollama service is already configured with a **4,096-token context window**, which the app's token budget assumes (TS-005), and with its cloud features switched off. Its logs confirm both: `docker compose logs ollama | grep -E "CONTEXT_LENGTH|cloud disabled"`.

## 6. Add documents

Put `.txt` or `.md` files (UTF-8) in `./data`, in subfolders if you like. For a first test:

```bash
printf '# Leave policy\n\nEmployees receive 25 days of paid annual leave per calendar year.\n' > data/leave-policy.md
```

Files can be added, changed or removed at any time while the service runs; the next question uses them (a new file is served about half a second after it was last written).

## 7. Check health

```bash
docker compose ps                         # app and ollama show (healthy) after a few seconds
curl -i http://127.0.0.1:8000/healthz     # 200 {"status":"ok"}: the app is up
curl -i http://127.0.0.1:8000/readyz      # 200 {"status":"ready",...}: the model is reachable
```

`/healthz` only says the app is running. `/readyz` also checks that the model server answers and lists `LLM_MODEL`. If it returns **503**, the `reason` says why:

| `/readyz` reason | Meaning | Fix |
|---|---|---|
| `unreachable` | Nothing answering at `LLM_URL` | `docker compose ps ollama` (start it with `docker compose up -d`); check `LLM_URL` and `COMPOSE_PROFILES` in `.env` |
| `timeout` | Model server slower than `LLM_HEALTH_TIMEOUT_SECONDS` (3 s) | Check the host's load; raise the timeout |
| `http_error` | Model server answered with an error status | Check that `LLM_URL` ends in `/v1` |
| `invalid_response` | The answer isn't an OpenAI-style model list | `LLM_URL` points at something that isn't OpenAI-compatible |
| `model_not_found` | Server is up but `LLM_MODEL` isn't listed (exact match) | Step 5 (`docker compose exec ollama ollama pull gemma3:1b`), or fix the name |

If `docker compose ps` doesn't list the app, it exited at startup because its configuration is invalid: `docker compose logs app` lists every problem (exit code 2).

## 8. First request

**Browser:** open http://127.0.0.1:8000, type *How many days of annual leave do employees get?* and press **Ask**. The answer appears as it is generated, followed by its sources and the request id.

**Command line** (`-N` prints events as they arrive):

```bash
curl -N -X POST http://127.0.0.1:8000/chat -H 'Content-Type: application/json' \
     -d '{"question":"How many days of annual leave do employees get?"}'
```

Output from a run on 2026-10-09 with the document from step 6 (the request id differs every time; some `sources` fields left out):

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
docker compose down     # stops everything; traces are discarded; ./data and the downloaded model are kept
```

## Other platforms

The model server is chosen in `.env` only; no code or Compose change (REQ-015, ADR-020). Each trace records which backend served the request (`llm.backend`).

### Docker Model Runner (not tested)

For an **Apple Silicon Mac** (or any machine where Docker Model Runner is available). No code or Compose change is needed: only Docker Desktop settings and `.env` (REQ-015, ADR-020). Not tested here: the development machine is an Intel Mac, where Docker Model Runner is unavailable (TS-001).

1. **Enable Docker Model Runner:** Docker Desktop → Settings → AI → *Enable Docker Model Runner*.
2. **Pull the model.** `ai/gemma3:1b-q4_K_M` on Docker Hub is the same 1B model at the same Q4_K_M quantisation used in the Ollama setups:

   ```bash
   docker model pull ai/gemma3:1b-q4_K_M
   docker model ls          # note the exact model name it lists
   ```

3. **Set the context window to 4,096 tokens**, which the app's token budget assumes (TS-005):

   ```bash
   docker model configure --context-size 4096 ai/gemma3:1b-q4_K_M
   ```

   If your Docker version has no such option, set `LLM_CONTEXT_TOKENS` in `.env` to the model's actual context window instead (and keep the rules in [Configuration](configuration.md#rules-that-combine-settings)).
4. **Edit `.env`:** delete the `COMPOSE_PROFILES` line (so the Ollama container isn't started) and set:

   ```bash
   LLM_URL=http://model-runner.docker.internal/engines/v1
   LLM_MODEL=ai/gemma3:1b-q4_K_M     # exactly as `docker model ls` shows it
   ```

5. **Start and check:**

   ```bash
   docker compose up -d --build        # app + Jaeger; the app image builds natively for arm64
   curl -i http://127.0.0.1:8000/readyz
   ```

   `ready` means the app reaches Docker Model Runner and finds the model. `model_not_found` means `LLM_MODEL` doesn't exactly match the listed name; `unreachable` means Docker Model Runner isn't enabled.

Each trace records `llm.backend=docker-model-runner`. Answers can differ slightly from the Intel Mac at temperature 0 (TS-017); the code-level controls (C1–C6, C8, C9) behave the same on any backend.

### Ollama installed natively on the host

Supported, but no longer the default (ADR-013, ADR-020). In `.env`, **delete the `COMPOSE_PROFILES` line** and set `LLM_URL=http://host.docker.internal:11434/v1`. Ollama must be started separately, with the same two settings the Compose service sets; for the macOS Ollama app:

```bash
launchctl setenv OLLAMA_CONTEXT_LENGTH 4096   # pin the context window (TS-005)
launchctl setenv OLLAMA_NO_CLOUD 1            # disable Ollama's cloud features
# Quit Ollama from the menu bar and open it again so it picks these up.
# Both settings are lost on reboot: run the two lines again after restarting the Mac.
ollama pull gemma3:1b
ollama run gemma3:1b "hi" >/dev/null && ollama ps   # CONTEXT column must show 4096
```

### Native Linux (not tested)

- The default setup (Ollama in Compose) should work unchanged: the app reaches Ollama over the Compose network.
- With a natively installed Ollama: `compose.yaml` maps `host.docker.internal` to the host (`host-gateway`), but Ollama listens on `127.0.0.1` by default, which a container can't reach on Linux. It would have to listen on the Docker bridge address (or `0.0.0.0`, which also exposes it to the network), with `OLLAMA_CONTEXT_LENGTH=4096`.
- File-change detection doesn't depend on file-system events, so it should behave the same as on macOS (ADR-005).

## Without Docker (development)

```bash
uv sync
LLM_URL=http://localhost:11434/v1 LLM_MODEL=gemma3:1b CORPUS_DIR=./data uv run python -m app
```

`localhost` here because the app runs on the host itself. Tests: `uv run pytest` (no Ollama or internet needed); the full verification run is `scripts/verify.sh`, described in the [README](../README.md#code-quality-and-security-checks-req-120123).
