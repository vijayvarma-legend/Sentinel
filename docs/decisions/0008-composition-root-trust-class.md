# ADR-0008: A `composition` trust class for the wiring layer

- **Status:** accepted
- **Date:** 2026-08-19
- **Phase:** 1
- **Affects:** SVC-05, and the enforcement rules from [ADR-0004](0004-trust-classes.md)

## Context

[ADR-0004](0004-trust-classes.md) assigns every module a trust class and enforces two rules:
an `llm` module may not reach execution or persistence, and a `control` module may not import
an `llm` module. Those rules assume every module sits somewhere in the pipeline.

The HTTP application does not. Something has to construct the document store, open a database
session, build the ingestion service, and hand the reasoning agent its evidence — and that
something necessarily depends on modules of every class, including both `llm` modules and
`sentinel.erp`. Under the existing taxonomy there is no honest class for it: calling it
`deterministic` would be true of its code but would quietly exempt it from the rules by
accident, and calling it `control` would forbid the wiring it exists to do.

Leaving it unclassified is worse. The registry test asserts that every package on disk has a
row, precisely so a new module cannot slip in unexamined.

## Decision

A fifth trust class, `composition`, for the composition root — `sentinel.api`.

| | |
| --- | --- |
| **May** | import any module, of any trust class |
| **Must not** | contain any financial calculation, policy evaluation, or authorization decision |

The permission is the point and so is the constraint. A composition root wires objects
together and translates HTTP to domain calls; the moment it *decides* anything, that decision
has escaped the module whose trust class governs it, and the enforcement in ADR-0004 stops
meaning anything.

There is exactly one `composition` module. That is enforced by the registry test, so a second
one cannot appear as a convenient place to put logic that belongs in a real service.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Classify the API as `deterministic` | Accurate about the code, but silently grants a blanket exemption from the ADR-0004 rules to a module nobody thought of as exempt |
| Classify it as `control` | Forbids importing `llm` modules, which is exactly the wiring the composition root exists to do |
| Wire everything inside each service | Every service would construct its own dependencies, which is how a monolith's module boundaries dissolve ([ADR-0003](0003-modular-monolith.md)) |
| Leave it out of the registry | The registry test exists to stop unexamined modules appearing. Suppressing it to avoid a taxonomy question is the wrong trade |

## Consequences

The trust taxonomy stays truthful: every module's class describes what it may actually do,
and no module is exempt by accident.

The cost is that `sentinel.api` is not covered by the import rules, so its discipline —
wiring only, never deciding — rests on review rather than on a test. That is a real weakening
and worth naming. It is bounded by there being exactly one such module, and by that module
having no reason to grow: every endpoint should read as *parse request → call service →
serialize result*. A function there that computes a variance or picks a route is a bug to be
moved, not a style preference.
