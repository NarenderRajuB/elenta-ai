# ELENTA chat service: documentation

The guide to the service (REQ-100). The repository [README](../README.md) is the quick start and test catalogue; the documents here explain how the system works, how to run and operate it, and where its limits are.

## Guide

| Document | What it covers | Requirement |
|---|---|---|
| [Overview](overview.md) | Purpose, supported use, scope, known limits | REQ-101 |
| [Architecture](architecture.md) | Components, boundaries, storage, model connection, diagram | REQ-102 |
| [Request and data flow](request-flow.md) | Corpus refresh and chat flow, with diagrams; following one request | REQ-103 |
| Setup *(to be written)* | Prerequisites, model pull, configuration, startup, health check, first request | REQ-104 |
| Configuration *(to be written)* | Every environment variable and its effect | REQ-105 |
| Operations *(to be written)* | Logs and traces, corpus refresh, restart and recovery, formats, platform notes | REQ-106 |
| Security *(to be written)* | Trust boundaries, prompt injection, rendering, filesystem, network, secrets, remaining risks | REQ-107 |
| Failure handling *(to be written)* | Behaviour for each failure mode in the brief | REQ-108 |

## Records kept while working

| Document | What it is |
|---|---|
| [Architecture decisions](architecture-decisions.md) | ADR-001 onwards: each decision with context, rationale, alternatives and consequences (REQ-110) |
| [Troubleshooting log](troubleshooting-log.md) | TS-001 onwards: every material issue and dead end, in the order they happened (REQ-111) |
| [Requirements](requirements.md) | Every requirement from the brief, with its source section (REQ-xxx) |
| [Acceptance criteria](acceptance-criteria.md) | How each requirement is checked (AC-xxx) |
| [Specification summary](spec.md) | The brief summarised, with interpretations kept separate |

## Evidence

| Location | What it holds |
|---|---|
| [`evidence/verify/`](evidence/verify/summary.md) | Output of `scripts/verify.sh`: tests, lint, format, types, security scans (REQ-120 – REQ-123) |
| [`evidence/injection-eval.md`](evidence/injection-eval.md) | Repeatable prompt-injection and conflict evaluation against the live model (TS-006, TS-008) |
