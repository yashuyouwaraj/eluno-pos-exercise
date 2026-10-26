from decimal import Decimal
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from eluno_pos.http_app import make_handler, system_status
from eluno_pos.adapters.http import HttpRequestError
from eluno_pos.checkout import CheckoutError, CheckoutResult
from eluno_pos.models import LensLimits, LensOption
from eluno_pos.services import CustomerPrescriptionService, PosApplication


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
        if invoice_no == "ELU/HSR/FY26/00001":
            return CheckoutResult("sale_1", "COMPLETED", "99", invoice_no, "Invoice status retrieved.")
        return None

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
    def __init__(self, error=None): self.calls = []; self.error = error; self.registered_customers = []
    def list_locations(self):
        self.calls.append("GET locations.json")
        if self.error: raise self.error
        return [{"id": 1}]
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


class ProbeSmt:
    def __init__(self, error=None): self.calls = []; self.error = error
    def list_staff(self):
        self.calls.append("GET /smt/staff")
        if self.error: raise self.error
        return [{"id": "S1"}]


class ProbeTerminal:
    def __init__(self, error=None, result=None): self.calls = []; self.error = error; self.result = result
    def get_charge(self, charge_id):
        self.calls.append(("GET", charge_id))
        if self.error: raise self.error
        return self.result


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
        self.smt_probe = ProbeSmt()
        self.terminal_probe = ProbeTerminal(HttpRequestError(404, "unknown charge"))
        self.application = PosApplication(CustomerPrescriptionService(Customers(), Prescriptions()), Catalog(), self.checkout, anomaly_service=Anomalies(), shopify_adapter=self.shopify_probe, smt_adapter=self.smt_probe, terminal_adapter=self.terminal_probe)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.application))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = "http://127.0.0.1:{}".format(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request_json(self, method, path, body=None):
        request = Request(self.base + path, data=body, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            payload = json.loads(error.read())
            error.close()
            return error.code, payload

    def test_customer_prescription_endpoint_success_and_not_found(self):
        status, payload = self.request_json("GET", "/api/customer-prescription?phone=%2B919000000001&business_date=2026-10-26")
        self.assertEqual(200, status)
        self.assertEqual("ready", payload["status"])
        self.assertEqual("RX1", payload["prescription"]["rx_id"])
        self.assertEqual(["campaign:festive_frames", "priority"],payload["customer"]["tags"])
        status, payload = self.request_json("GET", "/api/customer-prescription?phone=%2B919000009999&business_date=2026-10-26")
        self.assertEqual(404, status)
        self.assertEqual("customer_not_found", payload["status"])

    def test_customer_without_valid_prescription_is_returned_as_a_successful_lookup(self):
        class NoPrescriptions:
            def prescriptions_by_phone(self, phone):
                return []

        self.application.lookup_service=CustomerPrescriptionService(Customers(),NoPrescriptions())
        status,payload=self.request_json(
            "GET","/api/customer-prescription?phone=%2B919000000001&business_date=2026-10-26",
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
            "GET","/api/customer-prescription?phone=%2B919000000001&business_date=2026-10-26",
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

    def test_prescription_entry_is_disabled_outside_configured_local_mock(self):
        status, payload = self.request_json("POST", "/api/prescriptions", b"{}")
        self.assertEqual(503, status)
        self.assertEqual("prescription_entry_unavailable", payload["error"])
        self.assertEqual([], self.shopify_probe.calls)

    def test_root_describes_real_service_routes_and_health_is_unchanged(self):
        status, payload = self.request_json("GET", "/")
        self.assertEqual(200, status)
        self.assertEqual("eluno-pos", payload["service"])
        self.assertEqual("/health", payload["health"])
        self.assertIn("/api/checkouts", payload["endpoint_groups"]["checkout"])
        self.assertIn("/api/system-status", payload["endpoint_groups"]["system_status"])
        self.assertIn("python scripts/start_local.py", payload["cashier"])
        status, health = self.request_json("GET", "/health")
        self.assertEqual(200, status); self.assertEqual({"ok": True, "service": "eluno-pos"}, health)
        status, missing = self.request_json("GET", "/not-a-route")
        self.assertEqual(404, status); self.assertEqual("not_found", missing["error"])

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
        status,payload=self.request_json("POST","/api/checkouts",body)
        self.assertEqual(201,status)
        self.assertEqual(("FESTIVE10",),self.checkout.requests[-1].selected_offer_ids)

    def test_offer_list_requires_valid_business_date(self):
        status,payload=self.request_json("GET","/api/offers?business_date=bad")
        self.assertEqual(400,status)
        self.assertEqual("invalid_request",payload["error"])

    def test_open_till_listing_is_live_and_store_filtered(self):
        class TillStore:
            def open_tills(self,store_code):
                return [{"session_id":"till_live","store_code":store_code,"location_id":"70000001","status":"OPEN","expected_paise":500}]
        self.application.till_store=TillStore()
        status,payload=self.request_json("GET","/api/tills?store_code=MM")
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
        status, payload = self.request_json("POST", "/api/lens-validation", body)
        self.assertEqual(200, status)
        self.assertTrue(payload["valid"])
        status, payload = self.request_json("POST", "/api/lens-validation", b"not json")
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_lens_validation_endpoint_returns_staff_errors(self):
        invalid_rx = dict(PRESCRIPTION)
        invalid_rx["od"] = {"sph": "-5.00", "cyl": "-3.00", "axis": 87, "add": None}
        body = json.dumps({
            "lens": {"lens_type": "single_vision", "index": "1.50", "coating": "standard"},
            "prescription": invalid_rx,
        }).encode("utf-8")
        status, payload = self.request_json("POST", "/api/lens-validation", body)
        self.assertEqual(422, status)
        self.assertFalse(payload["valid"])
        self.assertTrue(any("sphere" in error for error in payload["errors"]))
        self.assertTrue(any("cylinder" in error for error in payload["errors"]))

    def test_lens_validation_endpoint_rejects_off_grid_and_fractional_axis(self):
        invalid_rx = dict(PRESCRIPTION)
        invalid_rx["od"] = dict(PRESCRIPTION["od"], axis=91)
        body = json.dumps({
            "lens": {"lens_type": "single_vision", "index": "1.50", "coating": "standard"},
            "prescription": invalid_rx,
        }).encode("utf-8")
        status, payload = self.request_json("POST", "/api/lens-validation", body)
        self.assertEqual(422, status)
        self.assertTrue(any("Right eye axis 91" in error for error in payload["errors"]))

        invalid_rx["od"] = dict(PRESCRIPTION["od"], axis=90.5)
        body = json.dumps({
            "lens": {"lens_type": "single_vision", "index": "1.50", "coating": "standard"},
            "prescription": invalid_rx,
        }).encode("utf-8")
        status, payload = self.request_json("POST", "/api/lens-validation", body)
        self.assertEqual(400, status)
        self.assertIn("Right eye axis must be an integer", payload["message"])

    def test_checkout_endpoint_accepts_intent_not_client_totals(self):
        body = json.dumps({
            "idempotency_key": "checkout-1", "business_date": "2026-10-26", "payment_method": "upi",
            "channel": "walk_in", "staff_id": "S1", "customer_id": "1", "customer_tags": ["priority"],
            "total": "0.01",  # ignored: pricing is always recomputed by CheckoutService
            "cart": {"store_code": "HSR", "location_id": "7", "lines": [
                {"variant_id": "1", "quantity": 1, "pair_ref": "p1"},
                {"variant_id": "2", "quantity": 1, "pair_ref": "p1", "prescription_id": "RX1"},
            ]},
        }).encode("utf-8")
        status, payload = self.request_json("POST", "/api/checkouts", body)
        self.assertEqual(201, status)
        self.assertEqual("sale_1", payload["sale_id"])
        self.assertEqual("checkout-1", self.checkout.requests[0].idempotency_key)
        self.assertEqual(frozenset(), self.checkout.requests[0].customer_tags)
        status, payload = self.request_json("POST", "/api/checkouts", b"{}")
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_checkout_status_and_recovery_are_operator_accessible_but_conservative(self):
        status, payload = self.request_json("GET", "/api/checkouts/sale_unknown")
        self.assertEqual(200, status)
        self.assertEqual("PAYMENT_UNKNOWN", payload["status"])
        status, payload = self.request_json("POST", "/api/checkouts/sale_unknown/recover", b"{}")
        self.assertEqual(202, status)
        self.assertEqual("PAYMENT_UNKNOWN", payload["status"])
        status, payload = self.request_json("GET", "/api/checkouts/nope")
        self.assertEqual(404, status)
        status, payload = self.request_json("POST", "/api/checkouts/sale_done/recover", b"{}")
        self.assertEqual(422, status)
        self.assertEqual("recovery_not_allowed", payload["error"])

    def test_anomaly_report_validates_query_and_returns_empty_flags(self):
        status, payload = self.request_json("GET", "/api/reports/anomalies?store_id=7&date=2026-10-26")
        self.assertEqual(200, status); self.assertEqual([], payload["flags"])
        status, payload = self.request_json("GET", "/api/reports/anomalies?store_id=7&date=bad")
        self.assertEqual(422, status); self.assertEqual("report_invalid", payload["error"])

    def test_day_end_report_returns_controlled_error_when_shopify_is_unavailable(self):
        class UnavailableReport:
            def report(self, store_id, day):
                raise HttpRequestError(503, "Shopify unavailable")

        self.application.report_service = UnavailableReport()
        status, payload = self.request_json("GET", "/api/reports/day-end?store_id=7&date=2026-10-26")
        self.assertEqual(502, status)
        self.assertEqual("report_upstream_unavailable", payload["error"])

    def test_anomaly_report_returns_controlled_error_when_shopify_is_unavailable(self):
        class UnavailableReport:
            def report(self, store_id, day):
                raise HttpRequestError(503, "Shopify unavailable")

        self.application.anomaly_service = UnavailableReport()
        status, payload = self.request_json("GET", "/api/reports/anomalies?store_id=7&date=2026-10-26")
        self.assertEqual(502, status)
        self.assertEqual("report_upstream_unavailable", payload["error"])


if __name__ == "__main__":
    unittest.main()
