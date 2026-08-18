"""Money is the foundation every financial guarantee rests on, so it is tested hard."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sentinel.core.money import Money, percentage_variance, quantize_money


class TestConstruction:
    def test_accepts_str_int_and_decimal(self) -> None:
        assert Money("10.50", "USD").amount == Decimal("10.50")
        assert Money(1000, "USD").amount == Decimal("1000.00")
        assert Money(Decimal("1050.005"), "USD").amount == Decimal("1050.01")

    def test_rejects_float(self) -> None:
        with pytest.raises(TypeError, match="rejects float"):
            Money(10.5, "USD")  # type: ignore[arg-type]

    def test_rejects_nonsense_amount(self) -> None:
        with pytest.raises(ValueError, match="not a valid monetary amount"):
            Money("twelve dollars", "USD")

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
    def test_rejects_non_finite(self, bad: str) -> None:
        with pytest.raises(ValueError, match="must be finite"):
            Money(bad, "USD")

    @pytest.mark.parametrize("bad", ["US", "USDD", "12$", ""])
    def test_rejects_bad_currency_code(self, bad: str) -> None:
        with pytest.raises(ValueError, match="ISO 4217"):
            Money("1", bad)

    def test_normalizes_currency_case(self) -> None:
        assert Money("1", "usd").currency == "USD"

    def test_is_immutable(self) -> None:
        with pytest.raises(AttributeError, match="immutable"):
            Money("1", "USD").amount = Decimal("2")  # type: ignore[misc]


class TestRounding:
    def test_rounds_half_up_not_bankers(self) -> None:
        """Finance rounds 0.005 up. Python's Decimal default would round 2.675 to 2.67."""
        assert quantize_money(Decimal("2.675")) == Decimal("2.68")
        assert quantize_money(Decimal("2.665")) == Decimal("2.67")
        assert Money("0.005", "USD").amount == Decimal("0.01")

    def test_the_float_trap_does_not_apply(self) -> None:
        """A tenth plus two tenths is exactly three tenths. In float it is not."""
        assert 0.10 + 0.20 != 0.30, "precondition: binary float really does get this wrong"

        assert Money("0.10", "USD") + Money("0.20", "USD") == Money("0.30", "USD")

        cents = sum((Money("0.10", "USD") for _ in range(10)), Money.zero("USD"))
        assert cents.amount == Decimal("1.00")


class TestArithmetic:
    def test_add_and_subtract(self) -> None:
        assert Money("1050.00", "USD") + Money("200", "USD") == Money("1250.00", "USD")
        assert Money("1050.00", "USD") - Money("50", "USD") == Money("1000.00", "USD")

    def test_scaling_by_quantity(self) -> None:
        assert Money("1000", "USD") * 9 == Money("9000.00", "USD")
        assert Decimal("2.5") * Money("100", "USD") == Money("250.00", "USD")

    def test_negation_and_abs(self) -> None:
        assert -Money("50", "USD") == Money("-50", "USD")
        assert abs(Money("-50", "USD")) == Money("50", "USD")

    def test_money_times_money_is_refused(self) -> None:
        with pytest.raises(TypeError, match="no meaning"):
            Money("2", "USD") * Money("3", "USD")  # type: ignore[operator]

    def test_scaling_by_float_is_refused(self) -> None:
        with pytest.raises(TypeError, match="float"):
            Money("100", "USD") * 1.5  # type: ignore[operator]


class TestCurrencySafety:
    @pytest.mark.parametrize(
        "operation",
        [
            lambda a, b: a + b,
            lambda a, b: a - b,
            lambda a, b: a < b,
            lambda a, b: a >= b,
        ],
    )
    def test_cross_currency_operations_raise(self, operation) -> None:
        usd, eur = Money("100", "USD"), Money("100", "EUR")
        with pytest.raises(ValueError, match="cannot combine USD and EUR"):
            operation(usd, eur)

    def test_cross_currency_equality_is_false_not_an_error(self) -> None:
        """Equality must stay total -- dicts and sets rely on it not raising."""
        assert Money("100", "USD") != Money("100", "EUR")
        assert Money("100", "USD") != "100 USD"

    def test_currency_participates_in_hash(self) -> None:
        assert len({Money("100", "USD"), Money("100", "EUR"), Money("100", "USD")}) == 2


class TestComparison:
    def test_ordering(self) -> None:
        assert Money("999.99", "USD") < Money("1000", "USD")
        assert Money("1000", "USD") <= Money("1000", "USD")
        assert Money("1000.01", "USD") > Money("1000", "USD")

    def test_truthiness_tracks_zero(self) -> None:
        assert not Money.zero("USD")
        assert Money("0.01", "USD")
        assert Money.zero("USD").is_zero()


class TestPercentageVariance:
    def test_golden_path_price_variance(self) -> None:
        """Spec section 15: invoice bills $1,050 against a PO price of $1,000."""
        variance = percentage_variance(Money("1050", "USD"), Money("1000", "USD"))
        assert variance == Decimal("5.0000")

    def test_overbilling_is_positive_underbilling_negative(self) -> None:
        assert percentage_variance(Money("102", "USD"), Money("100", "USD")) > 0
        assert percentage_variance(Money("98", "USD"), Money("100", "USD")) < 0

    def test_within_a_two_percent_tolerance(self) -> None:
        """The spec's example price tolerance is +/-2%."""
        assert abs(percentage_variance(Money("1019", "USD"), Money("1000", "USD"))) < 2
        assert abs(percentage_variance(Money("1021", "USD"), Money("1000", "USD"))) > 2

    def test_zero_baseline_raises_rather_than_guessing(self) -> None:
        """An unapproved $200 shipping charge has no PO counterpart to vary from.

        Returning 0 would make it look compliant; returning infinity would make every
        tolerance check meaningless. It is a different exception category entirely.
        """
        with pytest.raises(ValueError, match="unapproved-charge exception"):
            percentage_variance(Money("200", "USD"), Money.zero("USD"))

    def test_cross_currency_variance_raises(self) -> None:
        with pytest.raises(ValueError, match="cannot combine"):
            percentage_variance(Money("100", "USD"), Money("100", "EUR"))


class TestPydanticIntegration:
    def test_round_trips_through_json_as_a_string(self) -> None:
        from pydantic import BaseModel

        class Line(BaseModel):
            total: Money

        line = Line(total=Money("1050.50", "USD"))
        payload = line.model_dump_json()

        assert '"amount":"1050.50"' in payload, (
            "amount must serialize as a string, not a JSON number"
        )
        assert Line.model_validate_json(payload).total == Money("1050.50", "USD")

    def test_rejects_a_bare_number_from_json(self) -> None:
        from pydantic import BaseModel, ValidationError

        class Line(BaseModel):
            total: Money

        with pytest.raises(ValidationError):
            Line.model_validate_json('{"total": 1050.50}')
