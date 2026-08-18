# ADR-0006: v1 boundaries — ingestion sources, object storage, deferred vision model

- **Status:** accepted
- **Date:** 2026-08-19
- **Phase:** 0 → 1
- **Affects:** SVC-02, SVC-10, SVC-20

## Context

The spec describes the production surface: four ingestion sources (§4.1), object storage (§17), and a
vision-capable model (§4.2). Building all of it before anything runs end-to-end would put the riskiest,
most external-dependency-heavy work first, and would gate the deterministic core — where the system's
actual financial guarantees live — behind an API key and an IMAP parser.

The goal for v1 is a complete working system with mocked externals: every stage real, the golden path
(spec §15) actually executing, and only the outside world simulated.

## Decision

**Ingestion (SVC-10): REST API + file upload for v1.** Email polling and batch import are deferred, but
the normalizer is written to a `DocumentSource` abstraction from the start, so adding them is a new
adapter rather than a change to the ingestion core.

**Object storage (SVC-02): MinIO via docker-compose.** The application talks S3 through one storage
interface. Local dev therefore exercises real object-store semantics — presigned URLs, content-addressed
keys, immutability — and the move to S3 or another provider is configuration, not code.

**Vision extraction (SVC-20): contract now, model later.** We define `ExtractedInvoice` with field-level
confidence and source references, and ship a deterministic fixture-backed extractor implementing it. The
real model is selected in Phase 2, behind the same interface. Q-1 stays open until then.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Email ingestion in v1 | Credential handling, MIME/attachment edge cases, and mailbox state — a large surface that teaches us nothing about the financial core |
| Filesystem storage behind an S3-shaped interface | Zero infrastructure, but object-store semantics stay untested until the swap, which is when they would break |
| Pick the vision model now | Locks in a dependency before the contract it must satisfy is proven; the fixture extractor makes the whole pipeline testable without one |

## Consequences

The deterministic core — validation, duplicates, risk, policy, ERP — can be built and fully tested
without any model call or API key. That means the golden path is exercisable in CI from Phase 3 onward,
which is a significant testability win and directly serves DoD-2.

The fixture extractor is not a throwaway: it becomes the fake that keeps integration tests fast and
deterministic once a real model is wired in, and it is the baseline the benchmark suite compares against.

The deferred work is real and must not be forgotten: email and batch ingestion, and the Phase 2 model
selection. All three are tracked in [`PROGRESS.md`](../PROGRESS.md).
