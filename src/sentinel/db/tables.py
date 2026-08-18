"""The PostgreSQL schema.

Some of the system's guarantees are stated in Python and *enforced* here, because a
constraint the application checks is a constraint two concurrent workers can both pass:

* ``erp_transactions.idempotency_key`` is **UNIQUE**. That single index, not the application
  logic above it, is what makes DoD-6 true -- two workers racing to post the same invoice
  cannot both insert, so at most one ERP transaction exists per action however many times it
  is retried.
* ``audit_events`` is append-only, enforced by a trigger that raises on UPDATE and DELETE
  (see ``migrations/``). Spec §12 calls the log immutable; a convention that developers
  respect is not immutability.
* Every amount column is ``NUMERIC`` and sits beside its currency.
* Every timestamp is ``TIMESTAMPTZ``.

Storage of extraction and evidence payloads is ``JSONB``. Those are model outputs whose shape
changes with the prompt and the schema version, and normalising them into columns would mean
a migration every time a prompt changes -- while the fields that decisions actually depend on
are recomputed by the validation engine and stored in typed columns regardless.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sentinel.db.base import Amount, Base, Currency, Identifier, Quantity, Timestamp

__all__ = [
    "AuditEventRow",
    "DeadLetterRow",
    "ErpTransactionRow",
    "GoodsReceiptLineRow",
    "GoodsReceiptRow",
    "InvoiceRow",
    "PolicyVersionRow",
    "PurchaseOrderLineRow",
    "PurchaseOrderRow",
    "VendorBankAccountRow",
    "VendorContractRow",
    "VendorRow",
]


# ---------------------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------------------


class VendorRow(Base):
    __tablename__ = "vendors"

    vendor_id: Mapped[Identifier] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    first_seen: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    invoice_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    mean_invoice_amount: Mapped[Decimal | None] = mapped_column(nullable=True)
    max_invoice_amount: Mapped[Decimal | None] = mapped_column(nullable=True)
    currency: Mapped[Currency | None] = mapped_column(nullable=True)

    bank_accounts: Mapped[list[VendorBankAccountRow]] = relationship(
        back_populates="vendor", cascade="all, delete-orphan"
    )
    contracts: Mapped[list[VendorContractRow]] = relationship(
        back_populates="vendor", cascade="all, delete-orphan"
    )

    __table_args__ = (CheckConstraint("invoice_count >= 0", name="invoice_count_non_negative"),)


class VendorBankAccountRow(Base):
    """Banking history. Rows are added, never edited.

    A changed account is a *new row*, because spec §7's payment-change signal is computed
    from when the change happened. Updating in place would erase the very fact the signal
    depends on.
    """

    __tablename__ = "vendor_bank_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vendor_id: Mapped[Identifier] = mapped_column(ForeignKey("vendors.vendor_id"), index=True)
    account_fingerprint: Mapped[str] = mapped_column(String(128))
    """A hash of the account details. Sentinel reasons about *changes*, so it never stores
    or moves the account number itself."""

    effective_from: Mapped[dt.date] = mapped_column(Date)

    vendor: Mapped[VendorRow] = relationship(back_populates="bank_accounts")

    __table_args__ = (
        UniqueConstraint("vendor_id", "effective_from", name="uq_vendor_bank_effective"),
    )


class VendorContractRow(Base):
    __tablename__ = "vendor_contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vendor_id: Mapped[Identifier] = mapped_column(ForeignKey("vendors.vendor_id"), index=True)
    pricing_terms: Mapped[str] = mapped_column(Text, default="", server_default="")
    shipping_allowed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    max_shipping: Mapped[Decimal | None] = mapped_column(nullable=True)
    max_shipping_currency: Mapped[Currency | None] = mapped_column(nullable=True)
    price_tolerance_pct: Mapped[Decimal | None] = mapped_column(nullable=True)
    effective_from: Mapped[dt.date] = mapped_column(Date)
    effective_to: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    vendor: Mapped[VendorRow] = relationship(back_populates="contracts")

    __table_args__ = (
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effective_dates_ordered",
        ),
    )


# ---------------------------------------------------------------------------------------
# Purchase orders and goods receipts
# ---------------------------------------------------------------------------------------


class PurchaseOrderRow(Base):
    __tablename__ = "purchase_orders"

    po_number: Mapped[str] = mapped_column(String(64), primary_key=True)
    vendor_id: Mapped[Identifier] = mapped_column(ForeignKey("vendors.vendor_id"), index=True)
    currency: Mapped[Currency]
    issued_date: Mapped[dt.date] = mapped_column(Date)
    approved_shipping: Mapped[Decimal | None] = mapped_column(nullable=True)
    """NULL means no shipping was agreed -- deliberately distinct from an agreed zero,
    because the two route differently once a supplier bills for it (spec §5)."""

    lines: Mapped[list[PurchaseOrderLineRow]] = relationship(
        back_populates="purchase_order", cascade="all, delete-orphan"
    )


class PurchaseOrderLineRow(Base):
    __tablename__ = "purchase_order_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    po_number: Mapped[str] = mapped_column(ForeignKey("purchase_orders.po_number"), index=True)
    item_id: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    agreed_unit_price: Mapped[Amount]
    approved_qty: Mapped[Quantity]

    purchase_order: Mapped[PurchaseOrderRow] = relationship(back_populates="lines")

    __table_args__ = (
        UniqueConstraint("po_number", "item_id", name="uq_po_line_item"),
        CheckConstraint("approved_qty > 0", name="approved_qty_positive"),
    )


class GoodsReceiptRow(Base):
    __tablename__ = "goods_receipts"

    grn_number: Mapped[str] = mapped_column(String(64), primary_key=True)
    po_number: Mapped[str] = mapped_column(ForeignKey("purchase_orders.po_number"), index=True)
    received_date: Mapped[dt.date] = mapped_column(Date)

    lines: Mapped[list[GoodsReceiptLineRow]] = relationship(
        back_populates="goods_receipt", cascade="all, delete-orphan"
    )


class GoodsReceiptLineRow(Base):
    """ADR-0007: ``damaged_qty`` is a subset of ``received_qty``, enforced by a check
    constraint so the invariant survives a direct SQL insert."""

    __tablename__ = "goods_receipt_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    grn_number: Mapped[str] = mapped_column(ForeignKey("goods_receipts.grn_number"), index=True)
    item_id: Mapped[str] = mapped_column(String(64))
    received_qty: Mapped[Quantity]
    damaged_qty: Mapped[Quantity] = mapped_column(default=Decimal(0), server_default="0")

    goods_receipt: Mapped[GoodsReceiptRow] = relationship(back_populates="lines")

    __table_args__ = (
        UniqueConstraint("grn_number", "item_id", name="uq_grn_line_item"),
        CheckConstraint("received_qty >= 0", name="received_qty_non_negative"),
        CheckConstraint("damaged_qty >= 0", name="damaged_qty_non_negative"),
        CheckConstraint("damaged_qty <= received_qty", name="damaged_within_received"),
    )


# ---------------------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------------------


class InvoiceRow(Base):
    """One invoice's state and evidence.

    Note what is *not* unique here. ``(vendor_id, invoice_number)`` carries only an index,
    not a unique constraint: duplicate invoices must be **detected and routed** (spec §6),
    not rejected by the database. A unique constraint would turn a suspected duplicate into
    an insert failure with no evidence, no assessment, and nothing for a human to review.
    """

    __tablename__ = "invoices"

    invoice_id: Mapped[Identifier] = mapped_column(primary_key=True)
    correlation_id: Mapped[Identifier] = mapped_column(unique=True, index=True)
    """Unique: one correlation ID belongs to exactly one invoice, for its whole life."""

    document_hash: Mapped[str] = mapped_column(String(64), index=True)
    """Indexed, not unique -- an exact-hash duplicate is a finding, not an insert error."""

    storage_uri: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), index=True)

    vendor_id: Mapped[Identifier | None] = mapped_column(
        ForeignKey("vendors.vendor_id"), nullable=True, index=True
    )
    invoice_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    normalized_invoice_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """``INV-8821`` reduced to ``INV8821``. Stored rather than computed at query time so
    fuzzy duplicate lookup (spec §6) is an index scan instead of a table scan."""

    po_number: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    invoice_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    currency: Mapped[Currency | None] = mapped_column(nullable=True)
    total_due: Mapped[Decimal | None] = mapped_column(nullable=True)

    extraction: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    validation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    duplicates: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    risk: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    reasoning: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    policy_decision: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    risk_score: Mapped[Decimal | None] = mapped_column(nullable=True)
    """Lifted out of the JSONB payload into a typed column so the review queue can sort and
    filter on it without deserialising every invoice."""

    received_at: Mapped[Timestamp]
    updated_at: Mapped[Timestamp]

    __table_args__ = (
        Index("ix_invoices_vendor_number", "vendor_id", "normalized_invoice_number"),
        Index("ix_invoices_queue", "status", "risk_score"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
    )


class DeadLetterRow(Base):
    """Documents that never entered the pipeline. Spec §4.1.

    Kept, not discarded: a rejected upload is something a human will ask about, and the
    reason has to survive that conversation.
    """

    __tablename__ = "dead_letters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    correlation_id: Mapped[Identifier] = mapped_column(index=True)
    document_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filename: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="", server_default="")
    source: Mapped[str] = mapped_column(String(32))
    received_at: Mapped[Timestamp]


# ---------------------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------------------


class PolicyVersionRow(Base):
    """One immutable version of the policy set. Spec §9.

    Rows are never updated. A policy change writes a new version, and every decision records
    the version that produced it, so a historical decision can be replayed exactly.
    """

    __tablename__ = "policy_versions"

    policy_version_id: Mapped[Identifier] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(128))
    rules: Mapped[dict[str, Any]] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", index=True
    )
    created_at: Mapped[Timestamp]
    created_by: Mapped[str] = mapped_column(String(128))


class AuditEventRow(Base):
    """Append-only. Spec §12.

    The trigger in the initial migration raises on UPDATE and DELETE, including for the
    application's own role. Immutability that only holds while everyone remembers to respect
    it is not immutability, and DoD-5 depends on this log being complete.
    """

    __tablename__ = "audit_events"

    event_id: Mapped[Identifier] = mapped_column(primary_key=True)
    correlation_id: Mapped[Identifier] = mapped_column(index=True)
    stage: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(128))
    actor_role: Mapped[str] = mapped_column(String(32))
    result: Mapped[str] = mapped_column(Text)
    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    policy_version: Mapped[Identifier | None] = mapped_column(nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    occurred_at: Mapped[Timestamp]

    __table_args__ = (Index("ix_audit_correlation_time", "correlation_id", "occurred_at"),)


class ErpTransactionRow(Base):
    """One attempted ERP posting.

    **The unique index on ``idempotency_key`` is the mechanism behind DoD-6.** Two workers
    racing to post the same invoice both derive the same key, and PostgreSQL lets exactly one
    of them insert. The loser reads the winner's row and reports the posting as already done.

    Application-level "check then act" cannot provide this: between the check and the act,
    the other worker acts.
    """

    __tablename__ = "erp_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    correlation_id: Mapped[Identifier] = mapped_column(index=True)
    invoice_id: Mapped[Identifier | None] = mapped_column(nullable=True)
    erp_transaction_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    adapter: Mapped[str] = mapped_column(String(64))
    posted_amount: Mapped[Decimal | None] = mapped_column(nullable=True)
    posted_currency: Mapped[Currency | None] = mapped_column(nullable=True)
    succeeded: Mapped[bool] = mapped_column(Boolean)
    message: Mapped[str] = mapped_column(Text, default="", server_default="")
    executed_at: Mapped[Timestamp]

    __table_args__ = (
        CheckConstraint(
            "NOT succeeded OR erp_transaction_id IS NOT NULL",
            name="success_identifies_transaction",
        ),
    )
