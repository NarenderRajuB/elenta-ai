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
- **Model server:** Ollama in its own container, serving `gemma3:1b`, started by the same `docker compose up` (ADR-020). Docker Model Runner is the brief's preferred backend but is not available on the development machine, an Intel Mac (TS-001); switching to it, or to a natively installed Ollama, is a change to `.env` only.
- **Trace viewer:** Jaeger, in its own container, keeping traces in memory.
- **Corpus:** host `./data`, mounted read-only at `/data`.

Details: [Architecture](architecture.md).

## Known limits

These are deliberate trade-offs or measured weaknesses, each recorded where it was decided or found.

**Answers and evidence**
- **Exact-word matching only.** No synonyms ("holiday" does not find "leave"), no stemming ("policy" does not find "policies"), English stop words only (ADR-007).
- **Small model.** A model of at most 1B parameters sometimes answers from the first matching evidence block and misses the rest (TS-008).
- **Conflicts are flagged, not detected.** When two or more documents match a question about equally well, a notice names them above the answer (ADR-019). Code can't tell whether they actually disagree, and the model's answer may still give only one of their values (TS-008).
- **Token counts for the prompt are estimates** (characters ÷ 4) unless the model server reports real counts; estimates undercount for scripts like Chinese or Japanese (ADR-007).
- **Questions are limited to 8,000 characters**, and must also fit the model's context window with the evidence; a question that doesn't fit is rejected, never truncated (REQ-055).

**Safety**
- **Prompt injection is reduced, not eliminated.** Code-level controls stop documents from changing the role, removing sources, leaking the instructions, adding reasoning or getting an approval confirmed (the service never confirms approvals, ADR-021). The remaining risk is the model repeating other instruction-like claims in its own words; the evaluation measures it but cannot rule it out, and results vary by model and hardware (ADR-006, TS-017).
- **The instruction-leak guard catches verbatim or near-verbatim copying**, not paraphrase (ADR-006 C4).
- Full list of controls and remaining risks: [Security](security.md).

**Corpus**
- **A file being written is not served at all** until it has been unchanged for the settle window (0.5 s) and read cleanly, not even its previous version (ADR-005).
- **The corpus is re-scanned on every question.** This is fast for hundreds of files; it is not designed for very large corpora.

**Operations**
- **Traces are lost when the Jaeger container restarts** (in-memory storage, ADR-008).
- **Verified on an Intel Mac with Docker Desktop only.** Apple Silicon, native Linux and Docker Model Runner are configured for but not tested (TS-001, ADR-020).
- **The model must be pulled once** into the Ollama container (`docker compose exec ollama ollama pull gemma3:1b`); until then `/readyz` reports `model_not_found`. The Ollama image is large (3.8 GB download) and its binary has known vulnerabilities only an upstream release can fix; it publishes no port (TS-016).
