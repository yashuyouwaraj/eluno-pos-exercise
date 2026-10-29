from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from urllib.parse import parse_qs, urlparse

from eluno_pos.adapters.http import HttpRequestError, JsonHttpClient
from eluno_pos.adapters.shopify import ShopifyAdminClient
from eluno_pos.adapters.smt import SmtClient


class RetryHandler(BaseHTTPRequestHandler):
    calls = 0
    query = None
    planned_statuses = []

    def do_GET(self):
        type(self).calls += 1
        type(self).query = parse_qs(urlparse(self.path).query)
        status = type(self).planned_statuses.pop(0)
        body = json.dumps({"customers": []}).encode("utf-8")
        self.send_response(status)
        if status == 429:
            self.send_header("Retry-After", "0")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        return


class AdapterTests(unittest.TestCase):
    def setUp(self):
        RetryHandler.calls = 0
        RetryHandler.query = None
        RetryHandler.planned_statuses = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), RetryHandler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base_url = "http://127.0.0.1:{}".format(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_leading_plus_phone_is_url_encoded_and_retryable_responses_are_bounded(self):
        RetryHandler.planned_statuses = [429, 503, 200]
        sleeps = []
        client = ShopifyAdminClient(
            http=JsonHttpClient(self.base_url, max_attempts=3, sleeper=sleeps.append)
        )
        self.assertEqual([], client.search_customers_by_phone("+919000000005"))
        self.assertEqual(3, RetryHandler.calls)
        self.assertEqual(["phone:+919000000005"], RetryHandler.query["query"])
        self.assertEqual([0.0, 0.5], sleeps)

    def test_retry_stops_after_configured_attempts(self):
        RetryHandler.planned_statuses = [503, 503, 503]
        client = JsonHttpClient(self.base_url, max_attempts=2, sleeper=lambda _: None)
        with self.assertRaises(HttpRequestError) as raised:
            client.get("unavailable")
        self.assertEqual(503, raised.exception.status)
        self.assertEqual(2, RetryHandler.calls)

    def test_list_all_orders_advances_since_id_until_history_is_complete(self):
        class Http:
            def __init__(self):
                self.queries = []

            def get(self, path, query):
                self.queries.append((path, query))
                if query["since_id"] == "0":
                    return {"orders": [{"id": index} for index in range(1, 251)]}
                if query["since_id"] == "250":
                    return {"orders": [{"id": 251}]}
                return {"orders": []}

        http = Http()
        orders = ShopifyAdminClient(http=http).list_all_orders()

        self.assertEqual(251, len(orders))
        self.assertEqual(["0", "250"], [query["since_id"] for _, query in http.queries])
        self.assertTrue(all(query["status"] == "any" and query["limit"] == "250" for _, query in http.queries))

    def test_upstream_errors_do_not_expose_query_values_or_response_bodies(self):
        from urllib.error import HTTPError
        from io import BytesIO

        error = HTTPError(
            "http://example.test/customers/search?phone=%2B919876543210",
            500,
            "failure",
            {},
            BytesIO(b'{"error":"customer +919876543210 was not found"}'),
        )
        message = JsonHttpClient._error_message(
            error, "http://example.test/customers/search?phone=%2B919876543210"
        )
        self.assertIn("HTTP 500", message)
        self.assertNotIn("919876543210", message)
        self.assertNotIn("phone=", message)
        self.assertNotIn("was not found", message)
        post_message = JsonHttpClient._error_message(error, "http://example.test/products.json", "POST")
        self.assertIn("POST http://example.test/products.json failed with HTTP 500", post_message)
        error.close()

    def test_smt_staff_probe_is_read_only_and_validates_its_envelope(self):
        class Http:
            def __init__(self, response): self.response = response; self.calls = []
            def get(self, path, query=None):
                self.calls.append((path, query)); return self.response

        http = Http({"staff": [{"id": "S1"}]})
        self.assertEqual([{"id": "S1"}], SmtClient(http=http).list_staff())
        self.assertEqual([("staff", None)], http.calls)
        with self.assertRaisesRegex(ValueError, "staff envelope"):
            SmtClient(http=Http({"staff": "not-a-list"})).list_staff("HSR")

    def test_smt_prescription_creation_does_not_retry_and_checks_response(self):
        class Http:
            def __init__(self, response):
                self.response=response;self.calls=[]
            def post(self,path,payload,retry_statuses):
                self.calls.append((path,payload,retry_statuses))
                return self.response

        rx={"rx_id":"RX1"}
        http=Http({"prescription":rx})
        self.assertEqual(rx,SmtClient(http=http).create_prescription(
            {"phone":"+919000000001"},"rx-idem-1",
        ))
        self.assertEqual(("prescriptions",{"prescription":{"phone":"+919000000001"},"idempotency_key":"rx-idem-1"},()),http.calls[0])
        with self.assertRaisesRegex(ValueError,"prescription envelope"):
            SmtClient(http=Http({"prescription":"invalid"})).create_prescription({}, "rx-idem-2")

    def test_inventory_lookup_uses_supported_shopify_route_and_requested_location(self):
        class Http:
            def __init__(self, response): self.response = response; self.calls = []
            def get(self, path, query=None):
                self.calls.append((path, query)); return self.response

        payload = {"inventory_levels": [
            {"inventory_item_id": 10, "location_id": 7, "available": 2}
        ]}
        http = Http(payload)
        levels = ShopifyAdminClient(http=http).list_inventory_levels([10, 11], 7)
        self.assertEqual(payload["inventory_levels"], levels)
        self.assertEqual([("inventory_levels.json", {
            "inventory_item_ids": "10,11", "location_ids": "7",
        })], http.calls)
        with self.assertRaisesRegex(ValueError, "inventory_levels envelope"):
            ShopifyAdminClient(http=Http({"inventory_levels": "invalid"})).list_inventory_levels([10], 7)

    def test_catalog_and_inventory_writes_use_safe_retry_statuses_and_validate_envelopes(self):
        class Http:
            def __init__(self): self.calls=[]
            def post(self,path,payload,retry_statuses):
                self.calls.append((path,payload,retry_statuses))
                if path=="products.json":return {"product":{"id":20}}
                return {"inventory_level":{"available":5},"duplicate":True}

        http=Http()
        client=ShopifyAdminClient(http=http)
        product={"title":"Frame","variants":[{"sku":"F1"}]}
        self.assertEqual({"id":20},client.create_product(product))
        self.assertEqual({"available":5},client.connect_inventory_level(30,7))
        self.assertEqual(({"available":5},True),client.adjust_inventory(30,7,2,"GRN-001:30:7"))
        self.assertEqual([
            ("products.json",{"product":product},(429,)),
            ("inventory_levels/connect.json",{"inventory_item_id":30,"location_id":7},(429,)),
            ("inventory_levels/adjust.json",{
                "inventory_item_id":30,"location_id":7,"available_adjustment":2,
                "idempotency_key":"GRN-001:30:7",
            },(429,)),
        ],http.calls)


if __name__ == "__main__":
    unittest.main()
