"""Evidence contracts, with emphasis on the invariants that encode safety properties."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from sentinel.core.enums import (
    ActorRole,
    CheckStatus,
    DuplicateTier,
    ExceptionCategory,
    PipelineStage,
    PolicyRoute,
    RecommendedAction,
    RiskSignal,
)
from sentinel.core.evidence import (
    AuditEvent,
    CheckResult,
    DuplicateAssessment,
    DuplicateMatch,
    ErpTransactionResult,
    EvidenceBundle,
    ExceptionAnalysis,
    ExceptionFinding,
    ExtractedField,
    HumanDecision,
    PolicyDecision,
    RiskAssessment,
    RiskContribution,
    ValidationScorecard,
)
from sentinel.core.ids import (
    CorrelationId,
    InvoiceId,
    PolicyVersionId,
    idempotency_key,
)
from sentinel.core.money import Money
from tests.golden import (
    ACCEPTED_QTY,
    BILLED_QTY,
    golden_contract,
    golden_document,
    golden_extracted_invoice,
    golden_goods_receipt,
    golden_purchase_order,
    golden_vendor,
)

NOW = dt.datetime(2026, 1, 16, 10, 0, tzinfo=dt.UTC)
COR = CorrelationId.new()


class TestExtractedField:
    def test_value_and_confidence_travel_together(self) -> None:
        field = ExtractedField(value=Money("1050", "USD"), confidence=Decimal("0.94"), page=1)
        assert field.value == Money("1050", "USD")
        assert field.is_confident(Decimal("0.90"))
        assert not field.is_confident(Decimal("0.95"))

    @pytest.mark.parametrize("bad", [Decimal("-0.1"), Decimal("1.1")])
    def test_confidence_must_be_a_probability(self, bad: Decimal) -> None:
        with pytest.raises(ValidationError):
            ExtractedField(value="x", confidence=bad)


class TestExtractedInvoice:
    def test_the_golden_invoice_is_fully_confident(self) -> None:
        invoice = golden_extracted_invoice()
        assert invoice.low_confidence_fields(Decimal("0.80")) == ()

    def test_low_confidence_fields_are_named_precisely(self) -> None:
        """A reviewer needs to be told which number is doubted, not just that one is."""
        invoice = golden_extracted_invoice(
            total_due=ExtractedField(value=Money("11700", "USD"), confidence=Decimal("0.42"))
        )
        weak = invoice.low_confidence_fields(Decimal("0.80"))
        assert weak == ("total_due",)

    def test_weak_line_fields_are_reported_with_their_index(self) -> None:
        from sentinel.core.evidence import ExtractedLine

        invoice = golden_extracted_invoice(
            lines=(
                ExtractedLine(
                    item_id=ExtractedField(value="LAPTOP-01", confidence=Decimal("0.99")),
                    billed_qty=ExtractedField(value=Decimal(10), confidence=Decimal("0.99")),
                    billed_unit_price=ExtractedField(
                        value=Money("1050", "USD"), confidence=Decimal("0.31")
                    ),
                ),
            )
        )
        assert invoice.low_confidence_fields(Decimal("0.80")) == ("lines[0].billed_unit_price",)

    def test_records_the_model_and_prompt_that_produced_it(self) -> None:
        """Spec section 12: a decision must be reconstructable after the model changes."""
        invoice = golden_extracted_invoice()
        assert invoice.model_id
        assert invoice.prompt_version


class TestValidationScorecard:
    def test_a_failing_check_must_carry_its_numbers(self) -> None:
        """'Price check failed' without $1,050 vs $1,000 forces a human to redo the work."""
        with pytest.raises(ValidationError, match="cannot be reviewed or audited"):
            CheckResult(check="unit_price", status=CheckStatus.FAIL)

    def test_a_passing_check_needs_no_numbers(self) -> None:
        assert CheckResult(check="po_reference", status=CheckStatus.PASS).passed

    def test_within_tolerance_counts_as_passing_but_stays_visible(self) -> None:
        check = CheckResult(
            check="unit_price",
            status=CheckStatus.WITHIN_TOLERANCE,
            expected="1000.00 USD",
            actual="1019.00 USD",
            variance_pct=Decimal("1.9"),
            tolerance_pct=Decimal("2.0"),
        )
        card = ValidationScorecard(correlation_id=COR, checks=(check,), validated_at=NOW)

        assert card.passed
        assert card.absorbed_by_tolerance == (check,)
        assert card.failures == ()

    def test_an_empty_scorecard_does_not_pass(self) -> None:
        """Vacuous truth would let a validation outage auto-approve invoices."""
        card = ValidationScorecard(correlation_id=COR, checks=(), validated_at=NOW)
        assert not card.passed

    def test_one_failure_fails_the_card(self) -> None:
        card = ValidationScorecard(
            correlation_id=COR,
            checks=(
                CheckResult(check="po_reference", status=CheckStatus.PASS),
                CheckResult(
                    check="quantity",
                    status=CheckStatus.FAIL,
                    expected="9",
                    actual="10",
                ),
            ),
            validated_at=NOW,
        )
        assert not card.passed
        assert len(card.failures) == 1


class TestRiskExplainability:
    """Spec section 7: the score must be explainable through its contributing signals."""

    def signal(self, kind: RiskSignal, score: str) -> RiskContribution:
        return RiskContribution(signal=kind, score=Decimal(score), rationale="because")

    def test_a_score_within_its_signals_range_is_accepted(self) -> None:
        """The spec's own illustration: 0.91 overall from signals spanning 0.71 to 0.98."""
        assessment = RiskAssessment(
            correlation_id=COR,
            overall_score=Decimal("0.91"),
            contributions=(
                self.signal(RiskSignal.DUPLICATE, "0.92"),
                self.signal(RiskSignal.PAYMENT_CHANGE, "0.98"),
                self.signal(RiskSignal.VENDOR_ANOMALY, "0.84"),
                self.signal(RiskSignal.PRICE_ANOMALY, "0.71"),
            ),
            assessed_at=NOW,
            scorer_version="v1",
        )
        assert assessment.dominant_signal == RiskSignal.PAYMENT_CHANGE
        assert assessment.contribution_for(RiskSignal.DUPLICATE) is not None

    def test_a_score_above_every_signal_is_refused(self) -> None:
        """No contributing signal could account for it, so nothing can explain it."""
        with pytest.raises(ValidationError, match="outside the range of its own"):
            RiskAssessment(
                correlation_id=COR,
                overall_score=Decimal("0.99"),
                contributions=(self.signal(RiskSignal.NEW_VENDOR, "0.40"),),
                assessed_at=NOW,
                scorer_version="v1",
            )

    def test_a_score_below_every_signal_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="outside the range of its own"):
            RiskAssessment(
                correlation_id=COR,
                overall_score=Decimal("0.10"),
                contributions=(self.signal(RiskSignal.NEW_VENDOR, "0.40"),),
                assessed_at=NOW,
                scorer_version="v1",
            )

    def test_a_score_with_no_signals_at_all_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="no contributing signals"):
            RiskAssessment(
                correlation_id=COR,
                overall_score=Decimal("0.75"),
                contributions=(),
                assessed_at=NOW,
                scorer_version="v1",
            )

    def test_zero_risk_with_no_signals_is_the_one_valid_empty_case(self) -> None:
        assessment = RiskAssessment(
            correlation_id=COR,
            overall_score=Decimal(0),
            contributions=(),
            assessed_at=NOW,
            scorer_version="v1",
        )
        assert assessment.dominant_signal is None

    def test_every_contribution_states_why_it_fired(self) -> None:
        with pytest.raises(ValidationError):
            RiskContribution(signal=RiskSignal.NEW_VENDOR, score=Decimal("0.5"), rationale="")


class TestExceptionGrounding:
    """Spec section 8: the agent must not invent financial facts."""

    def test_a_finding_must_cite_evidence(self) -> None:
        with pytest.raises(ValidationError):
            ExceptionFinding(
                category=ExceptionCategory.PRICE,
                summary="the price looks wrong",
                evidence_refs=(),
            )

    def test_a_grounded_finding_is_accepted(self) -> None:
        finding = ExceptionFinding(
            category=ExceptionCategory.PRICE,
            summary="billed $1,050 against an agreed $1,000",
            evidence_refs=("check:unit_price", "po:9901"),
        )
        assert finding.category == ExceptionCategory.PRICE

    def test_categories_are_deduplicated_in_order(self) -> None:
        analysis = ExceptionAnalysis(
            correlation_id=COR,
            findings=(
                ExceptionFinding(
                    category=ExceptionCategory.QUANTITY,
                    summary="10 billed, 9 accepted",
                    evidence_refs=("check:quantity",),
                ),
                ExceptionFinding(
                    category=ExceptionCategory.PRICE,
                    summary="+5%",
                    evidence_refs=("check:unit_price",),
                ),
                ExceptionFinding(
                    category=ExceptionCategory.QUANTITY,
                    summary="short delivery",
                    evidence_refs=("grn:GRN-9901-01",),
                ),
            ),
            recommended_action=RecommendedAction.ESCALATE,
            confidence=Decimal("0.88"),
            model_id="fixture",
            prompt_version="v1",
            analyzed_at=NOW,
        )
        assert analysis.categories == (ExceptionCategory.QUANTITY, ExceptionCategory.PRICE)


class TestPolicyDecision:
    def test_a_route_must_state_its_reasons(self) -> None:
        with pytest.raises(ValidationError):
            PolicyDecision(
                correlation_id=COR,
                route=PolicyRoute.AUTO_PROCESS,
                policy_version=PolicyVersionId.new(),
                reasons=(),
                decided_at=NOW,
            )

    def test_only_auto_process_permits_execution_without_a_human(self) -> None:
        def route(kind: PolicyRoute) -> PolicyDecision:
            return PolicyDecision(
                correlation_id=COR,
                route=kind,
                policy_version=PolicyVersionId.new(),
                reasons=("because",),
                decided_at=NOW,
            )

        assert route(PolicyRoute.AUTO_PROCESS).permits_execution
        assert not route(PolicyRoute.REVIEW).permits_execution
        assert not route(PolicyRoute.MANDATORY_HITL).permits_execution
        assert not route(PolicyRoute.BLOCK).permits_execution

    def test_review_and_hitl_both_require_a_human(self) -> None:
        def route(kind: PolicyRoute) -> PolicyDecision:
            return PolicyDecision(
                correlation_id=COR,
                route=kind,
                policy_version=PolicyVersionId.new(),
                reasons=("because",),
                decided_at=NOW,
            )

        assert route(PolicyRoute.REVIEW).requires_human
        assert route(PolicyRoute.MANDATORY_HITL).requires_human
        assert not route(PolicyRoute.AUTO_PROCESS).requires_human


class TestHumanDecision:
    def test_the_system_role_cannot_author_a_human_decision(self) -> None:
        """Otherwise an automated path could manufacture the approval HITL exists to require."""
        with pytest.raises(ValidationError, match="cannot be authored by the SYSTEM role"):
            HumanDecision(
                correlation_id=COR,
                actor_id="svc-pipeline",
                actor_role=ActorRole.SYSTEM,
                action=RecommendedAction.APPROVE,
                rationale="automated",
                decided_at=NOW,
            )

    def test_a_decision_must_state_its_rationale(self) -> None:
        with pytest.raises(ValidationError):
            HumanDecision(
                correlation_id=COR,
                actor_id="alice",
                actor_role=ActorRole.AP_MANAGER,
                action=RecommendedAction.APPROVE,
                rationale="",
                decided_at=NOW,
            )

    def test_a_manager_approval_is_accepted(self) -> None:
        decision = HumanDecision(
            correlation_id=COR,
            actor_id="alice",
            actor_role=ActorRole.AP_MANAGER,
            action=RecommendedAction.REQUEST_CREDIT_NOTE,
            rationale="one unit damaged; requesting a credit note for the shipping charge",
            decided_at=NOW,
        )
        assert decision.actor_role == ActorRole.AP_MANAGER


class TestDuplicateAssessment:
    def test_no_matches_scores_zero_and_tiers_none(self) -> None:
        assessment = DuplicateAssessment(correlation_id=COR, assessed_at=NOW)
        assert assessment.score == 0
        assert assessment.tier == DuplicateTier.NONE
        assert not assessment.is_certain_duplicate

    def test_only_an_exact_hash_is_a_certain_duplicate(self) -> None:
        """Anything softer goes to a human -- a false positive rejects a real invoice."""

        def assess(tier: DuplicateTier, score: str) -> DuplicateAssessment:
            return DuplicateAssessment(
                correlation_id=COR,
                matches=(
                    DuplicateMatch(
                        invoice_id=InvoiceId.new(),
                        tier=tier,
                        score=Decimal(score),
                        matched_on=("document_hash",),
                    ),
                ),
                assessed_at=NOW,
            )

        assert assess(DuplicateTier.EXACT_HASH, "1.0").is_certain_duplicate
        assert not assess(DuplicateTier.SIMILAR, "0.95").is_certain_duplicate
        assert not assess(DuplicateTier.FUZZY_NUMBER, "0.99").is_certain_duplicate

    def test_the_strongest_match_wins(self) -> None:
        assessment = DuplicateAssessment(
            correlation_id=COR,
            matches=(
                DuplicateMatch(
                    invoice_id=InvoiceId.new(),
                    tier=DuplicateTier.SIMILAR,
                    score=Decimal("0.60"),
                    matched_on=("amount",),
                ),
                DuplicateMatch(
                    invoice_id=InvoiceId.new(),
                    tier=DuplicateTier.EXACT_NUMBER,
                    score=Decimal("0.95"),
                    matched_on=("invoice_number", "vendor"),
                ),
            ),
            assessed_at=NOW,
        )
        assert assessment.score == Decimal("0.95")
        assert assessment.tier == DuplicateTier.EXACT_NUMBER


class TestErpTransactionResult:
    def test_a_success_must_identify_its_transaction(self) -> None:
        with pytest.raises(ValidationError, match="must carry the transaction id"):
            ErpTransactionResult(
                correlation_id=COR,
                idempotency_key=idempotency_key("post", "inv_1"),
                succeeded=True,
                adapter="mock",
                executed_at=NOW,
            )

    def test_a_failure_needs_no_transaction_id(self) -> None:
        result = ErpTransactionResult(
            correlation_id=COR,
            idempotency_key=idempotency_key("post", "inv_1"),
            succeeded=False,
            adapter="mock",
            message="ERP rejected: period closed",
            executed_at=NOW,
        )
        assert not result.succeeded

    def test_an_absorbed_retry_is_distinguishable_from_a_fresh_posting(self) -> None:
        """DoD-6: the audit trail must not make an absorbed retry look like a second payment."""
        result = ErpTransactionResult(
            correlation_id=COR,
            idempotency_key=idempotency_key("post", "inv_1"),
            erp_transaction_id="ERP-000123",
            succeeded=True,
            was_already_posted=True,
            adapter="mock",
            executed_at=NOW,
        )
        assert result.succeeded and result.was_already_posted


class TestAuditEvent:
    def test_a_naive_timestamp_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            AuditEvent(
                event_id="evt_1",
                correlation_id=COR,
                stage=PipelineStage.VALIDATION,
                action="validate",
                actor_id="system",
                actor_role=ActorRole.SYSTEM,
                result="passed",
                occurred_at=dt.datetime(2026, 1, 16, 10, 0),  # noqa: DTZ001
            )

    def test_records_the_versions_behind_a_decision(self) -> None:
        policy = PolicyVersionId.new()
        event = AuditEvent(
            event_id="evt_2",
            correlation_id=COR,
            stage=PipelineStage.POLICY,
            action="route",
            actor_id="system",
            actor_role=ActorRole.SYSTEM,
            result="mandatory_hitl",
            occurred_at=NOW,
            policy_version=policy,
            model_id="fixture-extractor",
        )
        assert event.policy_version == policy


class TestEvidenceBundle:
    def bundle(self) -> EvidenceBundle:
        document = golden_document()
        return EvidenceBundle(
            document=document,
            invoice=golden_extracted_invoice(document),
            purchase_order=golden_purchase_order(),
            goods_receipt=golden_goods_receipt(),
            contract=golden_contract(),
            vendor=golden_vendor(),
            validation=ValidationScorecard(
                correlation_id=document.correlation_id,
                checks=(
                    CheckResult(
                        check="unit_price",
                        status=CheckStatus.FAIL,
                        expected="1000.00 USD",
                        actual="1050.00 USD",
                        variance_pct=Decimal("5.0"),
                    ),
                ),
                validated_at=NOW,
            ),
            duplicates=DuplicateAssessment(correlation_id=document.correlation_id, assessed_at=NOW),
            risk=RiskAssessment(
                correlation_id=document.correlation_id,
                overall_score=Decimal("0.35"),
                contributions=(
                    RiskContribution(
                        signal=RiskSignal.PRICE_ANOMALY,
                        score=Decimal("0.35"),
                        rationale="unit price 5% above the agreed figure",
                    ),
                ),
                assessed_at=NOW,
                scorer_version="v1",
            ),
        )

    def test_exposes_the_correlation_id_from_its_document(self) -> None:
        bundle = self.bundle()
        assert bundle.correlation_id == bundle.document.correlation_id

    def test_enumerates_every_citable_evidence_reference(self) -> None:
        """The set a reasoning finding is allowed to cite -- how grounding becomes testable."""
        refs = self.bundle().evidence_ref_ids()

        assert "check:unit_price" in refs
        assert "risk:price_anomaly" in refs
        assert "po:9901" in refs
        assert "grn:GRN-9901-01" in refs
        assert "check:invented_check" not in refs

    def test_the_golden_scenario_carries_the_spec_numbers(self) -> None:
        """Guards the fixture itself: spec section 15's 10 billed against 9 accepted."""
        bundle = self.bundle()
        assert bundle.invoice.lines[0].billed_qty.value == BILLED_QTY == Decimal(10)
        assert bundle.goods_receipt is not None
        assert bundle.goods_receipt.accepted_qty_for("LAPTOP-01") == ACCEPTED_QTY == Decimal(9)
        assert bundle.purchase_order is not None
        assert bundle.purchase_order.approved_shipping is None
        assert bundle.invoice.shipping is not None
        assert bundle.invoice.shipping.value == Money("200", "USD")
