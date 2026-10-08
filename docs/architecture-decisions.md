Architectu# Architecture Decision Records

Decision log required by brief §5.11 (REQ-110, REQ-112). Entries are appended **in the order decisions are made**. An entry is never rewritten after acceptance; if a decision changes, a new ADR supersedes it and the old one is marked `Superseded by ADR-xxx`.

**Statuses**: `Proposed` (drafted, awaiting candidate acceptance) · `Accepted` · `Superseded` · `Rejected`.

> **Current state (2026-10-08):** ADRs were drafted from the brief before any code was written and start as `Proposed`. Each must be explicitly accepted (or changed) by the candidate before the related feature is implemented. Accepted so far: ADR-001, ADR-002, ADR-005, ADR-006, ADR-007, ADR-011, ADR-012, ADR-013, ADR-014, ADR-015. Rejected: ADR-003.

Template:

```
## ADR-NNN: Title
- Status / Date / Requirements
- Context
- Decision
- Rationale
- Alternatives rejected
- Consequences (positive, negative, follow-ups)
```

---

## ADR-001: Python 3.12 with FastAPI + Uvicorn for the HTTP service

- **Status:** Accepted by the candidate on 2026-10-08 · **Date:** 2026-10-08
- **Requirements:** REQ-030, REQ-031, REQ-033, REQ-093, REQ-094

**Context.** We need an HTTP server that can stream responses, detect client disconnects, and serve a static page. The brief prefers "explicit, human-readable code over unnecessary framework abstraction" (§5.9).

**Decision.** Python 3.12, FastAPI on Uvicorn. Use FastAPI only for routing, request validation and `StreamingResponse`; no dependency-injection trees, no background-task framework, no ORM.

**Rationale.** Async streaming generators map directly onto the token stream; `request.is_disconnected()` and generator cancellation give a clear disconnect story (REQ-033). FastAPI is widely known, so reviewers can follow it.

**Alternatives rejected.**
- *Standard library `http.server`*: no async streaming, manual disconnect handling — more code to defend, not less.
- *Plain Starlette*: viable and lighter; rejected only because FastAPI's request-body validation removes hand-written parsing. (Revisit if FastAPI adds nothing we use.)
- *Flask*: sync-first; streaming with cancellation is awkward.

**Consequences.** + Small, conventional code. − Pulls in Pydantic transitively. Follow-up: pin versions; include in dependency scan (REQ-122).

---

## ADR-002: Call the model via raw HTTP to the OpenAI-compatible `/chat/completions` endpoint using `httpx`

- **Status:** Accepted by the candidate on 2026-10-08, with the offline condition below · **Date:** 2026-10-08
- **Requirements:** REQ-015, REQ-022, REQ-031, REQ-033, REQ-076

**Context.** Endpoint and model must come only from `LLM_URL` and `LLM_MODEL`; no provider-specific behaviour (§5.1, §5.2). DMR and Ollama both expose an OpenAI-compatible API.

**Decision.** A single inference module issues `POST {LLM_URL}/chat/completions` with `stream: true` using `httpx.AsyncClient`, parses the `data:` SSE lines, and yields content deltas. Explicit connect/read timeouts come from env vars. Closing the response on cancellation aborts upstream work.

**Rationale.** ~50 lines of explicit code we fully own; every byte on the wire is visible and explainable. No SDK-specific retries, headers or behaviours hidden from us.

**Alternatives rejected.**
- *`openai` Python SDK*: works, but adds a large dependency with its own retry/timeouts behaviour that must then be explained and configured.
- *LangChain / LlamaIndex*: heavy abstraction; directly contrary to §5.9.
- *Ollama/DMR native APIs*: provider-specific — violates REQ-022.

**Offline condition (added on acceptance, 2026-10-08).** The candidate required that, once installed, `httpx` works with no internet access. How this is met:
- Installation (`uv sync`) is the only online step. Packages land in `.venv` and uv's cache; `uv sync --offline` works afterwards. The container image will bake them in at build time.
- `httpx` has no telemetry or update checks. Its only outbound traffic is the requests we make.
- The client is created with `trust_env=False`, so `HTTP(S)_PROXY`, `NETRC` and similar environment settings cannot redirect traffic away from `LLM_URL`. This is tested in `tests/test_health.py`.

**Consequences.** + Endpoint-agnostic, testable with a fake HTTP server. − We own SSE parsing edge cases. Follow-up: whether to request `stream_options.include_usage` (an OpenAI-standard field) for reported token counts — see ADR-007.

---

## ADR-003: Docker Model Runner as default backend with a ≤1B quantised GGUF model

- **Status:** Rejected — DMR not available on the candidate hardware; superseded by ADR-013 (see TS-001) · **Date:** 2026-10-08
- **Requirements:** REQ-001, REQ-011, REQ-020, REQ-021

**Context.** DMR is preferred (§5.2); model must be quantised GGUF ≤1B. The candidate's machine is macOS with Docker Desktop.

**Decision.** Use DMR, reached from the app container via its OpenAI-compatible base URL, configured only through `LLM_URL`. Model: a small instruct model from Docker Hub `ai/` (candidates to test: a ~0.5B–1B Qwen, Llama 3.2 1B or Gemma 3 1B quantised tag). The exact tag is chosen after pulling and testing streaming on the machine, then recorded here.

**Rationale.** Matches the preferred backend; small models start fast and run on a laptop.

**Alternatives rejected.**
- *Ollama as default*: permitted only where DMR is unavailable; kept as documented fallback via `LLM_URL`/`LLM_MODEL`.
- *Models >1B*: violate REQ-020.

**Consequences.** + Native fit for the brief. − Small models follow instructions weakly, which is why safety must be enforced outside the model (ADR-006). Follow-up: decide whether Compose declares the model via the top-level `models:` element or the app simply receives `LLM_URL`; record the exact DMR URL used from inside a container.

---

## ADR-004: Server-Sent Events over `POST /chat` as the streaming transport

- **Status:** Proposed · **Date:** 2026-10-08
- **Requirements:** REQ-031, REQ-032, REQ-033, REQ-076, REQ-082

**Context.** Need explicit, documented framing including errors mid-stream (§5.3).

**Decision.** `POST /chat` returns `text/event-stream`. Named events, each a single JSON `data:` line:
`meta` (request_id, trace_id) → `sources` (selected source IDs, budget/truncation info) → `token` (repeated) → `done` (finish reason, token counts) **or** `error` (code, message, request_id). Exactly one terminal event per stream. The browser reads it with `fetch()` + `ReadableStream` (since `EventSource` only supports GET).

**Rationale.** SSE is plain text, inspectable with `curl -N`, and the brief names it explicitly. Sending `sources` as a structured event guarantees attribution even if the model omits it (supports REQ-051, REQ-062).

**Alternatives rejected.**
- *WebSocket*: bidirectional, unnecessary; harder to show with curl.
- *NDJSON*: equally viable; SSE chosen for its standard framing and named events.

**Consequences.** + Errors after stream start have a defined shape. − Proxies can buffer SSE; we set `Cache-Control: no-cache` and `X-Accel-Buffering: no` and avoid any compression middleware.

---

## ADR-005: Request-time corpus refresh using a stat fingerprint, with stable-read checks

- **Status:** Accepted by the candidate on 2026-10-08, with the decisions below · **Date:** 2026-10-08
- **Requirements:** REQ-003, REQ-042, REQ-043, REQ-044, REQ-045, REQ-046, REQ-056, REQ-064, REQ-073

**Context.** Corpus may change at any time; deletions must not leave stale chunks; partially written files must not produce mixed versions; Docker Desktop bind mounts on macOS do not reliably propagate inotify events into Linux containers.

**Decision.** At the start of every chat request (inside the "corpus refresh" span), walk `/data`, and for each candidate file compare `(path, size, mtime_ns)` against the in-memory index. Re-read only new/changed files; drop index entries for paths no longer present. A file is accepted only if `stat` is identical before and after reading **and** it decodes as UTF-8; otherwise it is excluded for this request with a structured diagnostic and retried next request. Index is rebuilt as a new immutable snapshot and swapped atomically, so a request always sees one consistent corpus version. Path rules: resolve real path and require it to stay under `/data`; skip symlinks *(policy to confirm)*, hidden files/dirs, and unsupported extensions, each with a diagnostic.

**Consistency model / ready-to-serve point.** A change is served by the first request that **starts** after the file's write has finished and its size/mtime are stable across our read. Requests are strongly consistent with the filesystem as observed at their start.

**Rationale.** Works identically on native Linux and Docker Desktop (no watcher dependency); trivially explains deletion semantics; easy to test.

**Alternatives rejected.**
- *`watchdog`/inotify watcher*: unreliable through Docker Desktop bind mounts; adds a thread and race conditions.
- *Background polling*: introduces a staleness window and the question "has the poller run yet?".
- *Hybrid*: more moving parts than a small corpus needs.

**Consequences.** + Simple, deterministic, platform-independent. − Per-request cost proportional to file count (one `stat` each); acceptable for a small corpus — enforce a documented max file count / max file size. − An mtime-preserving same-size overwrite could be missed; mitigation to consider: include a content hash for small files. Document limits.


**Candidate decisions on acceptance (2026-10-08).**
- Symlinks: **skip all** (files and directories), even if the target is inside `/data`.
- Limits: **100 MB per file** (`CORPUS_MAX_FILE_BYTES`, the candidate's choice; the original proposal was 1 MB) and **500 files** (`CORPUS_MAX_FILES`). Files over a limit are skipped and reported (`too_large`, `file_limit_exceeded`). The count cut-off is deterministic: the first 500 paths in sorted order.

**Implementation details added during the build (2026-10-08; open to the candidate's veto).**
- **Settle window** `CORPUS_SETTLE_SECONDS` (default 0.5 s, `0` disables). A file modified more recently is skipped as `settling` and picked up on a later request. Without this, a slow copy (likely with 100 MB files) could be read half-written while its size happens to be stable during our read.
- **While a file is changing, it is excluded, not served from cache.** Only content just read in full in its current state is served. Simpler to defend than "sometimes the previous version".
- **Fingerprint = (size, mtime_ns, ctime_ns, inode).** ctime can't be set by `touch`/`cp -p`, so a same-size edit with a restored mtime is still detected. The inode catches atomic replace-by-rename. Verified through the Docker Desktop bind mount.
- **Stable read:** the fingerprint at scan, at `fstat` before and after the read, and at `stat` of the path after the read must all match, and the bytes read must equal the size. Otherwise the file is skipped as `changing`.
- **Never opened:** pipes, sockets and devices (`not_regular_file`); opening a FIFO blocks. Files are opened with `O_NOFOLLOW | O_NONBLOCK`. The real path is re-checked to be inside the root before opening (`outside_root`), in case a directory is swapped for a symlink.
- **Content rules:** strict UTF-8 (a leading BOM is removed); NUL bytes → `binary_content`; empty, whitespace-only or BOM-only → `empty`. Extensions `.txt`/`.md` are matched case-insensitively.
- **Hidden** = any path component starting with `.`; hidden directories are not descended into.
- **Startup:** `CORPUS_DIR` must exist and be a directory, or the process exits with code 2. If it disappears at runtime, refresh reports `corpus_dir_missing` and serves an empty corpus.
- **Concurrency:** refresh is serialised with a lock and runs in a worker thread (blocking file I/O off the event loop). Each refresh publishes one immutable snapshot.
- **Logging:** one summary line per refresh (counts, version, duration) and one line per skip (path and reason). `error` reasons log at WARNING, policy skips at DEBUG. Document content is never logged.

**Amendment (2026-10-08, later the same day): per-file limit lowered to 50 MB.** After seeing the measured indexing cost (ADR-007), the candidate changed `CORPUS_MAX_FILE_BYTES` from 100 MB to **50 MB** (52,428,800 bytes). The 100 MB text above is kept as the original decision record.

**Verified through the Docker Desktop for Mac bind mount (2026-10-08):** add, modify, same-size edit with restored mtime, atomic replace, rename and delete are each reflected on the next refresh. Stress test: 200/200 write-then-refresh cycles correct after the TS-004 fix. Host/VM clock skew measured at under 1 ms (TS-004).
---

## ADR-006: Trust boundaries and prompt assembly enforced in code, not delegated to the model

- **Status:** Accepted by the candidate on 2026-10-08 · **Date:** 2026-10-08 · **Revised:** 2026-10-08. At the candidate's request, every control is now traced to the brief's own wording; items outside this decision's scope were moved out; one uncovered brief requirement was added.
- **Requirements:** REQ-052, REQ-060, REQ-061, REQ-062, REQ-063, REQ-072

**Context.** The brief, §5.6: "Both document content and model output are untrusted. Your design must establish clear trust boundaries rather than relying on the model to police itself." A ≤1B model (REQ-020) cannot be relied on to resist instructions embedded in documents, so each control below is enforced by application code. Prompt wording is used only as an additional layer.

**Decision.** Each control lists the brief text it implements.

| # | Control (enforced in code) | Brief source (quoted) | REQ |
|---|---|---|---|
| C1 | **Instruction hierarchy.** The `system` message contains only fixed application instructions defined in code. Evidence goes in a separate message, each chunk inside a delimited block labelled with its chunk ID. The user's question comes last. No document or user text is ever placed in the `system` message. | §5.6 "Implement and document an instruction hierarchy and prompt-assembly approach that keeps application instructions separate from retrieved evidence." §5.5 "Treat retrieved text as evidence, not as instructions." | 061, 052 |
| C2 | **Evidence cannot break out of its block.** Any occurrence of the block delimiters inside document text is neutralised before assembly, so a document cannot close its own block and pose as application instructions. | §5.6 same sentence ("keeps application instructions separate from retrieved evidence"). This is how the separation in C1 is made to hold. | 061 |
| C3 | **Attribution does not depend on the model.** The sources sent to the client are computed by selection code (ADR-007) and sent as structured data, independent of the model's text. | §5.6 "A malicious instruction inside a document must not … suppress source attribution." §5.5 "Each grounded answer must identify the source filename or stable chunk identifier used." | 062, 051 |
| C4 | **Hidden instructions are not exposed.** The streamed output is checked against the `system` instructions. If the answer reproduces a substantial verbatim part of them, the stream is stopped and replaced with a fixed refusal, and the event is logged. | §5.6 "must not … expose hidden instructions." §5.7 "The model attempts to expose … hidden instructions." | 062, 072 |
| C5 | **Reasoning is not shown.** The streamed output passes a fixed filter that removes reasoning blocks (e.g. `<think>…</think>`) before they reach the client, and logs when it does. | §5.7 "The model attempts to expose reasoning … or chain-of-thought-like content instead of a concise final answer." | 072 |
| C6 | **No answer without evidence.** If selection finds no qualifying evidence (ADR-007), the service returns a fixed "not enough evidence" response **without calling the model**, so the model cannot fill the gap with an invented decision. | §5.5 "When the corpus does not contain enough evidence, say so plainly instead of filling the gap from model memory." §5.6 "must not … manufacture an approval or other unsupported decision." | 053, 062 |
| C7 | **Role and decisions stay fixed.** The `system` instructions state the service role, that evidence is data and never instructions, that answers must be based only on the evidence, and that no approval or decision may be stated unless the evidence states it. This is the prompt-level layer on top of C1–C6. | §5.6 "must not change the assistant role … or manufacture an approval or other unsupported decision." §5.5 "The answer must remain within the service role even when a document contains prompt-like language." | 062, 052 |
| C8 | **No execution.** Document text is handled only as a string: it is never passed to `eval`/`exec`, a template engine, a shell, or a tool/function-call mechanism, and the app defines no tools. | §5.6 "Never execute document content as code, a shell command, a template, or a tool instruction." | 063 |

**Moved out of this ADR (still required by the brief, decided elsewhere):**
- Browser rendering of untrusted text (§5.6 "Render output using safe text encoding…", REQ-065) belongs to ADR-010.
- Keeping full prompts and document bodies out of logs and traces (§5.6, REQ-068) belongs to the observability decision (ADR-008). C4 and C5 log only the fact and type of the event, never the text.

**Rationale.** C1–C6 and C8 are deterministic and testable without a model. C7 depends on the model and is therefore only an extra layer, as §5.6 requires ("rather than relying on the model to police itself").

**Alternatives rejected.**
- *Prompt-only defences*: rely on the model, which §5.6 rules out.
- *A second model to classify injections*: still a model policing a model, adds latency, and isn't asked for by the brief.

**Consequences / remaining risk (to be stated in the Security docs, REQ-107).**
- When evidence *is* found, C7 is the only control on what the model concludes from it. A small model may still paraphrase injected text or overstate what the evidence says. No deterministic check for "unsupported decision" in free text is proposed, because any keyword rule would be invented beyond the brief. This residual risk is documented, and review scenario 4 demonstrates the behaviour.
- C4 catches verbatim or near-verbatim leakage of the instructions, not paraphrase.
- C5 recognises known reasoning markers only.

**Implementation (2026-10-08).** `app/prompt.py` (C1, C2, C7, context check), `app/output_guard.py` (C4, C5), `tests/test_no_execution.py` (C8 static scan). C3 and C6 are wired in the chat feature from `Selection.source_files` and `Selection.sufficient`.
- Evidence block: `<<<EVIDENCE id="…" source="…" [truncated="true"]>>> … <<<END EVIDENCE>>>`. `<<<`/`>>>` in document text, file names and the question are replaced with `‹‹‹`/`›››`.
- C4: blocked if any 60 consecutive characters of the system prompt appear in the answer (lower-cased, whitespace collapsed). Only a tail that also occurs in the system prompt is held back, so ordinary text streams immediately.
- C5: `<think>` and `<thinking>` blocks (any case) are removed, including when split across chunks or left unclosed.
- Whole-prompt check: settings `LLM_CONTEXT_TOKENS` (4096) and `LLM_MAX_TOKENS` (512). The prompt may use context minus answer allowance; a larger prompt raises `PromptTooLarge`. At startup, instructions + evidence budget + 100 question tokens must fit, otherwise exit 2.
- **Live result:** see TS-006. C4/C5/C6 behaved as designed; C7 (prompt-only) failed once out of two runs on an injected "APPROVED", which is the remaining risk above. Decision on mitigation pending.

## ADR-007: Lexical (BM25) evidence selection with an explicit, estimated token budget

- **Status:** Accepted by the candidate on 2026-10-08 (defaults: 800-char chunks, 1500-token evidence budget; Ollama context pinned to 4096, see TS-005) · **Date:** 2026-10-08
- **Requirements:** REQ-050, REQ-051, REQ-053, REQ-055, REQ-071, REQ-075, REQ-083

**Context.** Need a deliberate, explainable selection method and an explicit context-token budget; "a simple approach is acceptable when its limits are understood and documented" (§5.5).

**Decision.**
- **Chunking:** split each file into fixed-size, paragraph-aware chunks. Chunk ID = `relative_path#index` plus a short content hash, so IDs are stable for identical content.
- **Ranking:** BM25 over chunk tokens, implemented in plain Python (~60 lines) — no embedding model.
- **Relevance floor:** if no chunk scores above a configured threshold, skip the model and return "insufficient evidence" (REQ-053, REQ-075).
- **Budget:** `CONTEXT_TOKEN_BUDGET` env var. Add chunks in rank order until the budget is reached; record included, dropped and truncated chunk IDs in the trace and in the `sources` event.
- **Token counting:** estimate with a documented heuristic (characters ÷ 4) labelled `estimated`; if the endpoint returns `usage` in the stream, record it labelled `reported`.

**Rationale.** Fully offline, no second model to pull, deterministic and explainable line by line.

**Alternatives rejected.**
- *Embeddings via DMR/Ollama*: better semantic recall but needs a second model, an embedding endpoint (provider variance) and a vector store — more to run and explain.
- *Exact tokenizer (e.g. HF `tokenizers`)*: needs tokenizer files matching the model, which differ per `LLM_MODEL`; breaks endpoint-agnosticism.
- *Send the whole corpus*: silent overflow — explicitly forbidden.

**Consequences.** + Simple, testable. − Misses synonyms/paraphrases; heuristic token count can be off for non-English text, so budget leaves a safety margin below the model's real context. Limits documented.


**Implementation (2026-10-08).** `app/tokens.py`, `app/chunking.py`, `app/index.py`, `app/selection.py`.
- **Chunk ID** = `<path>#<ordinal>:<sha256(chunk text)[:8]>`, e.g. `leave.md#0:139e712c`. The same content gives the same ID; any edit changes the hash, so a cited ID always pins the exact text the model saw.
- **Chunking:** split on blank lines, pack paragraphs up to `CHUNK_MAX_CHARS`, cut long paragraphs at the last whitespace (hard cut only if there is none). No overlap.
- **BM25:** k1=1.5, b=0.75 (standard, not configurable); Lucene's always-positive idf. English stop words are removed from the **question only**. Tokens = `\w+`, lower-cased. No stemming or synonyms.
- **Relevance floor** `SELECTION_MIN_SCORE` (default 0, meaning at least one meaningful word in common). If nothing qualifies, selection returns `empty_corpus`, `no_meaningful_terms` or `no_relevant_evidence`, so the "insufficient evidence" answer can be given **without asking the model**.
- **Budget fill:** rank order. A chunk that doesn't fit is dropped and the next is tried, so smaller chunks can use the space. The top chunk is truncated only if it alone exceeds the budget, and is marked `truncated`. The dropped count is always recorded, with the first 10 dropped IDs and scores listed.
- **Index cache:** rebuilt only when the snapshot version changes. Per-document chunk statistics are cached by (path, sha256); the cache dict is replaced on rebuild, which evicts deleted documents.

**Measured cost (2026-10-08, Intel i7-8850H).** A 19 MB file (28,000 chunks): read 0.10 s, index build **2.5 s**, selection 5 ms, peak memory **~630 MB**. Extrapolating linearly to the candidate's 100 MB per-file limit gives roughly **13 s** to index one such file and **~3 GB** of memory. The first request after such a file changes would wait for the rebuild. This is a known limitation, accepted with the 100 MB limit; lowering `CORPUS_MAX_FILE_BYTES` is the operational lever.

**Amendment (2026-10-08): limit lowered to 50 MB** by the candidate in response to this measurement (see ADR-005 amendment). At 50 MB the same extrapolation gives roughly **6–7 s** to index one maximum-size file and **~1.6 GB** of memory. That's still a known limitation for the first request after such a file changes, but it is half the cost.
---

## ADR-008: OpenTelemetry tracing exported to a local Jaeger container; JSON logs to stdout

- **Status:** Proposed · **Date:** 2026-10-08
- **Requirements:** REQ-004, REQ-080 – REQ-085, REQ-012, REQ-068

**Context.** One trace per request, stage spans, token counts, local tooling only, and a trace the reviewer can open live (§5.8, §7.9).

**Decision.** OpenTelemetry Python SDK with OTLP exporter to a `jaegertracing/all-in-one` service in the same Compose file (UI on localhost). Structured JSON logs to stdout via the standard `logging` module with a small JSON formatter; every log line carries `request_id` and `trace_id`.

**Rationale.** OTel is the standard named by the brief; Jaeger all-in-one is a single local container with an in-memory store and a UI.

**Alternatives rejected.**
- *Phoenix*: LLM-focused UI but heavier and pulls in more dependencies.
- *Write traces to a JSON file only*: zero extra containers, but no viewer to "open one request trace" convincingly.
- *Hosted backends (Honeycomb, Langfuse cloud, etc.)*: violate REQ-012/REQ-085.

**Consequences.** + Real trace UI for the demo. − Adds one container image that must be pulled before going offline (document in Setup). − Jaeger in-memory storage loses traces on restart (documented limitation). **Needs candidate confirmation** as it adds a runtime component.

---

## ADR-009: Conflict handling — no automatic precedence; surface conflicts with named sources

- **Status:** Proposed · **Date:** 2026-10-08
- **Requirements:** REQ-054, REQ-074

**Context.** "When current documents conflict and no defensible precedence rule resolves the conflict, surface the conflict and identify the competing sources" (§5.5). File mtime is not a defensible indicator of truth.

**Decision.** Define no automatic precedence rule. The system instruction tells the model to state disagreements between cited sources and name them; because every evidence block carries its chunk ID and the client always receives the structured `sources` list, the competing sources are always visible to the user.

**Rationale.** Any precedence rule we invent (newest file wins, alphabetical, etc.) would itself need defending and could be wrong.

**Alternatives rejected.**
- *Newest-mtime wins*: mtime reflects copy time, not authority.
- *Deterministic contradiction detection*: not feasible to do reliably and simply.

**Consequences.** − Conflict *detection* relies on the model's reading; a weak model may miss it. This is a stated limitation. Follow-up: consider an opt-in documented convention (e.g. a front-matter `effective_date`) only if the candidate decides it is defensible.

---

## ADR-010: Minimal static browser UI with plain-text rendering only

- **Status:** Proposed · **Date:** 2026-10-08
- **Requirements:** REQ-005, REQ-030, REQ-065

**Context.** UI must be clean and usable, and must never place untrusted content into an executable HTML sink.

**Decision.** One static HTML file + one vanilla JS file + one CSS file served by the app. All untrusted content (answer tokens, source names, error messages) is written with `textContent`. No Markdown rendering. Strict `Content-Security-Policy` header (no inline scripts).

**Rationale.** Removes the need for a sanitiser library and its configuration; trivially auditable (grep for `innerHTML` returns nothing).

**Alternatives rejected.**
- *React/Vue SPA*: build toolchain and dependencies disproportionate to the scope.
- *Markdown + DOMPurify*: nicer formatting, but adds a sanitiser whose config must be defended.

**Consequences.** + Minimal attack surface. − Answers render as plain text (line breaks preserved via CSS `white-space: pre-wrap`).

---

## ADR-011: Configuration via a single env-var module with fail-fast validation

- **Status:** Accepted by the candidate on 2026-10-08 · **Date:** 2026-10-08
- **Requirements:** REQ-013, REQ-014, REQ-015, REQ-016

**Decision.** One `config` module reads `os.environ` into a frozen dataclass at startup, validating types, ranges and URL shape; any problem raises a single error listing all invalid/missing variables and the process exits non-zero. `.env.example` documents every variable.

**Alternatives rejected.** *`pydantic-settings`*: capable but adds a dependency and implicit behaviour for something ~40 lines of explicit code can do.

**Consequences.** + Explicit and testable. − Hand-written validation must be kept in sync with docs (covered by a test comparing `.env.example` keys to config fields).

---

## ADR-012: Container security posture

- **Status:** Accepted by the candidate on 2026-10-08 · **Date:** 2026-10-08
- **Requirements:** REQ-066, REQ-067, REQ-064, REQ-012

**Decision.** Slim Python base image; app runs as a dedicated non-root UID; `./data:/data:ro`; no secrets baked into the image; only the app port (and trace UI port) published on localhost.

**Alternatives rejected.** *Read-write corpus mount*: not needed — the app never writes to `/data`.

**Consequences.** + Matches brief defaults with no justification needed. Follow-up: consider `read_only: true` root filesystem with a tmpfs if nothing needs to write.


**Implementation (2026-10-08).** Implemented in `Dockerfile` and `compose.yaml`, including the follow-up above and three further hardening lines. Each is one line of Compose and needed nothing from the app:
- Base image pinned by digest: `python:3.12-slim@sha256:05cda977…` (Python 3.12.15, amd64). A floating tag would make "works offline after the pull" unrepeatable.
- Two-stage build. The builder installs `uv==0.11.24` from PyPI (avoiding a second base image) and runs `uv sync --frozen --no-dev`. Runtime holds only the virtualenv and `app/`, so `pytest`/`httpx2` are absent.
- Runs as UID/GID 10001 (`app`). `read_only: true` root filesystem, with `tmpfs: /tmp` as the only writable path. `cap_drop: [ALL]` and `no-new-privileges:true`.
- Port published as `127.0.0.1:8000:8000`, so it's reachable from this machine only. Inside the container `APP_HOST=0.0.0.0` is fixed, because network exposure is controlled by the host-side binding.
- `.dockerignore` excludes `.env`, `data/`, `.git/`, `docs/` and `tests/`, so secrets and corpus documents never enter an image layer.
- The Compose healthcheck targets `/healthz` (liveness) via Python's `urllib`, since the slim image has no curl.

**Verified (2026-10-08):** image 194 MB; `id` → `uid=10001(app)`; `touch /data/x` and `touch /app/x` → `Read-only file system`; `/tmp` writable; `CapEff: 0000000000000000`; started with `--network none`, `/healthz` → 200. Encoded in `tests/test_container_config.py` and `tests/test_container_runtime.py`.
---

## ADR-013: Ollama (OpenAI-compatible endpoint) as backend, model `qwen2.5:0.5b` (Q4_K_M)

- **Status:** Accepted, with Option B (Ollama native on the host), chosen by the candidate on 2026-10-08 · **Date:** 2026-10-08
- **Requirements:** REQ-001, REQ-011, REQ-012, REQ-015, REQ-020, REQ-021, REQ-022, REQ-055, REQ-083
- **Supersedes:** ADR-003

**Context.** Development and review machine: MacBook Pro, Intel Core i7-8850H (x86_64), macOS 15.7.9, Docker Desktop 4.94.0, Compose v5.5.1. `docker model` is an unknown command and Docker Desktop ships no `docker-model` CLI plugin on this host. On macOS, Docker Model Runner only supports Apple Silicon (TS-001). The brief §5.2 permits "an OpenAI-compatible local endpoint such as Ollama" where DMR is unavailable on the candidate hardware. Ollama 0.35.1 is already installed on the host.

**Verified facts (2026-10-08, host curl against `http://localhost:11434/v1`):**
- `qwen2.5:0.5b`: 494.03M parameters, quantisation `Q4_K_M` (GGUF), native context length 32768. Satisfies REQ-020.
- `POST /v1/chat/completions` with `stream: true` returns standard OpenAI `chat.completion.chunk` SSE frames, one per token or small token group, ending with `data: [DONE]`. This gives genuine progressive streaming (REQ-031).
- With `stream_options.include_usage: true`, the last chunk includes standard `usage` (`prompt_tokens`, `completion_tokens`), so token counts can be recorded as `reported` (REQ-083). Ollama also adds a non-standard `timings` object. **We do not read it** (REQ-022).

**Decision.**
1. The backend is Ollama, reached only via `LLM_URL` (base URL ending `/v1`) and `LLM_MODEL=qwen2.5:0.5b`. The application code contains nothing Ollama-specific. Switching to DMR on Apple Silicon only needs a change to those two variables (REQ-015).
2. **Compose topology: Option B was chosen.** Both options are kept below as considered:
   - **Option A (rejected): Ollama runs as a Compose service** (`ollama/ollama` image, model stored in a named volume). One-time pull: `docker compose run --rm ollama ollama pull qwen2.5:0.5b` (exact command to be confirmed in Setup docs). After that, a single `docker compose up` starts app + model server + trace viewer. This satisfies REQ-011 literally. Server-side context length can be pinned in Compose with the server's own env var, so the app stays endpoint-agnostic.
   - **Option B: Ollama runs natively on the host**, and the app reaches it at `http://host.docker.internal:11434/v1`. This reuses the already-pulled model and needs no extra image. However, `docker compose up` would not start the model server, so the reviewer depends on a separately running host process. This is weaker against REQ-011.
   - **Why B (candidate's choice):** the model is already pulled and verified on the host. There is no extra image to pull or keep offline, and native host inference avoids the Docker Desktop VM's CPU/RAM limits on an Intel machine. The model server is treated as a host prerequisite, in the same way DMR is a host-side component in the preferred design.
   - **How REQ-011 is still honoured:** "the complete working system" means the application stack that `docker compose up` starts (app and trace viewer), plus a model endpoint that must already be serving. Setup docs will list `ollama serve` running with `qwen2.5:0.5b` pulled as a prerequisite, next to "model has been pulled". The app's readiness signal (REQ-017) must report clearly when the model endpoint is unreachable, so a missing host process shows up as a clear failure rather than a silent one.

**Rationale.** Ollama is the fallback the brief names explicitly. `qwen2.5:0.5b` is clearly under the 1B limit, is already pulled, and has been verified to stream through the standard API.

**Alternatives rejected.**
- *Docker Model Runner*: not available on Intel macOS (TS-001).
- *`llama3.2:1b`*: about 1.24B actual parameters, which arguably breaks "1 billion parameters or fewer".
- *`llama3.2:latest` / `qwen2.5:latest` / `gemma3:4b`* (already on the host): 3B, 7B and 4B, so they violate REQ-020.
- *`gemma3:1b`*: about 1.0B, a possible secondary candidate. Not tested; no need while qwen2.5:0.5b works.

**Consequences.**
- \+ Uses only the standard OpenAI-compatible contract, so behaviour is portable to DMR.
- − **Silent context truncation risk:** Ollama decides the effective context window server-side, not per request through the OpenAI API. If our prompt exceeds it, the server may truncate without telling us. Mitigation: `CONTEXT_TOKEN_BUDGET` must stay well below the server's effective context, and docs must state that value. With Option B this is a host-side Ollama server setting (e.g. a context-length env var when starting `ollama serve`; exact variable to be verified and documented). Ties to REQ-055 and REQ-071.
- − CPU-only inference on Intel. Latency to be measured and recorded.
- Option B: Ollama listens on `127.0.0.1:11434` by default. **Verified 2026-10-08:** the app container reaches it at `http://host.docker.internal:11434/v1` on Docker Desktop 4.94 for Mac (`/readyz` → 200 `ready` from inside Compose), with no change to Ollama's bind address. On native Linux, `host-gateway` maps to the Docker bridge, so Ollama there must listen on an address the bridge can reach (e.g. `OLLAMA_HOST=0.0.0.0`). That case is untested here and is a documented limitation.
- − Option B: Compose cannot health-check or start the model server. Its state is visible only through the app's readiness signal and logs.
- − Native Linux reviewers would need `extra_hosts: host.docker.internal:host-gateway` in Compose. To be documented in the platform notes (REQ-046 / REQ-106).
- **Offline posture (decided 2026-10-08):** Ollama 0.35.1 had `OLLAMA_NO_CLOUD=0`, so its cloud features (remote inference, web search) were enabled. This app never uses them, but the candidate chose to disable them (`OLLAMA_NO_CLOUD=1` via `launchctl`) so the model server, like the app, has no internet-dependent features (REQ-012). This is documented in README "Ollama setup".
- Follow-up: Ollama ships a default system prompt in the model (`You are Qwen…`). Our own `system` message replaces it per request; this needs verifying when prompt assembly is built (ADR-006).

---

## ADR-014: uv for Python environment and dependency locking

- **Status:** Accepted by the candidate on 2026-10-08 · **Date:** 2026-10-08
- **Requirements:** REQ-011, REQ-093, REQ-120, REQ-122

**Context.** We need a reproducible Python 3.12 environment with pinned dependencies, for local tests and later for the container image. On this machine `python3` is a shell alias to another project's Python 3.14 alpha virtualenv, so relying on bare `python3` would silently use the wrong interpreter.

**Decision.** Use `uv` (0.11.24 locally). `.python-version` pins 3.12, `pyproject.toml` declares dependencies, and `uv.lock` (committed) pins exact versions. Tests run with `uv run pytest`. Dependencies are added only when a feature needs them. As of this ADR the only one is `pytest` (dev).

**Rationale.** A single tool covers interpreter selection, the virtualenv and a lockfile. The lockfile gives an exact input for dependency scanning (REQ-122) and for the container build.

**Alternatives rejected.**
- *`venv` + `pip` + hand-pinned `requirements.txt`*: works, but has no lockfile with hashes for transitive dependencies, and interpreter selection is manual.
- *Poetry*: similar outcome, heavier tool.

**Consequences.** + Reproducible environment; the wrong interpreter can't be picked up by accident. − Reviewers need `uv` installed to run tests locally (to be listed in Setup). Follow-up: the Dockerfile must install from `uv.lock`, so the image matches the tested environment.

---

## ADR-015: Separate liveness and readiness endpoints; readiness probes the model endpoint

- **Status:** Accepted by the candidate on 2026-10-08, including the model-list check (`model_not_found`) · **Date:** 2026-10-08
- **Requirements:** REQ-016, REQ-017, REQ-022, REQ-068

**Context.** REQ-017 asks for "a practical health or readiness signal". Ollama runs natively on the host (ADR-013), so Compose cannot start or health-check it, and a stopped Ollama would otherwise only show up as a failed chat request.

**Decision.**
- `GET /healthz` (liveness) always returns `200 {"status":"ok"}` while the process serves HTTP. It checks nothing else.
- `GET /readyz` (readiness) calls the standard OpenAI-compatible `GET {LLM_URL}/models` with a configurable timeout (`LLM_HEALTH_TIMEOUT_SECONDS`, default 3s). It returns `200 ready` only if the endpoint answers **and** lists `LLM_MODEL` exactly. Otherwise it returns `503 not_ready` with one fixed reason code: `timeout`, `unreachable`, `http_error`, `invalid_response` or `model_not_found`.
- No exception text or URL appears in responses, because `LLM_URL` could contain credentials.
- The process entry point (`python -m app`) validates config before creating any socket. On error it prints the problem list to stderr and exits with code **2**, without a traceback.
- Optional `APP_HOST` (default `127.0.0.1`) and `APP_PORT` (default `8000`) control the bind address. Loopback is the default so a local run isn't exposed on the network; the container will set `0.0.0.0` explicitly.

**Rationale.** Liveness is kept separate so a model outage never causes a restart loop of a healthy app. The model-list check catches the common "Ollama running but model not pulled / misspelled" misconfiguration before a user hits it. Fixed reason codes are testable and safe to expose.

**Alternatives rejected.**
- *A single `/health` that includes the model check*: conflates "process is broken" with "dependency is down".
- *Reachability only*: misses a wrong or missing model until the first chat request.
- *Probing with a tiny chat completion*: slow on CPU, consumes inference, and gives no extra information.

**Consequences.** + Ollama state is visible without starting a chat. − An endpoint that lists model IDs differently from the chat `model` field would show `model_not_found`. This is a documented limitation; the fix would be configuration, not code. − FastAPI 0.143 brings in `opentelemetry-api` transitively. On its own that package is a no-op (`ProxyTracerProvider`, no exporter) and sends nothing. Real tracing comes only with ADR-008.

**Verified (2026-10-08, real Ollama on host):** `/healthz` → 200; `/readyz` → 200 `ready`; with `LLM_MODEL=qwen2.5:7b-typo` → 503 `model_not_found`; with no config → exit 2 and a clear message.

---

## Pending decisions (to be recorded as ADRs when made)

- Tooling for REQ-121/122 (e.g. `ruff`, `mypy`, `pip-audit`, `bandit`, image scan) — choose when setting up the project skeleton.
- Test strategy: fake OpenAI-compatible streaming server for deterministic tests.
