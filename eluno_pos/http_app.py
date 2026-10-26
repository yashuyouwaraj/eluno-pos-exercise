"""Small JSON HTTP API for the implemented POS foundation workflows."""

from datetime import date
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Type
from urllib.parse import parse_qs, urlparse, urlsplit
from uuid import uuid4

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
from .models import Cart, CartLine, LensConfiguration, Prescription, axis_from_data
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


class PosHttpServer(ThreadingHTTPServer):
    """Do not print Windows connection-reset noise from short health probes."""
    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], ConnectionResetError):
            return
        super().handle_error(request, client_address)


def default_application() -> PosApplication:
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
    operations = OperationsStore(state_path)
    lookup = CustomerPrescriptionService(shopify, smt)
    checkout = CheckoutService(
        carts, terminal, shopify, shopify, SaleStore(state_path), shopify, operations,
        prescription_lookup=lookup, lens_catalog=shopify, inventory=shopify,
    )
    refunds = RefundService(shopify, operations)
    reports = DayEndReportService(shopify, operations, checkout._sales)
    anomalies = AnomalyService(shopify, operations, checkout._sales, smt)
    mock_port = os.environ.get("ELUNO_LOCAL_MOCK_PORT", "8787")
    local_mock_configured = (
        os.environ.get("ELUNO_LOCAL_MOCK_MODE") == "1"
        and _matches_local_mock_url(shopify_url, "/admin/api/2026-10", mock_port)
        and _matches_local_mock_url(smt_url, "/smt", mock_port)
        and _matches_local_mock_url(terminal_url, "/terminal", mock_port)
    )
    return PosApplication(lookup, shopify, checkout, refunds, operations, reports, DocumentService(checkout._sales, operations, shopify), anomalies, shopify, smt, terminal, local_mock_configured)


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
            if parsed.path == "/":
                self._send_json(200, {
                    "service": "eluno-pos", "description": "Local Eluno point-of-sale API.",
                    "health": "/health",
                    "endpoint_groups": {
                        "customer_and_prescription": ["/api/customer-prescription", "/api/customers"],
                        "catalog_and_offers": ["/api/catalog/variants", "/api/offers"],
                        "admin_catalog_inventory": ["/api/admin/catalog/products", "/api/admin/catalog/frames", "/api/admin/inventory/receipts"],
                        "checkout": ["/api/checkouts", "/api/checkouts/{sale_id}"],
                        "refunds": ["/api/refunds", "/api/refunds/{refund_id}"],
                        "cash_drawer": ["/api/tills", "/api/tills/{session_id}"],
                        "reports": ["/api/reports/day-end", "/api/reports/anomalies"],
                        "audit": ["/api/audit-events"],
                        "documents": ["/api/invoices/{sale_id}", "/api/invoices/lookup?invoice_no=...", "/api/credit-notes/{refund_id}"],
                        "system_status": ["/api/system-status", "/api/system-status/mock-payment-test"],
                    },
                    "cashier": "Start the interactive cashier with: python scripts/start_local.py",
                })
                return
            if parsed.path == "/health":
                self._send_json(200, {"ok": True, "service": "eluno-pos"})
                return
            if parsed.path == "/api/system-status":
                self._send_json(200, system_status(application))
                return
            if parsed.path == "/api/customer-prescription":
                self._customer_prescription(parse_qs(parsed.query))
                return
            if parsed.path == "/api/catalog/variants":
                self._catalog_variants(parse_qs(parsed.query))
                return
            if parsed.path == "/api/admin/locations":
                self._admin_locations()
                return
            if parsed.path == "/api/admin/catalog/frames":
                self._admin_frames()
                return
            if parsed.path == "/api/offers":
                self._available_offers(parse_qs(parsed.query))
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
            if parsed.path == "/api/reports/anomalies":
                self._anomalies(parse_qs(parsed.query)); return
            if parsed.path == "/api/audit-events":
                self._audit_events(parse_qs(parsed.query)); return
            self._send_json(404, {"error": "not_found", "message": "Endpoint not found."})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
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
            if path == "/api/admin/catalog/products":
                self._admin_create_product()
                return
            if path == "/api/admin/inventory/receipts":
                self._admin_receive_stock()
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
            if path == "/api/tills": self._open_till(); return
            if path.startswith("/api/tills/") and path.endswith("/movements"): self._till_movement(path); return
            if path.startswith("/api/tills/") and path.endswith("/close"): self._close_till(path); return
            if path == "/api/billing-anomalies": self._billing_anomalies(); return
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
            if result.is_valid:
                self._send_json(200, {"valid": True, "errors": [], "variant_id": result.option.variant_id})
            else:
                self._send_json(422, {"valid": False, "errors": list(result.errors)})

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
                request = CheckoutRequest(
                    idempotency_key=str(body["idempotency_key"]),
                    cart=Cart(str(body["cart"]["store_code"]), str(body["cart"]["location_id"]), lines),
                    business_date=date.fromisoformat(str(body["business_date"])),
                    payment_method=str(body["payment_method"]), channel=str(body["channel"]),
                    staff_id=str(body["staff_id"]), customer_id=(str(body["customer_id"]) if body.get("customer_id") is not None else None),
                    # customer_tags is intentionally ignored: offer eligibility
                    # is resolved from Shopify for the supplied customer phone.
                    customer_phone=(str(body["customer_phone"]) if body.get("customer_phone") is not None else None),
                    till_session_id=(str(body["till_session_id"]) if body.get("till_session_id") is not None else None),
                    selected_offer_ids=(
                        tuple(str(offer_id) for offer_id in body["offer_ids"])
                        if body.get("offer_ids") is not None else None
                    ),
                )
                if body.get("offer_ids") is not None and (
                    not isinstance(body["offer_ids"], list)
                    or any(not isinstance(offer_id, str) or not offer_id for offer_id in body["offer_ids"])
                ):
                    raise ValueError("offer_ids must be an array of non-empty offer codes")
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
            self._send_json(200, {"locations": [
                {"id": location["id"], "name": location["name"],
                 "store_code": (location.get("eluno") or {}).get("store_code")}
                for location in locations
                if location.get("active") is True
                and (location.get("eluno") or {}).get("kind") == "store"
            ]})

        def _admin_frames(self) -> None:
            if application.shopify_adapter is None:
                self._send_json(503, {"error": "catalog_unavailable", "message": "Shopify catalog is not configured."})
                return
            try:
                variants = []
                for product in application.shopify_adapter.list_products("Frame"):
                    if product.get("status") != "active":
                        continue
                    for variant in product.get("variants", []):
                        metadata = variant.get("eluno") or {}
                        if metadata.get("line_type") != "frame" or not variant.get("inventory_item_id"):
                            continue
                        variants.append({
                            "variant_id": variant["id"],
                            "inventory_item_id": variant["inventory_item_id"],
                            "product_title": product.get("title", ""),
                            "variant_title": variant.get("title", ""),
                            "sku": variant.get("sku", ""),
                        })
            except (HttpRequestError, KeyError, TypeError, ValueError) as exc:
                self._send_json(502, {"error": "catalog_unavailable", "message": str(exc)})
                return
            self._send_json(200, {"variants": variants})

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
                actor = body.get("actor")
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
                actor = body.get("actor")
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
            business_date_text = self._single_query_value(query, "business_date")
            if not business_date_text:
                self._send_json(400, {"error": "invalid_request", "message": "business_date is required."})
                return
            try:
                business_date = date.fromisoformat(business_date_text)
                offers = application.checkout_service.selectable_offers(business_date)
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
                self._send_json(200,{"tills":application.till_store.open_tills(store_code)})
            except ValueError as exc:
                self._send_json(400,{"error":"invalid_request","message":str(exc)})

        def _refund(self):
            if application.refund_service is None: self._send_json(503,{"error":"refund_unavailable"}); return
            try:
                b=self._read_json_object(); r=application.refund_service.refund(b['order_id'],str(b['store_code']),str(b['operator']),str(b['reason']),b.get('till_session_id'),b.get('items'),b.get('idempotency_key'))
            except (KeyError,TypeError,ValueError,OperationsError,HttpRequestError) as e: self._send_json(422,{"error":"refund_invalid","message":str(e)}); return
            self._send_json(201 if r['status']=='COMPLETED' else 202,r)
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
                b=self._read_json_object(); self._send_json(201,application.till_store.open_till(str(b['store_code']),str(b['location_id']),str(b['operator']),int(b['opening_float_paise'])))
            except (KeyError,TypeError,ValueError,OperationsError) as e:self._send_json(422,{"error":"till_invalid","message":str(e)})
        def _till_status(self,path):
            try:self._send_json(200,application.till_store.till_get(path.rsplit('/',1)[-1]))
            except OperationsError as e:self._send_json(404,{"error":"not_found","message":str(e)})
        def _day_end(self, query):
            try:
                store_id=self._single_query_value(query,'store_id'); date_text=self._single_query_value(query,'date')
                if not store_id or not date_text: raise ReportError('store_id and date are required')
                self._send_json(200,application.report_service.report(store_id,date.fromisoformat(date_text)))
            except HttpRequestError:
                self._send_json(502,{"error":"report_upstream_unavailable","message":"The Shopify report data could not be retrieved. Retry the report after checking the Shopify connection."})
            except (ValueError,ReportError) as e:self._send_json(422,{"error":"report_invalid","message":str(e)})
        def _anomalies(self, query):
            try:
                store_id=self._single_query_value(query,'store_id'); date_text=self._single_query_value(query,'date')
                if not store_id or not date_text: raise ReportError('store_id and date are required')
                if application.anomaly_service is None: raise ReportError('anomaly reporting is not configured')
                self._send_json(200,application.anomaly_service.report(store_id,date.fromisoformat(date_text)))
            except HttpRequestError:
                self._send_json(502,{"error":"report_upstream_unavailable","message":"The Shopify report data could not be retrieved. Retry the report after checking the Shopify connection."})
            except (ValueError,ReportError) as e:self._send_json(422,{"error":"report_invalid","message":str(e)})
        def _audit_events(self, query):
            if application.till_store is None:
                self._send_json(503,{"error":"audit_unavailable","message":"Audit storage is not configured."}); return
            entity_id=self._single_query_value(query,'entity_id')
            self._send_json(200,{"events":application.till_store.audit_events(entity_id),"note":"Actors are caller-supplied operator IDs; this application does not authenticate them."})
        def _till_movement(self,path):
            try:
                b=self._read_json_object(); kind=str(b['type'])
                if kind not in ('paid_in','paid_out'): raise OperationsError('cash_sale and cash_refund are system-recorded only')
                if not b.get('reason'): raise OperationsError('reason is required for manual cash movements')
                self._send_json(201,application.till_store.movement(path.split('/')[-2],kind,int(b['amount_paise']),str(b['operator']),b['reason'],b.get('ref')))
            except (KeyError,TypeError,ValueError,OperationsError) as e:self._send_json(422,{"error":"till_invalid","message":str(e)})
        def _close_till(self,path):
            try:
                b=self._read_json_object()
                closed=application.till_store.close_till(
                    path.split('/')[-2],str(b['operator']),int(b['counted_cash_paise']),
                )
                self._send_json(200,{
                    **closed,
                    "cash_variance_alert":application.till_store.cash_variance_alert(closed),
                })
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
                    result = application.checkout_service.recover(sale_id, str(body.get('operator') or 'unattributed'))
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
            business_date_text = self._single_query_value(query, "business_date")
            if not phone or not business_date_text:
                self._send_json(400, {"error": "invalid_request", "message": "phone and business_date are required."})
                return
            try:
                business_date = date.fromisoformat(business_date_text)
            except ValueError:
                self._send_json(400, {"error": "invalid_request", "message": "business_date must use YYYY-MM-DD."})
                return
            try:
                result = application.lookup_customer_prescription(phone, business_date)
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
                optometrist_id = str(body["optometrist_id"]).strip()
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
                    if axis is not None and (axis < 0 or axis > 180 or axis % 5):
                        raise ValueError("{} axis must be between 0 and 180 in 5-degree steps".format(side.upper()))
                    if cyl != 0 and axis is None:
                        raise ValueError("{} axis is required when cylinder is non-zero".format(side.upper()))
                    eyes[side] = {"sph": sph, "cyl": cyl, "axis": axis, "add": add}
                if not store_code or not optometrist_id:
                    raise ValueError("store_code and optometrist_id are required")
                if body.get("optometrist_confirmed") is not True:
                    raise ValueError("an optometrist must confirm the entered prescription")
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                self._send_json(400, {"error": "invalid_request", "message": "Invalid prescription entry: {}".format(exc)})
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
            self._send_json(201, {
                "prescription": prescription,
                "customer": {"id": customer["id"], "phone": customer["phone"]},
                "confirmed_by": optometrist_id,
            })

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
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, status: int, document: str) -> None:
            body=document.encode("utf-8"); self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length",str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def log_message(self, format_string: str, *args: object) -> None:
            return

    return ApiHandler


def serve(address: Tuple[str, int], application: Optional[PosApplication] = None) -> None:
    server = PosHttpServer(address, make_handler(application or default_application()))
    try:
        server.serve_forever()
    finally:
        server.server_close()
