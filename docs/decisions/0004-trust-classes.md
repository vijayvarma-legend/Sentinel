# ADR-0004: Trust classes, structurally enforced

- **Status:** accepted
- **Date:** 2026-08-19
- **Phase:** 0
- **Affects:** all, especially SVC-20, SVC-60, SVC-70, SVC-90

## Context

The spec's prime directive is that no LLM output directly posts money, and DoD-9 states it as an
acceptance criterion: *the LLM cannot bypass deterministic validation, policy, authorization, or
execution controls*.

"Cannot" is a strong word. A convention that reviewers enforce is not a "cannot" — it is a "usually
doesn't." Over a nine-phase build, with refactors and new contributors, a single well-meaning import
from the reasoning agent into the ERP client would silently void the system's central safety property,
and nothing would fail.

## Decision

Every module is assigned a **trust class**, declared in
[`SERVICE_REGISTRY.md`](../SERVICE_REGISTRY.md) and mirrored in module metadata:

| Class | May decide financial facts | May authorize execution |
| --- | --- | --- |
| `deterministic` | yes | no |
| `llm` | **no** | **no** |
| `control` | no | yes |
| `human` | no | yes, within role limits |
| `observability` | no | no |

Two rules follow, and both are enforced by an automated architecture test in the standard suite:

1. **No `llm` module may import, or transitively reach, `sentinel.erp` or any repository write path.**
2. **No `control` module may import an `llm` module.** Evidence produced by an LLM reaches the policy
   engine only as inert data on the pipeline state — never as a live call.

`llm` modules are pure functions of their input evidence to a structured recommendation. They have no
database write access and no ERP client in scope.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Code review + a documented convention | Not a guarantee; DoD-9 asks for a property, not a habit. Fails silently and invisibly |
| Runtime guard at the ERP boundary (inspect the call stack) | Catches the violation in production rather than in CI, and is defeated by any async hop |
| Separate processes with a network boundary | Gives real isolation but at the cost rejected in [ADR-0003](0003-modular-monolith.md); the import test buys most of the safety for none of the operational price |

## Consequences

DoD-9 becomes a test that fails a build rather than a claim in a document. Anyone attempting the unsafe
import gets a red CI run explaining why, which is also how the constraint teaches itself to newcomers.

The cost is real friction: when an LLM module genuinely needs data, it must be handed that data by the
orchestrator instead of fetching it. That is a slightly more verbose call graph, and it is the point —
the verbosity is the safety property made visible.

Anything that must move money is written in a `deterministic` or `control` module, with no exceptions
granted quietly. If an exception ever seems necessary, that is a superseding ADR, not a code comment.
