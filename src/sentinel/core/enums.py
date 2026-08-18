"""The closed vocabularies the pipeline speaks.

Every one of these is a ``StrEnum``, so it serializes as a readable string in JSON, in the
audit log, and in a Postgres column -- an audit trail full of ``2`` and ``5`` is not one a
finance team can read, and spec §18 requires that every financial action be reproducible
from that trail by a human.

These are closed sets on purpose. An exception category the policy engine has never heard of
cannot be routed, so adding a member here is a deliberate act with a matching policy change,
not something an LLM can invent at runtime.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ActorRole",
    "CheckStatus",
    "DocumentSource",
    "DuplicateTier",
    "ExceptionCategory",
    "InvoiceStatus",
    "PipelineStage",
    "PolicyRoute",
    "RecommendedAction",
    "RiskSignal",
    "TrustClass",
]


class TrustClass(StrEnum):
    """Whether a module's output can move money on its own. See ADR-0004."""

    DETERMINISTIC = "deterministic"
    """Pure code or SQL. Reproducible, and authoritative for financial facts."""

    LLM = "llm"
    """Model-driven. Advisory only -- evidence or a recommendation, never an authorization."""

    CONTROL = "control"
    """Enforces a gate: policy, authorization, idempotency. Must not be bypassable."""

    HUMAN = "human"
    """An authenticated person's decision."""

    OBSERVABILITY = "observability"
    """Records or measures. No effect on the decision path."""

    COMPOSITION = "composition"
    """The wiring layer. May depend on everything; decides nothing. See ADR-0008."""


class PipelineStage(StrEnum):
    """The stages of the procure-to-pay pipeline, in execution order.

    Order per ADR-0005: risk scoring runs *before* exception reasoning, so the agent has the
    risk evidence it is expected to explain.
    """

    INGESTION = "ingestion"
    EXTRACTION = "extraction"
    VALIDATION = "validation"
    DUPLICATE_DETECTION = "duplicate_detection"
    RISK_SCORING = "risk_scoring"
    EXCEPTION_REASONING = "exception_reasoning"
    POLICY = "policy"
    HUMAN_REVIEW = "human_review"
    ERP_EXECUTION = "erp_execution"


class DocumentSource(StrEnum):
    """How an invoice arrived. v1 implements API and UPLOAD only (ADR-0006)."""

    API = "api"
    UPLOAD = "upload"
    EMAIL = "email"
    """Deferred past v1; the enum member exists so stored records stay forward-compatible."""

    BATCH = "batch"
    """Deferred past v1."""


class InvoiceStatus(StrEnum):
    """Where an invoice currently sits.

    A status is a fact about the workflow, not a judgement about the invoice -- the reasons
    behind it live in the validation scorecard, the risk assessment, and the audit trail.
    """

    RECEIVED = "received"
    EXTRACTED = "extracted"
    VALIDATED = "validated"
    PENDING_REVIEW = "pending_review"
    """Waiting on a human. Spec §10's exception queue is exactly this set."""

    APPROVED = "approved"
    REJECTED = "rejected"
    POSTED = "posted"
    """Committed to the ERP. Terminal, and the only status that means money moved."""

    BLOCKED = "blocked"
    """Stopped by a safety or idempotency check. Spec §9. Requires intervention."""

    DEAD_LETTERED = "dead_lettered"
    """Never entered the pipeline -- unsupported, corrupt, or oversized. Spec §4.1."""


class CheckStatus(StrEnum):
    """The outcome of one deterministic validation check."""

    PASS = "pass"  # noqa: S105 -- a check outcome, not a credential
    WITHIN_TOLERANCE = "within_tolerance"
    """Varied from the agreed figure, but inside the configured tolerance band.

    Deliberately distinct from PASS: an invoice that passed exactly and one that passed
    because a tolerance absorbed a 1.9% overcharge are different facts, and collapsing them
    would hide the cumulative cost of a tolerance that is set too loose.
    """

    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    """The check could not apply -- no contract on file, no tax line. Not a pass."""


class ExceptionCategory(StrEnum):
    """How a discrepancy is classified. Spec §8."""

    PRICE = "price"
    QUANTITY = "quantity"
    TAX = "tax"
    SHIPPING = "shipping"
    DUPLICATE = "duplicate"
    CONTRACT = "contract"
    VENDOR = "vendor"
    OTHER = "other"


class RecommendedAction(StrEnum):
    """What the reasoning agent proposes. Spec §8.

    A recommendation only. The policy engine decides whether it may be taken, and by whom.
    """

    APPROVE = "approve"
    REQUEST_CORRECTED_INVOICE = "request_corrected_invoice"
    REQUEST_CREDIT_NOTE = "request_credit_note"
    ESCALATE = "escalate"


class PolicyRoute(StrEnum):
    """Where the policy engine sends an invoice. Spec §9."""

    AUTO_PROCESS = "auto_process"
    """Risk below threshold and every validation passed. Straight-through."""

    REVIEW = "review"
    """Moderate risk or controlled variance. Conditional action."""

    MANDATORY_HITL = "mandatory_hitl"
    """High risk or a sensitive condition. A human must decide."""

    BLOCK = "block"
    """A safety or idempotency check failed. Execution stops, unconditionally."""


class DuplicateTier(StrEnum):
    """How a suspected duplicate was matched. Spec §6.

    Ordered loosely by confidence. Nothing below EXACT_HASH may auto-reject -- spec §6 routes
    uncertain duplicates to a human rather than discarding a possibly-legitimate invoice.
    """

    NONE = "none"
    EXACT_HASH = "exact_hash"
    """Byte-identical to a document already processed. The only unambiguous tier."""

    EXACT_NUMBER = "exact_number"
    """Same invoice number from the same vendor."""

    FUZZY_NUMBER = "fuzzy_number"
    """Invoice numbers match after normalization -- INV-8821 against INV8821."""

    SIMILAR = "similar"
    """Close on vendor, amount, PO, date, and line items without matching identifiers."""


class RiskSignal(StrEnum):
    """The named contributors to a risk score. Spec §7.

    A score must always decompose into these; an unexplainable score is a bug, not a
    judgement call.
    """

    VENDOR_ANOMALY = "vendor_anomaly"
    PRICE_ANOMALY = "price_anomaly"
    PAYMENT_CHANGE = "payment_change"
    """Bank details changed shortly before a large invoice. The classic redirection fraud."""

    DUPLICATE = "duplicate"
    NEW_VENDOR = "new_vendor"
    BEHAVIORAL_ANOMALY = "behavioral_anomaly"


class ActorRole(StrEnum):
    """RBAC roles. Spec §12."""

    AP_OPERATOR = "ap_operator"
    AP_MANAGER = "ap_manager"
    FINANCE_ADMIN = "finance_admin"
    SYSTEM = "system"
    """Sentinel acting on its own behalf. Never used to authorize a human-only decision."""
