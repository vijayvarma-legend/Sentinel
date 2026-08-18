# ADR-0009: Tiered confidence thresholds, and two bands rather than one

- **Status:** accepted
- **Date:** 2026-08-19
- **Phase:** 2
- **Affects:** SVC-20, SVC-30, SVC-70

## Context

Spec §4.2 requires extraction to "reject malformed or low-confidence payloads instead of
silently passing them downstream". `Settings.extraction_min_confidence` currently holds a
single number, `0.80`, applied to nothing yet. Phase 2 has to make it real, and the
placeholder is wrong in two independent ways.

**A single number treats every field as equally dangerous.** It is not. The decisive question
for any extracted field is *what catches it if the model reads it wrong* — and the answer
varies enormously:

| Field | If misread | Caught by |
| --- | --- | --- |
| `billed_unit_price` | wrong price | SVC-30 compares against the PO price |
| `billed_qty` | wrong quantity | SVC-30 compares against GRN accepted quantity |
| `line_total` | wrong line total | SVC-30 recomputes price × quantity |
| `po_reference` | **validated against the wrong PO** | **nothing** |
| `invoice_number` | **duplicate detection misses the prior invoice** | **nothing** |
| `supplier_name` | wrong contract, wrong history, wrong bank | **nothing** |
| `currency` | 1,000 JPY paid as 1,000 USD | **nothing** |
| `shipping` | an unapproved charge, by definition without a PO counterpart | **nothing** |
| `description` | cosmetic | irrelevant |

The three-way match is itself a confidence mechanism for the fields it covers. A unit price
read as $1,050 when the page says $1,950 will fail validation loudly. But a PO reference read
as `9901` when the page says `9907` produces a *clean pass against the wrong baseline* — every
downstream check agrees, and the wrong invoice is paid with a perfect audit trail. Under one
global threshold, the field that most needs scrutiny gets exactly as much as the one that
needs least.

**Reject-or-accept is too blunt a response.** A field at 0.93 is not malformed; it is
slightly doubtful. Rejecting it outright discards a readable invoice and creates manual work;
accepting it silently lets a doubtful number reach a policy engine that will happily
auto-process it. The spec's own architecture already has the right destination for
"processable but not trustworthy enough to automate" — a human (§10).

## Decision

### Two bands, not one threshold

| Band | Condition | Outcome |
| --- | --- | --- |
| **Reject** | below the field's reject floor | `ExtractionRejected`. The read is too poor to be evidence. |
| **Review** | between the floors | Payload proceeds, marked. **Can never auto-process** — policy must route to a human. |
| **Accept** | at or above the review floor | Eligible for straight-through processing. |

A rejection is **not** a dead-letter. Dead-lettering (§4.1) is for a document Sentinel cannot
accept; this is a *valid document we failed to read*. The invoice is retained and routed for
human attention, with the offending fields named.

### Thresholds by field class

| Class | Fields | Reject below | Review below |
| --- | --- | --- | --- |
| **Identity** | `invoice_number`, `po_reference`, `supplier_name`, `currency` | 0.85 | **0.98** |
| **Unchecked money** | `total_due`, `tax`, `shipping`, `invoice_date` | 0.85 | 0.95 |
| **Cross-checked money** | `billed_qty`, `billed_unit_price`, `line_total`, `item_id`, `subtotal` | 0.70 | 0.90 |
| **Cosmetic** | `description` | — | — |

The ordering is the substance, and it is the opposite of the intuitive one: **the fields
subject to deterministic cross-checking get the *lowest* thresholds.** They are the ones
another part of the system will catch. Scrutiny is spent where nothing else is watching.

Identity sits highest at 0.98 because its failure mode is silent and total, and because these
fields are usually printed as clear labelled headers — a model that is unsure of an invoice
number on a clean page is telling us something real. Currency earns the same treatment for
the opposite reason: it is frequently *not* printed at all, only implied by a `$` that could
be four different currencies, and an ambiguous currency genuinely should reach a human.

### A missing field is not a low-confidence field

`po_reference` may legitimately be absent — non-PO invoices exist. Absence is `None` and
routes to its own exception path. It is never treated as low confidence, and the gate never
manufactures doubt about a field that simply is not there.

### The policy is versioned

Thresholds live in code as a named, versioned `ConfidencePolicy` (`confidence-v1`), recorded
on every extraction audit event. Changing a threshold means publishing a new version, exactly
as spec §9 requires for policy — because these numbers decide whether an invoice may be paid
without a human, which makes them decision-affecting whatever we call them.

## What these numbers are, and are not

**They are a triage heuristic, not a probability.** A model reporting 0.95 is not making a
calibrated statement that it is right 95% of the time; self-reported confidence from a
language model is a number of unknown calibration, and treating it as a probability would be
false precision. That is also why joint confidence is deliberately *not* computed: multiplying
ten field confidences to get a payload confidence would compound an uncalibrated,
non-independent quantity into a number that looks rigorous and means nothing.

The real safety net is not the threshold. It is that SVC-30 recomputes every figure it can
against the PO, the GRN, and the supplier's own printed totals — arithmetic that does not care
how confident the model claimed to be. Confidence gating catches what recomputation cannot
reach; recomputation catches what confidence gating gets wrong. Neither is trusted alone.

**The specific values are a starting point, and are expected to move.** Nobody can justify
0.98 over 0.96 without measurement. Phase 8's benchmark (spec §13) exists precisely to supply
it: field-level accuracy against known ground truth, per class, from which these thresholds
get calibrated for real. Until that data exists these are reasoned defaults, and the
direction of their error is deliberate — biased toward sending work to humans, because the
cost of an unnecessary review is an hour and the cost of an automated wrong payment is money
plus the trust of the finance team.

The identity band at 0.98 is the value most likely to prove too aggressive in practice, and
is the first thing to look at if the review queue is drowning.

## Alternatives considered

| Option | Why not |
| --- | --- |
| One global threshold (the current `0.80`) | Spends equal scrutiny on the field nothing checks and the field validation recomputes; 0.80 is simultaneously far too low for a PO reference and needlessly strict for a line total |
| Reject-only, no review band | Discards readable invoices over minor doubt, and the spec already provides a human review path for exactly this case |
| Joint/aggregate payload confidence | Compounds uncalibrated, correlated numbers into false precision |
| Defer the whole question with the model choice | The gating *structure* is independent of which model produces the numbers, and Phase 3 validation needs to know what a doubtful field means before it can route one |

## Consequences

Extraction now has a real, testable contract, and it can be built and tested in full against
the fixture extractor with no model and no API key — which is what ADR-0006 was for.

Field classification must be maintained: a new field added to `ExtractedInvoice` without a
class is a gap in the gate. A test asserts that every field on the model is classified, so
the omission fails the build rather than defaulting to lenient.

`Settings.extraction_min_confidence` is replaced by `extraction_confidence_policy`, which
names a version rather than carrying a bare number.

**Q-1 is now half-resolved.** Thresholds: decided here. Vision model: still deferred by
explicit choice, with the fixture extractor standing in behind the same interface.
