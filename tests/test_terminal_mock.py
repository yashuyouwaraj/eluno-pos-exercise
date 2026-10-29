import copy
import importlib.util
from http.server import HTTPServer
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen

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

        seed_order = next(
            order for order in MOCK.S["orders"].values()
            if order.get("line_items") and any(
                transaction.get("kind") == "sale" and transaction.get("status") == "success"
                for transaction in order.get("transactions", [])
            )
        )
        sale_transaction = next(
            transaction for transaction in seed_order["transactions"]
            if transaction.get("kind") == "sale" and transaction.get("status") == "success"
        )
        refund_note = "RETRY-CREDIT-NOTE-1"
        refund_payload = {
            "refund_line_items": [{
                "line_item_id": seed_order["line_items"][0]["id"],
                "quantity": 1,
                "restock_type": "no_restock",
            }],
            "transactions": [{
                "kind": "refund",
                "amount": "1.00",
                "gateway": sale_transaction["gateway"],
                "parent_id": sale_transaction["id"],
            }],
            "note": "Failure injection retry test",
            "note_attributes": [{"name": "credit_note_no", "value": refund_note}],
        }
        sleeps.clear()
        with patch.object(MOCK.random, "random", side_effect=[0.0, 1.0]):
            created_refund = shopify.create_refund(seed_order["id"], refund_payload)
        self.assertEqual([1.0], sleeps)
        self.assertEqual(seed_order["id"], created_refund["order_id"])
        matching_refunds = [
            refund for refund in seed_order["refunds"]
            if any(
                attribute.get("value") == refund_note
                for attribute in refund.get("note_attributes", [])
            )
        ]
        self.assertEqual(1, len(matching_refunds))

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

    def test_mock_cli_without_no_flake_runs_injection_on_an_isolated_port(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, "-u", str(MOCK_PATH), "--port", str(port)],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            health_url = "http://127.0.0.1:{}/health".format(port)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail("mock process exited before becoming healthy")
                try:
                    with urlopen(health_url, timeout=0.25) as response:
                        self.assertEqual(200, response.status)
                        response.read()
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                self.fail("mock process did not become healthy")

            shopify = ShopifyAdminClient(JsonHttpClient(
                "http://127.0.0.1:{}/admin/api/2026-10".format(port),
                max_attempts=1,
                sleeper=lambda _: None,
            ))
            injected_status = None
            for attempt in range(20):
                try:
                    shopify.create_product({
                        "title": "CLI flake probe {}".format(attempt),
                        "product_type": "Frame",
                        "variants": [{
                            "sku": "CLI-FLAKE-{}".format(attempt),
                            "price": "1.00",
                            "eluno": {"line_type": "frame", "tax_hsn": "90031900"},
                        }],
                    })
                except HttpRequestError as exc:
                    injected_status = exc.status
                    break
            self.assertEqual(429, injected_status)
        finally:
            process.terminate()
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate(timeout=5)
        self.assertIn("flaky 429/503: on", output)
