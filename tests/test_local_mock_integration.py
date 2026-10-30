"""Reproducible, isolated checks against the supplied no-flake local mock."""
import socket
import subprocess
import sys
import json
import os
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import time
import unittest
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import (
    HTTPCookieProcessor, Request, build_opener, install_opener, urlopen,
)
from uuid import uuid4

from eluno_pos.auth import AuthManager
from eluno_pos.business_time import bengaluru_today
from eluno_pos.adapters.http import HttpRequestError, JsonHttpClient
from eluno_pos.adapters.shopify import ShopifyAdminClient
from eluno_pos.adapters.smt import SmtClient
from eluno_pos.cli import ApiClient, CashierCLI, default_preview
from eluno_pos.http_app import default_application, make_handler
from eluno_pos.adapters.terminal import PaymentTerminalClient

ROOT = Path(__file__).resolve().parents[1]
AUTH_USERS = json.dumps({
    "admin": {"password": "Test-Integration-Admin-Password-2026!", "role": "admin", "staff_id": "S119"},
    "cashier": {"password": "Test-Integration-Cashier-Password-2026!", "role": "cashier", "staff_id": "S105"},
})
OPTOMETRIST_PASSWORD = "Test-Integration-Optometrist-Password-2026!"
install_opener(build_opener(HTTPCookieProcessor()))


def login_pos(base, username="admin"):
    passwords = {
        "admin": "Test-Integration-Admin-Password-2026!",
        "cashier": "Test-Integration-Cashier-Password-2026!",
        "optometrist": OPTOMETRIST_PASSWORD,
    }
    request = Request(
        base + "/api/auth/login",
        data=json.dumps({
            "username": username,
            "password": passwords[username],
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request) as response:
        if response.status != 200:
            raise RuntimeError("test POS authentication failed")
        response.read()


def start_authenticated_pos(application, username="admin"):
    with patch.dict(os.environ, {"ELUNO_AUTH_USERS": AUTH_USERS}):
        auth_path = Path(application.checkout_service._sales._path).with_name("auth.sqlite3")
        application.auth_manager = AuthManager.from_env(auth_path)
    if username == "optometrist":
        application.auth_manager.create_staff(
            "optometrist", OPTOMETRIST_PASSWORD, "optometrist", "Riya R.",
            "integration-admin", "MM", "S103",
        )
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(application))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    base = "http://127.0.0.1:{}".format(server.server_port)
    login_pos(base, username)
    return server, thread, base

class LocalMockIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.process = None
        cls.process_output = []
        cls.output_reader = None
        try:
            sock=socket.socket(); sock.bind(("127.0.0.1",0)); cls.port=sock.getsockname()[1]; sock.close()
            cls.process=subprocess.Popen([sys.executable,"-u",str(ROOT/'starter-kit/mock-apis/mock_server.py'),"--no-flake","--port",str(cls.port)],cwd=str(ROOT),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            cls.output_reader=threading.Thread(
                target=cls._drain_process_output,args=(cls.process,),daemon=True,
            )
            cls.output_reader.start()
            client=JsonHttpClient("http://127.0.0.1:{}".format(cls.port),timeout_seconds=.2,sleeper=lambda _:None)
            deadline=time.monotonic()+5
            last_health_error=None
            while time.monotonic()<deadline:
                try: client.get('health'); return
                except HttpRequestError as exc:
                    last_health_error=exc
                    if cls.process.poll() is not None:
                        break
                    time.sleep(.05)
            raise RuntimeError('local mock did not become healthy; last health check: {}'.format(last_health_error))
        except BaseException as exc:
            output=cls._stop_process_and_capture_output()
            if isinstance(exc, RuntimeError):
                raise RuntimeError('{}; mock startup output:\n{}'.format(exc,output)) from exc
            raise
    @staticmethod
    def _drain_process_output(process):
        if process.stdout is not None:
            for line in process.stdout:
                LocalMockIntegrationTests.process_output.append(line)
    @classmethod
    def tearDownClass(cls):
        cls._stop_process()
        process=getattr(cls, 'process', None)
        if process is not None and process.stdout is not None and process.poll() is not None:
            process.communicate()
    @classmethod
    def _stop_process(cls):
        process = getattr(cls, 'process', None)
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    @classmethod
    def _capture_process_output(cls):
        process=getattr(cls,'process',None)
        if process is None or process.stdout is None:
            return '(no mock process output captured)'
        if process.poll() is None:
            return '(mock process was still running; startup output was not captured)'
        reader=getattr(cls,'output_reader',None)
        if reader is not None:
            reader.join(timeout=1)
        output=''.join(getattr(cls,'process_output',[]))
        return output.strip() or '(mock process exited without output)'
    @classmethod
    def _stop_process_and_capture_output(cls):
        cls._stop_process()
        return cls._capture_process_output()
    def test_terminal_charge_and_lookup_use_real_mock(self):
        terminal=PaymentTerminalClient(base_url='http://127.0.0.1:{}/terminal'.format(self.port))
        charge=terminal.charge(100,'upi','integration-order-ref')
        self.assertEqual('captured',charge['status'])
        self.assertEqual(charge['charge_id'],terminal.get_charge(charge['charge_id'])['charge_id'])
    def test_shopify_order_history_supports_since_id_cursor_pages(self):
        shopify = ShopifyAdminClient(base_url="http://127.0.0.1:{}/admin/api/2026-10".format(self.port))
        first = shopify.list_orders(status="any", limit="1", since_id="0", order="created_at asc")
        self.assertEqual(1, len(first))
        second = shopify.list_orders(
            status="any", limit="1", since_id=str(first[0]["id"]), order="created_at asc",
        )
        self.assertEqual(1, len(second))
        self.assertGreater(int(second[0]["id"]), int(first[0]["id"]))
    def test_prescription_entry_requires_store_optometrist_and_round_trips_to_store_tool(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mock_url="http://127.0.0.1:{}".format(self.port)
            environment={
                "ELUNO_SHOPIFY_BASE_URL":mock_url+"/admin/api/2026-10",
                "ELUNO_SMT_BASE_URL":mock_url+"/smt",
                "ELUNO_TERMINAL_BASE_URL":mock_url+"/terminal",
                "ELUNO_STATE_DB":str(Path(temp_dir)/"prescription-entry.sqlite3"),
                "ELUNO_LOCAL_MOCK_MODE":"1",
                "ELUNO_LOCAL_MOCK_PORT":str(self.port),
            }
            with patch.dict(os.environ,environment):
                application=default_application()
            server,thread,base=start_authenticated_pos(application, "optometrist")
            application.auth_manager.update_staff("S105", {"store_code": "MM"}, "integration-admin")
            shopify=ShopifyAdminClient(base_url=mock_url+"/admin/api/2026-10")
            customer=shopify.search_customers_by_phone("+919000000001")[0]
            payload={
                "phone":"+919000000001","store_code":"MM","date":"2026-10-27",
                "valid_until":"2027-10-27","optometrist_id":"S103",
                "optometrist_confirmed":True,"idempotency_key":"rx-local-integration-1",
                "od":{"sph":"-1.00","cyl":"-0.50","axis":90,"add":None},
                "os":{"sph":"-1.00","cyl":"-0.50","axis":90,"add":None},
            }
            try:
                login_pos(base, "cashier")
                request=Request(base+"/api/prescription-approvers?store_code=MM",method="GET")
                with urlopen(request) as response:
                    self.assertEqual(200,response.status)
                    approvers=json.loads(response.read())["approvers"]
                self.assertIn("S103",[item["staff_id"] for item in approvers])
                request=Request(base+"/api/prescription-approvers?store_code=JN",method="GET")
                with self.assertRaises(HTTPError) as raised:urlopen(request)
                self.assertEqual(403,raised.exception.code);raised.exception.close()

                unauthorized=dict(payload,optometrist_id="S101",idempotency_key="rx-invalid-verifier")
                request=Request(base+"/api/prescriptions",data=json.dumps(unauthorized).encode(),headers={"Content-Type":"application/json"},method="POST")
                with self.assertRaises(HTTPError) as raised:urlopen(request)
                self.assertEqual(422,raised.exception.code);raised.exception.close()

                cashier_payload=dict(payload,idempotency_key="rx-local-integration-cashier")
                request=Request(base+"/api/prescriptions",data=json.dumps(cashier_payload).encode(),headers={"Content-Type":"application/json"},method="POST")
                with urlopen(request) as response:
                    self.assertEqual(201,response.status)
                    cashier_created=json.loads(response.read())["prescription"]
                self.assertEqual("S103",cashier_created["optometrist_id"])
                login_pos(base, "optometrist")

                unconfirmed=dict(payload,optometrist_confirmed="true")
                request=Request(base+"/api/prescriptions",data=json.dumps(unconfirmed).encode(),headers={"Content-Type":"application/json"},method="POST")
                with self.assertRaises(HTTPError) as raised:urlopen(request)
                self.assertEqual(400,raised.exception.code);raised.exception.close()

                off_grid=dict(payload, idempotency_key="rx-local-integration-off-grid")
                off_grid["od"] = dict(payload["od"], axis=87)
                request=Request(base+"/api/prescriptions",data=json.dumps(off_grid).encode(),headers={"Content-Type":"application/json"},method="POST")
                with urlopen(request) as response:
                    self.assertEqual(201,response.status)
                    off_grid_result=json.loads(response.read())
                self.assertTrue(any("OD axis 87" in warning and "optometrist review" in warning for warning in off_grid_result["warnings"]))

                invalid=dict(payload,od=dict(payload["od"],axis=181))
                request=Request(base+"/api/prescriptions",data=json.dumps(invalid).encode(),headers={"Content-Type":"application/json"},method="POST")
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request)
                self.assertEqual(400,raised.exception.code)
                raised.exception.close()

                request=Request(base+"/api/prescriptions",data=json.dumps(payload).encode(),headers={"Content-Type":"application/json"},method="POST")
                with urlopen(request) as response:
                    self.assertEqual(201,response.status)
                    created=json.loads(response.read())["prescription"]
                self.assertEqual(str(customer["id"]),str(created["customer_id"]))
                self.assertEqual("S103",created["optometrist_id"])

                repeat=dict(payload)
                request=Request(base+"/api/prescriptions",data=json.dumps(repeat).encode(),headers={"Content-Type":"application/json"},method="POST")
                with urlopen(request) as response:
                    self.assertEqual(201,response.status)
                    duplicate=json.loads(response.read())["prescription"]
                self.assertEqual(created["rx_id"],duplicate["rx_id"])
                stored=SmtClient(base_url=mock_url+"/smt").prescriptions_by_phone("+919000000001")
                self.assertEqual(1,len([item for item in stored if item["rx_id"]==created["rx_id"]]))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_admin_catalog_creation_and_stock_receipt_are_visible_to_cashier_catalog(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mock_url="http://127.0.0.1:{}".format(self.port)
            environment={
                "ELUNO_SHOPIFY_BASE_URL":mock_url+"/admin/api/2026-10",
                "ELUNO_SMT_BASE_URL":mock_url+"/smt",
                "ELUNO_TERMINAL_BASE_URL":mock_url+"/terminal",
                "ELUNO_STATE_DB":str(Path(temp_dir)/"admin-catalog.sqlite3"),
                "ELUNO_LOCAL_MOCK_MODE":"1",
                "ELUNO_LOCAL_MOCK_PORT":str(self.port),
            }
            with patch.dict(os.environ,environment):
                application=default_application()
            server,thread,base=start_authenticated_pos(application)
            sku="ADMIN-FRAME-"+uuid4().hex[:10]
            try:
                frame_request={
                    "line_type":"frame","title":"Admin Test Frame","sku":sku,
                    "variant_title":"Matte Black","price_paise":499900,
                    "actor":"S119",
                    "category":"optical","material":"TR90","size":"52-18-140",
                    "rx_able":True,
                }
                status,created=self._request_json(base,"POST","/api/admin/catalog/products",frame_request)
                self.assertEqual(201,status)
                frame_variant=created["product"]["variants"][0]
                self.assertEqual("shopify",frame_variant["inventory_management"])
                self.assertEqual("90031900",frame_variant["eluno"]["hsn"])
                audit=application.till_store.audit_events(str(created["product"]["id"]))
                self.assertEqual("CATALOG_PRODUCT_CREATED",audit[-1]["action"])
                self.assertEqual("S119",audit[-1]["actor"])
                status,duplicate=self._request_json(base,"POST","/api/admin/catalog/products",frame_request)
                self.assertEqual(409,status)
                self.assertEqual("duplicate_sku",duplicate["error"])
                status,invalid_location=self._request_json(base,"POST","/api/admin/inventory/receipts",{
                    "inventory_item_id":frame_variant["inventory_item_id"],
                    "location_id":99999999,"quantity":4,"actor":"S119","reference":"GRN-INVALID-LOC",
                })
                self.assertEqual(400,status)
                self.assertEqual("invalid_stock_receipt",invalid_location["error"])

                status,locations=self._request_json(base,"GET","/api/admin/locations")
                self.assertEqual(200,status)
                jayanagar=next(location for location in locations["locations"] if location["store_code"]=="JN")
                receipt_request={
                    "inventory_item_id":frame_variant["inventory_item_id"],
                    "location_id":jayanagar["id"],"quantity":4,
                    "actor":"S119","reference":"GRN-ELU-0001",
                }
                status,receipt=self._request_json(base,"POST","/api/admin/inventory/receipts",receipt_request)
                self.assertEqual(200,status)
                self.assertEqual(4,receipt["inventory_level"]["available"])
                status,repeat_receipt=self._request_json(
                    base,"POST","/api/admin/inventory/receipts",receipt_request,
                )
                self.assertEqual(200,status)
                self.assertEqual(4,repeat_receipt["inventory_level"]["available"])
                self.assertTrue(repeat_receipt["idempotent_replay"])
                changed_receipt=dict(receipt_request,quantity=5)
                status,conflicting_receipt=self._request_json(
                    base,"POST","/api/admin/inventory/receipts",changed_receipt,
                )
                self.assertEqual(409,status)
                self.assertEqual("stock_receipt_failed",conflicting_receipt["error"])
                receipt_audit=application.till_store.audit_events(
                    "{}@{}".format(frame_variant["inventory_item_id"],jayanagar["id"]),
                )
                self.assertEqual("FRAME_STOCK_RECEIVED",receipt_audit[-1]["action"])
                self.assertEqual("GRN-ELU-0001",receipt_audit[-1]["reason"])
                self.assertEqual(1,len([
                    event for event in receipt_audit
                    if event["reason"]=="GRN-ELU-0001"
                ]))

                status,catalog_admin=self._request_json(
                    base,"GET","/api/admin/catalog/products?q=Admin%20Test%20Frame",
                )
                self.assertEqual(200,status)
                editable=next(item for item in catalog_admin["products"] if item["variant_id"]==str(frame_variant["id"]))
                self.assertEqual(499900,editable["price_paise"])
                status,invalid_edit=self._request_json(
                    base,"PATCH","/api/admin/catalog/products/{}".format(created["product"]["id"]),
                    {"variant_id":str(frame_variant["id"]),"price_paise":4999.5},
                )
                self.assertEqual(400,status)
                self.assertEqual("invalid_product_update",invalid_edit["error"])
                status,updated=self._request_json(
                    base,"PATCH","/api/admin/catalog/products/{}".format(created["product"]["id"]),
                    {
                        "variant_id":str(frame_variant["id"]),"title":"Updated Admin Test Frame",
                        "description":"A tested optical catalog description.","price_paise":529900,
                        "material":"Acetate","size":"54-18-140","category":"sunglasses",
                        "rx_able":False,"status":"active",
                    },
                )
                self.assertEqual(200,status)
                self.assertEqual("Updated Admin Test Frame",updated["product"]["title"])
                self.assertEqual("Acetate",updated["product"]["variants"][0]["eluno"]["material"])
                levels=ShopifyAdminClient(base_url=mock_url+"/admin/api/2026-10").list_inventory_levels(
                    [frame_variant["inventory_item_id"]],jayanagar["id"],
                )
                self.assertEqual(4,levels[0]["available"],"catalog metadata edits must not alter stock")
                status,admin_frames=self._request_json(
                    base,"GET","/api/admin/catalog/frames?location_id={}".format(jayanagar["id"]),
                )
                self.assertEqual(200,status)
                reflected=next(
                    frame for frame in admin_frames["variants"]
                    if frame["variant_id"]==frame_variant["id"]
                )
                self.assertEqual(4,reflected["available"])
                self.assertEqual(529900,reflected["price_paise"])
                self.assertEqual("sunglasses",reflected["category"])
                self.assertEqual("Acetate",reflected["material"])
                self.assertEqual("54-18-140",reflected["size"])
                login_pos(base, "admin")
                status,assigned=self._request_json(
                    base,"PATCH","/api/admin/staff/S105",{"store_code":"JN"},
                )
                self.assertEqual(200,status,assigned)
                login_pos(base, "cashier")
                status,sale=self._request_json(base,"POST","/api/checkouts",{
                    "idempotency_key":"admin-stock-sale-"+uuid4().hex,
                    "business_date":bengaluru_today().isoformat(),
                    "payment_method":"upi","channel":"walk_in","staff_id":"S119",
                    "cart":{
                        "store_code":"JN","location_id":str(jayanagar["id"]),
                        "lines":[{"variant_id":str(frame_variant["id"]),"quantity":1}],
                    },
                })
                self.assertEqual(201,status)
                self.assertEqual("COMPLETED",sale["status"])
                stock=ShopifyAdminClient(base_url=mock_url+"/admin/api/2026-10").list_inventory_levels(
                    [frame_variant["inventory_item_id"]],jayanagar["id"],
                )
                self.assertEqual(3,stock[0]["available"])
                login_pos(base, "admin")
                status,archived=self._request_json(
                    base,"PATCH","/api/admin/catalog/products/{}".format(created["product"]["id"]),
                    {"variant_id":str(frame_variant["id"]),"status":"archived"},
                )
                self.assertEqual(200,status)
                self.assertEqual("archived",archived["product"]["status"])
                status,catalog=self._request_json(base,"GET","/api/catalog/variants?line_type=frame")
                self.assertNotIn(str(frame_variant["id"]),[variant["variant_id"] for variant in catalog["variants"]])
                stock=ShopifyAdminClient(base_url=mock_url+"/admin/api/2026-10").list_inventory_levels(
                    [frame_variant["inventory_item_id"]],jayanagar["id"],
                )
                self.assertEqual(3,stock[0]["available"],"archiving must retain current inventory")

                login_pos(base, "admin")
                lens_request={
                    "line_type":"lens","title":"Admin Test Lens","sku":"ADMIN-LENS-"+uuid4().hex[:10],
                    "variant_title":"Bifocal 1.74 Standard","price_paise":1799900,
                    "actor":"S119",
                    "lens_type":"bifocal","index":"1.74","coating":"standard",
                }
                status,lens_created=self._request_json(base,"POST","/api/admin/catalog/products",lens_request)
                self.assertEqual(201,status)
                lens_variant=lens_created["product"]["variants"][0]
                self.assertIsNone(lens_variant["inventory_item_id"])
                self.assertEqual("90015000",lens_variant["eluno"]["hsn"])
                self.assertTrue(lens_variant["eluno"]["rx_limits"]["add_required"])
                status,duplicate_lens=self._request_json(base,"POST","/api/admin/catalog/products",lens_request)
                self.assertEqual(409,status)
                self.assertEqual("duplicate_sku",duplicate_lens["error"])
                duplicate_configuration=dict(lens_request,sku="OTHER-"+uuid4().hex[:10])
                status,duplicate_lens=self._request_json(
                    base,"POST","/api/admin/catalog/products",duplicate_configuration,
                )
                self.assertEqual(409,status)
                self.assertEqual("duplicate_lens_configuration",duplicate_lens["error"])
                status,invalid_receipt=self._request_json(base,"POST","/api/admin/inventory/receipts",{
                    "inventory_item_id":lens_variant["id"],"location_id":jayanagar["id"],"quantity":2,
                })
                self.assertEqual(400,status)
                self.assertEqual("invalid_stock_receipt",invalid_receipt["error"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_staff_customer_search_matches_full_name_and_email(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mock_url="http://127.0.0.1:{}".format(self.port)
            environment={
                "ELUNO_SHOPIFY_BASE_URL":mock_url+"/admin/api/2026-10",
                "ELUNO_SMT_BASE_URL":mock_url+"/smt",
                "ELUNO_TERMINAL_BASE_URL":mock_url+"/terminal",
                "ELUNO_STATE_DB":str(Path(temp_dir)/"customer-search.sqlite3"),
                "ELUNO_LOCAL_MOCK_MODE":"1",
                "ELUNO_LOCAL_MOCK_PORT":str(self.port),
            }
            with patch.dict(os.environ,environment):
                application=default_application()
            server,thread,base=start_authenticated_pos(application)
            phone="+9197{:08d}".format(uuid4().int % 100000000)
            email="yashu.{}@example.test".format(uuid4().hex[:8])
            try:
                shopify=ShopifyAdminClient(base_url=mock_url+"/admin/api/2026-10")
                created,_=shopify.register_customer("Yashu","Youwaraj",phone)
                shopify.update_customer(str(created["id"]),{"email":email})
                for term in ("Yashu Youwaraj",email,phone):
                    request=Request(base+"/api/customers/search?"+urlencode({"q":term,"page":"1"}))
                    with urlopen(request) as response:
                        self.assertEqual(200,response.status)
                        payload=json.loads(response.read())
                    result=next((item for item in payload["customers"] if item["id"] == str(created["id"])),None)
                    self.assertIsNotNone(result,term)
                    self.assertEqual(0,result["total_spent_paise"])
                    self.assertEqual(0,result["order_count"])
                    self.assertEqual(0,result["total_refunded_paise"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_mock_sqlite_persists_customers_products_and_inventory_after_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_db=str(Path(temp_dir)/"mock-state.sqlite3")
            port_sock=socket.socket();port_sock.bind(("127.0.0.1",0));port=port_sock.getsockname()[1];port_sock.close()
            command=[sys.executable,"-u",str(ROOT/'starter-kit/mock-apis/mock_server.py'),
                     "--no-flake","--port",str(port),"--state-db",state_db]
            process=subprocess.Popen(command,cwd=str(ROOT),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            mock_url="http://127.0.0.1:{}".format(port)
            client=JsonHttpClient(mock_url,timeout_seconds=.3,sleeper=lambda _:None)
            try:
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    try:client.get("health");break
                    except HttpRequestError:time.sleep(.05)
                else:self.fail("persistent mock did not start")
                shopify=ShopifyAdminClient(base_url=mock_url+"/admin/api/2026-10")
                customer,_=shopify.register_customer("Persistent","Customer","+919123456789")
                repeated,created_again=shopify.register_customer("Persistent","Customer","+919123456789")
                self.assertEqual(customer["id"],repeated["id"])
                self.assertFalse(created_again)
                with self.assertRaisesRegex(ValueError,"already belongs"):
                    shopify.register_customer("Different","Customer","+919123456789")
                created=shopify.create_product({
                    "title":"Persistent Frame","vendor":"Eluno","product_type":"Frame",
                    "status":"active","tags":"frame,optical","variants":[{
                        "sku":"PERSIST-"+uuid4().hex[:10],"title":"Black","option1":"Black",
                        "price":"4999.00","inventory_management":"shopify",
                        "inventory_policy":"deny","requires_shipping":False,"taxable":True,
                        "eluno":{"line_type":"frame","hsn":"90031900","category":"optical",
                                 "material":"TR90","size":"52-18-140","rx_able":True},
                    }],
                })
                variant=created["variants"][0]
                shopify.connect_inventory_level(variant["inventory_item_id"],70000005)
                receipt_key="PERSIST-GRN:"+str(variant["inventory_item_id"])+":70000005"
                shopify.adjust_inventory(variant["inventory_item_id"],70000005,6,receipt_key)
            finally:
                process.terminate()
                process.wait(timeout=5)

            restarted=subprocess.Popen(command,cwd=str(ROOT),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    try:client.get("health");break
                    except HttpRequestError:time.sleep(.05)
                else:self.fail("persistent mock did not restart")
                shopify=ShopifyAdminClient(base_url=mock_url+"/admin/api/2026-10")
                self.assertEqual(customer["id"],shopify.search_customers_by_phone("+919123456789")[0]["id"])
                self.assertTrue(any(item["id"]==created["id"] for item in shopify.list_products("Frame")))
                levels=shopify.list_inventory_levels([variant["inventory_item_id"]],70000005)
                self.assertEqual(6,levels[0]["available"])
                repeated,replayed=shopify.adjust_inventory(
                    variant["inventory_item_id"],70000005,6,receipt_key,
                )
                self.assertEqual(6,repeated["available"])
                self.assertTrue(replayed)
            finally:
                restarted.terminate()
                restarted.wait(timeout=5)

    def test_prescription_entry_persists_and_is_available_after_mock_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_db=str(Path(temp_dir)/"prescription-state.sqlite3")
            port_sock=socket.socket();port_sock.bind(("127.0.0.1",0));mock_port=port_sock.getsockname()[1];port_sock.close()
            command=[sys.executable,"-u",str(ROOT/'starter-kit/mock-apis/mock_server.py'),
                     "--no-flake","--port",str(mock_port),"--state-db",state_db]
            mock_url="http://127.0.0.1:{}".format(mock_port)
            client=JsonHttpClient(mock_url,timeout_seconds=.5,sleeper=lambda _:None)
            mock_process=subprocess.Popen(command,cwd=str(ROOT),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            application_server=None
            try:
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    try:client.get("health");break
                    except HttpRequestError:time.sleep(.05)
                else:self.fail("persistent mock did not start")

                environment={
                    "ELUNO_SHOPIFY_BASE_URL":mock_url+"/admin/api/2026-10",
                    "ELUNO_SMT_BASE_URL":mock_url+"/smt",
                    "ELUNO_TERMINAL_BASE_URL":mock_url+"/terminal",
                    "ELUNO_STATE_DB":str(Path(temp_dir)/"pos-state.sqlite3"),
                    "ELUNO_LOCAL_MOCK_MODE":"1",
                    "ELUNO_LOCAL_MOCK_PORT":str(mock_port),
                }
                with patch.dict(os.environ,environment):
                    application=default_application()
                application_server,app_thread,base=start_authenticated_pos(application,username="optometrist")
                payload={
                    "phone":"+919000000001","store_code":"MM","date":"2026-10-27",
                    "valid_until":"2027-10-27","optometrist_id":"S103",
                    "optometrist_confirmed":True,"idempotency_key":"rx-persist-restart-1",
                    "od":{"sph":"-1.00","cyl":"-0.50","axis":90,"add":None},
                    "os":{"sph":"-1.00","cyl":"-0.50","axis":90,"add":None},
                }
                request=Request(base+"/api/prescriptions",data=json.dumps(payload).encode(),
                                headers={"Content-Type":"application/json"},method="POST")
                with urlopen(request) as response:
                    self.assertEqual(201,response.status)
                    created=json.loads(response.read())["prescription"]

                mock_process.terminate()
                mock_process.wait(timeout=5)
                mock_process=subprocess.Popen(command,cwd=str(ROOT),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    try:client.get("health");break
                    except HttpRequestError:time.sleep(.05)
                else:self.fail("persistent mock did not restart")

                request=Request(base+"/api/customer-prescription?phone=%2B919000000001&business_date=2026-10-27")
                with urlopen(request) as response:
                    result=json.loads(response.read())
                self.assertEqual("ready",result["status"])
                self.assertEqual(created["rx_id"],result["prescription"]["rx_id"])
            finally:
                if application_server is not None:
                    application_server.shutdown()
                    application_server.server_close()
                    app_thread.join()
                if mock_process.poll() is None:
                    mock_process.terminate()
                    mock_process.wait(timeout=5)

    def test_concurrent_checkout_cannot_sell_the_same_last_frame_twice(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mock_url="http://127.0.0.1:{}".format(self.port)
            environment={
                "ELUNO_SHOPIFY_BASE_URL":mock_url+"/admin/api/2026-10",
                "ELUNO_SMT_BASE_URL":mock_url+"/smt",
                "ELUNO_TERMINAL_BASE_URL":mock_url+"/terminal",
                "ELUNO_STATE_DB":str(Path(temp_dir)/"concurrent-stock.sqlite3"),
                "ELUNO_LOCAL_MOCK_MODE":"1",
                "ELUNO_LOCAL_MOCK_PORT":str(self.port),
            }
            with patch.dict(os.environ,environment):
                application=default_application()
            server,thread,base=start_authenticated_pos(application)
            before_orders=application.shopify_adapter.list_orders(
                location_id="70000005",status="any",limit="250",
            )
            try:
                product={
                    "line_type":"frame","title":"Concurrency Test Frame",
                    "sku":"CONCURRENT-"+uuid4().hex[:10],"variant_title":"Black",
                    "price_paise":499900,"actor":"S119","category":"optical",
                    "material":"TR90","size":"52-18-140","rx_able":True,
                }
                status,created=self._request_json(base,"POST","/api/admin/catalog/products",product)
                self.assertEqual(201,status)
                variant=created["product"]["variants"][0]
                status,locations=self._request_json(base,"GET","/api/admin/locations")
                self.assertEqual(200,status)
                location=next(item for item in locations["locations"] if item["id"]==70000005)
                status,_=self._request_json(base,"POST","/api/admin/inventory/receipts",{
                    "inventory_item_id":variant["inventory_item_id"],
                    "location_id":location["id"],"quantity":1,"actor":"S119",
                    "reference":"GRN-CONCURRENT-"+uuid4().hex[:8],
                })
                self.assertEqual(200,status)
                status,assigned=self._request_json(
                    base,"PATCH","/api/admin/staff/S105",{"store_code":"JN"},
                )
                self.assertEqual(200,status,assigned)

                class CountingTerminal:
                    def __init__(self,delegate):self.delegate,self.calls=delegate,0
                    def charge(self,*args):
                        self.calls+=1
                        return self.delegate.charge(*args)
                terminal=CountingTerminal(application.checkout_service._terminal)
                application.checkout_service._terminal=terminal
                login_pos(base, "cashier")

                def checkout(key):
                    payload={
                        "idempotency_key":key,"business_date":bengaluru_today().isoformat(),
                        "payment_method":"upi","channel":"walk_in","staff_id":"S1",
                        "cart":{"store_code":"JN","location_id":"70000005",
                                "lines":[{"variant_id":variant["id"],"quantity":1}]},
                    }
                    body=json.dumps(payload).encode("utf-8")
                    request=Request(base+"/api/checkouts",data=body,
                                    headers={"Content-Type":"application/json"},method="POST")
                    try:
                        with urlopen(request) as response:
                            return response.status,json.loads(response.read())
                    except HTTPError as error:
                        result=json.loads(error.read());status=error.code;error.close()
                        return status,result

                with ThreadPoolExecutor(max_workers=2) as executor:
                    results=list(executor.map(checkout,("last-frame-A","last-frame-B")))
                self.assertEqual([201,422],sorted(status for status,_ in results))
                self.assertEqual(1,terminal.calls)
                success=next(payload for status,payload in results if status==201)
                self.assertEqual("COMPLETED",success["status"])
                failure=next(payload for status,payload in results if status==422)
                self.assertIn("Insufficient frame stock",failure["message"])
                after_orders=application.shopify_adapter.list_orders(
                    location_id="70000005",status="any",limit="250",
                )
                self.assertEqual(len(before_orders)+1,len(after_orders))
                stock=application.shopify_adapter.list_inventory_levels(
                    [variant["inventory_item_id"]],70000005,
                )
                self.assertEqual(0,stock[0]["available"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_closed_till_cash_variance_is_returned_in_close_day_end_and_anomaly_reports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mock_url="http://127.0.0.1:{}".format(self.port)
            environment={
                "ELUNO_SHOPIFY_BASE_URL":mock_url+"/admin/api/2026-10",
                "ELUNO_SMT_BASE_URL":mock_url+"/smt",
                "ELUNO_TERMINAL_BASE_URL":mock_url+"/terminal",
                "ELUNO_STATE_DB":str(Path(temp_dir)/"cash-variance.sqlite3"),
                "ELUNO_LOCAL_MOCK_MODE":"1",
                "ELUNO_LOCAL_MOCK_PORT":str(self.port),
            }
            with patch.dict(os.environ,environment):
                application=default_application()
            server,thread,base=start_authenticated_pos(application)
            try:
                status,till=self._request_json(base,"POST","/api/tills",{
                    "store_code":"JN","location_id":"70000005",
                    "operator":"S119","opening_float_paise":100000,
                })
                self.assertEqual(201,status)
                status,closed=self._request_json(
                    base,"POST","/api/tills/{}/close".format(till["session_id"]),{
                        "operator":"S119","counted_cash_paise":120001,
                    },
                )
                self.assertEqual(200,status)
                alert=closed["cash_variance_alert"]
                self.assertEqual("cash_variance_threshold_exceeded",alert["type"])
                self.assertEqual("JN",alert["store_code"])
                self.assertEqual(till["session_id"],alert["session_id"])
                self.assertEqual(100000,alert["expected_cash_paise"])
                self.assertEqual(120001,alert["counted_cash_paise"])
                self.assertEqual(20001,alert["variance_paise"])
                self.assertEqual(20000,alert["threshold_paise"])

                status,day_end=self._request_json(
                    base,"GET","/api/reports/day-end?store_id=70000005&date="+alert["business_date"],
                )
                self.assertEqual(200,status)
                self.assertEqual([alert["id"]],[item["id"] for item in day_end["cash_variance_alerts"]])
                status,anomaly=self._request_json(
                    base,"GET","/api/reports/anomalies?store_id=70000005&date="+alert["business_date"],
                )
                self.assertEqual(200,status)
                flag=next(item for item in anomaly["flags"] if item["rule"]=="CASH_VARIANCE_OVER_200")
                self.assertEqual(20001,flag["variance_paise"])
                self.assertEqual(alert["business_date"],flag["business_date"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    @staticmethod
    def _request_json(base,method,path,payload=None):
        body=None if payload is None else json.dumps(payload).encode("utf-8")
        request=Request(base+path,data=body,method=method)
        if body is not None:request.add_header("Content-Type","application/json")
        try:
            with urlopen(request) as response:return response.status,json.loads(response.read())
        except HTTPError as error:
            result=json.loads(error.read());status=error.code;error.close();return status,result
    def test_connection_refused_is_bounded_error(self):
        unused=socket.socket(); unused.bind(('127.0.0.1',0)); port=unused.getsockname()[1]; unused.close()
        with self.assertRaises(HttpRequestError) as raised:
            JsonHttpClient('http://127.0.0.1:{}'.format(port),max_attempts=1,timeout_seconds=.1).get('health')
        self.assertIsNone(raised.exception.status)

    def test_new_customer_checkout_invoice_round_trip_uses_real_local_mock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mock_url="http://127.0.0.1:{}".format(self.port)
            environment={
                "ELUNO_SHOPIFY_BASE_URL":mock_url+"/admin/api/2026-10",
                "ELUNO_SMT_BASE_URL":mock_url+"/smt",
                "ELUNO_TERMINAL_BASE_URL":mock_url+"/terminal",
                "ELUNO_STATE_DB":str(Path(temp_dir)/"customer-sale.sqlite3"),
                "ELUNO_LOCAL_MOCK_MODE":"1",
                "ELUNO_LOCAL_MOCK_PORT":str(self.port),
            }
            with patch.dict(os.environ,environment):
                application=default_application()
            server,thread,base=start_authenticated_pos(application, "cashier")
            try:
                answers=iter([
                    "HSR","70000002","2026-10-26","walk_in","Asha Rao",
                    "+919876543210","S1","1","1","1","0","0","2","2","y","upi","y",
                ])
                output=[];opened=[]
                cashier=CashierCLI(
                    ApiClient(base),lambda _:next(answers),output.append,
                    preview=default_preview(mock_url+"/admin/api/2026-10"),
                    browser_open=lambda url,new=0:opened.append(url) or True,
                )
                cashier.new_sale()
                self.assertIsNotNone(cashier.last_sale,"\n".join(output))
                self.assertTrue(any("status: COMPLETED" in line for line in output))
                self.assertTrue(any("Invoice:" in line for line in output))
                self.assertEqual([base+"/api/invoices/"+cashier.last_sale],opened)

                shopify=ShopifyAdminClient(base_url=mock_url+"/admin/api/2026-10")
                customers=shopify.search_customers_by_phone("+919876543210")
                self.assertEqual(1,len(customers))
                customer=customers[0]
                self.assertEqual("Asha",customer["first_name"])
                self.assertEqual("Rao",customer["last_name"])
                registration=json.dumps({"name":"Asha Rao","phone":"+919876543210"}).encode()
                request=Request(base+"/api/customers",data=registration,headers={"Content-Type":"application/json"},method="POST")
                with urlopen(request) as response:
                    self.assertEqual(200,response.status)
                    repeated=json.loads(response.read())["customer"]
                self.assertEqual(customer["id"],repeated["id"])

                invoice_request=Request(base+"/api/invoices/{}".format(cashier.last_sale))
                with urlopen(invoice_request) as response:
                    invoice=response.read().decode("utf-8")
                sale=application.checkout_service._sales.get(cashier.last_sale)
                self.assertIn("Asha Rao",invoice)
                self.assertIn("+919876543210",invoice)
                self.assertIn(sale["invoice_no"],invoice)
                self.assertIn("FESTIVE10",invoice)

                cashier.input=lambda _:sale["invoice_no"]
                cashier.invoice()
                self.assertEqual(
                    [base+"/api/invoices/"+cashier.last_sale]*2,
                    opened,
                )
                self.assertTrue(any("Sale {} status: COMPLETED".format(cashier.last_sale) in line for line in output))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_customer_assisted_lens_sale_is_in_admin_report_after_cashier_approval(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mock_url = "http://127.0.0.1:{}".format(self.port)
            environment = {
                "ELUNO_SHOPIFY_BASE_URL": mock_url + "/admin/api/2026-10",
                "ELUNO_SMT_BASE_URL": mock_url + "/smt",
                "ELUNO_TERMINAL_BASE_URL": mock_url + "/terminal",
                "ELUNO_STATE_DB": str(Path(temp_dir) / "customer-assisted-sale.sqlite3"),
                "ELUNO_LOCAL_MOCK_MODE": "1",
                "ELUNO_LOCAL_MOCK_PORT": str(self.port),
            }
            with patch.dict(os.environ, environment):
                application = default_application()
            server, thread, base = start_authenticated_pos(application, "admin")
            try:
                application.auth_manager.update_staff(
                    "S105", {"store_code": "HSR"}, "integration-admin",
                )
                admin_token = application.auth_manager.login(
                    "admin", "Test-Integration-Admin-Password-2026!",
                )[0]
                cashier_token = application.auth_manager.login(
                    "cashier", "Test-Integration-Cashier-Password-2026!",
                )[0]
                shopify = ShopifyAdminClient(base_url=mock_url + "/admin/api/2026-10")
                location = next(
                    item for item in shopify.list_locations()
                    if (item.get("eluno") or {}).get("store_code") == "HSR"
                )
                catalog = shopify.list_catalog_variants()
                frame = next(item for item in catalog if item.line_type == "frame")
                lens = next(item for item in catalog if item.line_type == "lens")
                frame_inventory_id = shopify.get_variant(int(frame.variant_id))["inventory_item_id"]
                stock = shopify.list_inventory_levels(
                    [frame_inventory_id], int(location["id"]),
                )
                available = next(
                    (int(item["available"]) for item in stock
                     if str(item.get("inventory_item_id")) == str(frame_inventory_id)),
                    0,
                )
                if available < 2:
                    shopify.adjust_inventory(
                        frame_inventory_id, int(location["id"]), 2 - available,
                        "customer-assisted-integration-stock-" + uuid4().hex,
                    )
                phone = "+919{:09d}".format(uuid4().int % 1_000_000_000)
                customer = shopify.create_customer("Integration", "Buyer", phone)
                application.customer_accounts.create_account(
                    str(customer["id"]), phone, "Integration Buyer",
                    "Integration-Customer-Password-2026!",
                )
                customer_token = application.customer_accounts.login(
                    phone, "Integration-Customer-Password-2026!", "127.0.0.1",
                )[0]
                business_day = bengaluru_today().isoformat()

                def request_json(method, path, payload, token, cookie_name):
                    body = None if payload is None else json.dumps(payload).encode("utf-8")
                    request = Request(base + path, data=body, method=method)
                    if body is not None:
                        request.add_header("Content-Type", "application/json")
                    request.add_header("Cookie", "{}={}".format(cookie_name, token))
                    try:
                        with urlopen(request) as response:
                            return response.status, json.loads(response.read())
                    except HTTPError as error:
                        payload = json.loads(error.read())
                        error.close()
                        return error.code, payload

                report_path = "/api/reports/day-end?store_id={}&date={}".format(
                    location["id"], business_day,
                )
                status, before = request_json(
                    "GET", report_path, None, admin_token, "eluno_session",
                )
                self.assertEqual(200, status)
                checkout = {
                    "idempotency_key": "assisted-lens-" + uuid4().hex,
                    "cart": {
                        "location_id": str(location["id"]),
                        "lines": [
                            {
                                "variant_id": frame.variant_id,
                                "quantity": 1,
                                "pair_ref": "customer-assisted-pair",
                            },
                            {
                                "variant_id": lens.variant_id,
                                "quantity": 1,
                                "pair_ref": "customer-assisted-pair",
                                "needs_prescription_assistance": True,
                            },
                        ],
                    },
                    "delivery_address": {
                        "line1": "12 Integration Road",
                        "line2": "",
                        "city": "Bengaluru",
                        "state": "Karnataka",
                        "postal_code": "560001",
                    },
                    "payment_method_preference": "upi",
                }
                status, created = request_json(
                    "POST", "/api/customer/orders", checkout, customer_token,
                    "eluno_customer_session",
                )
                self.assertEqual(201, status, created)
                order = created["order"]
                self.assertEqual("paid", order["payment_status"])
                self.assertTrue(order["invoice_no"])
                self.assertTrue(order["shopify_order_id"])
                self.assertTrue(order["items"][1]["shopify_line_item_id"])

                status, queue = request_json(
                    "GET", "/api/store/orders/pending", None, cashier_token,
                    "eluno_session",
                )
                self.assertEqual(200, status)
                self.assertIn(order["order_id"], [item["order_id"] for item in queue["orders"]])
                status, decision = request_json(
                    "POST", "/api/store/orders/{}/confirm".format(order["order_id"]),
                    {}, cashier_token, "eluno_session",
                )
                self.assertEqual(200, status)
                self.assertEqual("sold", decision["order"]["status"])
                self.assertEqual(order["invoice_no"], decision["order"]["invoice_no"])

                denied_checkout = {
                    **checkout,
                    "idempotency_key": "assisted-lens-denial-" + uuid4().hex,
                    "cart": {
                        "location_id": str(location["id"]),
                        "lines": [
                            {
                                "variant_id": frame.variant_id,
                                "quantity": 1,
                                "pair_ref": "customer-denied-pair",
                            },
                            {
                                "variant_id": lens.variant_id,
                                "quantity": 1,
                                "pair_ref": "customer-denied-pair",
                                "needs_prescription_assistance": True,
                            },
                        ],
                    },
                }
                status, denied_created = request_json(
                    "POST", "/api/customer/orders", denied_checkout, customer_token,
                    "eluno_customer_session",
                )
                self.assertEqual(201, status, denied_created)
                denied_order = denied_created["order"]
                assisted_lens = next(
                    item for item in denied_order["items"]
                    if item["prescription_assistance_requested"]
                )
                status, denial = request_json(
                    "POST",
                    "/api/store/orders/{}/deny".format(denied_order["order_id"]),
                    {"reason": "Prescription could not be verified in store"},
                    cashier_token,
                    "eluno_session",
                )
                self.assertEqual(200, status, denial)
                self.assertEqual("partially_refunded", denial["order"]["status"])
                self.assertEqual("COMPLETED", denial["order"]["refund_status"])
                self.assertEqual(assisted_lens["total_paise"], denial["order"]["refund_amount_paise"])
                self.assertEqual(denied_order["invoice_no"], denial["order"]["invoice_no"])

                status, after = request_json(
                    "GET", report_path, None, admin_token, "eluno_session",
                )
                self.assertEqual(200, status)
                self.assertEqual(
                    before["completed_bill_count"] + 2,
                    after["completed_bill_count"],
                )
                self.assertEqual(
                    before["channels"].get("online_pickup", {}).get("completed_bill_count", 0) + 2,
                    after["channels"]["online_pickup"]["completed_bill_count"],
                )
                self.assertEqual(
                    before["metrics"]["refund_amount_paise"] + assisted_lens["total_paise"],
                    after["metrics"]["refund_amount_paise"],
                )
                invoice_request = Request(
                    base + "/api/customer/orders/{}/invoice".format(order["order_id"]),
                    headers={"Cookie": "eluno_customer_session={}".format(customer_token)},
                )
                with urlopen(invoice_request) as response:
                    invoice = response.read().decode("utf-8")
                self.assertIn(order["invoice_no"], invoice)
                self.assertIn("Integration Buyer", invoice)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_checkout_invoice_refund_restock_and_day_end_use_real_local_mock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mock_url="http://127.0.0.1:{}".format(self.port)
            environment={
                "ELUNO_SHOPIFY_BASE_URL":mock_url+"/admin/api/2026-10",
                "ELUNO_SMT_BASE_URL":mock_url+"/smt",
                "ELUNO_TERMINAL_BASE_URL":mock_url+"/terminal",
                "ELUNO_STATE_DB":str(Path(temp_dir)/"refund-report.sqlite3"),
                "ELUNO_LOCAL_MOCK_MODE":"1",
                "ELUNO_LOCAL_MOCK_PORT":str(self.port),
            }
            with patch.dict(os.environ,environment):
                application=default_application()
            server,thread,base=start_authenticated_pos(application, "cashier")
            shopify=ShopifyAdminClient(base_url=mock_url+"/admin/api/2026-10")
            try:
                variants=shopify.list_catalog_variants()
                selected=None
                for variant in variants:
                    if not variant.active or variant.line_type!="frame":
                        continue
                    remote=shopify.get_variant(int(variant.variant_id))
                    item_id=remote.get("inventory_item_id")
                    if item_id is None:
                        continue
                    levels=shopify.list_inventory_levels([int(item_id)],70000002)
                    if len(levels)==1 and levels[0]["available"]>0:
                        selected=(variant,int(item_id),levels[0]["available"])
                        break
                self.assertIsNotNone(selected,"seed must have a stocked active HSR frame")
                variant,item_id,initial_stock=selected
                business_day=bengaluru_today().isoformat()
                checkout={
                    "idempotency_key":"integration-refund-report-"+uuid4().hex,
                    "business_date":business_day,
                    "payment_method":"upi",
                    "channel":"walk_in",
                    "staff_id":"S101",
                    "cart":{
                        "store_code":"HSR","location_id":"70000002",
                        "lines":[{"variant_id":variant.variant_id,"quantity":1}],
                    },
                }
                request=Request(
                    base+"/api/checkouts",data=json.dumps(checkout).encode("utf-8"),
                    headers={"Content-Type":"application/json"},method="POST",
                )
                with urlopen(request) as response:
                    self.assertEqual(201,response.status)
                    sale=json.loads(response.read())
                self.assertEqual("COMPLETED",sale["status"])
                self.assertTrue(sale["invoice_no"])
                self.assertEqual("ELU/HSR/FY26/00003", sale["invoice_no"])
                with urlopen(Request(base+"/api/invoices/"+sale["sale_id"])) as response:
                    invoice=response.read().decode("utf-8")
                self.assertIn(sale["invoice_no"],invoice)

                order=shopify.get_order(int(sale["order_id"]))
                self.assertEqual(sale["invoice_no"],next(
                    item["value"] for item in order["note_attributes"]
                    if item["name"]=="invoice_no"
                ))
                after_sale=shopify.list_inventory_levels([item_id],70000002)[0]["available"]
                self.assertEqual(initial_stock-1,after_sale)

                refund_request=Request(
                    base+"/api/refunds",
                    data=json.dumps({
                        "order_id":sale["order_id"],"store_code":"HSR",
                        "operator":"S101","reason":"integration test return",
                        "return_item_ids":[str(order["line_items"][0]["id"])],
                    }).encode("utf-8"),
                    headers={"Content-Type":"application/json"},method="POST",
                )
                with urlopen(refund_request) as response:
                    self.assertEqual(201,response.status)
                    refund=json.loads(response.read())
                self.assertEqual("COMPLETED",refund["status"])
                self.assertTrue(refund["terminal_refund_id"])

                refunded_order=shopify.get_order(int(sale["order_id"]))
                charge_id = next(
                    transaction["authorization"]
                    for transaction in refunded_order["transactions"]
                    if transaction.get("kind") == "sale"
                )
                terminal = PaymentTerminalClient(base_url=mock_url+"/terminal")
                provider_refund = terminal.get_charge(charge_id)
                self.assertEqual(refund["amount_paise"], provider_refund["refunded_paise"])
                self.assertEqual(1,len(refunded_order["refunds"]))
                restock=refunded_order["refunds"][0]["refund_line_items"][0]
                self.assertEqual("return",restock["restock_type"])
                self.assertEqual(70000002,restock["location_id"])
                self.assertEqual(
                    initial_stock,
                    shopify.list_inventory_levels([item_id],70000002)[0]["available"],
                )
                login_pos(base, "admin")
                report_request=Request(
                    base+"/api/reports/day-end?store_id=70000002&date="+business_day,
                )
                with urlopen(report_request) as response:
                    self.assertEqual(200,response.status)
                    report=json.loads(response.read())
                self.assertGreaterEqual(report["completed_bill_count"],1)
                self.assertGreater(report["metrics"]["refund_amount_paise"],0)
                self.assertIn("upi",report["payment_methods_paise"])
                self.assertGreaterEqual(report["refund_count"],1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_checkout_api_rejects_out_of_stock_frame_before_persisting_sale_or_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mock_url="http://127.0.0.1:{}".format(self.port)
            environment={
                "ELUNO_SHOPIFY_BASE_URL":mock_url+"/admin/api/2026-10",
                "ELUNO_SMT_BASE_URL":mock_url+"/smt",
                "ELUNO_TERMINAL_BASE_URL":mock_url+"/terminal",
                "ELUNO_STATE_DB":str(Path(temp_dir)/"inventory-stop.sqlite3"),
                "ELUNO_LOCAL_MOCK_MODE":"1",
                "ELUNO_LOCAL_MOCK_PORT":str(self.port),
            }
            with patch.dict(os.environ,environment):
                application=default_application()
            server,thread,base=start_authenticated_pos(application, "cashier")
            shopify=ShopifyAdminClient(base_url=mock_url+"/admin/api/2026-10")
            variant=shopify.get_variant(91000001)
            item_id=int(variant["inventory_item_id"])
            location_id=70000002
            levels=shopify.list_inventory_levels([item_id],location_id)
            self.assertEqual(1,len(levels))
            original_available=levels[0]["available"]
            writer=JsonHttpClient(mock_url+"/admin/api/2026-10")
            try:
                writer.post("inventory_levels/set.json",{
                    "inventory_item_id":item_id,"location_id":location_id,"available":0,
                },retry_statuses=())
                before_orders=shopify.list_orders(status="any",limit="250")
                payload={
                    "idempotency_key":"integration-out-of-stock-frame",
                    "business_date":"2026-10-26",
                    "payment_method":"upi",
                    "channel":"walk_in",
                    "staff_id":"S1",
                    "cart":{
                        "store_code":"HSR","location_id":str(location_id),
                        "lines":[{"variant_id":"91000001","quantity":1}],
                    },
                }
                request=Request(
                    base+"/api/checkouts",data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type":"application/json"},method="POST",
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request)
                response=raised.exception
                self.assertEqual(422,response.code)
                error=json.loads(response.read().decode("utf-8"))
                response.close()
                self.assertEqual("checkout_invalid",error["error"])
                self.assertIn("Insufficient frame stock",error["message"])
                self.assertEqual(before_orders,shopify.list_orders(status="any",limit="250"))
                with application.checkout_service._sales._session() as connection:
                    self.assertEqual(0,connection.execute(
                        "SELECT COUNT(*) FROM sales WHERE idempotency_key = ?",
                        ("integration-out-of-stock-frame",),
                    ).fetchone()[0])
            finally:
                writer.post("inventory_levels/set.json",{
                    "inventory_item_id":item_id,"location_id":location_id,"available":original_available,
                },retry_statuses=())
                server.shutdown()
                server.server_close()
                thread.join()

    def test_checkout_api_rejects_progressive_without_add_before_payment_or_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mock_url="http://127.0.0.1:{}".format(self.port)
            environment={
                "ELUNO_SHOPIFY_BASE_URL":mock_url+"/admin/api/2026-10",
                "ELUNO_SMT_BASE_URL":mock_url+"/smt",
                "ELUNO_TERMINAL_BASE_URL":mock_url+"/terminal",
                "ELUNO_STATE_DB":str(Path(temp_dir)/"prescription-stop.sqlite3"),
                "ELUNO_LOCAL_MOCK_MODE":"1",
                "ELUNO_LOCAL_MOCK_PORT":str(self.port),
            }
            with patch.dict(os.environ,environment):
                application=default_application()

            class MustNotCharge:
                calls=0
                def charge(self,*_):
                    self.calls+=1
                    raise AssertionError("invalid prescription reached payment")

            class MustNotCreateOrder:
                calls=0
                def create_order(self,*_):
                    self.calls+=1
                    raise AssertionError("invalid prescription reached Shopify order creation")

            terminal=MustNotCharge()
            orders=MustNotCreateOrder()
            application.checkout_service._terminal=terminal
            application.checkout_service._orders=orders
            server,thread,base=start_authenticated_pos(application, "cashier")
            shopify=ShopifyAdminClient(base_url=mock_url+"/admin/api/2026-10")
            before_orders=shopify.list_orders(status="any",limit="250")
            payload={
                "idempotency_key":"integration-invalid-progressive-"+uuid4().hex,
                "business_date":bengaluru_today().isoformat(),
                "payment_method":"upi",
                "channel":"walk_in",
                "staff_id":"S1",
                "customer_id":"80001001",
                "customer_phone":"+919000000001",
                "cart":{
                    "store_code":"HSR","location_id":"70000002",
                    "lines":[{"variant_id":"91000083","quantity":1,"prescription_id":"RX5002"}],
                },
            }
            try:
                request=Request(
                    base+"/api/checkouts",data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type":"application/json"},method="POST",
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request)
                response=raised.exception
                self.assertEqual(422,response.code)
                error=json.loads(response.read().decode("utf-8"))
                response.close()
                self.assertIn("ADD is required",error["message"])
                self.assertEqual(0,terminal.calls)
                self.assertEqual(0,orders.calls)
                self.assertEqual(before_orders,shopify.list_orders(status="any",limit="250"))
                with application.checkout_service._sales._session() as connection:
                    self.assertEqual(0,connection.execute(
                        "SELECT COUNT(*) FROM sales WHERE idempotency_key = ?",
                        (payload["idempotency_key"],),
                    ).fetchone()[0])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
