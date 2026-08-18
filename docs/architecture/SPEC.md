# Sentinel — Architecture Specification (normalized)

> Source of truth: `docs/architecture/source/Autonomous_AP_Automation_Architecture_v2.pdf`
> (MAD TECH SOLUTIONS — *Autonomous Accounts Payable & Invoice Exception Handler*, v2, 8pp).
> Raw text extraction: `docs/architecture/source/spec-extracted.txt`.
> This file is the working restatement. If the two disagree, the PDF wins — and that is a bug to be
> fixed here, then recorded in the session log.

## 0. Prime directive

> **No LLM output directly posts money.** Every payment-impacting action passes through deterministic
> validation, policy, authorization, and execution controls.

Corollary (the architecture principle, spec p.1):

| Use LLMs for | Use deterministic code/SQL for |
| --- | --- |
| Perception (vision extraction) | Financial calculations |
| Classification (exception typing) | Policy enforcement |
| Explanation (evidence narration) | Idempotency |
| Ambiguity resolution (recommendations) | Transaction safety |

The final architectural principle (spec §18): *the goal is not to maximize the number of agents* — it is a
reliable financial workflow where agents reason and deterministic software controls everything exact.

## 1. Domain & runtime

| Axis | Value |
| --- | --- |
| Domain | Enterprise ERP & financial automation (procure-to-pay) |
| Pattern | Multi-agent / deterministic validation / human-in-the-loop |
| Target runtime | Python / LangGraph / FastAPI / PostgreSQL |

**Core objective:** convert incoming supplier invoices into validated, auditable, policy-compliant ERP
actions — keeping financial decisions deterministic where possible, routing ambiguity and risk to humans.

## 2. End-to-end pipeline

```
Invoice Source → Ingestion → Vision Extraction → Deterministic Validation → Duplicate Detection
  → Risk/Fraud Scoring → Exception Reasoning → Policy Engine → Auto-Resolution | HITL
  → ERP Execution → Audit/Event Log → AgentOps & Evaluation
```

| # | Stage | Purpose | Primary output |
| --- | --- | --- | --- |
| 1 | Invoice Ingestion | Receive from email, upload, API, batch | Normalized document + metadata |
| 2 | Vision Extraction | Unstructured PDF/image → structured invoice data | Validated extraction payload |
| 3 | Deterministic Validation | PO/GRN/tax/math/tolerance checks in code + SQL | Validation scorecard |
| 4 | Duplicate Detection | Exact and fuzzy duplicate invoices | Duplicate risk + match evidence |
| 5 | Exception Reasoning | Classify discrepancies, propose resolution | Exception category + action plan |
| 6 | Risk/Fraud Scoring | Vendor, transaction, bank, invoice anomalies | Risk score + reasons |
| 7 | Policy Engine | Apply configurable business/approval policy | Decision / escalation route |
| 8 | HITL Dashboard | Authorized approve, reject, request correction | Human decision |
| 9 | ERP Execution | Post approved transactions safely + idempotently | ERP transaction result |
| 10 | AgentOps + Evaluation | Quality, cost, reliability, regressions | Metrics + evaluation reports |

> Note: the spec's stage table lists Exception Reasoning as #5 and Risk as #6, while the pipeline arrow
> and the reference architecture (§14) both run **Risk before Reasoning**. We follow the arrow: risk
> scoring is an *input* to reasoning. Tracked as [ADR-0005](../decisions/0005-pipeline-stage-ordering.md).

## 3. Data model — ground-truth business documents

| Entity | Role | Important fields |
| --- | --- | --- |
| Purchase Order (PO) | Approved commercial commitment | `po_number`, `vendor_id`, `item_id`, `agreed_unit_price`, `approved_qty` |
| Goods Receipt Note (GRN) | Physical receipt evidence | `grn_number`, `po_number`, `received_qty`, `damaged_qty`, `received_date` |
| Vendor Invoice | Supplier payment request | `invoice_number`, `po_number`, `billed_qty`, `billed_unit_price`, `tax`, `total_due` |
| Vendor Contract | Commercial terms, allowed charges | `vendor_id`, `pricing_terms`, `shipping_terms`, `effective_dates` |
| Vendor Profile | Historical behavior + risk context | `vendor_id`, `history`, bank details, anomaly statistics |
| Policy | Configurable financial decision rules | `rule_id`, `threshold`, `tolerance`, `approver_role`, `action` |
| Audit Event | Immutable workflow evidence | `event_id`, `invoice_id`, `actor`, `timestamp`, `action`, `result` |

PO + GRN + invoice + contract are the core business evidence. Vendor history, policies, and audit events
extend that evidence into risk and governance.

## 4. Module responsibilities

### 4.1 Invoice Ingestion
- Accept via email inbox, REST API, file upload, batch import.
- Normalize metadata, store the original document, assign a **correlation ID**, compute a **document hash**.
- Basic file validation; unsupported/corrupt files go to a **dead-letter path**.

### 4.2 Vision Extraction
- Multi-page invoices and scanned documents via a vision-capable model.
- Extract supplier, invoice number, PO reference, line items, quantities, prices, taxes, totals, currency, charges.
- Return structured Pydantic data with **field-level confidence** and source/page references where possible.
- **Reject** malformed or low-confidence payloads rather than silently passing them downstream.

### 4.3 Deterministic Validation Engine
- Retrieve PO and GRN records through **controlled** database tools.
- Line-level quantity, price, subtotal, tax, total, tolerance calculations.
- Validate contract constraints and allowed charges where contract data exists.
- **All math stays outside the LLM.**

## 5. Matching, tolerance & validation rules

Baseline (strict three-way match, retained):

```
invoice.po_reference == po.po_number
AND invoice.quantity == grn.received_qty
AND invoice.unit_price == po.agreed_unit_price
```

Production layers configurable tolerance on top rather than rejecting every variance:

| Rule | Example policy | Outcome |
| --- | --- | --- |
| Price variance | ±2% | Within tolerance → pass |
| Quantity variance | ±5% | Within tolerance → pass or controlled exception |
| Tax variance | ≤ configured absolute threshold | Small rounding differences accepted |
| Shipping | Only approved contract/PO charges | Otherwise exception |
| Approval amount | > configured threshold | Mandatory human approval |

## 6. Duplicate detection

- Exact document-hash detection.
- Exact invoice-number + vendor match.
- Fuzzy invoice-number normalization (`INV-8821` ≡ `INV8821`).
- Similarity over vendor, amount, PO, date, line-item features.
- **Uncertain duplicates route to HITL, never auto-reject.**

Worked case: a document that looks different but matches vendor + invoice number + amount + PO should
score high on duplicate risk.

## 7. Risk & fraud scoring

Not a fraud *proof* mechanism — a **prioritization mechanism for review**.

| Signal | Example |
| --- | --- |
| Vendor anomaly | Invoice materially exceeds historical vendor amounts |
| Price anomaly | Unit price far outside historical or contracted range |
| Payment-change risk | Bank account changed shortly before a large invoice |
| Duplicate risk | Invoice closely resembles a previously processed one |
| New vendor risk | First invoice from a vendor with limited history |
| Behavioral anomaly | Unusual frequency, timing, amount, or charge pattern |

Illustrative output: overall `0.91` = duplicate `0.92`, bank-change `0.98`, vendor anomaly `0.84`, price
anomaly `0.71`. **The score must be explainable through its contributing signals.**

## 8. Exception Reasoning Agent

Receives deterministic discrepancy evidence; emits structured explanation + recommended action.
**It must not invent financial facts absent from the evidence.**

- Classify: price | quantity | tax | shipping | duplicate | contract | vendor | other.
- Explain using PO, GRN, invoice, contract, historical evidence.
- Recommend: approve | request corrected invoice | request credit note | escalate.
- Produce a structured action plan for the policy engine and the HITL interface.

## 9. Policy Engine

Business policy must not live inside agent prompts.

```
Risk < 0.30 AND all validations pass    → Auto-process
Risk 0.30–0.70 OR controlled variance   → Review / conditional action
Risk > 0.70 OR sensitive condition      → Mandatory HITL
Any failed safety/idempotency check     → Block execution
```

- Policies stored in PostgreSQL/configuration, not hardcoded in agent logic.
- **Version every policy** so historical decisions can be reconstructed.
- Record which policy version produced each decision.

## 10. HITL Dashboard

| View | Information |
| --- | --- |
| Overview | Processed, auto-approved, exceptions, pending reviews |
| Exception queue | Invoice, vendor, amount, exception type, risk, age, SLA |
| Invoice detail | Original document, extracted fields, PO, GRN, contract, calculations |
| Agent evidence | Reasons, tools used, confidence, discrepancy evidence |
| Decision controls | Approve, reject, request correction, escalate |
| Audit history | Every state transition and human action |

Human actions: authenticated, role-based, timestamped, written to the audit log. The evidence behind an
agent recommendation must be visible **before** approval.

## 11. ERP Execution Layer

```
Policy Approval → Authorization Check → Idempotency Check → ERP Adapter
  → Transaction Verification → Audit Event
```

- Mock ERP first, then adapters for real systems.
- **Idempotency keys** so retries cannot create duplicate financial transactions.
- Separate authorization from recommendation: an agent recommends; execution requires policy permission.
- Handle ERP timeouts and partial failures via retry + reconciliation workflows.

## 12. Auditability, reliability & security

- Immutable workflow events for every material decision.
- Store agent version, prompt/config version, model, tool calls, decision evidence.
- Correlation IDs across ingestion → LangGraph state → DB ops → ERP execution.
- Retries, exponential backoff, dead-letter handling, recovery from interrupted workflows.
- RBAC: AP operator, manager, finance admin, system service.
- Encryption, secrets management, least-privilege DB access.
- **Never allow arbitrary LLM-generated SQL or unrestricted ERP commands.**

## 13. AgentOps & evaluation

| Metric group | Examples |
| --- | --- |
| Extraction quality | Field accuracy, confidence, missing-field rate |
| Decision quality | Correct approval/rejection, exception classification accuracy |
| Operational | Processing time, throughput, retry rate, failure rate |
| AI economics | Tokens, model latency, LLM cost per invoice |
| Human interaction | HITL rate, override rate, approval time |
| Business | STP rate, exception rate, duplicate prevention, prevented overpayment |

Evaluation dataset: labeled invoice/PO/GRN benchmark with known ground truth; regression runs on any
model / prompt / schema / policy change; accuracy compared across model versions; FP/FN tracked for
duplicate and risk detection; results stored as **versioned experiment runs**.

## 14. Reference architecture

```
        Email / Upload / API
                 |
          Invoice Ingestion
                 |
          Vision Extraction
                 |
   Deterministic Validation Engine
   (PO + GRN + Tax + Contract +
    Tolerance + Math checks)
                 |
   Duplicate + Risk/Fraud Services
                 |
     Exception Reasoning Agent
                 |
           Policy Engine
            +----+----+
   Auto Resolution   HITL Dashboard
            +----+----+
                 |
           ERP Execution
                 |
          Audit / Event Log
                 |
          AgentOps + Evals
```

## 15. Canonical operational example (the golden path test)

TechCorp PO **#9901**: 10 laptops @ $1,000/unit. Warehouse receives **9** (one damaged). Supplier invoice
**INV-8821** bills **10 units @ $1,050** plus an unexpected **$200 shipping** fee.

| Stage | Expected behavior |
| --- | --- |
| Ingestion | Stores original, assigns correlation ID + document hash |
| Extraction | PO 9901, qty 10, unit price $1,050, shipping $200 |
| Validation | 10 billed vs 9 received; $1,050 vs $1,000; unapproved shipping |
| Duplicate | Checks number, vendor, amount, PO, hash, similarity vs history |
| Risk | Scores unusual price/shipping + vendor/payment anomalies |
| Reasoning | Classifies qty + price + unapproved-fee exceptions, proposes resolution |
| Policy | Splits automatable actions from manager-approval actions |
| HITL | Manager reviews evidence, decides the sensitive shipping question |
| ERP | Posts only the permitted transaction, after authorization + idempotency |
| AuditOps | Records every decision; measures time, cost, outcome, overrides |

Quantity variance = 10 billed − 9 received. Price variance is computed **against the accepted quantity**.
The shipping charge is resolved by configured contract/policy — never assumed valid or invalid.

## 16. Roadmap

| Phase | Build |
| --- | --- |
| 1 — Foundation | PostgreSQL schema, object storage, ingestion, Pydantic models, correlation IDs |
| 2 — Extraction | Vision extraction, structured output validation, confidence handling |
| 3 — Validation | PO/GRN matching, tolerance engine, tax/math validation, contract checks |
| 4 — Risk | Duplicate detection, vendor anomaly features, risk scoring |
| 5 — Agent | Exception reasoning, evidence-based action plans, LangGraph orchestration |
| 6 — Governance | Policy engine, HITL dashboard, RBAC, audit trail |
| 7 — Execution | Mock ERP adapter, idempotent posting, retries/reconciliation |
| 8 — AgentOps | Tracing, metrics, cost tracking, benchmark dataset, regression evaluation |
| 9 — Hardening | Security, load testing, failure injection, monitoring, deployment |

## 17. Technology stack

| Layer | Technology |
| --- | --- |
| Orchestration | Python + LangGraph |
| API | FastAPI |
| Data validation | Pydantic |
| Database | PostgreSQL |
| Vector / retrieval | pgvector *where justified by the retrieval use case* |
| State / caching | Redis |
| Document storage | Object storage |
| Agent tools | MCP or controlled internal tool interfaces |
| Vision | Vision-capable LLM / document model |
| Dashboard | React web application |
| Observability | OpenTelemetry-compatible tracing + metrics/logging |
| Evaluation | Versioned benchmark dataset + automated regression runs |
| Deployment | Docker; production orchestration as needed |

## 18. Definition of Done

Tracked as acceptance criteria in [`../PROGRESS.md`](../PROGRESS.md).

1. A valid invoice travels ingestion → ERP posting with no manual intervention when all policies pass.
2. An invoice with a price/quantity/tax/contract discrepancy is explained and routed correctly.
3. A repeated invoice is detected without creating a duplicate payment.
4. High-risk invoices are blocked or routed to authorized human reviewers.
5. Every financial action is reproducible from the audit trail.
6. ERP retries cannot create duplicate transactions.
7. Model or prompt changes can be evaluated against a fixed benchmark.
8. The system exposes measurable STP, accuracy, latency, cost, failure, and human-override metrics.
9. The LLM cannot bypass deterministic validation, policy, authorization, or execution controls.
