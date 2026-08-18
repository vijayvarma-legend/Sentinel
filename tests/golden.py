"""The canonical scenario from spec §15, as reusable builders.

    TechCorp issues PO #9901 for 10 laptops at $1,000/unit. The warehouse receives 9
    because one is damaged. Supplier invoice INV-8821 bills 10 units at $1,050/unit
    plus an unexpected $200 shipping fee.

Every phase from validation onward is tested against this one invoice, so the numbers live
in exactly one place. When a later phase changes what the pipeline does with them, one test
file breaks loudly rather than twelve drifting quietly.

The expected outcomes, fixed by the spec and by ADR-0007:

===========================  ====================================================
quantity                     10 billed against 9 accepted (10 received, 1 damaged)
unit price                   $1,050 billed against $1,000 agreed -- +5%
price variance basis         the **accepted** quantity, 9 units, not the 10 billed
shipping                     $200, with no PO or contract authorization
===========================  ====================================================
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sentinel.core.business import (
    GoodsReceiptLine,
    GoodsReceiptNote,
    PurchaseOrder,
    PurchaseOrderLine,
    VendorBankAccount,
    VendorContract,
    VendorProfile,
)
from sentinel.core.enums import DocumentSource
from sentinel.core.evidence import (
    ExtractedField,
    ExtractedInvoice,
    ExtractedLine,
    IngestedDocument,
)
from sentinel.core.ids import CorrelationId, DocumentHash, VendorId
from sentinel.core.money import Money

__all__ = [
    "ACCEPTED_QTY",
    "AGREED_UNIT_PRICE",
    "BILLED_QTY",
    "BILLED_UNIT_PRICE",
    "CURRENCY",
    "DAMAGED_QTY",
    "INVOICE_DATE",
    "INVOICE_NUMBER",
    "ITEM_ID",
    "PO_NUMBER",
    "RECEIVED_QTY",
    "SHIPPING_CHARGED",
    "VENDOR_ID",
    "VENDOR_NAME",
    "golden_contract",
    "golden_document",
    "golden_extracted_invoice",
    "golden_goods_receipt",
    "golden_purchase_order",
    "golden_vendor",
]

# -- the scenario's fixed numbers --------------------------------------------------------

VENDOR_ID = VendorId("ven_01930000-0000-7000-8000-000000000001")
VENDOR_NAME = "TechCorp"
PO_NUMBER = "9901"
GRN_NUMBER = "GRN-9901-01"
INVOICE_NUMBER = "INV-8821"
ITEM_ID = "LAPTOP-01"
CURRENCY = "USD"

AGREED_UNIT_PRICE = Money("1000", CURRENCY)
BILLED_UNIT_PRICE = Money("1050", CURRENCY)
APPROVED_QTY = Decimal(10)
BILLED_QTY = Decimal(10)
RECEIVED_QTY = Decimal(10)
DAMAGED_QTY = Decimal(1)
ACCEPTED_QTY = RECEIVED_QTY - DAMAGED_QTY  # 9 -- ADR-0007
SHIPPING_CHARGED = Money("200", CURRENCY)

PO_DATE = dt.date(2026, 1, 5)
RECEIPT_DATE = dt.date(2026, 1, 12)
INVOICE_DATE = dt.date(2026, 1, 15)
INGESTED_AT = dt.datetime(2026, 1, 16, 9, 30, tzinfo=dt.UTC)

#: What the supplier is asking for: 10 x $1,050 + $200 shipping.
BILLED_TOTAL = BILLED_UNIT_PRICE * BILLED_QTY + SHIPPING_CHARGED

CONFIDENT = Decimal("0.98")


def _field[T](value: T, confidence: Decimal = CONFIDENT) -> ExtractedField[T]:
    return ExtractedField(value=value, confidence=confidence, page=1)


# -- builders ----------------------------------------------------------------------------


def golden_purchase_order(**overrides: object) -> PurchaseOrder:
    """PO #9901: 10 laptops at $1,000, no shipping authorized."""
    base: dict[str, object] = {
        "po_number": PO_NUMBER,
        "vendor_id": VENDOR_ID,
        "currency": CURRENCY,
        "lines": (
            PurchaseOrderLine(
                item_id=ITEM_ID,
                description="15-inch laptop",
                agreed_unit_price=AGREED_UNIT_PRICE,
                approved_qty=APPROVED_QTY,
            ),
        ),
        "issued_date": PO_DATE,
        "approved_shipping": None,
    }
    return PurchaseOrder(**{**base, **overrides})  # type: ignore[arg-type]


def golden_goods_receipt(**overrides: object) -> GoodsReceiptNote:
    """Ten units arrived; one was damaged; nine are accepted (ADR-0007)."""
    base: dict[str, object] = {
        "grn_number": GRN_NUMBER,
        "po_number": PO_NUMBER,
        "lines": (
            GoodsReceiptLine(item_id=ITEM_ID, received_qty=RECEIVED_QTY, damaged_qty=DAMAGED_QTY),
        ),
        "received_date": RECEIPT_DATE,
    }
    return GoodsReceiptNote(**{**base, **overrides})  # type: ignore[arg-type]


def golden_contract(**overrides: object) -> VendorContract:
    """TechCorp's terms: shipping is not an allowed charge."""
    base: dict[str, object] = {
        "vendor_id": VENDOR_ID,
        "pricing_terms": "net 30",
        "shipping_allowed": False,
        "price_tolerance_pct": None,
        "effective_from": dt.date(2025, 1, 1),
        "effective_to": None,
    }
    return VendorContract(**{**base, **overrides})  # type: ignore[arg-type]


def golden_vendor(**overrides: object) -> VendorProfile:
    """An established vendor with stable banking -- so risk stays low by default.

    Tests that need a risk signal change one thing here (add a recent bank account, drop the
    invoice count) rather than rebuilding the profile, which keeps it obvious what the test
    is actually varying.
    """
    base: dict[str, object] = {
        "vendor_id": VENDOR_ID,
        "name": VENDOR_NAME,
        "first_seen": dt.date(2024, 3, 1),
        "invoice_count": 48,
        "mean_invoice_amount": Money("9800", CURRENCY),
        "max_invoice_amount": Money("14500", CURRENCY),
        "bank_accounts": (
            VendorBankAccount(
                account_fingerprint="sha256:techcorp-primary",
                effective_from=dt.date(2024, 3, 1),
            ),
        ),
    }
    return VendorProfile(**{**base, **overrides})  # type: ignore[arg-type]


def golden_document(
    correlation_id: CorrelationId | None = None, **overrides: object
) -> IngestedDocument:
    """The ingested INV-8821 PDF."""
    base: dict[str, object] = {
        "correlation_id": correlation_id or CorrelationId.new(),
        "document_hash": DocumentHash.of(b"golden-invoice-8821"),
        "storage_uri": "s3://sentinel-documents/sha256/golden-invoice-8821.pdf",
        "filename": "INV-8821.pdf",
        "content_type": "application/pdf",
        "size_bytes": 84_213,
        "page_count": 1,
        "source": DocumentSource.UPLOAD,
        "received_at": INGESTED_AT,
    }
    return IngestedDocument(**{**base, **overrides})  # type: ignore[arg-type]


def golden_extracted_invoice(
    document: IngestedDocument | None = None, **overrides: object
) -> ExtractedInvoice:
    """INV-8821 as read from the page: 10 units at $1,050, plus $200 shipping.

    Note what is *not* here: no computed subtotal of our own. ``subtotal`` and ``total_due``
    are what the document printed, so the validation engine has something of the supplier's
    to check its own arithmetic against (spec §4.3).
    """
    doc = document or golden_document()
    base: dict[str, object] = {
        "correlation_id": doc.correlation_id,
        "document_hash": doc.document_hash,
        "supplier_name": _field(VENDOR_NAME),
        "invoice_number": _field(INVOICE_NUMBER),
        "po_reference": _field(PO_NUMBER),
        "invoice_date": _field(INVOICE_DATE),
        "currency": _field(CURRENCY),
        "lines": (
            ExtractedLine(
                item_id=_field(ITEM_ID),
                description=_field("15-inch laptop"),
                billed_qty=_field(BILLED_QTY),
                billed_unit_price=_field(BILLED_UNIT_PRICE),
                line_total=_field(BILLED_UNIT_PRICE * BILLED_QTY),
            ),
        ),
        "subtotal": _field(BILLED_UNIT_PRICE * BILLED_QTY),
        "tax": None,
        "shipping": _field(SHIPPING_CHARGED),
        "total_due": _field(BILLED_TOTAL),
        "extracted_at": dt.datetime(2026, 1, 16, 9, 31, tzinfo=dt.UTC),
        "model_id": "fixture-extractor",
        "prompt_version": "golden-v1",
    }
    return ExtractedInvoice(**{**base, **overrides})  # type: ignore[arg-type]
