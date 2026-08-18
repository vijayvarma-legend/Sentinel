# ADR-0007: Accepted quantity is the basis for matching and price variance

- **Status:** accepted
- **Date:** 2026-08-19
- **Phase:** 1
- **Affects:** SVC-00, SVC-30

## Context

The spec's GRN model (§3) carries both `received_qty` and `damaged_qty`, but never states
their relationship, and its worked example (§15) can be read two ways.

The narrative says *"The warehouse receives 9 units because one is damaged"* — which reads as
10 units arriving, 1 damaged, 9 usable. The validation line then says *"Finds 10 billed vs 9
received"* — which reads as `received_qty = 9`. Both readings produce 9 as the number that
matters, but they disagree about what `received_qty` means, and therefore about what
`damaged_qty` is for. If `received_qty` already excludes damaged units, `damaged_qty` is
decorative.

The choice is not cosmetic. Under one reading a supplier who ships 10 and damages 1 has
delivered 10; under the other they have delivered 9. That is the difference between paying
for a broken laptop and not paying for it.

A second, sharper question sits behind it. Spec §15 says the price variance is computed
*"against the accepted quantity"* — so "accepted quantity" is a concept the spec relies on
without ever defining.

## Decision

`damaged_qty` is a **subset** of `received_qty`, never additional to it. A GRN line rejects
`damaged_qty > received_qty` at construction.

```
accepted_qty = received_qty - damaged_qty
```

**`accepted_qty` is the quantity the three-way match uses**, and the quantity the price
variance is computed against. It is a computed property on `GoodsReceiptLine`, defined once
in `sentinel.core.business`, so no downstream module can quietly adopt a different rule.

The golden path is therefore modelled as `received_qty=10, damaged_qty=1 → accepted_qty=9`.
This satisfies both readings of §15 — 9 is the operative number either way — while giving
`damaged_qty` real meaning and preserving the fact that ten physical units did arrive, which
a returns or credit-note workflow will need.

## Alternatives considered

| Option | Why not |
| --- | --- |
| `received_qty` already excludes damaged units | Makes `damaged_qty` decorative and loses the count of what physically arrived — information a credit-note workflow needs |
| Match against `received_qty`, handle damage separately downstream | Pays for goods that arrived broken, then relies on a second process to claw it back. The spec's whole design is to catch this *before* posting |
| Leave it to the validation engine | The relationship is a property of the document, not of one consumer. Defining it in a single module is what stops two stages disagreeing |

## Consequences

Quantity semantics are unambiguous and defined in exactly one place. The golden-path test
pins the behaviour: 10 billed against 9 accepted is a quantity exception, and the price
variance is computed on 9 units, not 10.

The reading is ours, not the spec's — the source document genuinely does not say. If a real
AP department's GRN feed turns out to use the other convention, the fix is an adapter at
ingestion that normalizes into this model, not a change to the matching rule. That keeps the
ambiguity at the boundary rather than in the financial core.
