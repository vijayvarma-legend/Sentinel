# Service Registry

Every major service in Sentinel. **One row per service, kept current as we build.** When a service's
status, contract, or invariants change, this file changes in the same commit.

**Status legend:** `planned` → `scaffolded` (module + contracts exist) → `in-progress` → `built`
(implemented + unit tested) → `verified` (passes its acceptance criteria end-to-end) → `hardened`.

**Trust class** — the single most important column. It answers: *can this service's output move money
on its own?*

| Class | Meaning |
| --- | --- |
| `deterministic` | Pure code/SQL. Reproducible. Authoritative for financial facts. |
| `llm` | Model-driven. **Advisory only.** Output is evidence or a recommendation, never an authorization. |
| `control` | Enforces a gate (policy, authz, idempotency). Must be deterministic and must not be bypassable. |
| `human` | A person's authenticated decision. |
| `observability` | Records or measures. No effect on the decision path. |

---

## Platform services

| ID | Service | Module | Trust | Status | Responsibility |
| --- | --- | --- | --- | --- | --- |
| SVC-00 | Core domain | `sentinel.core` | `deterministic` | planned | Pydantic models, money type, correlation IDs, error taxonomy, settings |
| SVC-01 | Persistence | `sentinel.db` | `deterministic` | planned | PostgreSQL schema, migrations, repositories, least-privilege roles |
| SVC-02 | Document store | `sentinel.storage` | `deterministic` | planned | Original document bytes, content-addressed, immutable |
| SVC-03 | Orchestrator | `sentinel.graph` | `deterministic` | planned | LangGraph pipeline, state, checkpointing, retries, dead-letter |
| SVC-04 | AuthN/AuthZ | `sentinel.auth` | `control` | planned | RBAC: ap_operator, ap_manager, finance_admin, system |

## Pipeline services

| ID | Service | Module | Trust | Status | Spec |
| --- | --- | --- | --- | --- | --- |
| SVC-10 | Invoice Ingestion | `sentinel.ingestion` | `deterministic` | planned | §4.1 |
| SVC-20 | Vision Extraction | `sentinel.extraction` | `llm` | planned | §4.2 |
| SVC-30 | Validation Engine | `sentinel.validation` | `deterministic` | planned | §4.3, §5 |
| SVC-40 | Duplicate Detection | `sentinel.duplicates` | `deterministic` | planned | §6 |
| SVC-50 | Risk & Fraud Scoring | `sentinel.risk` | `deterministic` | planned | §7 |
| SVC-60 | Exception Reasoning Agent | `sentinel.reasoning` | `llm` | planned | §8 |
| SVC-70 | Policy Engine | `sentinel.policy` | `control` | planned | §9 |
| SVC-80 | HITL Dashboard + API | `sentinel.hitl` / `web/` | `human` | planned | §10 |
| SVC-90 | ERP Execution | `sentinel.erp` | `control` | planned | §11 |
| SVC-95 | Audit / Event Log | `sentinel.audit` | `observability` | planned | §12 |
| SVC-99 | AgentOps & Evaluation | `sentinel.agentops` / `evals/` | `observability` | planned | §13 |

---

## Service contracts

### SVC-10 — Invoice Ingestion · `deterministic`

| | |
| --- | --- |
| **In** | Raw file (PDF/image) + source metadata (email, upload, API, batch) |
| **Out** | `IngestedDocument` — correlation_id, document_hash, storage_uri, mime, page_count, source |
| **Owns** | Correlation ID minting, SHA-256 document hash, dead-letter routing |
| **Invariants** | Original bytes are never mutated. Every accepted document gets exactly one correlation ID. Unsupported/corrupt input dead-letters — it never enters the pipeline. |
| **Acceptance** | Corrupt PDF → dead-letter with reason, no pipeline state created. Same bytes twice → same hash. |

### SVC-20 — Vision Extraction · `llm` (advisory)

| | |
| --- | --- |
| **In** | `IngestedDocument` |
| **Out** | `ExtractedInvoice` — supplier, invoice_number, po_reference, line items, qty, prices, tax, total, currency, charges; **field-level confidence + page/source refs** |
| **Invariants** | Emits structured Pydantic or **rejects**. Never silently downgrades a low-confidence field. Never computes a total — it reads what is printed. Arithmetic is SVC-30's job. |
| **Acceptance** | Malformed model output → `ExtractionRejected`, not a partial record. Low confidence on a money field → routed to review, never auto-processed. |
| **Risks** | Hallucinated line items; misread decimal separators; multi-page totals. Mitigated by SVC-30 recomputation + SVC-99 field accuracy evals. |

### SVC-30 — Validation Engine · `deterministic` (authoritative)

| | |
| --- | --- |
| **In** | `ExtractedInvoice` + PO + GRN + Contract (via controlled repositories) |
| **Out** | `ValidationScorecard` — per-check pass/fail, computed variances, evidence |
| **Owns** | **All financial math.** Three-way match, tolerance evaluation, tax/subtotal/total recomputation, allowed-charge checks |
| **Invariants** | No LLM call in this module, ever. Decimal arithmetic only — no floats on money. Every failed check carries the numbers that failed it. Price variance is computed **against the accepted quantity** (spec §15). |
| **Acceptance** | Golden path (PO 9901 / GRN 9 / INV-8821) yields qty variance −1, price variance +5%, unapproved shipping $200. |

### SVC-40 — Duplicate Detection · `deterministic`

| | |
| --- | --- |
| **In** | `IngestedDocument` + `ExtractedInvoice` + invoice history |
| **Out** | `DuplicateAssessment` — score, tier (exact_hash / exact_number / fuzzy / similar), match evidence |
| **Invariants** | **Never auto-rejects.** Uncertain duplicates route to HITL. Normalizes invoice numbers (`INV-8821` ≡ `INV8821`) without collapsing genuinely distinct ones. |
| **Acceptance** | Byte-identical re-upload → exact_hash, blocked before ERP. Reformatted same invoice → high similarity → HITL. |

### SVC-50 — Risk & Fraud Scoring · `deterministic`

| | |
| --- | --- |
| **In** | `ExtractedInvoice`, vendor profile/history, `DuplicateAssessment` |
| **Out** | `RiskAssessment` — overall score + **per-signal contributions** |
| **Signals** | vendor anomaly, price anomaly, payment-change (bank), duplicate, new-vendor, behavioral |
| **Invariants** | Score is always decomposable into named contributing signals — an unexplainable score is a bug. This is triage, **not** a fraud determination. |
| **Acceptance** | Bank change + large invoice → payment-change signal dominant, overall > 0.70 → mandatory HITL. |

### SVC-60 — Exception Reasoning Agent · `llm` (advisory)

| | |
| --- | --- |
| **In** | `ValidationScorecard` + `DuplicateAssessment` + `RiskAssessment` + business documents |
| **Out** | `ExceptionAnalysis` — category, explanation, recommended action, structured action plan |
| **Invariants** | **Grounded only in supplied evidence — must not invent financial facts.** Recommends; never authorizes. Every claim traceable to an evidence item. |
| **Acceptance** | Golden path → classifies quantity + price + unapproved-fee exceptions, cites the actual numbers, recommends escalation. Evidence-free assertion → caught by the grounding check. |

### SVC-70 — Policy Engine · `control`

| | |
| --- | --- |
| **In** | Scorecard + risk + duplicate + `ExceptionAnalysis` + active policy set |
| **Out** | `PolicyDecision` — route (auto / review / hitl / block), permitted actions, **policy_version** |
| **Invariants** | Policies live in PostgreSQL/config, **never in prompts**. Every policy is versioned; every decision records the version that produced it. Failed safety/idempotency check → block, unconditionally. |
| **Acceptance** | A historical decision can be replayed exactly against its recorded policy version. |

### SVC-80 — HITL Dashboard + API · `human`

| | |
| --- | --- |
| **In** | Pending decisions + full evidence bundle |
| **Out** | `HumanDecision` — actor, role, action, rationale, timestamp |
| **Views** | Overview · exception queue · invoice detail · agent evidence · decision controls · audit history |
| **Invariants** | Authenticated + role-checked + timestamped + audited. Evidence is visible **before** the approve button is usable. |
| **Acceptance** | An ap_operator cannot approve above their threshold. Every click lands in the audit log. |

### SVC-90 — ERP Execution · `control`

| | |
| --- | --- |
| **In** | Authorized `PolicyDecision` (+ `HumanDecision` where required) |
| **Out** | `ErpTransactionResult` |
| **Flow** | authorization check → idempotency check → adapter → verification → audit event |
| **Invariants** | **Idempotency key per financial action — retries can never double-post.** An agent recommendation alone is never sufficient input. Mock adapter first; real adapters behind the same interface. Timeouts/partial failures go to reconciliation, not blind retry. |
| **Acceptance** | Same action submitted 100× concurrently → exactly one ERP transaction. |

### SVC-95 — Audit / Event Log · `observability`

| | |
| --- | --- |
| **Out** | Append-only `AuditEvent` stream |
| **Records** | agent version, prompt/config version, model, tool calls, evidence, actor, result |
| **Invariants** | Append-only — no UPDATE, no DELETE. Correlation ID on every event. Every material decision is reconstructable from this log alone. |
| **Acceptance** | Any completed invoice can be fully replayed from audit events. |

### SVC-99 — AgentOps & Evaluation · `observability`

| | |
| --- | --- |
| **Out** | Metrics, traces, cost per invoice, versioned experiment runs |
| **Measures** | extraction quality · decision quality · operational · AI economics · human interaction · business (STP, prevented overpayment) |
| **Invariants** | Benchmark dataset is fixed and versioned. Any model/prompt/schema/policy change triggers a regression run. |
| **Acceptance** | Swapping the extraction model produces a diffable accuracy report against the fixed benchmark. |

---

## Cross-cutting invariants (apply to every service)

1. **No LLM output directly posts money.** SVC-20 and SVC-60 are the only `llm` services, and neither has an execution path.
2. **No LLM-generated SQL. No unrestricted ERP commands.** Data access is through controlled repositories only.
3. Every record carries a **correlation ID** from ingestion through ERP.
4. Money is `Decimal`, never `float`.
5. Every material decision emits an audit event before the next stage runs.
