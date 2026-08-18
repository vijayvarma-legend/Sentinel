"""The ground-truth business documents. Spec §3.

These are the evidence the whole system reasons over: what was agreed (PO), what actually
arrived (GRN), what the supplier is asking for (invoice), and what the commercial
relationship permits (contract, vendor profile).

Every model here is **frozen**. Evidence does not change shape as it moves down the
pipeline; a stage that needs a different view builds a new object rather than mutating the
record a later audit will have to explain.

Quantity semantics are defined once, here, and used everywhere -- see
:attr:`GoodsReceiptLine.accepted_qty` and ADR-0007.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from sentinel.core.ids import VendorId
from sentinel.core.money import Money

__all__ = [
    "GoodsReceiptLine",
    "GoodsReceiptNote",
    "PurchaseOrder",
    "PurchaseOrderLine",
    "VendorBankAccount",
    "VendorContract",
    "VendorProfile",
]


class _Frozen(BaseModel):
    """Immutable, strictly validated, and intolerant of unknown fields.

    ``extra="forbid"`` is deliberate: a typo'd field name that silently vanishes into an
    ignored extra is exactly the failure that produces a confidently wrong invoice total.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=False)


# ---------------------------------------------------------------------------------------
# Purchase Order -- the approved commercial commitment
# ---------------------------------------------------------------------------------------


class PurchaseOrderLine(_Frozen):
    """One agreed item, quantity, and price."""

    item_id: str = Field(min_length=1)
    description: str = ""
    agreed_unit_price: Money
    approved_qty: Decimal = Field(gt=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def committed_total(self) -> Money:
        """What this line commits the buyer to, at the agreed price and quantity."""
        return self.agreed_unit_price * self.approved_qty


class PurchaseOrder(_Frozen):
    """An approved commercial commitment. Spec §3."""

    po_number: str = Field(min_length=1)
    vendor_id: VendorId
    currency: str = Field(min_length=3, max_length=3)
    lines: tuple[PurchaseOrderLine, ...] = Field(min_length=1)
    issued_date: dt.date
    approved_shipping: Money | None = None
    """Shipping the PO explicitly authorizes. ``None`` means none was agreed.

    Distinct from ``Money.zero``: "no shipping was agreed" and "shipping was agreed at zero"
    route differently once a supplier bills for it (spec §5).
    """

    @model_validator(mode="after")
    def _currencies_agree(self) -> PurchaseOrder:
        for line in self.lines:
            if line.agreed_unit_price.currency != self.currency:
                raise ValueError(
                    f"PO {self.po_number} is denominated in {self.currency} but line "
                    f"{line.item_id} is priced in {line.agreed_unit_price.currency}"
                )
        if self.approved_shipping and self.approved_shipping.currency != self.currency:
            raise ValueError(
                f"PO {self.po_number} shipping is in {self.approved_shipping.currency}, "
                f"not {self.currency}"
            )
        return self

    def line_for(self, item_id: str) -> PurchaseOrderLine | None:
        """The agreed line for `item_id`, or ``None`` if this PO does not cover it.

        An invoice line with no PO counterpart is an exception, not a lookup failure -- the
        caller decides how to classify it.
        """
        return next((line for line in self.lines if line.item_id == item_id), None)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def committed_total(self) -> Money:
        return sum((line.committed_total for line in self.lines), Money.zero(self.currency))


# ---------------------------------------------------------------------------------------
# Goods Receipt Note -- physical receipt evidence
# ---------------------------------------------------------------------------------------


class GoodsReceiptLine(_Frozen):
    """What physically arrived for one item, and how much of it was usable."""

    item_id: str = Field(min_length=1)
    received_qty: Decimal = Field(ge=0)
    """Units that physically arrived, damaged ones included."""

    damaged_qty: Decimal = Field(default=Decimal(0), ge=0)
    """Units that arrived unusable. A subset of ``received_qty``, never additional to it."""

    @model_validator(mode="after")
    def _damaged_cannot_exceed_received(self) -> GoodsReceiptLine:
        if self.damaged_qty > self.received_qty:
            raise ValueError(
                f"item {self.item_id}: {self.damaged_qty} damaged of {self.received_qty} "
                "received -- damaged units are a subset of received units, not additional"
            )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def accepted_qty(self) -> Decimal:
        """Units actually accepted: received minus damaged.

        **This is the quantity the three-way match uses**, and the quantity the price
        variance is computed against (spec §15, ADR-0007). Matching against ``received_qty``
        would pay for goods that arrived broken.
        """
        return self.received_qty - self.damaged_qty


class GoodsReceiptNote(_Frozen):
    """Physical receipt evidence. Spec §3."""

    grn_number: str = Field(min_length=1)
    po_number: str = Field(min_length=1)
    lines: tuple[GoodsReceiptLine, ...] = Field(min_length=1)
    received_date: dt.date

    def line_for(self, item_id: str) -> GoodsReceiptLine | None:
        return next((line for line in self.lines if line.item_id == item_id), None)

    def accepted_qty_for(self, item_id: str) -> Decimal:
        """Accepted quantity for `item_id`; zero when nothing was received.

        Zero is the correct answer for an unreceived item, and it is also the answer that
        makes an invoice for it fail the match -- which is what should happen.
        """
        line = self.line_for(item_id)
        return line.accepted_qty if line else Decimal(0)


# ---------------------------------------------------------------------------------------
# Vendor context -- contract, banking, history
# ---------------------------------------------------------------------------------------


class VendorBankAccount(_Frozen):
    """Where a vendor is paid, and since when.

    ``effective_from`` exists for one reason: spec §7 names a bank account changing shortly
    before a large invoice as a payment-change risk signal. Without the date, the signal
    cannot be computed at all.
    """

    account_fingerprint: str = Field(min_length=1)
    """A hash of the account details. Sentinel reasons about *changes*, so it never needs
    to store or move the account number itself."""

    effective_from: dt.date


class VendorContract(_Frozen):
    """Commercial terms and allowed charges. Spec §3."""

    vendor_id: VendorId
    pricing_terms: str = ""
    shipping_allowed: bool = False
    """Whether shipping may be charged at all, absent a PO line authorizing it."""

    max_shipping: Money | None = None
    """Ceiling on an allowed shipping charge. ``None`` means no ceiling was agreed."""

    price_tolerance_pct: Decimal | None = Field(default=None, ge=0)
    """Vendor-specific price tolerance, overriding the global policy when present."""

    effective_from: dt.date
    effective_to: dt.date | None = None

    @model_validator(mode="after")
    def _dates_are_ordered(self) -> VendorContract:
        if self.effective_to and self.effective_to < self.effective_from:
            raise ValueError(
                f"contract for {self.vendor_id} ends {self.effective_to}, before it "
                f"begins {self.effective_from}"
            )
        return self

    def is_effective_on(self, when: dt.date) -> bool:
        """Whether this contract governs an invoice dated `when`.

        Invoices routinely arrive against expired contracts. Answering that question with a
        date rather than with "the current contract" is what lets a historical decision be
        reconstructed exactly (spec §18).
        """
        if when < self.effective_from:
            return False
        return self.effective_to is None or when <= self.effective_to


class VendorProfile(_Frozen):
    """Historical behaviour and risk context. Spec §3.

    Feeds the risk service (spec §7). Everything here is an *input to a score*, never a
    verdict -- a new vendor is not a fraudulent one.
    """

    vendor_id: VendorId
    name: str = Field(min_length=1)
    first_seen: dt.date | None = None
    """``None`` means Sentinel has never processed an invoice from this vendor."""

    invoice_count: int = Field(default=0, ge=0)
    mean_invoice_amount: Money | None = None
    max_invoice_amount: Money | None = None
    bank_accounts: tuple[VendorBankAccount, ...] = ()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_new(self) -> bool:
        """Whether this vendor has too little history to reason about.

        The threshold is deliberately low and deliberately here rather than in the risk
        module: it is a statement about the *data*, not about the policy applied to it.
        """
        return self.invoice_count < 3

    def bank_account_on(self, when: dt.date) -> VendorBankAccount | None:
        """The account in effect on `when` -- the most recent one not dated after it."""
        eligible = [a for a in self.bank_accounts if a.effective_from <= when]
        return max(eligible, key=lambda a: a.effective_from, default=None)

    def days_since_bank_change(self, when: dt.date) -> int | None:
        """Days between the latest banking change and `when`; ``None`` if never changed.

        A small number alongside a large invoice is the payment-redirection pattern in
        spec §7.
        """
        current = self.bank_account_on(when)
        if current is None or len(self.bank_accounts) < 2:
            return None
        return (when - current.effective_from).days
