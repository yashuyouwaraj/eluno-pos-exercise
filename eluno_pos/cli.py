"""Cashier-facing CLI. Durable work is performed only by the POS API."""
import argparse
from datetime import date, datetime
import getpass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
import webbrowser

from .adapters.shopify import ShopifyAdminClient
from .business_time import bengaluru_today
from .cart import CartService
from .models import Cart, CartLine
from .offers import OfferBook, OfferEngine
from .services import customer_name_parts, normalize_customer_phone
from .tax import TaxRateTable


class SaleCancelled(Exception):
    pass


class ApiError(RuntimeError):
    def __init__(self, message, status=None, uncertain=False, payload=None):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain
        self.payload = payload or {}

class ApiClient:
    def __init__(self, base_url="http://127.0.0.1:8790"):
        self.base_url=base_url.rstrip("/")
        self.session_cookie=None
    def request(self, method, path, body=None):
        data=json.dumps(body).encode("utf-8") if body is not None else None
        req=Request(self.base_url+path,data=data,method=method)
        if data is not None: req.add_header("Content-Type","application/json")
        if self.session_cookie:
            req.add_header("Cookie",self.session_cookie)
        try:
            with urlopen(req,timeout=8) as response:
                status=response.status
                raw=response.read()
                if path == "/api/auth/login":
                    cookie=response.headers.get("Set-Cookie","").split(";",1)[0]
                    if cookie.startswith("eluno_session="):
                        self.session_cookie=cookie
        except HTTPError as exc:
            payload = {}
            try:
                error_body=exc.read()
                exc.close()
                response_payload=json.loads(error_body)
                payload=response_payload if isinstance(response_payload,dict) else {}
                message=(payload.get("message") or payload.get("error")) if isinstance(payload,dict) else None
            except (ValueError,UnicodeDecodeError):
                exc.close()
                message=None
            raise ApiError(message or str(exc),status=exc.code,uncertain=exc.code>=500,payload=payload)
        except (URLError,OSError) as exc:
            raise ApiError(
                "Server unavailable: {}. Start it with: python scripts/start_local.py".format(getattr(exc,"reason",exc)),
                uncertain=method == "POST",
            )
        try:
            payload=json.loads(raw)
        except (ValueError,UnicodeDecodeError) as exc:
            raise ApiError("Server returned malformed JSON.",status=status,uncertain=method == "POST") from exc
        if not isinstance(payload,dict):
            raise ApiError("Server returned an unexpected response; expected a JSON object.",status=status,uncertain=method == "POST")
        return status,payload
    def health(self): self.request("GET","/health")

def rupees(paise): return "Rs. {:,.2f}".format(int(paise or 0)/100)

class CashierCLI:
    MENU=("Cashier","Admin & Management","System Status")
    CHANNELS=("walk_in","whatsapp_booked","online_pickup")
    def __init__(self,api=None,input_fn=input,output_fn=print,preview=None,browser_open=None):
        self.api,self.input,self.out=api or ApiClient(),input_fn,output_fn;self.preview=preview;self.browser_open=browser_open;self.draft=None;self.last_sale=None
    def run(self):
        try:self.api.health()
        except ApiError as exc:self.out(str(exc));return 1
        try:
            while True:
                self._menu(); choice=self._ask("Select option")
                if choice=="0":self.out("Goodbye.");return 0
                handler={"1":self.cashier_menu,"2":self.admin_menu,"3":self.system_status}.get(choice)
                if not handler:self.out("Please enter a menu number from 0 to 3.");continue
                try:handler()
                except ApiError as exc:self.out("Unable to complete that action: "+str(exc))
                except (ValueError,KeyError) as exc:self.out("Please check the information entered: "+str(exc))
        except KeyboardInterrupt:self.out("\nInterrupted; no additional operation was submitted.");return 130
    def _menu(self):
        self.out("\n==================================================")
        self.out("                  ELUNO POS")
        self.out("             Optical Retail System")
        self.out("==================================================")
        for number,title in enumerate(self.MENU,1):self.out(" {}. {}".format(number,title))
        self.out(" 0. Exit")
    def _ask(self,label,required=True,default=None):
        value=self.input(label+(" [{}]".format(default) if default is not None else "")+": ").strip()
        if not value and default is not None:return str(default)
        if required and not value:raise ValueError(label+" is required")
        return value
    def _yes(self,label,default=False):
        while True:
            value=self._ask(label+(" [Y/n]" if default else " [y/N]"),False).lower()
            if not value:return default
            if value in ("y","yes"):return True
            if value in ("n","no"):return False
            self.out("Please enter Y or N.")
    def _choose(self,label,choices,default):
        self.out(label+":")
        for index,(value,title) in enumerate(choices,1):
            self.out(" {}. {}".format(index,title))
        self.out(" 0. Cancel")
        by_number={str(index):value for index,(value,_) in enumerate(choices,1)}
        by_value={value.lower():value for value,_ in choices}
        while True:
            choice=self._ask("Select option",default=default).lower()
            if choice=="0":return None
            selected=by_number.get(choice) or by_value.get(choice)
            if selected is not None:return selected
            self.out("Choose a listed option number or value.")
    def _date(self,default_today=False):return date.fromisoformat(self._ask("Business date (YYYY-MM-DD)",default=date.today().isoformat() if default_today else None))
    def _store(self):return self._ask("Store code"),self._ask("Store location ID")
    def _lookup(self,phone,day):
        _,result=self.api.request("GET","/api/customer-prescription?phone={}&business_date={}".format(phone.replace("+","%2B"),day.isoformat()))
        customer=result.get("customer",{});rx=result.get("prescription")
        self.out("Customer: {} {} ({})".format(customer.get("first_name",""),customer.get("last_name",""),customer.get("id","")))
        self.out("Latest valid prescription: {}".format(rx.get("rx_id")) if rx else result.get("message","No valid prescription was returned for this date."))
        return result

    def _customer_name(self):
        while True:
            value=self._ask("Customer full name")
            try:return " ".join(part for part in customer_name_parts(value) if part)
            except ValueError as exc:self.out("Please correct the name: {}".format(exc))

    def _customer_phone(self):
        while True:
            value=self._ask("Customer mobile number")
            try:return normalize_customer_phone(value)
            except ValueError as exc:self.out("Please correct the mobile number: {}".format(exc))

    def _sale_customer(self,day):
        name=self._ask("Customer name (leave blank for walk-in)",False)
        phone_text=self._ask("Customer mobile number (leave blank for walk-in)",False)
        while not name and phone_text:
            self.out("Enter both the customer name and mobile number, or leave both blank for walk-in.")
            name=self._ask("Customer name (leave blank for walk-in)",False)
        while name and not phone_text:
            self.out("Enter both the customer name and mobile number, or leave both blank for walk-in.")
            phone_text=self._ask("Customer mobile number (leave blank for walk-in)",False)
        if not name and not phone_text:
            self.out("Walk-in sale; customer details are optional.")
            return None,None,None
        while True:
            try:
                first_name,last_name=customer_name_parts(name)
                name=" ".join(part for part in (first_name,last_name) if part)
                break
            except ValueError as exc:
                self.out("Please correct the name: {}".format(exc))
                name=self._ask("Customer full name")
        while True:
            try:
                phone=normalize_customer_phone(phone_text)
                break
            except ValueError as exc:
                self.out("Please correct the mobile number: {}".format(exc))
                phone_text=self._ask("Customer mobile number")

        try:
            found=self._lookup(phone,day)
        except ApiError as exc:
            customer=exc.payload.get("customer") if exc.status==422 else None
            if exc.status==404 and exc.payload.get("status")=="customer_not_found":
                customer=None
            elif not isinstance(customer,dict):
                raise
            if isinstance(customer,dict):
                saved_name=" ".join(part for part in (
                    str(customer.get("first_name") or "").strip(),
                    str(customer.get("last_name") or "").strip(),
                ) if part)
                self.out("Using saved customer details: {} ({})".format(saved_name,customer.get("phone") or phone))
                self._display_customer_flags(customer)
                if exc.payload.get("status")=="no_valid_prescription":
                    self.out("No valid prescription is available for this date; frame-only sales may continue.")
                return customer.get("id"),customer.get("phone") or phone,None
        else:
            customer=found.get("customer",{})
            saved_name=" ".join(part for part in (
                str(customer.get("first_name") or "").strip(),
                str(customer.get("last_name") or "").strip(),
            ) if part)
            if saved_name.casefold()!=name.casefold():
                self.out("This mobile number is already registered to {}. Using the saved customer record.".format(saved_name))
            self._display_customer_flags(customer)
            if not found.get("prescription"):
                self.out("No valid prescription is available for this date; frame-only sales may continue.")
            return customer.get("id"),customer.get("phone"),found.get("prescription")

        first_name,last_name=customer_name_parts(name)
        try:
            _,result=self.api.request("POST","/api/customers",{"name":name,"phone":phone})
        except ApiError as exc:
            if exc.uncertain:
                self.out("Customer registration may have completed for {}. Do not change the mobile number; repeating the same name and number will resolve the existing customer.".format(phone))
            if exc.status==409:
                try:
                    found=self._lookup(phone,day)
                except ApiError as lookup_error:
                    customer=lookup_error.payload.get("customer") if lookup_error.status==422 else None
                    if isinstance(customer,dict):
                        self._display_customer_flags(customer)
                        return customer.get("id"),customer.get("phone") or phone,None
                    raise exc
                customer=found.get("customer",{})
                self.out("Using the customer record already registered for this mobile number.")
                self._display_customer_flags(customer)
                return customer.get("id"),customer.get("phone"),found.get("prescription")
            raise
        customer=result.get("customer") if isinstance(result,dict) else None
        if not isinstance(customer,dict):
            raise ApiError("Customer registration returned an incomplete response.",uncertain=True)
        customer_id=customer.get("id")
        customer_phone=customer.get("phone")
        if (
            customer_id is None or customer_phone!=phone
            or customer.get("first_name")!=first_name
            or customer.get("last_name","")!=last_name
        ):
            raise ApiError("Customer registration returned incomplete or mismatched details.",uncertain=True)
        self.out("New customer registered: {} {} ({})".format(first_name,last_name,customer_phone))
        self._display_customer_flags(customer)
        return customer_id,customer_phone,None

    def _display_customer_flags(self, customer):
        raw_tags=customer.get("tags")
        if isinstance(raw_tags,list):
            tags={str(tag).strip() for tag in raw_tags if isinstance(tag,str) and tag.strip()}
        elif isinstance(raw_tags,str):
            tags={tag.strip() for tag in raw_tags.split(",") if tag.strip()}
        else:
            tags=set()
        priority=any(tag.casefold()=="priority" for tag in tags)
        campaigns=sorted(
            (tag.split(":",1)[1] for tag in tags
             if tag.casefold().startswith("campaign:") and tag.split(":",1)[1].strip()),
            key=str.casefold,
        )
        self.out("Verified Shopify customer flags (separate from sales channel):")
        self.out("  Priority: {}".format("Yes" if priority else "No"))
        self.out("  Campaign tags: {}".format(", ".join(campaigns) if campaigns else "none"))

    def _submenu(self, title, choices):
        self.out("\n"+title)
        for number, label in choices: self.out(" {}. {}".format(number,label))
        self.out(" 0. Back")
        return self._ask("Select option")

    def cashier_menu(self):
        while True:
            choice=self._submenu("CASHIER",(("1","New Sale"),("2","Find Customer / Prescription"),("3","Find Sale / Invoice"),("4","Cash Drawer"),("5","Add Prescription"),("6","Browse Product Catalog")))
            if choice=="0": return
            handler={"1":self.new_sale,"2":self.find_customer,"3":lambda:self.invoice(True),"4":self.cash_drawer,"5":self.add_prescription,"6":self.browse_catalog}.get(choice)
            if not handler:self.out("Please enter a menu number from 0 to 6.");continue
            try: handler()
            except ApiError as exc:self.out("Unable to complete that action: "+str(exc))
            except (ValueError,KeyError) as exc:self.out("Please check the information entered: "+str(exc))

    def admin_menu(self):
        choices=(("1","Refunds & Returns"),("2","Day-End Reports"),("3","Sales Channel Report"),("4","Anomaly Monitoring"),("5","Unresolved Operations"),("6","Audit History"),("7","Lens Validation / Diagnostic Tools"),("8","Catalog & Inventory"))
        while True:
            choice=self._submenu("ADMIN & MANAGEMENT",choices)
            if choice=="0": return
            handler={"1":self.refund_menu,"2":self.day_end,"3":self.channel_report,"4":self.anomalies,"5":self.unresolved,"6":self.audit_history,"7":self.advanced_tools,"8":self.catalog_inventory_menu}.get(choice)
            if not handler:self.out("Please enter a menu number from 0 to 8.");continue
            try: handler()
            except ApiError as exc:self.out("Unable to complete that action: "+str(exc))
            except (ValueError,KeyError) as exc:self.out("Please check the information entered: "+str(exc))

    def catalog_inventory_menu(self):
        while True:
            choice=self._submenu("CATALOG & INVENTORY",(("1","Add a Frame or Lens"),("2","Receive Frame Stock"),("3","Browse Product Catalog")))
            if choice=="0":return
            handler={"1":self.add_catalog_product,"2":self.receive_frame_stock,"3":self.browse_catalog}.get(choice)
            if not handler:self.out("Please choose 1, 2, 3, or 0.");continue
            try:handler()
            except ApiError as exc:self.out("Unable to complete that action: "+str(exc))
            except (ValueError,KeyError) as exc:self.out("Please check the information entered: "+str(exc))

    def browse_catalog(self):
        self.out("\nACTIVE PRODUCT CATALOG")
        for line_type in ("frame", "lens"):
            _,result=self.api.request("GET","/api/catalog/variants?line_type="+line_type)
            variants=result.get("variants")
            if not isinstance(variants,list):
                raise ApiError("Catalog returned an unexpected response; expected a variants list.")
            self.out("{}s:".format(line_type.title()))
            if not variants:
                self.out("  No active {} products are available.".format(line_type))
                continue
            for variant in variants:
                title=" ".join(part for part in (
                    str(variant.get("product_title") or "").strip(),
                    str(variant.get("variant_title") or "").strip(),
                ) if part)
                self.out("  {} — {} (ID {})".format(
                    title or "Variant",rupees(variant.get("price_paise")),variant["variant_id"],
                ))
        self.out("To sell a product, choose Cashier > New Sale, then select its item type.")

    def add_catalog_product(self):
        line_type=self._choose("Product type",(("frame","Frame"),("lens","Lens")),"frame")
        if line_type is None:return
        title=self._ask("Product name")
        sku=self._ask("SKU (must be unique)")
        price_paise=self._paise("Selling price")
        actor=self._ask("Operator ID (caller-supplied; not authenticated)")
        body={"line_type":line_type,"title":title,"sku":sku,"price_paise":price_paise,"actor":actor}
        if line_type=="frame":
            category=self._choose("Frame category",(("optical","Optical frame"),("sunglasses","Sunglasses")),"optical")
            if category is None:return
            body.update({
                "category":category,
                "variant_title":self._ask("Colour / variant name"),
                "material":self._ask("Frame material"),
                "size":self._ask("Frame size (for example 52-18-140)"),
                "rx_able":self._yes("Can this frame take prescription lenses",True),
            })
        else:
            lens_type=self._choose("Lens type",(
                ("single_vision","Single vision"),("bifocal","Bifocal"),("progressive","Progressive"),
            ),"single_vision")
            if lens_type is None:return
            index=self._choose("Lens index",tuple((value,value) for value in ("1.50","1.56","1.60","1.67","1.74")),"1.50")
            if index is None:return
            coating=self._choose("Lens coating",(
                ("standard","Standard"),("anti_reflective","Anti-reflective"),
                ("blue_cut","Blue cut"),("photochromic","Photochromic"),
            ),"standard")
            if coating is None:return
            body.update({
                "lens_type":lens_type,"index":index,"coating":coating,
                "variant_title":"{} {} {}".format(lens_type.replace("_"," ").title(),index,coating.replace("_"," ").title()),
            })
        _,result=self.api.request("POST","/api/admin/catalog/products",body)
        product=result["product"]
        variant=product["variants"][0]
        self.out("Added {} product {} (SKU {}, variant {}).".format(
            line_type,product["title"],variant.get("sku"),variant.get("id"),
        ))
        if line_type=="frame":
            self.out("Frame stock starts at zero. Use Receive Frame Stock to record stock delivered to a store.")
        else:
            self.out("Lens is catalogued as made-to-order; no stock quantity was created.")

    def receive_frame_stock(self):
        _,result=self.api.request("GET","/api/admin/catalog/frames")
        variants=result.get("variants",[])
        if not variants:
            self.out("No tracked frame variants were returned by the catalog.")
            return
        self.out("Tracked frame variants:")
        for index,variant in enumerate(variants,1):
            self.out(" {}. {} {} — SKU {} (variant {})".format(
                index,variant.get("product_title",""),variant.get("variant_title",""),
                variant.get("sku",""),variant["variant_id"],
            ))
        selected=self._ask("Variant option or ID")
        variant=next((item for index,item in enumerate(variants,1)
                      if selected in (str(index),str(item["variant_id"]))),None)
        if variant is None:raise ValueError("choose a listed frame option or variant ID")
        _,location_result=self.api.request("GET","/api/admin/locations")
        locations=location_result.get("locations",[])
        if not locations:raise ValueError("no active store locations are available")
        self.out("Store locations:")
        for index,location in enumerate(locations,1):
            self.out(" {}. {} ({}) — ID {}".format(
                index,location.get("name"),location.get("store_code"),location["id"],
            ))
        selected=self._ask("Store option or location ID")
        location=next((item for index,item in enumerate(locations,1)
                       if selected in (str(index),str(item["id"]))),None)
        if location is None:raise ValueError("choose a listed store option or location ID")
        quantity=self._ask("Received quantity")
        if not quantity.isdigit() or int(quantity)<1:raise ValueError("received quantity must be a positive whole number")
        actor=self._ask("Receiving operator ID (caller-supplied; not authenticated)")
        reference=self._ask("Delivery / goods-received reference")
        _,receipt=self.api.request("POST","/api/admin/inventory/receipts",{
            "inventory_item_id":variant["inventory_item_id"],
            "location_id":location["id"],
            "quantity":int(quantity),
            "actor":actor,
            "reference":reference,
        })
        level=receipt["inventory_level"]
        if receipt.get("idempotent_replay"):
            self.out("That delivery reference was already processed; no additional stock was added. {} at {} currently has {} available.".format(
                variant.get("product_title"),location.get("store_code"),level.get("available"),
            ))
        else:
            self.out("Received {} unit(s). {} at {} now has {} available.".format(
                quantity,variant.get("product_title"),location.get("store_code"),level.get("available"),
            ))

    def _safe_url(self, value):
        """Render an endpoint without credentials, query values, or fragments.

        ``parsed.hostname`` intentionally excludes user info, but it also
        excludes a non-default port.  Keeping that port matters when an
        operator is checking a local/dev configuration.
        """
        parsed=urlsplit(value)
        host=parsed.hostname or ""
        try:
            if parsed.port is not None:
                host="{}:{}".format(host,parsed.port)
        except ValueError:
            # A malformed configured URL is still safe to display without
            # exposing its authority credentials.
            host=""
        return urlunsplit((parsed.scheme,host,parsed.path,"",""))

    def _ledger_status(self):
        path=Path(os.environ.get("ELUNO_STATE_DB",str(Path(__file__).resolve().parents[1]/"eluno_pos_state.sqlite3")))
        try:
            connection=sqlite3.connect("file:{}?mode=ro".format(path.resolve().as_posix()),uri=True,timeout=1)
            try: connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            finally: connection.close()
            return "HEALTHY", "SQLite ledger can be read"
        except (sqlite3.Error,OSError) as exc:
            return "UNAVAILABLE", "SQLite ledger cannot be read: {}".format(exc)

    def _status_snapshot(self):
        checks=[];required_statuses=[];environment="PRODUCTION_OR_UNKNOWN"
        try:
            _, health=self.api.request("GET","/health")
            health_status="HEALTHY" if health.get("ok") else "DEGRADED"
            checks.append(("POS API",health_status,"/health response"));required_statuses.append(health_status)
            _,service_info=self.api.request("GET","/")
            service_status="HEALTHY" if service_info.get("service")=="eluno-pos" else "UNKNOWN"
            checks.append(("Service information",service_status,"GET / response"));required_statuses.append(service_status)
            _, adapter_status=self.api.request("GET","/api/system-status")
            environment_value=adapter_status.get("environment")
            environment=environment_value if environment_value in ("LOCAL_MOCK","PRODUCTION_OR_UNKNOWN") else "PRODUCTION_OR_UNKNOWN"
            components=adapter_status.get("components")
            if not isinstance(components,list):
                checks.append(("Adapter checks","UNKNOWN","Status response did not contain a component list."))
                required_statuses.append("UNKNOWN")
                components=[]
            names=set()
            for component in components:
                if not isinstance(component,dict):
                    checks.append(("Adapter check","UNKNOWN","Status response contained an invalid component."))
                    required_statuses.append("UNKNOWN")
                    continue
                name=component.get("name")
                status=component.get("status")
                explanation=component.get("explanation")
                if isinstance(name,str) and isinstance(status,str) and isinstance(explanation,str):
                    checks.append((name,status,explanation))
                    names.add(name)
                    if component.get("required",True) is not False:
                        required_statuses.append(status)
            required_components=("Shopify adapter","Store-tool adapter","Local mock payment flow" if environment=="LOCAL_MOCK" else "Real payment terminal")
            for name in required_components:
                if name not in names:
                    checks.append((name,"UNKNOWN","Status response did not contain this required check."))
                    required_statuses.append("UNKNOWN")
        except ApiError as exc:
            checks.extend((("POS API","UNAVAILABLE",str(exc)),("Service information","NOT CHECKED","POS API is unavailable"),("Shopify adapter","NOT CHECKED","POS API is unavailable"),("Store-tool adapter","NOT CHECKED","POS API is unavailable"),("Payment terminal","NOT CHECKED","POS API is unavailable")))
            required_statuses.append("UNAVAILABLE")
        ledger, detail=self._ledger_status(); checks.append(("Local sale ledger",ledger,detail));required_statuses.append(ledger)
        statuses=required_statuses
        overall="UNAVAILABLE" if "UNAVAILABLE" in statuses else ("DEGRADED" if any(x in statuses for x in ("DEGRADED","UNKNOWN","NOT CHECKED","REACHABLE","NOT CONFIGURED")) else "READY")
        return overall,checks,environment

    def system_status(self):
        while True:
            overall, checks, environment=self._status_snapshot()
            self.out("\nSYSTEM STATUS")
            self.out("-"*50)
            for name,status,detail in checks:self.out("{:<22} {:<13} {}".format(name,status,detail))
            self.out("Environment: {}".format(environment.replace("_"," ")))
            self.out("Overall: {} | Last check: {}".format(
                overall, datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            ))
            choice=self._ask("1. Run checks again  2. View configuration summary  3. Test local mock payment flow  0. Back",default="0")
            if choice=="0": return
            if choice=="1": continue
            if choice=="2": self.configuration_summary(); continue
            if choice=="3":
                if environment!="LOCAL_MOCK":
                    self.out("Mock payment verification is disabled: the configured integrations are not positively identified as the supplied localhost mock.")
                    continue
                if not self._yes("Create a fixed Rs. 1.00 UPI test charge on the local mock only? No sale, order, or ledger entry will be created"):
                    self.out("Mock payment test cancelled; no test charge was submitted.")
                    continue
                try:
                    _,result=self.api.request("POST","/api/system-status/mock-payment-test",{})
                except ApiError as exc:
                    if exc.uncertain:
                        self.out("Mock test result is uncertain. Do not repeat immediately; run status checks to inspect the recorded result.")
                    else:
                        self.out("Mock payment test could not run: {}".format(exc))
                    continue
                self.out("Local mock payment flow: {} — {}".format(result.get("status","UNKNOWN"),result.get("explanation","No verification detail was returned.")))
                continue
            self.out("Please enter 0, 1, 2, or 3.")

    def configuration_summary(self):
        ledger=os.environ.get("ELUNO_STATE_DB",str(Path(__file__).resolve().parents[1]/"eluno_pos_state.sqlite3"))
        self.out("POS API: {}".format(self._safe_url(self.api.base_url)))
        self.out("Shopify adapter: {}".format(self._safe_url(os.environ.get("ELUNO_SHOPIFY_BASE_URL","http://127.0.0.1:8787/admin/api/2026-10"))))
        self.out("Store-tool adapter: {}".format(self._safe_url(os.environ.get("ELUNO_SMT_BASE_URL","http://127.0.0.1:8787/smt"))))
        self.out("Payment terminal: {}".format(self._safe_url(os.environ.get("ELUNO_TERMINAL_BASE_URL","http://127.0.0.1:8787/terminal"))))
        self.out("SQLite ledger: {}".format("configured" if ledger else "not configured"))
        self.out("Startup: python scripts/start_local.py | Health: {}/health".format(self._safe_url(self.api.base_url)))

    def new_sale(self):
        if self.draft and self.draft.get("outcome_uncertain"):
            sale_id=self.draft.get("sale_id")
            if sale_id:
                self.out("An earlier sale is unresolved. Do not start another sale. Check GET {}/api/checkouts/{}; if needed, POST {}/api/checkouts/{}/recover (this does not retry payment or order creation).".format(self.api.base_url,sale_id,self.api.base_url,sale_id))
            else:
                self.out("An earlier sale response was lost. Do not start another sale. Keep this draft and its idempotency key ({}) for operator reconciliation.".format(self.draft["idempotency_key"]))
            return
        self.out("\nNEW SALE — nothing is charged until final confirmation.")
        code,location=self._store();day=self._date(True)
        channel=self._choose("Sales channel",(
            ("walk_in","Walk-in"),
            ("whatsapp_booked","WhatsApp booked"),
            ("online_pickup","Online order pickup"),
        ),"1")
        if channel is None:
            self.out("Sale cancelled before submission; no charge was made.")
            return
        self.out("Sales channel: {}".format(channel.replace("_"," ").title()))
        try:customer_id,customer_phone,rx=self._sale_customer(day)
        except SaleCancelled:return
        self.draft={"idempotency_key":"cli-"+uuid4().hex,"business_date":day.isoformat(),"channel":channel,"staff_id":self._ask("Staff ID"),"customer_id":str(customer_id) if customer_id is not None else None,"customer_phone":customer_phone,"cart":{"store_code":code,"location_id":location,"lines":[]}}
        self._add_items(rx)
        if not self.draft["cart"]["lines"]:self.out("Sale cancelled before submission; no charge was made.");return
        self._select_offers(day)
        while True:
            if not self._show_preview():
                if self.draft.get("offer_ids") is not None:
                    self.out("Choose another offer mode or selection.")
                    self._select_offers(day)
                    continue
                self.out("Pricing preview is unavailable. The sale was not submitted.");return
            if self._yes("Continue to payment",True):break
            if not self._yes("Clear the cart and correct the items"):self.out("Sale cancelled before submission; the draft key is retained.");return
            self.draft["cart"]["lines"]=[];self._add_items(rx)
            if not self.draft["cart"]["lines"]:self.out("Sale cancelled before submission; no charge was made.");return
            self._select_offers(day)
        self.complete_checkout()
    def _add_items(self,rx):
        while True:
            self.out("Add item:\n 1. Frame\n 2. Lens\n 0. Done")
            kind=self._ask("Select item type",default="0").lower()
            kind={"1":"frame","2":"lens","0":"done"}.get(kind,kind)
            if kind=="done":return
            if kind not in ("frame","lens"):self.out("Choose 1 (frame), 2 (lens), or 0 (done).");continue
            if kind=="lens" and not rx:
                self.out("A lens needs a valid prescription. Look up the customer or add a frame only.")
                continue
            _,catalog=self.api.request("GET","/api/catalog/variants?line_type="+kind)
            variants=catalog.get("variants")
            if not isinstance(variants,list) or not variants:
                self.out("No active {} variants are available in the catalog.".format(kind))
                continue
            self.out("Available {} variants:".format(kind))
            for index,variant in enumerate(variants,1):
                title=" ".join(part for part in (
                    str(variant.get("product_title") or "").strip(),
                    str(variant.get("variant_title") or "").strip(),
                ) if part)
                self.out(" {}. {} — {} (ID {})".format(
                    index,title or "Variant",rupees(variant.get("price_paise")),variant["variant_id"],
                ))
            selection=self._ask("Variant option or ID")
            variant=next((
                item for index,item in enumerate(variants,1)
                if selection==str(index) or selection==str(item.get("variant_id"))
            ),None)
            if variant is None:
                self.out("Choose one of the listed {} variants by option number or ID.".format(kind))
                continue
            line={"variant_id":str(variant["variant_id"]),"quantity":int(self._ask("Quantity",default="1"))}
            pair=self._pair_reference()
            if pair:line["pair_ref"]=pair
            if kind=="lens":
                lens={
                    "lens_type":variant["lens_type"],
                    "index":variant["index"],
                    "coating":variant["coating"],
                }
                try:
                    _,valid=self.api.request("POST","/api/lens-validation",{"lens":lens,"prescription":rx})
                except ApiError as exc:
                    errors=exc.payload.get("errors") if exc.status==422 else None
                    if isinstance(errors,list):
                        self.out("Lens was not added: "+"; ".join(str(error) for error in errors))
                        continue
                    raise
                if not valid.get("valid"):self.out("Lens was not added: "+"; ".join(valid.get("errors",["configuration is not valid"])));continue
                validated_variant=valid.get("variant_id")
                if validated_variant is None or str(validated_variant)!=str(line["variant_id"]):
                    self.out("Lens was not added: the selected product does not match the validated lens configuration.")
                    continue
                line["prescription_id"]=rx["rx_id"]
                _,flags=self.api.request("POST","/api/billing-anomalies",{"phone":rx["phone"],"rx_id":rx["rx_id"],"lens":lens})
                for flag in flags.get("flags",[]):self.out("Review needed — {}: {}".format(flag["rule"],flag["explanation"]))
            self.draft["cart"]["lines"].append(line);self.out("Added {} item.".format(kind))

    def _pair_reference(self):
        references=[]
        for line in self.draft["cart"]["lines"]:
            ref=line.get("pair_ref")
            if ref and ref not in references:
                references.append(ref)
        self.out("Pairing:\n 0. Do not pair this item\n 1. Start a new pair")
        for index,reference in enumerate(references,2):
            self.out(" {}. Add to pair {}".format(index,index-1))
        while True:
            choice=self._ask("Pair selection",default="0")
            if choice=="0":
                return None
            if choice=="1":
                return "pair-"+uuid4().hex
            if choice.isdigit() and 2<=int(choice)<2+len(references):
                return references[int(choice)-2]
            self.out("Choose 0, 1, or a listed existing pair.")

    def _select_offers(self,day):
        self.out("Offer selection:\n 1. Automatic best eligible offers\n 2. Choose offers manually\n 3. No offers")
        while True:
            choice=self._ask("Select offer mode",default="1").lower()
            if choice in ("1","automatic","auto"):
                self.draft.pop("offer_ids",None)
                self.out("Automatic offer selection enabled.")
                return
            if choice in ("3","none","no"):
                self.draft["offer_ids"]=[]
                self.out("No offers will be applied.")
                return
            if choice in ("2","manual"):
                break
            self.out("Choose 1, 2, or 3.")
        _,response=self.api.request(
            "GET","/api/offers?business_date={}".format(day.isoformat()),
        )
        offers=response.get("offers")
        if not isinstance(offers,list):
            raise ApiError("The offer service returned an invalid offer list.")
        if not offers:
            self.out("No manually selectable offers are active for this business date.")
            self.draft["offer_ids"]=[]
            return
        self.out("Available offer choices:")
        for index,offer in enumerate(offers,1):
            stack_label="stackable" if offer.get("stackable") else "exclusive"
            self.out(" {}. {} ({}, {})".format(
                index,offer.get("name",offer["offer_id"]),offer["offer_id"],stack_label,
            ))
        while True:
            selection=self._ask("Offer option numbers, separated by commas (blank for none)",False)
            if not selection:
                self.draft["offer_ids"]=[]
                return
            choices=[item.strip() for item in selection.split(",")]
            selected=[]
            invalid=[]
            for choice in choices:
                offer=next((
                    item for index,item in enumerate(offers,1)
                    if choice==str(index) or choice==str(item["offer_id"])
                ),None)
                if offer is None:
                    invalid.append(choice)
                elif offer["offer_id"] not in selected:
                    selected.append(offer["offer_id"])
            if invalid:
                self.out("Unknown offer choice(s): {}. Select the listed number or code.".format(", ".join(invalid)))
                continue
            if len(selected)>1 and any(
                not offer.get("stackable")
                for offer in offers if offer["offer_id"] in selected
            ):
                self.out("An exclusive offer cannot be combined with another offer. Select one offer or choose stackable offers.")
                continue
            self.draft["offer_ids"]=selected
            return

    def _show_preview(self):
        if not self.preview:self.out("Pricing preview is unavailable in this cashier configuration.");return False
        try:pricing=self.preview(self.draft)
        except (ValueError,KeyError) as exc:self.out("Pricing preview could not be calculated: {}".format(exc));return False
        self.out("\nPRICE REVIEW")
        for line in pricing.lines:self.out("  {} x{} — {}".format(line.catalog_variant.line_type.title(),line.cart_line.quantity,rupees(line.payable_paise)))
        self.out("Gross {} | Offers {} | Taxable {}".format(rupees(pricing.gross_paise),rupees(pricing.discount_paise),rupees(pricing.taxable_paise)))
        applied=getattr(getattr(pricing,"offers",None),"applied",())
        if applied:
            self.out("Offers applied: {}".format(", ".join(
                "{} ({})".format(offer.offer_id,rupees(offer.discount_paise)) for offer in applied
            )))
        else:
            self.out("Offers applied: none")
        self.out("CGST {} | SGST {} | TOTAL PAYABLE {}".format(rupees(pricing.cgst_paise),rupees(pricing.sgst_paise),rupees(pricing.payable_paise)))
        return True
    def complete_checkout(self):
        if not self.draft:self.out("No sale draft is open. Choose New sale first.");return
        payment=self._choose("Payment method",(
            ("cash","Cash"),
            ("card","Card"),
            ("upi","UPI"),
        ),"3")
        if payment is None:
            self.out("Checkout cancelled; no payment was submitted.")
            return
        payload=dict(self.draft);payload["payment_method"]=payment
        if payment=="cash":
            payload["till_session_id"]=self._checkout_till(payload)
        if not self._yes("Submit sale for payment"):self.out("Sale cancelled before submission; draft and idempotency key retained.");return
        try:
            _,result=self.api.request("POST","/api/checkouts",payload)
        except ApiError as exc:
            if exc.uncertain:
                self.draft["outcome_uncertain"]=True
                self.out("Checkout outcome is uncertain. Do not submit another sale. The draft key {} is retained; reconcile it before continuing.".format(self.draft["idempotency_key"]))
                return
            raise
        self.last_sale=result.get("sale_id")
        self.draft["sale_id"]=self.last_sale
        status=result.get("status","UNKNOWN")
        self.draft["outcome_uncertain"]=status not in ("COMPLETED","PAYMENT_DECLINED")
        self.out("Sale {} — status: {}".format(self.last_sale or "(no sale ID)",status))
        if status=="COMPLETED":
            self.out("Payment recorded. Invoice: {}.".format(result.get("invoice_no") or "pending"))
            self._open_invoice(self.last_sale) if self.last_sale else None
        elif status=="PAYMENT_DECLINED":self.out("Payment was declined. No order was created.")
        elif status in ("PAYMENT_FAILED","ORDER_FAILED"):
            self.out("Sale failed but may need reconciliation. Do not start another sale; check status and contact a manager before proceeding.")
        else:
            self.draft["outcome_uncertain"]=True
            if self.last_sale:
                self.out("This result is uncertain. Do not submit another sale. Check GET {}/api/checkouts/{}; if needed, POST {}/api/checkouts/{}/recover (this does not retry payment or order creation).".format(self.api.base_url,self.last_sale,self.api.base_url,self.last_sale))
            else:
                self.out("This result is uncertain. Do not submit another sale. Keep the draft key {} for operator reconciliation.".format(self.draft["idempotency_key"]))

    def _checkout_till(self, payload):
        cart=payload["cart"]
        store_code=cart["store_code"]
        location_id=str(cart["location_id"])
        _,result=self.api.request("GET","/api/tills?store_code="+quote(store_code,safe=""))
        tills=[
            till for till in result.get("tills",[])
            if str(till.get("location_id"))==location_id
        ]
        if not tills:
            if not self._yes("No open till for {} at location {}. Open one now?".format(store_code,location_id)):
                raise ValueError("cash checkout needs an open till; open one from Cash Drawer before retrying")
            _,opened=self.api.request("POST","/api/tills",{
                "store_code":store_code,
                "location_id":location_id,
                "operator":self._ask("Operator ID",default=payload.get("staff_id")),
                "opening_float_paise":self._paise("Opening float"),
            })
            self.out("Opened till session {} for {}.".format(opened["session_id"],store_code))
            return opened["session_id"]
        if len(tills)==1:
            self.out("Using open till session {}.".format(tills[0]["session_id"]))
            return tills[0]["session_id"]
        choices=[
            (till["session_id"],"{} | expected {}".format(
                till["session_id"],rupees(till.get("expected_paise")),
            ))
            for till in tills
        ]
        selected=self._choose("Select open till for {}".format(store_code),choices,"1")
        if selected is None:
            raise ValueError("cash checkout needs an open till")
        return selected

    def find_or_sale(self):
        choice=self._ask("Find: customer, sale, invoice, or back",default="back").lower()
        if choice=="customer":self.find_customer()
        elif choice in ("sale","invoice"):self.invoice(choice=="invoice")
        elif choice!="back":self.out("Enter customer, sale, invoice, or back.")
    def find_customer(self):
        phone=self._customer_phone()
        return self._lookup(phone,bengaluru_today())
    def _prescription_eye(self,side):
        self.out("{} eye prescription".format(side))
        while True:
            try:sph=float(self._ask("Sphere (SPH)"))
            except ValueError:self.out("Enter a numeric sphere value.");continue
            if -30<=sph<=30:break
            self.out("Sphere must be between -30 and +30.")
        while True:
            try:cyl=float(self._ask("Cylinder (CYL)"))
            except ValueError:self.out("Enter a numeric cylinder value.");continue
            if -10<=cyl<=0:break
            self.out("Cylinder must be between -10 and 0.")
        while True:
            raw_axis=self._ask("Axis (0-180 in 5-degree steps; blank if none)",False)
            if not raw_axis:
                axis=None
                if cyl==0:break
                self.out("Axis is required when cylinder is non-zero.")
                continue
            try:axis=int(raw_axis)
            except ValueError:self.out("Axis must be a whole number.");continue
            if 0<=axis<=180 and axis%5==0:break
            self.out("Axis must be between 0 and 180 in 5-degree steps.")
        while True:
            raw_add=self._ask("ADD (leave blank if none)",False)
            if not raw_add:add=None;break
            try:add=float(raw_add)
            except ValueError:self.out("ADD must be numeric or blank.");continue
            if 0<=add<=5:break
            self.out("ADD must be between 0 and 5.")
        return {"sph":sph,"cyl":cyl,"axis":axis,"add":add}
    def add_prescription(self):
        prescribed_on=self._date(True)
        self.out("Enter the customer name and mobile number; an existing customer is matched by mobile, otherwise the customer is registered.")
        customer_id,phone,_=self._sale_customer(prescribed_on)
        if customer_id is None or phone is None:
            raise ValueError("prescription entry requires a named customer with a mobile number")
        store_code=self._ask("Store code (for example MM or JN; not the Shopify location ID)").strip().upper()
        valid_until=self._date()
        while valid_until<prescribed_on:
            self.out("Validity date cannot be before prescription date.")
            valid_until=self._date()
        od=self._prescription_eye("Right (OD)")
        os=self._prescription_eye("Left (OS)")
        optometrist_id=self._ask("Confirming optometrist ID")
        if not self._yes("Confirm this prescription has been reviewed by the optometrist"):
            self.out("Prescription not saved.")
            return
        prescription={
            "phone":phone,"store_code":store_code,"date":prescribed_on.isoformat(),
            "valid_until":valid_until.isoformat(),"od":od,"os":os,
            "optometrist_id":optometrist_id,"optometrist_confirmed":True,
        }
        serialized=json.dumps(prescription,sort_keys=True,separators=(",",":"))
        key="rx-"+hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        try:
            _,result=self.api.request("POST","/api/prescriptions",{
                **prescription,"idempotency_key":key,
            })
        except ApiError as exc:
            if exc.uncertain:
                self.out("Prescription save outcome is uncertain. Re-enter the same details to safely resolve the same request; do not change its values until confirmed.")
            raise
        saved=result["prescription"]
        self.out("Prescription {} saved and confirmed by optometrist {} for {}.".format(
            saved["rx_id"],result["confirmed_by"],phone,
        ))
    def invoice(self,show_invoice=True):
        identifier=self._ask("Sale ID or invoice number",False) or self.last_sale
        if not identifier:raise ValueError("enter a sale ID or invoice number")
        if identifier.startswith("sale_"):
            _,result=self.api.request("GET","/api/checkouts/"+identifier)
        else:
            _,result=self.api.request("GET","/api/invoices/lookup?invoice_no="+quote(identifier,safe=""))
        sale=result.get("sale_id") or identifier
        self.out("Sale {} status: {}".format(sale,result["status"]))
        if self.draft and self.draft.get("sale_id")==sale:
            self.draft["outcome_uncertain"]=result["status"] not in ("COMPLETED","PAYMENT_DECLINED")
        if result["status"]=="COMPLETED" and show_invoice:self._open_invoice(sale)
        elif result["status"]!="PAYMENT_DECLINED":self.out("Do not resubmit this sale; use the status/recovery action above.")
    def _open_invoice(self,sale_id):
        url="{}/api/invoices/{}".format(self.api.base_url,sale_id)
        self.out("Invoice page: {}".format(url))
        if self.browser_open is not None:
            try:
                if not self.browser_open(url,new=2):
                    self.out("The browser did not open automatically. Copy the invoice page URL above into your browser.")
            except (OSError,webbrowser.Error) as exc:
                self.out("The browser could not be opened: {}. Copy the invoice page URL above into your browser.".format(exc))
    def refund_menu(self):
        choice=self._ask("Choose: refund, status, or back",default="back").lower()
        if choice=="refund":self.refund()
        elif choice=="status":self.refund_status()
        elif choice!="back":self.out("Enter refund, status, or back.")
    def refund(self):
        order=self._ask("Shopify order ID");code=self._ask("Store code");operator=self._ask("Operator ID");reason=self._ask("Reason");items=None
        if self._yes("Process a partial return"):
            items=[]
            while self._yes("Add returned item"):items.append({"line_item_id":self._ask("Line item ID"),"quantity":int(self._ask("Quantity"))})
            if not items:raise ValueError("a partial return needs at least one item")
        payload={"order_id":order,"store_code":code,"operator":operator,"reason":reason,"items":items}
        if self._yes("Refund cash from an open till"):payload["till_session_id"]=self._ask("Till session ID")
        if not self._yes("Submit refund"):self.out("Refund cancelled; nothing was submitted.");return
        _,result=self.api.request("POST","/api/refunds",payload);self.out("Refund {} — {}. Credit note: {}".format(result["refund_id"],result["status"],result.get("credit_note") or "pending"))
        if result["status"]!="COMPLETED":self.out("Do not submit this refund again; check its status first.")
    def refund_status(self):
        rid=self._ask("Refund ID");_,result=self.api.request("GET","/api/refunds/"+rid);self.out("Refund {} status: {}".format(rid,result.get("status","unknown")))
    def cash_drawer(self):
        choice=self._ask("Choose: open, view, paid-in, paid-out, close, or back",default="back").lower()
        if choice in ("open","view"):self.till(choice=="open")
        elif choice in ("paid-in","paid-out"):self.movement(choice.replace("-","_"))
        elif choice=="close":self.close_till()
        elif choice!="back":self.out("Enter open, view, paid-in, paid-out, close, or back.")
    def till(self,open_new=False):
        if open_new:
            code,location=self._store();_,result=self.api.request("POST","/api/tills",{"store_code":code,"location_id":location,"operator":self._ask("Operator ID"),"opening_float_paise":self._paise("Opening float")})
        else:
            session_id=self._ask("Live till session ID (leave blank to list open tills)",False)
            if not session_id:
                code=self._ask("Store code")
                _,result=self.api.request("GET","/api/tills?store_code="+quote(code,safe=""))
                tills=result.get("tills",[])
                if not tills:
                    self.out("No open till sessions for {}. Choose open to start a live till; sample till IDs are not active sessions.".format(code))
                    return
                self.out("Open till sessions:")
                for till in tills:
                    self.out("{} | {} | location {} | expected {}".format(
                        till["session_id"],till["store_code"],till["location_id"],
                        rupees(till.get("expected_paise")),
                    ))
                return
            _,result=self.api.request("GET","/api/tills/"+session_id)
        self.out("Till {} is {}. Expected cash: {}".format(result["session_id"],result["status"],rupees(result.get("expected_paise"))))
    def movement(self,kind=None):
        kind=kind or self._ask("Type (paid_in or paid_out)")
        if kind not in ("paid_in","paid_out"):raise ValueError("only paid_in or paid_out are permitted")
        sid=self._ask("Till session ID");_,result=self.api.request("POST","/api/tills/{}/movements".format(sid),{"type":kind,"amount_paise":self._paise("Amount"),"operator":self._ask("Operator ID"),"reason":self._ask("Reason")});self.out("Expected cash: "+rupees(result["expected_paise"]))
    def close_till(self):
        sid=self._ask("Till session ID");_,r=self.api.request("POST","/api/tills/{}/close".format(sid),{"operator":self._ask("Operator ID"),"counted_cash_paise":self._paise("Counted cash")});self.out("Drawer closed. Expected {}; counted {}; variance {}.".format(rupees(r["expected_paise"]),rupees(r["counted_paise"]),rupees(r["variance_paise"])))
        alert=r.get("cash_variance_alert")
        if alert:
            self.out("FINANCE ALERT — cash variance exceeds Rs. 200.00. Store {}, session {}, business date {}, expected {}, counted {}, variance {}. Review this in Admin & Management > Anomaly Monitoring; no email was sent.".format(
                alert["store_code"],alert["session_id"],alert["business_date"],
                rupees(alert["expected_cash_paise"]),rupees(alert["counted_cash_paise"]),
                rupees(alert["variance_paise"]),
            ))
    def reports_alerts(self):
        choice=self._ask("Choose: day-end, alerts, unresolved, or back",default="back").lower()
        if choice=="day-end":self.day_end()
        elif choice=="alerts":self.anomalies()
        elif choice=="unresolved":self.unresolved()
        elif choice!="back":self.out("Enter day-end, alerts, unresolved, or back.")
    def _report(self,path):
        _,result=self.api.request("GET",path.format(self._ask("Store location ID"),self._date().isoformat()));return result
    def day_end(self):
        r=self._report("/api/reports/day-end?store_id={}&date={}");m=r["metrics"]
        self.out("Bills {} | Gross {} | Discounts {} | Net {} | Refunds {}".format(r["completed_bill_count"],rupees(m["gross_sales_paise"]),rupees(m["discounts_paise"]),rupees(m["net_sales_before_refunds_paise"]),rupees(m["refund_amount_paise"])))
        self.out("Taxable {} | CGST {} | SGST {}".format(rupees(m["taxable_sales_paise"]),rupees(m["cgst_paise"]),rupees(m["sgst_paise"])))
        self.out("Payments: {}".format(", ".join("{} {}".format(k,rupees(v)) for k,v in r.get("payment_methods_paise",{}).items()) or "none"))
        for alert in r.get("cash_variance_alerts",[]):
            self.out("FINANCE ALERT — store {}, till {}, business date {}, expected {}, counted {}, variance {} (threshold Rs. 200.00). No email was sent.".format(
                alert["store_code"],alert["session_id"],alert["business_date"],
                rupees(alert["expected_cash_paise"]),rupees(alert["counted_cash_paise"]),
                rupees(alert["variance_paise"]),
            ))
    def channel_report(self):
        r=self._report("/api/reports/day-end?store_id={}&date={}")
        if not r.get("channels"):self.out("No sales-channel figures are available for this date.");return
        self.out("SALES CHANNEL REPORT")
        for name,values in sorted(r["channels"].items()):self.out("{}: {} bill(s), gross {}, discounts {}, net {}, refunds {}".format(name,values["completed_bill_count"],rupees(values["gross_sales_paise"]),rupees(values["discounts_paise"]),rupees(values["net_sales_before_refunds_paise"]),rupees(values["refund_amount_paise"])))
    def anomalies(self):
        r=self._report("/api/reports/anomalies?store_id={}&date={}");self.out("Alerts:")
        for flag in r["flags"]:
            self.out("{} [{}] {}".format(flag["rule"],flag["severity"],flag["explanation"]))
            if flag["rule"]=="CASH_VARIANCE_OVER_200":
                self.out("  Store {} | Till {} | Date {} | Expected {} | Counted {} | Variance {}".format(
                    flag["store_code"],flag["session_id"],flag["business_date"],
                    rupees(flag["expected_cash_paise"]),rupees(flag["counted_cash_paise"]),
                    rupees(flag["variance_paise"]),
                ))
        if not r["flags"]:self.out("No alerts in the returned data.")
    def unresolved(self):self.out("Unresolved operations are shown in the day-end report and alerts.");self.day_end();self.anomalies()
    def audit_history(self):
        entity=self._ask("Entity ID (leave blank for all recorded events)",False)
        path="/api/audit-events"+("?entity_id="+entity if entity else "")
        _,result=self.api.request("GET",path);events=result.get("events",[])
        self.out("AUDIT HISTORY — caller-supplied operator IDs, not authenticated identities.")
        for event in events[-20:]:self.out("{} | {} | {} | {}".format(event.get("occurred_at"),event.get("actor"),event.get("action"),event.get("entity_id")))
        if not events:self.out("No matching audit events.")
    def advanced_tools(self):
        choice=self._ask("Choose: validate-lens, preview-draft, or back",default="back").lower()
        if choice=="validate-lens":self.validate_lens()
        elif choice=="preview-draft":self._show_preview() if self.draft else self.out("No sale draft is open.")
        elif choice!="back":self.out("Enter validate-lens, preview-draft, or back.")
    def validate_lens(self):
        found=self._lookup(self._customer_phone(),self._date());rx=found.get("prescription")
        if not rx:raise ValueError("a valid customer prescription is required")
        lens={"lens_type":self._ask("Lens type"),"index":self._ask("Lens index"),"coating":self._ask("Lens coating")};_,result=self.api.request("POST","/api/lens-validation",{"lens":lens,"prescription":rx});self.out("Lens configuration is valid." if result.get("valid") else "Lens configuration failed: "+"; ".join(result.get("errors",[])));return result
    def _paise(self,label):
        value=self._ask(label+" (rupees)");whole,dot,fraction=value.partition(".")
        if not whole.lstrip("-").isdigit() or (dot and (not fraction.isdigit() or len(fraction)>2)):raise ValueError("enter a rupee amount with at most two decimals")
        return int(whole)*100+(int((fraction+"00")[:2]) if int(whole)>=0 else -int((fraction+"00")[:2]))

def default_preview(mock_url):
    root=Path(__file__).resolve().parents[1]
    shopify=ShopifyAdminClient(base_url=mock_url)
    carts=CartService(shopify,OfferEngine(OfferBook.from_file(root/"starter-kit/data/offers.json")),TaxRateTable.from_file(root/"starter-kit/data/tax_rates.json"))
    def preview(draft):
        lines=tuple(CartLine(str(x["variant_id"]),x["quantity"],x.get("pair_ref"),x.get("prescription_id")) for x in draft["cart"]["lines"])
        tags=frozenset()
        if draft.get("customer_id") and draft.get("customer_phone"):
            customers=shopify.search_customers_by_phone(draft["customer_phone"])
            matches=[customer for customer in customers if str(customer.get("id"))==str(draft["customer_id"])]
            if len(matches)==1 and isinstance(matches[0].get("tags"),str):
                tags=frozenset(tag.strip() for tag in matches[0]["tags"].split(",") if tag.strip())
        selected=draft.get("offer_ids")
        return carts.price(
            Cart(draft["cart"]["store_code"],draft["cart"]["location_id"],lines),
            date.fromisoformat(draft["business_date"]),tags,selected,
        )
    return preview
def main():
    parser=argparse.ArgumentParser(description="Interactive Eluno POS cashier CLI");parser.add_argument("--base-url",default="http://127.0.0.1:8790");parser.add_argument("--mock-url",default="http://127.0.0.1:8787/admin/api/2026-10");args=parser.parse_args()
    api=ApiClient(args.base_url)
    username=os.environ.get("ELUNO_CLI_USERNAME") or input("POS username: ").strip()
    password=getpass.getpass("POS password: ")
    try:
        api.request("POST","/api/auth/login",{"username":username,"password":password})
    except ApiError as exc:
        print("Unable to sign in to the POS API: {}".format(exc))
        return 1
    return CashierCLI(api,preview=default_preview(args.mock_url),browser_open=webbrowser.open).run()
if __name__=="__main__":raise SystemExit(main())
