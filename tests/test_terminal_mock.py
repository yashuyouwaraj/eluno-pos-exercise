import copy
import importlib.util
from http.server import HTTPServer
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from eluno_pos.adapters.http import HttpRequestError, JsonHttpClient
from eluno_pos.adapters.shopify import ShopifyAdminClient
from eluno_pos.adapters.terminal import PaymentTerminalClient


ROOT = Path(__file__).resolve().parents[1]
MOCK_PATH = ROOT / "starter-kit" / "mock-apis" / "mock_server.py"
SPEC = importlib.util.spec_from_file_location("eluno_terminal_mock", MOCK_PATH)
MOCK = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MOCK)


class TerminalMockRefundTests(unittest.TestCase):
    def setUp(self):
        self.original_state = MOCK.S
        self.original_flake = MOCK.FLAKE
        self.original_database = MOCK.STATE_DB
        MOCK.S = copy.deepcopy(MOCK.S)
        MOCK.FLAKE = False
        MOCK.STATE_DB = None
        self.server = HTTPServer(("127.0.0.1", 0), MOCK.H)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.terminal = PaymentTerminalClient(
            JsonHttpClient(
                "http://127.0.0.1:{}/terminal".format(self.server.server_address[1]),
                max_attempts=2,
                sleeper=lambda _: None,
            )
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        MOCK.S = self.original_state
        MOCK.FLAKE = self.original_flake
        MOCK.STATE_DB = self.original_database

    def test_terminal_refund_is_idempotent_and_rejects_key_reuse(self):
        charge = self.terminal.charge(25000, "upi", "refund-test-sale")
        first = self.terminal.refund(charge["charge_id"], 10000, "customer request", "refund-test-key")
        replay = self.terminal.refund(charge["charge_id"], 10000, "customer request", "refund-test-key")
        self.assertEqual(first, replay)
        self.assertEqual("processed", first["status"])
        self.assertEqual(10000, MOCK.S["charges"][charge["charge_id"]]["refunded_paise"])
        with self.assertRaises(HttpRequestError) as raised:
            self.terminal.refund(charge["charge_id"], 5000, "customer request", "refund-test-key")
        self.assertEqual(409, raised.exception.status)
        self.assertEqual(10000, MOCK.S["charges"][charge["charge_id"]]["refunded_paise"])

    def test_injected_429_503_are_retried_with_retry_after_and_no_duplicate_charge(self):
        sleeps = []
        MOCK.FLAKE = True
        shopify = ShopifyAdminClient(JsonHttpClient(
            "http://127.0.0.1:{}/admin/api/2026-10".format(self.server.server_address[1]),
            max_attempts=2,
            sleeper=sleeps.append,
        ))
        with patch.object(MOCK.random, "random", side_effect=[0.0, 1.0]):
            created = shopify.create_product({
                "title": "Retry-safe test frame",
                "product_type": "Frame",
                "variants": [{
                    "sku": "RETRY-TEST-1",
                    "price": "1.00",
                    "eluno": {"line_type": "frame", "tax_hsn": "90031900"},
                }],
            })
        self.assertEqual("Retry-safe test frame", created["title"])
        self.assertEqual([1.0], sleeps)
        created_skus = [
            variant["sku"]
            for product in MOCK.S["products"]
            for variant in product.get("variants", [])
            if variant.get("sku") == "RETRY-TEST-1"
        ]
        self.assertEqual(["RETRY-TEST-1"], created_skus)

        sleeps.clear()
        terminal = PaymentTerminalClient(
            JsonHttpClient(
                "http://127.0.0.1:{}/terminal".format(self.server.server_address[1]),
                max_attempts=2,
                sleeper=sleeps.append,
            )
        )
        with patch.object(MOCK.random, "random", side_effect=[0.0, 1.0]):
            charge = terminal.charge(25000, "upi", "injected-503-sale")
        self.assertEqual("captured", charge["status"])
        self.assertEqual([0.25], sleeps)
        actual_charges = [item for key, item in MOCK.S["charges"].items() if not key.startswith("_flaked_")]
        self.assertEqual(1, len(actual_charges))
