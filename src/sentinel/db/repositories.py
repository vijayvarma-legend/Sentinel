"""Controlled data access.

Spec §4.3 says the validation engine retrieves PO and GRN records "using controlled database
tools", and §12 forbids arbitrary LLM-generated SQL. These repositories are what "controlled"
means in practice: a fixed, named set of queries, each returning domain objects rather than
rows. Nothing above this layer composes SQL, and the advisory modules cannot reach this layer
at all (ADR-0004).

Repositories translate at the boundary -- rows in, :mod:`sentinel.core` models out. That is
what keeps the domain layer free of any persistence dependency, which the architecture test
enforces.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from sentinel.core.business import (
    GoodsReceiptLine,
    GoodsReceiptNote,
    PurchaseOrder,
    PurchaseOrderLine,
    VendorBankAccount,
    VendorContract,
    VendorProfile,
)
from sentinel.core.enums import InvoiceStatus
from sentinel.core.errors import IdempotencyConflict
from sentinel.core.evidence import AuditEvent, IngestedDocument
from sentinel.core.ids import CorrelationId, DocumentHash, InvoiceId, VendorId
from sentinel.core.money import Money
from sentinel.db.tables import (
    AuditEventRow,
    DeadLetterRow,
    ErpTransactionRow,
    GoodsReceiptRow,
    InvoiceRow,
    PurchaseOrderRow,
    VendorRow,
)

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

__all__ = [
    "AuditRepository",
    "ErpTransactionRepository",
    "GoodsReceiptRepository",
    "InvoiceRepository",
    "PurchaseOrderRepository",
    "VendorRepository",
    "normalize_invoice_number",
]


def normalize_invoice_number(raw: str) -> str:
    """Reduce an invoice number to its comparable form.

    Spec §6 wants ``INV-8821`` and ``INV8821`` recognised as the same number. Punctuation,
    whitespace, and case are the formatting a supplier's template varies; the alphanumerics
    are the number itself.

        >>> normalize_invoice_number("INV-8821")
        'INV8821'
        >>> normalize_invoice_number("inv 8821")
        'INV8821'

    Leading zeros are **kept**: ``INV-0042`` and ``INV-42`` may well be different invoices,
    and collapsing them would merge two real documents into one apparent duplicate.
    """
    return "".join(character for character in raw if character.isalnum()).upper()


class VendorRepository:
    """Vendor profiles, contracts, and banking history."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, vendor_id: VendorId) -> VendorProfile | None:
        row = self._session.get(VendorRow, str(vendor_id))
        return self._to_profile(row) if row else None

    def find_by_name(self, name: str) -> VendorProfile | None:
        row = self._session.scalars(select(VendorRow).where(VendorRow.name == name)).one_or_none()
        return self._to_profile(row) if row else None

    def contract_effective_on(self, vendor_id: VendorId, when: dt.date) -> VendorContract | None:
        """The contract governing an invoice dated `when`.

        Resolved by date rather than by "the current contract" so a historical decision can
        be reconstructed exactly (spec §18) -- invoices routinely arrive against contracts
        that have since expired.
        """
        row = self._session.get(VendorRow, str(vendor_id))
        if row is None:
            return None

        candidates = [
            contract
            for contract in row.contracts
            if contract.effective_from <= when
            and (contract.effective_to is None or when <= contract.effective_to)
        ]
        if not candidates:
            return None

        latest = max(candidates, key=lambda c: c.effective_from)
        return VendorContract(
            vendor_id=VendorId(latest.vendor_id),
            pricing_terms=latest.pricing_terms,
            shipping_allowed=latest.shipping_allowed,
            max_shipping=(
                Money(latest.max_shipping, latest.max_shipping_currency or "USD")
                if latest.max_shipping is not None
                else None
            ),
            price_tolerance_pct=latest.price_tolerance_pct,
            effective_from=latest.effective_from,
            effective_to=latest.effective_to,
        )

    def _to_profile(self, row: VendorRow) -> VendorProfile:
        currency = row.currency or "USD"
        return VendorProfile(
            vendor_id=VendorId(row.vendor_id),
            name=row.name,
            first_seen=row.first_seen,
            invoice_count=row.invoice_count,
            mean_invoice_amount=(
                Money(row.mean_invoice_amount, currency)
                if row.mean_invoice_amount is not None
                else None
            ),
            max_invoice_amount=(
                Money(row.max_invoice_amount, currency)
                if row.max_invoice_amount is not None
                else None
            ),
            bank_accounts=tuple(
                VendorBankAccount(
                    account_fingerprint=account.account_fingerprint,
                    effective_from=account.effective_from,
                )
                for account in sorted(row.bank_accounts, key=lambda a: a.effective_from)
            ),
        )


class PurchaseOrderRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, po_number: str) -> PurchaseOrder | None:
        row = self._session.get(PurchaseOrderRow, po_number)
        if row is None:
            return None

        return PurchaseOrder(
            po_number=row.po_number,
            vendor_id=VendorId(row.vendor_id),
            currency=row.currency,
            lines=tuple(
                PurchaseOrderLine(
                    item_id=line.item_id,
                    description=line.description,
                    agreed_unit_price=Money(line.agreed_unit_price, row.currency),
                    approved_qty=line.approved_qty,
                )
                for line in row.lines
            ),
            issued_date=row.issued_date,
            approved_shipping=(
                Money(row.approved_shipping, row.currency)
                if row.approved_shipping is not None
                else None
            ),
        )


class GoodsReceiptRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, grn_number: str) -> GoodsReceiptNote | None:
        row = self._session.get(GoodsReceiptRow, grn_number)
        return self._to_note(row) if row else None

    def latest_for_po(self, po_number: str) -> GoodsReceiptNote | None:
        """The most recent receipt against `po_number`.

        Partial and repeated deliveries against one PO are normal; taking the latest is a
        simplification that holds while a PO has a single receipt. Consolidating multiple
        receipts is Phase 3 work, flagged here rather than assumed away.
        """
        row = self._session.scalars(
            select(GoodsReceiptRow)
            .where(GoodsReceiptRow.po_number == po_number)
            .order_by(GoodsReceiptRow.received_date.desc())
        ).first()
        return self._to_note(row) if row else None

    def _to_note(self, row: GoodsReceiptRow) -> GoodsReceiptNote:
        return GoodsReceiptNote(
            grn_number=row.grn_number,
            po_number=row.po_number,
            lines=tuple(
                GoodsReceiptLine(
                    item_id=line.item_id,
                    received_qty=line.received_qty,
                    damaged_qty=line.damaged_qty,
                )
                for line in row.lines
            ),
            received_date=row.received_date,
        )


class InvoiceRepository:
    """Invoice records and the lookups duplicate detection needs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, document: IngestedDocument) -> InvoiceId:
        """Record a newly ingested document as an invoice in ``RECEIVED`` status."""
        invoice_id = InvoiceId.new()
        now = dt.datetime.now(dt.UTC)

        self._session.add(
            InvoiceRow(
                invoice_id=str(invoice_id),
                correlation_id=str(document.correlation_id),
                document_hash=str(document.document_hash),
                storage_uri=document.storage_uri,
                filename=document.filename,
                content_type=document.content_type,
                size_bytes=document.size_bytes,
                page_count=document.page_count,
                source=document.source,
                status=InvoiceStatus.RECEIVED,
                received_at=document.received_at,
                updated_at=now,
            )
        )
        self._session.flush()
        return invoice_id

    def get(self, invoice_id: InvoiceId) -> InvoiceRow | None:
        return self._session.get(InvoiceRow, str(invoice_id))

    def by_correlation_id(self, correlation_id: CorrelationId) -> InvoiceRow | None:
        return self._session.scalars(
            select(InvoiceRow).where(InvoiceRow.correlation_id == str(correlation_id))
        ).one_or_none()

    def with_document_hash(self, document_hash: DocumentHash) -> list[InvoiceRow]:
        """Every invoice whose original document was byte-identical.

        The exact-hash tier of spec §6 -- the only tier that is unambiguous.
        """
        return list(
            self._session.scalars(
                select(InvoiceRow).where(InvoiceRow.document_hash == str(document_hash))
            )
        )

    def candidates_for_duplicate_check(
        self, *, vendor_id: VendorId | None, invoice_number: str, exclude: InvoiceId
    ) -> list[InvoiceRow]:
        """Prior invoices from the same vendor with a matching normalized number.

        Deliberately narrow. This is the *candidate* set that the duplicate service then
        scores; casting a wider net here would push similarity work into SQL, where it could
        not be explained to a reviewer.
        """
        query = select(InvoiceRow).where(
            InvoiceRow.normalized_invoice_number == normalize_invoice_number(invoice_number),
            InvoiceRow.invoice_id != str(exclude),
        )
        if vendor_id is not None:
            query = query.where(InvoiceRow.vendor_id == str(vendor_id))
        return list(self._session.scalars(query))

    def set_status(self, invoice_id: InvoiceId, status: InvoiceStatus) -> None:
        row = self._session.get(InvoiceRow, str(invoice_id))
        if row is None:
            raise LookupError(f"no invoice {invoice_id}")
        row.status = status
        row.updated_at = dt.datetime.now(dt.UTC)
        self._session.flush()

    def record_stage(
        self,
        invoice_id: InvoiceId,
        *,
        field: str,
        payload: dict[str, Any],
        status: InvoiceStatus | None = None,
    ) -> None:
        """Persist one stage's evidence payload, optionally advancing the status."""
        if field not in {
            "extraction",
            "validation",
            "duplicates",
            "risk",
            "reasoning",
            "policy_decision",
        }:
            raise ValueError(f"{field!r} is not a stage payload column")

        row = self._session.get(InvoiceRow, str(invoice_id))
        if row is None:
            raise LookupError(f"no invoice {invoice_id}")

        setattr(row, field, payload)
        if status is not None:
            row.status = status
        row.updated_at = dt.datetime.now(dt.UTC)
        self._session.flush()

    def dead_letter(
        self,
        *,
        correlation_id: CorrelationId,
        filename: str,
        content_type: str,
        size_bytes: int,
        reason: str,
        detail: str,
        source: str,
        document_hash: DocumentHash | None = None,
    ) -> None:
        """Quarantine a document that never entered the pipeline. Spec §4.1.

        Recorded rather than discarded: a rejected upload is something a person will ask
        about later, and the reason has to survive that conversation.
        """
        self._session.add(
            DeadLetterRow(
                correlation_id=str(correlation_id),
                document_hash=str(document_hash) if document_hash else None,
                filename=filename,
                content_type=content_type,
                size_bytes=size_bytes,
                reason=reason,
                detail=detail,
                source=source,
                received_at=dt.datetime.now(dt.UTC),
            )
        )
        self._session.flush()


class AuditRepository:
    """Append-only. The only write this class performs is an INSERT.

    There is no update or delete method, and adding one would not help: the database refuses
    both (spec §12).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, event: AuditEvent) -> None:
        self._session.add(
            AuditEventRow(
                event_id=event.event_id,
                correlation_id=str(event.correlation_id),
                stage=event.stage,
                action=event.action,
                actor_id=event.actor_id,
                actor_role=event.actor_role,
                result=event.result,
                agent_version=event.agent_version,
                model_id=event.model_id,
                policy_version=str(event.policy_version) if event.policy_version else None,
                detail=event.detail,
                occurred_at=event.occurred_at,
            )
        )
        self._session.flush()

    def trail_for(self, correlation_id: CorrelationId) -> list[AuditEventRow]:
        """Every event for one invoice, oldest first.

        This is the query DoD-5 is answered with: a completed invoice must be replayable
        from these rows alone.
        """
        return list(
            self._session.scalars(
                select(AuditEventRow)
                .where(AuditEventRow.correlation_id == str(correlation_id))
                .order_by(AuditEventRow.occurred_at, AuditEventRow.event_id)
            )
        )


class ErpTransactionRepository:
    """The idempotency gate in front of the ERP. Spec §11, DoD-6."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find(self, idempotency_key: str) -> ErpTransactionRow | None:
        return self._session.scalars(
            select(ErpTransactionRow).where(ErpTransactionRow.idempotency_key == idempotency_key)
        ).one_or_none()

    def claim(
        self,
        *,
        idempotency_key: str,
        correlation_id: CorrelationId,
        invoice_id: InvoiceId | None,
        adapter: str,
    ) -> ErpTransactionRow:
        """Reserve the right to post this action, or raise if someone already holds it.

        The claim is an INSERT, so the unique index decides the race rather than this code.
        Two workers that both derive the same key both attempt the insert; PostgreSQL lets
        exactly one through, and the other gets :class:`IdempotencyConflict` carrying the
        winner's transaction so the caller can report the posting as already done.

        A "check whether it exists, then insert" version of this method would pass every
        test and double-pay under concurrency.
        """
        row = ErpTransactionRow(
            idempotency_key=idempotency_key,
            correlation_id=str(correlation_id),
            invoice_id=str(invoice_id) if invoice_id else None,
            adapter=adapter,
            succeeded=False,
            executed_at=dt.datetime.now(dt.UTC),
        )
        self._session.add(row)

        try:
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            existing = self.find(idempotency_key)
            raise IdempotencyConflict(
                "this action has already been claimed",
                correlation_id=str(correlation_id),
                idempotency_key=idempotency_key,
                existing_transaction_id=existing.erp_transaction_id if existing else None,
                existing_succeeded=existing.succeeded if existing else None,
            ) from exc

        return row

    def complete(
        self,
        row: ErpTransactionRow,
        *,
        erp_transaction_id: str | None,
        succeeded: bool,
        message: str = "",
        posted_amount: Decimal | None = None,
        posted_currency: str | None = None,
    ) -> None:
        """Record the outcome of a claimed posting."""
        row.erp_transaction_id = erp_transaction_id
        row.succeeded = succeeded
        row.message = message
        row.posted_amount = posted_amount
        row.posted_currency = posted_currency
        row.executed_at = dt.datetime.now(dt.UTC)
        self._session.flush()
