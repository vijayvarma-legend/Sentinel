# ADR-0005: Risk scoring runs before exception reasoning

- **Status:** accepted
- **Date:** 2026-08-19
- **Phase:** 0
- **Affects:** SVC-50, SVC-60, SVC-03

## Context

The specification is internally inconsistent about two adjacent stages.

Its numbered stage table (§2) lists *5. Exception Reasoning* before *6. Risk/Fraud Scoring*. But the
pipeline arrow immediately above that table reads `… Duplicate Detection → Risk/Fraud Scoring →
Exception Reasoning → Policy Engine …`, and the reference architecture diagram (§14) draws
`Duplicate + Risk/Fraud Services` feeding directly into the `Exception Reasoning Agent`. Two of the
three say risk comes first.

The substance settles it independently of the vote count. §8 says the Exception Reasoning Agent
"receives deterministic discrepancy evidence" and must not invent financial facts — and §7's risk
signals (vendor anomaly, price anomaly, payment-change, new-vendor, behavioral) are exactly that kind
of evidence. An agent explaining why an invoice is anomalous should be able to cite the bank-account
change; it cannot if the risk service has not run yet. Ordering it the other way would either starve
the agent of evidence or tempt it into inferring risk itself, which would put an `llm` module in the
business of producing financial facts — barred by [ADR-0004](0004-trust-classes.md).

## Decision

The canonical pipeline order is:

```
Ingestion → Extraction → Validation → Duplicate Detection → Risk Scoring
  → Exception Reasoning → Policy Engine → (Auto | HITL) → ERP Execution → Audit → AgentOps
```

`RiskAssessment` is an **input** to SVC-60, alongside `ValidationScorecard` and `DuplicateAssessment`.
The stage numbering in §2's table is treated as a typo in the source document; §2 of our normalized
[`SPEC.md`](../architecture/SPEC.md) carries a note recording it.

Duplicate detection stays ahead of risk scoring, since duplicate risk is one of §7's named input signals.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Follow the table (reasoning, then risk) | Leaves the agent without risk evidence it is expected to explain, and invites it to infer risk itself |
| Run risk and reasoning in parallel | Removes the dependency the agent actually needs; the marginal latency saving is trivial next to the vision-extraction call that dominates the path |
| Run risk twice, before and after | Two scores for one invoice, ambiguous provenance in the audit trail, no benefit |

## Consequences

The agent's prompt receives a complete evidence bundle — validation, duplicate, and risk — which makes
grounding checkable: every claim it makes should trace to an item in that bundle.

The pipeline is strictly sequential through this section, so risk-scoring latency lands on the critical
path. Risk scoring is deterministic and database-bound, so this is small; if it ever stops being small,
the fix is caching vendor-history features, not reordering the stages.

Anyone reading the source PDF will hit the same contradiction. This ADR is the answer, and the note in
`SPEC.md` §2 points here.
