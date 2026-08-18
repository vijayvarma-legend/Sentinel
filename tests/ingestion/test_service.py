"""Ingestion behaviour. Integration -- ingestion writes rows, so it needs the database."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from sentinel.core.enums import DocumentSource, InvoiceStatus
from sentinel.core.errors import DocumentTooLarge, IngestionError, UnsupportedDocument
from sentinel.db.repositories import AuditRepository, InvoiceRepository
from sentinel.db.tables import DeadLetterRow
from sentinel.ingestion.service import IngestionService, sniff_content_type
from sentinel.storage.memory import InMemoryDocumentStore

pytestmark = pytest.mark.integration

PDF = b"%PDF-1.7\nfake invoice INV-8821\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


@pytest.fixture
def store() -> InMemoryDocumentStore:
    return InMemoryDocumentStore()


@pytest.fixture
def service(session: Session, store: InMemoryDocumentStore) -> Iterator[IngestionService]:
    yield IngestionService(
        store=store,
        invoices=InvoiceRepository(session),
        audit=AuditRepository(session),
        max_bytes=1024,
    )


class TestContentSniffing:
    """The declared content type comes from the client, so the bytes decide."""

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (PDF, "application/pdf"),
            (PNG, "image/png"),
            (JPEG, "image/jpeg"),
            (b"II*\x00rest", "image/tiff"),
            (b"MM\x00*rest", "image/tiff"),
            (b"RIFF\x00\x00\x00\x00WEBPvp8", "image/webp"),
        ],
    )
    def test_recognises_supported_formats(self, data: bytes, expected: str) -> None:
        assert sniff_content_type(data) == expected

    def test_a_riff_container_that_is_not_webp_is_not_an_image(self) -> None:
        """RIFF is a container family -- a WAV file starts the same way as a WebP."""
        assert sniff_content_type(b"RIFF\x00\x00\x00\x00WAVEfmt ") is None

    def test_unrecognised_bytes_return_none(self) -> None:
        assert sniff_content_type(b"PK\x03\x04 this is a zip") is None
        assert sniff_content_type(b"") is None


class TestAcceptance:
    def test_a_valid_pdf_is_admitted(self, service: IngestionService) -> None:
        result = service.ingest(PDF, filename="INV-8821.pdf")

        assert result.document.content_type == "application/pdf"
        assert result.document.size_bytes == len(PDF)
        assert result.document.source == DocumentSource.UPLOAD
        assert not result.was_already_stored

    def test_a_correlation_id_is_minted_per_ingestion(self, service: IngestionService) -> None:
        first = service.ingest(PDF, filename="a.pdf")
        second = service.ingest(PDF + b" v2", filename="b.pdf")
        assert first.document.correlation_id != second.document.correlation_id

    def test_the_invoice_starts_in_received_status(
        self, service: IngestionService, session: Session
    ) -> None:
        result = service.ingest(PDF, filename="INV-8821.pdf")
        row = InvoiceRepository(session).get(result.invoice_id)

        assert row is not None
        assert row.status == InvoiceStatus.RECEIVED
        assert row.document_hash == str(result.document.document_hash)

    def test_a_declared_content_type_is_ignored_in_favour_of_the_bytes(
        self, service: IngestionService
    ) -> None:
        """An uploader mislabelling a PNG as a PDF should not fool the pipeline."""
        result = service.ingest(PNG, filename="scan.pdf", declared_content_type="application/pdf")
        assert result.document.content_type == "image/png"

    def test_resubmitting_identical_bytes_reports_prior_storage(
        self, service: IngestionService
    ) -> None:
        """A fact about storage, not a duplicate verdict -- that is SVC-40's call."""
        first = service.ingest(PDF, filename="INV-8821.pdf")
        second = service.ingest(PDF, filename="INV-8821-resent.pdf")

        assert not first.was_already_stored
        assert second.was_already_stored
        assert second.document.document_hash == first.document.document_hash
        assert second.invoice_id != first.invoice_id, (
            "a resubmission is still its own invoice record -- spec section 6 requires it to "
            "be assessed and routed, not silently swallowed"
        )


class TestRejection:
    def test_an_empty_file_is_refused(self, service: IngestionService) -> None:
        with pytest.raises(UnsupportedDocument, match="empty"):
            service.ingest(b"", filename="nothing.pdf")

    def test_an_oversized_file_is_refused(self, service: IngestionService) -> None:
        with pytest.raises(DocumentTooLarge, match="over the"):
            service.ingest(b"%PDF-1.7" + b"\x00" * 2048, filename="huge.pdf")

    def test_an_unreadable_format_is_refused(self, service: IngestionService) -> None:
        with pytest.raises(UnsupportedDocument, match="not a document Sentinel can read"):
            service.ingest(b"PK\x03\x04 this is a docx", filename="invoice.docx")

    def test_a_rejected_document_is_dead_lettered_with_its_reason(
        self, service: IngestionService, session: Session
    ) -> None:
        """Spec section 4.1. A rejected upload is something a person will ask about later."""
        with pytest.raises(IngestionError):
            service.ingest(b"PK\x03\x04 nope", filename="invoice.docx")

        letters = session.query(DeadLetterRow).all()
        assert len(letters) == 1
        assert letters[0].reason == "UnsupportedDocument"
        assert "invoice.docx" in letters[0].filename
        assert letters[0].detail

    def test_a_rejected_document_is_findable_by_correlation_id(
        self, service: IngestionService, session: Session
    ) -> None:
        """The ID is minted before validation precisely so this lookup works."""
        with pytest.raises(IngestionError):
            service.ingest(b"PK\x03\x04 nope", filename="invoice.docx")

        letter = session.query(DeadLetterRow).one()
        assert letter.correlation_id.startswith("cor_")

    def test_a_rejected_document_creates_no_invoice(
        self, service: IngestionService, session: Session
    ) -> None:
        """It must not enter the pipeline at all -- spec section 4.1."""
        from sentinel.db.tables import InvoiceRow

        with pytest.raises(IngestionError):
            service.ingest(b"PK\x03\x04 nope", filename="invoice.docx")

        assert session.query(InvoiceRow).count() == 0

    def test_a_rejected_document_is_not_stored_in_the_object_store(
        self, service: IngestionService, store: InMemoryDocumentStore
    ) -> None:
        with pytest.raises(IngestionError):
            service.ingest(b"PK\x03\x04 nope", filename="invoice.docx")

        assert len(store) == 0


class TestAuditTrail:
    def test_acceptance_is_recorded(self, service: IngestionService, session: Session) -> None:
        result = service.ingest(PDF, filename="INV-8821.pdf")
        trail = AuditRepository(session).trail_for(result.document.correlation_id)

        assert len(trail) == 1
        assert trail[0].action == "ingest"
        assert trail[0].result == "accepted"
        assert trail[0].detail["document_hash"] == str(result.document.document_hash)

    def test_rejection_is_recorded_too(self, service: IngestionService, session: Session) -> None:
        """A dead-letter is a decision, and spec section 12 audits every material decision."""
        with pytest.raises(IngestionError):
            service.ingest(b"PK\x03\x04 nope", filename="invoice.docx")

        from sentinel.db.tables import AuditEventRow

        event = session.query(AuditEventRow).one()
        assert event.result.startswith("dead_lettered")
        assert event.stage == "ingestion"
