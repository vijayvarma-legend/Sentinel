"""The contracts that move between services.

Every arrow in the reference architecture (spec §14) is one of these types. They are the
seams that make the modular monolith real (ADR-0003): a module produces one of these and
hands it on, rather than reaching into the next module's internals.

All of them are frozen. A stage that needs to change something builds a new object, so the
audit trail never has to explain how a record came to differ from what was recorded.

Three of these encode a safety property that would otherwise depend on discipline:

* :class:`ExtractedField` makes per-field confidence impossible to drop, because the value
  and its confidence are the same object.
* :class:`RiskAssessment` refuses to hold an overall score that its own named signals cannot
  account for -- spec §7's "the score should be explainable through the contributing signals"
  as a constructor check.
* :class:`ExceptionFinding` refuses to hold a claim with no evidence behind it -- spec §8's
  "it should not invent financial facts" made structural.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from sentinel.core.business import (
    GoodsReceiptNote,
    PurchaseOrder,
    VendorContract,
    VendorProfile,
)
from sentinel.core.enums import (
    ActorRole,
    CheckStatus,
    DocumentSource,
    DuplicateTier,
    ExceptionCategory,
    PipelineStage,
    PolicyRoute,
    RecommendedAction,
    RiskSignal,
)
from sentinel.core.ids import (
    CorrelationId,
    DocumentHash,
    IdempotencyKey,
    InvoiceId,
    PolicyVersionId,
    VendorId,
)
from sentinel.core.money import Money

__all__ = [
    "AuditEvent",
    "CheckResult",
    "DuplicateAssessment",
    "DuplicateMatch",
    "ErpTransactionResult",
    "EvidenceBundle",
    "ExceptionAnalysis",
    "ExceptionFinding",
    "ExtractedField",
    "ExtractedInvoice",
    "ExtractedLine",
    "HumanDecision",
    "IngestedDocument",
    "PolicyDecision",
    "RiskAssessment",
    "RiskContribution",
    "ValidationScorecard",
]

Score = Decimal
"""A probability-like value in [0, 1]. Decimal, so a threshold comparison is exact."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------------------
# SVC-10 Ingestion
# ---------------------------------------------------------------------------------------


class IngestedDocument(_Frozen):
    """A document admitted to the pipeline. Spec §4.1.

    The correlation ID minted here travels with this invoice through every stage, store,
    and external call, and appears on every audit event (spec §12).
    """

    correlation_id: CorrelationId
    document_hash: DocumentHash
    storage_uri: str = Field(min_length=1)
    """Where the original bytes live. Content-addressed and never overwritten."""

    filename: str
    content_type: str
    size_bytes: int = Field(gt=0)
    page_count: int | None = Field(default=None, gt=0)
    source: DocumentSource
    received_at: dt.datetime

    @model_validator(mode="after")
    def _received_at_is_tz_aware(self) -> Self:
        if self.received_at.tzinfo is None:
            raise ValueError(
                "received_at must be timezone-aware -- a naive timestamp in an audit trail "
                "cannot be ordered against events from another region"
            )
        return self


# ---------------------------------------------------------------------------------------
# SVC-20 Extraction (advisory)
# ---------------------------------------------------------------------------------------


class ExtractedField[T](_Frozen):
    """One field a model read off a document, with how sure it was and where it saw it.

    Value and confidence are one object by design. The alternative -- a payload plus a
    parallel confidence map -- lets a caller read the value and forget the confidence, which
    is precisely the failure spec §4.2 guards against when it requires low-confidence
    payloads to be rejected rather than silently passed downstream.
    """

    value: T
    confidence: Score = Field(ge=0, le=1)
    page: int | None = Field(default=None, gt=0)
    source_text: str | None = None
    """The raw text the model read, kept so a human reviewer can check it against the page."""

    def is_confident(self, threshold: Score) -> bool:
        return self.confidence >= threshold


class ExtractedLine(_Frozen):
    """One invoice line as read from the document. Nothing here is computed."""

    item_id: ExtractedField[str]
    description: ExtractedField[str] | None = None
    billed_qty: ExtractedField[Decimal]
    billed_unit_price: ExtractedField[Money]
    line_total: ExtractedField[Money] | None = None
    """What the document *printed* as the line total, if it printed one.

    Never computed here. Spec §4.3 keeps arithmetic in the validation engine; this field
    exists so the engine can compare its own multiplication against the supplier's.
    """


class ExtractedInvoice(_Frozen):
    """The structured reading of an invoice document. Spec §4.2.

    **Advisory.** Every number here is a model's reading of a picture. Nothing downstream
    treats it as a financial fact until :class:`ValidationScorecard` has recomputed it.
    """

    correlation_id: CorrelationId
    document_hash: DocumentHash

    supplier_name: ExtractedField[str]
    invoice_number: ExtractedField[str]
    po_reference: ExtractedField[str] | None = None
    invoice_date: ExtractedField[dt.date] | None = None
    currency: ExtractedField[str]
    lines: tuple[ExtractedLine, ...] = Field(min_length=1)

    subtotal: ExtractedField[Money] | None = None
    tax: ExtractedField[Money] | None = None
    shipping: ExtractedField[Money] | None = None
    total_due: ExtractedField[Money]

    extracted_at: dt.datetime
    model_id: str = Field(min_length=1)
    """Which model produced this. Spec §12 requires it in the audit trail so a decision can
    be reconstructed after the model has been changed."""

    prompt_version: str = Field(min_length=1)

    def low_confidence_fields(self, threshold: Score) -> tuple[str, ...]:
        """Names of every field below `threshold`, for the rejection message.

        Line fields are reported as ``lines[0].billed_unit_price`` so a reviewer can go
        straight to the disputed number.
        """
        weak: list[str] = []

        for name in (
            "supplier_name",
            "invoice_number",
            "po_reference",
            "invoice_date",
            "currency",
            "subtotal",
            "tax",
            "shipping",
            "total_due",
        ):
            field = getattr(self, name)
            if field is not None and not field.is_confident(threshold):
                weak.append(name)

        for index, line in enumerate(self.lines):
            for name in ("item_id", "description", "billed_qty", "billed_unit_price", "line_total"):
                field = getattr(line, name)
                if field is not None and not field.is_confident(threshold):
                    weak.append(f"lines[{index}].{name}")

        return tuple(weak)


# ---------------------------------------------------------------------------------------
# SVC-30 Validation (authoritative)
# ---------------------------------------------------------------------------------------


class CheckResult(_Frozen):
    """One deterministic check, its verdict, and the numbers behind it.

    ``expected`` and ``actual`` are mandatory context on a failure: a scorecard that says
    "price check failed" without saying $1,050 against $1,000 forces a human to redo the
    work the engine already did.
    """

    check: str = Field(min_length=1)
    status: CheckStatus
    item_id: str | None = None
    expected: str | None = None
    actual: str | None = None
    variance_pct: Decimal | None = None
    tolerance_pct: Decimal | None = None
    message: str = ""

    @model_validator(mode="after")
    def _failures_carry_their_numbers(self) -> Self:
        if self.status in (CheckStatus.FAIL, CheckStatus.WITHIN_TOLERANCE) and not (
            self.expected and self.actual
        ):
            raise ValueError(
                f"check {self.check!r} reports {self.status} without expected/actual values. "
                "A variance with no numbers behind it cannot be reviewed or audited."
            )
        return self

    @property
    def passed(self) -> bool:
        """Whether this check permits the invoice to continue.

        ``WITHIN_TOLERANCE`` counts as passing -- that is what a tolerance is for -- while
        remaining visible as a distinct status so the cost of a loose tolerance stays
        measurable.
        """
        return self.status in (CheckStatus.PASS, CheckStatus.WITHIN_TOLERANCE)


class ValidationScorecard(_Frozen):
    """The authoritative record of what the deterministic engine found. Spec §4.3, §5."""

    correlation_id: CorrelationId
    checks: tuple[CheckResult, ...]
    computed_subtotal: Money | None = None
    computed_total: Money | None = None
    po_number: str | None = None
    grn_number: str | None = None
    validated_at: dt.datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        """Whether every check permits continuing. An empty scorecard does not pass.

        Vacuous truth is the wrong default here: no checks having run is not the same as
        every check having passed, and treating it as such would let a validation outage
        auto-approve invoices.
        """
        return bool(self.checks) and all(check.passed for check in self.checks)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status == CheckStatus.FAIL)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def absorbed_by_tolerance(self) -> tuple[CheckResult, ...]:
        """Checks that would have failed but for a configured tolerance.

        Surfaced deliberately: this is the set that tells you whether a tolerance is quietly
        absorbing a systematic overcharge.
        """
        return tuple(c for c in self.checks if c.status == CheckStatus.WITHIN_TOLERANCE)


# ---------------------------------------------------------------------------------------
# SVC-40 Duplicate detection
# ---------------------------------------------------------------------------------------


class DuplicateMatch(_Frozen):
    """One prior invoice this one resembles, and why."""

    invoice_id: InvoiceId
    tier: DuplicateTier
    score: Score = Field(ge=0, le=1)
    matched_on: tuple[str, ...] = Field(min_length=1)
    """The features that matched -- vendor, amount, po_number, document_hash."""


class DuplicateAssessment(_Frozen):
    """Whether this invoice has been seen before. Spec §6."""

    correlation_id: CorrelationId
    matches: tuple[DuplicateMatch, ...] = ()
    assessed_at: dt.datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def score(self) -> Score:
        """The strongest match found, or zero."""
        return max((m.score for m in self.matches), default=Decimal(0))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tier(self) -> DuplicateTier:
        return max(self.matches, key=lambda m: m.score).tier if self.matches else DuplicateTier.NONE

    @property
    def is_certain_duplicate(self) -> bool:
        """True only for a byte-identical document.

        Spec §6 permits automatic handling for exact hash matches alone; everything softer
        goes to a human, because a false positive here rejects a legitimate invoice.
        """
        return self.tier == DuplicateTier.EXACT_HASH


# ---------------------------------------------------------------------------------------
# SVC-50 Risk scoring
# ---------------------------------------------------------------------------------------


class RiskContribution(_Frozen):
    """One named signal's contribution to the risk score. Spec §7."""

    signal: RiskSignal
    score: Score = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)
    """Why this signal fired, in terms a reviewer can check against the evidence."""

    evidence: dict[str, Any] = Field(default_factory=dict)


class RiskAssessment(_Frozen):
    """An explainable risk score. Spec §7.

    Prioritization for review, **not** a fraud determination -- the spec is explicit, and the
    distinction matters both ethically and legally.
    """

    correlation_id: CorrelationId
    overall_score: Score = Field(ge=0, le=1)
    contributions: tuple[RiskContribution, ...] = ()
    assessed_at: dt.datetime
    scorer_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _score_must_be_explainable(self) -> Self:
        """The overall score must lie within the range of its own contributions.

        Spec §7: *the score should be explainable through the contributing signals*. A score
        higher than every signal that produced it, or lower than all of them, cannot be
        explained by any of them -- and an unexplainable risk score is a bug, not a
        judgement call. With no contributions at all, the only defensible score is zero.
        """
        if not self.contributions:
            if self.overall_score != 0:
                raise ValueError(
                    f"risk score {self.overall_score} was produced with no contributing "
                    "signals, so nothing can explain it"
                )
            return self

        scores = [c.score for c in self.contributions]
        if not min(scores) <= self.overall_score <= max(scores):
            raise ValueError(
                f"risk score {self.overall_score} lies outside the range of its own "
                f"signals [{min(scores)}, {max(scores)}] -- no contributing signal can "
                "account for it. See spec §7."
            )
        return self

    def contribution_for(self, signal: RiskSignal) -> RiskContribution | None:
        return next((c for c in self.contributions if c.signal == signal), None)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dominant_signal(self) -> RiskSignal | None:
        """The signal that contributed most -- the headline for a reviewer."""
        return max(self.contributions, key=lambda c: c.score).signal if self.contributions else None


# ---------------------------------------------------------------------------------------
# SVC-60 Exception reasoning (advisory)
# ---------------------------------------------------------------------------------------


class ExceptionFinding(_Frozen):
    """One classified discrepancy, with the evidence it rests on."""

    category: ExceptionCategory
    summary: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    """Identifiers of the evidence items supporting this finding -- check names, match ids,
    risk signals. **Required**: spec §8 forbids the agent from inventing financial facts,
    and a finding that cites nothing is indistinguishable from an invented one."""

    item_id: str | None = None


class ExceptionAnalysis(_Frozen):
    """The reasoning agent's structured output. Spec §8.

    **Advisory.** It classifies, explains, and recommends. It never authorizes -- the policy
    engine consumes this as inert data (ADR-0004).
    """

    correlation_id: CorrelationId
    findings: tuple[ExceptionFinding, ...] = ()
    explanation: str = ""
    recommended_action: RecommendedAction
    confidence: Score = Field(ge=0, le=1)
    model_id: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    analyzed_at: dt.datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def categories(self) -> tuple[ExceptionCategory, ...]:
        """Distinct categories found, in first-seen order."""
        return tuple(dict.fromkeys(f.category for f in self.findings))


# ---------------------------------------------------------------------------------------
# SVC-70 Policy
# ---------------------------------------------------------------------------------------


class PolicyDecision(_Frozen):
    """Where policy sends this invoice, and under whose authority. Spec §9."""

    correlation_id: CorrelationId
    route: PolicyRoute
    policy_version: PolicyVersionId
    """Which immutable policy version produced this. Spec §9 requires it so a historical
    decision can be replayed exactly."""

    permitted_actions: tuple[str, ...] = ()
    required_approver_role: ActorRole | None = None
    reasons: tuple[str, ...] = Field(min_length=1)
    """Why this route was chosen. Never empty -- an unexplained routing decision cannot be
    audited, and spec §18 requires every financial action to be reproducible."""

    decided_at: dt.datetime

    @property
    def requires_human(self) -> bool:
        return self.route in (PolicyRoute.REVIEW, PolicyRoute.MANDATORY_HITL)

    @property
    def permits_execution(self) -> bool:
        """Whether execution may proceed without further human authorization."""
        return self.route == PolicyRoute.AUTO_PROCESS


# ---------------------------------------------------------------------------------------
# SVC-80 Human decision
# ---------------------------------------------------------------------------------------


class HumanDecision(_Frozen):
    """An authenticated person's decision. Spec §10."""

    correlation_id: CorrelationId
    actor_id: str = Field(min_length=1)
    actor_role: ActorRole
    action: RecommendedAction
    rationale: str = Field(min_length=1)
    """Required. A decision with no stated reason is not reviewable, and overriding an
    agent recommendation without explanation is exactly what spec §13's override metrics
    need to be able to interpret."""

    decided_at: dt.datetime

    @model_validator(mode="after")
    def _system_cannot_impersonate_a_human(self) -> Self:
        """The system role may never author a human decision.

        Spec §10 requires human actions to be authenticated and role-based. Allowing the
        service account here would let an automated path manufacture the approval that a
        mandatory-HITL route exists to require.
        """
        if self.actor_role == ActorRole.SYSTEM:
            raise ValueError(
                "a HumanDecision cannot be authored by the SYSTEM role -- that is an "
                "automated action, and recording it as a human approval would defeat "
                "mandatory human review"
            )
        return self


# ---------------------------------------------------------------------------------------
# SVC-90 ERP execution
# ---------------------------------------------------------------------------------------


class ErpTransactionResult(_Frozen):
    """The outcome of an ERP posting. Spec §11."""

    correlation_id: CorrelationId
    idempotency_key: IdempotencyKey
    erp_transaction_id: str | None = None
    posted_amount: Money | None = None
    succeeded: bool
    was_already_posted: bool = False
    """True when the idempotency check found this action already done.

    Distinct from a fresh success: it means a retry was correctly absorbed, which is the
    behaviour DoD-6 tests for, and it must be visible in the audit trail rather than looking
    like a second payment.
    """

    adapter: str = Field(min_length=1)
    message: str = ""
    executed_at: dt.datetime

    @model_validator(mode="after")
    def _a_success_identifies_its_transaction(self) -> Self:
        if self.succeeded and not self.erp_transaction_id:
            raise ValueError(
                "a successful ERP posting must carry the transaction id it created -- "
                "without it, spec §18's requirement that every financial action be "
                "reproducible from the audit trail cannot be met"
            )
        return self


# ---------------------------------------------------------------------------------------
# SVC-95 Audit
# ---------------------------------------------------------------------------------------


class AuditEvent(_Frozen):
    """One immutable record of something that happened. Spec §12.

    Append-only at every level: the model is frozen, the table takes no UPDATE or DELETE,
    and the stream is the primary evidence for DoD-5.
    """

    event_id: str = Field(min_length=1)
    correlation_id: CorrelationId
    stage: PipelineStage
    action: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    actor_role: ActorRole
    result: str = Field(min_length=1)
    occurred_at: dt.datetime
    agent_version: str | None = None
    model_id: str | None = None
    policy_version: PolicyVersionId | None = None
    detail: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _occurred_at_is_tz_aware(self) -> Self:
        if self.occurred_at.tzinfo is None:
            raise ValueError("audit timestamps must be timezone-aware")
        return self


# ---------------------------------------------------------------------------------------
# The bundle handed to advisory modules and to human reviewers
# ---------------------------------------------------------------------------------------


class EvidenceBundle(_Frozen):
    """Everything known about one invoice at reasoning time.

    Two jobs, and they are the same job. It is what the orchestrator hands to the reasoning
    agent -- which has no data access of its own (ADR-0004), so this bundle *is* its world --
    and it is what the HITL dashboard renders, so a human sees exactly the evidence the
    agent saw before approving anything (spec §10).
    """

    document: IngestedDocument
    invoice: ExtractedInvoice
    purchase_order: PurchaseOrder | None = None
    goods_receipt: GoodsReceiptNote | None = None
    contract: VendorContract | None = None
    vendor: VendorProfile | None = None
    validation: ValidationScorecard
    duplicates: DuplicateAssessment
    risk: RiskAssessment

    @property
    def correlation_id(self) -> CorrelationId:
        return self.document.correlation_id

    @property
    def vendor_id(self) -> VendorId | None:
        return self.vendor.vendor_id if self.vendor else None

    def evidence_ref_ids(self) -> frozenset[str]:
        """Every identifier a finding is allowed to cite.

        The reasoning agent's ``evidence_refs`` are checked against this set, which is how
        spec §8's "must not invent financial facts" becomes something testable rather than
        something hoped for.
        """
        refs = {f"check:{c.check}" for c in self.validation.checks}
        refs |= {f"risk:{c.signal}" for c in self.risk.contributions}
        refs |= {f"duplicate:{m.invoice_id}" for m in self.duplicates.matches}
        if self.purchase_order:
            refs.add(f"po:{self.purchase_order.po_number}")
        if self.goods_receipt:
            refs.add(f"grn:{self.goods_receipt.grn_number}")
        if self.contract:
            refs.add(f"contract:{self.contract.vendor_id}")
        return frozenset(refs)
