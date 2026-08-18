# ADR-0001: Record architecture decisions

- **Status:** accepted
- **Date:** 2026-08-19
- **Phase:** 0
- **Affects:** all

## Context

Sentinel is a financial system with a nine-phase roadmap and a hard regulatory-shaped requirement:
*every financial action must be reproducible from the audit trail* (spec §18). Reproducing a decision
means knowing not just what the code did, but which rule version and which design constraint made it do
that. The build spans months and many sessions; the reasoning behind a choice evaporates far faster
than the code that encodes it.

The spec itself already demands versioned policies and versioned prompts. Extending the same discipline
to architecture choices costs almost nothing and closes the gap between "the system is auditable" and
"the system's design is auditable."

## Decision

We keep a numbered decision log in `docs/decisions/`. One file per decision, immutable once accepted;
changes are made by writing a superseding ADR, never by editing history.

Every ADR names the roadmap phase and the affected services, so the registry, the tracker, and the log
cross-reference each other.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Decisions in commit messages | Not discoverable; a rationale spread across 40 commits is not a record |
| A single `DECISIONS.md` | Grows into an unreadable append-only wall; no stable link target per decision |
| A wiki or external tracker | Drifts from the code; not reviewable in the same PR as the change it justifies |

## Consequences

Writing an ADR is a small tax on every non-obvious choice, paid at the moment the reasoning is freshest.
In exchange, any future session — human or agent — can reconstruct why the system is shaped the way it
is without archaeology.

The risk is ADRs for trivia. The guard is the rule in `README.md`: write one when reversal is expensive,
when a reader would otherwise reverse-engineer the reasoning, or when we argued about it.
