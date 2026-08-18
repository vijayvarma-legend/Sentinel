"""The taxonomy's shape matters as much as its behaviour, so both are tested."""

from __future__ import annotations

import pytest

from sentinel.core.errors import (
    AuthorizationError,
    DeadLetter,
    DocumentTooLarge,
    ErpExecutionError,
    ErpTimeout,
    ExecutionBlocked,
    ExtractionRejected,
    IdempotencyConflict,
    IngestionError,
    SentinelError,
    StorageError,
    UnsupportedDocument,
)


class TestBaseBehaviour:
    def test_carries_correlation_id_and_context(self) -> None:
        error = SentinelError(
            "something broke", correlation_id="cor_abc", invoice_number="INV-8821"
        )
        assert error.correlation_id == "cor_abc"
        assert error.context["invoice_number"] == "INV-8821"

    def test_str_includes_correlation_and_context(self) -> None:
        rendered = str(SentinelError("broke", correlation_id="cor_abc", vendor="TechCorp"))
        assert "broke" in rendered
        assert "correlation_id=cor_abc" in rendered
        assert "vendor='TechCorp'" in rendered

    def test_str_is_bare_when_there_is_no_context(self) -> None:
        assert str(SentinelError("broke")) == "broke"

    def test_as_dict_is_audit_ready(self) -> None:
        payload = ErpTimeout("no answer", correlation_id="cor_1", erp="mock").as_dict()
        assert payload == {
            "error": "ErpTimeout",
            "message": "no answer",
            "correlation_id": "cor_1",
            "retryable": False,
            "erp": "mock",
        }

    def test_is_catchable_as_a_plain_exception(self) -> None:
        with pytest.raises(Exception, match="broke"):
            raise SentinelError("broke")


class TestHierarchy:
    """Catching a category must catch its members -- these relationships are load-bearing."""

    @pytest.mark.parametrize("specific", [UnsupportedDocument, DocumentTooLarge, DeadLetter])
    def test_ingestion_failures_share_a_base(self, specific: type[SentinelError]) -> None:
        assert issubclass(specific, IngestionError)
        with pytest.raises(IngestionError):
            raise specific("nope")

    def test_idempotency_conflict_is_a_blocked_execution(self) -> None:
        """Anything guarding execution must catch a key conflict without naming it."""
        assert issubclass(IdempotencyConflict, ExecutionBlocked)
        with pytest.raises(ExecutionBlocked):
            raise IdempotencyConflict("key reused")

    def test_erp_timeout_is_an_erp_execution_error(self) -> None:
        assert issubclass(ErpTimeout, ErpExecutionError)

    def test_everything_descends_from_sentinel_error(self) -> None:
        for error_type in (
            AuthorizationError,
            ErpTimeout,
            ExtractionRejected,
            ExecutionBlocked,
            IngestionError,
            StorageError,
        ):
            assert issubclass(error_type, SentinelError)


class TestRetryability:
    def test_transport_faults_are_retryable(self) -> None:
        assert StorageError("s3 unreachable").retryable is True

    def test_blocked_execution_is_not_retryable(self) -> None:
        """Retrying a blocked payment is precisely what the block prevents."""
        assert ExecutionBlocked("idempotency check failed").retryable is False
        assert IdempotencyConflict("key reused").retryable is False

    def test_erp_timeout_is_not_retryable_despite_being_transport_shaped(self) -> None:
        """The subtle one.

        A timeout looks like a transient network fault, so the reflex is to retry it. But
        the posting may have succeeded -- the response was lost, not the request. Retrying
        can double-pay. Spec section 11 sends this to reconciliation instead.
        """
        assert ErpTimeout("no answer in 30s").retryable is False

    def test_bad_input_is_not_retryable(self) -> None:
        assert UnsupportedDocument("it is a .docx").retryable is False


class TestExtractionRejected:
    def test_names_the_fields_that_failed_confidence(self) -> None:
        error = ExtractionRejected(
            "confidence below threshold",
            correlation_id="cor_1",
            low_confidence_fields=["total_due", "billed_unit_price"],
        )
        assert error.context["low_confidence_fields"] == ["total_due", "billed_unit_price"]
        assert "total_due" in str(error)

    def test_defaults_to_an_empty_field_list(self) -> None:
        assert ExtractionRejected("malformed payload").context["low_confidence_fields"] == []
