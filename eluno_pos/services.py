"""Application services composed from domain types and adapter protocols."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re
import threading
from typing import Any, List, Optional, Protocol, Sequence, Tuple

from .models import Customer, LensConfiguration, LensOption, LensValidationResult, Prescription


def customer_name_parts(value: str) -> Tuple[str, str]:
    name = " ".join(value.split())
    if not name or len(name) > 100 or any(ord(character) < 32 for character in name):
        raise ValueError("enter a customer name between 1 and 100 characters")
    if not any(character.isalpha() for character in name):
        raise ValueError("customer name must include at least one letter")
    parts = name.split(" ", 1)
    return parts[0], parts[1] if len(parts) == 2 else ""


def normalize_customer_phone(value: str) -> str:
    compact = value.strip().replace(" ", "").replace("-", "")
    if len(compact) == 10 and compact.isdigit():
        compact = "+91" + compact
    if not re.fullmatch(r"\+91[6-9]\d{9}", compact):
        raise ValueError("enter a valid 10-digit Indian mobile number, optionally prefixed with +91")
    return compact


class CustomerSource(Protocol):
    def search_customers_by_phone(self, phone: str) -> List[dict]:
        ...


class PrescriptionSource(Protocol):
    def prescriptions_by_phone(self, phone: str) -> List[dict]:
        ...


class LensCatalogSource(Protocol):
    def list_lens_options(self) -> List[LensOption]:
        ...


@dataclass(frozen=True)
class CustomerPrescriptionResult:
    code: str
    message: str
    customer: Optional[Customer] = None
    prescription: Optional[Prescription] = None

    @property
    def is_ready(self) -> bool:
        return self.code == "ready"


class CustomerPrescriptionService:
    def __init__(self, customers: CustomerSource, prescriptions: PrescriptionSource) -> None:
        self._customers = customers
        self._prescriptions = prescriptions

    def lookup(self, phone: str, business_date: date) -> CustomerPrescriptionResult:
        matches = [Customer.from_data(item) for item in self._customers.search_customers_by_phone(phone)]
        if not matches:
            return CustomerPrescriptionResult("customer_not_found", "No customer was found for this phone number.")
        if len(matches) > 1:
            return CustomerPrescriptionResult(
                "customer_ambiguous", "More than one customer matches this phone number; select the customer before billing."
            )
        customer = matches[0]
        raw_prescriptions = self._prescriptions.prescriptions_by_phone(phone)
        prescriptions = [Prescription.from_data(item) for item in raw_prescriptions]
        mismatched = [
            prescription for prescription in prescriptions
            if prescription.customer_id != customer.customer_id or prescription.phone != customer.phone
        ]
        if mismatched:
            return CustomerPrescriptionResult(
                "prescription_mismatch",
                "Prescription data does not belong to the selected customer; ask an optometrist to verify the record.",
                customer=customer,
            )
        valid = [
            prescription for prescription in prescriptions
            if prescription.prescribed_on <= business_date <= prescription.valid_until
        ]
        if not valid:
            return CustomerPrescriptionResult(
                "no_valid_prescription",
                "No valid prescription is on file for this date; ask the customer to complete or confirm an eye test.",
                customer=customer,
            )
        newest = max(valid, key=lambda prescription: prescription.prescribed_on)
        return CustomerPrescriptionResult("ready", "Customer and latest valid prescription found.", customer, newest)


class LensValidator:
    """Pure lens-matrix and prescription validator."""

    def validate(
        self,
        configuration: LensConfiguration,
        prescription: Prescription,
        options: Sequence[LensOption],
    ) -> LensValidationResult:
        option = next(
            (
                item for item in options
                if item.lens_type == configuration.lens_type
                and item.index == configuration.index
                and item.coating == configuration.coating
            ),
            None,
        )
        if option is None:
            return LensValidationResult(
                (
                    "{} {} with {} coating is not available in the lens matrix.".format(
                        configuration.lens_type, configuration.index, configuration.coating
                    ),
                ),
                None,
            )

        errors = []
        for eye_name, eye in (("Right eye", prescription.od), ("Left eye", prescription.os)):
            if eye.sph is None:
                errors.append("{} sphere is missing from the prescription.".format(eye_name))
            elif not option.limits.sph_min <= eye.sph <= option.limits.sph_max:
                errors.append(
                    "{} sphere {} is outside this lens range ({} to {}).".format(
                        eye_name, eye.sph, option.limits.sph_min, option.limits.sph_max
                    )
                )
            if eye.cyl is None:
                errors.append("{} cylinder is missing from the prescription.".format(eye_name))
            elif abs(eye.cyl) > option.limits.cyl_abs_max:
                errors.append(
                    "{} cylinder {} exceeds this lens limit of {}.".format(
                        eye_name, eye.cyl, option.limits.cyl_abs_max
                    )
                )
            if eye.axis is None:
                if eye.cyl is not None and eye.cyl != 0:
                    errors.append(
                        "{} axis is required when cylinder is non-zero; enter the prescribed axis in 5-degree increments.".format(
                            eye_name
                        )
                    )
            elif isinstance(eye.axis, bool) or not isinstance(eye.axis, int):
                errors.append(
                    "{} axis must be an integer from 0 to 180 degrees in 5-degree increments.".format(
                        eye_name
                    )
                )
            elif not 0 <= eye.axis <= 180:
                errors.append(
                    "{} axis {} is outside the supported range of 0 to 180 degrees.".format(
                        eye_name, eye.axis
                    )
                )
            # The brief makes ADD mandatory for these lens families. Honor a
            # catalog-required ADD too, so a future lens family can declare
            # the same constraint without changing this validator.
            add_required = option.lens_type in ("progressive", "bifocal") or option.limits.add_required
            if add_required and eye.add is None:
                errors.append(
                    "{} ADD is required for {} lenses.".format(eye_name, option.lens_type)
                )
            elif add_required and option.limits.add_min is not None and eye.add < option.limits.add_min:
                errors.append(
                    "{} ADD {} is below this lens minimum of {}.".format(
                        eye_name, eye.add, option.limits.add_min
                    )
                )
            elif add_required and option.limits.add_max is not None and eye.add > option.limits.add_max:
                errors.append(
                    "{} ADD {} exceeds this lens maximum of {}.".format(
                        eye_name, eye.add, option.limits.add_max
                    )
                )
        return LensValidationResult(tuple(errors), option)


class PosApplication:
    """Thin composition root used by the HTTP API."""

    def __init__(self, lookup_service: CustomerPrescriptionService, catalog: LensCatalogSource, checkout_service=None, refund_service=None, till_store=None, report_service=None, document_service=None, anomaly_service=None, shopify_adapter=None, smt_adapter=None, terminal_adapter=None, local_mock_configured=False, pricing_service=None, auth_manager=None, customer_accounts=None, customer_orders=None) -> None:
        self.lookup_service = lookup_service
        self.catalog = catalog
        self.lens_validator = LensValidator()
        self.checkout_service = checkout_service
        self.refund_service = refund_service
        self.till_store = till_store
        self.report_service = report_service
        self.document_service = document_service
        self.anomaly_service = anomaly_service
        # Kept at the composition boundary so read-only operational probes use
        # the very same configured clients as the checkout application.
        self.shopify_adapter = shopify_adapter
        self.smt_adapter = smt_adapter
        self.terminal_adapter = terminal_adapter
        self.pricing_service = pricing_service
        self.auth_manager = auth_manager
        self.customer_accounts = customer_accounts
        self.customer_orders = customer_orders
        self.customer_registration_lock = threading.Lock()
        self.local_mock_configured = local_mock_configured
        self.mock_payment_readiness = None
        self.mock_payment_readiness_lock = threading.Lock()

    def lookup_customer_prescription(self, phone: str, business_date: date) -> CustomerPrescriptionResult:
        return self.lookup_service.lookup(phone, business_date)

    def validate_lens(self, configuration: LensConfiguration, prescription: Prescription) -> LensValidationResult:
        return self.lens_validator.validate(configuration, prescription, self.catalog.list_lens_options())
