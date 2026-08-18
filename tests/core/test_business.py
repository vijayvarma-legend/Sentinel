"""Business document semantics -- especially the quantity rules from ADR-0007."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from sentinel.core.business import (
    GoodsReceiptLine,
    GoodsReceiptNote,
    PurchaseOrder,
    PurchaseOrderLine,
    VendorBankAccount,
    VendorContract,
    VendorProfile,
)
from sentinel.core.ids import VendorId
from sentinel.core.money import Money

VENDOR = VendorId.new()
JAN = dt.date(2026, 1, 15)


def po_line(item: str = "LAPTOP-01", price: str = "1000", qty: str = "10") -> PurchaseOrderLine:
    return PurchaseOrderLine(
        item_id=item, agreed_unit_price=Money(price, "USD"), approved_qty=Decimal(qty)
    )


def purchase_order(**overrides: object) -> PurchaseOrder:
    base: dict[str, object] = {
        "po_number": "9901",
        "vendor_id": VENDOR,
        "currency": "USD",
        "lines": (po_line(),),
        "issued_date": JAN,
    }
    return PurchaseOrder(**{**base, **overrides})  # type: ignore[arg-type]


class TestPurchaseOrder:
    def test_line_total_is_price_times_quantity(self) -> None:
        assert po_line().committed_total == Money("10000.00", "USD")

    def test_committed_total_sums_lines(self) -> None:
        order = purchase_order(lines=(po_line(), po_line(item="MOUSE-01", price="25", qty="10")))
        assert order.committed_total == Money("10250.00", "USD")

    def test_line_lookup_returns_none_for_an_unknown_item(self) -> None:
        """An invoice line with no PO counterpart is an exception, not a crash."""
        assert purchase_order().line_for("NOT-ORDERED") is None

    def test_a_line_in_the_wrong_currency_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="priced in EUR"):
            purchase_order(
                lines=(
                    PurchaseOrderLine(
                        item_id="X", agreed_unit_price=Money("1", "EUR"), approved_qty=Decimal(1)
                    ),
                )
            )

    def test_requires_at_least_one_line(self) -> None:
        with pytest.raises(ValidationError):
            purchase_order(lines=())

    def test_is_immutable(self) -> None:
        with pytest.raises(ValidationError):
            purchase_order().po_number = "9902"  # type: ignore[misc]

    def test_unknown_fields_are_refused(self) -> None:
        """A silently-ignored typo is how a confidently wrong total gets built."""
        with pytest.raises(ValidationError):
            purchase_order(totl=Money("1", "USD"))

    def test_approved_shipping_none_differs_from_zero(self) -> None:
        """'No shipping agreed' and 'shipping agreed at zero' route differently."""
        assert purchase_order().approved_shipping is None
        assert purchase_order(approved_shipping=Money.zero("USD")).approved_shipping is not None


class TestQuantitySemantics:
    """ADR-0007. The rule that decides whether a broken laptop gets paid for."""

    def test_accepted_is_received_minus_damaged(self) -> None:
        line = GoodsReceiptLine(
            item_id="LAPTOP-01", received_qty=Decimal(10), damaged_qty=Decimal(1)
        )
        assert line.accepted_qty == Decimal(9)

    def test_the_golden_path_receipt(self) -> None:
        """Spec section 15: ten arrive, one is damaged, nine are accepted."""
        grn = GoodsReceiptNote(
            grn_number="GRN-5501",
            po_number="9901",
            lines=(
                GoodsReceiptLine(
                    item_id="LAPTOP-01", received_qty=Decimal(10), damaged_qty=Decimal(1)
                ),
            ),
            received_date=JAN,
        )
        assert grn.accepted_qty_for("LAPTOP-01") == Decimal(9)

    def test_damaged_cannot_exceed_received(self) -> None:
        """Damaged units are a subset of received units, never additional to them."""
        with pytest.raises(ValidationError, match="subset of received"):
            GoodsReceiptLine(item_id="X", received_qty=Decimal(5), damaged_qty=Decimal(6))

    def test_undamaged_receipt_accepts_everything(self) -> None:
        line = GoodsReceiptLine(item_id="X", received_qty=Decimal(10))
        assert line.accepted_qty == Decimal(10)

    def test_a_fully_damaged_delivery_accepts_nothing(self) -> None:
        line = GoodsReceiptLine(item_id="X", received_qty=Decimal(5), damaged_qty=Decimal(5))
        assert line.accepted_qty == Decimal(0)

    def test_an_unreceived_item_accepts_zero_rather_than_raising(self) -> None:
        """Zero is correct, and it is also the answer that fails the match. Both right."""
        grn = GoodsReceiptNote(
            grn_number="GRN-1",
            po_number="9901",
            lines=(GoodsReceiptLine(item_id="A", received_qty=Decimal(1)),),
            received_date=JAN,
        )
        assert grn.accepted_qty_for("NEVER-ARRIVED") == Decimal(0)

    def test_fractional_quantities_are_supported(self) -> None:
        """Invoices bill fractional units -- 2.5 hours, 0.75 tonnes."""
        line = GoodsReceiptLine(
            item_id="STEEL", received_qty=Decimal("2.75"), damaged_qty=Decimal("0.25")
        )
        assert line.accepted_qty == Decimal("2.50")


class TestVendorContract:
    def test_is_effective_within_its_window(self) -> None:
        contract = VendorContract(
            vendor_id=VENDOR,
            effective_from=dt.date(2026, 1, 1),
            effective_to=dt.date(2026, 12, 31),
        )
        assert contract.is_effective_on(dt.date(2026, 6, 1))
        assert not contract.is_effective_on(dt.date(2025, 12, 31))
        assert not contract.is_effective_on(dt.date(2027, 1, 1))

    def test_boundaries_are_inclusive(self) -> None:
        contract = VendorContract(
            vendor_id=VENDOR,
            effective_from=dt.date(2026, 1, 1),
            effective_to=dt.date(2026, 12, 31),
        )
        assert contract.is_effective_on(dt.date(2026, 1, 1))
        assert contract.is_effective_on(dt.date(2026, 12, 31))

    def test_an_open_ended_contract_never_expires(self) -> None:
        contract = VendorContract(vendor_id=VENDOR, effective_from=dt.date(2026, 1, 1))
        assert contract.is_effective_on(dt.date(2099, 1, 1))

    def test_a_contract_ending_before_it_starts_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="before it"):
            VendorContract(
                vendor_id=VENDOR,
                effective_from=dt.date(2026, 6, 1),
                effective_to=dt.date(2026, 1, 1),
            )


class TestVendorProfile:
    def test_a_vendor_with_little_history_is_new(self) -> None:
        assert VendorProfile(vendor_id=VENDOR, name="TechCorp", invoice_count=2).is_new
        assert not VendorProfile(vendor_id=VENDOR, name="TechCorp", invoice_count=3).is_new

    def test_bank_account_resolves_as_of_a_date(self) -> None:
        profile = VendorProfile(
            vendor_id=VENDOR,
            name="TechCorp",
            bank_accounts=(
                VendorBankAccount(account_fingerprint="old", effective_from=dt.date(2025, 1, 1)),
                VendorBankAccount(account_fingerprint="new", effective_from=dt.date(2026, 6, 1)),
            ),
        )
        assert profile.bank_account_on(dt.date(2026, 3, 1)).account_fingerprint == "old"  # type: ignore[union-attr]
        assert profile.bank_account_on(dt.date(2026, 7, 1)).account_fingerprint == "new"  # type: ignore[union-attr]

    def test_no_account_is_in_effect_before_the_earliest_one(self) -> None:
        profile = VendorProfile(
            vendor_id=VENDOR,
            name="TechCorp",
            bank_accounts=(
                VendorBankAccount(account_fingerprint="a", effective_from=dt.date(2026, 1, 1)),
            ),
        )
        assert profile.bank_account_on(dt.date(2025, 1, 1)) is None

    def test_days_since_bank_change_drives_the_payment_risk_signal(self) -> None:
        """Spec section 7: a bank change shortly before a large invoice."""
        profile = VendorProfile(
            vendor_id=VENDOR,
            name="TechCorp",
            bank_accounts=(
                VendorBankAccount(account_fingerprint="old", effective_from=dt.date(2025, 1, 1)),
                VendorBankAccount(account_fingerprint="new", effective_from=dt.date(2026, 6, 1)),
            ),
        )
        assert profile.days_since_bank_change(dt.date(2026, 6, 8)) == 7

    def test_a_vendor_that_never_changed_banks_has_no_signal(self) -> None:
        """None, not zero -- 'never changed' is not 'changed today'."""
        profile = VendorProfile(
            vendor_id=VENDOR,
            name="TechCorp",
            bank_accounts=(
                VendorBankAccount(account_fingerprint="only", effective_from=dt.date(2025, 1, 1)),
            ),
        )
        assert profile.days_since_bank_change(dt.date(2026, 6, 1)) is None
