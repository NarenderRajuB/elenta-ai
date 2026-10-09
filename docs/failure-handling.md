# Failure handling

What the service does for each failure mode named in the brief (REQ-108, covering REQ-071 to REQ-076). For each: what the user sees, the structured diagnostic left behind, and how to recover or what the limitation is. The brief's rule (REQ-070) is that nothing fails silently: the service must never silently overflow, silently omit, or invent.

Every diagnostic below can be found from the **request id** shown under the answer: in the events, in `docker compose logs app | grep <id>`, and in the trace at `http://127.0.0.1:16686/trace/<id>` ([Operations](operations.md)).

## Summary

| Failure mode | User sees | Model called? | Main diagnostic | Status |
|---|---|---|---|---|
| [Corpus larger than the context window](#1-corpus-larger-than-the-context-window-req-071) | An answer from the best evidence, with what was left out stated under the sources | Yes | `dropped_chunks`, `truncated` on the `sources` event and the trace | Handled |
| [Question too long to fit](#1-corpus-larger-than-the-context-window-req-071) | Error: "The question is too long…" | No | `error` event `question_too_long`; trace `prompt.estimated_tokens` vs `prompt.limit_tokens` | Handled |
| [Reasoning or hidden instructions in the output](#2-reasoning-or-hidden-instructions-in-the-output-req-072) | Reasoning removed silently from the answer; a leak replaced by a refusal | Yes | `refusal` event; `reasoning_blocks_removed`; log warning; trace `guard.*` | Handled; limits stated |
| [Corrupt, incomplete, changing or unsupported document](#3-corrupt-incomplete-changing-or-unsupported-documents-req-073) | Answer from the other files; "Not used: file (reason)" under the sources | Yes | `skipped_files`; `corpus file skipped` log; trace `corpus.skipped_errors` | Handled |
| [Conflicting or outdated documents](#4-conflicting-or-outdated-documents-req-074) | Both sources listed; the answer text usually gives one value | Yes | Both files in `sources` and the trace | **Limitation** (TS-008) |
| [Empty corpus or unsupported question](#5-empty-corpus-or-question-not-covered-req-075) | A fixed "not enough information" reply | **No** | `insufficient_reason`; `model_called: false` | Handled |
| [Model unavailable, slow or failing mid-stream](#6-model-unavailable-slow-or-failing-mid-stream-req-076) | Error with the request id; any partial answer marked incomplete | Tried | `error` event code and `partial`; `model call failed` log; trace `error.code` | Handled |

## 1. Corpus larger than the context window (REQ-071)

The corpus can be far larger than the model's 4,096-token window; only a bounded amount of evidence is ever sent (ADR-007).

**What happens**
- Evidence is chosen by BM25 rank and added until `CONTEXT_TOKEN_BUDGET` (1,500 estimated tokens) is full. A chunk that doesn't fit is **left out and recorded**, and the next one is tried. Only if the single best chunk is bigger than the whole budget is it **cut, and marked truncated**.
- Before calling the model, the whole prompt (instructions + evidence + question) is checked against `LLM_CONTEXT_TOKENS − LLM_MAX_TOKENS`. If it doesn't fit, the request is **rejected, never truncated**.
- At startup, the app checks the worst case fits (instructions + full budget + 100 question tokens); if not, it exits with code 2 and says which settings to lower ([Configuration](configuration.md#rules-that-combine-settings)).

**User sees**
- Under the sources: *"Evidence: ~1480 of 1500 tokens (estimated, chars/4) · 3 lower-ranked chunk(s) left out to stay within budget"*, and *"truncated to fit the budget"* next to a cut chunk.
- A question too long to fit: an `error` event, *"The question is too long to answer within the model's context window."* (questions over 8,000 characters are rejected before streaming with HTTP 422).

**Diagnostics**
- `sources` event: `budget_tokens`, `used_tokens`, `dropped_chunks`, `truncated`, and per chunk `estimated_tokens` and `truncated`.
- Trace `evidence.selection`: the same counts and the selected chunk ids (dropped chunks are counted, not listed individually). `prompt.assembly`: `prompt.estimated_tokens` and `prompt.limit_tokens`; `error.code: question_too_long` when rejected.

**Limitation:** token counts before the model call are estimates (characters ÷ 4). They undercount for some scripts (for example Chinese or Japanese); the 1,500-token budget inside 4,096 leaves margin for that. When the model reports real counts, `done` shows both.

**Tests:** `tests/test_selection.py::TestReq055Budget`, `tests/test_prompt.py::TestPromptBudget`, `tests/test_observability.py::TestFailureTraces`.

## 2. Reasoning or hidden instructions in the output (REQ-072)

Model output is untrusted; it is filtered before it reaches the browser (ADR-006 C4, C5).

**What happens**
- **Reasoning:** text inside `<think>…</think>` or `<thinking>…</thinking>` (any letter case) is removed, including when the markers are split across streamed pieces or never closed. The rest of the answer streams normally.
- **Hidden instructions:** if the answer reproduces any 60 consecutive characters of the system instructions (ignoring case and spacing), the stream is stopped, the upstream model call is closed, and a fixed refusal replaces everything shown so far.

**User sees**
- Reasoning: nothing; it never reaches the browser.
- Leak attempt: *"I can't share that. I can only answer questions using the documents in the corpus."* in place of the answer.

**Diagnostics**
- `refusal` event; `done` with `finish_reason: instruction_leak_blocked` and `reasoning_blocks_removed: N`.
- Logs: `output guard blocked instruction leak` (WARNING), `output guard removed reasoning` (INFO, with the count). The text itself is never logged.
- Trace `inference.stream`: `guard.leak_blocked`, `guard.reasoning_blocks_removed`.

**Limitations:** only those two reasoning markers are recognised; the leak check catches copying, not paraphrase ([Security](security.md#remaining-risks)). In the evaluation, `gemma3:1b` attempted a leak in all 3 runs and was blocked every time.

**Tests:** `tests/test_prompt.py::TestC5Reasoning`, `::TestC4InstructionLeak`, `::TestGuardStreaming`; `tests/test_chat.py::TestGuardOnLivePath`.

## 3. Corrupt, incomplete, changing or unsupported documents (REQ-073)

A bad file never takes down the service or the rest of the corpus (REQ-056, ADR-005).

**What happens:** each file is read safely and either served in full or skipped with a reason. Every other file is still used.

| Situation | Reason | Shown under the sources? |
|---|---|---|
| Being written (modified in the last 0.5 s) | `settling` | No (expected, served shortly) |
| Changed while being read | `changing` | Yes |
| Not valid UTF-8 | `not_utf8` | Yes |
| Contains NUL bytes (binary or corrupt) | `binary_content` | Yes |
| Over 50 MB | `too_large` (not read at all) | Yes |
| Beyond 500 files | `file_limit_exceeded` | Yes |
| Permission denied or read error | `unreadable` | Yes |
| Name not valid UTF-8 | `invalid_filename` | Yes |
| Resolves outside `/data` | `outside_root` | Yes |
| Not `.txt`/`.md`; hidden; symlink; pipe or device; empty | `unsupported_type`, `hidden`, `symlink`, `not_regular_file`, `empty` | No (policy, logged only) |
| `./data` itself gone | `corpus_dir_missing`: empty corpus, app keeps running | Yes |

Skipped files are checked again on every question (only served files are cached), so a fixed file is picked up on the next one.

A file that is changing is **not served at all**, not even its previous version, so an answer never mixes old and new text.

**User sees:** a normal answer from the remaining files, and under the sources *"Not used: reports/q3.txt (not_utf8)"* for each problem file. Policy skips (hidden, unsupported type and so on) aren't listed, to keep the page readable; they're in the logs.

**Diagnostics:** `sources.skipped_files` (problems only); log `corpus file skipped: path=… reason=…` (WARNING for problems, DEBUG for policy skips); trace `corpus.refresh` attribute `corpus.skipped_errors` and the `corpus.skipped` count.

**Recovery:** fix, replace or remove the file; the next question uses the result, with no restart.

**Tests:** `tests/test_corpus.py::TestReq056CorruptFiles`, `::TestReq045PartialWrites`, `::TestReq041Formats`, `::TestLimits`, `::TestReq064Boundary`; `tests/test_chat.py::TestVisibility`.

## 4. Conflicting or outdated documents (REQ-074)

**Decision (ADR-009):** no automatic precedence. File dates reflect when a file was copied, not which document is authoritative, so the service doesn't pick a winner. The system instructions tell the model to say when evidence blocks disagree and to cite each one.

**What happens today:** both conflicting chunks are selected and sent to the model, and **both files are always listed as sources**. But in the evaluation, the model answered with only one of the values in every run, and changing the instruction wording or the model (three models of at most 1B parameters) did not fix it (TS-008).

**User sees:** an answer giving one value, with both documents under the sources. For example, with `remote-2023.md` (2 days) and `remote-2024.md` (4 days): *"Up to 2 days per week."* with both files listed.

**Diagnostics:** both chunk ids and files in the `sources` event and the `evidence.selection` span; the evaluation's conflict case in [evidence/injection-eval.md](evidence/injection-eval.md).

**Limitation and workaround:** the answer text can't be relied on to flag a conflict; **check the listed sources** when more than one document is cited. Removing or renaming the outdated document resolves it, since the next question uses the current corpus. Whether to add a code-level notice when several documents are cited is an open decision (TS-008).

**Tests:** manual (`scripts/eval_injection.py`, conflict case); the instruction wording is checked in `tests/test_prompt.py::TestC7SystemPromptContent`.

## 5. Empty corpus or question not covered (REQ-075)

**What happens:** if selection finds no qualifying evidence, the service sends a fixed reply and **does not call the model**, so it can't fill the gap from its own memory (ADR-006 C6).

| Reason (`insufficient_reason`) | When | User sees |
|---|---|---|
| `empty_corpus` | No readable documents | *"There are no readable documents in the corpus, so I can't answer this."* |
| `no_meaningful_terms` | Question has only stop words or punctuation | *"Please ask a question about the documents. I couldn't find any searchable words in it."* |
| `no_relevant_evidence` | No chunk shares a meaningful word with the question | *"The documents do not contain enough information to answer this question."* |

**Diagnostics:** `sources.insufficient_reason`; `done` with `finish_reason: insufficient_evidence` and `model_called: false`; log `evidence selected` with the reason; trace `selection.insufficient_reason`, `chat.model_called: false`, and no `prompt.assembly` or `inference.stream` span.

**Limitation:** matching is by exact words, so a question using a synonym ("holiday" for "leave") may get `no_relevant_evidence` although the answer is in the corpus. When some evidence is found but doesn't actually answer the question, the model is asked and is instructed to say the evidence isn't enough; that part relies on the model.

**Tests:** `tests/test_selection.py::TestReq053Insufficient`, `tests/test_chat.py::TestNoEvidence`, `tests/test_observability.py::TestTraceStructure`.

## 6. Model unavailable, slow or failing mid-stream (REQ-076)

**What happens:** every way the model call can fail becomes a fixed error code. Three limits bound the call: connecting (5 s), the gap between streamed pieces including the first (60 s), and the whole call (180 s, checked after each piece). The app itself stays up and healthy.

| Situation | Code | `partial` | User sees |
|---|---|---|---|
| Model server not running / connection refused | `model_unavailable` | false | *"The language model is not reachable. Please try again shortly."* |
| Connect, first-piece or between-piece timeout, or whole-call limit | `model_timeout` | true if text was already shown | *"The language model took too long to respond."* |
| Server answers with an error status (e.g. 404 for an unknown model) | `model_http_error` | false | *"The language model returned an error."* |
| Connection drops or stream is malformed mid-answer | `model_stream_failed` | true if text was already shown | *"The answer was interrupted because the language model stream failed."* |

When `partial` is true the browser keeps the text received so far and adds *"The answer above is incomplete."* Every error shows the request id.

**Diagnostics:** `error` event with `code`, `message`, `partial` and `request_id`; log `model call failed` (WARNING) with the code and `partial`; trace `inference.stream` and `chat.request` with ERROR status and `error.code`. Separately, `/readyz` returns 503 with `unreachable`, `timeout`, `http_error`, `invalid_response` or `model_not_found`, so the problem can be seen before anyone asks a question.

**Recovery:** start or restart Ollama, or fix `LLM_URL` / `LLM_MODEL`; the next question works without restarting the app ([Operations](operations.md#restart-and-recovery)).

**Related, also handled:**
- **Answer cut off by the length limit:** the answer ends at `LLM_MAX_TOKENS` (512) and the request line shows finish reason `length`.
- **Browser closed or Stop pressed** (REQ-033): the response, the pipeline and the upstream model call are all closed, so the model stops generating. The trace still ends, with outcome `client_disconnected`.
- **Unexpected bug:** `error` event `internal_error` with the request id; the stack trace is logged and the trace records the exception class.

**Tests:** `tests/test_inference.py`, `tests/test_chat.py::TestModelFailures`, `tests/test_health.py`, `tests/test_streaming_e2e.py`, `tests/test_observability.py::TestFailureTraces`.
