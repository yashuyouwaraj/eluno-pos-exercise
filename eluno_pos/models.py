"""Normalized POS domain data, independent of remote API envelopes."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re
from typing import Any, Dict, FrozenSet, Optional, Tuple


class PrescriptionDataError(ValueError):
    pass


def axis_from_data(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise PrescriptionDataError("axis must be an integer degree value")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value.strip()):
        return int(value)
    raise PrescriptionDataError("axis must be an integer degree value")


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    return None if value is None else Decimal(str(value))


@dataclass(frozen=True)
class Customer:
    customer_id: str
    phone: str
    first_name: str
    last_name: str
    tags: FrozenSet[str] = frozenset()

    @classmethod
    def from_data(cls, data: Dict[str, Any]) -> "Customer":
        return cls(
            customer_id=str(data["id"]),
            phone=str(data["phone"]),
            first_name=str(data.get("first_name") or ""),
            last_name=str(data.get("last_name") or ""),
            tags=frozenset(
                tag.strip() for tag in str(data.get("tags") or "").split(",") if tag.strip()
            ),
        )


@dataclass(frozen=True)
class EyePrescription:
    sph: Optional[Decimal]
    cyl: Optional[Decimal]
    axis: Optional[int]
    add: Optional[Decimal]

    @classmethod
    def from_data(cls, data: Dict[str, Any]) -> "EyePrescription":
        return cls(
            sph=_decimal_or_none(data.get("sph")),
            cyl=_decimal_or_none(data.get("cyl")),
            axis=axis_from_data(data.get("axis")),
            add=_decimal_or_none(data.get("add")),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "sph": None if self.sph is None else str(self.sph),
            "cyl": None if self.cyl is None else str(self.cyl),
            "axis": self.axis,
            "add": None if self.add is None else str(self.add),
        }


@dataclass(frozen=True)
class Prescription:
    rx_id: str
    customer_id: str
    phone: str
    prescribed_on: date
    valid_until: date
    od: EyePrescription
    os: EyePrescription

    @classmethod
    def from_data(cls, data: Dict[str, Any]) -> "Prescription":
        try:
            od = EyePrescription.from_data(data["od"])
        except PrescriptionDataError as exc:
            raise PrescriptionDataError("Right eye {}".format(exc)) from exc
        try:
            os = EyePrescription.from_data(data["os"])
        except PrescriptionDataError as exc:
            raise PrescriptionDataError("Left eye {}".format(exc)) from exc
        return cls(
            rx_id=str(data["rx_id"]),
            customer_id=str(data["customer_id"]),
            phone=str(data["phone"]),
            prescribed_on=date.fromisoformat(str(data["date"])),
            valid_until=date.fromisoformat(str(data["valid_until"])),
            od=od,
            os=os,
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rx_id": self.rx_id,
            "customer_id": self.customer_id,
            "phone": self.phone,
            "date": self.prescribed_on.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "od": self.od.as_dict(),
            "os": self.os.as_dict(),
        }


@dataclass(frozen=True)
class LensLimits:
    sph_min: Decimal
    sph_max: Decimal
    cyl_abs_max: Decimal
    add_required: bool
    # These are optional only so that a catalog which does not publish ADD
    # bounds remains representable. The supplied catalog publishes both.
    add_min: Optional[Decimal] = None
    add_max: Optional[Decimal] = None


@dataclass(frozen=True)
class LensOption:
    variant_id: str
    lens_type: str
    index: Decimal
    coating: str
    limits: LensLimits


@dataclass(frozen=True)
class LensConfiguration:
    lens_type: str
    index: Decimal
    coating: str

    @classmethod
    def from_data(cls, data: Dict[str, Any]) -> "LensConfiguration":
        return cls(
            lens_type=str(data["lens_type"]),
            index=Decimal(str(data["index"])),
            coating=str(data["coating"]),
        )


@dataclass(frozen=True)
class LensValidationResult:
    errors: Tuple[str, ...]
    option: Optional[LensOption]

    @property
    def is_valid(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class CatalogVariant:
    """The small, POS-owned projection of a Shopify product variant."""

    variant_id: str
    product_id: str
    line_type: str
    hsn: str
    price_paise: int
    active: bool
    lens_type: Optional[str] = None
    index: Optional[Decimal] = None
    coating: Optional[str] = None
    product_title: Optional[str] = None
    variant_title: Optional[str] = None


@dataclass(frozen=True)
class CartLine:
    variant_id: str
    quantity: int
    pair_ref: Optional[str] = None
    prescription_id: Optional[str] = None
    prescription_assistance_requested: bool = False


@dataclass(frozen=True)
class Cart:
    """A pre-payment cart. Product facts always come from the catalog."""

    store_code: str
    location_id: str
    lines: Tuple[CartLine, ...]


@dataclass(frozen=True)
class OfferDecision:
    offer_id: str
    status: str
    reason: str
    discount_paise: int = 0


@dataclass(frozen=True)
class OfferEvaluation:
    eligible: Tuple[OfferDecision, ...]
    applied: Tuple[OfferDecision, ...]
    rejected: Tuple[OfferDecision, ...]
    line_discounts_paise: Tuple[int, ...]

    @property
    def total_discount_paise(self) -> int:
        return sum(self.line_discounts_paise)


@dataclass(frozen=True)
class PricedLine:
    cart_line: CartLine
    catalog_variant: CatalogVariant
    gross_paise: int
    discount_paise: int
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int
    tax_paise: int
    payable_paise: int
    rate_bps: int


@dataclass(frozen=True)
class CartPricing:
    lines: Tuple[PricedLine, ...]
    offers: OfferEvaluation
    gross_paise: int
    discount_paise: int
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int
    tax_paise: int
    payable_paise: int

    def reconciles(self) -> bool:
        return (
            self.gross_paise - self.discount_paise == self.payable_paise
            and self.taxable_paise + self.tax_paise == self.payable_paise
            and self.cgst_paise + self.sgst_paise == self.tax_paise
            and sum(line.payable_paise for line in self.lines) == self.payable_paise
        )
