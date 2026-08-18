# Progress Tracker

Live build state. Updated at the end of every working session.
Companion files: [`SERVICE_REGISTRY.md`](SERVICE_REGISTRY.md) (what each service is),
[`decisions/`](decisions/) (why we chose it), [`SESSION_LOG.md`](SESSION_LOG.md) (what happened when).

**Current phase:** Phase 0 — Project setup
**Last updated:** 2026-08-19

---

## Phase status

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Project setup: repo, tracking, toolchain, ADR process | 🟡 in progress |
| 1 | Foundation: PostgreSQL schema, object storage, ingestion, Pydantic models, correlation IDs | ⚪ not started |
| 2 | Extraction: vision extraction, structured output validation, confidence handling | ⚪ not started |
| 3 | Validation: PO/GRN matching, tolerance engine, tax/math validation, contract checks | ⚪ not started |
| 4 | Risk: duplicate detection, vendor anomaly features, risk scoring | ⚪ not started |
| 5 | Agent: exception reasoning, evidence-based action plans, LangGraph orchestration | ⚪ not started |
| 6 | Governance: policy engine, HITL dashboard, RBAC, audit trail | ⚪ not started |
| 7 | Execution: mock ERP adapter, idempotent posting, retries/reconciliation | ⚪ not started |
| 8 | AgentOps: tracing, metrics, cost tracking, benchmark dataset, regression evaluation | ⚪ not started |
| 9 | Hardening: security, load testing, failure injection, monitoring, deployment | ⚪ not started |

Legend: ⚪ not started · 🟡 in progress · 🟢 done · 🔴 blocked

---

## Phase 0 — Project setup

- [x] Extract and archive the source specification
- [x] Normalize the spec into `docs/architecture/SPEC.md`
- [x] Stand up the service registry
- [x] Stand up the progress tracker
- [x] Stand up the decision log (ADR process)
- [x] Stand up the session log
- [ ] Choose the Python toolchain and lock dependencies
- [ ] `docker-compose` for PostgreSQL + Redis + object storage
- [ ] Package skeleton with module boundaries matching the service registry
- [ ] Test harness + CI-ready `make`/task targets
- [ ] `CLAUDE.md` so future sessions pick up the conventions

---

## Definition of Done — acceptance criteria

The nine criteria from spec §18. Each is a real test we must be able to run, not a checkbox we assert.

| # | Criterion | Verified by | Status |
| --- | --- | --- | --- |
| DoD-1 | Clean invoice flows ingestion → ERP with no manual step when all policies pass | E2E straight-through test | ⚪ |
| DoD-2 | Price/qty/tax/contract discrepancy is explained and routed correctly | E2E golden-path test (PO 9901) | ⚪ |
| DoD-3 | Repeated invoice detected without creating a duplicate payment | Duplicate re-upload test | ⚪ |
| DoD-4 | High-risk invoices blocked or routed to authorized reviewers | Risk routing test | ⚪ |
| DoD-5 | Every financial action reproducible from the audit trail | Audit replay test | ⚪ |
| DoD-6 | ERP retries cannot create duplicate transactions | Concurrent idempotency test | ⚪ |
| DoD-7 | Model/prompt changes evaluable against a fixed benchmark | Regression eval run | ⚪ |
| DoD-8 | STP, accuracy, latency, cost, failure, override metrics exposed | Metrics endpoint + eval report | ⚪ |
| DoD-9 | The LLM cannot bypass validation, policy, authorization, or execution controls | Architecture test (no `llm` → execution edge) | ⚪ |

**DoD-9 note:** this is enforced structurally, not by review. An automated import/call-graph test asserts
that no `llm`-class module can reach `sentinel.erp`. See [ADR-0004](decisions/0004-trust-classes.md).

---

## Open questions

Questions that need an answer before the phase that depends on them. Resolved ones move to an ADR.

| # | Question | Blocks | Status |
| --- | --- | --- | --- |
| Q-1 | Which vision model for extraction, and what confidence threshold rejects a payload? | Phase 2 | open |
| Q-2 | Real ERP target after the mock adapter (SAP / NetSuite / Dynamics / none)? | Phase 7 | open |
| Q-3 | Is email ingestion in scope for v1, or API + upload only? | Phase 1 | open |
| Q-4 | Object storage: MinIO locally, or filesystem now and S3 later? | Phase 1 | open |
| Q-5 | Does the benchmark dataset use synthetic invoices, or are real anonymized ones available? | Phase 8 | open |

---

## Risk register

| # | Risk | Impact | Mitigation |
| --- | --- | --- | --- |
| R-1 | Extraction hallucinates line items or misreads decimals | Wrong payment | SVC-30 recomputes all math; field-confidence gating; eval suite |
| R-2 | Tolerance policy too loose → silent overpayment | Financial loss | Tolerances versioned + audited; prevented-overpayment metric |
| R-3 | Duplicate detection too aggressive → blocks valid invoices | Ops friction | Uncertain → HITL, never auto-reject; FP/FN tracked |
| R-4 | Idempotency key collision or gap | Double payment | Key derived from stable action identity; concurrent test in DoD-6 |
| R-5 | Scope sprawl across 9 phases | Nothing ships | Phase gates; each phase ends verified before the next starts |
