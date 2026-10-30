from decimal import Decimal
from http.server import ThreadingHTTPServer
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
from pathlib import Path

from eluno_pos.auth import AuthManager
from eluno_pos.business_time import bengaluru_today
from eluno_pos.http_app import make_handler, system_status
from eluno_pos.adapters.http import HttpRequestError
from eluno_pos.checkout import CheckoutError, CheckoutResult
from eluno_pos.cart import CartService
from eluno_pos.cli import ApiClient
from eluno_pos.models import CatalogVariant, LensLimits, LensOption, OfferDecision, OfferEvaluation
from eluno_pos.offers import OfferBook, OfferEngine
from eluno_pos.customer_accounts import CustomerAccountStore, CustomerOrderStore
from eluno_pos.operations import OperationsStore, RefundService
from eluno_pos.services import CustomerPrescriptionService, PosApplication
from eluno_pos.tax import TaxRateTable


CUSTOMER = {
    "id": 1, "phone": "+919000000001", "first_name": "Test",
    "last_name": "Customer", "tags": "priority,campaign:festive_frames",
}
PRESCRIPTION = {
    "rx_id": "RX1", "customer_id": 1, "phone": "+919000000001", "date": "2026-01-01", "valid_until": "2027-01-01",
    "od": {"sph": -1, "cyl": -0.5, "axis": 90, "add": None},
    "os": {"sph": -1, "cyl": -0.5, "axis": 90, "add": None},
}


class Customers:
    def search_customers_by_phone(self, phone):
        return [CUSTOMER] if phone == CUSTOMER["phone"] else []

class DuplicateCustomers:
    def search_customers_by_phone(self, phone):
        return [CUSTOMER, dict(CUSTOMER, id=2)]


class Prescriptions:
    def prescriptions_by_phone(self, phone):
        return [PRESCRIPTION] if phone == CUSTOMER["phone"] else []


class Catalog:
    def list_lens_options(self):
        return [
            LensOption(
                "1", "single_vision", Decimal("1.50"), "standard",
                LensLimits(Decimal("-4"), Decimal("4"), Decimal("2"), False),
            )
        ]


class Checkout:
    def __init__(self):
        self.requests = []

    def checkout(self, request):
        self.requests.append(request)
        return CheckoutResult("sale_1", "COMPLETED", "99", "ELU/HSR/FY26/00001", "Checkout completed.")

    def sale_status(self, sale_id):
        if sale_id != "sale_unknown":
            raise CheckoutError("sale {} was not found".format(sale_id))
        return CheckoutResult(sale_id, "PAYMENT_UNKNOWN", None, None, "Sale status retrieved.")

    def invoice_status(self, invoice_no):
        if self.invoice_error:
            raise self.invoice_error
        if invoice_no == "ELU/HSR/FY26/00001":
            return CheckoutResult("sale_1", "COMPLETED", self.invoice_order_id, invoice_no, "Invoice status retrieved.")
        return None

    invoice_error = None
    invoice_order_id = "99"

    def selectable_offers(self, business_date):
        return [SimpleNamespace(offer_id="FESTIVE10",name="Festive discount",stackable=False)]

    def recover(self, sale_id):
        if sale_id == "sale_unknown":
            return CheckoutResult(sale_id, "PAYMENT_UNKNOWN", None, None, "Reconciliation is required.")
        raise CheckoutError("sale is not awaiting external reconciliation")

class Anomalies:
    def report(self, store_id, day):
        return {"store_id": store_id, "business_date": day.isoformat(), "flags": []}


class ProbeShopify:
    def __init__(self, error=None):
        self.calls = []
        self.error = error
        self.registered_customers = []
        self.inventory_available = 3
        self.order_records = []
        self.order_line_items = [{"id": 10, "variant_id": 1, "quantity": 1}]
        self.search_results = None
    def search_customers_by_phone(self, phone):
        matches = [item for item in self.registered_customers if item.get("phone") == phone]
        if phone == CUSTOMER["phone"]:
            matches.append(CUSTOMER)
        return matches
    def list_locations(self):
        self.calls.append("GET locations.json")
        if self.error: raise self.error
        return [{
            "id": 1, "name": "HSR Store", "active": True,
            "eluno": {"kind": "store", "store_code": "HSR"},
        }]
    def register_customer(self, first_name, last_name, phone):
        self.calls.append(("REGISTER", first_name, last_name, phone))
        if self.error: raise self.error
        existing = next((item for item in self.registered_customers if item["phone"] == phone), None)
        if existing:
            if (existing["first_name"], existing["last_name"]) != (first_name, last_name):
                from eluno_pos.adapters.shopify import CustomerRegistrationConflict
                raise CustomerRegistrationConflict("this mobile number already belongs to a different customer")
            return existing, False
        customer = {"id": 42, "first_name": first_name, "last_name": last_name, "phone": phone}
        self.registered_customers.append(customer)
        return customer, True
    def search_customers(self, query, page=1, limit=20):
        if self.search_results is not None:
            matching = [
                customer for customer in self.search_results
                if query.casefold() in " ".join(
                    str(customer.get(key) or "")
                    for key in ("id", "first_name", "last_name", "phone", "email")
                ).casefold()
            ]
        else:
            matching = [
                dict(CUSTOMER, email="test@example.com", total_spent="25.00", orders_count=2)
            ]
            if query.casefold() not in "test customer +919000000001 test@example.com 12345".casefold():
                matching = []
        start = (page - 1) * limit
        return matching[start:start + limit], False
    def list_all_orders(self):
        if self.error:
            raise self.error
        return list(self.order_records)
    def create_customer(self, first_name, last_name, phone, email=None):
        customer = {
            "id": 42 + len(self.registered_customers),
            "first_name": first_name,
            "last_name": last_name,
            "phone": phone,
            "email": email,
            "tags": "",
        }
        self.registered_customers.append(customer)
        return customer
    def get_customer(self, customer_id):
        match = next((item for item in self.registered_customers if str(item["id"]) == str(customer_id)), None)
        return match or dict(CUSTOMER, id=int(customer_id), email="test@example.com", tags="priority")
    def update_customer(self, customer_id, changes):
        customer = self.get_customer(customer_id)
        customer.update(changes)
        return customer
    def list_catalog_variants(self):
        return [CatalogVariant(
            "1", "10", "frame", "90031900", 12000, True,
            product_title="North Frame", variant_title="Black",
        )]
    def get_variant(self, variant_id):
        return {"id": variant_id, "inventory_item_id": 92000001}
    def list_inventory_levels(self, inventory_item_ids, location_id):
        return [{
            "inventory_item_id": 92000001,
            "location_id": location_id,
            "available": self.inventory_available,
        }]
    def get_order(self, order_id):
        return {"id": order_id, "line_items": list(self.order_line_items)}


class RefundLookupShopify:
    def __init__(self):
        self.calls = []
        self.order = {
            "id": 99, "name": "#1099", "location_id": 1, "created_at": "2026-10-29T10:00:00Z",
            "total_price": "150.00", "financial_status": "paid", "cancelled_at": None,
            "customer": {"name": "Test Customer"},
            "note_attributes": [
                {"name": "store_code", "value": "HSR"},
                {"name": "invoice_no", "value": "ELU/HSR/FY26/00001"},
            ],
            "transactions": [{
                "id": 9, "kind": "sale", "status": "success", "gateway": "razorpay_terminal",
                "authorization": "charge-99", "amount": "150.00",
            }],
            "refunds": [],
            "line_items": [
                {"id": 10, "title": "Frame", "quantity": 1, "price": "100.00", "total_discount": "0.00",
                 "tax_lines": [{"price": "18.00"}], "properties": [{"name": "line_type", "value": "frame"}]},
                {"id": 11, "title": "Lens", "quantity": 1, "price": "50.00", "total_discount": "0.00",
                 "tax_lines": [{"price": "2.50"}], "properties": [{"name": "line_type", "value": "lens"}]},
            ],
        }

    def get_order(self, order_id):
        self.calls.append(("GET_ORDER", order_id))
        order = dict(self.order)
        order["id"] = order_id
        if order_id == 100:
            order["note_attributes"] = [
                {"name": "store_code", "value": "HSR"},
                {"name": "invoice_no", "value": "ELU/HSR/FY26/00002"},
            ]
        return order

    def list_locations(self):
        return [{
            "id": 1, "name": "HSR", "active": True,
            "eluno": {"kind": "store", "store_code": "HSR"},
        }]

    def create_refund(self, order_id, payload):
        self.calls.append(("CREATE_REFUND", order_id, payload))
        return {
            "id": 199,
            "order_id": order_id,
            "refund_line_items": payload["refund_line_items"],
            "transactions": [
                dict(transaction, id=299, status="success")
                for transaction in payload["transactions"]
            ],
            "note_attributes": payload["note_attributes"],
        }


class ProbeSmt:
    def __init__(self, error=None): self.calls = []; self.error = error
    def list_staff(self, store_code=None):
        self.calls.append("GET /smt/staff" + ("/" + store_code if store_code else ""))
        if self.error: raise self.error
        return [
            {"staff_id": "S107", "display_name": "Aarav P.", "role": "optometrist", "store_code": "HSR"},
            {"staff_id": "S103", "display_name": "Riya R.", "role": "optometrist", "store_code": "MM"},
        ]


class ProbeTerminal:
    def __init__(self, error=None, result=None): self.calls = []; self.error = error; self.result = result
    def get_charge(self, charge_id):
        self.calls.append(("GET", charge_id))
        if self.error: raise self.error
        return self.result


class MockRefundTerminal:
    def __init__(self): self.calls = []; self.error = None
    def refund(self, charge_id, amount_paise, reason, idempotency_key):
        self.calls.append((charge_id, amount_paise, reason, idempotency_key))
        if self.error: raise self.error
        return {
            "refund_id": "terminal-refund-1",
            "charge_id": charge_id,
            "amount_paise": amount_paise,
            "status": "processed",
        }


class MockPaymentTerminal:
    def __init__(self): self.calls = []
    def charge(self, amount_paise, method, order_ref):
        self.calls.append(("POST", amount_paise, method, order_ref))
        self.charge_result = {
            "charge_id": "mock_charge_1", "status": "captured",
            "amount_paise": amount_paise, "method": method, "order_ref": order_ref,
        }
        return self.charge_result
    def get_charge(self, charge_id):
        self.calls.append(("GET", charge_id))
        if charge_id == "eluno-pos-read-only-status-probe-never-charge":
            raise HttpRequestError(404, "unknown charge")
        return dict(self.charge_result)


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.checkout = Checkout()
        self.shopify_probe = ProbeShopify()
        self.refund_shopify = RefundLookupShopify()
        self.refund_tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.refund_tempdir.cleanup)
        self.refund_store = OperationsStore(self.refund_tempdir.name + "/refunds.sqlite3")
        self.smt_probe = ProbeSmt()
        self.terminal_probe = ProbeTerminal(HttpRequestError(404, "unknown charge"))
        self.application = PosApplication(CustomerPrescriptionService(Customers(), Prescriptions()), Catalog(), self.checkout, anomaly_service=Anomalies(), shopify_adapter=self.shopify_probe, smt_adapter=self.smt_probe, terminal_adapter=self.terminal_probe)
        auth_config = json.dumps({
            "admin": {"password": "Test-Admin-Password-2026!", "role": "admin", "staff_id": "S-ADMIN"},
            "cashier": {"password": "Test-Cashier-Password-2026!", "role": "cashier", "staff_id": "S-CASHIER"},
        })
        with patch.dict(os.environ, {"ELUNO_AUTH_USERS": auth_config}):
            self.application.auth_manager = AuthManager.from_env(Path(self.refund_tempdir.name) / "auth.sqlite3")
        self.application.auth_manager.create_staff(
            "optometrist", "Test-Optometrist-Password-2026!", "optometrist",
            "Test Optometrist", "S-ADMIN", "HSR", "S107",
        )
        self.application.till_store = self.refund_store
        self.application.customer_accounts = CustomerAccountStore(
            Path(self.refund_tempdir.name) / "auth.sqlite3"
        )
        self.application.customer_orders = CustomerOrderStore(
            Path(self.refund_tempdir.name) / "auth.sqlite3"
        )
        self.auth_tokens = {
            role: self.application.auth_manager.login(username, password)[0]
            for role, username, password in (
                ("admin", "admin", "Test-Admin-Password-2026!"),
                ("cashier", "cashier", "Test-Cashier-Password-2026!"),
                ("optometrist", "optometrist", "Test-Optometrist-Password-2026!"),
            )
        }
        self.refund_terminal = MockRefundTerminal()
        self.application.refund_service = RefundService(
            self.refund_shopify, self.refund_store, terminal=self.refund_terminal,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.application))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = "http://127.0.0.1:{}".format(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request_json(self, method, path, body=None, role="admin"):
        request = Request(self.base + path, data=body, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        if role:
            request.add_header("Cookie", "eluno_session={}".format(self.auth_tokens[role]))
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            payload = json.loads(error.read())
            error.close()
            return error.code, payload

    def request_customer_json(self, method, path, body=None, token=None):
        request = Request(self.base + path, data=body, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Cookie", "eluno_customer_session={}".format(token))
        try:
            with urlopen(request) as response:
                payload = response.read()
                return response.status, json.loads(payload) if payload else None
        except HTTPError as error:
            payload = json.loads(error.read())
            error.close()
            return error.code, payload

    def test_browser_pos_assets_are_served_alongside_json_api(self):
        with urlopen(self.base + "/pos") as response:
            html = response.read().decode("utf-8")
            self.assertEqual(200, response.status)
            self.assertIn('<title>| Premium Eyeglasses, Sunglasses &amp; Clip-On</title>', html)
            script_path = html.split('src="', 1)[1].split('"', 1)[0]
            style_path = html.split('href="', 1)[1].split('"', 1)[0]
            self.assertTrue(script_path.startswith("/pos/assets/"))
            self.assertTrue(style_path.startswith("/pos/assets/"))
        with urlopen(self.base + style_path) as response:
            css = response.read().decode("utf-8")
            self.assertIn('--berry-ink:', css)
        with urlopen(self.base + "/pos/images/eluno.png") as response:
            logo = response.read()
            self.assertEqual(200, response.status)
            self.assertTrue(logo.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_authentication_requires_login_and_server_controls_role_permissions(self):
        status, payload = self.request_json("GET", "/api/catalog/variants?line_type=frame", role=None)
        self.assertEqual(401, status)
        self.assertEqual("authentication_required", payload["error"])
        status, payload = self.request_json("GET", "/api/reports/day-end?store_id=1&date=2026-10-26", role="cashier")
        self.assertEqual(403, status)
        self.assertEqual("admin_role_required", payload["error"])
        status, _ = self.request_json("GET", "/api/locations", role="cashier")
        self.assertEqual(200, status)
        status, payload = self.request_json("GET", "/api/admin/locations", role="cashier")
        self.assertEqual(403, status)
        self.assertEqual("admin_role_required", payload["error"])
        status, payload = self.request_json("GET", "/api/auth/me", role="cashier")
        self.assertEqual(200, status)
        self.assertEqual({
            "username": "cashier", "staff_id": "S-CASHIER", "operator_id": "S-CASHIER",
            "role": "cashier", "display_name": "cashier", "store_code": "HSR",
            "external_staff_id": None,
        }, payload["user"])

    def test_staff_roles_cannot_bypass_profile_catalog_clinical_or_financial_permissions(self):
        for role in ("cashier", "optometrist"):
            for path in (
                "/api/customers/1", "/api/admin/catalog/products", "/api/admin/staff",
                "/api/admin/tills?store_code=HSR", "/api/reports/anomalies?store_id=1&date=2026-10-29",
            ):
                status, payload = self.request_json("GET", path, role=role)
                self.assertEqual(403, status, (role, path, payload))
        status, payload = self.request_json("GET", "/api/tills?store_code=HSR", role="optometrist")
        self.assertEqual(403, status)
        self.assertEqual("till_role_required", payload["error"])
        status, payload = self.request_json("POST", "/api/prescriptions", b"{}", role="admin")
        self.assertEqual(403, status, ("admin", payload))
        status, payload = self.request_json("POST", "/api/prescriptions", b"{}", role="cashier")
        self.assertEqual(503, status, ("cashier", payload))
        self.assertEqual("prescription_entry_unavailable", payload["error"])
        status, payload = self.request_json(
            "POST", "/api/prescriptions", b"{}", role="optometrist",
        )
        self.assertEqual(503, status)
        self.assertEqual("prescription_entry_unavailable", payload["error"])
        status, _ = self.request_json("GET", "/api/customers/search?q=Test", role="cashier")
        self.assertEqual(200, status)
        status, payload = self.request_json(
            "POST", "/api/admin/staff",
            json.dumps({
                "username": "clinic-one",
                "password": "Clinic-Optometrist-Password-2026!",
                "display_name": "Clinic One",
                "role": "optometrist",
                "store_code": "HSR",
                "external_staff_id": "S107",
            }).encode(),
        )
        self.assertEqual(201, status, payload)
        self.assertEqual("S107", payload["staff"]["external_staff_id"])
        self.assertNotIn("password_hash", payload["staff"])
        status, denied = self.request_json("GET", "/api/admin/staff", role="cashier")
        self.assertEqual(403, status)
        self.assertEqual("admin_role_required", denied["error"])

    def test_cashier_till_operations_use_session_identity_and_reject_fabricated_sales(self):
        self.shopify_probe.list_locations = lambda: [
            {"id": 1, "name": "HSR Store", "active": True, "eluno": {"kind": "store", "store_code": "HSR"}},
            {"id": 2, "name": "MM Store", "active": True, "eluno": {"kind": "store", "store_code": "MM"}},
        ]
        status, denied = self.request_json(
            "POST", "/api/tills",
            json.dumps({"store_code": "MM", "location_id": "2", "opening_float_paise": 0}).encode(),
            role="cashier",
        )
        self.assertEqual(403, status)
        self.assertEqual("store_access_denied", denied["error"])
        status, opened = self.request_json(
            "POST", "/api/tills",
            json.dumps({
                "store_code": "HSR", "location_id": "1",
                "opening_float_paise": 100000, "operator": "forged-operator",
            }).encode(),
            role="cashier",
        )
        self.assertEqual(201, status, opened)
        self.assertEqual("S-CASHIER", opened["opened_by"])
        status, rejected = self.request_json(
            "POST", "/api/tills/{}/movements".format(opened["session_id"]),
            json.dumps({"type": "cash_sale", "amount_paise": 50000, "ref": "fake-sale"}).encode(),
            role="cashier",
        )
        self.assertEqual(422, status)
        self.assertIn("system-recorded only", rejected["message"])
        status, updated = self.request_json(
            "POST", "/api/tills/{}/movements".format(opened["session_id"]),
            json.dumps({
                "type": "paid_in", "amount_paise": 5000,
                "reason": "float change", "ref": "manual-test-1",
                "operator": "forged-operator",
            }).encode(),
            role="cashier",
        )
        self.assertEqual(201, status)
        self.assertEqual(105000, updated["expected_paise"])
        self.assertEqual("S-CASHIER", updated["movements"][0]["operator"])
        status, closed = self.request_json(
            "POST", "/api/tills/{}/close".format(opened["session_id"]),
            json.dumps({"counted_cash_paise": 105000}).encode(),
            role="cashier",
        )
        self.assertEqual(200, status)
        self.assertEqual("S-CASHIER", closed["closed_by"])

    def test_business_date_endpoint_uses_bengaluru_clock_and_is_read_only(self):
        fixed = SimpleNamespace(isoformat=lambda: "2026-10-30")
        with patch("eluno_pos.http_app.bengaluru_today", return_value=fixed):
            with patch("eluno_pos.http_app.business_timezone_name", return_value="Asia/Kolkata"):
                status, payload = self.request_json("GET", "/api/business-date")
        self.assertEqual(200, status)
        self.assertEqual({"business_date": "2026-10-30", "timezone": "Asia/Kolkata"}, payload)

    def test_staff_customer_search_returns_only_non_sensitive_paged_summaries(self):
        status, payload = self.request_json(
            "GET", "/api/customers/search?q=Test&page=1", role="cashier",
        )
        self.assertEqual(200, status)
        self.assertEqual("1", payload["customers"][0]["id"])
        self.assertEqual(2500, payload["customers"][0]["total_spent_paise"])
        self.assertEqual(2, payload["customers"][0]["order_count"])
        self.assertNotIn("default_address", payload["customers"][0])
        self.assertNotIn("dob", payload["customers"][0])
        status, denied = self.request_json(
            "GET", "/api/customers/search?q=12345&page=1", role="cashier",
        )
        self.assertEqual(403, status)
        self.assertEqual("customer_id_search_admin_only", denied["error"])
        status, by_id = self.request_json(
            "GET", "/api/customers/search?q=12345&page=1", role="admin",
        )
        self.assertEqual(200, status)
        self.assertEqual(1, len(by_id["customers"]))

    def test_staff_customer_search_derives_missing_metrics_from_paid_orders_and_refunds(self):
        self.shopify_probe.search_results = [{
            "id": 42,
            "first_name": "Yashu",
            "last_name": "Youwaraj",
            "phone": "+917970629433",
            "email": "yashu@example.com",
        }]
        self.shopify_probe.order_records = [
            {
                "id": 10,
                "customer": {"id": 42},
                "financial_status": "partially_refunded",
                "total_price": "120.00",
                "processed_at": "2026-10-28T11:00:00+05:30",
                "refunds": [{
                    "transactions": [
                        {"kind": "refund", "status": "success", "amount": "20.00"},
                        {"kind": "refund", "status": "pending", "amount": "70.00"},
                    ],
                }],
            },
            {
                "id": 11,
                "customer": {"id": 42},
                "financial_status": "pending",
                "total_price": "50.00",
                "processed_at": "2026-10-29T11:00:00+05:30",
                "refunds": [],
            },
            {
                "id": 12,
                "customer": {"id": 43},
                "financial_status": "paid",
                "total_price": "999.00",
                "processed_at": "2026-10-30T11:00:00+05:30",
                "refunds": [],
            },
        ]

        status, payload = self.request_json(
            "GET", "/api/customers/search?q=Yashu&page=1", role="cashier",
        )

        self.assertEqual(200, status)
        self.assertEqual({
            "total_spent_paise": 10000,
            "order_count": 1,
            "total_refunded_paise": 2000,
            "most_recent_purchase": "2026-10-28T11:00:00+05:30",
        }, {key: payload["customers"][0][key] for key in (
            "total_spent_paise", "order_count", "total_refunded_paise", "most_recent_purchase",
        )})

    def test_cashier_store_assignment_limits_location_list_and_checkout(self):
        self.shopify_probe.list_locations = lambda: [
            {"id": 1, "name": "HSR", "active": True, "eluno": {"kind": "store", "store_code": "HSR"}},
            {"id": 2, "name": "MM", "active": True, "eluno": {"kind": "store", "store_code": "MM"}},
        ]
        status, payload = self.request_json("GET", "/api/locations", role="cashier")
        self.assertEqual(200, status)
        self.assertEqual(["HSR"], [location["store_code"] for location in payload["locations"]])
        request = json.dumps({
            "idempotency_key": "cross-store-attempt",
            "business_date": "1900-01-01",
            "payment_method": "upi",
            "channel": "walk_in",
            "cart": {"store_code": "MM", "location_id": "2", "lines": [{"variant_id": "1", "quantity": 1}]},
        }).encode()
        status, denied = self.request_json("POST", "/api/checkouts", request, role="cashier")
        self.assertEqual(403, status)
        self.assertEqual("store_access_denied", denied["error"])
        self.assertEqual([], self.checkout.requests)

    def test_checkout_and_refund_actions_are_cashier_only_but_admin_can_view_invoice_documents(self):
        checkout_body = json.dumps({
            "idempotency_key": "role-checkout",
            "business_date": "2026-10-29",
            "payment_method": "upi",
            "channel": "walk_in",
            "cart": {"store_code": "HSR", "location_id": "1", "lines": [{"variant_id": "1", "quantity": 1}]},
        }).encode()
        status, payload = self.request_json("POST", "/api/checkouts", checkout_body, role=None)
        self.assertEqual(401, status)
        self.assertEqual("authentication_required", payload["error"])
        status, payload = self.request_json("POST", "/api/refunds", b"{}", role=None)
        self.assertEqual(401, status)
        self.assertEqual("authentication_required", payload["error"])
        status, payload = self.request_json("POST", "/api/checkouts", checkout_body, role="admin")
        self.assertEqual(403, status)
        self.assertEqual("cashier_role_required", payload["error"])
        self.assertEqual([], self.checkout.requests)
        status, payload = self.request_json("POST", "/api/checkouts", checkout_body, role="cashier")
        self.assertEqual(201, status)
        self.assertEqual("S-CASHIER", self.checkout.requests[-1].staff_id)
        self.assertEqual(bengaluru_today(), self.checkout.requests[-1].business_date)

        refund_body = json.dumps({
            "order_id": "99", "invoice_no": "ELU/HSR/FY26/00001", "store_code": "HSR",
            "operator": "forged-operator", "reason": "CHANGED_MIND", "idempotency_key": "role-refund",
        }).encode()
        status, payload = self.request_json("POST", "/api/refunds", refund_body, role="admin")
        self.assertEqual(403, status)
        self.assertEqual("cashier_role_required", payload["error"])
        status, payload = self.request_json(
            "GET", "/api/refunds/lookup?identifier=99&store_code=HSR", role="admin",
        )
        self.assertEqual(403, status)
        self.assertEqual("cashier_role_required", payload["error"])
        self.assertEqual([], self.refund_terminal.calls)
        status, payload = self.request_json("POST", "/api/refunds", refund_body, role="cashier")
        self.assertEqual(201, status)
        self.assertEqual(1, len(self.refund_terminal.calls))

        self.application.document_service = SimpleNamespace(
            invoice_html=lambda sale_id: "<!doctype html><title>Persisted invoice {}</title>".format(sale_id),
        )
        request = Request(self.base + "/api/invoices/sale_1")
        request.add_header("Cookie", "eluno_session={}".format(self.auth_tokens["admin"]))
        with urlopen(request) as response:
            self.assertEqual(200, response.status)
            self.assertEqual("text/html; charset=utf-8", response.headers["Content-Type"])
            self.assertIn("Persisted invoice sale_1", response.read().decode())

    def test_local_staff_database_is_available_without_environment_configuration(self):
        self.assertTrue(self.application.auth_manager.configured)
        status, payload = self.request_json("GET", "/api/catalog/variants?line_type=frame", role=None)
        self.assertEqual(401, status)
        self.assertEqual("authentication_required", payload["error"])

    def test_staff_management_is_admin_only_and_never_returns_password_hashes(self):
        status, _ = self.request_json("GET", "/api/admin/staff", role=None)
        self.assertEqual(401, status)
        status, payload = self.request_json("GET", "/api/admin/staff", role="cashier")
        self.assertEqual(403, status)
        self.assertEqual("admin_role_required", payload["error"])
        status, payload = self.request_json("GET", "/api/admin/staff")
        self.assertEqual(200, status)
        self.assertEqual({"admin", "cashier", "optometrist"}, {item["username"] for item in payload["staff"]})
        self.assertTrue(all("password_hash" not in item and "password" not in item for item in payload["staff"]))
        self.assertTrue(all(item["operator_id"] == item["staff_id"] for item in payload["staff"]))

    def test_admin_can_create_staff_reset_password_and_disable_accounts(self):
        status, created = self.request_json("POST", "/api/admin/staff", json.dumps({
            "username": "new.cashier", "display_name": "New Cashier",
            "role": "cashier", "password": "New-Cashier-Password-2026!",
            "store_code": "HSR",
        }).encode())
        self.assertEqual(201, status, created)
        self.assertEqual("cashier", created["staff"]["role"])
        self.assertEqual("HSR", created["staff"]["store_code"])
        self.assertNotIn("password_hash", created["staff"])
        staff_id = created["staff"]["staff_id"]

        status, created_admin = self.request_json("POST", "/api/admin/staff", json.dumps({
            "username": "new.admin", "display_name": "New Admin",
            "role": "admin", "password": "New-Admin-Password-2026!",
        }).encode())
        self.assertEqual(201, status)
        self.assertEqual("admin", created_admin["staff"]["role"])
        self.assertNotIn("password_hash", created_admin["staff"])

        status, duplicate = self.request_json("POST", "/api/admin/staff", json.dumps({
            "username": "NEW.CASHIER", "display_name": "Duplicate",
            "role": "admin", "password": "Duplicate-Password-2026!",
        }).encode())
        self.assertEqual(409, status)
        self.assertEqual("duplicate_username", duplicate["error"])

        status, reset = self.request_json("PATCH", "/api/admin/staff/" + staff_id, json.dumps({
            "password": "Reset-Cashier-Password-2026!",
        }).encode())
        self.assertEqual(200, status)
        self.assertNotIn("password_hash", reset["staff"])
        self.assertIsNotNone(self.application.auth_manager.login(
            "new.cashier", "Reset-Cashier-Password-2026!", "127.0.0.22",
        ))

        status, disabled = self.request_json("PATCH", "/api/admin/staff/" + staff_id, json.dumps({
            "is_active": False,
        }).encode())
        self.assertEqual(200, status)
        self.assertFalse(disabled["staff"]["is_active"])
        self.assertIsNone(self.application.auth_manager.login(
            "new.cashier", "Reset-Cashier-Password-2026!", "127.0.0.23",
        ))

    def test_cashier_cannot_access_admin_only_endpoints_even_with_client_role_claim(self):
        get_paths = (
            "/api/admin/staff",
            "/api/admin/locations",
            "/api/admin/catalog/frames?location_id=1",
            "/api/reports/day-end?store_id=1&date=2026-10-26",
            "/api/reports/anomalies?store_id=1&date=2026-10-26",
            "/api/audit-events",
            "/api/billing-anomalies",
            "/api/system-status",
        )
        for path in get_paths:
            with self.subTest(path=path):
                status, payload = self.request_json("GET", path, role="cashier")
                self.assertEqual(403, status)
                self.assertEqual("admin_role_required", payload["error"])

        post_paths = (
            "/api/admin/staff",
            "/api/admin/catalog/products",
            "/api/admin/inventory/receipts",
            "/api/billing-anomalies",
            "/api/system-status/mock-payment-test",
        )
        for path in post_paths:
            with self.subTest(path=path):
                status, payload = self.request_json(
                    "POST", path,
                    json.dumps({"role": "admin", "username": "forged"}).encode(),
                    role="cashier",
                )
                self.assertEqual(403, status)
                self.assertEqual("admin_role_required", payload["error"])

    def test_login_issues_an_http_only_session_cookie_and_logout_revokes_it(self):
        request = Request(
            self.base + "/api/auth/login",
            data=json.dumps({"username": "cashier", "password": "Test-Cashier-Password-2026!"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            self.assertEqual(200, response.status)
            self.assertIn("HttpOnly", response.headers["Set-Cookie"])
            self.assertIn("SameSite=Strict", response.headers["Set-Cookie"])
            self.assertNotIn("Secure", response.headers["Set-Cookie"])
            token = response.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        unauthenticated = Request(
            self.base + "/api/auth/me",
            headers={"Cookie": "eluno_session=invalid-session"},
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(unauthenticated)
        self.assertEqual(401, raised.exception.code)
        raised.exception.close()
        request = Request(self.base + "/api/auth/me", headers={"Cookie": "eluno_session=" + token})
        with urlopen(request) as response:
            self.assertEqual("cashier", json.loads(response.read())["user"]["role"])
        request = Request(self.base + "/api/auth/logout", data=b"{}", headers={"Cookie": "eluno_session=" + token}, method="POST")
        with urlopen(request) as response:
            self.assertEqual(204, response.status)
        request = Request(self.base + "/api/auth/me", headers={"Cookie": "eluno_session=" + token})
        with self.assertRaises(HTTPError) as raised:
            urlopen(request)
        self.assertEqual(401, raised.exception.code)
        raised.exception.close()

    def test_login_throttling_and_cross_origin_mutations_are_rejected(self):
        body = json.dumps({"username": "admin", "password": "incorrect"}).encode()
        for _ in range(4):
            status, _ = self.request_json("POST", "/api/auth/login", body, role=None)
            self.assertEqual(401, status)
        status, throttled = self.request_json("POST", "/api/auth/login", body, role=None)
        self.assertEqual(429, status)
        self.assertEqual("login_throttled", throttled["error"])

        request = Request(
            self.base + "/api/admin/staff",
            data=json.dumps({
                "username": "csrf-user", "password": "Strong-Cross-Site-2026!",
                "role": "cashier", "display_name": "CSRF",
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "Cookie": "eluno_session={}".format(self.auth_tokens["admin"]),
                "Origin": "http://attacker.example",
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request)
        self.assertEqual(403, raised.exception.code)
        raised.exception.close()

        unauthenticated = Request(
            self.base + "/api/admin/staff",
            data=b"{}",
            headers={"Content-Type": "application/json", "Origin": "http://attacker.example"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(unauthenticated)
        self.assertEqual(401, raised.exception.code)
        raised.exception.close()

    def test_https_cookie_configuration_adds_secure_attribute(self):
        request = Request(
            self.base + "/api/auth/login",
            data=json.dumps({
                "username": "admin", "password": "Test-Admin-Password-2026!",
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": "https://" + self.base.split("://", 1)[1],
            },
            method="POST",
        )
        with patch.dict(os.environ, {"ELUNO_COOKIE_SECURE": "1"}):
            with urlopen(request) as response:
                self.assertIn("Secure", response.headers["Set-Cookie"])

    def test_optional_cli_api_client_can_login_and_reuse_its_session(self):
        client = ApiClient(self.base)
        status, _ = client.request("POST", "/api/auth/login", {
            "username": "cashier", "password": "Test-Cashier-Password-2026!",
        })
        self.assertEqual(200, status)
        status, current = client.request("GET", "/api/auth/me")
        self.assertEqual(200, status)
        self.assertEqual("cashier", current["user"]["role"])

    def test_customer_prescription_endpoint_success_and_not_found(self):
        status, payload = self.request_json("GET", "/api/customer-prescription?phone=%2B919000000001&business_date=2026-10-26", role="cashier")
        self.assertEqual(200, status)
        self.assertEqual("ready", payload["status"])
        self.assertEqual("RX1", payload["prescription"]["rx_id"])
        self.assertEqual(["campaign:festive_frames", "priority"],payload["customer"]["tags"])
        status, payload = self.request_json("GET", "/api/customer-prescription?phone=%2B919000009999&business_date=2026-10-26", role="cashier")
        self.assertEqual(404, status)
        self.assertEqual("customer_not_found", payload["status"])

    def test_customer_without_valid_prescription_is_returned_as_a_successful_lookup(self):
        class NoPrescriptions:
            def prescriptions_by_phone(self, phone):
                return []

        self.application.lookup_service=CustomerPrescriptionService(Customers(),NoPrescriptions())
        status,payload=self.request_json(
            "GET","/api/customer-prescription?phone=%2B919000000001&business_date=2026-10-26", role="cashier",
        )
        self.assertEqual(200,status)
        self.assertEqual("no_valid_prescription",payload["status"])
        self.assertEqual("Test",payload["customer"]["first_name"])
        self.assertNotIn("prescription",payload)

    def test_ambiguous_customer_lookup_does_not_return_an_unverified_customer(self):
        self.application.lookup_service=CustomerPrescriptionService(
            DuplicateCustomers(),Prescriptions(),
        )
        status,payload=self.request_json(
            "GET","/api/customer-prescription?phone=%2B919000000001&business_date=2026-10-26", role="cashier",
        )
        self.assertEqual(409,status)
        self.assertEqual("customer_ambiguous",payload["status"])
        self.assertNotIn("customer",payload)

    def test_customer_registration_validates_and_is_safe_to_repeat(self):
        status, invalid = self.request_json("POST", "/api/customers", json.dumps({
            "name": "   ", "phone": "123",
        }).encode())
        self.assertEqual(400, status)
        self.assertEqual("invalid_customer", invalid["error"])
        self.assertEqual([], self.shopify_probe.calls)

        request_body = json.dumps({"name": "Asha Rao", "phone": "9876543210"}).encode()
        status, created = self.request_json("POST", "/api/customers", request_body)
        self.assertEqual(201, status)
        self.assertEqual("Asha", created["customer"]["first_name"])
        self.assertEqual("Rao", created["customer"]["last_name"])
        self.assertEqual("+919876543210", created["customer"]["phone"])
        self.assertTrue(created["created"])

        status, repeated = self.request_json("POST", "/api/customers", request_body)
        self.assertEqual(200, status)
        self.assertEqual(created["customer"]["id"], repeated["customer"]["id"])
        self.assertFalse(repeated["created"])
        status, conflict = self.request_json("POST", "/api/customers", json.dumps({
            "name": "Different Person", "phone": "+919876543210",
        }).encode())
        self.assertEqual(409, status)
        self.assertEqual("customer_conflict", conflict["error"])

    def test_customer_registration_reports_upstream_unavailable(self):
        self.shopify_probe.error = HttpRequestError(None, "connection refused")
        status, payload = self.request_json("POST", "/api/customers", json.dumps({
            "name": "Asha Rao", "phone": "9876543210",
        }).encode())
        self.assertEqual(502, status)
        self.assertEqual("customer_registration_upstream_error", payload["error"])

    def test_customer_profile_history_and_admin_anonymization_preserve_records(self):
        order = {
            "id": 99,
            "name": "#1099",
            "location_id": 1,
            "created_at": "2026-10-29T10:00:00Z",
            "processed_at": "2026-10-29T10:00:00Z",
            "financial_status": "paid",
            "total_price": "118.00",
            "customer": {"id": 1},
            "note_attributes": [
                {"name": "invoice_no", "value": "ELU/HSR/FY26/00012"},
                {"name": "store_code", "value": "HSR"},
                {"name": "payment_method", "value": "upi"},
            ],
            "transactions": [{"kind": "sale", "status": "success", "gateway": "upi"}],
            "line_items": [{
                "title": "North Frame",
                "quantity": 1,
                "price": "118.00",
                "total_discount": "0.00",
                "tax_lines": [
                    {"title": "CGST", "price": "9.00"},
                    {"title": "SGST", "price": "9.00"},
                ],
            }],
            "refunds": [],
        }

        class ProfileShopify:
            def __init__(self):
                self.customer = dict(CUSTOMER)
                self.orders = [order]
                self.changes = []

            def get_customer(self, customer_id):
                self.assert_customer_id = customer_id
                return dict(self.customer)

            def list_orders(self, **_kwargs):
                return self.orders

            def update_customer(self, customer_id, changes):
                self.changes.append((customer_id, dict(changes)))
                self.customer.update(changes)
                return dict(self.customer)

        class ProfileSmt:
            def prescriptions_by_customer(self, customer_id):
                return [{"rx_id": "RX1", "customer_id": customer_id}]

        profile_shopify = ProfileShopify()
        self.application.shopify_adapter = profile_shopify
        self.application.smt_adapter = ProfileSmt()

        status, profile = self.request_json("GET", "/api/customers/1", role="admin")
        self.assertEqual(200, status)
        self.assertEqual(1, profile["purchase_count"])
        self.assertEqual(11800, profile["lifetime_spend_paise"])
        self.assertEqual("ELU/HSR/FY26/00012", profile["purchases"][0]["invoice_no"])
        self.assertEqual("RX1", profile["prescriptions"][0]["rx_id"])

        profile_shopify.orders = []
        status, empty_profile = self.request_json("GET", "/api/customers/1", role="admin")
        self.assertEqual(200, status)
        self.assertEqual(0, empty_profile["purchase_count"])
        self.assertEqual(0, empty_profile["lifetime_spend_paise"])
        self.assertIsNone(empty_profile["most_recent_purchase"])
        profile_shopify.orders = [order]

        status, denied = self.request_json(
            "GET", "/api/customers/1", role="cashier",
        )
        self.assertEqual(403, status)
        status, denied = self.request_json(
            "PATCH", "/api/customers/1",
            json.dumps({"first_name": "Asha"}).encode(), role="cashier",
        )
        self.assertEqual(403, status)
        self.assertEqual("admin_role_required", denied["error"])

        status, updated = self.request_json(
            "PATCH", "/api/customers/1",
            json.dumps({"first_name": "Asha"}).encode(), role="admin",
        )
        self.assertEqual(200, status)
        self.assertEqual("Asha", updated["customer"]["first_name"])

        status, denied = self.request_json(
            "POST", "/api/customers/1/anonymize",
            json.dumps({"reason": "customer privacy request"}).encode(), role="cashier",
        )
        self.assertEqual(403, status)
        status, anonymized = self.request_json(
            "POST", "/api/customers/1/anonymize",
            json.dumps({"reason": "customer privacy request"}).encode(), role="admin",
        )
        self.assertEqual(200, status)
        self.assertTrue(anonymized["customer"]["anonymized"])
        self.assertIsNone(anonymized["customer"]["phone"])
        self.assertEqual("Anonymized", anonymized["customer"]["first_name"])
        self.assertIn("retained", anonymized["message"])
        self.assertEqual(2, len(profile_shopify.changes))
        self.assertEqual(1, len(profile_shopify.orders))

    def test_prescription_entry_is_disabled_outside_configured_local_mock(self):
        status, payload = self.request_json("POST", "/api/prescriptions", b"{}", role="optometrist")
        self.assertEqual(503, status)
        self.assertEqual("prescription_entry_unavailable", payload["error"])
        status, payload = self.request_json("POST", "/api/prescriptions", b"{}", role="cashier")
        self.assertEqual(503, status)
        self.assertEqual("prescription_entry_unavailable", payload["error"])
        self.assertEqual([], self.shopify_probe.calls)

    def test_root_and_pos_serve_browser_and_api_route_describes_service(self):
        with urlopen(self.base + "/") as response:
            root = response.read().decode("utf-8")
            self.assertEqual(200, response.status)
        self.assertIn("<title>| Premium Eyeglasses, Sunglasses &amp; Clip-On</title>", root)
        self.assertIn("/pos/assets/", root)
        status, payload = self.request_json("GET", "/api")
        self.assertEqual(200, status)
        self.assertEqual("eluno-pos", payload["service"])
        self.assertEqual("/health", payload["health"])
        self.assertIn("/api/checkouts", payload["endpoint_groups"]["checkout"])
        self.assertIn("/api/system-status", payload["endpoint_groups"]["system_status"])
        self.assertIn("python scripts/start_local.py", payload["cashier"])
        with urlopen(self.base + "/pos") as response:
            pos = response.read().decode("utf-8")
            self.assertEqual(200, response.status)
        self.assertIn("<title>| Premium Eyeglasses, Sunglasses &amp; Clip-On</title>", pos)
        status, health = self.request_json("GET", "/health")
        self.assertEqual(200, status); self.assertEqual({"ok": True, "service": "eluno-pos"}, health)
        status, missing = self.request_json("GET", "/not-a-route")
        self.assertEqual(404, status); self.assertEqual("not_found", missing["error"])

    def test_refund_lookup_resolves_formatted_invoice_to_authoritative_order(self):
        status,payload=self.request_json(
            "GET","/api/refunds/lookup?identifier=ELU%2FHSR%2FFY26%2F00001",
            role="cashier",
        )
        self.assertEqual(200,status)
        self.assertEqual("ELU/HSR/FY26/00001",payload["invoice_no"])
        self.assertEqual("99",payload["order_id"])
        self.assertTrue(payload["refund_eligible"])
        self.assertEqual(15000,payload["total_paise"])
        self.assertIn(("GET_ORDER",99),self.refund_shopify.calls)

    def test_refund_lookup_accepts_numeric_shopify_order_id(self):
        status,payload=self.request_json("GET","/api/refunds/lookup?identifier=99",role="cashier")
        self.assertEqual(200,status)
        self.assertEqual("99",payload["order_id"])
        self.assertEqual("ELU/HSR/FY26/00001",payload["invoice_no"])
        self.assertTrue(payload["refund_eligible"])

    def test_refund_lookup_reports_unknown_invoice(self):
        status,payload=self.request_json(
            "GET","/api/refunds/lookup?identifier=ELU%2FHSR%2FFY26%2F99999&store_code=HSR",
            role="cashier",
        )
        self.assertEqual(404,status)
        self.assertEqual("invoice_not_found",payload["error"])
        self.assertIn("Shopify order ID",payload["message"])

    def test_refund_lookup_rejects_invoice_mapped_to_different_shopify_order(self):
        self.checkout.invoice_order_id="100"
        status,payload=self.request_json(
            "GET","/api/refunds/lookup?identifier=ELU%2FHSR%2FFY26%2F00001&store_code=HSR",
            role="cashier",
        )
        self.assertEqual(409,status)
        self.assertEqual("invoice_order_mismatch",payload["error"])
        self.assertIn(("GET_ORDER",100),self.refund_shopify.calls)

    def test_refund_lookup_rejects_ambiguous_or_uncompleted_ledger_mapping(self):
        self.checkout.invoice_error=CheckoutError("invoice number is ambiguous in the POS sale ledger")
        status,payload=self.request_json(
            "GET","/api/refunds/lookup?identifier=ELU%2FHSR%2FFY26%2F00001&store_code=HSR",
            role="cashier",
        )
        self.assertEqual(409,status)
        self.assertEqual("invoice_ambiguous",payload["error"])
        self.checkout.invoice_error=None
        self.checkout.invoice_order_id=None
        status,payload=self.request_json(
            "GET","/api/refunds/lookup?identifier=ELU%2FHSR%2FFY26%2F00001&store_code=HSR",
            role="cashier",
        )
        self.assertEqual(409,status)
        self.assertEqual("invoice_order_unavailable",payload["error"])

    def test_refund_lookup_rejects_incomplete_shopify_order_response(self):
        self.refund_shopify.order={"id":99,"note_attributes":[
            {"name":"store_code","value":"HSR"},
            {"name":"invoice_no","value":"ELU/HSR/FY26/00001"},
        ]}
        status,payload=self.request_json(
            "GET","/api/refunds/lookup?identifier=ELU%2FHSR%2FFY26%2F00001&store_code=HSR",
            role="cashier",
        )
        self.assertEqual(422,status)
        self.assertEqual("refund_lookup_invalid",payload["error"])
        self.assertIn("transactions",payload["message"])

    def test_duplicate_refund_submission_replays_same_record_and_order_id(self):
        body=json.dumps({
            "order_id":"99","invoice_no":"ELU/HSR/FY26/00001","store_code":"HSR",
            "operator":"S1","reason":"CHANGED_MIND","idempotency_key":"refund-duplicate",
        }).encode()
        first_status,first=self.request_json("POST","/api/refunds",body,role="cashier")
        second_status,second=self.request_json("POST","/api/refunds",body,role="cashier")
        self.assertEqual(201,first_status)
        self.assertEqual(201,second_status)
        self.assertEqual(first["refund_id"],second["refund_id"])
        self.assertEqual("99",second["order_id"])
        self.assertEqual(1,len(self.refund_terminal.calls))
        self.assertEqual("ELU/HSR/FY26/00001",second["invoice_no"])
        refund_calls=[call for call in self.refund_shopify.calls if call[0]=="CREATE_REFUND"]
        self.assertEqual(1,len(refund_calls))

    def test_refund_recovery_endpoint_retries_terminal_idempotently_and_verifies_shopify(self):
        body=json.dumps({
            "order_id":"99","invoice_no":"ELU/HSR/FY26/00001","store_code":"HSR",
            "operator":"S1","reason":"CHANGED_MIND","idempotency_key":"refund-recovery-api",
        }).encode()
        self.refund_terminal.error=HttpRequestError(None,"terminal response lost")
        status,pending=self.request_json("POST","/api/refunds",body,role="cashier")
        self.assertEqual(202,status)
        self.assertEqual("REFUND_UNKNOWN",pending["status"])
        self.refund_terminal.error=None
        status,recovered=self.request_json(
            "POST","/api/refunds/{}/recover".format(pending["refund_id"]),b"{}",role="cashier",
        )
        self.assertEqual(202,status)
        self.assertEqual("COMPLETED",recovered["status"])
        self.assertEqual(2,len(self.refund_terminal.calls))
        self.assertEqual(
            self.refund_terminal.calls[0][3],
            self.refund_terminal.calls[1][3],
        )
        self.assertEqual(1,len([
            call for call in self.refund_shopify.calls if call[0]=="CREATE_REFUND"
        ]))

    def test_refund_endpoint_rejects_invoice_as_order_id_without_numeric_conversion(self):
        status,payload=self.request_json("POST","/api/refunds",json.dumps({
            "order_id":"ELU/HSR/FY26/00001","store_code":"HSR","operator":"S1","reason":"CHANGED_MIND",
        }).encode(),role="cashier")
        self.assertEqual(422,status)
        self.assertIn("look up the invoice first",payload["message"])

    def test_invoice_number_lookup_resolves_sale_and_unknown_invoice(self):
        status,payload=self.request_json("GET","/api/invoices/lookup?invoice_no=ELU%2FHSR%2FFY26%2F00001")
        self.assertEqual(200,status)
        self.assertEqual("sale_1",payload["sale_id"])
        self.assertEqual("COMPLETED",payload["status"])
        status,payload=self.request_json("GET","/api/invoices/lookup?invoice_no=missing")
        self.assertEqual(404,status)
        self.assertEqual("not_found",payload["error"])

    def test_invoice_number_lookup_requires_invoice_number(self):
        status,payload=self.request_json("GET","/api/invoices/lookup")
        self.assertEqual(400,status)
        self.assertEqual("invalid_request",payload["error"])

    def test_catalog_variant_list_is_filtered_to_active_requested_item_type(self):
        self.application.catalog.list_catalog_variants=lambda:[
            SimpleNamespace(variant_id="F1",line_type="frame",product_title="Frame",variant_title="Black",price_paise=12000,active=True,lens_type=None,index=None,coating=None),
            SimpleNamespace(variant_id="L1",line_type="lens",product_title="Lens",variant_title="Blue cut",price_paise=4000,active=True,lens_type="single_vision",index=Decimal("1.60"),coating="blue_cut"),
            SimpleNamespace(variant_id="F2",line_type="frame",product_title="Inactive",variant_title="Hidden",price_paise=9000,active=False,lens_type=None,index=None,coating=None),
        ]
        status,payload=self.request_json("GET","/api/catalog/variants?line_type=lens")
        self.assertEqual(200,status)
        self.assertEqual(["L1"],[variant["variant_id"] for variant in payload["variants"]])
        self.assertEqual("1.60",payload["variants"][0]["index"])
        status,payload=self.request_json("GET","/api/catalog/variants?line_type=unknown")
        self.assertEqual(400,status)
        self.assertEqual("invalid_request",payload["error"])

    def test_admin_frame_inventory_is_returned_for_the_selected_store_location(self):
        self.shopify_probe.list_products = lambda product_type=None: [{
            "id": 701, "title": "North Frame", "status": "active",
            "variants": [{
                "id": 801, "inventory_item_id": 901, "title": "Black",
                "sku": "NORTH-BLK", "price": "49.99",
                "eluno": {
                    "line_type": "frame", "category": "optical",
                    "material": "TR90", "size": "52-18-140",
                },
            }],
        }]
        self.shopify_probe.list_inventory_levels = lambda inventory_item_ids, location_id: [{
            "inventory_item_id": 901, "location_id": location_id, "available": 3,
        }]
        status, payload = self.request_json(
            "GET", "/api/admin/catalog/frames?location_id=1",
        )
        self.assertEqual(200, status)
        self.assertEqual([{
            "variant_id": 801, "inventory_item_id": 901,
            "product_title": "North Frame", "variant_title": "Black",
            "sku": "NORTH-BLK", "price_paise": 4999,
            "category": "optical", "material": "TR90", "size": "52-18-140",
            "available": 3, "location_id": 1,
        }], payload["variants"])

    def test_offer_list_and_checkout_preserve_manual_offer_selection(self):
        status,payload=self.request_json("GET","/api/offers?business_date=2026-10-27")
        self.assertEqual(200,status)
        self.assertEqual([{"offer_id":"FESTIVE10","name":"Festive discount","stackable":False}],payload["offers"])
        body=json.dumps({
            "idempotency_key":"manual-offer","business_date":"2026-10-27",
            "payment_method":"upi","channel":"walk_in","staff_id":"S1",
            "offer_ids":["FESTIVE10"],
            "cart":{"store_code":"HSR","location_id":"1","lines":[{"variant_id":"1","quantity":1}]},
        }).encode()
        status,payload=self.request_json("POST","/api/checkouts",body,role="cashier")
        self.assertEqual(201,status)
        self.assertEqual(("FESTIVE10",),self.checkout.requests[-1].selected_offer_ids)

    def test_offer_list_uses_server_business_date_not_client_input(self):
        status,payload=self.request_json("GET","/api/offers?business_date=bad")
        self.assertEqual(200,status)
        self.assertEqual(["FESTIVE10"], [offer["offer_id"] for offer in payload["offers"]])

    def test_pricing_preview_returns_authoritative_offer_evaluation(self):
        evaluation = OfferEvaluation(
            eligible=(
                OfferDecision("AUTO", "eligible", "Offer conditions are satisfied."),
                OfferDecision("MANUAL", "eligible", "Offer conditions are satisfied."),
            ),
            applied=(OfferDecision("AUTO", "applied", "Selected by offer policy.", 1250),),
            rejected=(
                OfferDecision("MANUAL", "rejected", "Not selected by the conflict policy."),
                OfferDecision("BLOCKED", "rejected", "Customer does not have required tag priority."),
            ),
            line_discounts_paise=(),
        )

        class Pricing:
            def price(self, cart, business_date, tags, selected_offer_ids):
                self.selected_offer_ids = selected_offer_ids
                return SimpleNamespace(
                    gross_paise=10000, discount_paise=1250, taxable_paise=7797,
                    cgst_paise=477, sgst_paise=476, tax_paise=953,
                    payable_paise=9703, lines=[], offers=evaluation,
                )

            def offer_catalog(self):
                return [
                    SimpleNamespace(offer_id="AUTO", name="Automatic discount", stackable=True),
                    SimpleNamespace(offer_id="MANUAL", name="Manual offer", stackable=False),
                    SimpleNamespace(offer_id="BLOCKED", name="Customer-only offer", stackable=False),
                ]

        pricing = Pricing()
        self.application.pricing_service = pricing
        status, payload = self.request_json("POST", "/api/pricing-preview", json.dumps({
            "business_date": "2026-10-29",
            "cart": {"store_code": "HSR", "location_id": "1", "lines": []},
        }).encode())
        self.assertEqual(200, status)
        self.assertIsNone(pricing.selected_offer_ids)
        self.assertEqual([
            {"offer_id": "AUTO", "name": "Automatic discount", "stackable": True, "status": "applied", "reason": "Selected by offer policy.", "discount_paise": 1250},
            {"offer_id": "BLOCKED", "name": "Customer-only offer", "stackable": False, "status": "rejected", "reason": "Customer does not have required tag priority.", "discount_paise": 0},
            {"offer_id": "MANUAL", "name": "Manual offer", "stackable": False, "status": "eligible", "reason": "Not selected by the conflict policy.", "discount_paise": 0},
        ], payload["offer_evaluation"])

    def test_pricing_preview_rejects_malformed_offer_ids(self):
        self.application.pricing_service = object()
        status, payload = self.request_json("POST", "/api/pricing-preview", json.dumps({
            "business_date": "2026-10-29",
            "offer_ids": "FESTIVE10",
            "cart": {"store_code": "HSR", "location_id": "1", "lines": []},
        }).encode())
        self.assertEqual(400, status)
        self.assertEqual("pricing_invalid", payload["error"])

    def test_open_till_listing_is_live_and_store_filtered(self):
        class TillStore:
            def open_tills(self,store_code):
                return [{"session_id":"till_live","store_code":store_code,"location_id":"70000001","status":"OPEN","expected_paise":500}]
        self.application.till_store=TillStore()
        status,payload=self.request_json("GET","/api/tills?store_code=HSR",role="cashier")
        self.assertEqual(200,status)
        self.assertEqual("till_live",payload["tills"][0]["session_id"])

    def test_system_status_runs_only_read_probes_and_reports_terminal_reachable(self):
        status, payload = self.request_json("GET", "/api/system-status")
        self.assertEqual(200, status)
        components = {item["name"]: item for item in payload["components"]}
        self.assertEqual("HEALTHY", components["Shopify adapter"]["status"])
        self.assertEqual("HEALTHY", components["Store-tool adapter"]["status"])
        self.assertEqual("REACHABLE", components["Payment terminal"]["status"])
        self.assertIn("payment readiness was not tested", components["Payment terminal"]["explanation"])
        self.assertEqual(["GET locations.json"], self.shopify_probe.calls)
        self.assertEqual(["GET /smt/staff"], self.smt_probe.calls)
        self.assertEqual([( "GET", "eluno-pos-read-only-status-probe-never-charge")], self.terminal_probe.calls)
        self.assertEqual("UNKNOWN", {item["name"]: item for item in payload["components"]}["Real payment terminal"]["status"])

    def test_system_status_keeps_independent_results_on_failures(self):
        app = PosApplication(
            CustomerPrescriptionService(Customers(), Prescriptions()), Catalog(),
            shopify_adapter=ProbeShopify(HttpRequestError(None, "connection refused")),
            smt_adapter=ProbeSmt(),
            terminal_adapter=ProbeTerminal(HttpRequestError(None, "connection refused")),
        )
        components = {item["name"]: item for item in system_status(app)["components"]}
        self.assertEqual("UNAVAILABLE", components["Shopify adapter"]["status"])
        self.assertEqual("HEALTHY", components["Store-tool adapter"]["status"])
        self.assertEqual("UNAVAILABLE", components["Payment terminal"]["status"])

    def test_system_status_marks_unexpected_terminal_success_unknown(self):
        app = PosApplication(
            CustomerPrescriptionService(Customers(), Prescriptions()), Catalog(),
            shopify_adapter=ProbeShopify(), smt_adapter=ProbeSmt(), terminal_adapter=ProbeTerminal(result={"id": "unexpected"}),
        )
        components = {item["name"]: item for item in system_status(app)["components"]}
        self.assertEqual("UNKNOWN", components["Payment terminal"]["status"])

    def test_opt_in_mock_payment_test_requires_local_mock_and_verifies_charge(self):
        status, payload = self.request_json("POST", "/api/system-status/mock-payment-test", b"{}")
        self.assertEqual(403, status)
        self.assertEqual("mock_payment_test_forbidden", payload["error"])
        self.assertEqual([], self.terminal_probe.calls)

        terminal = MockPaymentTerminal()
        self.application.local_mock_configured = True
        self.application.terminal_adapter = terminal
        status, before = self.request_json("GET", "/api/system-status")
        self.assertEqual("LOCAL_MOCK", before["environment"])
        components = {item["name"]: item for item in before["components"]}
        self.assertEqual("UNKNOWN", components["Local mock payment flow"]["status"])
        self.assertEqual("NOT_CONFIGURED", components["Real payment terminal"]["status"])

        status, result = self.request_json("POST", "/api/system-status/mock-payment-test", b"{}")
        self.assertEqual(200, status)
        self.assertEqual("HEALTHY", result["status"])
        charge_call = next(call for call in terminal.calls if call[0] == "POST")
        self.assertEqual((100, "upi"), charge_call[1:3])
        self.assertTrue(charge_call[3].startswith("eluno-readiness-"))
        self.assertEqual(("GET", "mock_charge_1"), terminal.calls[2])
        self.assertEqual([], self.checkout.requests)
        self.assertEqual(["GET locations.json"], self.shopify_probe.calls)
        self.assertEqual(["GET /smt/staff"], self.smt_probe.calls)

        status, repeated = self.request_json("POST", "/api/system-status/mock-payment-test", b"{}")
        self.assertEqual(200, status)
        self.assertEqual("HEALTHY", repeated["status"])
        self.assertEqual(1, len([call for call in terminal.calls if call[0] == "POST"]))

        status, after = self.request_json("GET", "/api/system-status")
        self.assertEqual("HEALTHY", {item["name"]: item for item in after["components"]}["Local mock payment flow"]["status"])
        self.assertEqual(["GET locations.json", "GET locations.json"], self.shopify_probe.calls)
        self.assertEqual(["GET /smt/staff", "GET /smt/staff"], self.smt_probe.calls)

    def test_uncertain_local_mock_payment_test_is_not_retried_or_reported_ready(self):
        class LostResponseTerminal:
            def __init__(self): self.charge_calls = 0
            def get_charge(self, charge_id):
                if charge_id == "eluno-pos-read-only-status-probe-never-charge":
                    raise HttpRequestError(404, "unknown charge")
                raise HttpRequestError(None, "connection lost")
            def charge(self, amount_paise, method, order_ref):
                self.charge_calls += 1
                raise HttpRequestError(None, "connection lost after possible write")
        terminal=LostResponseTerminal()
        self.application.local_mock_configured=True
        self.application.terminal_adapter=terminal
        status,result=self.request_json("POST","/api/system-status/mock-payment-test",b"{}")
        self.assertEqual(200,status)
        self.assertEqual("UNKNOWN",result["status"])
        self.assertEqual(1,terminal.charge_calls)
        status,report=self.request_json("GET","/api/system-status")
        components={item["name"]:item for item in report["components"]}
        self.assertEqual("UNKNOWN",components["Local mock payment flow"]["status"])

    def test_lens_validation_endpoint_success_and_invalid_request(self):
        body = json.dumps({"lens": {"lens_type": "single_vision", "index": "1.50", "coating": "standard"}, "prescription": PRESCRIPTION}).encode("utf-8")
        status, payload = self.request_json("POST", "/api/lens-validation", body, role="cashier")
        self.assertEqual(200, status)
        self.assertTrue(payload["valid"])
        status, payload = self.request_json("POST", "/api/lens-validation", b"not json", role="cashier")
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_lens_validation_endpoint_returns_staff_errors(self):
        invalid_rx = dict(PRESCRIPTION)
        invalid_rx["od"] = {"sph": "-5.00", "cyl": "-3.00", "axis": 87, "add": None}
        body = json.dumps({
            "lens": {"lens_type": "single_vision", "index": "1.50", "coating": "standard"},
            "prescription": invalid_rx,
        }).encode("utf-8")
        status, payload = self.request_json("POST", "/api/lens-validation", body, role="cashier")
        self.assertEqual(422, status)
        self.assertFalse(payload["valid"])
        self.assertTrue(any("sphere" in error for error in payload["errors"]))
        self.assertTrue(any("cylinder" in error for error in payload["errors"]))

    def test_lens_validation_endpoint_warns_on_off_grid_and_rejects_fractional_axis(self):
        invalid_rx = dict(PRESCRIPTION)
        invalid_rx["od"] = dict(PRESCRIPTION["od"], axis=91)
        body = json.dumps({
            "lens": {"lens_type": "single_vision", "index": "1.50", "coating": "standard"},
            "prescription": invalid_rx,
        }).encode("utf-8")
        status, payload = self.request_json("POST", "/api/lens-validation", body, role="cashier")
        self.assertEqual(200, status)
        self.assertTrue(payload["valid"])
        self.assertTrue(any("OD axis 91" in warning for warning in payload["warnings"]))

        invalid_rx["od"] = dict(PRESCRIPTION["od"], axis=90.5)
        body = json.dumps({
            "lens": {"lens_type": "single_vision", "index": "1.50", "coating": "standard"},
            "prescription": invalid_rx,
        }).encode("utf-8")
        status, payload = self.request_json("POST", "/api/lens-validation", body, role="cashier")
        self.assertEqual(400, status)
        self.assertIn("Right eye axis must be an integer", payload["message"])

    def test_checkout_endpoint_accepts_intent_not_client_totals(self):
        body = json.dumps({
            "idempotency_key": "checkout-1", "business_date": "2026-10-26", "payment_method": "upi",
            "channel": "walk_in", "staff_id": "S1", "customer_id": "1", "customer_tags": ["priority"],
            "total": "0.01",  # ignored: pricing is always recomputed by CheckoutService
            "cart": {"store_code": "HSR", "location_id": "1", "lines": [
                {"variant_id": "1", "quantity": 1, "pair_ref": "p1"},
                {"variant_id": "2", "quantity": 1, "pair_ref": "p1", "prescription_id": "RX1"},
            ]},
        }).encode("utf-8")
        status, payload = self.request_json("POST", "/api/checkouts", body, role="cashier")
        self.assertEqual(201, status)
        self.assertEqual("sale_1", payload["sale_id"])
        self.assertEqual("checkout-1", self.checkout.requests[0].idempotency_key)
        self.assertEqual(frozenset(), self.checkout.requests[0].customer_tags)
        self.assertEqual("S-CASHIER", self.checkout.requests[0].staff_id)
        status, payload = self.request_json("POST", "/api/checkouts", b"{}", role="cashier")
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_checkout_status_and_recovery_are_operator_accessible_but_conservative(self):
        status, payload = self.request_json("GET", "/api/checkouts/sale_unknown", role="cashier")
        self.assertEqual(200, status)
        self.assertEqual("PAYMENT_UNKNOWN", payload["status"])
        status, payload = self.request_json("POST", "/api/checkouts/sale_unknown/recover", b"{}", role="cashier")
        self.assertEqual(202, status)
        self.assertEqual("PAYMENT_UNKNOWN", payload["status"])
        status, payload = self.request_json("GET", "/api/checkouts/nope", role="cashier")
        self.assertEqual(404, status)
        status, payload = self.request_json("POST", "/api/checkouts/sale_done/recover", b"{}", role="cashier")
        self.assertEqual(422, status)
        self.assertEqual("recovery_not_allowed", payload["error"])

    def test_anomaly_report_validates_query_and_returns_empty_flags(self):
        status, payload = self.request_json("GET", "/api/reports/anomalies?store_id=1&date=2026-10-26")
        self.assertEqual(200, status); self.assertEqual([], payload["flags"])
        status, payload = self.request_json("GET", "/api/reports/anomalies?store_id=1&date=bad")
        self.assertEqual(422, status); self.assertEqual("report_invalid", payload["error"])

    def test_day_end_report_returns_controlled_error_when_shopify_is_unavailable(self):
        class UnavailableReport:
            def report(self, store_id, day):
                raise HttpRequestError(503, "Shopify unavailable")

        self.application.report_service = UnavailableReport()
        status, payload = self.request_json("GET", "/api/reports/day-end?store_id=1&date=2026-10-26")
        self.assertEqual(502, status)
        self.assertEqual("report_upstream_unavailable", payload["error"])

    def test_overall_operations_report_is_admin_only_and_uses_requested_date(self):
        class OverallReport:
            def overall_report(self, day):
                return {"store_code": "Overall operations", "business_date": day.isoformat()}

        self.application.report_service = OverallReport()
        status, denied = self.request_json(
            "GET", "/api/reports/overall?date=2026-10-26", role="cashier",
        )
        self.assertEqual(403, status)
        self.assertEqual("admin_role_required", denied["error"])
        status, report = self.request_json(
            "GET", "/api/reports/overall?date=2026-10-26", role="admin",
        )
        self.assertEqual(200, status)
        self.assertEqual("Overall operations", report["store_code"])
        self.assertEqual("2026-10-26", report["business_date"])

    def test_anomaly_report_returns_controlled_error_when_shopify_is_unavailable(self):
        class UnavailableReport:
            def report(self, store_id, day):
                raise HttpRequestError(503, "Shopify unavailable")

        self.application.anomaly_service = UnavailableReport()
        status, payload = self.request_json("GET", "/api/reports/anomalies?store_id=1&date=2026-10-26")
        self.assertEqual(502, status)
        self.assertEqual("report_upstream_unavailable", payload["error"])

    def test_customer_registration_creates_separate_hashed_account_and_login(self):
        body = json.dumps({
            "name": "New Customer",
            "phone": "+919000000002",
            "email": "new@example.com",
            "password": "New-Customer-Password-2026!",
        }).encode()
        status, payload = self.request_json("POST", "/api/customer-auth/register", body, role=None)
        self.assertEqual(201, status)
        self.assertEqual("+919000000002", payload["customer"]["phone"])
        self.assertNotIn("password_hash", payload["customer"])
        connection = sqlite3.connect(Path(self.refund_tempdir.name) / "auth.sqlite3")
        try:
            row = connection.execute(
                "SELECT password_hash FROM customer_accounts WHERE phone = ?",
                ("+919000000002",),
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row)
        self.assertTrue(row[0].startswith("scrypt$"))
        self.assertNotEqual("New-Customer-Password-2026!", row[0])

        login_request = Request(
            self.base + "/api/customer-auth/login",
            data=json.dumps({
                "phone": "+919000000002",
                "password": "New-Customer-Password-2026!",
            }).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(login_request) as response:
            self.assertEqual(200, response.status)
            self.assertIn("eluno_customer_session=", response.headers["Set-Cookie"])
            self.assertIn("HttpOnly", response.headers["Set-Cookie"])
            token = response.headers["Set-Cookie"].split("=", 1)[1].split(";", 1)[0]
            self.assertEqual("+919000000002", json.loads(response.read())["customer"]["phone"])
        self.assertIsNotNone(self.application.customer_accounts.authenticate(token))
        status, denied = self.request_json("POST", "/api/customer-auth/login", json.dumps({
            "phone": "+919000000002",
            "password": "Wrong-Customer-Password-2026!",
        }).encode(), role=None)
        self.assertEqual(401, status)
        self.assertEqual("invalid_customer_credentials", denied["error"])
        logout_request = Request(
            self.base + "/api/customer-auth/logout", data=b"{}", method="POST",
            headers={
                "Content-Type": "application/json",
                "Cookie": "eluno_customer_session={}".format(token),
            },
        )
        with urlopen(logout_request) as response:
            self.assertEqual(204, response.status)
        self.assertIsNone(self.application.customer_accounts.authenticate(token))

    def test_customer_registration_does_not_link_existing_shopify_customers_without_verification(self):
        body = json.dumps({
            "name": "Test Customer",
            "phone": CUSTOMER["phone"],
            "password": "New-Customer-Password-2026!",
        }).encode()
        status, payload = self.request_json("POST", "/api/customer-auth/register", body, role=None)
        self.assertEqual(409, status)
        self.assertEqual("customer_registration_unavailable", payload["error"])

    def test_customer_sessions_are_separate_and_profile_is_owned(self):
        first = self.application.customer_accounts.create_account(
            "42", "+919000000002", "First Customer", "First-Customer-Password-2026!", "first@example.com",
        )
        second = self.application.customer_accounts.create_account(
            "43", "+919000000003", "Second Customer", "Second-Customer-Password-2026!",
        )
        first_token = self.application.customer_accounts.login(
            first.phone, "First-Customer-Password-2026!", "127.0.0.1",
        )[0]
        second_token = self.application.customer_accounts.login(
            second.phone, "Second-Customer-Password-2026!", "127.0.0.2",
        )[0]
        status, denied = self.request_customer_json("GET", "/api/admin/staff", token=first_token)
        self.assertEqual(401, status)
        self.assertEqual("authentication_required", denied["error"])
        status, denied = self.request_json("GET", "/api/customer/profile", role="cashier")
        self.assertEqual(401, status)
        self.assertEqual("customer_authentication_required", denied["error"])
        status, profile = self.request_customer_json("GET", "/api/customer/profile", token=second_token)
        self.assertEqual(200, status)
        self.assertEqual(second.customer_id, profile["customer"]["customer_id"])
        self.assertNotEqual(first.customer_id, profile["customer"]["customer_id"])

        update = json.dumps({
            "name": "Second Updated",
            "phone": second.phone,
            "email": "second@example.com",
            "address": {
                "line1": "12 Retail Road", "line2": "", "city": "Bengaluru",
                "state": "Karnataka", "postal_code": "560001",
            },
        }).encode()
        status, updated = self.request_customer_json(
            "PATCH", "/api/customer/profile", update, second_token,
        )
        self.assertEqual(200, status)
        self.assertEqual("Bengaluru", updated["customer"]["address"]["city"])
        status, _ = self.request_customer_json(
            "POST",
            "/api/customer/password",
            json.dumps({
                "current_password": "Second-Customer-Password-2026!",
                "new_password": "Second-Customer-Password-2027!",
            }).encode(),
            second_token,
        )
        self.assertEqual(204, status)
        self.assertIsNone(self.application.customer_accounts.authenticate(second_token))
        replacement = self.application.customer_accounts.login(
            second.phone, "Second-Customer-Password-2027!", "127.0.0.3",
        )
        self.assertIsNotNone(replacement)

    def test_customer_order_is_priced_stock_checked_idempotent_and_private(self):
        customer_record = self.application.customer_accounts.create_account(
            "42", "+919000000002", "Online Customer", "Online-Customer-Password-2026!",
        )
        token = self.application.customer_accounts.login(
            customer_record.phone, "Online-Customer-Password-2026!", "127.0.0.1",
        )[0]
        self.application.pricing_service = CartService(
            self.shopify_probe,
            OfferEngine(OfferBook([])),
            TaxRateTable.from_file(Path(__file__).resolve().parents[1] / "starter-kit" / "data" / "tax_rates.json"),
        )
        order_payload = {
            "idempotency_key": "online-order-1",
            "cart": {
                "location_id": "1",
                "lines": [{"variant_id": "1", "quantity": 1}],
            },
            "delivery_address": {
                "line1": "12 Retail Road", "line2": "", "city": "Bengaluru",
                "state": "Karnataka", "postal_code": "560001",
            },
            "payment_method_preference": "card",
        }
        body = json.dumps(order_payload).encode()
        status, first = self.request_customer_json(
            "POST", "/api/customer/orders", body, token,
        )
        self.assertEqual(201, status, first)
        order = first["order"]
        self.assertEqual("paid", order["payment_status"])
        self.assertEqual("ELU/HSR/FY26/00001", order["invoice_no"])
        self.assertEqual("card", order["payment_method_preference"])
        self.assertEqual("not_submitted", order["fulfillment_status"])
        self.assertFalse(order["tracking_available"])
        self.assertEqual("North Frame Black", order["items"][0]["title"])
        self.assertGreater(order["tax_paise"], 0)
        self.assertEqual(order["gross_paise"] - order["discount_paise"], order["total_paise"])

        self.shopify_probe.inventory_available = 0
        status, duplicate = self.request_customer_json(
            "POST", "/api/customer/orders", body, token,
        )
        self.assertEqual(200, status)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(order["order_id"], duplicate["order"]["order_id"])
        invalid_payment = dict(order_payload, idempotency_key="online-order-invalid-payment", payment_method_preference="cash")
        status, invalid_method = self.request_customer_json(
            "POST", "/api/customer/orders", json.dumps(invalid_payment).encode(), token,
        )
        self.assertEqual(400, status)
        self.assertEqual("customer_order_invalid", invalid_method["error"])
        changed_payload = dict(order_payload, cart={
            "location_id": "1",
            "lines": [{"variant_id": "1", "quantity": 2}],
        })
        status, conflict = self.request_customer_json(
            "POST", "/api/customer/orders", json.dumps(changed_payload).encode(), token,
        )
        self.assertEqual(400, status)
        self.assertEqual("customer_order_invalid", conflict["error"])
        out_of_stock = dict(order_payload, idempotency_key="online-order-out-of-stock", cart={
            "location_id": "1",
            "lines": [{"variant_id": "1", "quantity": 1}],
        })
        status, stock_error = self.request_customer_json(
            "POST", "/api/customer/orders", json.dumps(out_of_stock).encode(), token,
        )
        self.assertEqual(400, status)
        self.assertIn("Insufficient stock", stock_error["message"])

        other = self.application.customer_accounts.create_account(
            "43", "+919000000003", "Other Customer", "Other-Customer-Password-2026!",
        )
        other_token = self.application.customer_accounts.login(
            other.phone, "Other-Customer-Password-2026!", "127.0.0.2",
        )[0]
        status, history = self.request_customer_json(
            "GET", "/api/customer/orders", token=other_token,
        )
        self.assertEqual(200, status)
        self.assertEqual([], history["orders"])
        self.assertEqual(order["order_id"], self.request_customer_json(
            "GET", "/api/customer/orders", token=token,
        )[1]["orders"][0]["order_id"])
        stored_order = {
            key: value for key, value in order.items()
            if key not in ("order_id", "status")
        }
        for index in range(20):
            self.application.customer_orders.create(
                customer_record.customer_id,
                "page-order-{}".format(index),
                stored_order,
                {"page_order": index},
            )
        status, first_page = self.request_customer_json(
            "GET", "/api/customer/orders?page=1", token=token,
        )
        self.assertEqual(200, status)
        self.assertEqual(20, len(first_page["orders"]))
        self.assertTrue(first_page["has_more"])
        status, second_page = self.request_customer_json(
            "GET", "/api/customer/orders?page=2", token=token,
        )
        self.assertEqual(200, status)
        self.assertEqual(1, len(second_page["orders"]))
        self.assertFalse(second_page["has_more"])

    def test_customer_order_does_not_require_stock_for_make_to_order_lens(self):
        customer_record = self.application.customer_accounts.create_account(
            "42", "+919000000002", "Online Customer", "Online-Customer-Password-2026!",
        )
        token = self.application.customer_accounts.login(
            customer_record.phone, "Online-Customer-Password-2026!", "127.0.0.1",
        )[0]
        self.shopify_probe.list_catalog_variants = lambda: [CatalogVariant(
            "2", "10", "lens", "90015000", 4000, True,
            product_title="Single Vision Lens", variant_title="1.60",
        )]
        self.shopify_probe.get_variant = lambda variant_id: {
            "id": variant_id, "inventory_item_id": 92000002,
        }
        self.shopify_probe.order_line_items = [{
            "id": 11, "variant_id": 2, "quantity": 1,
            "properties": [{"name": "prescription_assistance_requested", "value": "true"}],
        }]
        inventory_requests = []
        self.shopify_probe.list_inventory_levels = lambda item_ids, location_id: (
            inventory_requests.append((item_ids, location_id)) or []
        )
        self.application.pricing_service = CartService(
            self.shopify_probe,
            OfferEngine(OfferBook([])),
            TaxRateTable.from_file(Path(__file__).resolve().parents[1] / "starter-kit" / "data" / "tax_rates.json"),
        )
        payload = {
            "idempotency_key": "online-order-make-to-order-lens",
            "cart": {
                "location_id": "1",
                "lines": [{
                    "variant_id": "2", "quantity": 1,
                    "needs_prescription_assistance": True,
                }],
            },
            "delivery_address": {
                "line1": "12 Retail Road", "line2": "", "city": "Bengaluru",
                "state": "Karnataka", "postal_code": "560001",
            },
            "payment_method_preference": "upi",
        }

        status, response = self.request_customer_json(
            "POST", "/api/customer/orders", json.dumps(payload).encode(), token,
        )

        self.assertEqual(201, status, response)
        self.assertEqual("paid", response["order"]["payment_status"])
        self.assertTrue(self.checkout.requests[-1].allow_prescription_assistance)
        self.assertEqual([], inventory_requests)

    def test_store_order_queue_is_cashier_only_store_scoped_and_decisions_are_audited(self):
        customer = self.application.customer_accounts.create_account(
            "7404", "+919000000004", "Store Queue Customer", "Queue-Customer-Password-2026!",
        )
        order, _ = self.application.customer_orders.create(
            customer.customer_id,
            "store-queue-order",
            {
                "created_at": "2026-10-30",
                "store_code": "HSR",
                "location_id": "1",
                "customer_name": customer.display_name,
                "customer_phone": customer.phone,
                "delivery_address": {
                    "line1": "Private address", "line2": "", "city": "Bengaluru",
                    "state": "Karnataka", "postal_code": "560001",
                },
                "items": [{
                    "title": "Prescription Lens",
                    "quantity": 1,
                    "prescription_assistance_requested": True,
                }],
                "total_paise": 25000,
                "payment_method_preference": "card",
                "payment_status": "paid",
                "fulfillment_status": "not_submitted",
                "status_message": "Order request saved.",
                "invoice_no": "ELU/HSR/FY26/00001",
            },
            {"idempotency_key": "store-queue-order"},
        )
        self.application.customer_orders.create(
            customer.customer_id,
            "other-store-order",
            {
                "created_at": "2026-10-30",
                "store_code": "KMG",
                "customer_name": customer.display_name,
                "customer_phone": customer.phone,
                "items": [],
                "total_paise": 0,
                "payment_status": "awaiting_payment",
            },
            {"idempotency_key": "other-store-order"},
        )

        status, unauthorized = self.request_json(
            "GET", "/api/store/orders/pending", role=None,
        )
        self.assertEqual(401, status)
        self.assertEqual("authentication_required", unauthorized["error"])
        for role in ("admin", "optometrist"):
            status, denied = self.request_json(
                "GET", "/api/store/orders/pending", role=role,
            )
            self.assertEqual(403, status)
            self.assertEqual("cashier_role_required", denied["error"])

        status, queue = self.request_json("GET", "/api/store/orders/pending", role="cashier")
        self.assertEqual(200, status)
        self.assertEqual([order["order_id"]], [item["order_id"] for item in queue["orders"]])
        self.assertEqual("Store Queue Customer", queue["orders"][0]["customer_name"])
        self.assertEqual("card", queue["orders"][0]["payment_method_preference"])
        self.assertNotIn("customer_phone", queue["orders"][0])
        self.assertNotIn("delivery_address", queue["orders"][0])

        decision_path = "/api/store/orders/{}/confirm".format(order["order_id"])
        status, result = self.request_json("POST", decision_path, b"{}", role="cashier")
        self.assertEqual(200, status, result)
        self.assertEqual("confirmed", result["order"]["store_confirmation_status"])
        self.assertEqual("S-CASHIER", result["order"]["store_decision_by"])
        self.assertNotIn("delivery_address", result["order"])
        self.assertEqual("sold", result["order"]["status"])
        self.assertEqual("paid", result["order"]["payment_status"])
        status, duplicate = self.request_json("POST", decision_path, b"{}", role="cashier")
        self.assertEqual(409, status)
        self.assertEqual("store_order_decision_conflict", duplicate["error"])

    def test_store_order_denial_requires_verified_partial_lens_refund(self):
        customer = self.application.customer_accounts.create_account(
            "7405", "+919000000005", "Denied Order Customer", "Queue-Customer-Password-2026!",
        )
        order, _ = self.application.customer_orders.create(
            customer.customer_id,
            "store-denial-order",
            {
                "created_at": "2026-10-30",
                "store_code": "HSR",
                "location_id": "1",
                "customer_name": customer.display_name,
                "items": [{
                    "title": "Prescription Lens",
                    "quantity": 1,
                    "prescription_assistance_requested": True,
                    "shopify_line_item_id": "11",
                }],
                "total_paise": 15000,
                "payment_status": "paid",
                "shopify_order_id": "99",
                "invoice_no": "ELU/HSR/FY26/00001",
                "fulfillment_status": "not_submitted",
            },
            {"idempotency_key": "store-denial-order"},
        )
        path = "/api/store/orders/{}/deny".format(order["order_id"])
        status, invalid = self.request_json("POST", path, b"{}", role="cashier")
        self.assertEqual(409, status)
        self.assertIn("reason is required", invalid["message"])
        status, result = self.request_json(
            "POST", path, json.dumps({"reason": "Prescription cannot be completed"}).encode(),
            role="cashier",
        )
        self.assertEqual(200, status, result)
        self.assertEqual("partially_refunded", result["order"]["status"])
        self.assertEqual("paid", result["order"]["payment_status"])
        self.assertEqual(5000, result["order"]["refund_amount_paise"])
        self.assertTrue(result["order"]["refund_id"])
        self.assertEqual(1, len(self.refund_terminal.calls))
        refund_calls = [call for call in self.refund_shopify.calls if call[0] == "CREATE_REFUND"]
        self.assertEqual(1, len(refund_calls))

    def test_optometrist_assistance_queue_is_store_scoped_and_role_protected(self):
        customer = self.application.customer_accounts.create_account(
            "7406", "+919000000006", "Optical Assistance Customer", "Queue-Customer-Password-2026!",
        )
        assisted_order, _ = self.application.customer_orders.create(
            customer.customer_id,
            "optometrist-assistance-order",
            {
                "created_at": "2026-10-30",
                "store_code": "HSR",
                "customer_name": customer.display_name,
                "customer_phone": customer.phone,
                "delivery_address": {"line1": "Private address"},
                "items": [{
                    "title": "Prescription Lens",
                    "quantity": 1,
                    "prescription_assistance_requested": True,
                }],
                "total_paise": 25000,
                "payment_status": "paid",
                "fulfillment_status": "not_submitted",
            },
            {"idempotency_key": "optometrist-assistance-order"},
        )
        self.application.customer_orders.decide_for_store(
            assisted_order["order_id"], "HSR", "S-CASHIER", "confirm"
        )
        status, unauthorized = self.request_json(
            "GET", "/api/optometrist/orders/assistance", role=None,
        )
        self.assertEqual(401, status)
        for role in ("admin", "cashier"):
            status, denied = self.request_json(
                "GET", "/api/optometrist/orders/assistance", role=role,
            )
            self.assertEqual(403, status)
            self.assertEqual("optometrist_role_required", denied["error"])

        status, queue = self.request_json(
            "GET", "/api/optometrist/orders/assistance", role="optometrist",
        )
        self.assertEqual(200, status)
        self.assertEqual([assisted_order["order_id"]], [item["order_id"] for item in queue["orders"]])
        self.assertEqual("+919000000006", queue["orders"][0]["customer_phone"])
        self.assertEqual(
            [{"title": "Prescription Lens", "quantity": 1}],
            queue["orders"][0]["items"],
        )
        self.assertNotIn("delivery_address", queue["orders"][0])

if __name__ == "__main__":
    unittest.main()
