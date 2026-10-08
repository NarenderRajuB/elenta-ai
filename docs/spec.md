# Specification Summary — ELENTA AI Developer Practical Assessment

| Field | Value |
|---|---|
| Source of truth | `ELENTA_Candidate_Assesment.pdf` (ref. ELN-TA-BTR-003, 7 pages) |
| Purpose of this file | A faithful, structured restatement of the brief, plus clearly separated interpretations and open questions |
| Rule | If this file and the PDF disagree, **the PDF wins**. Interpretations are marked as such and are not requirements. |

Requirement IDs (`REQ-xxx`) are defined in [requirements.md](requirements.md). Verification is defined in [acceptance-criteria.md](acceptance-criteria.md). Decisions are recorded in [architecture-decisions.md](architecture-decisions.md).

---

## 1. What is being built (brief: Overview, §2)

A **small, local, containerised chat service** that:

1. Answers questions from a **live document corpus** in host `./data`, mounted into the app container at `/data`.
2. Is backed by **Docker Model Runner** (preferred) or another OpenAI-compatible local endpoint (e.g. Ollama) when DMR is unavailable.
3. **Streams** its response progressively.
4. Picks up corpus changes (add / modify / rename / remove) **without restart, rebuild or manual re-index**.
5. Stays **safe around untrusted content** (documents *and* model output).
6. Is **observable** (one trace per request, structured logs).
7. Can be **fully explained** by the candidate during a live review.

## 2. What is being evaluated (brief: §1)

Practical engineering judgement across:

- local AI architecture
- retrieval and grounding
- secure handling of untrusted content
- operability
- observability
- code ownership
- a clean and usable front end

Guiding principle stated in the brief:

> "A simple, well-structured implementation with clear limits and defensible decisions is preferred over a larger system that is difficult to run, observe, or explain."

## 3. Required outcome (brief: §4)

A reviewer must be able to:

1. Start the complete system.
2. Ask a grounded question.
3. See genuine progressive streaming.
4. Change the corpus at runtime and see the next answer reflect it.
5. Follow one request through the **code, logs, and trace**.

## 4. Functional and technical requirements — section map

| Brief section | Topic | REQ range |
|---|---|---|
| §1, §2, §4 | Core purpose and outcome | REQ-001 – REQ-005 |
| §5.1 | Reproducibility and offline operation | REQ-010 – REQ-017 |
| §5.2 | Model and endpoint | REQ-020 – REQ-022 |
| §5.3 | Chat and progressive streaming | REQ-030 – REQ-033 |
| §5.4 | Live corpus | REQ-040 – REQ-046 |
| §5.5 | Grounding, source visibility, context selection | REQ-050 – REQ-056 |
| §5.6 | Untrusted input and output safety | REQ-060 – REQ-068 |
| §5.7 | Standard failure modes | REQ-070 – REQ-076 |
| §5.8 | Observability | REQ-080 – REQ-085 |
| §5.9 | Architecture and code standard | REQ-090 – REQ-094 |
| §5.10 | Documentation | REQ-100 – REQ-108 |
| §5.11 | Decision and troubleshooting records | REQ-110 – REQ-112 |
| §5.12 | Verification evidence | REQ-120 – REQ-123 |
| §6 | AI assistants and code ownership | REQ-130 |
| §3, §8 | Delivery and review availability | REQ-140 – REQ-141 |
| §7 | Live review scenarios | Acceptance scenarios AC-S01 – AC-S10 |

## 5. Hard constraints (non-negotiable, quoted or closely paraphrased)

- Single `docker compose up` brings up the complete working system after the model pull. (§5.1)
- No cloud services, external APIs, hosted telemetry or internet dependency after the model pull. (§5.1)
- All configuration via environment variables; `LLM_URL` and `LLM_MODEL` drive the inference client. (§5.1)
- Quantised GGUF model, **≤ 1 billion parameters**. (§5.2)
- No provider-specific behaviour beyond configured URL and model name. (§5.2)
- "A response generated fully on the server and delivered in one flush does not meet the requirement." (§5.3)
- "A restart, rebuild, or manual re-index step is not acceptable." (§5.4)
- "Stale chunks or cached content must not survive deletion." (§5.4)
- "Silent context overflow is not acceptable." (§5.5)
- Trust boundaries must not rely "on the model to police itself." (§5.6)
- Non-root container; corpus mounted read-only unless explicitly justified. (§5.6)
- No secrets, full prompts, unnecessary document bodies or sensitive user content in logs/traces. (§5.6)
- Local observability tooling only. (§5.8)
- Every source file opens with a header comment block. (§5.9)
- Decision and troubleshooting logs must reflect the **actual sequence of work**, not be reconstructed at the end. (§5.11)

## 6. Live review scenarios (brief: §7, verbatim intent)

1. Start via the single Compose command; show the health signal.
2. Ask a supported question; show progressive streaming with source attribution.
3. Add, modify, remove a document while running; each answer uses only the current corpus.
4. Instruction-like text in a document stays inert; cannot change role or expose hidden instructions.
5. Safe rendering of HTML / script-like content from documents or model output.
6. Conflicting or incomplete documents produce an appropriately qualified answer.
7. Deliberate context-budget behaviour for a corpus/document larger than the context.
8. Graceful degradation for an unreadable file and an unavailable/interrupted model request.
9. Open one request trace; connect stages, token counts, latency, selected sources to the visible answer.
10. Walk the request path in code; answer architecture, operations and security questions.

## 7. Delivery (brief: §3, §8)

- Three calendar days from receipt of the brief.
- Deliver a **public GitHub repository link only** to the recruitment agency.
- Review over Microsoft Teams, screen-shared from the candidate's own environment.
- Keep the working environment available until after the review.

---

## 8. Interpretations (NOT requirements — candidate's reading of the brief)

These are flagged so they can be challenged. Each is resolved (or not) by an ADR.

| ID | Brief text | Interpretation |
|---|---|---|
| INT-01 | "Files … renamed" (§5.4) | A rename is treated as remove(old path) + add(new path). Chunk IDs derived from the path therefore change on rename. |
| INT-02 | "point at which a change is considered ready to serve" (§5.4) | Must be a single, documented, testable moment (e.g. "the next request that starts after the file's write has completed and is stable"). |
| INT-03 | "partially written … silently serving mixed versions" (§5.4) | A file whose content changes *during* our read must not be served as a blend; it is either served as one consistent version or excluded with a recorded diagnostic. |
| INT-04 | "stable chunk identifier" (§5.5) | Identifier must be deterministic for the same file path + content, so the same evidence produces the same ID across requests and in traces. |
| INT-05 | "Docker Model Runner is the preferred backend" (§5.2) | DMR is used where the hardware supports it. On this Intel Mac it does not (TS-001), so Ollama is the backend (ADR-013). Either one is selected purely via `LLM_URL`/`LLM_MODEL`. |
| INT-06 | "After the model pull … without … internet" (§5.1) | Container images (app base image, any trace viewer) also need to be present locally. Docs will list every image to pre-pull/build so the offline claim is honest. |
| INT-07 | "Front end must be clean and usable" (§1) + "HTTP endpoint or a minimal browser interface" (§5.3) | Both are delivered: a streaming HTTP endpoint (for curl/tests) and a minimal browser UI on top of it. |

## 9. Open questions / ambiguities to resolve before or during implementation

| ID | Question | Why it matters | Proposed resolution |
|---|---|---|---|
| OQ-01 | Is Docker Model Runner available on the development/review machine (macOS, Docker Desktop version, Apple Silicon)? | Determines whether DMR or the Ollama fallback is the demo path. | **Resolved 2026-10-08: No.** The machine is an Intel Mac and DMR is Apple Silicon only. Using Ollama, as §5.2 permits. See TS-001, ADR-013. |
| OQ-02 | Which exact ≤1B GGUF model tag? | Must be quantised, ≤1B, and must stream via `/chat/completions`. | **Resolved 2026-10-08:** first `qwen2.5:0.5b` (ADR-013); **changed the same day to `gemma3:1b`** (999.89M params, Q4_K_M) after the injection evaluation (TS-006, ADR-016). |
| OQ-07 | Run Ollama as a Compose service, or natively on the host? | Affects whether a single `docker compose up` starts the *complete* system (REQ-011). | **Resolved 2026-10-08: natively on the host** (ADR-013 Option B). Ollama running is a documented prerequisite for `docker compose up`. |
| OQ-03 | What "defensible precedence rule" for conflicts, if any? | §5.5 only requires surfacing when no rule resolves it. | Proposed: no automatic precedence rule; surface conflicts with sources. See ADR-009. |
| OQ-04 | Which local trace viewer? | §5.8 + §7.9 require opening a trace during review. | See ADR-008 (OpenTelemetry → local Jaeger proposed). Adds one local image. |
| OQ-05 | Should the PDF brief itself be committed to the *public* repo? | The brief is an employer document; publishing it may not be intended. | **Recommend not committing it**; add to `.gitignore`. Candidate to confirm. |
| OQ-06 | Max corpus size / file size targets? | §5.4 requires stating limits. | Set explicit configurable limits (e.g. max file bytes, max files) and document them; values chosen during implementation and recorded. |
