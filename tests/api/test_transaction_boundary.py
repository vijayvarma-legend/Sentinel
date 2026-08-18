"""The dead-letter record must survive the request that produced it.

Regression test for a bug found by running the real application rather than the test suite:
ingestion wrote the dead-letter row and its audit event, then raised; the request-scoped
session saw an exception, rolled back, and erased both. The API answered 422 with a
correlation ID pointing at a record that no longer existed.

The other API tests could not catch it -- they override `get_session` with a fixture that
does not roll back on exception, so the very behaviour at fault was substituted away. These
tests use the **real** session dependency and read the result back on a separate connection.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from sentinel.api.app import create_app
from sentinel.api.dependencies import get_document_store
from sentinel.storage.memory import InMemoryDocumentStore

pytestmark = pytest.mark.integration

BAD_DOCUMENT = b"PK\x03\x04 this is really a docx"


@pytest.fixture
def live_client(raw_connection: Engine) -> Iterator[TestClient]:
    """A client using the real session dependency, so transactions behave as in production."""
    app = create_app()
    app.dependency_overrides[get_document_store] = lambda: InMemoryDocumentStore()

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()


class TestDeadLetterDurability:
    """Spec §4.1: unsupported documents are routed to a dead-letter path.

    A path that discards the record is not a dead-letter path.
    """

    def test_the_dead_letter_row_persists_after_the_422(
        self, live_client: TestClient, raw_connection: Engine
    ) -> None:
        response = live_client.post(
            "/invoices", files={"file": ("invoice.docx", BAD_DOCUMENT, "application/pdf")}
        )
        assert response.status_code == 422
        correlation_id = response.json()["correlation_id"]

        with raw_connection.connect() as connection:
            stored = connection.execute(
                text("SELECT reason, filename FROM dead_letters WHERE correlation_id = :cid"),
                {"cid": correlation_id},
            ).one_or_none()

        assert stored is not None, (
            "the dead-letter record was rolled back with the failing request -- the "
            "correlation ID in the 422 response points at nothing"
        )
        assert stored.reason == "UnsupportedDocument"
        assert stored.filename == "invoice.docx"

    def test_the_rejection_audit_event_persists_too(
        self, live_client: TestClient, raw_connection: Engine
    ) -> None:
        """Spec §12 audits every material decision, and a rejection is one."""
        correlation_id = live_client.post(
            "/invoices", files={"file": ("invoice.docx", BAD_DOCUMENT, "application/pdf")}
        ).json()["correlation_id"]

        with raw_connection.connect() as connection:
            result = connection.execute(
                text("SELECT result FROM audit_events WHERE correlation_id = :cid"),
                {"cid": correlation_id},
            ).one_or_none()

        assert result is not None, "the rejection left no audit trail"
        assert result.result.startswith("dead_lettered")

    def test_the_audit_endpoint_can_find_it_afterwards(self, live_client: TestClient) -> None:
        """The end-to-end promise: quote the ID from the 422 and see what happened."""
        correlation_id = live_client.post(
            "/invoices", files={"file": ("invoice.docx", BAD_DOCUMENT, "application/pdf")}
        ).json()["correlation_id"]

        events = live_client.get(f"/invoices/{correlation_id}/audit").json()

        assert len(events) == 1
        assert events[0]["result"].startswith("dead_lettered")

    def test_a_rejected_document_still_creates_no_invoice(
        self, live_client: TestClient, raw_connection: Engine
    ) -> None:
        """Committing the dead-letter must not also commit a half-built pipeline record."""
        live_client.post(
            "/invoices", files={"file": ("invoice.docx", BAD_DOCUMENT, "application/pdf")}
        )

        with raw_connection.connect() as connection:
            count = connection.execute(text("SELECT count(*) FROM invoices")).scalar_one()

        assert count == 0


class TestGenuineFaultsStillRollBack:
    """The commit-on-IngestionError rule must not become commit-on-anything."""

    def test_an_unexpected_error_rolls_the_request_back(self, raw_connection: Engine) -> None:
        class ExplodingStore(InMemoryDocumentStore):
            def put(self, data: bytes, *, content_type: str):
                raise RuntimeError("object store is on fire")

        app = create_app()
        app.dependency_overrides[get_document_store] = ExplodingStore

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/invoices", files={"file": ("INV.pdf", b"%PDF-1.7 ok", "application/pdf")}
            )
        app.dependency_overrides.clear()

        assert response.status_code == 500

        with raw_connection.connect() as connection:
            invoices = connection.execute(text("SELECT count(*) FROM invoices")).scalar_one()
            events = connection.execute(text("SELECT count(*) FROM audit_events")).scalar_one()

        assert invoices == 0
        assert events == 0, "a genuine fault must leave no partial record behind"
