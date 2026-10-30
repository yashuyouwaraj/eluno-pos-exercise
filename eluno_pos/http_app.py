"""Small JSON HTTP API for the implemented POS foundation workflows."""

from datetime import date
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import sqlite3
import ssl
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type
from urllib.parse import parse_qs, unquote, urlparse, urlsplit
from uuid import uuid4

from .auth import AuthenticationThrottled, AuthManager, DuplicateUsername, StaffAccountError, validate_password
from .customer_accounts import (
    CustomerAccountError,
    CustomerAccountStore,
    CustomerLoginThrottled,
    CustomerOrderConflict,
    CustomerOrderStore,
)
from .business_time import bengaluru_today, business_timezone, business_timezone_name
from .adapters.http import HttpRequestError
from .adapters.shopify import (
    DEFAULT_SHOPIFY_BASE_URL,
    CustomerRegistrationConflict,
    ShopifyAdminClient,
)
from .adapters.smt import DEFAULT_SMT_BASE_URL, SmtClient
from .adapters.terminal import DEFAULT_TERMINAL_BASE_URL, PaymentTerminalClient
from .cart import CartService
from .checkout import (
    CheckoutDependencyError,
    CheckoutError,
    CheckoutRequest,
    CheckoutResult,
    CheckoutService,
    IdempotencyConflict,
    SaleStore,
)
from .models import CatalogVariant, Cart, CartLine, LensConfiguration, Prescription, axis_from_data
from .offers import OfferBook, OfferEngine
from .services import (
    CustomerPrescriptionService,
    PosApplication,
    customer_name_parts,
    normalize_customer_phone,
)
from .operations import OperationsError, OperationsStore, RefundService
from .reporting import DayEndReportService, ReportError
from .tax import TaxRateTable
from .documents import DocumentError, DocumentService
from .anomalies import AnomalyService
from .money import paise_from_decimal, decimal_string_from_paise


class PosHttpServer(ThreadingHTTPServer):
    """Do not print Windows connection-reset noise from short health probes."""
    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], ConnectionResetError):
            return
        super().handle_error(request, client_address)


def default_application() -> PosApplication:
    business_timezone()
    shopify_url = os.environ.get("ELUNO_SHOPIFY_BASE_URL", DEFAULT_SHOPIFY_BASE_URL)
    smt_url = os.environ.get("ELUNO_SMT_BASE_URL", DEFAULT_SMT_BASE_URL)
    terminal_url = os.environ.get("ELUNO_TERMINAL_BASE_URL", DEFAULT_TERMINAL_BASE_URL)
    shopify = ShopifyAdminClient(base_url=shopify_url)
    smt = SmtClient(base_url=smt_url)
    root = Path(__file__).resolve().parents[1]
    carts = CartService(
        shopify, OfferEngine(OfferBook.from_file(root / "starter-kit" / "data" / "offers.json")),
        TaxRateTable.from_file(root / "starter-kit" / "data" / "tax_rates.json"),
    )
    terminal = PaymentTerminalClient(base_url=terminal_url)
    state_path = Path(os.environ.get("ELUNO_STATE_DB", str(root / "eluno_pos_state.sqlite3")))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    operations = OperationsStore(state_path)
    lookup = CustomerPrescriptionService(shopify, smt)
    checkout = CheckoutService(
        carts, terminal, shopify, shopify, SaleStore(state_path), shopify, operations,
        prescription_lookup=lookup, lens_catalog=shopify, inventory=shopify,
    )
    reports = DayEndReportService(shopify, operations, checkout._sales)
    anomalies = AnomalyService(shopify, operations, checkout._sales, smt)
    mock_port = os.environ.get("ELUNO_LOCAL_MOCK_PORT", "8787")
    local_mock_configured = (
        os.environ.get("ELUNO_LOCAL_MOCK_MODE") == "1"
        and _matches_local_mock_url(shopify_url, "/admin/api/2026-10", mock_port)
        and _matches_local_mock_url(smt_url, "/smt", mock_port)
        and _matches_local_mock_url(terminal_url, "/terminal", mock_port)
    )
    refunds = RefundService(
        shopify,
        operations,
        operations,
        terminal if local_mock_configured else None,
    )
    return PosApplication(
        lookup, shopify, checkout, refunds, operations, reports,
        DocumentService(checkout._sales, operations, shopify, smt), anomalies,
        shopify, smt, terminal, local_mock_configured, pricing_service=carts,
        auth_manager=AuthManager.from_env(state_path),
        customer_accounts=CustomerAccountStore(state_path),
        customer_orders=CustomerOrderStore(state_path),
    )


def _matches_local_mock_url(value: str, path: str, port: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
            and parsed.port == int(port) and parsed.path.rstrip("/") == path
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment
        )
    except ValueError:
        return False


def _validated_delivery_address(value: Dict[str, Any]) -> Dict[str, str]:
    allowed = {"line1", "line2", "city", "state", "postal_code"}
    if set(value) - allowed:
        raise ValueError("Delivery address contains unsupported fields.")
    normalized = {}
    for key in allowed:
        raw = value.get(key, "")
        if not isinstance(raw, str) or len(raw.strip()) > 200:
            raise ValueError("Delivery address fields must be text of at most 200 characters.")
        normalized[key] = raw.strip()
    if not all(normalized[key] for key in ("line1", "city", "state")):
        raise ValueError("Address line, city, and state are required.")
    if not re.fullmatch(r"\d{6}", normalized["postal_code"]):
        raise ValueError("Enter a valid 6-digit Indian postal code.")
    return normalized


DIAGNOSTIC_CHARGE_ID = "eluno-pos-read-only-status-probe-never-charge"


def _adapter_failure_status(exc: Exception) -> str:
    """Classify a failed read probe without returning possibly sensitive URLs."""
    if isinstance(exc, HttpRequestError) and (exc.status is None or exc.status >= 500):
        return "UNAVAILABLE"
    return "UNKNOWN"


def system_status(application: PosApplication) -> Dict[str, Any]:
    """Safely probe configured adapters; each probe is independent and GET-only."""
    components = []
    environment = "LOCAL_MOCK" if application.local_mock_configured else "PRODUCTION_OR_UNKNOWN"

    try:
        if application.shopify_adapter is None:
            raise ValueError("Shopify adapter is not configured")
        application.shopify_adapter.list_locations()
        components.append({"name": "Shopify adapter", "status": "HEALTHY", "explanation": "Read-only locations query succeeded.", "required": True})
    except (HttpRequestError, ValueError) as exc:
        components.append({"name": "Shopify adapter", "status": _adapter_failure_status(exc), "explanation": "Read-only locations query could not be verified.", "required": True})

    try:
        if application.smt_adapter is None:
            raise ValueError("Store-tool adapter is not configured")
        application.smt_adapter.list_staff()
        components.append({"name": "Store-tool adapter", "status": "HEALTHY", "explanation": "Read-only staff query succeeded.", "required": True})
    except (HttpRequestError, ValueError) as exc:
        components.append({"name": "Store-tool adapter", "status": _adapter_failure_status(exc), "explanation": "Read-only staff query could not be verified.", "required": True})

    try:
        if application.terminal_adapter is None:
            raise ValueError("Payment terminal adapter is not configured")
        # The reserved identifier is never a payment request.  The supplied
        # terminal's explicit 404 is evidence that this GET route responded.
        result = application.terminal_adapter.get_charge(DIAGNOSTIC_CHARGE_ID)
        if isinstance(result, dict):
            components.append({"name": "Payment terminal", "status": "UNKNOWN", "explanation": "Diagnostic charge identifier unexpectedly exists; payment readiness was not tested.", "required": not application.local_mock_configured})
        else:
            components.append({"name": "Payment terminal", "status": "UNKNOWN", "explanation": "Read-only charge lookup returned an unexpected result; payment readiness was not tested.", "required": not application.local_mock_configured})
    except HttpRequestError as exc:
        if exc.status == 404:
            components.append({"name": "Payment terminal", "status": "REACHABLE", "explanation": "Read-only charge lookup responded with unknown charge; payment readiness was not tested.", "required": not application.local_mock_configured})
        else:
            components.append({"name": "Payment terminal", "status": _adapter_failure_status(exc), "explanation": "Read-only charge lookup could not be verified; payment readiness was not tested.", "required": not application.local_mock_configured})
    except ValueError:
        components.append({"name": "Payment terminal", "status": "UNKNOWN", "explanation": "Read-only charge lookup returned an unexpected result; payment readiness was not tested.", "required": not application.local_mock_configured})

    if application.local_mock_configured:
        readiness = application.mock_payment_readiness or {
            "status": "UNKNOWN",
            "explanation": "The opt-in local mock payment check has not been run.",
        }
        components.append({
            "name": "Local mock payment flow",
            "status": readiness["status"],
            "explanation": readiness["explanation"],
            "required": True,
        })
        components.append({
            "name": "Real payment terminal",
            "status": "NOT_CONFIGURED",
            "explanation": "This process is configured for the supplied local mock only.",
            "required": False,
        })
    else:
        components.append({
            "name": "Local mock payment flow",
            "status": "NOT_CONFIGURED",
            "explanation": "Configured integrations are not positively identified as the supplied local mock.",
            "required": False,
        })
        components.append({
            "name": "Real payment terminal",
            "status": "UNKNOWN",
            "explanation": "A real terminal readiness check is not supported by the available read-only contract.",
            "required": True,
        })
    return {"environment": environment, "components": components}


def verify_local_mock_payment(application: PosApplication) -> Dict[str, Any]:
    """Opt-in fixed-value authorization against only the supplied loopback mock."""
    if not application.local_mock_configured:
        raise PermissionError("Mock payment verification is only enabled for the configured supplied localhost mock.")
    with application.mock_payment_readiness_lock:
        if application.mock_payment_readiness is not None:
            return application.mock_payment_readiness
        application.mock_payment_readiness = {
            "status": "UNKNOWN",
            "explanation": "Mock payment verification is in progress or has an unresolved result; do not repeat it.",
        }
        if application.terminal_adapter is None:
            result = {
                "status": "UNKNOWN",
                "explanation": "Payment terminal adapter is not configured.",
            }
        else:
            order_ref = "eluno-readiness-" + uuid4().hex
            try:
                charge = application.terminal_adapter.charge(100, "upi", order_ref)
                charge_id = charge.get("charge_id") if isinstance(charge, dict) else None
                charge_matches = (
                    charge_id
                    and charge.get("status") == "captured"
                    and charge.get("amount_paise") == 100
                    and charge.get("method") == "upi"
                    and charge.get("order_ref") == order_ref
                )
                if not charge_matches:
                    result = {
                        "status": "UNKNOWN",
                        "explanation": "Mock authorization did not return the expected captured test charge; inspect the local mock before any retry.",
                    }
                else:
                    confirmed = application.terminal_adapter.get_charge(str(charge_id))
                    if (
                        isinstance(confirmed, dict)
                        and confirmed.get("charge_id") == charge_id
                        and confirmed.get("status") == "captured"
                        and confirmed.get("amount_paise") == 100
                        and confirmed.get("method") == "upi"
                        and confirmed.get("order_ref") == order_ref
                    ):
                        result = {
                            "status": "HEALTHY",
                            "explanation": "Fixed Rs. 1.00 UPI authorization and read-back succeeded on the local mock; no Shopify order or sale ledger entry was created.",
                        }
                    else:
                        result = {
                            "status": "UNKNOWN",
                            "explanation": "The mock charge was created but could not be confirmed; do not repeat until the mock is inspected.",
                        }
            except HttpRequestError as exc:
                result = {
                    "status": "UNKNOWN" if exc.status is None else "UNAVAILABLE",
                    "explanation": "Mock payment verification could not be confirmed; do not repeat until the mock is inspected.",
                }
        application.mock_payment_readiness = result
        return result


def make_handler(application: PosApplication) -> Type[BaseHTTPRequestHandler]:
    class ApiHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/auth/me":
                self._auth_me()
                return
            if parsed.path == "/api/customer-auth/me":
                self._customer_auth_me()
                return
            if parsed.path.startswith("/api/customer/"):
                if parsed.path not in ("/api/customer/catalog", "/api/customer/locations") and not self._authorize_customer():
                    return
                if parsed.path == "/api/customer/profile":
                    self._customer_profile()
                    return
                if parsed.path == "/api/customer/orders":
                    self._customer_orders()
                    return
                if parsed.path == "/api/customer/catalog":
                    self._customer_catalog()
                    return
                if parsed.path == "/api/customer/locations":
                    self._customer_locations()
                    return
            if parsed.path.startswith("/api/") and not self._authorize_api("GET", parsed.path):
                return
            if parsed.path == "/":
                self._serve_pos_asset("index.html")
                return
            if parsed.path == "/api":
                self._send_json(200, {
                    "service": "eluno-pos", "description": "Local Eluno point-of-sale API.",
                    "health": "/health",
                    "endpoint_groups": {
                        "customer_and_prescription": ["/api/customer-prescription", "/api/prescriptions", "/api/prescription-approvers", "/api/customers"],
                        "customer_accounts": ["/api/customer-auth/register", "/api/customer-auth/login", "/api/customer-auth/logout", "/api/customer-auth/me"],
                        "customer_self_service": ["/api/customer/profile", "/api/customer/catalog", "/api/customer/locations", "/api/customer/pricing", "/api/customer/orders"],
                        "store_order_review": ["/api/store/orders/pending", "/api/store/orders/{order_id}/confirm", "/api/store/orders/{order_id}/deny"],
                        "optometrist_order_assistance": ["/api/optometrist/orders/assistance"],
                        "store_context": ["/api/locations"],
                        "catalog_and_offers": ["/api/catalog/variants", "/api/offers"],
                        "admin_catalog_inventory": ["/api/admin/catalog/products", "/api/admin/catalog/frames", "/api/admin/inventory/receipts"],
                        "admin_staff": ["/api/admin/staff", "/api/admin/staff/{staff_id}"],
                        "checkout": ["/api/checkouts", "/api/checkouts/{sale_id}"],
                        "refunds": ["/api/refunds/lookup", "/api/refunds", "/api/refunds/{refund_id}"],
                        "cash_drawer": ["/api/tills", "/api/tills/{session_id}"],
                        "reports": ["/api/reports/day-end", "/api/reports/overall", "/api/reports/anomalies"],
                        "audit": ["/api/audit-events"],
                        "documents": ["/api/invoices/{sale_id}", "/api/invoices/lookup?invoice_no=...", "/api/credit-notes/{refund_id}"],
                        "system_status": ["/api/system-status", "/api/system-status/mock-payment-test"],
                    },
                    "cashier": "Start the browser POS with: python scripts/start_local.py; optional CLI: python scripts/start_local.py --cli",
                })
                return
            if parsed.path == "/health":
                self._send_json(200, {"ok": True, "service": "eluno-pos"})
                return
            if parsed.path in ("/pos", "/pos/"):
                self._serve_pos_asset("index.html")
                return
            if parsed.path.startswith("/pos/"):
                self._serve_pos_asset(parsed.path[len("/pos/"):])
                return
            if parsed.path == "/api/system-status":
                self._send_json(200, system_status(application))
                return
            if parsed.path == "/api/business-date":
                self._send_json(200, {
                    "business_date": bengaluru_today().isoformat(),
                    "timezone": business_timezone_name(),
                })
                return
            if parsed.path == "/api/customer-prescription":
                self._customer_prescription(parse_qs(parsed.query))
                return
            if parsed.path == "/api/prescriptions":
                self._prescriptions(parse_qs(parsed.query))
                return
            if parsed.path == "/api/prescription-approvers":
                self._prescription_approvers(parse_qs(parsed.query))
                return
            if parsed.path == "/api/customers/search":
                self._customer_search(parse_qs(parsed.query))
                return
            if parsed.path.startswith("/api/customers/"):
                self._staff_customer_profile(parsed.path.rsplit("/", 1)[-1], parse_qs(parsed.query))
                return
            if parsed.path == "/api/catalog/variants":
                self._catalog_variants(parse_qs(parsed.query))
                return
            if parsed.path == "/api/locations":
                self._admin_locations()
                return
            if parsed.path == "/api/admin/locations":
                self._admin_locations()
                return
            if parsed.path == "/api/admin/catalog/frames":
                self._admin_frames(parse_qs(parsed.query))
                return
            if parsed.path == "/api/admin/catalog/products":
                self._admin_products(parse_qs(parsed.query))
                return
            if parsed.path == "/api/admin/staff":
                self._admin_staff()
                return
            if parsed.path == "/api/admin/tills":
                self._admin_till_sessions(parse_qs(parsed.query))
                return
            if parsed.path == "/api/store/orders/pending":
                self._store_pending_orders()
                return
            if parsed.path == "/api/optometrist/orders/assistance":
                self._optometrist_assistance_orders()
                return
            if parsed.path == "/api/offers":
                self._available_offers(parse_qs(parsed.query))
                return
            if parsed.path == "/api/refunds/lookup":
                self._refund_lookup(parse_qs(parsed.query))
                return
            if parsed.path == "/api/invoices/lookup":
                self._invoice_lookup(parse_qs(parsed.query))
                return
            if parsed.path.startswith("/api/invoices/"):
                self._document(parsed.path, "invoice"); return
            if parsed.path == "/api/tills":
                self._list_open_tills(parse_qs(parsed.query))
                return
            if parsed.path.startswith("/api/credit-notes/"):
                self._document(parsed.path, "credit_note"); return
            if parsed.path.startswith("/api/checkouts/"):
                self._checkout_status(parsed.path)
                return
            if parsed.path.startswith("/api/refunds/"):
                self._refund_status(parsed.path)
                return
            if parsed.path.startswith("/api/tills/"):
                self._till_status(parsed.path)
                return
            if parsed.path == "/api/reports/day-end":
                self._day_end(parse_qs(parsed.query)); return
            if parsed.path == "/api/reports/overall":
                self._overall_report(parse_qs(parsed.query)); return
            if parsed.path == "/api/reports/anomalies":
                self._anomalies(parse_qs(parsed.query)); return
            if parsed.path == "/api/audit-events":
                self._audit_events(parse_qs(parsed.query)); return
            self._send_json(404, {"error": "not_found", "message": "Endpoint not found."})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/auth/login":
                if not self._check_same_origin():
                    return
                self._auth_login()
                return
            if path == "/api/auth/logout":
                if not self._check_same_origin():
                    return
                self._auth_logout()
                return
            if path in ("/api/customer-auth/register", "/api/customer-auth/login", "/api/customer-auth/logout"):
                if not self._check_same_origin():
                    return
                if path.endswith("/register"):
                    self._customer_register()
                elif path.endswith("/login"):
                    self._customer_login()
                else:
                    self._customer_logout()
                return
            if path.startswith("/api/customer/"):
                if not self._check_same_origin() or not self._authorize_customer():
                    return
                if path == "/api/customer/pricing":
                    self._customer_pricing()
                    return
                if path == "/api/customer/orders":
                    self._customer_create_order()
                    return
                if path == "/api/customer/password":
                    self._customer_change_password()
                    return
            if path.startswith("/api/") and not self._authorize_api("POST", path):
                return
            if not self._check_same_origin():
                return
            if path == "/api/system-status/mock-payment-test":
                try:
                    self._read_json_object()
                except (TypeError, ValueError) as exc:
                    self._send_json(400, {"error": "invalid_request", "message": str(exc)})
                    return
                try:
                    self._send_json(200, verify_local_mock_payment(application))
                except PermissionError as exc:
                    self._send_json(403, {"error": "mock_payment_test_forbidden", "message": str(exc)})
                return
            if path == "/api/customers":
                self._register_customer()
                return
            if path.startswith("/api/customers/") and path.endswith("/anonymize"):
                self._anonymize_customer(path)
                return
            if path == "/api/admin/catalog/products":
                self._admin_create_product()
                return
            if path == "/api/admin/inventory/receipts":
                self._admin_receive_stock()
                return
            if path == "/api/admin/staff":
                self._admin_create_staff()
                return
            if path.startswith("/api/store/orders/"):
                self._store_order_decision(path)
                return
            if path == "/api/prescriptions":
                self._register_prescription()
                return
            if path == "/api/checkouts":
                self._checkout()
                return
            if path.startswith("/api/checkouts/") and path.endswith("/recover"):
                self._recover_checkout(path)
                return
            if path == "/api/refunds": self._refund(); return
            if path.startswith("/api/refunds/") and path.endswith("/recover"): self._recover_refund(path); return
            if path.startswith("/api/admin/tills/") and path.endswith("/review"): self._review_till(path); return
            if path == "/api/tills": self._open_till(); return
            if path.startswith("/api/tills/") and path.endswith("/movements"): self._till_movement(path); return
            if path.startswith("/api/tills/") and path.endswith("/close"): self._close_till(path); return
            if path == "/api/billing-anomalies": self._billing_anomalies(); return
            if path == "/api/pricing-preview": self._pricing_preview(); return
            if path != "/api/lens-validation":
                self._send_json(404, {"error": "not_found", "message": "Endpoint not found."})
                return
            try:
                body = self._read_json_object()
                configuration = LensConfiguration.from_data(body["lens"])
                prescription = Prescription.from_data(body["prescription"])
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                self._send_json(400, {"error": "invalid_request", "message": "Invalid lens or prescription: {}".format(exc)})
                return
            try:
                result = application.validate_lens(configuration, prescription)
            except (HttpRequestError, ValueError) as exc:
                self._send_json(502, {"error": "catalog_unavailable", "message": str(exc)})
                return
            warnings = []
            for eye_name, eye in (("OD", prescription.od), ("OS", prescription.os)):
                if eye.axis is not None and eye.axis % 5 != 0:
                    warnings.append("{} axis {} is off the 5-degree grid and requires optometrist review.".format(eye_name, eye.axis))
            if result.is_valid:
                self._send_json(200, {"valid": True, "errors": [], "warnings": warnings, "variant_id": result.option.variant_id})
            else:
                self._send_json(422, {"valid": False, "errors": list(result.errors), "warnings": warnings})

        def do_PATCH(self) -> None:
            path = urlparse(self.path).path
            if path.startswith("/api/customer/"):
                if not self._check_same_origin() or not self._authorize_customer():
                    return
                if path == "/api/customer/profile":
                    self._customer_update_profile()
                    return
            if path.startswith("/api/") and not self._authorize_api("PATCH", path):
                return
            if not self._check_same_origin():
                return
            if path.startswith("/api/admin/staff/"):
                self._admin_update_staff(path)
                return
            if path.startswith("/api/admin/catalog/products/"):
                self._admin_update_product(path)
                return
            if path.startswith("/api/customers/"):
                self._update_customer(path)
                return
            self._send_json(404, {"error": "not_found", "message": "Endpoint not found."})

        def _pricing_preview(self):
            try:
                if application.pricing_service is None:
                    raise ValueError("pricing service is not configured")
                body = self._read_json_object()
                lines = tuple(CartLine(
                    str(line["variant_id"]), line["quantity"], line.get("pair_ref"), line.get("prescription_id")
                ) for line in body["cart"]["lines"])
                cart = Cart(str(body["cart"]["store_code"]), str(body["cart"]["location_id"]), lines)
                self._validate_operator_store(cart.store_code, cart.location_id)
                tags = frozenset()
                if body.get("customer_id") and body.get("customer_phone"):
                    matches = application.shopify_adapter.search_customers_by_phone(str(body["customer_phone"]))
                    matches = [item for item in matches if str(item.get("id")) == str(body["customer_id"])]
                    if len(matches) == 1 and isinstance(matches[0].get("tags"), str):
                        tags = frozenset(tag.strip() for tag in matches[0]["tags"].split(",") if tag.strip())
                offer_ids = body.get("offer_ids")
                if offer_ids is not None and (
                    not isinstance(offer_ids, list)
                    or any(not isinstance(offer_id, str) or not offer_id for offer_id in offer_ids)
                ):
                    raise ValueError("offer_ids must be an array of non-empty offer codes")
                pricing = application.pricing_service.price(
                    cart, bengaluru_today(), tags,
                    tuple(offer_ids) if offer_ids is not None else None,
                )
                evaluation = pricing.offers
                offer_catalog = getattr(application.pricing_service, "offer_catalog", None)
                offer_metadata = {
                    offer.offer_id: offer
                    for offer in offer_catalog()
                } if callable(offer_catalog) else {}
                eligible = {decision.offer_id: decision for decision in evaluation.eligible}
                rejected = {decision.offer_id: decision for decision in evaluation.rejected}
                applied = {decision.offer_id: decision for decision in evaluation.applied}
                decisions = []
                for offer_id in sorted(set(eligible) | set(rejected) | set(applied)):
                    if offer_id in applied:
                        decision = applied[offer_id]
                    elif offer_id in eligible:
                        decision = eligible[offer_id]
                        if offer_id in rejected:
                            decision = rejected[offer_id]
                        decisions.append({
                            "offer_id": offer_id,
                            "name": offer_metadata[offer_id].name if offer_id in offer_metadata else offer_id,
                            "stackable": offer_metadata[offer_id].stackable if offer_id in offer_metadata else None,
                            "status": "eligible",
                            "reason": decision.reason,
                            "discount_paise": 0,
                        })
                        continue
                    else:
                        decision = rejected[offer_id]
                    decisions.append({
                        "offer_id": decision.offer_id,
                        "name": offer_metadata[offer_id].name if offer_id in offer_metadata else offer_id,
                        "stackable": offer_metadata[offer_id].stackable if offer_id in offer_metadata else None,
                        "status": decision.status,
                        "reason": decision.reason,
                        "discount_paise": decision.discount_paise,
                    })
                self._send_json(200, {
                    "gross_paise": pricing.gross_paise,
                    "discount_paise": pricing.discount_paise,
                    "taxable_paise": pricing.taxable_paise,
                    "cgst_paise": pricing.cgst_paise,
                    "sgst_paise": pricing.sgst_paise,
                    "tax_paise": pricing.tax_paise,
                    "payable_paise": pricing.payable_paise,
                    "lines": [{
                        "variant_id": line.cart_line.variant_id,
                        "quantity": line.cart_line.quantity,
                        "gross_paise": line.gross_paise,
                        "discount_paise": line.discount_paise,
                        "taxable_paise": line.taxable_paise,
                        "cgst_paise": line.cgst_paise,
                        "sgst_paise": line.sgst_paise,
                        "tax_paise": line.tax_paise,
                        "payable_paise": line.payable_paise,
                    } for line in pricing.lines],
                    "offers": [{"offer_id": decision.offer_id, "discount_paise": decision.discount_paise}
                               for decision in evaluation.applied],
                    "offer_evaluation": decisions,
                })
            except PermissionError as exc:
                self._send_json(403, {"error": "store_access_denied", "message": str(exc)})
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                self._send_json(400, {"error": "pricing_invalid", "message": str(exc)})
            except (HttpRequestError, CheckoutDependencyError) as exc:
                self._send_json(502, {"error": "pricing_upstream_unavailable", "message": str(exc)})

        def _billing_anomalies(self):
            try:
                body=self._read_json_object()
                if application.anomaly_service is None: raise ReportError('anomaly reporting is not configured')
                self._send_json(200,application.anomaly_service.billing_flags(str(body['phone']),str(body['rx_id']),body['lens']))
            except (KeyError,TypeError,ValueError,ReportError) as exc:
                self._send_json(422,{"error":"billing_anomaly_invalid","message":str(exc)})

        def _checkout(self) -> None:
            if application.checkout_service is None:
                self._send_json(503, {"error": "checkout_unavailable", "message": "Checkout is not configured."})
                return
            try:
                body = self._read_json_object()
                lines = tuple(
                    CartLine(
                        variant_id=str(line["variant_id"]), quantity=line["quantity"],
                        pair_ref=line.get("pair_ref"), prescription_id=line.get("prescription_id"),
                    ) for line in body["cart"]["lines"]
                )
                cart = Cart(str(body["cart"]["store_code"]), str(body["cart"]["location_id"]), lines)
                self._validate_operator_store(cart.store_code, cart.location_id)
                request = CheckoutRequest(
                    idempotency_key=str(body["idempotency_key"]),
                    cart=cart,
                    business_date=bengaluru_today(),
                    payment_method=str(body["payment_method"]), channel=str(body["channel"]),
                    staff_id=self.authenticated_user.staff_id, customer_id=(str(body["customer_id"]) if body.get("customer_id") is not None else None),
                    # customer_tags is intentionally ignored: offer eligibility
                    # is resolved from Shopify for the supplied customer phone.
                    customer_phone=(str(body["customer_phone"]) if body.get("customer_phone") is not None else None),
                    till_session_id=(str(body["till_session_id"]) if body.get("till_session_id") is not None else None),
                    selected_offer_ids=(
                        tuple(str(offer_id) for offer_id in body["offer_ids"])
                        if body.get("offer_ids") is not None else None
                    ),
                    reviewed_prescription_ids=(
                        tuple(body["reviewed_prescription_ids"])
                        if body.get("reviewed_prescription_ids") is not None else ()
                    ),
                )
                if body.get("offer_ids") is not None and (
                    not isinstance(body["offer_ids"], list)
                    or any(not isinstance(offer_id, str) or not offer_id for offer_id in body["offer_ids"])
                ):
                    raise ValueError("offer_ids must be an array of non-empty offer codes")
                if body.get("reviewed_prescription_ids") is not None and (
                    not isinstance(body["reviewed_prescription_ids"], list)
                    or any(not isinstance(rx_id, str) or not rx_id for rx_id in body["reviewed_prescription_ids"])
                ):
                    raise ValueError("reviewed_prescription_ids must be an array of prescription IDs")
            except PermissionError as exc:
                self._send_json(403, {"error": "store_access_denied", "message": str(exc)})
                return
            except HttpRequestError as exc:
                self._send_json(502, {"error": "store_directory_unavailable", "message": str(exc)})
                return
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                self._send_json(400, {"error": "invalid_request", "message": "Invalid checkout request: {}".format(exc)})
                return
            try:
                result = application.checkout_service.checkout(request)
            except IdempotencyConflict as exc:
                self._send_json(409, {"error": "idempotency_conflict", "message": str(exc)})
                return
            except CheckoutDependencyError as exc:
                self._send_json(502, {"error": "checkout_dependency_unavailable", "message": str(exc)})
                return
            except CheckoutError as exc:
                self._send_json(422, {"error": "checkout_invalid", "message": str(exc)})
                return
            status = 201 if result.status == "COMPLETED" else (402 if result.status == "PAYMENT_DECLINED" else 202)
            self._send_json(status, {
                "sale_id": result.sale_id, "status": result.status, "order_id": result.order_id,
                "invoice_no": result.invoice_no, "message": result.message,
            })

        def _checkout_status(self, path: str) -> None:
            prefix = "/api/checkouts/"
            sale_id = path[len(prefix):] if path.startswith(prefix) else path
            if not sale_id or "/" in sale_id:
                self._send_json(404, {"error": "not_found", "message": "Checkout was not found."})
                return
            try:
                result = application.checkout_service.sale_status(sale_id)
            except CheckoutError as exc:
                self._send_json(404, {"error": "not_found", "message": str(exc)})
                return
            self._send_checkout_result(200, result)

        def _invoice_lookup(self, query):
            try:
                invoice_no=self._single_query_value(query,'invoice_no')
                if not invoice_no:
                    raise ValueError('invoice_no is required')
                result=application.checkout_service.invoice_status(invoice_no)
                if result is None:
                    self._send_json(404,{"error":"not_found","message":"Invoice number was not found in the POS sale ledger."})
                    return
                self._send_json(200,{
                    "sale_id":result.sale_id,"status":result.status,
                    "order_id":result.order_id,"invoice_no":result.invoice_no,
                })
            except ValueError as exc:
                self._send_json(400,{"error":"invalid_request","message":str(exc)})

        def _catalog_variants(self, query):
            line_type = self._single_query_value(query, "line_type")
            if line_type not in ("frame", "lens"):
                self._send_json(400, {
                    "error": "invalid_request",
                    "message": "line_type must be frame or lens.",
                })
                return
            try:
                variants = [
                    {
                        "variant_id": variant.variant_id,
                        "line_type": variant.line_type,
                        "product_title": variant.product_title,
                        "variant_title": variant.variant_title,
                        "price_paise": variant.price_paise,
                        "lens_type": variant.lens_type,
                        "index": str(variant.index) if variant.index is not None else None,
                        "coating": variant.coating,
                    }
                    for variant in application.catalog.list_catalog_variants()
                    if variant.active and variant.line_type == line_type
                ]
            except (HttpRequestError, ValueError) as exc:
                self._send_json(502, {"error": "catalog_unavailable", "message": str(exc)})
                return
            self._send_json(200, {"variants": variants})

        def _admin_locations(self) -> None:
            if application.shopify_adapter is None:
                self._send_json(503, {"error": "catalog_unavailable", "message": "Shopify catalog is not configured."})
                return
            try:
                locations = application.shopify_adapter.list_locations()
            except (HttpRequestError, ValueError) as exc:
                self._send_json(502, {"error": "catalog_unavailable", "message": str(exc)})
                return
            available = [
                {"id": location["id"], "name": location["name"],
                 "store_code": (location.get("eluno") or {}).get("store_code")}
                for location in locations
                if location.get("active") is True
                and (location.get("eluno") or {}).get("kind") == "store"
            ]
            if self.authenticated_user.role in ("cashier", "optometrist"):
                available = [
                    location for location in available
                    if location["store_code"] == self.authenticated_user.store_code
                ]
            self._send_json(200, {"locations": available})

        def _admin_frames(self, query) -> None:
            if application.shopify_adapter is None:
                self._send_json(503, {"error": "catalog_unavailable", "message": "Shopify catalog is not configured."})
                return
            try:
                location_text = self._single_query_value(query, "location_id")
                location_id = None
                if location_text:
                    location_id = int(location_text)
                    locations = application.shopify_adapter.list_locations()
                    if not any(
                        item.get("id") == location_id
                        and item.get("active") is True
                        and (item.get("eluno") or {}).get("kind") == "store"
                        for item in locations
                    ):
                        raise ValueError("choose an active Eluno store location")
                tracked_frames = [
                    (product, variant)
                    for product in application.shopify_adapter.list_products("Frame")
                    if product.get("status") == "active"
                    for variant in product.get("variants", [])
                    if (variant.get("eluno") or {}).get("line_type") == "frame"
                    and variant.get("inventory_item_id")
                ]
                available_by_item = {}
                if location_id is not None and tracked_frames:
                    item_ids = list({
                        variant["inventory_item_id"] for _, variant in tracked_frames
                    })
                    available_by_item = {
                        level["inventory_item_id"]: level["available"]
                        for level in application.shopify_adapter.list_inventory_levels(item_ids, location_id)
                        if level.get("location_id") == location_id
                        and level.get("inventory_item_id") in item_ids
                        and isinstance(level.get("available"), int)
                    }
                variants = [{
                    "variant_id": variant["id"],
                    "inventory_item_id": variant["inventory_item_id"],
                    "product_title": product.get("title", ""),
                    "variant_title": variant.get("title", ""),
                    "sku": variant.get("sku", ""),
                    "price_paise": paise_from_decimal(str(variant["price"])),
                    "category": (variant.get("eluno") or {}).get("category"),
                    "material": (variant.get("eluno") or {}).get("material"),
                    "size": (variant.get("eluno") or {}).get("size"),
                    "available": available_by_item.get(variant["inventory_item_id"]) if location_id is not None else None,
                    "location_id": location_id,
                } for product, variant in tracked_frames]
            except (HttpRequestError, KeyError, TypeError, ValueError) as exc:
                status = 400 if isinstance(exc, ValueError) and not isinstance(exc, HttpRequestError) else 502
                self._send_json(status, {"error": "catalog_unavailable", "message": str(exc)})
                return
            self._send_json(200, {"variants": variants})

        def _admin_products(self, query) -> None:
            if application.shopify_adapter is None:
                self._send_json(503, {"error": "catalog_unavailable", "message": "Shopify catalog is not configured."})
                return
            line_type=self._single_query_value(query,"line_type")
            search=(self._single_query_value(query,"q") or "").strip().casefold()
            if line_type not in (None,"frame","lens") or len(search)>100:
                self._send_json(400,{"error":"invalid_catalog_filter","message":"Choose a supported product type and search term."})
                return
            try:
                products=application.shopify_adapter.list_products()
                variants=[]
                for product in products:
                    for variant in product.get("variants",[]):
                        metadata=variant.get("eluno") or {}
                        kind=metadata.get("line_type")
                        if kind not in ("frame","lens") or (line_type and kind!=line_type):
                            continue
                        row={
                            "product_id":str(product["id"]),
                            "variant_id":str(variant["id"]),
                            "line_type":kind,
                            "title":str(product.get("title") or ""),
                            "description":str(product.get("body_html") or ""),
                            "status":str(product.get("status") or "active"),
                            "sku":str(variant.get("sku") or ""),
                            "variant_title":str(variant.get("title") or ""),
                            "price_paise":paise_from_decimal(str(variant["price"])),
                            "category":metadata.get("category"),
                            "material":metadata.get("material"),
                            "size":metadata.get("size"),
                            "rx_able":metadata.get("rx_able"),
                            "lens_type":metadata.get("lens_type"),
                            "index":metadata.get("index"),
                            "coating":metadata.get("coating"),
                        }
                        if not search or search in " ".join(str(value or "") for value in row.values()).casefold():
                            variants.append(row)
            except (HttpRequestError,KeyError,TypeError,ValueError,InvalidOperation) as exc:
                self._send_json(502,{"error":"catalog_unavailable","message":str(exc)})
                return
            self._send_json(200,{"products":variants})

        def _admin_update_product(self,path):
            prefix="/api/admin/catalog/products/"
            product_id=unquote(path[len(prefix):])
            if not product_id.isascii() or not product_id.isdecimal():
                self._send_json(400,{"error":"invalid_product_id","message":"Product ID must be numeric."})
                return
            if application.shopify_adapter is None:
                self._send_json(503,{"error":"catalog_unavailable","message":"Shopify catalog is not configured."})
                return
            try:
                body=self._read_json_object()
                allowed={
                    "variant_id","title","description","status","sku","variant_title",
                    "price_paise","category","material","size","rx_able",
                    "lens_type","index","coating",
                }
                if not body or set(body)-allowed:
                    raise ValueError("Provide supported catalog fields to update.")
                product=application.shopify_adapter.get_product(int(product_id))
                variants=product.get("variants")
                if not isinstance(variants,list):
                    raise ValueError("Product has no supported variants.")
                variant=next(
                    (item for item in variants if str(item.get("id"))==str(body.get("variant_id"))),
                    None,
                )
                if variant is None:
                    raise ValueError("Choose a variant belonging to this product.")
                metadata=variant.get("eluno")
                if not isinstance(metadata,dict) or metadata.get("line_type") not in ("frame","lens"):
                    raise ValueError("Product does not have supported Eluno optical metadata.")
                line_type=metadata["line_type"]
                updates={}
                if "title" in body:
                    title=body["title"]
                    if not isinstance(title,str) or not title.strip() or len(title.strip())>120:
                        raise ValueError("Product name must contain 1–120 characters.")
                    updates["title"]=title.strip()
                if "description" in body:
                    description=body["description"]
                    if not isinstance(description,str) or len(description)>5000:
                        raise ValueError("Description must be text of at most 5,000 characters.")
                    updates["body_html"]=description
                if "status" in body:
                    if body["status"] not in ("active","archived"):
                        raise ValueError("Product status must be active or archived.")
                    updates["status"]=body["status"]
                variant_updates={"id":variant["id"]}
                if "sku" in body:
                    sku=body["sku"]
                    if not isinstance(sku,str) or not sku.strip() or len(sku.strip())>120:
                        raise ValueError("SKU must contain 1–120 characters.")
                    for other_product in application.shopify_adapter.list_products():
                        for other in other_product.get("variants",[]):
                            if str(other.get("id"))!=str(variant["id"]) and str(other.get("sku") or "").casefold()==sku.strip().casefold():
                                self._send_json(409,{"error":"duplicate_sku","message":"That SKU is already in the catalog."})
                                return
                    variant_updates["sku"]=sku.strip()
                if "variant_title" in body:
                    value=body["variant_title"]
                    if not isinstance(value,str) or not value.strip() or len(value.strip())>120:
                        raise ValueError("Variant name must contain 1–120 characters.")
                    variant_updates["title"]=value.strip()
                    variant_updates["option1"]=value.strip()
                if "price_paise" in body:
                    price=body["price_paise"]
                    if isinstance(price,bool) or not isinstance(price,int) or not 1<=price<=10**12:
                        raise ValueError("Selling price must be a positive integer paise amount.")
                    variant_updates["price"]=decimal_string_from_paise(price)
                changed_metadata=dict(metadata)
                if line_type=="frame":
                    if "category" in body:
                        if body["category"] not in ("optical","sunglasses"):
                            raise ValueError("Frame category must be optical or sunglasses.")
                        changed_metadata["category"]=body["category"]
                        changed_metadata["hsn"]="90031900" if body["category"]=="optical" else "90049000"
                        updates["tags"]="frame,"+body["category"]
                    for field,maximum in (("material",80),("size",40)):
                        if field in body:
                            value=body[field]
                            if not isinstance(value,str) or not value.strip() or len(value.strip())>maximum:
                                raise ValueError("{} must contain 1–{} characters.".format(field.capitalize(),maximum))
                            changed_metadata[field]=value.strip()
                    if "rx_able" in body:
                        if not isinstance(body["rx_able"],bool):
                            raise ValueError("rx_able must be true or false.")
                        changed_metadata["rx_able"]=body["rx_able"]
                else:
                    choices={
                        "lens_type":("single_vision","bifocal","progressive"),
                        "index":("1.50","1.56","1.60","1.67","1.74"),
                        "coating":("standard","anti_reflective","blue_cut","photochromic"),
                    }
                    for field,values in choices.items():
                        if field in body:
                            value=str(body[field])
                            if value not in values:
                                raise ValueError("{} is not supported.".format(field.replace("_"," ").capitalize()))
                            changed_metadata[field]=float(value) if field=="index" else value
                    if "lens_type" in body and bool(changed_metadata["lens_type"] in ("bifocal","progressive")):
                        limits=dict(changed_metadata.get("rx_limits") or {})
                        limits["add_required"]=True
                        changed_metadata["rx_limits"]=limits
                if changed_metadata!=metadata:
                    variant_updates["eluno"]=changed_metadata
                updates["variants"]=[variant_updates]
                updated=application.shopify_adapter.update_product(int(product_id),updates)
            except HttpRequestError as exc:
                status=409 if exc.status==409 else (422 if exc.status==422 else 502)
                message=str(exc)
                if exc.status is None:
                    message="Product update outcome is unknown; inspect the catalog before retrying. "+message
                self._send_json(status,{"error":"product_update_failed","message":message})
                return
            except (KeyError,TypeError,ValueError,InvalidOperation) as exc:
                self._send_json(400,{"error":"invalid_product_update","message":str(exc)})
                return
            try:
                application.till_store.audit(
                    self.authenticated_user.staff_id,"CATALOG_PRODUCT_UPDATED","product",product_id,
                    metadata={"variant_id":str(variant["id"]),"line_type":line_type,
                              "fields":sorted(key for key in body if key!="variant_id")},
                )
            except (OSError,sqlite3.Error,OperationsError) as exc:
                self._send_json(500,{"error":"catalog_audit_failed","message":"Product was updated but its audit record failed; inspect the catalog before retrying. {}".format(exc)})
                return
            self._send_json(200,{"product":updated})

        def _admin_create_product(self) -> None:
            if application.shopify_adapter is None:
                self._send_json(503, {"error": "catalog_unavailable", "message": "Shopify catalog is not configured."})
                return
            try:
                body = self._read_json_object()
                line_type = body.get("line_type")
                title = body.get("title")
                sku = body.get("sku")
                variant_title = body.get("variant_title")
                price_paise = body.get("price_paise")
                actor = self.authenticated_user.staff_id
                if line_type not in ("frame", "lens"):
                    raise ValueError("choose frame or lens")
                if not isinstance(actor, str) or not actor.strip() or len(actor.strip()) > 100:
                    raise ValueError("operator ID is required")
                for label, value in (("product name", title), ("SKU", sku), ("variant name", variant_title)):
                    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120:
                        raise ValueError("{} is required and must be at most 120 characters".format(label))
                if any(character.isspace() for character in sku.strip()):
                    raise ValueError("SKU cannot contain whitespace")
                if not isinstance(price_paise, int) or isinstance(price_paise, bool) or not 1 <= price_paise <= 100000000:
                    raise ValueError("price must be a positive amount in paise")
                existing_products = application.shopify_adapter.list_products()
                existing_skus = {
                    str(variant.get("sku", "")).casefold()
                    for product in existing_products
                    for variant in product.get("variants", [])
                }
                if sku.strip().casefold() in existing_skus:
                    self._send_json(409, {"error": "duplicate_sku", "message": "That SKU is already in the catalog."})
                    return

                if line_type == "frame":
                    category = body.get("category")
                    if category not in ("optical", "sunglasses"):
                        raise ValueError("frame category must be optical or sunglasses")
                    material = body.get("material")
                    size = body.get("size")
                    rx_able = body.get("rx_able")
                    if not isinstance(material, str) or not material.strip() or len(material.strip()) > 80:
                        raise ValueError("frame material is required")
                    if not isinstance(size, str) or not size.strip() or len(size.strip()) > 40:
                        raise ValueError("frame size is required")
                    if not isinstance(rx_able, bool):
                        raise ValueError("rx_able must be true or false")
                    metadata = {
                        "line_type": "frame",
                        "hsn": "90031900" if category == "optical" else "90049000",
                        "category": category,
                        "material": material.strip(),
                        "size": size.strip(),
                        "rx_able": rx_able,
                    }
                    product_type = "Frame"
                    tags = "frame," + category
                    inventory_management = "shopify"
                else:
                    lens_type = body.get("lens_type")
                    index = body.get("index")
                    coating = body.get("coating")
                    if lens_type not in ("single_vision", "bifocal", "progressive"):
                        raise ValueError("lens type must be single_vision, bifocal, or progressive")
                    if index not in ("1.50", "1.56", "1.60", "1.67", "1.74"):
                        raise ValueError("lens index is not supported")
                    if coating not in ("standard", "anti_reflective", "blue_cut", "photochromic"):
                        raise ValueError("lens coating is not supported")
                    configuration_exists = any(
                        (variant.get("eluno") or {}).get("line_type") == "lens"
                        and (variant.get("eluno") or {}).get("lens_type") == lens_type
                        and Decimal(str((variant.get("eluno") or {}).get("index"))) == Decimal(index)
                        and (variant.get("eluno") or {}).get("coating") == coating
                        for product in existing_products
                        for variant in product.get("variants", [])
                    )
                    if configuration_exists:
                        self._send_json(409, {
                            "error": "duplicate_lens_configuration",
                            "message": "That lens type, index, and coating already exists in the catalog.",
                        })
                        return
                    sph_limits = {
                        "1.50": (-4.0, 4.0, 2.0), "1.56": (-4.0, 4.0, 2.0),
                        "1.60": (-8.0, 6.0, 4.0), "1.67": (-10.0, 8.0, 4.0),
                        "1.74": (-14.0, 10.0, 6.0),
                    }
                    sph_min, sph_max, cyl_abs_max = sph_limits[index]
                    metadata = {
                        "line_type": "lens", "hsn": "90015000",
                        "lens_type": lens_type, "index": float(index), "coating": coating,
                        "rx_limits": {
                            "sph_min": sph_min, "sph_max": sph_max,
                            "cyl_abs_max": cyl_abs_max,
                            "add_required": lens_type != "single_vision",
                            "add_min": 0.75, "add_max": 3.0,
                        },
                    }
                    product_type = "Lens"
                    tags = "lens,ophthalmic"
                    inventory_management = None

                product = application.shopify_adapter.create_product({
                    "title": title.strip(),
                    "vendor": "Eluno",
                    "product_type": product_type,
                    "status": "active",
                    "tags": tags,
                    "variants": [{
                        "sku": sku.strip(),
                        "title": variant_title.strip(),
                        "option1": variant_title.strip(),
                        "price": "{:.2f}".format(price_paise / 100),
                        "inventory_management": inventory_management,
                        "inventory_policy": "deny",
                        "requires_shipping": False,
                        "taxable": True,
                        "eluno": metadata,
                    }],
                })
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                self._send_json(400, {"error": "invalid_product", "message": str(exc)})
                return
            except HttpRequestError as exc:
                status = 409 if exc.status == 409 else (422 if exc.status == 422 else 502)
                message = str(exc)
                if exc.status is None:
                    message = "Product creation outcome is unknown; check the SKU in the catalog before retrying. " + message
                self._send_json(status, {"error": "product_creation_failed", "message": message})
                return
            if application.till_store is None:
                self._send_json(503, {
                    "error": "audit_unavailable",
                    "message": "Product was created, but audit storage is unavailable; inspect the catalog before retrying.",
                })
                return
            try:
                variant = product["variants"][0]
                application.till_store.audit(
                    actor.strip(), "CATALOG_PRODUCT_CREATED", "product", str(product["id"]),
                    metadata={
                        "product_type": product_type,
                        "sku": variant["sku"],
                        "variant_id": variant["id"],
                        "price_paise": price_paise,
                    },
                )
            except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as exc:
                self._send_json(500, {
                    "error": "catalog_audit_failed",
                    "message": "Product was created, but its audit record failed; inspect the catalog before retrying. {}".format(exc),
                })
                return
            self._send_json(201, {"product": product})

        def _admin_receive_stock(self) -> None:
            if application.shopify_adapter is None:
                self._send_json(503, {"error": "inventory_unavailable", "message": "Shopify inventory is not configured."})
                return
            try:
                body = self._read_json_object()
                inventory_item_id = body.get("inventory_item_id")
                location_id = body.get("location_id")
                quantity = body.get("quantity")
                actor = self.authenticated_user.staff_id
                reference = body.get("reference")
                if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0
                       for value in (inventory_item_id, location_id, quantity)):
                    raise ValueError("inventory item, store location, and positive received quantity are required")
                if not isinstance(actor, str) or not actor.strip() or len(actor.strip()) > 100:
                    raise ValueError("receiving operator ID is required")
                if not isinstance(reference, str) or not reference.strip() or len(reference.strip()) > 120:
                    raise ValueError("delivery / goods-received reference is required")
                locations = application.shopify_adapter.list_locations()
                location = next((item for item in locations if item.get("id") == location_id
                                 and item.get("active") is True
                                 and (item.get("eluno") or {}).get("kind") == "store"), None)
                if location is None:
                    raise ValueError("choose an active Eluno store location")
                tracked_frame = any(
                    variant.get("inventory_item_id") == inventory_item_id
                    and (variant.get("eluno") or {}).get("line_type") == "frame"
                    for product in application.shopify_adapter.list_products("Frame")
                    for variant in product.get("variants", [])
                )
                if not tracked_frame:
                    raise ValueError("stock receipts are only supported for tracked frame variants")
                application.shopify_adapter.connect_inventory_level(inventory_item_id, location_id)
                level, duplicate_receipt = application.shopify_adapter.adjust_inventory(
                    inventory_item_id, location_id, quantity,
                    idempotency_key="{}:{}:{}".format(
                        reference.strip(), inventory_item_id, location_id,
                    ),
                )
            except (KeyError, TypeError, ValueError) as exc:
                self._send_json(400, {"error": "invalid_stock_receipt", "message": str(exc)})
                return
            except HttpRequestError as exc:
                status = 409 if exc.status == 409 else (422 if exc.status == 422 else 502)
                message = str(exc)
                if exc.status is None:
                    message = "Stock receipt outcome is unknown; check the store quantity before retrying. " + message
                self._send_json(status, {"error": "stock_receipt_failed", "message": message})
                return
            if application.till_store is None:
                self._send_json(503, {
                    "error": "audit_unavailable",
                    "message": "Stock was received, but audit storage is unavailable; inspect inventory before retrying.",
                })
                return
            receipt_entity = "{}@{}".format(inventory_item_id, location_id)
            try:
                already_audited = any(
                    event.get("action") == "FRAME_STOCK_RECEIVED"
                    and event.get("reason") == reference.strip()
                    for event in application.till_store.audit_events(receipt_entity)
                )
                if not already_audited:
                    application.till_store.audit(
                        actor.strip(), "FRAME_STOCK_RECEIVED", "inventory_item",
                        receipt_entity,
                        reason=reference.strip(),
                        metadata={"quantity": quantity, "reference": reference.strip()},
                    )
            except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as exc:
                self._send_json(500, {
                    "error": "inventory_audit_failed",
                    "message": "Stock was received, but its audit record failed; inspect inventory before retrying. {}".format(exc),
                })
                return
            self._send_json(200, {
                "inventory_level": level,
                "received_quantity": quantity,
                "idempotent_replay": duplicate_receipt,
            })

        def _available_offers(self, query):
            if application.checkout_service is None:
                self._send_json(503, {"error": "offers_unavailable", "message": "Offer evaluation is not configured."})
                return
            try:
                offers = application.checkout_service.selectable_offers(bengaluru_today())
            except ValueError as exc:
                self._send_json(400, {"error": "invalid_request", "message": str(exc)})
                return
            self._send_json(200, {"offers": [
                {"offer_id": offer.offer_id, "name": offer.name, "stackable": offer.stackable}
                for offer in offers
            ]})

        def _list_open_tills(self, query):
            try:
                store_code=self._single_query_value(query,'store_code')
                if not store_code:
                    raise ValueError('store_code is required')
                self._validate_store_code(store_code)
                self._send_json(200,{"tills":application.till_store.open_tills(store_code)})
            except PermissionError as exc:
                self._send_json(403,{"error":"store_access_denied","message":str(exc)})
            except (ValueError, HttpRequestError) as exc:
                self._send_json(400,{"error":"invalid_request","message":str(exc)})

        def _admin_till_sessions(self, query):
            try:
                store_code=self._single_query_value(query,'store_code')
                if store_code:
                    self._validate_store_code(store_code)
                self._send_json(200,{
                    "sessions":application.till_store.list_till_sessions(store_code),
                })
            except PermissionError as exc:
                self._send_json(403,{"error":"store_access_denied","message":str(exc)})
            except HttpRequestError as exc:
                self._send_json(502,{"error":"store_directory_unavailable","message":str(exc)})
            except ValueError as exc:
                self._send_json(422,{"error":"invalid_request","message":str(exc)})

        def _review_till(self,path):
            session_id=unquote(path[len("/api/admin/tills/"):-len("/review")].strip("/"))
            try:
                body=self._read_json_object()
                record=application.till_store.review_till(
                    session_id,self.authenticated_user.staff_id,body.get("review_note"),
                )
                self._send_json(200,record)
            except OperationsError as exc:
                self._send_json(422,{"error":"till_review_invalid","message":str(exc)})
            except (TypeError,ValueError) as exc:
                self._send_json(400,{"error":"till_review_invalid","message":str(exc)})

        def _refund(self):
            if application.refund_service is None: self._send_json(503,{"error":"refund_unavailable"}); return
            try:
                b=self._read_json_object()
                self._validate_store_code(str(b["store_code"]))
                return_item_ids=b.get("return_item_ids",[])
                if not isinstance(return_item_ids,list) or any(
                    not isinstance(item,str) or not item for item in return_item_ids
                ):
                    raise ValueError("return_item_ids must be an array of line item IDs")
                r=application.refund_service.refund(
                    b['order_id'],str(b['store_code']),self.authenticated_user.staff_id,str(b['reason']),
                    b.get('till_session_id'),b.get('items'),b.get('idempotency_key'),
                    invoice_no=b.get('invoice_no'),
                    return_item_ids=return_item_ids,
                )
            except PermissionError as e:
                self._send_json(403,{"error":"store_access_denied","message":str(e)}); return
            except HttpRequestError as e:
                self._send_json(502,{"error":"refund_upstream_unavailable","message":str(e)}); return
            except (KeyError,TypeError,ValueError,OperationsError) as e: self._send_json(422,{"error":"refund_invalid","message":str(e)}); return
            self._send_json(201 if r['status']=='COMPLETED' else 202,r)
        def _refund_lookup(self,query):
            if application.refund_service is None:
                self._send_json(503,{"error":"refund_unavailable","message":"Refund lookup is not configured."})
                return
            identifier=self._single_query_value(query,'identifier')
            store_code=self._single_query_value(query,'store_code')
            if not identifier:
                self._send_json(400,{"error":"invalid_request","message":"invoice number or Shopify order ID is required."})
                return
            invoice_no=None
            order_id=identifier
            if not identifier.isascii() or not identifier.isdecimal():
                if application.checkout_service is None:
                    self._send_json(503,{"error":"invoice_lookup_unavailable","message":"The POS invoice ledger is unavailable; use the Shopify order ID or restore the POS ledger."})
                    return
                try:
                    sale=application.checkout_service.invoice_status(identifier)
                except CheckoutError as exc:
                    self._send_json(409,{"error":"invoice_ambiguous","message":str(exc)})
                    return
                if sale is None:
                    self._send_json(404,{"error":"invoice_not_found","message":"Invoice was not found in the POS sale ledger. Verify the invoice or use its Shopify order ID."})
                    return
                if sale.invoice_no!=identifier:
                    self._send_json(409,{"error":"invoice_mapping_invalid","message":"The POS ledger returned a different invoice number; do not proceed with this refund."})
                    return
                if sale.status!="COMPLETED" or not sale.order_id:
                    self._send_json(409,{"error":"invoice_order_unavailable","message":"This invoice is not linked to a completed Shopify order. Reconcile the sale before refunding."})
                    return
                invoice_no=identifier
                order_id=str(sale.order_id)
            try:
                preview=application.refund_service.preview(order_id,store_code or None,invoice_no)
            except OperationsError as exc:
                code="invoice_order_mismatch" if invoice_no and "invoice number does not match" in str(exc) else "refund_lookup_invalid"
                status=409 if code=="invoice_order_mismatch" else 422
                self._send_json(status,{"error":code,"message":str(exc)})
                return
            except HttpRequestError as exc:
                self._send_json(502,{"error":"refund_lookup_upstream_unavailable","message":str(exc)})
                return
            except (KeyError,TypeError,ValueError,InvalidOperation) as exc:
                self._send_json(422,{"error":"refund_lookup_invalid","message":"Shopify returned an incomplete refund-order record: {}".format(exc)})
                return
            try:
                self._validate_store_code(preview["store_code"])
            except PermissionError as exc:
                self._send_json(403, {"error": "store_access_denied", "message": str(exc)})
                return
            except (HttpRequestError, ValueError) as exc:
                self._send_json(502, {"error": "store_directory_unavailable", "message": str(exc)})
                return
            self._send_json(200,preview)
        def _document(self,path,kind):
            ident=path.rsplit('/',1)[-1]
            if not ident or '/' in ident or application.document_service is None:
                self._send_json(404,{"error":"not_found"}); return
            try:
                html=application.document_service.invoice_html(ident) if kind=="invoice" else application.document_service.credit_note_html(ident)
            except (DocumentError, CheckoutError, OperationsError, HttpRequestError, ValueError) as e:
                self._send_json(404,{"error":"document_unavailable","message":str(e)}); return
            self._send_html(200,html)
        def _refund_status(self,path):
            try: self._send_json(200,application.refund_service.status(path.rsplit('/',1)[-1]))
            except OperationsError as e:self._send_json(404,{"error":"not_found","message":str(e)})
        def _recover_refund(self,path):
            try:self._send_json(202,application.refund_service.recover(path.split('/')[-2]))
            except OperationsError as e:self._send_json(422,{"error":"recovery_not_allowed","message":str(e)})
        def _open_till(self):
            try:
                b=self._read_json_object()
                self._validate_store_code(str(b["store_code"]), str(b["location_id"]))
                self._send_json(201,application.till_store.open_till(str(b['store_code']),str(b['location_id']),self.authenticated_user.staff_id,b['opening_float_paise']))
            except PermissionError as e:
                self._send_json(403,{"error":"store_access_denied","message":str(e)})
            except HttpRequestError as e:
                self._send_json(502,{"error":"store_directory_unavailable","message":str(e)})
            except (KeyError,TypeError,ValueError,OperationsError) as e:self._send_json(422,{"error":"till_invalid","message":str(e)})
        def _till_status(self,path):
            try:
                till=application.till_store.till_get(path.rsplit('/',1)[-1])
                self._validate_store_code(till["store_code"], till["location_id"])
                self._send_json(200,till)
            except PermissionError as e:self._send_json(403,{"error":"store_access_denied","message":str(e)})
            except OperationsError as e:self._send_json(404,{"error":"not_found","message":str(e)})
        def _day_end(self, query):
            try:
                store_id=self._single_query_value(query,'store_id'); date_text=self._single_query_value(query,'date')
                if not store_id or not date_text: raise ReportError('store_id and date are required')
                self._validate_store_code(self._store_code_for_location(store_id), store_id)
                self._send_json(200,application.report_service.report(store_id,date.fromisoformat(date_text)))
            except HttpRequestError:
                self._send_json(502,{"error":"report_upstream_unavailable","message":"The Shopify report data could not be retrieved. Retry the report after checking the Shopify connection."})
            except (ValueError,ReportError) as e:self._send_json(422,{"error":"report_invalid","message":str(e)})
        def _overall_report(self, query):
            try:
                date_text=self._single_query_value(query,'date')
                if not date_text: raise ReportError('date is required')
                self._send_json(200,application.report_service.overall_report(date.fromisoformat(date_text)))
            except HttpRequestError:
                self._send_json(502,{"error":"report_upstream_unavailable","message":"The Shopify report data could not be retrieved. Retry the report after checking the Shopify connection."})
            except (ValueError,ReportError) as e:self._send_json(422,{"error":"report_invalid","message":str(e)})
        def _anomalies(self, query):
            try:
                store_id=self._single_query_value(query,'store_id'); date_text=self._single_query_value(query,'date')
                if not store_id or not date_text: raise ReportError('store_id and date are required')
                self._validate_store_code(self._store_code_for_location(store_id), store_id)
                if application.anomaly_service is None: raise ReportError('anomaly reporting is not configured')
                self._send_json(200,application.anomaly_service.report(store_id,date.fromisoformat(date_text)))
            except HttpRequestError:
                self._send_json(502,{"error":"report_upstream_unavailable","message":"The Shopify report data could not be retrieved. Retry the report after checking the Shopify connection."})
            except (ValueError,ReportError) as e:self._send_json(422,{"error":"report_invalid","message":str(e)})
        def _audit_events(self, query):
            if application.till_store is None:
                self._send_json(503,{"error":"audit_unavailable","message":"Audit storage is not configured."}); return
            entity_id=self._single_query_value(query,'entity_id')
            self._send_json(200,{"events":application.till_store.audit_events(entity_id),"note":"Actors are assigned from the authenticated POS account."})
        def _till_movement(self,path):
            try:
                b=self._read_json_object(); kind=str(b['type'])
                if kind not in ('paid_in','paid_out'): raise OperationsError('cash_sale and cash_refund are system-recorded only')
                if not isinstance(b.get('reason'),str) or not b['reason'].strip(): raise OperationsError('reason is required for manual cash movements')
                till=application.till_store.till_get(path.split('/')[-2])
                self._validate_store_code(till["store_code"], till["location_id"])
                self._send_json(201,application.till_store.movement(path.split('/')[-2],kind,b['amount_paise'],self.authenticated_user.staff_id,b['reason'].strip(),b.get('ref')))
            except PermissionError as e:self._send_json(403,{"error":"store_access_denied","message":str(e)})
            except HttpRequestError as e:self._send_json(502,{"error":"store_directory_unavailable","message":str(e)})
            except (KeyError,TypeError,ValueError,OperationsError) as e:self._send_json(422,{"error":"till_invalid","message":str(e)})
        def _close_till(self,path):
            try:
                b=self._read_json_object()
                till=application.till_store.till_get(path.split('/')[-2])
                self._validate_store_code(till["store_code"], till["location_id"])
                closed=application.till_store.close_till(
                    path.split('/')[-2],self.authenticated_user.staff_id,b['counted_cash_paise'],
                    b.get("review_note"),
                )
                self._send_json(200,{
                    **closed,
                    "cash_variance_alert":application.till_store.cash_variance_alert(closed),
                })
            except PermissionError as e:self._send_json(403,{"error":"store_access_denied","message":str(e)})
            except HttpRequestError as e:self._send_json(502,{"error":"store_directory_unavailable","message":str(e)})
            except (KeyError,TypeError,ValueError,OperationsError) as e:self._send_json(422,{"error":"till_invalid","message":str(e)})

        def _recover_checkout(self, path: str) -> None:
            prefix = "/api/checkouts/"
            suffix = "/recover"
            sale_id = path[len(prefix):] if path.startswith(prefix) else path
            if sale_id.endswith(suffix):
                sale_id = sale_id[:-len(suffix)]
            if not sale_id or "/" in sale_id:
                self._send_json(404, {"error": "not_found", "message": "Checkout was not found."})
                return
            try:
                body=self._read_json_object()
                try:
                    result = application.checkout_service.recover(sale_id, self.authenticated_user.staff_id)
                except TypeError:
                    # Lightweight legacy/test checkout implementations may not
                    # yet accept an actor; the real service persists it.
                    result = application.checkout_service.recover(sale_id)
            except CheckoutError as exc:
                self._send_json(422, {"error": "recovery_not_allowed", "message": str(exc)})
                return
            self._send_checkout_result(202, result)

        def _send_checkout_result(self, status: int, result: CheckoutResult) -> None:
            self._send_json(status, {
                "sale_id": result.sale_id, "status": result.status, "order_id": result.order_id,
                "invoice_no": result.invoice_no, "message": result.message,
            })

        def _customer_prescription(self, query: Dict[str, Any]) -> None:
            phone = self._single_query_value(query, "phone")
            if not phone:
                self._send_json(400, {"error": "invalid_request", "message": "phone is required."})
                return
            try:
                result = application.lookup_customer_prescription(phone, bengaluru_today())
            except (HttpRequestError, ValueError) as exc:
                self._send_json(502, {"error": "upstream_error", "message": str(exc)})
                return
            status = {
                "ready": 200,
                "customer_not_found": 404,
                "customer_ambiguous": 409,
                "no_valid_prescription": 200,
                "prescription_mismatch": 502,
            }[result.code]
            payload = {"status": result.code, "message": result.message}
            if result.customer:
                payload["customer"] = {
                    "id": result.customer.customer_id,
                    "phone": result.customer.phone,
                    "first_name": result.customer.first_name,
                    "last_name": result.customer.last_name,
                    "tags": sorted(result.customer.tags),
                }
            if result.prescription:
                payload["prescription"] = result.prescription.as_dict()
            self._send_json(status, payload)

        def _prescriptions(self, query: Dict[str, Any]) -> None:
            phone = self._single_query_value(query, "phone")
            try:
                phone = normalize_customer_phone(phone or "")
                matches = application.shopify_adapter.search_customers_by_phone(phone)
                if len(matches) != 1:
                    self._send_json(404 if not matches else 409, {
                        "error": "customer_not_found" if not matches else "customer_ambiguous",
                        "message": "Prescription records require exactly one matching customer.",
                    })
                    return
                customer = matches[0]
                prescriptions = application.smt_adapter.prescriptions_by_phone(phone)
            except (HttpRequestError, ValueError, AttributeError) as exc:
                self._send_json(502, {"error": "prescription_lookup_failed", "message": str(exc)})
                return
            matching = [
                item for item in prescriptions
                if str(item.get("customer_id")) == str(customer.get("id"))
                and item.get("phone") == phone
            ]
            self._send_json(200, {
                "prescriptions": sorted(matching, key=lambda item: str(item.get("date") or ""), reverse=True),
            })

        def _prescription_approvers(self, query: Dict[str, Any]) -> None:
            store_code = self._single_query_value(query, "store_code")
            if not store_code:
                self._send_json(400, {"error": "invalid_request", "message": "store_code is required."})
                return
            if (
                self.authenticated_user.role in ("cashier", "optometrist")
                and store_code != self.authenticated_user.store_code
            ):
                self._send_json(403, {
                    "error": "store_access_denied",
                    "message": "The Optometrist directory is available only for your assigned store.",
                })
                return
            try:
                staff = application.smt_adapter.list_staff(store_code)
            except (HttpRequestError, ValueError, AttributeError) as exc:
                self._send_json(502, {"error": "prescription_approvers_unavailable", "message": str(exc)})
                return
            self._send_json(200, {"approvers": [
                {
                    "staff_id": str(item["staff_id"]),
                    "display_name": str(item.get("display_name") or item.get("name") or item["staff_id"]),
                }
                for item in staff
                if item.get("role") == "optometrist" and item.get("store_code") == store_code
            ]})

        def _customer_search(self, query: Dict[str, Any]) -> None:
            if application.shopify_adapter is None:
                self._send_json(503, {"error": "customer_search_unavailable", "message": "Customer records are not configured."})
                return
            term = self._single_query_value(query, "q")
            page_text = self._single_query_value(query, "page") or "1"
            if not term or len(term.strip()) < 2 or len(term.strip()) > 100:
                self._send_json(400, {
                    "error": "invalid_customer_search",
                    "message": "Search with at least 2 and at most 100 characters.",
                })
                return
            try:
                page = int(page_text)
                if page < 1:
                    raise ValueError("page must be a positive integer")
                if (
                    self.authenticated_user.role == "cashier"
                    and term.strip().isascii()
                    and term.strip().isdecimal()
                    and len(term.strip()) != 10
                ):
                    self._send_json(403, {
                        "error": "customer_id_search_admin_only",
                        "message": "Only Admins may search by customer ID.",
                    })
                    return
                customers, has_more = application.shopify_adapter.search_customers(
                    term.strip(), page=page, limit=20,
                )
                all_orders = application.shopify_adapter.list_all_orders()
                results = []
                for customer in customers:
                    full_name = " ".join(
                        part for part in (
                            str(customer.get("first_name") or "").strip(),
                            str(customer.get("last_name") or "").strip(),
                        ) if part
                    )
                    customer_orders = [
                        order for order in all_orders
                        if str((order.get("customer") or {}).get("id")) == str(customer.get("id"))
                        and not order.get("cancelled_at")
                        and order.get("financial_status") in (
                            "paid", "partially_refunded", "refunded",
                        )
                    ]
                    refunded_paise = sum(
                        paise_from_decimal(transaction.get("amount", "0"))
                        for order in customer_orders
                        for refund in order.get("refunds", [])
                        for transaction in refund.get("transactions", [])
                        if transaction.get("kind") == "refund"
                        and transaction.get("status") == "success"
                    )
                    latest_purchase = max(
                        (
                            str(order.get("processed_at") or order.get("created_at") or "")
                            for order in customer_orders
                        ),
                        default="",
                    ) or None
                    total_spent = customer.get("total_spent")
                    order_count = customer.get("orders_count")
                    if total_spent is None:
                        total_spent_paise = max(0, sum(
                            paise_from_decimal(order.get("total_price", "0"))
                            for order in customer_orders
                        ) - refunded_paise)
                    else:
                        total_spent_paise = paise_from_decimal(total_spent)
                    results.append({
                        "id": str(customer.get("id")),
                        "name": full_name or "Customer",
                        "phone": customer.get("phone"),
                        "email": customer.get("email"),
                        "total_spent_paise": total_spent_paise,
                        "order_count": order_count if order_count is not None else len(customer_orders),
                        "total_refunded_paise": refunded_paise,
                        "most_recent_purchase": customer.get("last_order_date") or latest_purchase,
                        "created_at": customer.get("created_at"),
                    })
            except HttpRequestError as exc:
                self._send_json(502, {"error": "customer_search_unavailable", "message": str(exc)})
                return
            except (TypeError, ValueError, InvalidOperation, AttributeError) as exc:
                self._send_json(502, {"error": "customer_search_invalid", "message": str(exc)})
                return
            self._send_json(200, {
                "customers": results,
                "page": page,
                "has_more": has_more,
            })

        def _staff_customer_profile(self, customer_id: str, query: Dict[str, Any]) -> None:
            if not customer_id or not customer_id.isascii() or not customer_id.isdecimal():
                self._send_json(400, {"error": "invalid_customer_id", "message": "Customer ID must be numeric."})
                return
            page_text = self._single_query_value(query, "page") or "1"
            try:
                page = int(page_text)
                if page < 1:
                    raise ValueError("page must be a positive integer")
            except ValueError as exc:
                self._send_json(400, {"error": "invalid_customer_profile_page", "message": str(exc)})
                return
            if application.shopify_adapter is None:
                self._send_json(503, {"error": "customer_unavailable", "message": "Customer records are not configured."})
                return
            try:
                customer = application.shopify_adapter.get_customer(customer_id)
                orders = application.shopify_adapter.list_orders(status="any", limit="250")
                records = [
                    order for order in orders
                    if str((order.get("customer") or {}).get("id")) == customer_id
                ]
                history = []
                lifetime_spend = 0
                completed_orders = []
                for order in sorted(records, key=lambda item: str(item.get("processed_at") or item.get("created_at") or "")):
                    lines = []
                    tax_total = cgst = sgst = discount_total = gross_total = 0
                    for line in order.get("line_items", []):
                        quantity = int(line.get("quantity", 0))
                        gross = paise_from_decimal(line.get("price", "0")) * quantity
                        discount = paise_from_decimal(line.get("total_discount", "0"))
                        tax = 0
                        line_cgst = line_sgst = 0
                        for tax_line in line.get("tax_lines", []):
                            amount = paise_from_decimal(tax_line.get("price", "0"))
                            tax += amount
                            if tax_line.get("title") == "CGST":
                                line_cgst += amount
                            elif tax_line.get("title") == "SGST":
                                line_sgst += amount
                        gross_total += gross
                        discount_total += discount
                        tax_total += tax
                        cgst += line_cgst
                        sgst += line_sgst
                        lines.append({
                            "title": str(line.get("title") or line.get("name") or line.get("sku") or "Item"),
                            "quantity": quantity,
                            "discount_paise": discount,
                            "tax_paise": tax,
                            "line_total_paise": max(0, gross - discount),
                        })
                    refunds = []
                    refunded_paise = 0
                    for refund in order.get("refunds", []):
                        amount = sum(
                            paise_from_decimal(tx.get("amount", "0"))
                            for tx in refund.get("transactions", [])
                            if tx.get("kind") == "refund"
                        )
                        refunded_paise += amount
                        refunds.append({
                            "id": str(refund.get("id")),
                            "created_at": refund.get("created_at"),
                            "amount_paise": amount,
                            "tax_paise": sum(
                                paise_from_decimal(item.get("total_tax", "0"))
                                for item in refund.get("refund_line_items", [])
                            ),
                            "reason": next((
                                attr.get("value") for attr in refund.get("note_attributes", [])
                                if attr.get("name") == "reason_code"
                            ), None),
                        })
                    if (
                        not order.get("cancelled_at")
                        and order.get("financial_status") in ("paid", "partially_refunded", "refunded")
                    ):
                        lifetime_spend += max(0, paise_from_decimal(order.get("total_price", "0")) - refunded_paise)
                        completed_orders.append(order)
                    attributes = {
                        attr.get("name"): attr.get("value")
                        for attr in order.get("note_attributes", [])
                        if isinstance(attr, dict)
                    }
                    sale_store = getattr(application.checkout_service, "_sales", None)
                    sale = (
                        sale_store.sale_for_order(str(order.get("id")))
                        if sale_store is not None and hasattr(sale_store, "sale_for_order") else None
                    )
                    sale_transactions = [
                        tx for tx in order.get("transactions", [])
                        if tx.get("kind") == "sale" and tx.get("status") == "success"
                    ]
                    history.append({
                        "order_id": str(order.get("id")),
                        "shopify_reference": str(order.get("name") or order.get("id")),
                        "invoice_no": attributes.get("invoice_no"),
                        "sale_id": sale.get("sale_id") if sale else None,
                        "date": order.get("processed_at") or order.get("created_at"),
                        "store_code": attributes.get("store_code"),
                        "items": lines,
                        "gross_paise": gross_total,
                        "discount_paise": discount_total,
                        "tax_paise": tax_total,
                        "cgst_paise": cgst,
                        "sgst_paise": sgst,
                        "total_paise": paise_from_decimal(order.get("total_price", "0")),
                        "refunded_paise": refunded_paise,
                        "payment_method": attributes.get("payment_method") or (
                            sale_transactions[0].get("gateway") if sale_transactions else None
                        ),
                        "status": order.get("financial_status") or "unknown",
                        "fulfillment_status": order.get("fulfillment_status") or "not recorded",
                        "refunds": refunds,
                    })
                prescriptions = []
                if application.smt_adapter is not None:
                    prescriptions = application.smt_adapter.prescriptions_by_customer(customer_id)
                history.reverse()
                page_size = 20
                first = (page - 1) * page_size
                page_purchases = history[first:first + page_size]
                self._send_json(200, {
                    "customer": self._customer_response(customer),
                    "purchase_count": len(completed_orders),
                    "lifetime_spend_paise": lifetime_spend,
                    "most_recent_purchase": (
                        max((
                            item.get("processed_at") or item.get("created_at") or ""
                            for item in completed_orders
                            if item.get("processed_at") or item.get("created_at")
                        ), default="") or None
                    ),
                    "history_complete": len(records) < 250,
                    "prescriptions": prescriptions,
                    "purchase_page": page,
                    "has_more_purchases": first + page_size < len(history),
                    "purchases": page_purchases,
                    "deletion_policy": (
                        "Customer deletion is handled as anonymization. Financial records, invoices, "
                        "Shopify orders, prescriptions, and audit history are retained."
                    ),
                })
            except (HttpRequestError, CheckoutError, KeyError, TypeError, ValueError, AttributeError) as exc:
                self._send_json(502, {"error": "customer_profile_unavailable", "message": str(exc)})

        @staticmethod
        def _customer_response(customer: Dict[str, Any]) -> Dict[str, Any]:
            tags = customer.get("tags") or ""
            if isinstance(tags, str):
                tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
            elif not isinstance(tags, list):
                tags = []
            return {
                "id": str(customer.get("id")),
                "first_name": customer.get("first_name") or "",
                "last_name": customer.get("last_name") or "",
                "phone": customer.get("phone"),
                "email": customer.get("email"),
                "created_at": customer.get("created_at"),
                "note": customer.get("note"),
                "tags": tags,
                "anonymized": bool(customer.get("anonymized")),
            }

        def _update_customer(self, path: str) -> None:
            customer_id = path[len("/api/customers/"):]
            if not customer_id or "/" in customer_id or not customer_id.isascii() or not customer_id.isdecimal():
                self._send_json(400, {"error": "invalid_customer_id", "message": "Customer ID must be numeric."})
                return
            try:
                body = self._read_json_object()
                changes = {}
                for field in ("first_name", "last_name"):
                    if field in body:
                        value = body[field]
                        if not isinstance(value, str) or len(value.strip()) > 100 or any(ord(ch) < 32 for ch in value):
                            raise ValueError("{} must be text of at most 100 characters".format(field))
                        changes[field] = value.strip()
                if "email" in body:
                    email = body["email"]
                    if email is not None and (
                        not isinstance(email, str)
                        or len(email) > 254
                        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
                    ):
                        raise ValueError("email must be a valid email address")
                    changes["email"] = email.strip() if isinstance(email, str) else None
                if not changes:
                    raise ValueError("provide a supported customer field to update")
                customer = application.shopify_adapter.update_customer(customer_id, changes)
                if application.till_store is not None:
                    application.till_store.audit(
                        self.authenticated_user.staff_id, "CUSTOMER_UPDATED", "customer", customer_id,
                        metadata={"fields": sorted(changes)},
                    )
            except (KeyError, TypeError, ValueError) as exc:
                self._send_json(400, {"error": "invalid_customer_update", "message": str(exc)})
                return
            except (HttpRequestError, AttributeError) as exc:
                self._send_json(502, {"error": "customer_update_failed", "message": str(exc)})
                return
            self._send_json(200, {"customer": self._customer_response(customer)})

        def _anonymize_customer(self, path: str) -> None:
            customer_id = path[len("/api/customers/"):-len("/anonymize")].strip("/")
            try:
                body = self._read_json_object()
                reason = body.get("reason")
                if not customer_id.isascii() or not customer_id.isdecimal():
                    raise ValueError("customer ID must be numeric")
                if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
                    raise ValueError("a reason of at most 500 characters is required")
                customer = application.shopify_adapter.update_customer(customer_id, {
                    "first_name": "Anonymized",
                    "last_name": "Customer",
                    "phone": None,
                    "email": None,
                    "note": "Contact details anonymized; financial and prescription records retained.",
                    "anonymized": True,
                })
                if application.till_store is not None:
                    application.till_store.audit(
                        self.authenticated_user.staff_id, "CUSTOMER_ANONYMIZED", "customer", customer_id,
                        reason=reason.strip(), metadata={"prescriptions_retained": True},
                    )
            except (KeyError, TypeError, ValueError) as exc:
                self._send_json(400, {"error": "invalid_anonymization", "message": str(exc)})
                return
            except (HttpRequestError, AttributeError) as exc:
                self._send_json(502, {"error": "customer_anonymization_failed", "message": str(exc)})
                return
            self._send_json(200, {
                "customer": self._customer_response(customer),
                "message": "Customer contact details were anonymized. Financial, invoice, order, prescription, and audit records were retained.",
            })

        def _register_customer(self) -> None:
            if application.shopify_adapter is None:
                self._send_json(503, {"error": "customer_registration_unavailable", "message": "Customer registration is not configured."})
                return
            try:
                body = self._read_json_object()
                if not isinstance(body.get("name"), str) or not isinstance(body.get("phone"), str):
                    raise ValueError("name and phone must be text")
                first_name, last_name = customer_name_parts(body["name"])
                phone = normalize_customer_phone(body["phone"])
            except (TypeError, ValueError) as exc:
                self._send_json(400, {"error": "invalid_customer", "message": str(exc)})
                return
            try:
                customer, created = application.shopify_adapter.register_customer(first_name, last_name, phone)
            except CustomerRegistrationConflict as exc:
                self._send_json(409, {"error": "customer_conflict", "message": str(exc)})
                return
            except HttpRequestError as exc:
                self._send_json(502, {"error": "customer_registration_upstream_error", "message": str(exc)})
                return
            except ValueError as exc:
                self._send_json(502, {"error": "customer_registration_invalid_response", "message": str(exc)})
                return
            self._send_json(201 if created else 200, {
                "customer": {
                    "id": customer["id"],
                    "first_name": customer["first_name"],
                    "last_name": customer["last_name"],
                    "phone": customer["phone"],
                },
                "created": created,
            })

        def _register_prescription(self) -> None:
            if not application.local_mock_configured:
                self._send_json(503, {
                    "error": "prescription_entry_unavailable",
                    "message": "Prescription entry is available only with the configured local mock; use the connected store tool in production.",
                })
                return
            if application.smt_adapter is None or application.shopify_adapter is None:
                self._send_json(503, {"error": "prescription_entry_unavailable", "message": "Prescription entry adapters are not configured."})
                return
            try:
                body = self._read_json_object()
                phone = normalize_customer_phone(str(body["phone"]))
                store_code = str(body["store_code"]).strip()
                idempotency_key = body.get("idempotency_key")
                if not isinstance(idempotency_key,str) or not idempotency_key.strip():
                    raise ValueError("idempotency_key is required")
                prescribed_on = date.fromisoformat(str(body["date"]))
                valid_until = date.fromisoformat(str(body["valid_until"]))
                if valid_until < prescribed_on:
                    raise ValueError("valid_until must not be before prescription date")
                eyes = {}
                for side in ("od", "os"):
                    eye = body[side]
                    if not isinstance(eye, dict):
                        raise ValueError("{} values are required".format(side.upper()))
                    sph = float(eye["sph"])
                    cyl = float(eye["cyl"])
                    add = None if eye.get("add") in (None, "") else float(eye["add"])
                    axis = axis_from_data(eye.get("axis"))
                    if not (-30 <= sph <= 30 and -10 <= cyl <= 0):
                        raise ValueError("{} sphere/cylinder is outside supported entry bounds".format(side.upper()))
                    if add is not None and not (0 <= add <= 5):
                        raise ValueError("{} ADD is outside supported entry bounds".format(side.upper()))
                    if axis is not None and (axis < 0 or axis > 180):
                        raise ValueError("{} axis must be between 0 and 180 degrees".format(side.upper()))
                    if cyl != 0 and axis is None:
                        raise ValueError("{} axis is required when cylinder is non-zero".format(side.upper()))
                    eyes[side] = {"sph": sph, "cyl": cyl, "axis": axis, "add": add}
                if not store_code:
                    raise ValueError("store_code is required")
                if body.get("optometrist_confirmed") is not True:
                    raise ValueError("the prescription must be confirmed as verified by the selected optometrist")
                self._validate_store_code(store_code)
                optometrist_id = body.get("optometrist_id")
                if not isinstance(optometrist_id, str) or not optometrist_id.strip():
                    raise ValueError("choose the Optometrist who verified this prescription")
                optometrist_id = optometrist_id.strip()
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                self._send_json(400, {"error": "invalid_request", "message": "Invalid prescription entry: {}".format(exc)})
                return
            except PermissionError as exc:
                self._send_json(403,{"error":"store_access_denied","message":str(exc)})
                return
            try:
                matches = application.shopify_adapter.search_customers_by_phone(phone)
                if len(matches) != 1:
                    self._send_json(404 if not matches else 409, {
                        "error": "customer_not_found" if not matches else "customer_ambiguous",
                        "message": "Prescription entry requires exactly one Shopify customer for this mobile number.",
                    })
                    return
                customer = matches[0]
                staff = application.smt_adapter.list_staff(store_code)
                confirmed_by = next((
                    member for member in staff
                    if str(member.get("staff_id")) == optometrist_id
                    and member.get("role") == "optometrist"
                    and member.get("store_code") == store_code
                ), None)
                if confirmed_by is None:
                    self._send_json(422, {
                        "error": "optometrist_not_authorized",
                        "message": "Enter an optometrist ID assigned to this store.",
                    })
                    return
                prescription = application.smt_adapter.create_prescription({
                    "customer_id": customer["id"],
                    "phone": phone,
                    "date": prescribed_on.isoformat(),
                    "valid_until": valid_until.isoformat(),
                    "od": eyes["od"],
                    "os": eyes["os"],
                    "store_code": store_code,
                    "optometrist_id": optometrist_id,
                },idempotency_key)
            except HttpRequestError as exc:
                status = 422 if exc.status == 422 else 502
                self._send_json(status, {
                    "error": "prescription_entry_invalid" if status == 422 else "prescription_store_unavailable",
                    "message": str(exc),
                })
                return
            except (KeyError, TypeError, ValueError) as exc:
                self._send_json(502, {"error": "prescription_entry_failed", "message": str(exc)})
                return
            warnings = [
                "{} axis {} is off the 5-degree grid and requires optometrist review.".format(side.upper(), eyes[side]["axis"])
                for side in ("od", "os")
                if eyes[side]["axis"] is not None and eyes[side]["axis"] % 5
            ]
            self._send_json(201, {
                "prescription": prescription,
                "customer": {"id": customer["id"], "phone": customer["phone"]},
                "confirmed_by": optometrist_id,
                "warnings": warnings,
            })

        def _validate_store_code(self, store_code: Any, location_id: Optional[str] = None) -> None:
            if not isinstance(store_code, str) or not store_code.strip():
                raise ValueError("a configured store code is required")
            if application.shopify_adapter is None:
                raise ValueError("store directory is unavailable")
            locations = application.shopify_adapter.list_locations()
            matches = [
                location for location in locations
                if location.get("active") is True
                and (location.get("eluno") or {}).get("kind") == "store"
                and (location.get("eluno") or {}).get("store_code") == store_code
                and (location_id is None or str(location.get("id")) == str(location_id))
            ]
            if len(matches) != 1:
                raise ValueError("store code and location do not identify one active Eluno store")
            if self.authenticated_user.role in ("cashier", "optometrist") and store_code != self.authenticated_user.store_code:
                raise PermissionError("This account is not assigned to the selected store.")

        def _store_code_for_location(self, location_id: str) -> str:
            if not location_id.isascii() or not location_id.isdecimal():
                raise ValueError("location ID must be numeric")
            locations = application.shopify_adapter.list_locations()
            matches = [
                (location.get("eluno") or {}).get("store_code")
                for location in locations
                if location.get("active") is True
                and (location.get("eluno") or {}).get("kind") == "store"
                and str(location.get("id")) == location_id
            ]
            if len(matches) != 1 or not matches[0]:
                raise ValueError("location is not an active Eluno store")
            return str(matches[0])

        def _validate_operator_store(self, store_code: str, location_id: str) -> None:
            self._validate_store_code(store_code, location_id)

        @staticmethod
        def _single_query_value(query: Dict[str, Any], name: str) -> Optional[str]:
            values = query.get(name, [])
            return values[0] if len(values) == 1 else None

        def _read_json_object(self) -> Dict[str, Any]:
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("Content-Length must be an integer") from exc
            if content_length <= 0:
                raise ValueError("JSON request body is required")
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("request body must be valid JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _session_token(self) -> str:
            from http.cookies import CookieError, SimpleCookie
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except CookieError:
                return ""
            morsel = cookie.get("eluno_session")
            return morsel.value if morsel is not None else ""

        def _customer_session_token(self) -> str:
            from http.cookies import CookieError, SimpleCookie
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except CookieError:
                return ""
            morsel = cookie.get("eluno_customer_session")
            return morsel.value if morsel is not None else ""

        def _customer_payload(self, account) -> Dict[str, Any]:
            return {
                "customer_id": account.customer_id,
                "shopify_customer_id": account.shopify_customer_id,
                "name": account.display_name,
                "phone": account.phone,
                "email": account.email,
                "address": account.address or {},
            }

        def _authorize_customer(self) -> bool:
            if application.customer_accounts is None:
                self._send_json(503, {
                    "error": "customer_accounts_unavailable",
                    "message": "Customer accounts are not configured.",
                })
                return False
            try:
                account = application.customer_accounts.authenticate(self._customer_session_token())
            except sqlite3.Error:
                self._send_json(503, {
                    "error": "customer_sessions_unavailable",
                    "message": "Customer authentication storage is unavailable.",
                })
                return False
            if account is None:
                self._send_json(401, {
                    "error": "customer_authentication_required",
                    "message": "Sign in to your customer account.",
                })
                return False
            self.authenticated_customer = account
            return True

        def _secure_cookie(self) -> bool:
            return (
                os.environ.get("ELUNO_COOKIE_SECURE") == "1"
                or isinstance(self.connection, ssl.SSLSocket)
            )

        def _check_same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            try:
                parsed_origin = urlsplit(origin)
                expected_scheme = "https" if self._secure_cookie() else "http"
                if (
                    parsed_origin.scheme != expected_scheme
                    or parsed_origin.netloc != self.headers.get("Host")
                    or parsed_origin.path or parsed_origin.query or parsed_origin.fragment
                ):
                    raise ValueError
            except ValueError:
                self._send_json(403, {
                    "error": "origin_check_failed",
                    "message": "This request must come from the POS origin.",
                })
                return False
            return True

        def _authorize_api(self, method: str, path: str) -> bool:
            manager = application.auth_manager
            if manager is None or not manager.configured:
                self._send_json(503, {
                    "error": "authentication_not_configured",
                    "message": "The local staff account database is unavailable.",
                })
                return False
            user = manager.user_for_token(self._session_token())
            if user is None:
                self._send_json(401, {"error": "authentication_required", "message": "Sign in to continue to Eluno."})
                return False
            admin_only = (
                path.startswith("/api/admin/")
                or path.startswith("/api/reports/")
                or path == "/api/audit-events"
                or path.startswith("/api/billing-anomalies")
                or path.startswith("/api/system-status")
                or path.startswith("/api/customers/")
                and path != "/api/customers/search"
                and (
                    method == "GET"
                    or
                    method == "PATCH"
                    or method == "POST" and path.endswith("/anonymize")
                )
            )
            cashier_only = (
                path.startswith("/api/checkouts")
                or path.startswith("/api/refunds")
                or path == "/api/store/orders/pending"
                or path.startswith("/api/store/orders/")
            )
            optometrist_only = path == "/api/optometrist/orders/assistance"
            till_operation = path == "/api/tills" or path.startswith("/api/tills/")
            prescription_write = method == "POST" and path == "/api/prescriptions"
            approver_directory = path == "/api/prescription-approvers"
            clinical_input = path in ("/api/customer-prescription", "/api/prescriptions", "/api/lens-validation")
            if admin_only and user.role != "admin":
                self._send_json(403, {
                    "error": "admin_role_required",
                    "message": "This operation requires an Admin account.",
                })
                return False
            if cashier_only and user.role != "cashier":
                self._send_json(403, {
                    "error": "cashier_role_required",
                    "message": "This operation requires a Cashier account.",
                })
                return False
            if optometrist_only and user.role != "optometrist":
                self._send_json(403, {
                    "error": "optometrist_role_required",
                    "message": "Prescription-assistance requests require an authenticated Optometrist account.",
                })
                return False
            if till_operation and user.role not in ("admin", "cashier"):
                self._send_json(403, {
                    "error": "till_role_required",
                    "message": "Till operations require an Admin or Cashier account.",
                })
                return False
            if prescription_write and user.role not in ("cashier", "optometrist"):
                self._send_json(403, {
                    "error": "clinical_access_denied",
                    "message": "Prescription entry requires an authenticated Cashier or Optometrist account.",
                })
                return False
            if approver_directory and user.role not in ("admin", "cashier", "optometrist"):
                self._send_json(403, {
                    "error": "clinical_directory_access_denied",
                    "message": "The Optometrist directory is not available to this role.",
                })
                return False
            if clinical_input and user.role not in ("cashier", "optometrist"):
                self._send_json(403, {
                    "error": "clinical_access_denied",
                    "message": "Clinical workflows are not available to Admin accounts.",
                })
                return False
            self.authenticated_user = user
            return True

        def _auth_login(self) -> None:
            manager = application.auth_manager
            if manager is None or not manager.configured:
                self._send_json(503, {
                    "error": "authentication_not_configured",
                    "message": "The local staff account database is unavailable.",
                })
                return
            try:
                body = self._read_json_object()
                username = body.get("username")
                password = body.get("password")
                if not isinstance(username, str) or not isinstance(password, str):
                    raise ValueError("username and password are required")
            except (TypeError, ValueError) as exc:
                self._send_json(400, {"error": "invalid_request", "message": str(exc)})
                return
            try:
                result = manager.login(username, password, self.client_address[0])
            except AuthenticationThrottled as exc:
                body_bytes = json.dumps({
                    "error": "login_throttled", "message": str(exc),
                }).encode("utf-8")
                self.send_response(429)
                self.send_header("Retry-After", str(exc.retry_after))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return
            if result is None:
                self._send_json(401, {"error": "invalid_credentials", "message": "Username or password is incorrect."})
                return
            token, user = result
            body_bytes = json.dumps({
                "user": {
                    "username": user.username, "staff_id": user.staff_id,
                    "operator_id": user.staff_id, "role": user.role,
                    "display_name": user.display_name, "store_code": user.store_code,
                }
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body_bytes)))
            cookie = "eluno_session={}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800".format(token)
            if self._secure_cookie():
                cookie += "; Secure"
            self.send_header("Set-Cookie", cookie)
            if application.customer_accounts is not None:
                application.customer_accounts.logout(self._customer_session_token())
            customer_cookie = "eluno_customer_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
            if self._secure_cookie():
                customer_cookie += "; Secure"
            self.send_header("Set-Cookie", customer_cookie)
            self.end_headers()
            self.wfile.write(body_bytes)

        def _auth_me(self) -> None:
            manager = application.auth_manager
            if manager is None or not manager.configured:
                self._send_json(503, {
                    "error": "authentication_not_configured",
                    "message": "The local staff account database is unavailable.",
                })
                return
            user = manager.user_for_token(self._session_token())
            if user is None:
                self._send_json(401, {"error": "authentication_required", "message": "Sign in to continue to Eluno."})
                return
            self._send_json(200, {
                "user": {
                    "username": user.username, "staff_id": user.staff_id,
                    "operator_id": user.staff_id, "role": user.role,
                    "display_name": user.display_name, "store_code": user.store_code,
                    "external_staff_id": user.external_staff_id,
                }
            })

        def _auth_logout(self) -> None:
            manager = application.auth_manager
            if manager is not None:
                manager.logout(self._session_token())
            self.send_response(204)
            self.send_header(
                "Set-Cookie",
                "eluno_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                + ("; Secure" if self._secure_cookie() else ""),
            )
            self.end_headers()

        def _customer_auth_me(self) -> None:
            if not self._authorize_customer():
                return
            self._send_json(200, {"customer": self._customer_payload(self.authenticated_customer)})

        def _customer_locations(self) -> None:
            if application.shopify_adapter is None:
                self._send_json(503, {"error": "store_directory_unavailable", "message": "Stores are unavailable."})
                return
            try:
                locations = [
                    {
                        "id": str(location["id"]),
                        "name": str(location["name"]),
                        "store_code": (location.get("eluno") or {}).get("store_code"),
                    }
                    for location in application.shopify_adapter.list_locations()
                    if location.get("active") is True
                    and (location.get("eluno") or {}).get("kind") == "store"
                    and (location.get("eluno") or {}).get("store_code")
                ]
            except (HttpRequestError, KeyError, TypeError, ValueError) as exc:
                self._send_json(502, {"error": "store_directory_unavailable", "message": str(exc)})
                return
            self._send_json(200, {"locations": locations})

        def _customer_catalog(self) -> None:
            if application.pricing_service is None:
                self._send_json(503, {"error": "catalog_unavailable", "message": "Product catalog is unavailable."})
                return
            try:
                variants = [
                    {
                        "variant_id": item.variant_id,
                        "line_type": item.line_type,
                        "product_title": item.product_title or "Eluno product",
                        "variant_title": item.variant_title or "",
                        "price_paise": item.price_paise,
                        "lens_type": item.lens_type,
                        "index": str(item.index) if item.index is not None else None,
                        "coating": item.coating,
                    }
                    for item in application.shopify_adapter.list_catalog_variants()
                    if item.active and item.line_type in ("frame", "lens")
                ]
            except (HttpRequestError, TypeError, ValueError) as exc:
                self._send_json(502, {"error": "catalog_unavailable", "message": str(exc)})
                return
            self._send_json(200, {"variants": variants})

        def _customer_cart(self, body: Dict[str, Any]) -> Tuple[Cart, Tuple[CatalogVariant, ...], List[Dict[str, Any]]]:
            cart_body = body.get("cart")
            if not isinstance(cart_body, dict) or not isinstance(cart_body.get("lines"), list):
                raise ValueError("A cart with at least one item is required.")
            location_id = str(cart_body.get("location_id") or "")
            if not location_id.isascii() or not location_id.isdecimal():
                raise ValueError("Select an active Eluno store.")
            matching_locations = [
                location for location in application.shopify_adapter.list_locations()
                if location.get("active") is True
                and (location.get("eluno") or {}).get("kind") == "store"
                and str(location.get("id")) == location_id
            ]
            if len(matching_locations) != 1:
                raise ValueError("Selected store is not active.")
            store_code = (matching_locations[0].get("eluno") or {}).get("store_code")
            if not isinstance(store_code, str) or not store_code:
                raise ValueError("Selected store is not configured for Eluno.")
            line_values = []
            raw_lines = []
            for value in cart_body["lines"]:
                if not isinstance(value, dict):
                    raise ValueError("Cart lines must be objects.")
                variant_id = str(value.get("variant_id") or "")
                quantity = value.get("quantity")
                assistance = value.get("needs_prescription_assistance") is True
                prescription_id = value.get("prescription_id")
                if value.get("prescription_id") is not None:
                    if not isinstance(value["prescription_id"], str) or not value["prescription_id"]:
                        raise ValueError("Prescription reference is invalid.")
                    prescriptions = application.smt_adapter.prescriptions_by_customer(
                        self.authenticated_customer.shopify_customer_id
                    )
                    match = next((
                        item for item in prescriptions
                        if str(item.get("rx_id")) == value["prescription_id"]
                        and str(item.get("customer_id")) == self.authenticated_customer.shopify_customer_id
                    ), None)
                    if match is None:
                        raise ValueError("Prescription is not associated with this customer account.")
                if assistance:
                    prescription_id = "online-assistance"
                line_values.append(CartLine(
                    variant_id, quantity, value.get("pair_ref"), prescription_id
                ))
                raw_lines.append({
                    "variant_id": variant_id,
                    "quantity": quantity,
                    "pair_ref": value.get("pair_ref"),
                    "prescription_id": prescription_id,
                    "needs_prescription_assistance": assistance,
                })
            cart = Cart(store_code, location_id, tuple(line_values))
            variants = application.pricing_service.resolve(cart)
            for line, variant in zip(line_values, variants):
                if variant.line_type == "lens":
                    raw = next(item for item in raw_lines if item["variant_id"] == line.variant_id)
                    if not raw.get("prescription_id"):
                        raise ValueError("Choose a saved prescription or request optometrist assistance for each lens.")
            return cart, variants, raw_lines

        def _validate_online_stock(self, cart: Cart, variants: Tuple[CatalogVariant, ...]) -> None:
            frame_variant_ids = {
                variant.variant_id for variant in variants if variant.line_type == "frame"
            }
            grouped: Dict[str, int] = {}
            for line in cart.lines:
                if line.variant_id in frame_variant_ids:
                    grouped[line.variant_id] = grouped.get(line.variant_id, 0) + line.quantity
            if not grouped:
                return
            inventory_by_variant = {}
            for variant_id in grouped:
                if not variant_id.isascii() or not variant_id.isdecimal():
                    raise ValueError("A cart item has an invalid catalog reference.")
                variant = application.shopify_adapter.get_variant(int(variant_id))
                item_id = variant.get("inventory_item_id")
                if item_id is not None:
                    inventory_by_variant[variant_id] = int(item_id)
            inventory_item_ids = list(inventory_by_variant.values())
            if not inventory_item_ids:
                return
            levels = application.shopify_adapter.list_inventory_levels(
                inventory_item_ids, int(cart.location_id)
            )
            available_by_item = {
                str(level.get("inventory_item_id")): int(level.get("available", 0))
                for level in levels
            }
            for variant_id, item_id in inventory_by_variant.items():
                if available_by_item.get(str(item_id), 0) < grouped[variant_id]:
                    raise ValueError("Insufficient stock for {} at the selected store.".format(variant_id))

        def _customer_pricing(self) -> None:
            try:
                body = self._read_json_object()
                cart, variants, raw_lines = self._customer_cart(body)
                self._validate_online_stock(cart, variants)
                if application.pricing_service is None:
                    raise ValueError("Pricing is unavailable.")
                offer_ids = body.get("offer_ids")
                if offer_ids is not None and (
                    not isinstance(offer_ids, list)
                    or any(not isinstance(value, str) or not value for value in offer_ids)
                ):
                    raise ValueError("offer_ids must be an array of offer codes.")
                tags = frozenset()
                if application.shopify_adapter is not None:
                    customer = application.shopify_adapter.get_customer(
                        self.authenticated_customer.shopify_customer_id
                    )
                    if isinstance(customer.get("tags"), str):
                        tags = frozenset(tag.strip() for tag in customer["tags"].split(",") if tag.strip())
                pricing = application.pricing_service.price(
                    cart, bengaluru_today(), tags,
                    tuple(offer_ids) if offer_ids is not None else None,
                )
            except PermissionError as exc:
                self._send_json(403, {"error": "store_access_denied", "message": str(exc)})
                return
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                self._send_json(400, {"error": "customer_cart_invalid", "message": str(exc)})
                return
            except HttpRequestError as exc:
                self._send_json(502, {"error": "customer_order_upstream_unavailable", "message": str(exc)})
                return
            except sqlite3.Error:
                self._send_json(503, {"error": "customer_pricing_unavailable", "message": "Customer order storage is unavailable."})
                return
            self._send_json(200, {
                "store_code": cart.store_code,
                "location_id": cart.location_id,
                "business_date": bengaluru_today().isoformat(),
                "gross_paise": pricing.gross_paise,
                "discount_paise": pricing.discount_paise,
                "taxable_paise": pricing.taxable_paise,
                "cgst_paise": pricing.cgst_paise,
                "sgst_paise": pricing.sgst_paise,
                "tax_paise": pricing.tax_paise,
                "payable_paise": pricing.payable_paise,
                "lines": [
                    {
                        "variant_id": line.cart_line.variant_id,
                        "quantity": line.cart_line.quantity,
                        "gross_paise": line.gross_paise,
                        "discount_paise": line.discount_paise,
                        "taxable_paise": line.taxable_paise,
                        "cgst_paise": line.cgst_paise,
                        "sgst_paise": line.sgst_paise,
                        "tax_paise": line.tax_paise,
                        "payable_paise": line.payable_paise,
                    } for line in pricing.lines
                ],
                "offers": [
                    {"offer_id": item.offer_id, "discount_paise": item.discount_paise}
                    for item in pricing.offers.applied
                ],
                "offer_evaluation": [
                    {
                        "offer_id": item.offer_id,
                        "name": item.offer_id,
                        "stackable": None,
                        "status": item.status,
                        "reason": item.reason,
                        "discount_paise": item.discount_paise,
                    }
                    for item in pricing.offers.eligible + pricing.offers.rejected + pricing.offers.applied
                ],
            })

        def _customer_create_order(self) -> None:
            try:
                body = self._read_json_object()
                address = _validated_delivery_address(body.get("delivery_address"))
                payment_method_preference = body.get("payment_method_preference")
                if payment_method_preference not in (None, "upi", "card"):
                    raise ValueError("payment_method_preference must be upi or card.")
                request_payload = {
                    "cart": body.get("cart"),
                    "delivery_address": address,
                    "offer_ids": body.get("offer_ids"),
                    "payment_method_preference": payment_method_preference,
                }
                replay = application.customer_orders.replay(
                    self.authenticated_customer.customer_id,
                    body.get("idempotency_key"),
                    request_payload,
                )
                if replay is not None:
                    self._send_json(200, {"order": replay, "duplicate": True})
                    return
                cart, variants, raw_lines = self._customer_cart(body)
                self._validate_online_stock(cart, variants)
                offer_ids = body.get("offer_ids")
                if offer_ids is not None and (
                    not isinstance(offer_ids, list)
                    or any(not isinstance(value, str) or not value for value in offer_ids)
                ):
                    raise ValueError("offer_ids must be an array of offer codes.")
                customer = application.shopify_adapter.get_customer(
                    self.authenticated_customer.shopify_customer_id
                )
                tags = frozenset(
                    tag.strip() for tag in customer.get("tags", "").split(",") if tag.strip()
                ) if isinstance(customer.get("tags"), str) else frozenset()
                pricing = application.pricing_service.price(
                    cart, bengaluru_today(), tags,
                    tuple(offer_ids) if offer_ids is not None else None,
                )
                catalog_by_id = {variant.variant_id: variant for variant in variants}
                items = []
                needs_assistance = False
                for line in pricing.lines:
                    selected = catalog_by_id[line.cart_line.variant_id]
                    raw = next(item for item in raw_lines if item["variant_id"] == line.cart_line.variant_id)
                    needs_assistance = needs_assistance or raw["needs_prescription_assistance"]
                    items.append({
                        "variant_id": line.cart_line.variant_id,
                        "title": " ".join(
                            part for part in (selected.product_title, selected.variant_title) if part
                        ),
                        "line_type": selected.line_type,
                        "quantity": line.cart_line.quantity,
                        "prescription_assistance_requested": raw["needs_prescription_assistance"],
                        "gross_paise": line.gross_paise,
                        "discount_paise": line.discount_paise,
                        "taxable_paise": line.taxable_paise,
                        "cgst_paise": line.cgst_paise,
                        "sgst_paise": line.sgst_paise,
                        "tax_paise": line.tax_paise,
                        "total_paise": line.payable_paise,
                    })
                order, duplicate = application.customer_orders.create(
                    self.authenticated_customer.customer_id,
                    body.get("idempotency_key"),
                    {
                        "created_at": bengaluru_today().isoformat(),
                        "store_code": cart.store_code,
                        "location_id": cart.location_id,
                        "customer_name": self.authenticated_customer.display_name,
                        "customer_phone": self.authenticated_customer.phone,
                        "delivery_address": address,
                        "items": items,
                        "offers": [
                            {"offer_id": item.offer_id, "discount_paise": item.discount_paise}
                            for item in pricing.offers.applied
                        ],
                        "gross_paise": pricing.gross_paise,
                        "discount_paise": pricing.discount_paise,
                        "taxable_paise": pricing.taxable_paise,
                        "cgst_paise": pricing.cgst_paise,
                        "sgst_paise": pricing.sgst_paise,
                        "tax_paise": pricing.tax_paise,
                        "total_paise": pricing.payable_paise,
                        "payment_method_preference": payment_method_preference,
                        "payment_status": "awaiting_payment",
                        "fulfillment_status": "not_submitted",
                        "tracking_available": False,
                        "status_message": (
                            "Awaiting store confirmation for prescription assistance."
                            if needs_assistance else "Order request saved; payment and store confirmation are pending."
                        ),
                    },
                    request_payload,
                )
            except (KeyError, TypeError, ValueError, CustomerOrderConflict) as exc:
                self._send_json(400, {"error": "customer_order_invalid", "message": str(exc)})
                return
            except HttpRequestError as exc:
                self._send_json(502, {"error": "customer_order_upstream_unavailable", "message": str(exc)})
                return
            except sqlite3.Error:
                self._send_json(503, {"error": "customer_order_storage_unavailable", "message": "The order request could not be saved."})
                return
            self._send_json(200 if duplicate else 201, {"order": order, "duplicate": duplicate})

        def _customer_orders(self) -> None:
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            try:
                raw_page = self._single_query_value(query, "page") or "1"
                if not raw_page.isascii() or not raw_page.isdecimal():
                    raise ValueError("page must be a positive integer.")
                orders, has_more = application.customer_orders.list_for_customer(
                    self.authenticated_customer.customer_id, int(raw_page)
                )
            except (ValueError, CustomerOrderConflict) as exc:
                self._send_json(400, {"error": "customer_orders_invalid", "message": str(exc)})
                return
            except sqlite3.Error:
                self._send_json(503, {"error": "customer_order_storage_unavailable", "message": "Order history is unavailable."})
                return
            self._send_json(200, {"orders": orders, "page": int(raw_page), "has_more": has_more})

        def _store_pending_orders(self) -> None:
            store_code = self.authenticated_user.store_code
            if not store_code:
                self._send_json(403, {
                    "error": "store_assignment_required",
                    "message": "A store assignment is required to review customer order requests.",
                })
                return
            try:
                orders = application.customer_orders.pending_for_store(store_code)
            except (CustomerOrderConflict, sqlite3.Error):
                self._send_json(503, {
                    "error": "store_order_queue_unavailable",
                    "message": "The store order queue could not be loaded. Refresh and retry.",
                })
                return
            self._send_json(200, {"orders": [{
                "order_id": order["order_id"],
                "created_at": order["created_at"],
                "store_code": order["store_code"],
                "customer_name": order["customer_name"],
                "items": [{
                    "title": item["title"],
                    "quantity": item["quantity"],
                    "prescription_assistance_requested": item.get("prescription_assistance_requested") is True,
                } for item in order.get("items", [])],
                "total_paise": order["total_paise"],
                "payment_status": order["payment_status"],
                "payment_method_preference": order.get("payment_method_preference"),
                "store_confirmation_status": order.get("store_confirmation_status", "pending"),
                "status_message": order.get("status_message", ""),
            } for order in orders]})

        def _optometrist_assistance_orders(self) -> None:
            store_code = self.authenticated_user.store_code
            if not store_code:
                self._send_json(403, {
                    "error": "store_assignment_required",
                    "message": "A store assignment is required to review prescription-assistance requests.",
                })
                return
            try:
                orders = application.customer_orders.prescription_assistance_for_store(store_code)
            except (CustomerOrderConflict, sqlite3.Error):
                self._send_json(503, {
                    "error": "optometrist_order_queue_unavailable",
                    "message": "The prescription-assistance queue could not be loaded. Refresh and retry.",
                })
                return
            self._send_json(200, {"orders": [{
                "order_id": order["order_id"],
                "created_at": order["created_at"],
                "store_code": order["store_code"],
                "customer_name": order.get("customer_name", ""),
                "customer_phone": order.get("customer_phone", ""),
                "items": [{
                    "title": item["title"],
                    "quantity": item["quantity"],
                } for item in order.get("items", [])
                   if item.get("prescription_assistance_requested") is True],
                "payment_status": order["payment_status"],
                "store_confirmation_status": order.get("store_confirmation_status", "pending"),
                "status_message": order.get("status_message", ""),
            } for order in orders]})

        def _store_order_decision(self, path: str) -> None:
            prefix = "/api/store/orders/"
            suffix, separator, decision = path[len(prefix):].rpartition("/")
            if not separator or decision not in ("confirm", "deny") or not suffix or "/" in suffix:
                self._send_json(404, {"error": "not_found", "message": "Store order action was not found."})
                return
            try:
                body = self._read_json_object()
                store_code = self.authenticated_user.store_code
                if not store_code:
                    raise PermissionError("A store assignment is required to decide this order.")
                result = application.customer_orders.decide_for_store(
                    suffix,
                    store_code,
                    self.authenticated_user.staff_id,
                    decision,
                    body.get("reason"),
                )
            except PermissionError as exc:
                self._send_json(403, {"error": "store_assignment_required", "message": str(exc)})
                return
            except CustomerOrderConflict as exc:
                status = 404 if "not found" in str(exc).casefold() else 409
                self._send_json(status, {"error": "store_order_decision_conflict", "message": str(exc)})
                return
            except (TypeError, ValueError) as exc:
                self._send_json(400, {"error": "invalid_store_order_decision", "message": str(exc)})
                return
            except sqlite3.Error:
                self._send_json(503, {
                    "error": "store_order_decision_unavailable",
                    "message": "The order decision could not be saved. Refresh the queue before retrying.",
                })
                return
            self._send_json(200, {"order": {
                "order_id": result["order_id"],
                "status": result["status"],
                "store_confirmation_status": result["store_confirmation_status"],
                "store_decision_by": result["store_decision_by"],
                "store_decision_at": result["store_decision_at"],
                "status_message": result["status_message"],
            }})

        def _customer_register(self) -> None:
            if application.customer_accounts is None or application.shopify_adapter is None:
                self._send_json(503, {
                    "error": "customer_registration_unavailable",
                    "message": "Customer registration is not configured.",
                })
                return
            try:
                body = self._read_json_object()
                name = body.get("name")
                phone = normalize_customer_phone(body.get("phone"))
                email = body.get("email")
                password = body.get("password")
                if email == "":
                    email = None
                if email is not None and (
                    not isinstance(email, str) or len(email) > 254
                    or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
                ):
                    raise ValueError("Enter a valid email address.")
                if not isinstance(password, str):
                    raise ValueError("A password is required.")
                validate_password(password)
                first_name, last_name = customer_name_parts(name)
            except (TypeError, ValueError) as exc:
                self._send_json(400, {"error": "invalid_customer_registration", "message": str(exc)})
                return
            try:
                with application.customer_registration_lock:
                    matches = application.shopify_adapter.search_customers_by_phone(phone)
                    if matches:
                        self._send_json(409, {
                            "error": "customer_registration_unavailable",
                            "message": "We could not create an account with those details. Contact Eluno staff to verify and link your customer record.",
                        })
                        return
                    customer = application.shopify_adapter.create_customer(
                        first_name, last_name, phone, email
                    )
                    account = application.customer_accounts.create_account(
                        str(customer["id"]), phone, " ".join((first_name, last_name)).strip(),
                        password, email,
                    )
            except CustomerAccountError as exc:
                self._send_json(409, {
                    "error": "customer_registration_unavailable",
                    "message": "This account could not be created. Contact Eluno staff if the issue continues.",
                })
                return
            except sqlite3.Error:
                self._send_json(503, {
                    "error": "customer_registration_unavailable",
                    "message": "The Shopify customer may exist, but account setup could not be confirmed. Contact Eluno staff before retrying.",
                })
                return
            except CustomerRegistrationConflict:
                self._send_json(409, {
                    "error": "customer_registration_unavailable",
                    "message": "We could not create an account with those details. Contact Eluno staff to verify and link your customer record.",
                })
                return
            except HttpRequestError as exc:
                self._send_json(502, {
                    "error": "customer_directory_unavailable",
                    "message": "Customer registration could not be confirmed. Contact Eluno staff before retrying.",
                })
                return
            except (KeyError, TypeError, ValueError) as exc:
                self._send_json(502, {
                    "error": "customer_record_invalid",
                    "message": "The customer service returned an invalid record; no account was created.",
                })
                return
            self._send_json(201, {
                "message": "Customer account created. Sign in to continue.",
                "customer": self._customer_payload(account),
            })

        def _customer_login(self) -> None:
            if application.customer_accounts is None:
                self._send_json(503, {"error": "customer_accounts_unavailable", "message": "Customer accounts are not configured."})
                return
            try:
                body = self._read_json_object()
                phone = normalize_customer_phone(body.get("phone"))
                password = body.get("password")
                if not isinstance(password, str):
                    raise ValueError("Mobile number and password are required.")
            except (TypeError, ValueError) as exc:
                self._send_json(400, {"error": "invalid_request", "message": str(exc)})
                return
            try:
                result = application.customer_accounts.login(phone, password, self.client_address[0])
            except CustomerLoginThrottled as exc:
                self._send_json(429, {"error": "customer_login_throttled", "message": str(exc)})
                return
            except sqlite3.Error:
                self._send_json(503, {
                    "error": "customer_sessions_unavailable",
                    "message": "Customer authentication storage is unavailable.",
                })
                return
            if result is None:
                self._send_json(401, {
                    "error": "invalid_customer_credentials",
                    "message": "Mobile number or password is incorrect.",
                })
                return
            token, account = result
            if application.auth_manager is not None:
                application.auth_manager.logout(self._session_token())
            cookie = "eluno_customer_session={}; Path=/; HttpOnly; SameSite=Strict; Max-Age=1209600".format(token)
            if self._secure_cookie():
                cookie += "; Secure"
            body_bytes = json.dumps({"customer": self._customer_payload(account)}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Set-Cookie", cookie)
            staff_cookie = "eluno_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
            if self._secure_cookie():
                staff_cookie += "; Secure"
            self.send_header("Set-Cookie", staff_cookie)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

        def _customer_logout(self) -> None:
            token = self._customer_session_token()
            if application.customer_accounts is not None:
                application.customer_accounts.logout(token)
            self.send_response(204)
            self.send_header(
                "Set-Cookie",
                "eluno_customer_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                + ("; Secure" if self._secure_cookie() else ""),
            )
            self.end_headers()

        def _customer_profile(self) -> None:
            self._send_json(200, {"customer": self._customer_payload(self.authenticated_customer)})

        def _customer_update_profile(self) -> None:
            account = self.authenticated_customer
            try:
                body = self._read_json_object()
                allowed = {"name", "phone", "email", "address"}
                if set(body) - allowed or not {"name", "phone", "address"}.issubset(body):
                    raise ValueError("Name, mobile number, and delivery address are required.")
                first_name, last_name = customer_name_parts(body["name"])
                phone = normalize_customer_phone(body["phone"])
                email = body.get("email") or None
                if email is not None and (
                    not isinstance(email, str) or len(email) > 254
                    or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
                ):
                    raise ValueError("Enter a valid email address.")
                address = body["address"]
                if not isinstance(address, dict):
                    raise ValueError("Delivery address must be an object.")
                checked_address = _validated_delivery_address(address)
            except (KeyError, TypeError, ValueError) as exc:
                self._send_json(400, {"error": "invalid_customer_profile", "message": str(exc)})
                return
            try:
                with application.customer_registration_lock:
                    if not application.customer_accounts.phone_available(phone, account.customer_id):
                        raise CustomerAccountError("Another account already uses this mobile number.")
                    application.shopify_adapter.update_customer(
                        account.shopify_customer_id,
                        {"first_name": first_name, "last_name": last_name, "phone": phone, "email": email},
                    )
                    updated = application.customer_accounts.update_profile(
                        account, " ".join((first_name, last_name)).strip(), phone, email, checked_address
                    )
            except CustomerAccountError as exc:
                self._send_json(409, {"error": "customer_profile_conflict", "message": str(exc)})
                return
            except HttpRequestError:
                self._send_json(502, {
                    "error": "customer_profile_upstream_unavailable",
                    "message": "The profile was not updated because the customer service is unavailable.",
                })
                return
            except sqlite3.Error:
                self._send_json(503, {
                    "error": "customer_profile_storage_unavailable",
                    "message": "Customer profile synchronization may be incomplete. Reload the profile before retrying.",
                })
                return
            self._send_json(200, {"customer": self._customer_payload(updated)})

        def _customer_change_password(self) -> None:
            try:
                body = self._read_json_object()
                application.customer_accounts.change_password(
                    self.authenticated_customer, body.get("current_password"), body.get("new_password")
                )
            except (TypeError, ValueError, CustomerAccountError) as exc:
                self._send_json(400, {"error": "customer_password_update_failed", "message": str(exc)})
                return
            self.send_response(204)
            self.send_header(
                "Set-Cookie",
                "eluno_customer_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                + ("; Secure" if self._secure_cookie() else ""),
            )
            self.end_headers()

        def _admin_staff(self) -> None:
            self._send_json(200, {"staff": application.auth_manager.list_staff()})

        def _admin_create_staff(self) -> None:
            try:
                body = self._read_json_object()
                store_code = body.get("store_code")
                role = body.get("role")
                external_staff_id = body.get("external_staff_id")
                if role in ("cashier", "optometrist") and not store_code:
                    raise ValueError("Cashier and Optometrist accounts must be assigned to one store.")
                if store_code is not None:
                    self._validate_store_code(store_code)
                if role == "optometrist":
                    if not isinstance(external_staff_id, str) or not external_staff_id.strip():
                        raise ValueError("Choose the matching clinical staff record for this Optometrist account.")
                    if application.smt_adapter is None:
                        raise ValueError("Clinical staff directory is unavailable.")
                    directory = application.smt_adapter.list_staff(store_code)
                    if not any(
                        str(member.get("staff_id")) == external_staff_id
                        and member.get("role") == "optometrist"
                        and member.get("store_code") == store_code
                        for member in directory
                    ):
                        raise ValueError("The selected clinical staff record is not an Optometrist assigned to this store.")
                elif external_staff_id is not None:
                    raise ValueError("Only Optometrist accounts may be linked to a clinical staff record.")
                record = application.auth_manager.create_staff(
                    body.get("username"),
                    body.get("password"),
                    role,
                    body.get("display_name"),
                    self.authenticated_user.staff_id,
                    store_code,
                    external_staff_id,
                )
            except DuplicateUsername as exc:
                self._send_json(409, {"error": "duplicate_username", "message": str(exc)})
                return
            except HttpRequestError as exc:
                self._send_json(502, {"error": "store_directory_unavailable", "message": str(exc)})
                return
            except (TypeError, ValueError, StaffAccountError) as exc:
                self._send_json(400, {"error": "invalid_staff_account", "message": str(exc)})
                return
            self._send_json(201, {"staff": record})

        def _admin_update_staff(self, path: str) -> None:
            staff_id = unquote(path[len("/api/admin/staff/"):])
            if not staff_id or "/" in staff_id:
                self._send_json(404, {"error": "not_found", "message": "Staff account was not found."})
                return
            try:
                changes = self._read_json_object()
                if "store_code" in changes and changes["store_code"] is not None:
                    self._validate_store_code(changes["store_code"])
                current = next(
                    (account for account in application.auth_manager.list_staff()
                     if account["staff_id"] == staff_id),
                    None,
                )
                if current is None:
                    raise StaffAccountError("Staff account was not found.")
                resulting_role = changes.get("role", current["role"])
                resulting_store = changes.get("store_code", current.get("store_code"))
                resulting_external_id = changes.get(
                    "external_staff_id", current.get("external_staff_id")
                )
                if resulting_role in ("cashier", "optometrist"):
                    if not resulting_store:
                        raise ValueError("Cashier and Optometrist accounts must be assigned to a store.")
                    self._validate_store_code(resulting_store)
                if resulting_role == "optometrist":
                    if not resulting_external_id or application.smt_adapter is None:
                        raise ValueError("Optometrist accounts require a clinical staff link.")
                    directory = application.smt_adapter.list_staff(resulting_store)
                    if not any(
                        str(member.get("staff_id")) == resulting_external_id
                        and member.get("role") == "optometrist"
                        and member.get("store_code") == resulting_store
                        for member in directory
                    ):
                        raise ValueError("The linked Optometrist must be assigned to the selected store.")
                record = application.auth_manager.update_staff(
                    staff_id, changes, self.authenticated_user.staff_id
                )
            except HttpRequestError as exc:
                self._send_json(502, {"error": "store_directory_unavailable", "message": str(exc)})
                return
            except (TypeError, ValueError, StaffAccountError) as exc:
                self._send_json(400, {"error": "invalid_staff_update", "message": str(exc)})
                return
            self._send_json(200, {"staff": record})

        def _send_html(self, status: int, document: str) -> None:
            body=document.encode("utf-8"); self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length",str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def _serve_pos_asset(self, relative_path: str) -> None:
            if relative_path == "":
                relative_path = "index.html"
            safe_path = Path(relative_path)
            if safe_path.is_absolute() or ".." in safe_path.parts:
                self._send_json(404, {"error": "not_found", "message": "Asset not found."})
                return
            web_root = Path(__file__).resolve().parent / "web"
            built_root = web_root / "react-build"
            asset_root = built_root if (built_root / "index.html").is_file() else web_root
            asset = asset_root / safe_path
            if not asset.is_file():
                self._send_json(404, {"error": "not_found", "message": "Asset not found."})
                return
            content_types = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".svg": "image/svg+xml",
                ".json": "application/json; charset=utf-8",
            }
            body = asset.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_types.get(asset.suffix, "application/octet-stream"))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format_string: str, *args: object) -> None:
            return

    return ApiHandler


def serve(address: Tuple[str, int], application: Optional[PosApplication] = None) -> None:
    server = PosHttpServer(address, make_handler(application or default_application()))
    try:
        server.serve_forever()
    finally:
        server.server_close()
