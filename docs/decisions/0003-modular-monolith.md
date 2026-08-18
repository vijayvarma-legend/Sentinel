# ADR-0003: Modular monolith, not microservices

- **Status:** accepted
- **Date:** 2026-08-19
- **Phase:** 0
- **Affects:** all

## Context

The spec describes eleven services and draws them as separate boxes in the reference architecture
(§14). That diagram is a *responsibility* decomposition; it does not by itself dictate a deployment
topology. We have to choose one.

Two facts push hard on this choice. First, spec §12 requires correlation IDs spanning ingestion,
LangGraph state, database operations, and ERP execution, and §18 requires that any financial action be
replayable from the audit trail — distributed tracing and cross-service transactional consistency are
both markedly harder to get right across process boundaries. Second, the whole pipeline runs
per-invoice and is dominated by model latency and human review time, not by CPU; there is no stage with
a load profile that demands independent scaling.

## Decision

One deployable Python application, with the service boundaries from
[`SERVICE_REGISTRY.md`](../SERVICE_REGISTRY.md) enforced as **module** boundaries under `src/sentinel/`.

Each service is a package with an explicit public interface and Pydantic contracts at its edges. Modules
talk to each other through those contracts — never by reaching into each other's internals, and never
through shared mutable state. Persistence goes through repositories; no module writes another module's
tables.

The boundaries are enforced by an automated import-linting test, not by good intentions. Because the
seams are real, extracting any module into its own process later is a deployment change, not a rewrite.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Microservice per spec service | Eleven deployments, eleven failure modes, distributed transactions across the ERP posting path — enormous operational cost for a system with no independent scaling need |
| Single flat package | No enforceable boundary between `llm` and `control` code, which is exactly the boundary [ADR-0004](0004-trust-classes.md) depends on |
| Serverless functions per stage | State handoff and long HITL waits fit badly; cold starts on the vision path; audit continuity gets harder |

## Consequences

Correlation IDs, transactions, and the audit trail stay simple, which directly serves DoD-5. Local
development is one process plus Docker dependencies. Debugging the golden path means one stack trace.

The cost is discipline: a monolith with unenforced boundaries decays into a ball of mud, so the import
test is not optional — it is the thing that keeps this decision honest. We also give up independent
scaling and independent deploys; if the extraction stage ever becomes a genuine bottleneck, it is the
first candidate for extraction into its own worker.
