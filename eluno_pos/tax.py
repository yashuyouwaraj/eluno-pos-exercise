"""Inclusive GST calculations and effective-dated tax-rate lookup."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
from typing import Iterable, Optional


_ONE_PAISE = Decimal("1")


@dataclass(frozen=True)
class TaxRate:
    hsn: str
    rate_bps: int
    effective_from: date
    effective_to: Optional[date] = None

    def applies_on(self, on_date: date) -> bool:
        return self.effective_from <= on_date and (
            self.effective_to is None or on_date <= self.effective_to
        )


class TaxRateTable:
    """Data-driven lookup for the POS-owned HSN tax table."""

    def __init__(self, rates: Iterable[TaxRate]):
        self._rates = tuple(rates)

    @classmethod
    def from_file(cls, path: Path) -> "TaxRateTable":
        with path.open(encoding="utf-8") as data_file:
            payload = json.load(data_file)
        rates = []
        for row in payload["rates"]:
            rates.append(
                TaxRate(
                    hsn=row["hsn"],
                    rate_bps=int(row["rate_bps"]),
                    effective_from=date.fromisoformat(row["effective_from"]),
                    effective_to=(
                        date.fromisoformat(row["effective_to"])
                        if row.get("effective_to")
                        else None
                    ),
                )
            )
        return cls(rates)

    def rate_for(self, hsn: str, on_date: date) -> TaxRate:
        matches = [
            rate for rate in self._rates
            if rate.hsn == hsn and rate.applies_on(on_date)
        ]
        if len(matches) != 1:
            raise ValueError(
                "expected exactly one effective tax rate for HSN {} on {}, found {}".format(
                    hsn, on_date.isoformat(), len(matches)
                )
            )
        return matches[0]


@dataclass(frozen=True)
class LineTax:
    """A reconciled inclusive-GST breakdown in integer paise."""

    gross_paise: int
    taxable_paise: int
    tax_paise: int
    cgst_paise: int
    sgst_paise: int
    rate_bps: int

    def reconciles(self) -> bool:
        return (
            self.taxable_paise + self.tax_paise == self.gross_paise
            and self.cgst_paise + self.sgst_paise == self.tax_paise
        )


def calculate_inclusive_gst(gross_paise: int, rate_bps: int) -> LineTax:
    """Back out GST using the starter-kit half-up, per-line policy.

    taxable = gross * 10000 / (10000 + rate), rounded half-up to paise.
    CGST is half of the resulting tax, rounded half-up; SGST receives the remainder.
    """
    if isinstance(gross_paise, bool) or not isinstance(gross_paise, int):
        raise TypeError("gross_paise must be an integer")
    if isinstance(rate_bps, bool) or not isinstance(rate_bps, int):
        raise TypeError("rate_bps must be an integer")
    if gross_paise < 0:
        raise ValueError("gross_paise cannot be negative")
    if rate_bps < 0:
        raise ValueError("rate_bps cannot be negative")

    taxable = (
        Decimal(gross_paise) * Decimal(10000) / Decimal(10000 + rate_bps)
    ).quantize(_ONE_PAISE, rounding=ROUND_HALF_UP)
    taxable_paise = int(taxable)
    tax_paise = gross_paise - taxable_paise
    cgst_paise = int(
        (Decimal(tax_paise) / 2).quantize(_ONE_PAISE, rounding=ROUND_HALF_UP)
    )
    return LineTax(
        gross_paise=gross_paise,
        taxable_paise=taxable_paise,
        tax_paise=tax_paise,
        cgst_paise=cgst_paise,
        sgst_paise=tax_paise - cgst_paise,
        rate_bps=rate_bps,
    )


def calculate_line_tax(
    unit_price_paise: int, quantity: int, total_discount_paise: int, rate_bps: int
) -> LineTax:
    """Calculate tax after a line-level inclusive-price discount."""
    if isinstance(unit_price_paise, bool) or not isinstance(unit_price_paise, int):
        raise TypeError("unit_price_paise must be an integer")
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise TypeError("quantity must be an integer")
    if isinstance(total_discount_paise, bool) or not isinstance(total_discount_paise, int):
        raise TypeError("total_discount_paise must be an integer")
    if unit_price_paise < 0 or quantity <= 0 or total_discount_paise < 0:
        raise ValueError("price/discount must be non-negative and quantity must be positive")
    gross_paise = unit_price_paise * quantity - total_discount_paise
    if gross_paise < 0:
        raise ValueError("discount cannot exceed the line subtotal")
    return calculate_inclusive_gst(gross_paise, rate_bps)

