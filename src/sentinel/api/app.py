"""The HTTP surface.

Every endpoint reads as *parse request → call service → serialize result*. Anything that
calculates, evaluates a policy, or authorizes belongs in a classified service, not here
(ADR-0008).

Error mapping deserves a note. An :class:`~sentinel.core.errors.IngestionError` becomes
**422**, not 500: the request was well-formed, the *document* was not, and the caller can act
on that. A 500 would tell an integrator to retry, which for a corrupt PDF means retrying
forever. Every error response carries the correlation ID, so the caller can quote it and the
dead-letter record can be found.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from sentinel.api.dependencies import SessionDep, get_ingestion_service
from sentinel.core.enums import DocumentSource
from sentinel.core.errors import IngestionError, SentinelError, StorageError
from sentinel.core.ids import CorrelationId
from sentinel.core.settings import get_settings
from sentinel.db.repositories import AuditRepository, InvoiceRepository
from sentinel.ingestion.service import IngestionService

__all__ = ["create_app"]


# -- response models ---------------------------------------------------------------------


class IngestResponse(BaseModel):
    """What a caller gets back from a successful upload."""

    invoice_id: str
    correlation_id: str = Field(
        description="Quote this in any follow-up. It threads the whole pipeline (spec §12)."
    )
    document_hash: str
    status: str
    was_already_stored: bool = Field(
        description=(
            "Whether these exact bytes were already held. A storage fact, not a duplicate "
            "verdict -- duplicate detection assesses that with evidence (spec §6)."
        )
    )


class InvoiceResponse(BaseModel):
    invoice_id: str
    correlation_id: str
    status: str
    filename: str
    document_hash: str
    invoice_number: str | None
    po_number: str | None
    vendor_id: str | None


class AuditEventResponse(BaseModel):
    event_id: str
    stage: str
    action: str
    actor_id: str
    actor_role: str
    result: str
    occurred_at: str
    detail: dict[str, Any]


class HealthResponse(BaseModel):
    status: str
    environment: str
    extraction_provider: str


# -- application -------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Build the application. A factory so tests can construct isolated instances."""
    app = FastAPI(
        title="Sentinel",
        description="Autonomous Accounts Payable & Invoice Exception Handler",
        version="0.1.0",
    )

    @app.exception_handler(IngestionError)
    async def _ingestion_failed(_: Request, exc: IngestionError) -> JSONResponse:
        """422, not 500: the request was fine, the document was not.

        A 500 would tell an integrator to retry, and a corrupt PDF is still corrupt on the
        second attempt.
        """
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": type(exc).__name__,
                "message": exc.message,
                "correlation_id": exc.correlation_id,
                "retryable": exc.retryable,
                "detail": (
                    "The document was recorded in the dead-letter log with this reason. "
                    "Correct it and resubmit."
                ),
            },
        )

    @app.exception_handler(StorageError)
    async def _storage_failed(_: Request, exc: StorageError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": type(exc).__name__,
                "message": exc.message,
                "correlation_id": exc.correlation_id,
                "retryable": exc.retryable,
            },
        )

    @app.exception_handler(SentinelError)
    async def _sentinel_failed(_: Request, exc: SentinelError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": type(exc).__name__,
                "message": exc.message,
                "correlation_id": exc.correlation_id,
                "retryable": exc.retryable,
            },
        )

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health() -> HealthResponse:
        settings = get_settings()
        return HealthResponse(
            status="ok",
            environment=settings.env,
            extraction_provider=settings.extraction_provider,
        )

    @app.post(
        "/invoices",
        response_model=IngestResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["ingestion"],
        summary="Submit an invoice document",
    )
    def ingest_invoice(
        service: Annotated[IngestionService, Depends(get_ingestion_service)],
        file: Annotated[UploadFile, File(description="The invoice PDF or image")],
        source: Annotated[DocumentSource, Form()] = DocumentSource.UPLOAD,
    ) -> IngestResponse:
        """Admit a document to the pipeline. Spec §4.1.

        The file's declared content type is ignored in favour of its actual leading bytes.
        Unsupported or corrupt documents are dead-lettered with a reason and answered 422.
        """
        if source in (DocumentSource.EMAIL, DocumentSource.BATCH):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"the {source} source is not implemented in v1 (ADR-0006)",
            )

        result = service.ingest(
            file.file.read(),
            filename=file.filename or "unnamed",
            declared_content_type=file.content_type,
            source=source,
        )
        return IngestResponse(
            invoice_id=str(result.invoice_id),
            correlation_id=str(result.document.correlation_id),
            document_hash=str(result.document.document_hash),
            status="received",
            was_already_stored=result.was_already_stored,
        )

    @app.get(
        "/invoices/{correlation_id}",
        response_model=InvoiceResponse,
        tags=["ingestion"],
    )
    def get_invoice(correlation_id: str, session: SessionDep) -> InvoiceResponse:
        row = InvoiceRepository(session).by_correlation_id(_parse_correlation(correlation_id))
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no invoice for {correlation_id}")

        return InvoiceResponse(
            invoice_id=row.invoice_id,
            correlation_id=row.correlation_id,
            status=row.status,
            filename=row.filename,
            document_hash=row.document_hash,
            invoice_number=row.invoice_number,
            po_number=row.po_number,
            vendor_id=row.vendor_id,
        )

    @app.get(
        "/invoices/{correlation_id}/audit",
        response_model=list[AuditEventResponse],
        tags=["audit"],
        summary="The full audit trail for one invoice",
    )
    def get_audit_trail(correlation_id: str, session: SessionDep) -> list[AuditEventResponse]:
        """Every recorded event, oldest first. Spec §12, DoD-5.

        This is the endpoint that answers "why did this happen?" -- a completed invoice must
        be reconstructable from these events alone.
        """
        events = AuditRepository(session).trail_for(_parse_correlation(correlation_id))
        return [
            AuditEventResponse(
                event_id=event.event_id,
                stage=event.stage,
                action=event.action,
                actor_id=event.actor_id,
                actor_role=event.actor_role,
                result=event.result,
                occurred_at=event.occurred_at.isoformat(),
                detail=event.detail,
            )
            for event in events
        ]

    return app


def _parse_correlation(raw: str) -> CorrelationId:
    """Validate a path parameter, answering 400 rather than leaking a ValueError as a 500."""
    try:
        return CorrelationId.parse(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


app = create_app()
