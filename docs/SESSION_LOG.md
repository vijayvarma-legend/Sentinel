# Session Log

A running journal of what we did, what we decided in passing, and what the next session should pick up.
Newest first. Decisions substantial enough to constrain future work get an ADR and are linked from here.

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
