// Browser client for POST /chat (ADR-004, ADR-010).
//
// Reads the Server-Sent Events stream with fetch() + ReadableStream (EventSource only
// supports GET) and renders each event as it arrives, so the answer appears
// progressively (REQ-031). Stop aborts the request; the server then cancels the model
// call (REQ-033).
//
// Safety (REQ-065): every untrusted string (answer text, file names, chunk ids, error
// messages) is written with textContent or as a text node. This file never uses
// innerHTML, outerHTML, insertAdjacentHTML, document.write or eval; a test enforces it.
"use strict";

const $ = (id) => document.getElementById(id);
let controller = null;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function resetView() {
  $("answer").textContent = "";
  $("answer").classList.remove("refused");
  $("error").hidden = true;
  $("error").textContent = "";
  $("sources").replaceChildren();
  $("skipped").replaceChildren();
  $("budget").textContent = "";
  $("request").textContent = "";
}

function showError(message, requestId) {
  $("error").textContent = requestId ? `${message} (request ${requestId})` : message;
  $("error").hidden = false;
}

function renderSources(data) {
  const list = $("sources");
  if (data.chunks.length === 0) {
    list.append(el("li", null, "No document evidence was used."));
  }
  for (const chunk of data.chunks) {
    const item = el("li");
    item.append(el("span", "file", chunk.source));
    const flags = chunk.truncated ? ", truncated to fit the budget" : "";
    item.append(el("span", "detail", `${chunk.id} · score ${chunk.score} · ~${chunk.estimated_tokens} tokens${flags}`));
    list.append(item);
  }
  let budget = `Evidence: ~${data.used_tokens} of ${data.budget_tokens} tokens (estimated, ${data.token_count_method})`;
  if (data.dropped_chunks > 0) budget += ` · ${data.dropped_chunks} lower-ranked chunk(s) left out to stay within budget`;
  budget += ` · corpus version ${data.corpus_version}, ${data.documents} document(s)`;
  $("budget").textContent = budget;
  for (const skip of data.skipped_files) {
    $("skipped").append(el("li", null, `Not used: ${skip.path} (${skip.reason})`));
  }
}

function handle(name, data) {
  switch (name) {
    case "meta":
      $("request").textContent = `Request ${data.request_id}`;
      break;
    case "sources":
      renderSources(data);
      break;
    case "token":
      $("answer").append(document.createTextNode(data.text));
      break;
    case "refusal":
      $("answer").textContent = data.text;
      $("answer").classList.add("refused");
      break;
    case "done": {
      const t = data.tokens ? ` · tokens: prompt ${data.tokens.prompt}, completion ${data.tokens.completion} (${data.tokens.source})` : "";
      const ms = data.timings_ms && data.timings_ms.first_token_ms ? ` · first text after ${Math.round(data.timings_ms.first_token_ms)} ms` : "";
      $("request").textContent = `Request ${data.request_id} · ${data.finish_reason}${t}${ms}`;
      break;
    }
    case "error":
      showError(data.message + (data.partial ? " The answer above is incomplete." : ""), data.request_id);
      break;
  }
}

// SSE frames are separated by a blank line; each has "event:" and one "data:" line.
function parseFrames(buffer, onFrame) {
  let boundary;
  while ((boundary = buffer.indexOf("\n\n")) !== -1) {
    const raw = buffer.slice(0, boundary);
    buffer = buffer.slice(boundary + 2);
    let name = "message";
    let data = "";
    for (const line of raw.split("\n")) {
      if (line.startsWith("event:")) name = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (data) onFrame(name, JSON.parse(data));
  }
  return buffer;
}

async function ask(question) {
  resetView();
  controller = new AbortController();
  $("send").disabled = true;
  $("stop").disabled = false;
  $("status").textContent = "Answering…";
  try {
    const response = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
      signal: controller.signal,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      showError(body.error || `The request was rejected (HTTP ${response.status}).`);
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer = parseFrames(buffer + decoder.decode(value, { stream: true }), handle);
    }
  } catch (err) {
    if (err.name === "AbortError") showError("Stopped. The answer above is incomplete.");
    else showError("Lost connection to the service.");
  } finally {
    $("send").disabled = false;
    $("stop").disabled = true;
    $("status").textContent = "";
    controller = null;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("ask").addEventListener("submit", (event) => {
    event.preventDefault();
    const question = $("question").value.trim();
    if (question) ask(question);
  });
  $("stop").addEventListener("click", () => controller && controller.abort());
});
