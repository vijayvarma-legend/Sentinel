# Session Log

A running journal of what we did, what we decided in passing, and what the next session should pick up.
Newest first. Decisions substantial enough to constrain future work get an ADR and are linked from here.

---

## 2026-08-19 — Session 3: Phase 1 Foundation complete

**Goal:** a working front door — document in, stored, recorded, auditable.

### Done

`sentinel.core` (SVC-00), `sentinel.storage` (SVC-02), `sentinel.db` (SVC-01),
`sentinel.ingestion` (SVC-10), `sentinel.api` (SVC-05). **248 tests**, ruff + mypy strict
clean. The live app was run against real PostgreSQL and MinIO end to end.

### Worth remembering

- **The transaction-boundary bug, and why the tests missed it.** Ingestion wrote the
  dead-letter row and its audit event, then raised; the request-scoped session saw an
  exception, rolled back, and erased both. The API answered 422 with a correlation ID
  pointing at nothing — a documented rejection turned into a silent drop, which is precisely
  what the dead-letter path exists to prevent.

  It was invisible to the API tests because they override `get_session` with a fixture that
  does not roll back on exception — **the tests had substituted away the behaviour that was
  broken.** It surfaced only when the real app ran and `dead_letters` came back empty.
  Fixed by committing on `IngestionError` (a recorded decision, not a fault) while genuine
  faults still roll back. `tests/api/test_transaction_boundary.py` uses the real dependency
  and reads back on a separate connection.

  *A test that overrides the component under suspicion is not testing it.*

- **Database-level enforcement.** Ten guarantees were checked by hand against the running
  database before being captured as tests: audit append-only, idempotency uniqueness,
  policy-rule immutability, damaged ⊆ received, success-names-its-transaction, one
  correlation ID per invoice, and the deliberate *non*-constraint that lets duplicates in so
  they can be assessed.

- **`ErpTransactionRepository.claim` inserts rather than checking first.** The unique index
  decides the race. A check-then-act version would pass every test and double-pay under
  concurrency — the losing worker gets `IdempotencyConflict` carrying the winner's row.

- Two smaller mistakes, both caught by tests: SQLAlchemy's unit of work is free to order a
  bare-FK insert before its parent (no ORM relationship to order by), and `session.execute`
  on raw SQL fires immediately rather than at flush.

### Decisions taken

[ADR-0007](decisions/0007-quantity-semantics.md) — `damaged_qty` is a subset of
`received_qty`; `accepted_qty = received − damaged` is the basis for both matching and price
variance. The spec never states the relationship and §15 reads either way.

[ADR-0008](decisions/0008-composition-root-trust-class.md) — a `composition` trust class for
`sentinel.api`. The wiring layer must depend on every class including `llm`, and neither
existing class fits honestly. The exemption is real, so exactly one such module is allowed,
enforced by the registry test.

### Next session — Phase 2 Extraction

1. The `Extractor` protocol and the fixture-backed implementation (ADR-0006).
2. Confidence gating: reject rather than pass a low-confidence payload downstream (spec §4.2).
3. Persist the extraction payload; advance the invoice to `EXTRACTED`.
4. Then Phase 3 validation — where the golden path finally computes real numbers.

**Q-1 becomes live at Phase 2's end:** which vision model, and what confidence threshold
rejects a payload.

---

## 2026-08-19 — Session 2: Phase 0 complete

**Goal:** close out project setup and make DoD-9 a build failure rather than a promise.

### Done

- Installed `uv`; `pyproject.toml` + committed `uv.lock`. Ruff, mypy strict, pytest, import-linter wired.
- Package skeleton: 16 modules under `src/sentinel/`, one per registry service, each declaring its own
  `SERVICE_ID` and `TRUST_CLASS` in its docstring and module metadata.
- `docker-compose.yml`: PostgreSQL 17, Redis 7, MinIO. All three verified healthy and reachable
  (`select version()`, `PING`, bucket created over the S3 API).
- `Makefile` with `setup / up / down / fmt / lint / types / test / check`.
- **[`tests/architecture/test_trust_boundaries.py`](../tests/architecture/test_trust_boundaries.py)** —
  the enforcement of [ADR-0004](decisions/0004-trust-classes.md). 14 tests. It walks the import graph by
  parsing source (so it holds even for unimportable modules) and cross-checks with import-linter over
  the real graph.
- `make check` green: ruff clean, mypy strict clean on 17 files, 14/14 tests.

### Worth remembering

- **Port collision.** This machine already runs a `kinnred` project on 5433 / 6380 / 9000-9001, plus a
  local PostgreSQL 17. Sentinel moved to **5434 / 6381 / 9010-9011**. Those containers were left alone.
- **The guard had a real bug, found by testing the test.** The first version passed against a deliberately
  injected violation: the AST walk recorded `node.module` for `ImportFrom`, so `from sentinel import erp`
  registered as an import of `sentinel`, not of `sentinel.erp` — invisible. Fixed by also recording
  `f"{module}.{alias}"` for every alias. Re-verified against three violation shapes: 2-hop transitive
  (`reasoning → graph → erp`), direct dotted (`extraction → sentinel.db`), and control-imports-llm
  (`policy → reasoning`). All three now fail loudly with the offending path printed.
  *A safety test that has never been seen to fail is not evidence of safety.*
- Non-obvious spec detail now pinned in the SVC-30 contract: price variance is computed against the
  **accepted** quantity, not the billed quantity (spec §15).

### Decisions taken

[ADR-0006](decisions/0006-v1-boundaries.md) — v1 takes API + upload ingestion only, MinIO for object
storage, and defers the vision-model choice behind a fixture-backed extractor so the deterministic core
is fully testable without an API key. Q-3 and Q-4 resolved; Q-1 deliberately left open until Phase 2.

### Next session — Phase 1 Foundation

1. `sentinel.core`: money as `Decimal`, correlation IDs, error taxonomy, settings, the document/PO/GRN/
   invoice domain models.
2. `sentinel.db`: schema + Alembic migrations for PO, GRN, invoice, contract, vendor, policy, audit.
3. `sentinel.storage`: content-addressed S3 store, immutable writes.
4. `sentinel.ingestion`: API + upload, hashing, correlation ID minting, dead-letter path.
5. Seed the golden-path fixture (PO 9901 / GRN 9 units / INV-8821) — every later phase tests against it.

---

## 2026-08-19 — Session 1: Project inception

**Goal:** turn the architecture PDF into a tracked, buildable project.

### Done

- Extracted the source spec (`Autonomous_AP_Automation_Architecture_v2.pdf`, 8pp) and archived both the
  PDF and its text extraction under `docs/architecture/source/`.
- Normalized it into [`docs/architecture/SPEC.md`](architecture/SPEC.md) — all 18 sections, tables
  preserved, with the source PDF named as the tiebreaker if the two ever disagree.
- Stood up the tracking spine: [service registry](SERVICE_REGISTRY.md), [progress tracker](PROGRESS.md),
  [decision log](decisions/), this journal.
- Surveyed the toolchain: Python 3.13.1, Docker 29.7.2, Compose v5.3.1, PostgreSQL 17.6 client,
  Node 24.13.0. No `uv` yet; the Poetry on PATH is a broken shim pointing at a removed Store Python.
- Wrote ADRs 0001–0005.

### Found in the spec

- **Stage-ordering contradiction.** §2's numbered table puts Exception Reasoning at 5 and Risk at 6, but
  the pipeline arrow and the §14 diagram both run risk first. Resolved in favour of risk-first —
  [ADR-0005](decisions/0005-pipeline-stage-ordering.md).
- **DoD-9 needs teeth.** "The LLM cannot bypass deterministic validation, policy, authorization, or
  execution controls" is a structural property, not a review guideline. Made it an enforced import
  constraint — [ADR-0004](decisions/0004-trust-classes.md).
- **Price variance is computed against the accepted quantity**, per §15 — easy to get wrong by computing
  it against billed quantity. Written into the SVC-30 contract so the golden-path test pins it.
- The spec hedges pgvector with "where justified by the retrieval use case." Not adopting it until a
  concrete retrieval need appears; noted so it does not get pulled in by reflex.

### Decisions taken

| | |
| --- | --- |
| [ADR-0001](decisions/0001-record-architecture-decisions.md) | Keep a numbered, immutable decision log |
| [ADR-0002](decisions/0002-python-toolchain.md) | Python 3.13 + uv, committed lockfile |
| [ADR-0003](decisions/0003-modular-monolith.md) | Modular monolith with enforced module boundaries |
| [ADR-0004](decisions/0004-trust-classes.md) | Trust classes, enforced by an architecture test |
| [ADR-0005](decisions/0005-pipeline-stage-ordering.md) | Risk scoring before exception reasoning |

### Next session

1. Install `uv`; create `pyproject.toml` and the package skeleton mirroring the service registry.
2. `docker-compose.yml` for PostgreSQL + Redis (+ object storage, pending Q-4).
3. The architecture test from ADR-0004 — written before there is anything for it to catch.
4. Phase 1 foundation: core domain models, correlation IDs, money as `Decimal`, database schema.

### Open questions raised

Q-1 (vision model + confidence threshold), Q-2 (real ERP target), Q-3 (email ingestion in v1?),
Q-4 (object storage: MinIO vs filesystem), Q-5 (benchmark dataset: synthetic vs anonymized real).
Tracked in [`PROGRESS.md`](PROGRESS.md#open-questions). Q-3 and Q-4 block Phase 1.
