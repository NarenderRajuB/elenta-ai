# Overview

## Purpose

A small, local chat service that answers questions **only from a folder of documents** (`./data` on the host), streams its answer as it is generated, and names the documents it used. Everything runs on one machine: the model, the service, the trace viewer. After the one-time model and image downloads, nothing needs the internet (REQ-012).

It was built for the ELENTA AI Developer Practical Assessment. The brief values "a simple, well-structured implementation with clear limits and defensible decisions" over a larger system, and the design follows that: plain Python, no database, no second model for retrieval, and every decision recorded in the [architecture decisions](architecture-decisions.md).

## Supported use

- **One user on their own machine**, asking questions in a browser (http://127.0.0.1:8000) or with `POST /chat`.
- **A live corpus:** `.txt` and `.md` files in `./data`, which can be added, edited, renamed or removed while the service runs. The next question uses the current files; no restart, rebuild or re-index (REQ-003, REQ-042).
- **Grounded answers with sources:** each answer lists the files and chunk ids its evidence came from. If the documents don't contain the answer, the service says so without asking the model (REQ-051, REQ-053).
- **Following a request:** the request id shown under each answer is also the trace id in Jaeger (http://127.0.0.1:16686) and appears on every log line for that request (REQ-004).

## Scope

| In scope | Out of scope |
|---|---|
| UTF-8 `.txt` and `.md` files, up to 50 MB each and 500 files (configurable) | PDF, Word, HTML, images or any other format (skipped and reported) |
| Keyword (BM25) evidence selection within a fixed token budget | Embeddings, vector databases, semantic search |
| One question at a time per request; no conversation memory | Multi-turn chat history |
| Local use on loopback (`127.0.0.1`) | Authentication, multiple users, exposure to a network |
| A quantised model of at most 1 billion parameters (`gemma3:1b`) behind any OpenAI-compatible endpoint | Hosted model APIs, larger models |
| Markdown shown as plain text | Rendering Markdown or HTML from documents or answers |

## Components at a glance

- **App container** (`elenta-ai:local`): FastAPI service with the browser UI, the chat pipeline and health endpoints. Runs as a non-root user with a read-only filesystem.
- **Model server:** Ollama, running natively on the host, serving `gemma3:1b`. Docker Model Runner is the brief's preferred backend but is not available on the development machine (ADR-013, TS-001); switching is a change to `LLM_URL` only.
- **Trace viewer:** Jaeger, in its own container, keeping traces in memory.
- **Corpus:** host `./data`, mounted read-only at `/data`.

Details: [Architecture](architecture.md).

## Known limits

These are deliberate trade-offs or measured weaknesses, each recorded where it was decided or found.

**Answers and evidence**
- **Exact-word matching only.** No synonyms ("holiday" does not find "leave"), no stemming ("policy" does not find "policies"), English stop words only (ADR-007).
- **Small model.** A model of at most 1B parameters sometimes answers from the first matching evidence block and misses the rest (TS-008).
- **Conflicting documents are not reliably surfaced in the answer text.** Both sources are always listed, but no model tried surfaced the conflict in its answer in any evaluation run (TS-008, ADR-009). How to address this is an open decision.
- **Token counts for the prompt are estimates** (characters ÷ 4) unless the model server reports real counts; estimates undercount for scripts like Chinese or Japanese (ADR-007).
- **Questions are limited to 8,000 characters**, and must also fit the model's context window with the evidence; a question that doesn't fit is rejected, never truncated (REQ-055).

**Safety**
- **Prompt injection is reduced, not eliminated.** Code-level controls stop documents from changing the role, removing sources, leaking the instructions or adding reasoning to the answer. The remaining risk is the model repeating an instruction-like claim (for example an "approval") in its own words; the evaluation measures it but cannot rule it out (ADR-006, TS-006).
- **The instruction-leak guard catches verbatim or near-verbatim copying**, not paraphrase (ADR-006 C4).

**Corpus**
- **A file being written is not served at all** until it has been unchanged for the settle window (0.5 s) and read cleanly, not even its previous version (ADR-005).
- **The corpus is re-scanned on every question.** This is fast for hundreds of files; it is not designed for very large corpora.

**Operations**
- **Traces are lost when the Jaeger container restarts** (in-memory storage, ADR-008).
- **Verified on macOS with Docker Desktop only.** Native Linux is configured for (`host.docker.internal` mapping) but not tested; Docker Model Runner is not tested (TS-001).
- **Ollama must be started separately**; Compose cannot start or health-check it. `/readyz` reports when it is missing (ADR-015).
