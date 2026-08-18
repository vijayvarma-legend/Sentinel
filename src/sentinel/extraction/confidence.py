"""Confidence gating for extracted payloads. Spec §4.2, ADR-0009.

The governing question for any extracted field is *what catches it if the model reads it
wrong* -- and the answer decides how much scrutiny it gets:

* A **unit price** misread is caught by SVC-30 comparing against the PO. Low threshold.
* A **PO reference** misread is caught by nothing. It validates cleanly against the *wrong*
  purchase order, and the wrong invoice is paid with a perfect audit trail. High threshold.

So the thresholds run counter to intuition: fields under deterministic cross-check get the
*lowest* floors, because something else is already watching them. Scrutiny is spent where
nothing else is.

Three outcomes rather than two. A field at 0.93 is not malformed, it is doubtful -- rejecting
it discards a readable invoice, and accepting it silently lets a doubtful number reach a
policy engine that would auto-process it. It goes to a human instead.

**These numbers are triage, not probability.** A model reporting 0.95 is not making a
calibrated claim. The real safety net is SVC-30 recomputing every figure it can against the
PO, the GRN, and the supplier's own printed totals -- arithmetic that does not care how
confident the model claimed to be.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover
    from sentinel.core.evidence import ExtractedInvoice

__all__ = [
    "POLICIES",
    "ConfidenceBand",
    "ConfidencePolicy",
    "ConfidenceReport",
    "FieldClass",
    "FieldVerdict",
    "PayloadVerdict",
    "policy_named",
]


class FieldClass(StrEnum):
    """How much protection a field has from somewhere other than its own confidence."""

    IDENTITY = "identity"
    """Decides *which* records this invoice is checked against.

    A misread here is silent and total: validation passes cleanly against the wrong baseline.
    Nothing downstream can detect it, so this class carries the highest floors.
    """

    MONEY_UNCHECKED = "money_unchecked"
    """An amount with no independent counterpart to check it against.

    Shipping is the canonical case -- an unapproved charge has no PO line by definition, which
    is exactly why it is an exception, and exactly why nothing can verify the figure.
    """

    MONEY_CROSS_CHECKED = "money_cross_checked"
    """An amount SVC-30 re-derives from the PO, the GRN, or the document's own arithmetic.

    Lowest floors, deliberately. A wrong value here fails validation loudly.
    """

    COSMETIC = "cosmetic"
    """Carries no financial or routing meaning. Ungated."""


class FieldVerdict(StrEnum):
    ACCEPT = "accept"
    REVIEW = "review"
    REJECT = "reject"


class PayloadVerdict(StrEnum):
    """The payload's outcome: the worst verdict any of its fields received."""

    ACCEPT = "accept"
    """Every field is trustworthy. Eligible for straight-through processing."""

    REVIEW = "review"
    """Processable, but **never** automatically. Policy must route it to a human."""

    REJECT = "reject"
    """Too poorly read to be evidence.

    Not a dead-letter -- the *document* was fine, our *reading* of it was not, so the invoice
    is retained for human attention rather than refused at the door.
    """


@dataclass(frozen=True, slots=True)
class ConfidenceBand:
    """The two floors for one field class."""

    reject_below: Decimal
    review_below: Decimal

    def verdict_for(self, confidence: Decimal) -> FieldVerdict:
        if confidence < self.reject_below:
            return FieldVerdict.REJECT
        if confidence < self.review_below:
            return FieldVerdict.REVIEW
        return FieldVerdict.ACCEPT


#: Which class each field belongs to. Every field on ``ExtractedInvoice`` must appear here --
#: a test fails the build if one is missing, so a newly added field cannot default to lenient.
FIELD_CLASSES: Final[dict[str, FieldClass]] = {
    # Identity -- decides which records we check against. Nothing else catches a misread.
    "invoice_number": FieldClass.IDENTITY,
    "po_reference": FieldClass.IDENTITY,
    "supplier_name": FieldClass.IDENTITY,
    "currency": FieldClass.IDENTITY,
    # Amounts with no independent counterpart.
    "total_due": FieldClass.MONEY_UNCHECKED,
    "tax": FieldClass.MONEY_UNCHECKED,
    "shipping": FieldClass.MONEY_UNCHECKED,
    "invoice_date": FieldClass.MONEY_UNCHECKED,
    # Amounts SVC-30 re-derives from the PO, the GRN, or the document's own arithmetic.
    "subtotal": FieldClass.MONEY_CROSS_CHECKED,
    "item_id": FieldClass.MONEY_CROSS_CHECKED,
    "billed_qty": FieldClass.MONEY_CROSS_CHECKED,
    "billed_unit_price": FieldClass.MONEY_CROSS_CHECKED,
    "line_total": FieldClass.MONEY_CROSS_CHECKED,
    # No financial or routing meaning.
    "description": FieldClass.COSMETIC,
}


@dataclass(frozen=True, slots=True)
class ConfidencePolicy:
    """A named, versioned set of bands.

    Versioned because these numbers decide whether an invoice may be paid without a human,
    which makes them decision-affecting whatever we call them. The version is recorded on the
    extraction audit event, so a past grading can be reconstructed after the numbers move.
    """

    version: str
    bands: dict[FieldClass, ConfidenceBand]

    def band_for(self, field_name: str) -> ConfidenceBand | None:
        """The band governing `field_name`, or ``None`` if the field is ungated."""
        field_class = FIELD_CLASSES.get(field_name)
        if field_class is None or field_class is FieldClass.COSMETIC:
            return None
        return self.bands[field_class]


#: ADR-0009. Starting points, expected to move once Phase 8's benchmark supplies real
#: field-level accuracy. Their error is biased deliberately toward sending work to humans:
#: an unnecessary review costs an hour, an automated wrong payment costs money and trust.
POLICIES: Final[dict[str, ConfidencePolicy]] = {
    "v1": ConfidencePolicy(
        version="confidence-v1",
        bands={
            FieldClass.IDENTITY: ConfidenceBand(
                reject_below=Decimal("0.85"), review_below=Decimal("0.98")
            ),
            FieldClass.MONEY_UNCHECKED: ConfidenceBand(
                reject_below=Decimal("0.85"), review_below=Decimal("0.95")
            ),
            FieldClass.MONEY_CROSS_CHECKED: ConfidenceBand(
                reject_below=Decimal("0.70"), review_below=Decimal("0.90")
            ),
        },
    )
}


def policy_named(name: str) -> ConfidencePolicy:
    try:
        return POLICIES[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown confidence policy {name!r}; available: {sorted(POLICIES)}"
        ) from exc


@dataclass(frozen=True, slots=True)
class ConfidenceReport:
    """How a payload graded, and which fields are responsible."""

    verdict: PayloadVerdict
    policy_version: str
    rejected_fields: tuple[tuple[str, Decimal], ...]
    review_fields: tuple[tuple[str, Decimal], ...]

    @property
    def may_auto_process(self) -> bool:
        """Whether this payload is eligible for straight-through processing."""
        return self.verdict is PayloadVerdict.ACCEPT

    def summary(self) -> str:
        """A one-line explanation naming the fields and their confidences.

        Named fields, not a bare count: a reviewer needs to know *which* number is doubted,
        and going straight to it is the difference between a minute and a re-read.
        """
        if self.verdict is PayloadVerdict.ACCEPT:
            return "all fields above their confidence floors"

        def render(fields: tuple[tuple[str, Decimal], ...]) -> str:
            return ", ".join(f"{name} ({value})" for name, value in fields)

        parts = []
        if self.rejected_fields:
            parts.append(f"below reject floor: {render(self.rejected_fields)}")
        if self.review_fields:
            parts.append(f"below review floor: {render(self.review_fields)}")
        return "; ".join(parts)


def assess(invoice: ExtractedInvoice, policy: ConfidencePolicy) -> ConfidenceReport:
    """Grade every gated field on `invoice`.

    The payload's verdict is the worst any single field received -- one badly read PO
    reference is enough, however confident the rest of the page was.

    A field that is ``None`` is **absent, not doubtful**. A non-PO invoice legitimately has no
    ``po_reference``, and that belongs on its own exception path rather than being punished as
    a bad read.
    """
    rejected: list[tuple[str, Decimal]] = []
    review: list[tuple[str, Decimal]] = []

    def grade(field: object, *, key: str, label: str) -> None:
        """Grade one field.

        `key` classifies (``billed_unit_price``); `label` is what a reviewer reads
        (``lines[0].billed_unit_price``). Keeping them separate is what lets a line field be
        classified once and still be reported at the position a human has to go and look at.
        """
        if field is None:
            return  # absent, not doubtful
        band = policy.band_for(key)
        if band is None:
            return  # cosmetic or ungated
        confidence = field.confidence  # type: ignore[attr-defined]
        match band.verdict_for(confidence):
            case FieldVerdict.REJECT:
                rejected.append((label, confidence))
            case FieldVerdict.REVIEW:
                review.append((label, confidence))
            case FieldVerdict.ACCEPT:
                pass

    for name in (
        "supplier_name",
        "invoice_number",
        "po_reference",
        "invoice_date",
        "currency",
        "subtotal",
        "tax",
        "shipping",
        "total_due",
    ):
        grade(getattr(invoice, name), key=name, label=name)

    for index, line in enumerate(invoice.lines):
        for name in ("item_id", "description", "billed_qty", "billed_unit_price", "line_total"):
            grade(getattr(line, name), key=name, label=f"lines[{index}].{name}")

    if rejected:
        verdict = PayloadVerdict.REJECT
    elif review:
        verdict = PayloadVerdict.REVIEW
    else:
        verdict = PayloadVerdict.ACCEPT

    return ConfidenceReport(
        verdict=verdict,
        policy_version=policy.version,
        rejected_fields=tuple(rejected),
        review_fields=tuple(review),
    )
