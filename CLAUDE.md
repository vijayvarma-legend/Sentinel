# Sentinel — working agreement

Autonomous Accounts Payable & Invoice Exception Handler. An agentic procure-to-pay workflow: supplier
invoices in, validated and auditable ERP actions out.

## Read these first

| File | What it is |
| --- | --- |
| [`docs/architecture/SPEC.md`](docs/architecture/SPEC.md) | The specification, normalized. The source PDF is the tiebreaker. |
| [`docs/SERVICE_REGISTRY.md`](docs/SERVICE_REGISTRY.md) | Every service: contract, trust class, invariants, acceptance criteria. |
| [`docs/PROGRESS.md`](docs/PROGRESS.md) | Current phase, DoD status, open questions, risks. |
| [`docs/decisions/`](docs/decisions/) | Why the system is shaped this way. |
| [`docs/SESSION_LOG.md`](docs/SESSION_LOG.md) | What happened in previous sessions. |

## The rule that outranks the others

**No LLM output directly posts money.** Every payment-impacting action passes through deterministic
validation, policy, authorization, and execution controls.

LLMs do perception, classification, explanation, and ambiguity resolution. Deterministic code and SQL do
financial calculations, policy enforcement, idempotency, and transaction safety. If a change blurs that
line, it is wrong — regardless of how much cleaner it looks.

## Non-negotiable invariants

1. Money is `Decimal`. Never `float`. Not in models, not in intermediate math, not in tests.
2. No LLM-generated SQL. No unrestricted ERP commands. Data access goes through controlled repositories.
3. Every record carries its correlation ID from ingestion through ERP execution.
4. Every financial action has an idempotency key. Retries must never double-post.
5. The audit log is append-only. No UPDATE, no DELETE, ever.
6. `llm`-class modules may not import `sentinel.erp` or any write path. Enforced by an architecture
   test — see [ADR-0004](docs/decisions/0004-trust-classes.md).
7. Policies live in the database, versioned. Never in prompts, never hardcoded in agent logic.
8. Every decision records the policy version that produced it.

## Tracking discipline

This project is tracked deliberately. Keeping it current is part of the work, not overhead after it.

- **Changed a service's status, contract, or invariants?** Update `SERVICE_REGISTRY.md` in the same commit.
- **Finished or started a phase item?** Update `PROGRESS.md`.
- **Made a choice that is expensive to reverse, that a reader would otherwise reverse-engineer, or that
  we argued about?** Write an ADR. Never edit an accepted one — supersede it.
- **Ending a session?** Append to `SESSION_LOG.md`: what got done, what was decided in passing, what the
  next session picks up, what questions opened.
- **Hit an ambiguity in the spec?** Do not silently pick. Record it, resolve it in an ADR, and note it in
  `SPEC.md` where the ambiguity lives.

## Conventions

- Python 3.13, `uv` for dependencies ([ADR-0002](docs/decisions/0002-python-toolchain.md)). Install from
  the committed lockfile; do not hand-edit it.
- Modular monolith. Service boundaries are module boundaries
  ([ADR-0003](docs/decisions/0003-modular-monolith.md)). Modules talk through Pydantic contracts at their
  edges — never by reaching into each other's internals.
- Ruff for lint and format. Mypy strict on `deterministic` and `control` modules.
- Tests colocated by service. The golden path (PO 9901 / GRN 9 / INV-8821, spec §15) is the canonical
  integration case — it should break loudly whenever the pipeline's behaviour changes.
- Every service's acceptance criteria in the registry should map to a real test, not an assertion in prose.

## How to work here

Build a phase, verify it against its acceptance criteria, then start the next one. A phase is not done
because the code exists — it is done when its criteria pass. When something is only partly working, say
so plainly and say which part.
