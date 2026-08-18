"""Confidence gating. ADR-0009.

The central claim under test is counter-intuitive and worth stating plainly: fields that
SVC-30 cross-checks get the *lowest* floors, and fields nothing checks get the highest. These
tests exist mostly to stop someone "fixing" that inversion.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sentinel.core.evidence import ExtractedField, ExtractedInvoice, ExtractedLine
from sentinel.core.money import Money
from sentinel.extraction.confidence import (
    FIELD_CLASSES,
    ConfidenceBand,
    FieldClass,
    FieldVerdict,
    PayloadVerdict,
    assess,
    policy_named,
)
from tests.golden import golden_extracted_invoice

POLICY = policy_named("v1")


def graded(**field_overrides: ExtractedField) -> ExtractedInvoice:
    """The golden invoice with specific fields' confidences replaced."""
    return golden_extracted_invoice(**field_overrides)


def field(value: object, confidence: str) -> ExtractedField:
    return ExtractedField(value=value, confidence=Decimal(confidence))


class TestClassificationIsComplete:
    def test_every_gated_field_on_the_model_is_classified(self) -> None:
        """A newly added field must not default to lenient -- that is a silent hole.

        This test is the reason the classification table can be trusted: adding a field to
        ExtractedInvoice without classifying it fails the build.
        """
        invoice_fields = set(ExtractedInvoice.model_fields)
        line_fields = set(ExtractedLine.model_fields)

        # Fields that carry no confidence of their own and so are not gradeable.
        metadata = {
            "correlation_id",
            "document_hash",
            "lines",
            "extracted_at",
            "model_id",
            "prompt_version",
        }

        unclassified = (invoice_fields | line_fields) - metadata - set(FIELD_CLASSES)
        assert not unclassified, (
            f"unclassified extracted fields: {sorted(unclassified)}. "
            "Add them to FIELD_CLASSES -- an unclassified field is silently ungated."
        )

    def test_no_classification_refers_to_a_field_that_no_longer_exists(self) -> None:
        known = set(ExtractedInvoice.model_fields) | set(ExtractedLine.model_fields)
        stale = set(FIELD_CLASSES) - known
        assert not stale, f"FIELD_CLASSES names fields that do not exist: {sorted(stale)}"


class TestTheThresholdOrdering:
    """The design claim of ADR-0009, asserted directly."""

    def test_cross_checked_money_is_the_most_lenient_class(self) -> None:
        """Because SVC-30 re-derives it. A wrong value fails validation loudly."""
        cross_checked = POLICY.bands[FieldClass.MONEY_CROSS_CHECKED]
        unchecked = POLICY.bands[FieldClass.MONEY_UNCHECKED]

        assert cross_checked.review_below < unchecked.review_below
        assert cross_checked.reject_below < unchecked.reject_below

    def test_identity_is_the_strictest_class(self) -> None:
        """A misread PO reference validates cleanly against the wrong purchase order."""
        identity = POLICY.bands[FieldClass.IDENTITY]
        for other in (FieldClass.MONEY_UNCHECKED, FieldClass.MONEY_CROSS_CHECKED):
            assert identity.review_below >= POLICY.bands[other].review_below

    def test_cosmetic_fields_are_ungated(self) -> None:
        assert POLICY.band_for("description") is None

    def test_an_unknown_field_is_ungated_rather_than_crashing(self) -> None:
        assert POLICY.band_for("some_future_field") is None


class TestBandBoundaries:
    def band(self) -> ConfidenceBand:
        return ConfidenceBand(reject_below=Decimal("0.85"), review_below=Decimal("0.98"))

    @pytest.mark.parametrize(
        ("confidence", "expected"),
        [
            ("0.00", FieldVerdict.REJECT),
            ("0.84", FieldVerdict.REJECT),
            ("0.85", FieldVerdict.REVIEW),  # the floor itself is not a rejection
            ("0.97", FieldVerdict.REVIEW),
            ("0.98", FieldVerdict.ACCEPT),  # at the floor is accepted
            ("1.00", FieldVerdict.ACCEPT),
        ],
    )
    def test_boundaries_are_inclusive_upward(self, confidence: str, expected: FieldVerdict) -> None:
        assert self.band().verdict_for(Decimal(confidence)) is expected


class TestPayloadGrading:
    def test_the_golden_invoice_is_accepted(self) -> None:
        report = assess(golden_extracted_invoice(), POLICY)

        assert report.verdict is PayloadVerdict.ACCEPT
        assert report.may_auto_process
        assert report.summary() == "all fields above their confidence floors"

    def test_a_doubtful_po_reference_blocks_automation(self) -> None:
        """0.96 would clear every other class. Identity is where it matters most.

        A PO reference read wrong sends validation to a different purchase order, where every
        check passes and the wrong invoice is paid with a clean audit trail.
        """
        report = assess(graded(po_reference=field("9901", "0.96")), POLICY)

        assert report.verdict is PayloadVerdict.REVIEW
        assert not report.may_auto_process
        assert report.review_fields == (("po_reference", Decimal("0.96")),)

    def test_the_same_confidence_on_a_cross_checked_field_is_accepted(self) -> None:
        """The inversion, demonstrated: 0.96 passes here and fails on identity."""
        report = assess(graded(subtotal=field(Money("10500", "USD"), "0.96")), POLICY)
        assert report.verdict is PayloadVerdict.ACCEPT

    def test_an_unreadable_field_rejects_the_payload(self) -> None:
        report = assess(graded(invoice_number=field("INV-8821", "0.40")), POLICY)

        assert report.verdict is PayloadVerdict.REJECT
        assert not report.may_auto_process
        assert report.rejected_fields == (("invoice_number", Decimal("0.40")),)

    def test_the_worst_field_decides_the_payload(self) -> None:
        """One badly read field is enough, however confident the rest of the page was."""
        report = assess(
            graded(
                po_reference=field("9901", "0.96"),  # review
                invoice_number=field("INV-8821", "0.20"),  # reject
            ),
            POLICY,
        )
        assert report.verdict is PayloadVerdict.REJECT

    def test_a_doubtful_line_field_is_reported_at_its_position(self) -> None:
        """A reviewer needs to know which line, not just that a line is doubtful."""
        invoice = golden_extracted_invoice(
            lines=(
                ExtractedLine(
                    item_id=field("LAPTOP-01", "0.99"),
                    billed_qty=field(Decimal(10), "0.99"),
                    billed_unit_price=field(Money("1050", "USD"), "0.55"),
                ),
            )
        )
        report = assess(invoice, POLICY)

        assert report.verdict is PayloadVerdict.REJECT
        assert report.rejected_fields == (("lines[0].billed_unit_price", Decimal("0.55")),)

    def test_shipping_is_treated_as_unchecked_money(self) -> None:
        """The golden path's $200 fee has no PO counterpart -- nothing can verify the figure."""
        report = assess(graded(shipping=field(Money("200", "USD"), "0.93")), POLICY)

        assert report.verdict is PayloadVerdict.REVIEW
        assert report.review_fields == (("shipping", Decimal("0.93")),)


class TestAbsenceIsNotDoubt:
    def test_a_missing_optional_field_does_not_lower_the_verdict(self) -> None:
        """A non-PO invoice legitimately has no po_reference.

        That belongs on its own exception path, not punished as a bad read.
        """
        report = assess(golden_extracted_invoice(po_reference=None, tax=None), POLICY)

        assert report.verdict is PayloadVerdict.ACCEPT
        assert report.review_fields == ()
        assert report.rejected_fields == ()


class TestReporting:
    def test_the_summary_names_fields_and_their_confidences(self) -> None:
        report = assess(
            graded(
                po_reference=field("9901", "0.96"),
                invoice_number=field("INV-8821", "0.30"),
            ),
            POLICY,
        )
        summary = report.summary()

        assert "invoice_number (0.30)" in summary
        assert "po_reference (0.96)" in summary
        assert "reject floor" in summary

    def test_the_policy_version_is_carried_for_the_audit_trail(self) -> None:
        """A past grading must be reconstructable after the thresholds move."""
        assert assess(golden_extracted_invoice(), POLICY).policy_version == "confidence-v1"


class TestPolicyLookup:
    def test_a_known_version_resolves(self) -> None:
        assert policy_named("v1").version == "confidence-v1"

    def test_an_unknown_version_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="unknown confidence policy"):
            policy_named("v99")
