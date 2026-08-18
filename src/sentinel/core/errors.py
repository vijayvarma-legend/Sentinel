"""The error taxonomy.

The distinction this module enforces, and the reason it exists:

**A failed invoice is not a failed program.** An invoice that misses its PO price by 5% has
worked exactly as designed -- it produced a discrepancy, which is a *business outcome*
carried on the pipeline state and routed by policy. Raising an exception for it would
conflate "this invoice needs a human" with "this service is broken", and the two need
opposite responses: one goes to an AP manager's queue, the other pages an engineer.

So: exceptions here describe **system faults and refusals to proceed** -- malformed input,
an unreachable ERP, a blocked execution. Discrepancies, risk scores, and policy routes are
values, not exceptions.

Every error carries a correlation ID where one exists, because spec §12 requires the trail
to be reconstructable and an untraceable stack trace in a financial system is nearly
useless.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AuthorizationError",
    "ConfigurationError",
    "DeadLetter",
    "DocumentTooLarge",
    "ErpExecutionError",
    "ErpTimeout",
    "ExecutionBlocked",
    "ExtractionRejected",
    "IdempotencyConflict",
    "IngestionError",
    "PolicyConfigurationError",
    "SentinelError",
    "StorageError",
    "UnsupportedDocument",
]


class SentinelError(Exception):
    """Base for every Sentinel fault.

    Carries a correlation ID and arbitrary structured context so that failures land in the
    audit trail as evidence rather than as prose.
    """

    #: Whether the pipeline may retry this operation unchanged. Transport faults are
    #: retryable; a corrupt PDF or a rejected authorization will fail identically forever.
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        correlation_id: str | None = None,
        **context: Any,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.correlation_id = correlation_id
        self.context = context

    def __str__(self) -> str:
        parts = [self.message]
        if self.correlation_id:
            parts.append(f"correlation_id={self.correlation_id}")
        parts.extend(f"{key}={value!r}" for key, value in sorted(self.context.items()))
        return " | ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        """Structured form, for audit events and log records."""
        return {
            "error": type(self).__name__,
            "message": self.message,
            "correlation_id": self.correlation_id,
            "retryable": self.retryable,
            **self.context,
        }


# ---------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------


class ConfigurationError(SentinelError):
    """The system is misconfigured. Fail at startup, never limp along in production."""


# ---------------------------------------------------------------------------------------
# Ingestion (SVC-10) -- spec §4.1
# ---------------------------------------------------------------------------------------


class IngestionError(SentinelError):
    """A document could not be admitted to the pipeline.

    Spec §4.1 routes these to a dead-letter path: the document is preserved with its reason
    so it can be inspected and resubmitted, but no pipeline state is created for it.
    """


class UnsupportedDocument(IngestionError):
    """The file is not a document type Sentinel can process."""


class DocumentTooLarge(IngestionError):
    """The file exceeds the configured size ceiling.

    A limit, rather than best-effort processing, because an unbounded upload is a denial of
    service on the vision extraction budget as much as on memory.
    """


class DeadLetter(IngestionError):
    """A document was quarantined. Terminal for this attempt; resubmission is a new attempt."""


# ---------------------------------------------------------------------------------------
# Extraction (SVC-20) -- spec §4.2
# ---------------------------------------------------------------------------------------


class ExtractionRejected(SentinelError):
    """Extraction produced nothing trustworthy enough to pass downstream.

    Spec §4.2 is explicit that malformed or low-confidence payloads are *rejected* rather
    than silently passed on. A half-read invoice that flows into validation produces
    confident arithmetic over fabricated inputs, which is worse than no answer at all.
    """

    def __init__(
        self,
        message: str,
        *,
        correlation_id: str | None = None,
        low_confidence_fields: list[str] | None = None,
        **context: Any,
    ) -> None:
        super().__init__(
            message,
            correlation_id=correlation_id,
            low_confidence_fields=low_confidence_fields or [],
            **context,
        )


# ---------------------------------------------------------------------------------------
# Storage (SVC-02)
# ---------------------------------------------------------------------------------------


class StorageError(SentinelError):
    """The document store could not be read or written."""

    retryable = True


# ---------------------------------------------------------------------------------------
# Governance and execution (SVC-04, SVC-70, SVC-90) -- spec §§9, 11, 12
# ---------------------------------------------------------------------------------------


class AuthorizationError(SentinelError):
    """The actor may not perform this action.

    Spec §11 separates recommendation from authorization: an agent may propose an ERP
    posting, but only policy grants permission to execute it. This is that refusal.
    """


class PolicyConfigurationError(SentinelError):
    """The active policy set is missing, malformed, or unversioned.

    Not a routing decision -- a refusal to decide at all. Spec §9 requires every decision to
    record the policy version that produced it, so a decision made against an unidentifiable
    policy set could never be reconstructed, and must not be made.
    """


class ExecutionBlocked(SentinelError):
    """A safety or idempotency check failed, so execution was stopped.

    Spec §9: *any failed safety/idempotency check → block execution*. Unconditional, and
    deliberately not retryable -- retrying a blocked payment is the failure mode the block
    exists to prevent.
    """


class IdempotencyConflict(ExecutionBlocked):
    """This idempotency key has already been used for a different action.

    Either a key-derivation bug or two genuinely distinct actions collapsing to one key.
    Both are serious: the first risks a missed payment, the second a double payment.
    """


class ErpExecutionError(SentinelError):
    """The ERP rejected the transaction. Definite outcome -- the posting did not happen."""


class ErpTimeout(ErpExecutionError):
    """The ERP did not answer in time, so the outcome is *unknown*.

    The dangerous case, and why it is a distinct type: the posting may have succeeded. A
    blind retry can double-pay. Spec §11 routes this to reconciliation -- confirm the actual
    ERP state, then act -- rather than to the retry loop.
    """

    retryable = False
