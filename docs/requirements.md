# Requirements Register

Source of truth: `ELENTA_Candidate_Assesment.pdf` (ELN-TA-BTR-003). Every requirement below cites the brief section it comes from. Nothing here is invented; where the brief uses "preferred" or "may", the level is **SHOULD** or **MAY** rather than **MUST**.

**Columns**

- **Level** — MUST / SHOULD / MAY, as worded in the brief.
- **Type** — F = functional, NF = non-functional (security, ops, quality), D = documentation, P = process / delivery.
- **Verify** — T = automated test, M = manual demo, I = inspection of docs/code/config, E = stored evidence (reports/output).

Status for all items is initially **Open**.

---

## Core (§1, §2, §4)

| ID | Requirement | Src | Level | Type | Verify |
|---|---|---|---|---|---|
| REQ-001 | Build a containerised chat service backed by Docker Model Runner (fallback rules in REQ-021). | §2 | MUST | F | M, I |
| REQ-002 | The service answers questions using documents under host `./data`, mounted into the application at `/data`. | §2 | MUST | F | T, M |
| REQ-003 | A request made after a corpus change has been detected uses the current corpus, without restarting or rebuilding the service. | §2 | MUST | F | T, M |
| REQ-004 | A reviewer can follow one request through the code, logs, and trace. | §4 | MUST | NF | M |
| REQ-005 | The front end is clean and usable. | §1 | MUST | F | M |

## 5.1 Reproducibility and offline operation

| ID | Requirement | Src | Level | Type | Verify |
|---|---|---|---|---|---|
| REQ-010 | Repository created from scratch with Git; committed incrementally; history shown in review. | §5.1 | MUST | P | I |
| REQ-011 | After the model has been pulled, a single `docker compose up` brings up the complete working system. | §5.1 | MUST | NF | M |
| REQ-012 | After the model pull, the system operates without cloud services, external APIs, hosted telemetry, or other internet dependencies. | §5.1 | MUST | NF | M, I |
| REQ-013 | All application configuration is provided through environment variables. | §5.1 | MUST | NF | I, T |
| REQ-014 | Provide `.env.example` (or equivalent configuration reference) with safe example values. | §5.1 | MUST | D | I |
| REQ-015 | The inference client obtains its endpoint and model identifier from `LLM_URL` and `LLM_MODEL`; switching between compatible local endpoints requires no code change. | §5.1 | MUST | NF | T, M |
| REQ-016 | Fail fast with a clear error when required configuration is missing or invalid. | §5.1 | MUST | NF | T |
| REQ-017 | Provide a practical health or readiness signal for the running service. | §5.1 | MUST | NF | T, M |

## 5.2 Model and endpoint

| ID | Requirement | Src | Level | Type | Verify |
|---|---|---|---|---|---|
| REQ-020 | Use a quantised GGUF model with ≤ 1 billion parameters (e.g. Docker Hub `ai/` namespace). | §5.2 | MUST | NF | I |
| REQ-021 | Docker Model Runner is the preferred backend; an OpenAI-compatible local endpoint (e.g. Ollama) is permitted where DMR is unavailable on the candidate hardware. | §5.2 | SHOULD | NF | I |
| REQ-022 | The application is endpoint-agnostic and does not depend on provider-specific behaviour outside the configured URL and model name. | §5.2 | MUST | NF | I, T |

## 5.3 Chat and progressive streaming

| ID | Requirement | Src | Level | Type | Verify |
|---|---|---|---|---|---|
| REQ-030 | Provide an HTTP endpoint or a minimal browser interface for chat. | §5.3 | MUST | F | M |
| REQ-031 | Responses stream progressively as generation occurs: the client receives multiple partial chunks before the complete answer is available. A fully server-generated, single-flush response does not qualify. | §5.3 | MUST | F | T, M |
| REQ-032 | Use an explicit streaming transport (SSE, chunked text, NDJSON, or WebSocket) and document its framing and error behaviour. | §5.3 | MUST | F, D | I, T |
| REQ-033 | A client disconnect or interrupted model stream does not leave the request or inference work running indefinitely. | §5.3 | MUST | NF | T |

## 5.4 Live corpus

| ID | Requirement | Src | Level | Type | Verify |
|---|---|---|---|---|---|
| REQ-040 | Mount host `./data` at `/data` in the application container, using documents of the candidate's choice. | §5.4 | MUST | NF | I |
| REQ-041 | Support at minimum UTF-8 `.txt` and `.md` files. State every supported format and any relevant limits. | §5.4 | MUST | F, D | T, I |
| REQ-042 | Files added, modified, renamed, or removed while running are reflected automatically in subsequent answers. No restart, rebuild, or manual re-index. | §5.4 | MUST | F | T, M |
| REQ-043 | The refresh mechanism (event-driven, polling, request-time, or hybrid) is documented, including the consistency model and the point at which a change is ready to serve. | §5.4 | MUST | D | I |
| REQ-044 | Removed documents stop contributing evidence; stale chunks or cached content do not survive deletion. | §5.4 | MUST | F | T, M |
| REQ-045 | Rapid changes and partially written files are handled without crashing or silently serving mixed versions. | §5.4 | MUST | NF | T |
| REQ-046 | Document platform-specific watcher or bind-mount behaviour, especially native Linux vs Docker Desktop. | §5.4 | MUST | D | I |

## 5.5 Grounding, source visibility, and context selection

| ID | Requirement | Src | Level | Type | Verify |
|---|---|---|---|---|---|
| REQ-050 | Use a deliberate method to select evidence from the current corpus; its limits are understood and documented. | §5.5 | MUST | F, D | T, I |
| REQ-051 | Each grounded answer identifies the source filename or stable chunk identifier used. | §5.5 | MUST | F | T, M |
| REQ-052 | Retrieved text is treated as evidence, not instructions; the answer stays within the service role even when a document contains prompt-like language. | §5.5 | MUST | NF | T, M |
| REQ-053 | When the corpus lacks enough evidence, the answer says so plainly rather than filling the gap from model memory. | §5.5 | MUST | F | T, M |
| REQ-054 | When current documents conflict and no defensible precedence rule resolves it, surface the conflict and identify the competing sources. | §5.5 | MUST | F | M |
| REQ-055 | Define an explicit context-token budget. Selection, ranking, summarisation, or truncation is deliberate and observable; silent context overflow is not acceptable. | §5.5 | MUST | F, NF | T, M |
| REQ-056 | Unreadable, unsupported, or corrupt files do not take down the service; the failure is recorded and the valid corpus continues to be used where possible. | §5.5 | MUST | NF | T, M |

## 5.6 Untrusted input and output safety

| ID | Requirement | Src | Level | Type | Verify |
|---|---|---|---|---|---|
| REQ-060 | Treat both document content and model output as untrusted; establish clear trust boundaries rather than relying on the model to police itself. | §5.6 | MUST | NF | I |
| REQ-061 | Implement and document an instruction hierarchy and prompt-assembly approach that keeps application instructions separate from retrieved evidence. | §5.6 | MUST | NF, D | T, I |
| REQ-062 | A malicious instruction inside a document cannot change the assistant role, suppress source attribution, expose hidden instructions, or manufacture an approval or other unsupported decision. | §5.6 | MUST | NF | T, M |
| REQ-063 | Never execute document content as code, a shell command, a template, or a tool instruction. | §5.6 | MUST | NF | I, T |
| REQ-064 | Restrict corpus reads to the `/data` boundary. Document handling of path traversal, symbolic links, hidden files, and unsupported file types. | §5.6 | MUST | NF, D | T, I |
| REQ-065 | Render output using safe text encoding; the browser client never places untrusted content into an executable HTML sink. If Markdown is supported, use a deliberately configured sanitisation path. | §5.6 | MUST | NF | T, M |
| REQ-066 | Run the application container as a non-root user. | §5.6 | MUST | NF | I, M |
| REQ-067 | Mount the corpus read-only, unless a different posture is necessary and explicitly justified. | §5.6 | MUST | NF | I |
| REQ-068 | Keep secrets, full prompts, unnecessary document bodies, and sensitive user content out of logs and trace attributes. | §5.6 | MUST | NF | T, I |

## 5.7 Standard failure modes

For each failure mode, the brief requires: (a) a deliberate **user-visible behaviour**, (b) a **structured diagnostic signal**, and (c) a **documented recovery or limitation**.

| ID | Requirement | Src | Level | Type | Verify |
|---|---|---|---|---|---|
| REQ-070 | For each relevant failure mode: user-visible behaviour + structured diagnostic + documented recovery/limitation. Never silently overflow, silently omit, or confidently invent. | §5.7 | MUST | NF, D | T, I |
| REQ-071 | Failure mode: corpus content exceeds the model context window. | §5.7 | MUST | NF | T, M |
| REQ-072 | Failure mode: model attempts to expose reasoning, hidden instructions, or chain-of-thought-like content instead of a concise final answer. | §5.7 | MUST | NF | T |
| REQ-073 | Failure mode: document is corrupt, incomplete, changing during ingestion, or unsupported. | §5.7 | MUST | NF | T, M |
| REQ-074 | Failure mode: documents contain conflicting or outdated claims. | §5.7 | MUST | NF | M |
| REQ-075 | Failure mode: corpus is empty, or the question is not supported by the corpus. | §5.7 | MUST | NF | T, M |
| REQ-076 | Failure mode: model endpoint is unavailable, times out, or fails after the response stream has started. | §5.7 | MUST | NF | T, M |

## 5.8 Observability

| ID | Requirement | Src | Level | Type | Verify |
|---|---|---|---|---|---|
| REQ-080 | Create one trace per chat request. | §5.8 | MUST | NF | T, M |
| REQ-081 | The trace spans corpus refresh, evidence/context selection, prompt/context assembly, and inference streaming. | §5.8 | MUST | NF | T, M |
| REQ-082 | Carry a request or trace identifier through structured logs and streamed error events. | §5.8 | MUST | NF | T |
| REQ-083 | Record per-stage latency and token counts for context/prompt and completion. Estimated counts are labelled as such and the method is documented. | §5.8 | MUST | NF, D | T, M |
| REQ-084 | Record selected source filenames or stable chunk IDs without copying unnecessary document bodies into telemetry. | §5.8 | MUST | NF | T |
| REQ-085 | Use local observability tooling only (OpenTelemetry, Phoenix, a local trace viewer, or a well-structured equivalent). | §5.8 | MUST | NF | I |

## 5.9 Architecture and code standard

| ID | Requirement | Src | Level | Type | Verify |
|---|---|---|---|---|---|
| REQ-090 | Keep ingestion, corpus state/indexing, context selection, prompt assembly, inference access, transport/streaming, and observability behind clear boundaries. | §5.9 | MUST | NF | I |
| REQ-091 | Every source file opens with a header comment block stating its purpose and place in the system. | §5.9 | MUST | NF | I, T |
| REQ-092 | Comments explain intent, assumptions, or trade-offs rather than narrating obvious mechanics. | §5.9 | MUST | NF | I |
| REQ-093 | Prefer explicit, human-readable code over unnecessary framework abstraction. | §5.9 | SHOULD | NF | I |
| REQ-094 | The candidate can explain any material line, dependency, configuration choice, and failure path during review. | §5.9, §6 | MUST | P | M |

## 5.10 Documentation (structured guide under `docs/`)

| ID | Requirement | Src | Level | Type | Verify |
|---|---|---|---|---|---|
| REQ-100 | Provide a structured guide under `docs/`; a README that only repeats startup commands is insufficient. | §5.10 | MUST | D | I |
| REQ-101 | **Overview**: purpose, supported use, scope, known limits. | §5.10 | MUST | D | I |
| REQ-102 | **Architecture**: components, boundaries, storage, model connection, and an architecture diagram that matches the implementation. | §5.10 | MUST | D | I |
| REQ-103 | **Request and data flow**: corpus refresh and chat flow, with a matching Mermaid diagram or exported image. | §5.10 | MUST | D | I |
| REQ-104 | **Setup**: prerequisites, model pull, configuration, startup, health check, first request. | §5.10 | MUST | D | I, M |
| REQ-105 | **Configuration**: every environment variable, required or not, safe example values, operational effect. | §5.10 | MUST | D | I |
| REQ-106 | **Operations**: logs and traces, corpus refresh behaviour, restart and recovery, supported formats, platform-specific notes. | §5.10 | MUST | D | I |
| REQ-107 | **Security**: trust boundaries, prompt-injection posture, rendering safety, filesystem scope, network assumptions, secrets, remaining risks. | §5.10 | MUST | D | I |
| REQ-108 | **Failure handling**: expected behaviour for each failure mode in the brief (REQ-071 – REQ-076). | §5.10 | MUST | D | I |

## 5.11 Decision and troubleshooting records

| ID | Requirement | Src | Level | Type | Verify |
|---|---|---|---|---|---|
| REQ-110 | Maintain a decision log while working: each significant decision with context, rationale, alternatives rejected, consequences. | §5.11 | MUST | D, P | I |
| REQ-111 | Maintain a troubleshooting log while working: every material issue and dead end with symptom, diagnosis, attempted actions, resolution or remaining limitation. | §5.11 | MUST | D, P | I |
| REQ-112 | Both records reflect the actual sequence of work, not a narrative reconstructed at the end. | §5.11 | MUST | P | I (git history) |

## 5.12 Verification evidence

| ID | Requirement | Src | Level | Type | Verify |
|---|---|---|---|---|---|
| REQ-120 | Include repeatable commands and evidence for the build and automated tests. | §5.12 | MUST | P | E |
| REQ-121 | Run appropriate code-quality checks (linting, formatting validation, or type checking). | §5.12 | MUST | P | E |
| REQ-122 | Run a relevant security check (dependency scanning, static analysis, secret scanning, and/or container-image scanning). | §5.12 | MUST | P | E |
| REQ-123 | Keep reports, command output, configuration, or CI evidence available to show during review. | §5.12 | MUST | P | E |

## §6 AI coding assistants and code ownership

| ID | Requirement | Src | Level | Type | Verify |
|---|---|---|---|---|---|
| REQ-130 | Review generated code and dependencies rather than accepting them without understanding; be able to open a file, explain a function/line, trace a request, compare alternatives, diagnose a failure, or make a small change live. | §6 | MUST | P | M |

## §3, §8 Delivery and review availability

| ID | Requirement | Src | Level | Type | Verify |
|---|---|---|---|---|---|
| REQ-140 | Complete within three calendar days of receipt; deliver a public GitHub repository link only, via the recruitment agency. | §3 | MUST | P | — |
| REQ-141 | During review, have available: the running system and local model; the repo and incremental Git history; complete `docs/` set, diagrams, decision log, troubleshooting log; build/test/quality/security evidence; structured logs and a local trace view; a clean way to add, edit and remove test documents under `./data`. | §3, §8 | MUST | P | M |

---

## Traceability: live review scenarios (§7) → requirements

| Scenario | Description | Requirements exercised |
|---|---|---|
| AC-S01 | Single Compose command + health signal | REQ-011, REQ-017 |
| AC-S02 | Progressive streamed answer with sources | REQ-031, REQ-032, REQ-051 |
| AC-S03 | Add / modify / remove doc at runtime | REQ-003, REQ-042, REQ-044 |
| AC-S04 | Prompt injection in document stays inert | REQ-052, REQ-061, REQ-062 |
| AC-S05 | Safe rendering of HTML/script content | REQ-065 |
| AC-S06 | Conflicting / incomplete documents | REQ-053, REQ-054, REQ-074 |
| AC-S07 | Context-budget behaviour on oversized corpus | REQ-055, REQ-071 |
| AC-S08 | Unreadable file; unavailable / interrupted model | REQ-056, REQ-073, REQ-076, REQ-033 |
| AC-S09 | Trace ↔ visible answer | REQ-080 – REQ-084 |
| AC-S10 | Code walk-through and Q&A | REQ-090, REQ-094, REQ-130 |
