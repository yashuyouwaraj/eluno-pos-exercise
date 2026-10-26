"""Money boundary helpers. Domain amounts are always integer paise."""

from decimal import Decimal, InvalidOperation
from typing import Union


ExternalAmount = Union[str, Decimal]
_PAISE = Decimal("0.01")


class MoneyConversionError(ValueError):
    """Raised when an external amount cannot be represented in whole paise."""


def paise_from_decimal(amount: ExternalAmount) -> int:
    """Convert a decimal-string/Decimal boundary value to integer paise exactly.

    Shopify accepts decimal strings with two fraction digits. Refusing fractions of
    a paisa keeps any rounding decision in the pricing domain rather than hiding it
    at an integration boundary.
    """
    try:
        value = amount if isinstance(amount, Decimal) else Decimal(amount)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MoneyConversionError("amount must be a decimal string or Decimal") from exc

    if not value.is_finite():
        raise MoneyConversionError("amount must be finite")
    paise = value * 100
    if paise != paise.to_integral_value():
        raise MoneyConversionError("amount must be representable in whole paise")
    return int(paise)


def decimal_string_from_paise(amount_paise: int) -> str:
    """Serialize an integer-paise domain amount for a decimal-string API boundary."""
    if isinstance(amount_paise, bool) or not isinstance(amount_paise, int):
        raise TypeError("amount_paise must be an integer")
    return format((Decimal(amount_paise) / 100).quantize(_PAISE), ".2f")

