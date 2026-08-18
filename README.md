# Sentinel

**Autonomous Accounts Payable & Invoice Exception Handler.**

Supplier invoices arrive as PDFs. Sentinel turns them into validated, auditable, policy-compliant ERP
actions — automatically when the evidence is clean, and in front of a human when it is not.

> **No LLM output directly posts money.** Models handle perception, classification, explanation, and
> ambiguity. Deterministic code and SQL handle every calculation, policy check, idempotency guarantee,
> and transaction.

## The pipeline

```
Email / Upload / API
        ↓
   Ingestion ──────────── correlation ID, document hash, original stored immutably
        ↓
Vision Extraction ─────── structured fields + per-field confidence          [advisory]
        ↓
Validation Engine ─────── three-way match, tolerances, tax & totals recomputed
        ↓
Duplicate Detection ───── exact hash, exact number, fuzzy, similarity
        ↓
 Risk Scoring ─────────── vendor, price, bank-change, new-vendor, behavioural signals
        ↓
Exception Reasoning ───── classifies and explains, grounded in the evidence  [advisory]
        ↓
  Policy Engine ───────── versioned rules decide: auto, review, HITL, or block
        ↓            ↘
Auto-resolution      HITL Dashboard ── authenticated human decision, fully evidenced
        ↓            ↙
 ERP Execution ────────── authorization → idempotency → adapter → verification
        ↓
Audit / Event Log ─────── append-only, replayable
        ↓
AgentOps + Evals ──────── accuracy, cost, latency, STP, override rates
```

## Status

**Phase 0 — project setup.** See [`docs/PROGRESS.md`](docs/PROGRESS.md) for live status,
[`docs/SERVICE_REGISTRY.md`](docs/SERVICE_REGISTRY.md) for what each service does and how far along it
is, and [`docs/decisions/`](docs/decisions/) for why it is built this way.

## Stack

Python 3.13 · LangGraph · FastAPI · Pydantic · PostgreSQL · Redis · React · OpenTelemetry · Docker

## Documentation

| | |
| --- | --- |
| [Specification](docs/architecture/SPEC.md) | The full architecture, normalized from the source PDF |
| [Service registry](docs/SERVICE_REGISTRY.md) | Contracts, trust classes, invariants, acceptance criteria |
| [Progress](docs/PROGRESS.md) | Phases, Definition of Done, open questions, risks |
| [Decisions](docs/decisions/) | Architecture decision records |
| [Session log](docs/SESSION_LOG.md) | Build journal |
| [Working agreement](CLAUDE.md) | Conventions and invariants for contributors |
