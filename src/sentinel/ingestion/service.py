"""SVC-10 -- invoice ingestion. Spec §4.1.

The pipeline's front door, and the only place that decides whether a document gets to exist
in the system at all. Its contract:

* Normalize metadata, store the original, mint a **correlation ID**, compute a **document
  hash**.
* Validate the file, and route anything unsupported or corrupt to a **dead-letter path**
  rather than letting it enter the pipeline.

The dead-letter path is a record, not a discard. A rejected upload is something a person will
ask about, and "we rejected it" is only a useful answer if it comes with the reason.

Sniffing over trusting: a browser's declared ``Content-Type`` comes from the client, and an
uploader that mislabels a Word document as ``application/pdf`` should be told so at the door,
not two stages later when a vision model returns nonsense for it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from sentinel.core.enums import ActorRole, DocumentSource, PipelineStage
from sentinel.core.errors import DocumentTooLarge, UnsupportedDocument
from sentinel.core.evidence import AuditEvent, IngestedDocument
from sentinel.core.ids import CorrelationId, DocumentHash, InvoiceId, uuid7

if TYPE_CHECKING:  # pragma: no cover
    from sentinel.db.repositories import AuditRepository, InvoiceRepository
    from sentinel.storage.store import DocumentStore

__all__ = ["IngestionResult", "IngestionService", "sniff_content_type"]

#: What the vision stage can actually read. Anything else is refused at the door rather than
#: discovered downstream.
SUPPORTED_TYPES: Final = frozenset(
    {"application/pdf", "image/png", "image/jpeg", "image/tiff", "image/webp"}
)

#: Leading bytes that identify a format. Checked instead of the declared content type, which
#: the client controls.
MAGIC_BYTES: Final[tuple[tuple[bytes, str], ...]] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"RIFF", "image/webp"),  # refined below -- RIFF alone is not enough
)


def sniff_content_type(data: bytes) -> str | None:
    """Identify a document from its leading bytes, or ``None`` if unrecognised."""
    for signature, content_type in MAGIC_BYTES:
        if data.startswith(signature):
            if content_type == "image/webp":
                # RIFF is a container family; only the WEBP form is an image we can read.
                return "image/webp" if data[8:12] == b"WEBP" else None
            return content_type
    return None


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """What ingestion produced, and whether these bytes had been seen before."""

    invoice_id: InvoiceId
    document: IngestedDocument
    was_already_stored: bool
    """True when the object store already held these exact bytes.

    Not a duplicate *verdict* -- that is SVC-40's job, with evidence. It is a fact about
    storage that duplicate detection will later use as its strongest signal.
    """


class IngestionService:
    """Admits documents to the pipeline, or dead-letters them with a reason."""

    def __init__(
        self,
        *,
        store: DocumentStore,
        invoices: InvoiceRepository,
        audit: AuditRepository,
        max_bytes: int,
    ) -> None:
        self._store = store
        self._invoices = invoices
        self._audit = audit
        self._max_bytes = max_bytes

    def ingest(
        self,
        data: bytes,
        *,
        filename: str,
        declared_content_type: str | None = None,
        source: DocumentSource = DocumentSource.UPLOAD,
    ) -> IngestionResult:
        """Admit a document, or raise an :class:`~sentinel.core.errors.IngestionError`.

        A correlation ID is minted **before** validation, so that even a rejected document
        has an identifier its dead-letter record can be found by. An upload nobody can look
        up afterwards is not much better than one that was silently dropped.
        """
        correlation_id = CorrelationId.new()

        try:
            content_type = self._validate(data, filename=filename)
        except (UnsupportedDocument, DocumentTooLarge) as exc:
            # Attach the ID before re-raising. Minting it early is only useful if it
            # actually reaches the caller -- otherwise they cannot quote the identifier
            # their dead-letter record is filed under.
            exc.correlation_id = str(correlation_id)
            self._invoices.dead_letter(
                correlation_id=correlation_id,
                filename=filename,
                content_type=declared_content_type or "unknown",
                size_bytes=len(data),
                reason=type(exc).__name__,
                detail=exc.message,
                source=source,
                document_hash=DocumentHash.of(data) if data else None,
            )
            self._record(
                correlation_id,
                action="ingest",
                result=f"dead_lettered: {type(exc).__name__}",
                detail={"filename": filename, "reason": exc.message},
            )
            raise

        stored = self._store.put(data, content_type=content_type)

        document = IngestedDocument(
            correlation_id=correlation_id,
            document_hash=stored.document_hash,
            storage_uri=stored.uri,
            filename=filename,
            content_type=content_type,
            size_bytes=len(data),
            page_count=None,  # counted during extraction, which already parses the document
            source=source,
            received_at=dt.datetime.now(dt.UTC),
        )

        invoice_id = self._invoices.create(document)

        self._record(
            correlation_id,
            action="ingest",
            result="accepted",
            detail={
                "invoice_id": str(invoice_id),
                "filename": filename,
                "content_type": content_type,
                "size_bytes": len(data),
                "document_hash": str(stored.document_hash),
                "storage_uri": stored.uri,
                "was_already_stored": stored.already_existed,
            },
        )

        return IngestionResult(
            invoice_id=invoice_id,
            document=document,
            was_already_stored=stored.already_existed,
        )

    # -- internals ----------------------------------------------------------------------

    def _validate(self, data: bytes, *, filename: str) -> str:
        """Return the sniffed content type, or raise explaining why the file is refused."""
        if not data:
            raise UnsupportedDocument(f"{filename!r} is empty", filename=filename, size_bytes=0)

        if len(data) > self._max_bytes:
            raise DocumentTooLarge(
                f"{filename!r} is {len(data):,} bytes, over the {self._max_bytes:,}-byte limit",
                filename=filename,
                size_bytes=len(data),
                limit=self._max_bytes,
            )

        sniffed = sniff_content_type(data)
        if sniffed is None:
            raise UnsupportedDocument(
                f"{filename!r} is not a document Sentinel can read. Its contents match no "
                f"supported format ({', '.join(sorted(SUPPORTED_TYPES))}).",
                filename=filename,
                size_bytes=len(data),
            )

        if sniffed not in SUPPORTED_TYPES:  # pragma: no cover -- defensive
            raise UnsupportedDocument(
                f"{filename!r} is {sniffed}, which is not supported",
                filename=filename,
            )

        return sniffed

    def _record(
        self,
        correlation_id: CorrelationId,
        *,
        action: str,
        result: str,
        detail: dict[str, object],
    ) -> None:
        self._audit.append(
            AuditEvent(
                event_id=f"evt_{uuid7()}",
                correlation_id=correlation_id,
                stage=PipelineStage.INGESTION,
                action=action,
                actor_id="sentinel.ingestion",
                actor_role=ActorRole.SYSTEM,
                result=result,
                occurred_at=dt.datetime.now(dt.UTC),
                detail=detail,
            )
        )
