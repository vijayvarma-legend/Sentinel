# Decision Log

Architecture Decision Records. One file per decision, numbered, immutable once accepted.

**When to write one:** any choice that would be expensive to reverse, that a future reader would
otherwise have to reverse-engineer from the code, or that we argued about. Library picks that are
obvious and cheap to swap do not need one.

**When a decision changes:** do not edit the accepted ADR. Write a new one that supersedes it, and add
a `Superseded by ADR-XXXX` line to the old one's status. The record of having been wrong is the point.

**Status values:** `proposed` · `accepted` · `superseded by ADR-XXXX` · `rejected`

## Index

| # | Title | Status | Date |
| --- | --- | --- | --- |
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | accepted | 2026-08-19 |
| [0002](0002-python-toolchain.md) | Python 3.13 + uv for the toolchain | accepted | 2026-08-19 |
| [0003](0003-modular-monolith.md) | Modular monolith, not microservices | accepted | 2026-08-19 |
| [0004](0004-trust-classes.md) | Trust classes, structurally enforced | accepted | 2026-08-19 |
| [0005](0005-pipeline-stage-ordering.md) | Risk scoring runs before exception reasoning | accepted | 2026-08-19 |
| [0006](0006-v1-boundaries.md) | v1 boundaries: ingestion sources, object storage, deferred vision model | accepted | 2026-08-19 |
| [0007](0007-quantity-semantics.md) | Accepted quantity is the basis for matching and price variance | accepted | 2026-08-19 |
| [0008](0008-composition-root-trust-class.md) | A `composition` trust class for the wiring layer | accepted | 2026-08-19 |
| [0009](0009-confidence-thresholds.md) | Tiered confidence thresholds, and two bands rather than one | accepted | 2026-08-19 |

Template: [`template.md`](template.md)
