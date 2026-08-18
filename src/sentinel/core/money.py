"""Money as a first-class value, and the arithmetic the rest of the system is allowed to do.

Two classes of bug this module exists to make impossible:

1. **Binary floating point.** ``0.1 + 0.2 != 0.3``. On an invoice line that is a rounding
   artefact; across a tolerance boundary it is an approval decision that flips. Every amount
   here is a :class:`~decimal.Decimal`, and constructing one from a ``float`` is refused.

2. **Currency-blind arithmetic.** A raw ``Decimal`` will happily let you add 500 EUR to
   1,000 USD and hand back 1,500 of nothing. :class:`Money` carries its currency and refuses
   cross-currency operations.

Rounding is ``ROUND_HALF_UP`` throughout -- the convention finance departments expect, and
notably *not* Python's default ``ROUND_HALF_EVEN``. Sentinel never invents a rounded value
silently: rounding happens where this module says it happens, and the validation engine
compares against what the supplier actually printed.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final, Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

__all__ = ["Money", "Quantity", "percentage_variance", "quantize_money"]

#: Minor-unit precision. ISO 4217 has 0- and 3-decimal currencies (JPY, KWD); when Sentinel
#: needs those, this becomes a per-currency lookup rather than a constant. Tracked as a known
#: simplification rather than an oversight.
MONEY_PLACES: Final = Decimal("0.01")

#: Quantities are decimal because invoices bill fractional units -- 2.5 hours, 0.75 tonnes.
QUANTITY_PLACES: Final = Decimal("0.0001")

Quantity = Decimal


def quantize_money(value: Decimal) -> Decimal:
    """Round a raw decimal to minor units, half-up."""
    return value.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


class Money:
    """An amount in a currency. Immutable, exact, and currency-safe.

    Construct from ``str``, ``int``, or ``Decimal`` -- never from ``float``::

        >>> Money("1050.00", "USD") + Money("200", "USD")
        Money('1250.00', 'USD')
        >>> Money(1000, "USD") * 9
        Money('9000.00', 'USD')

    Cross-currency arithmetic raises rather than guessing::

        >>> Money("100", "USD") + Money("100", "EUR")
        Traceback (most recent call last):
        ...
        ValueError: cannot combine USD and EUR ...
    """

    __slots__ = ("_amount", "_currency")

    _amount: Decimal
    _currency: str

    def __init__(self, amount: Decimal | str | int, currency: str) -> None:
        if isinstance(amount, float):  # pragma: no cover -- guarded for runtime callers
            raise TypeError(
                f"Money rejects float ({amount!r}): binary floating point cannot represent "
                "decimal currency exactly. Pass a str or Decimal."
            )
        try:
            value = Decimal(amount)
        except InvalidOperation as exc:
            raise ValueError(f"not a valid monetary amount: {amount!r}") from exc

        if not value.is_finite():
            raise ValueError(f"monetary amount must be finite, got {amount!r}")

        code = currency.upper()
        if len(code) != 3 or not code.isalpha():
            raise ValueError(f"currency must be a 3-letter ISO 4217 code, got {currency!r}")

        object.__setattr__(self, "_amount", quantize_money(value))
        object.__setattr__(self, "_currency", code)

    # -- accessors ----------------------------------------------------------------------

    @property
    def amount(self) -> Decimal:
        """The exact amount, quantized to minor units."""
        return self._amount

    @property
    def currency(self) -> str:
        """ISO 4217 currency code, uppercase."""
        return self._currency

    @classmethod
    def zero(cls, currency: str) -> Self:
        """The additive identity in `currency`. Useful as a ``sum`` seed."""
        return cls(Decimal(0), currency)

    # -- arithmetic ---------------------------------------------------------------------

    def _check(self, other: Money, op: str) -> None:
        if self._currency != other._currency:
            raise ValueError(
                f"cannot combine {self._currency} and {other._currency} ({op}). "
                "Sentinel does not convert currencies implicitly -- an invoice and its PO "
                "must be reconciled in one currency, or the mismatch is itself an exception."
            )

    def __add__(self, other: Money) -> Money:
        self._check(other, "add")
        return Money(self._amount + other._amount, self._currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other, "subtract")
        return Money(self._amount - other._amount, self._currency)

    def __mul__(self, factor: Decimal | int) -> Money:
        """Scale by a dimensionless quantity -- unit price times quantity, or a tax rate.

        Multiplying two Money values is meaningless and is not supported.
        """
        # Both guards are unreachable for a type-checked caller, which is the point: they
        # exist for JSON payloads, notebooks, and anything else that arrives untyped.
        if isinstance(factor, float):
            raise TypeError(f"cannot scale Money by float {factor!r}; use Decimal or int")
        if isinstance(factor, Money):  # type: ignore[unreachable]
            raise TypeError("cannot multiply Money by Money -- the result has no meaning")
        return Money(self._amount * Decimal(factor), self._currency)

    __rmul__ = __mul__

    def __neg__(self) -> Money:
        return Money(-self._amount, self._currency)

    def __abs__(self) -> Money:
        return Money(abs(self._amount), self._currency)

    # -- comparison ---------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self._amount == other._amount and self._currency == other._currency

    def __lt__(self, other: Money) -> bool:
        self._check(other, "compare")
        return self._amount < other._amount

    def __le__(self, other: Money) -> bool:
        self._check(other, "compare")
        return self._amount <= other._amount

    def __gt__(self, other: Money) -> bool:
        self._check(other, "compare")
        return self._amount > other._amount

    def __ge__(self, other: Money) -> bool:
        self._check(other, "compare")
        return self._amount >= other._amount

    def __hash__(self) -> int:
        return hash((self._amount, self._currency))

    def __bool__(self) -> bool:
        return bool(self._amount)

    def is_zero(self) -> bool:
        return not self._amount

    # -- representation -----------------------------------------------------------------

    def __repr__(self) -> str:
        return f"Money('{self._amount}', '{self._currency}')"

    def __str__(self) -> str:
        return f"{self._amount} {self._currency}"

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Money is immutable")

    # -- pydantic integration -----------------------------------------------------------

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        """Serialize as ``{"amount": "1050.00", "currency": "USD"}``.

        A string, not a number: JSON numbers are IEEE 754 doubles, so serializing an amount
        as a number reintroduces exactly the float problem this class exists to prevent.
        """

        def from_mapping(value: dict[str, Any]) -> Money:
            return cls(value["amount"], value["currency"])

        mapping_schema = core_schema.chain_schema(
            [
                core_schema.typed_dict_schema(
                    {
                        "amount": core_schema.typed_dict_field(core_schema.str_schema()),
                        "currency": core_schema.typed_dict_field(core_schema.str_schema()),
                    }
                ),
                core_schema.no_info_plain_validator_function(from_mapping),
            ]
        )
        return core_schema.json_or_python_schema(
            json_schema=mapping_schema,
            python_schema=core_schema.union_schema(
                [core_schema.is_instance_schema(cls), mapping_schema]
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda m: {"amount": str(m.amount), "currency": m.currency},
                return_schema=core_schema.dict_schema(),
            ),
        )


def percentage_variance(actual: Money, expected: Money) -> Decimal:
    """Signed variance of `actual` against `expected`, as a percentage.

    Positive means the supplier billed above the agreed figure -- the direction that costs
    money, and the one tolerance policies are written against.

        >>> percentage_variance(Money("1050", "USD"), Money("1000", "USD"))
        Decimal('5.0000')

    Raises when `expected` is zero: there is no meaningful percentage variance from nothing,
    and returning 0 or infinity would both silently mislead a tolerance check. The caller
    must treat an unexpected charge as its own exception category, not as a variance.
    """
    actual._check(expected, "compare")
    if expected.is_zero():
        raise ValueError(
            "percentage variance is undefined against a zero baseline; an amount charged "
            "with no agreed counterpart is an unapproved-charge exception, not a variance"
        )
    ratio = (actual.amount - expected.amount) / expected.amount * 100
    return ratio.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
