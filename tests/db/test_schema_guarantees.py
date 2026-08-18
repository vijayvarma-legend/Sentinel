"""The guarantees the database enforces on its own.

These are the invariants that must survive a concurrent worker, a direct SQL session, and a
future refactor that forgets the application-level check. Each one is a property the spec
asks for, expressed where it cannot be bypassed.

All integration tests -- they need the real PostgreSQL from `make up`, because the mechanisms
under test (triggers, unique indexes, check constraints) do not exist anywhere else.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from sentinel.db.tables import (
    AuditEventRow,
    ErpTransactionRow,
    GoodsReceiptLineRow,
    GoodsReceiptRow,
    PurchaseOrderRow,
    VendorRow,
)

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 1, 16, 10, 0, tzinfo=dt.UTC)


def _audit(event_id: str) -> AuditEventRow:
    return AuditEventRow(
        event_id=event_id,
        correlation_id="cor_test",
        stage="validation",
        action="validate",
        actor_id="system",
        actor_role="system",
        result="passed",
        occurred_at=NOW,
    )


class TestAuditLogIsAppendOnly:
    """Spec §12 calls the audit log immutable. DoD-5 depends on it being complete.

    A convention developers respect is not immutability, so PostgreSQL refuses the write --
    for the application's own role too.
    """

    def test_events_can_be_appended(self, raw_connection: Engine) -> None:
        with Session(raw_connection) as session:
            session.add(_audit("evt_append"))
            session.commit()
            assert session.get(AuditEventRow, "evt_append") is not None

    def test_an_update_is_refused(self, raw_connection: Engine) -> None:
        with Session(raw_connection) as session:
            session.add(_audit("evt_update"))
            session.commit()

        with (
            Session(raw_connection) as session,
            pytest.raises(DBAPIError, match="append-only"),
        ):
            session.execute(
                text("UPDATE audit_events SET result = 'tampered' WHERE event_id = 'evt_update'")
            )
            session.commit()

    def test_a_delete_is_refused(self, raw_connection: Engine) -> None:
        with Session(raw_connection) as session:
            session.add(_audit("evt_delete"))
            session.commit()

        with (
            Session(raw_connection) as session,
            pytest.raises(DBAPIError, match="append-only"),
        ):
            session.execute(text("DELETE FROM audit_events WHERE event_id = 'evt_delete'"))
            session.commit()


class TestIdempotencyIsEnforcedByTheDatabase:
    """DoD-6: ERP retries cannot create duplicate transactions.

    The unique index is the mechanism. Application-level check-then-act cannot provide this,
    because between the check and the act the other worker acts.
    """

    def erp(self, key: str, transaction_id: str) -> ErpTransactionRow:
        return ErpTransactionRow(
            idempotency_key=key,
            correlation_id="cor_test",
            adapter="mock",
            succeeded=True,
            erp_transaction_id=transaction_id,
            executed_at=NOW,
        )

    def test_the_first_posting_is_accepted(self, session: Session) -> None:
        session.add(self.erp("key_first", "ERP-1"))
        session.flush()

    def test_a_second_posting_with_the_same_key_is_refused(self, session: Session) -> None:
        session.add(self.erp("key_dup", "ERP-1"))
        session.flush()

        session.add(self.erp("key_dup", "ERP-2"))
        with pytest.raises(IntegrityError, match="idempotency_key"):
            session.flush()

    def test_distinct_keys_coexist(self, session: Session) -> None:
        session.add(self.erp("key_a", "ERP-1"))
        session.add(self.erp("key_b", "ERP-2"))
        session.flush()

    def test_a_success_must_name_its_transaction(self, session: Session) -> None:
        """Otherwise spec §18's reproducibility requirement cannot be met."""
        session.add(
            ErpTransactionRow(
                idempotency_key="key_nameless",
                correlation_id="cor_test",
                adapter="mock",
                succeeded=True,
                erp_transaction_id=None,
                executed_at=NOW,
            )
        )
        with pytest.raises(IntegrityError, match="success_identifies_transaction"):
            session.flush()

    def test_a_failure_needs_no_transaction_id(self, session: Session) -> None:
        session.add(
            ErpTransactionRow(
                idempotency_key="key_failed",
                correlation_id="cor_test",
                adapter="mock",
                succeeded=False,
                erp_transaction_id=None,
                message="ERP rejected: period closed",
                executed_at=NOW,
            )
        )
        session.flush()


class TestQuantityInvariantSurvivesDirectSql:
    """ADR-0007 in the schema, so it holds for a bulk load as well as for the ORM."""

    def _receipt(self, session: Session) -> None:
        """Insert the vendor, PO, and GRN in dependency order.

        Flushed in stages deliberately: GoodsReceiptRow references PurchaseOrderRow by a
        bare foreign key with no ORM relationship, so SQLAlchemy's unit of work has nothing
        to order them by and is free to emit the child first.
        """
        session.add(VendorRow(vendor_id="ven_test", name="TechCorp", invoice_count=10))
        session.flush()

        session.add(
            PurchaseOrderRow(
                po_number="9901",
                vendor_id="ven_test",
                currency="USD",
                issued_date=dt.date(2026, 1, 5),
            )
        )
        session.flush()

        session.add(
            GoodsReceiptRow(
                grn_number="GRN-1", po_number="9901", received_date=dt.date(2026, 1, 12)
            )
        )
        session.flush()

    def test_the_golden_receipt_is_accepted(self, session: Session) -> None:
        """Ten received, one damaged, nine accepted -- spec §15."""
        self._receipt(session)
        session.add(
            GoodsReceiptLineRow(
                grn_number="GRN-1",
                item_id="LAPTOP-01",
                received_qty=Decimal(10),
                damaged_qty=Decimal(1),
            )
        )
        session.flush()

    def test_damaged_exceeding_received_is_refused(self, session: Session) -> None:
        self._receipt(session)
        session.add(
            GoodsReceiptLineRow(
                grn_number="GRN-1",
                item_id="LAPTOP-01",
                received_qty=Decimal(5),
                damaged_qty=Decimal(6),
            )
        )
        with pytest.raises(IntegrityError, match="damaged_within_received"):
            session.flush()


class TestPolicyVersionsAreImmutable:
    """Spec §9: a historical decision must be replayable against the rules that produced it.

    Editing a version in place would silently rewrite the past. Activation is the one
    legitimately mutable aspect.
    """

    def _publish(self, connection: Engine, version_id: str) -> None:
        with connection.begin() as db:
            db.execute(
                text(
                    "INSERT INTO policy_versions "
                    "(policy_version_id, label, rules, is_active, created_at, created_by) "
                    "VALUES (:id, 'v1', '{\"price_tolerance_pct\": 2}', true, :now, 'admin')"
                ),
                {"id": version_id, "now": NOW},
            )

    def test_rules_cannot_be_edited(self, raw_connection: Engine) -> None:
        self._publish(raw_connection, "pol_immutable")

        with pytest.raises(DBAPIError, match="is immutable"), raw_connection.begin() as db:
            db.execute(
                text(
                    "UPDATE policy_versions SET rules = '{\"price_tolerance_pct\": 50}' "
                    "WHERE policy_version_id = 'pol_immutable'"
                )
            )

    def test_activation_can_still_be_toggled(self, raw_connection: Engine) -> None:
        self._publish(raw_connection, "pol_toggle")

        with raw_connection.begin() as db:
            db.execute(
                text(
                    "UPDATE policy_versions SET is_active = false "
                    "WHERE policy_version_id = 'pol_toggle'"
                )
            )
            active = db.execute(
                text("SELECT is_active FROM policy_versions WHERE policy_version_id = 'pol_toggle'")
            ).scalar_one()
        assert active is False


class TestDuplicatesAreDetectedNotRejected:
    """Spec §6 routes duplicates to review. The database must not pre-empt that.

    A unique constraint on (vendor, invoice_number) would turn a suspected duplicate into an
    insert failure -- no evidence, no assessment, nothing for a human to review.
    """

    def test_two_invoices_may_share_a_number_and_hash(self, session: Session) -> None:
        session.add(VendorRow(vendor_id="ven_dup", name="TechCorp", invoice_count=10))
        session.flush()

        for suffix in ("a", "b"):
            session.execute(
                text(
                    "INSERT INTO invoices (invoice_id, correlation_id, document_hash, "
                    "storage_uri, filename, content_type, size_bytes, source, status, "
                    "vendor_id, invoice_number, normalized_invoice_number, received_at, "
                    "updated_at) VALUES (:iid, :cid, :hash, 's3://x', 'INV-8821.pdf', "
                    "'application/pdf', 1024, 'upload', 'received', 'ven_dup', 'INV-8821', "
                    "'INV8821', :now, :now)"
                ),
                {
                    "iid": f"inv_{suffix}",
                    "cid": f"cor_{suffix}",
                    "hash": "a" * 64,
                    "now": NOW,
                },
            )
        session.flush()

        count = session.execute(
            text("SELECT count(*) FROM invoices WHERE normalized_invoice_number = 'INV8821'")
        ).scalar_one()
        assert count == 2, "the database must let duplicates in so they can be assessed"

    def test_a_correlation_id_belongs_to_exactly_one_invoice(self, session: Session) -> None:
        session.add(VendorRow(vendor_id="ven_cor", name="TechCorp", invoice_count=1))
        session.flush()

        def insert(invoice_id: str) -> None:
            session.execute(
                text(
                    "INSERT INTO invoices (invoice_id, correlation_id, document_hash, "
                    "storage_uri, filename, content_type, size_bytes, source, status, "
                    "received_at, updated_at) VALUES (:iid, 'cor_shared', :hash, 's3://x', "
                    "'f.pdf', 'application/pdf', 10, 'upload', 'received', :now, :now)"
                ),
                {"iid": invoice_id, "hash": "b" * 64, "now": NOW},
            )

        insert("inv_one")
        session.flush()

        # execute() emits immediately, so the violation surfaces here, not at flush.
        with pytest.raises(IntegrityError, match="correlation_id"):
            insert("inv_two")
