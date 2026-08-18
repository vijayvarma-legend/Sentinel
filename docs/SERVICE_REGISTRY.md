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
| `composition` | The wiring layer. May depend on everything; decides nothing ([ADR-0008](decisions/0008-composition-root-trust-class.md)). Exactly one module. |

---

## Platform services

| ID | Service | Module | Trust | Status | Responsibility |
| --- | --- | --- | --- | --- | --- |
| SVC-00 | Core domain | `sentinel.core` | `deterministic` | **built** | Pydantic models, money type, correlation IDs, error taxonomy, settings |
| SVC-01 | Persistence | `sentinel.db` | `deterministic` | **built** | PostgreSQL schema, migrations, repositories, least-privilege roles |
| SVC-02 | Document store | `sentinel.storage` | `deterministic` | **built** | Original document bytes, content-addressed, immutable |
| SVC-03 | Orchestrator | `sentinel.graph` | `deterministic` | planned | LangGraph pipeline, state, checkpointing, retries, dead-letter |
| SVC-04 | AuthN/AuthZ | `sentinel.auth` | `control` | planned | RBAC: ap_operator, ap_manager, finance_admin, system |
| SVC-05 | HTTP API | `sentinel.api` | `composition` | **built** | FastAPI transport + the composition root |

## Pipeline services

| ID | Service | Module | Trust | Status | Spec |
| --- | --- | --- | --- | --- | --- |
| SVC-10 | Invoice Ingestion | `sentinel.ingestion` | `deterministic` | **built** | §4.1 |
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

## Built so far

### SVC-00 `sentinel.core` — the domain layer · `deterministic` · **built**, 175 tests

Depends on nothing else in Sentinel (enforced by the architecture test). Everything above it
is built from these pieces.

| Module | What it guarantees |
| --- | --- |
| `money` | Exact, currency-safe amounts. See below. |
| `ids` | Typed, time-ordered identifiers; SHA-256 document hashes; **derived** idempotency keys |
| `errors` | System faults, separated from business outcomes |
| `enums` | The closed vocabularies the pipeline speaks |
| `settings` | Configuration validated at startup, not mid-invoice |
| `business` | PO, GRN, contract, vendor profile — the ground-truth documents (spec §3) |
| `evidence` | Every contract that moves between services |

**Three invariants are enforced by constructors rather than by convention**, which is what
turns spec prose into something a test can check:

| Type | Refuses to exist when | Spec |
| --- | --- | --- |
| `RiskAssessment` | the overall score lies outside the range of its own contributing signals — nothing could explain it | §7 |
| `ExceptionFinding` | it cites no evidence — indistinguishable from an invented financial fact | §8 |
| `HumanDecision` | its author is the `SYSTEM` role — that would manufacture the approval mandatory-HITL exists to require | §10 |

Smaller ones worth knowing: a `CheckResult` that fails must carry the numbers that failed it;
an empty `ValidationScorecard` does **not** pass (a validation outage must not auto-approve);
a successful `ErpTransactionResult` must name the transaction it created; an absorbed retry is
distinguishable from a fresh posting.

`ExtractedField[T]` binds a value to its confidence in one object, so a caller cannot read the
number and forget how sure the model was.

`EvidenceBundle` is what the orchestrator hands the reasoning agent — which has no data access
of its own — and what the HITL dashboard renders. Its `evidence_ref_ids()` enumerates exactly
what a finding may cite, making grounding testable.

**Golden-path fixture:** `tests/golden.py` holds the spec §15 scenario (PO 9901 / GRN 9
accepted / INV-8821) in one place. Every phase from validation onward tests against it.

### SVC-10 `sentinel.ingestion` — the front door · `deterministic` · **built**, 22 tests

Sniffs the leading bytes rather than trusting the client's `Content-Type`: an uploader who
mislabels a `.docx` as `application/pdf` is told so at the door, not two stages later when a
vision model returns nonsense.

The correlation ID is minted **before** validation, so a rejected document still has an
identifier the caller can quote and the dead-letter record can be found by. A rejection is
recorded — dead-letter row + audit event — never silently dropped, and creates no invoice and
no stored object.

### SVC-05 `sentinel.api` — HTTP + wiring · `composition` · **built**, 19 tests

`POST /invoices` · `GET /invoices/{correlation_id}` · `GET /invoices/{correlation_id}/audit`
· `GET /health`

An `IngestionError` maps to **422, not 500**: the request was well-formed, the document was
not, and a 500 would tell an integrator to retry a PDF that is still corrupt on the second
attempt. Every error response carries the correlation ID.

**Transaction boundary.** The request-scoped session rolls back on any fault — but *commits*
on `IngestionError`, because that exception reports a decision ingestion has already made and
recorded. Rolling it back erased the dead-letter row the 422 response points at. Found by
running the live app; the test suite had substituted the faulty behaviour away. Regression
test: `tests/api/test_transaction_boundary.py`.

### SVC-01 `sentinel.db` — the schema · `deterministic` · schema **built**, 14 integration tests

Some guarantees are stated in Python and *enforced* here, because a constraint the
application checks is a constraint two concurrent workers can both pass.

| Guarantee | Mechanism | Verified |
| --- | --- | --- |
| **DoD-6** — ERP retries cannot double-post | `UNIQUE` on `erp_transactions.idempotency_key` | ✅ second insert refused |
| **Spec §12** — the audit log is immutable | trigger raising on `UPDATE`/`DELETE`, binding the app's own role | ✅ both refused |
| **Spec §9** — a decision is replayable against its rules | trigger: policy rules immutable, `is_active` still mutable | ✅ edit refused, toggle allowed |
| **ADR-0007** — damaged ⊆ received | `CHECK (damaged_qty <= received_qty)` | ✅ refused |
| A successful posting names its transaction | `CHECK (NOT succeeded OR erp_transaction_id IS NOT NULL)` | ✅ refused |
| One correlation ID, one invoice | `UNIQUE` on `invoices.correlation_id` | ✅ refused |
| **Spec §6** — duplicates are *detected*, not rejected | `(vendor_id, normalized_invoice_number)` indexed but **not** unique | ✅ both rows accepted |

That last row is a deliberate non-constraint. A unique constraint there would turn a
suspected duplicate into an insert failure — no evidence, no assessment, and nothing for a
human to review.

Amounts are `NUMERIC(18,2)` beside a currency column; timestamps are `TIMESTAMPTZ`. Model
outputs (extraction, reasoning) are `JSONB`, since their shape changes with the prompt —
while every field a decision depends on is recomputed by validation into a typed column.

Migrations: Alembic, `migrations/`. The URL comes from `Settings`, so no credential is
committed.

### SVC-02 `sentinel.storage` — the document store · `deterministic` · **built**, 17 tests

Content-addressed and write-once. Two properties fall out rather than being maintained:

- **Idempotent ingestion.** A document's key *is* its SHA-256, so re-submitting the same
  bytes lands on the same key and stores nothing. No bookkeeping to get wrong.
- **Corruption is detectable.** `get()` re-hashes what it read and compares against the key
  it was fetched from. Without that check, a truncated object would be handed to extraction
  and read as though it were the invoice — and the resulting payment would be perfectly
  auditable and completely wrong.

`InMemoryDocumentStore` implements the identical contract, including read verification, so
the same test bodies run against both. It ships in the package rather than the test suite
because the evaluation harness (spec §13) needs to replay a benchmark set without leaving
debris in a bucket.

Integration tests run the same assertions against real MinIO — `make test-int`.

### `sentinel.core.money` — the money type · `deterministic`

The foundation every financial guarantee rests on, so it is the first thing built and the
most heavily tested (34 cases).

| | |
| --- | --- |
| `Money` | Immutable amount + ISO 4217 currency. Rejects `float` at construction. Refuses cross-currency add/subtract/compare. Refuses `Money * Money`. |
| Rounding | `ROUND_HALF_UP` — the finance convention, deliberately not Python's default `ROUND_HALF_EVEN` |
| Serialization | Amount serializes as a JSON **string**; a JSON number is an IEEE 754 double, which would reintroduce the exact problem the type prevents |
| `percentage_variance` | Signed, positive when the supplier billed above the agreed figure. **Raises on a zero baseline** rather than returning 0 or infinity — an unapproved charge is its own exception category, not a variance |

Known simplification: minor units are fixed at 2 decimal places. ISO 4217 has 0- and 3-decimal
currencies (JPY, KWD); supporting them turns one constant into a per-currency lookup.

---

## Cross-cutting invariants (apply to every service)

1. **No LLM output directly posts money.** SVC-20 and SVC-60 are the only `llm` services, and neither has an execution path.
2. **No LLM-generated SQL. No unrestricted ERP commands.** Data access is through controlled repositories only.
3. Every record carries a **correlation ID** from ingestion through ERP.
4. Money is `Decimal`, never `float`.
5. Every material decision emits an audit event before the next stage runs.
