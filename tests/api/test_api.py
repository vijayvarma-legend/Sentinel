"""HTTP surface behaviour.

The app is built with its session and store dependencies overridden, so these exercise real
routing, real validation, and real error mapping against a rolled-back transaction.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from sentinel.api.app import create_app
from sentinel.api.dependencies import get_document_store, get_session
from sentinel.storage.memory import InMemoryDocumentStore

pytestmark = pytest.mark.integration

PDF = b"%PDF-1.7\nfake invoice INV-8821\n%%EOF"


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    app = create_app()
    store = InMemoryDocumentStore()

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_document_store] = lambda: store

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


class TestHealth:
    def test_reports_readiness_and_configuration(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_names_the_extraction_provider(self, client: TestClient) -> None:
        """Worth surfacing: the fixture provider returning canned data must be visible."""
        assert "extraction_provider" in client.get("/health").json()


class TestIngestEndpoint:
    def upload(self, client: TestClient, data: bytes, name: str = "INV-8821.pdf"):
        return client.post("/invoices", files={"file": (name, data, "application/pdf")})

    def test_a_valid_document_is_accepted(self, client: TestClient) -> None:
        response = self.upload(client, PDF)

        assert response.status_code == 201
        body = response.json()
        assert body["invoice_id"].startswith("inv_")
        assert body["correlation_id"].startswith("cor_")
        assert len(body["document_hash"]) == 64
        assert body["status"] == "received"
        assert body["was_already_stored"] is False

    def test_a_resubmission_reports_prior_storage(self, client: TestClient) -> None:
        self.upload(client, PDF)
        second = self.upload(client, PDF, name="INV-8821-again.pdf")

        assert second.status_code == 201
        assert second.json()["was_already_stored"] is True

    def test_an_unreadable_document_is_422_not_500(self, client: TestClient) -> None:
        """The request was well-formed; the document was not.

        A 500 would tell an integrator to retry, and a corrupt PDF is still corrupt on the
        second attempt.
        """
        response = self.upload(client, b"PK\x03\x04 a docx", name="invoice.docx")

        assert response.status_code == 422
        body = response.json()
        assert body["error"] == "UnsupportedDocument"
        assert body["retryable"] is False
        assert "dead-letter" in body["detail"]

    def test_an_empty_document_is_refused(self, client: TestClient) -> None:
        assert self.upload(client, b"").status_code == 422

    def test_a_missing_file_is_a_validation_error(self, client: TestClient) -> None:
        assert client.post("/invoices").status_code == 422

    def test_a_deferred_source_is_refused_explicitly(self, client: TestClient) -> None:
        """Email ingestion is out of v1 (ADR-0006), and says so rather than failing oddly."""
        response = client.post(
            "/invoices",
            files={"file": ("INV.pdf", PDF, "application/pdf")},
            data={"source": "email"},
        )
        assert response.status_code == 400
        assert "not implemented in v1" in response.json()["detail"]


class TestInvoiceLookup:
    def test_an_ingested_invoice_can_be_retrieved(self, client: TestClient) -> None:
        correlation_id = client.post(
            "/invoices", files={"file": ("INV-8821.pdf", PDF, "application/pdf")}
        ).json()["correlation_id"]

        response = client.get(f"/invoices/{correlation_id}")

        assert response.status_code == 200
        assert response.json()["correlation_id"] == correlation_id
        assert response.json()["status"] == "received"

    def test_an_unknown_correlation_id_is_404(self, client: TestClient) -> None:
        from sentinel.core.ids import CorrelationId

        response = client.get(f"/invoices/{CorrelationId.new()}")
        assert response.status_code == 404

    def test_a_malformed_correlation_id_is_400_not_500(self, client: TestClient) -> None:
        """A bad path parameter is the caller's error, and must not leak a stack trace."""
        response = client.get("/invoices/not-an-id")
        assert response.status_code == 400
        assert "must start with" in response.json()["detail"]


class TestAuditEndpoint:
    def test_the_trail_is_readable_for_an_ingested_invoice(self, client: TestClient) -> None:
        """DoD-5: a completed invoice must be reconstructable from its events alone."""
        correlation_id = client.post(
            "/invoices", files={"file": ("INV-8821.pdf", PDF, "application/pdf")}
        ).json()["correlation_id"]

        response = client.get(f"/invoices/{correlation_id}/audit")

        assert response.status_code == 200
        events = response.json()
        assert len(events) == 1
        assert events[0]["stage"] == "ingestion"
        assert events[0]["action"] == "ingest"
        assert events[0]["result"] == "accepted"
        assert events[0]["actor_role"] == "system"

    def test_a_rejected_document_still_leaves_a_trail(self, client: TestClient) -> None:
        """The dead-letter is itself a decision, and spec section 12 audits every one."""
        rejection = client.post(
            "/invoices", files={"file": ("bad.docx", b"PK\x03\x04", "application/pdf")}
        )
        correlation_id = rejection.json()["correlation_id"]
        assert correlation_id, "a rejection must still report an identifier to quote"

        events = client.get(f"/invoices/{correlation_id}/audit").json()
        assert len(events) == 1
        assert events[0]["result"].startswith("dead_lettered")


class TestOpenApi:
    def test_the_schema_is_generated(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        assert "/invoices" in schema["paths"]
        assert "/invoices/{correlation_id}/audit" in schema["paths"]
