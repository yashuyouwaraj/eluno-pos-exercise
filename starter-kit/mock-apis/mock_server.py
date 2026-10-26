#!/usr/bin/env python3
"""Zero-dependency local mock of the three systems the POS talks to.

    python3 mock_server.py                 # http://localhost:8787
    python3 mock_server.py --port 9000 --no-flake

1. Shopify Admin REST shape, base /admin/api/2026-10  (the system of record)
   GET  customers/search.json?query=phone:%2B919000000005
   POST customers.json {"customer":{"first_name":"Asha","last_name":"Rao","phone":"+919876543210"}}
   GET  locations.json
   GET  products.json?product_type=Frame|Lens&limit=250        GET products/{id}.json   GET variants/{id}.json
   POST products.json {"product": {...}}                       -> creates one Eluno frame/lens variant
   GET  inventory_levels.json?inventory_item_ids=1,2&location_ids=3
   POST inventory_levels/connect.json {location_id, inventory_item_id}
   POST inventory_levels/adjust.json {location_id, inventory_item_id, available_adjustment}
   POST inventory_levels/set.json    {location_id, inventory_item_id, available}
   POST orders.json {"order": {...}}                            -> 201 with id, order_number, name, line_item ids
        stock is decremented only if order.inventory_behaviour is decrement_obeying_policy / decrement_ignoring_policy (default bypass)
   GET  orders.json?created_at_min=&created_at_max=&location_id=&status=any&financial_status=&limit=
   GET  orders/{id}.json
   POST orders/{id}/transactions.json {"transaction": {kind, amount, gateway, ...}}
   POST orders/{id}/refunds/calculate.json {"refund": {refund_line_items:[{line_item_id, quantity, restock_type}]}}
   POST orders/{id}/refunds.json {"refund": {refund_line_items:[...], transactions:[{kind:"refund", amount, gateway, parent_id}], note}}
   Behaviour that matches the real thing: money is a decimal string ("104.98"), ids are integers, order_number is global,
   taxes_included must be true, ~10% of writes return 429 with Retry-After, X-Shopify-Shop-Api-Call-Limit header on every response,
   refunds cannot exceed captured sale transactions, restock_type "return" adjusts inventory at the given location.
2. SMT (store tool) shape
   GET /smt/prescriptions?phone=%2B919000000005        GET /smt/staff?store=HSR
   POST /smt/prescriptions {"prescription": {...}, "idempotency_key": "..."}
        local exercise extension only; requires an existing customer and a
        store-matched optometrist; not a production SMT write contract
3. Payment terminal shape (Razorpay-like)
   POST /terminal/charges {amount_paise, method, order_ref}    -> 201 {charge_id, status:"captured"} or 503 once, retry
   GET  /terminal/charges/{id}                                 -> idempotent status
   POST /terminal/refunds {charge_id, amount_paise, reason}
Run with --state-db to persist mock state in SQLite across process restarts. Without it, the mock resets to seed data.
"""
import json, os, sys, random, argparse, re, threading, sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
def L(n):
    with open(os.path.join(HERE, "..", "data", n), encoding="utf-8") as seed_file:
        return json.load(seed_file)
API = "/admin/api/2026-10/"
random.seed(7)
FLAKE = True

S = {
    "customers": L("customers.json"), "locations": L("locations.json"), "products": L("products.json"),
    "inventory": {(r["inventory_item_id"], r["location_id"]): r for r in L("inventory_levels.json")},
    "orders": {o["id"]: o for o in L("orders_sample.json")}, "rx": L("prescriptions.json"), "staff": L("staff.json"),
    "charges": {}, "prescription_requests": {}, "inventory_adjustments": {},
    "seq": {"order": max(o["order_number"] for o in L("orders_sample.json")), "id": 99000000, "calls": 0},
}
CUSTOMER_LOCK = threading.Lock()
PRESCRIPTION_LOCK = threading.Lock()
VARIANTS = {v["id"]: v for p in S["products"] for v in p["variants"]}
STATE_DB = None
STATE_LOCK = threading.Lock()
MONEY = re.compile(r"^-?\d+\.\d{2}$")
def nid(): S["seq"]["id"] += 1; return S["seq"]["id"]
def now(): return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
def paise(s): return int(round(float(s) * 100))
def money(p): return f"{'-' if p < 0 else ''}{abs(p) // 100}.{abs(p) % 100:02d}"

def _state_payload():
    payload = {key: S[key] for key in ("customers", "locations", "products", "rx", "staff", "charges", "prescription_requests", "inventory_adjustments", "seq")}
    payload["inventory"] = [
        {"inventory_item_id": item_id, "location_id": location_id, "row": row}
        for (item_id, location_id), row in S["inventory"].items()
    ]
    payload["orders"] = [{"id": order_id, "order": order} for order_id, order in S["orders"].items()]
    return json.dumps(payload, separators=(",", ":"))

def _save_state():
    if STATE_DB is None:
        return
    with STATE_LOCK, sqlite3.connect(STATE_DB, timeout=10) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS mock_state (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO mock_state(id, payload) VALUES(1, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
            (_state_payload(),),
        )

def _load_state(path):
    global VARIANTS
    with sqlite3.connect(path, timeout=10) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS mock_state (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL)")
        row = connection.execute("SELECT payload FROM mock_state WHERE id=1").fetchone()
        if row is None:
            _save_state()
            return
    payload = json.loads(row[0])
    payload.setdefault("inventory_adjustments", {})
    for key in ("customers", "locations", "products", "rx", "staff", "charges", "prescription_requests", "inventory_adjustments", "seq"):
        if key not in payload:
            raise ValueError("mock state database is missing {}".format(key))
        S[key] = payload[key]
    S["inventory"] = {
        (item["inventory_item_id"], item["location_id"]): item["row"]
        for item in payload["inventory"]
    }
    S["orders"] = {item["id"]: item["order"] for item in payload["orders"]}
    VARIANTS = {v["id"]: v for product in S["products"] for v in product["variants"]}

class H(BaseHTTPRequestHandler):
    def _send(self, code, body, extra=None):
        if self.command == "POST":
            _save_state()
        b = json.dumps(body).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(b)))
        S["seq"]["calls"] = (S["seq"]["calls"] + 1) % 40; self.send_header("X-Shopify-Shop-Api-Call-Limit", f"{S['seq']['calls']}/40")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(b)
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try: return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError: return None
    def log_message(self, fmt, *a): sys.stderr.write(f"{self.command} {urlparse(self.path).path}\n")
    def _err(self, code, msg): return self._send(code, {"errors": msg})

    # ------------------------------------------------------------ GET
    def do_GET(self):
        u = urlparse(self.path); q = {k: unquote(v[0]) for k, v in parse_qs(u.query).items()}; p = u.path
        if p == "/health": return self._send(200, {"ok": True})
        if p.startswith("/smt/"):
            if p == "/smt/prescriptions":
                ph = q.get("phone", "").replace(" ", "+")
                return self._send(200, {"prescriptions": sorted([r for r in S["rx"] if r["phone"] == ph], key=lambda r: r["date"], reverse=True)})
            if p == "/smt/staff": return self._send(200, {"staff": [s for s in S["staff"] if not q.get("store") or s["store_code"] == q["store"]]})
            return self._err(404, "Not Found")
        if p.startswith("/terminal/charges/"):
            c = S["charges"].get(p.rsplit("/", 1)[1]); return self._send(200, c) if c else self._err(404, "unknown charge")
        if not p.startswith(API): return self._err(404, "Not Found")
        r = p[len(API):]
        if r == "customers/search.json":
            m = re.search(r"phone:(\S+)", q.get("query", "")); ph = (m.group(1) if m else "").replace(" ", "+")
            return self._send(200, {"customers": [c for c in S["customers"] if ph and c["phone"] == ph]})
        if r == "locations.json": return self._send(200, {"locations": S["locations"]})
        if r == "products.json":
            ps = [x for x in S["products"] if not q.get("product_type") or x["product_type"] == q["product_type"]]
            return self._send(200, {"products": ps[: int(q.get("limit", 50))]})
        m = re.match(r"products/(\d+)\.json$", r)
        if m: x = next((x for x in S["products"] if x["id"] == int(m.group(1))), None); return self._send(200, {"product": x}) if x else self._err(404, "Not Found")
        m = re.match(r"variants/(\d+)\.json$", r)
        if m: v = VARIANTS.get(int(m.group(1))); return self._send(200, {"variant": v}) if v else self._err(404, "Not Found")
        if r == "inventory_levels.json":
            ids = {int(x) for x in q.get("inventory_item_ids", "").split(",") if x}; locs = {int(x) for x in q.get("location_ids", "").split(",") if x}
            if not ids and not locs: return self._err(422, "inventory_item_ids or location_ids required")
            rows = [v for (i, l), v in S["inventory"].items() if (not ids or i in ids) and (not locs or l in locs)]
            return self._send(200, {"inventory_levels": rows})
        if r == "orders.json":
            os_ = list(S["orders"].values())
            if q.get("status", "open") != "any": os_ = [o for o in os_ if o.get("cancelled_at") is None]
            for k in ("created_at_min", "created_at_max"):
                if q.get(k): os_ = [o for o in os_ if (o["created_at"] >= q[k]) == (k == "created_at_min") or o["created_at"] == q[k]]
            if q.get("location_id"): os_ = [o for o in os_ if o["location_id"] == int(q["location_id"])]
            if q.get("financial_status"): os_ = [o for o in os_ if o["financial_status"] == q["financial_status"]]
            return self._send(200, {"orders": sorted(os_, key=lambda o: o["created_at"])[: int(q.get("limit", 50))]})
        m = re.match(r"orders/(\d+)\.json$", r)
        if m: o = S["orders"].get(int(m.group(1))); return self._send(200, {"order": o}) if o else self._err(404, "Not Found")
        self._err(404, "Not Found")

    # ------------------------------------------------------------ POST
    def do_POST(self):
        self._handle_post()

    def _handle_post(self):
        p = urlparse(self.path).path; b = self._body()
        if b is None: return self._err(400, "invalid JSON")
        if p == "/smt/prescriptions":
            rx = b.get("prescription")
            if not isinstance(rx, dict): return self._err(422, "prescription is required")
            phone = rx.get("phone")
            customer_id = rx.get("customer_id")
            customer = next((item for item in S["customers"] if item.get("phone") == phone and str(item.get("id")) == str(customer_id)), None)
            if customer is None: return self._err(422, "prescription customer does not match an existing Shopify customer")
            optometrist_id = rx.get("optometrist_id")
            store_code = rx.get("store_code")
            staff = next((item for item in S["staff"] if item.get("staff_id") == optometrist_id), None)
            if not staff or staff.get("role") != "optometrist" or staff.get("store_code") != store_code:
                return self._err(422, "a store-matched optometrist must confirm the prescription")
            rx_date = rx.get("date")
            valid_until = rx.get("valid_until")
            try:
                if not isinstance(rx_date, str) or not isinstance(valid_until, str):
                    raise ValueError
                if datetime.fromisoformat(rx_date).date() > datetime.fromisoformat(valid_until).date():
                    return self._err(422, "valid_until must not be before prescription date")
            except ValueError:
                return self._err(422, "prescription dates must be valid YYYY-MM-DD dates")
            for eye_name in ("od", "os"):
                eye = rx.get(eye_name)
                if not isinstance(eye, dict):
                    return self._err(422, "{} prescription is required".format(eye_name))
                try:
                    sph = float(eye["sph"])
                    cyl = float(eye["cyl"])
                    add = None if eye.get("add") is None else float(eye["add"])
                    axis = eye.get("axis")
                    axis = None if axis is None else int(axis)
                    if not (-30 <= sph <= 30 and -10 <= cyl <= 0):
                        raise ValueError
                    if add is not None and not (0 <= add <= 5):
                        raise ValueError
                    if axis is not None and (axis < 0 or axis > 180 or axis % 5):
                        raise ValueError
                    if cyl != 0 and axis is None:
                        raise ValueError
                except (KeyError, TypeError, ValueError, OverflowError):
                    return self._err(422, "{} has invalid sphere, cylinder, axis, or add.".format(eye_name.upper()))
            request_key = b.get("idempotency_key")
            request_hash = json.dumps(rx, sort_keys=True, separators=(",", ":"))
            with PRESCRIPTION_LOCK:
                if request_key and request_key in S["prescription_requests"]:
                    previous = S["prescription_requests"][request_key]
                    if previous["request_hash"] != request_hash:
                        return self._err(409, "idempotency key was already used for a different prescription")
                    return self._send(200, {"prescription": previous["prescription"]})
                created = dict(rx)
                created["rx_id"] = "RX{}".format(nid())
                created["customer_id"] = customer["id"]
                created["source"] = "in_store"
                S["rx"].append(created)
                if request_key:
                    S["prescription_requests"][request_key] = {
                        "request_hash": request_hash,
                        "prescription": created,
                    }
            return self._send(201, {"prescription": created})
        if p == "/terminal/charges":
            amt = int(b.get("amount_paise", 0))
            if amt <= 0: return self._err(422, "amount_paise must be > 0")
            key = b.get("order_ref")
            if FLAKE and key and key not in S["charges"] and random.random() < 0.3 and not S["charges"].get("_flaked_" + str(key)):
                S["charges"]["_flaked_" + str(key)] = True; return self._send(503, {"error": "terminal timeout, retry with the same order_ref"})
            cid = "chg_" + str(nid()); c = {"charge_id": cid, "status": "captured", "amount_paise": amt, "method": b.get("method", "upi"), "order_ref": key, "refunded_paise": 0, "captured_at": now()}
            S["charges"][cid] = c; return self._send(201, c)
        if p == "/terminal/refunds":
            c = S["charges"].get(b.get("charge_id")); amt = int(b.get("amount_paise", 0))
            if not c: return self._err(404, "unknown charge")
            if amt <= 0 or c["refunded_paise"] + amt > c["amount_paise"]: return self._send(422, {"error": "exceeds refundable amount", "refundable_paise": c["amount_paise"] - c["refunded_paise"]})
            if not b.get("reason"): return self._err(422, "reason required")
            c["refunded_paise"] += amt; return self._send(201, {"refund_id": "trf_" + str(nid()), "charge_id": c["charge_id"], "amount_paise": amt, "status": "processed"})
        if not p.startswith(API): return self._err(404, "Not Found")
        if FLAKE and random.random() < 0.1: return self._send(429, {"errors": "Exceeded 2 calls per second for api client. Reduce request rates to resume uninterrupted service."}, {"Retry-After": "1.0"})
        r = p[len(API):]
        if r == "customers.json":
            customer = b.get("customer")
            if not isinstance(customer, dict): return self._err(422, "customer is required")
            first = customer.get("first_name")
            last = customer.get("last_name", "")
            phone = customer.get("phone")
            if not isinstance(first, str) or not first.strip() or not isinstance(last, str):
                return self._err(422, "first_name and last_name must be text")
            if not isinstance(phone, str) or not re.fullmatch(r"\+91[6-9]\d{9}", phone):
                return self._err(422, "phone must be a valid +91 mobile number")
            with CUSTOMER_LOCK:
                matches = [item for item in S["customers"] if item.get("phone") == phone]
                if matches:
                    existing = matches[0]
                    if existing.get("first_name") != first or (existing.get("last_name") or "") != last:
                        return self._err(409, "phone already belongs to a different customer")
                    return self._send(200, {"customer": existing})
                created = {
                    "id": nid(), "first_name": first, "last_name": last, "phone": phone,
                    "tags": "", "email": None, "created_at": now(),
                }
                S["customers"].append(created)
            return self._send(201, {"customer": created})
        if r == "products.json":
            global VARIANTS
            product = b.get("product")
            if not isinstance(product, dict):
                return self._err(422, "product is required")
            variants = product.get("variants")
            if not isinstance(variants, list) or len(variants) != 1 or not isinstance(variants[0], dict):
                return self._err(422, "exactly one product variant is required")
            variant = variants[0]
            for existing in S["products"]:
                if any(str(item.get("sku", "")).casefold() == str(variant.get("sku", "")).casefold()
                       for item in existing.get("variants", [])):
                    return self._err(409, "SKU already exists")
            metadata = variant.get("eluno")
            if not isinstance(metadata, dict) or metadata.get("line_type") not in ("frame", "lens"):
                return self._err(422, "valid Eluno frame or lens metadata is required")
            try:
                if not isinstance(product.get("title"), str) or not product["title"].strip():
                    raise ValueError
                if not isinstance(variant.get("sku"), str) or not variant["sku"].strip():
                    raise ValueError
                if not (isinstance(variant.get("price"), str) and MONEY.fullmatch(variant["price"])):
                    raise ValueError
            except (KeyError, ValueError):
                return self._err(422, "product title, SKU, and decimal variant price are required")
            product_type = "Frame" if metadata["line_type"] == "frame" else "Lens"
            if product.get("product_type") != product_type:
                return self._err(422, "product_type does not match Eluno line_type")
            created_variant = dict(variant)
            created_variant["id"] = nid()
            created_variant["product_id"] = nid()
            created_variant["inventory_item_id"] = nid() if product_type == "Frame" else None
            created_variant["inventory_management"] = "shopify" if product_type == "Frame" else None
            created_product = {
                "id": created_variant["product_id"],
                "title": product["title"].strip(),
                "vendor": product.get("vendor", "Eluno"),
                "product_type": product_type,
                "status": "active",
                "tags": product.get("tags", ""),
                "variants": [created_variant],
            }
            S["products"].append(created_product)
            VARIANTS[created_variant["id"]] = created_variant
            if product_type == "Frame":
                for location in S["locations"]:
                    if location.get("active") and (location.get("eluno") or {}).get("kind") == "store":
                        item_id = created_variant["inventory_item_id"]
                        location_id = location["id"]
                        S["inventory"][(item_id, location_id)] = {
                            "inventory_item_id": item_id,
                            "location_id": location_id,
                            "available": 0,
                            "updated_at": now(),
                        }
            return self._send(201, {"product": created_product})
        if r == "inventory_levels/adjust.json" or r == "inventory_levels/set.json":
            k = (int(b.get("inventory_item_id", 0)), int(b.get("location_id", 0))); row = S["inventory"].get(k)
            if not row: return self._err(404, "inventory level not found for that item/location")
            adjustment_key = b.get("idempotency_key") if r.endswith("adjust.json") else None
            signature = {
                "inventory_item_id": k[0], "location_id": k[1],
                "available_adjustment": int(b.get("available_adjustment", 0)),
            }
            if adjustment_key is not None:
                if not isinstance(adjustment_key, str) or not adjustment_key.strip():
                    return self._err(422, "idempotency_key must be non-empty text")
                previous = S["inventory_adjustments"].get(adjustment_key)
                if previous:
                    if previous["request"] != signature:
                        return self._err(409, "idempotency key was already used for a different inventory receipt")
                    return self._send(200, {
                        "inventory_level": dict(row), "duplicate": True,
                    })
            row["available"] = row["available"] + int(b["available_adjustment"]) if r.endswith("adjust.json") else int(b["available"])
            row["updated_at"] = now()
            if adjustment_key is not None:
                S["inventory_adjustments"][adjustment_key] = {
                    "request": signature, "inventory_level": dict(row),
                }
            return self._send(200, {"inventory_level": row})
        if r == "inventory_levels/connect.json":
            item_id = int(b.get("inventory_item_id", 0))
            location_id = int(b.get("location_id", 0))
            variant = next((item for item in VARIANTS.values()
                            if item.get("inventory_item_id") == item_id
                            and item.get("inventory_management") == "shopify"), None)
            location = next((item for item in S["locations"]
                             if item.get("id") == location_id and item.get("active")), None)
            if variant is None or location is None:
                return self._err(422, "tracked inventory item and active location are required")
            row = S["inventory"].get((item_id, location_id))
            if row is None:
                row = {
                    "inventory_item_id": item_id, "location_id": location_id,
                    "available": 0, "updated_at": now(),
                }
                S["inventory"][(item_id, location_id)] = row
            return self._send(200, {"inventory_level": row})
        if r == "orders.json":
            o = b.get("order")
            if not isinstance(o, dict): return self._err(422, "order is required")
            if o.get("taxes_included") is not True: return self._err(422, {"taxes_included": ["must be true for this shop"]})
            lis = o.get("line_items") or []
            if not lis: return self._err(422, {"line_items": ["can't be blank"]})
            for li in lis:
                v = VARIANTS.get(li.get("variant_id"))
                if not v: return self._err(422, {"line_items": [f"variant_id {li.get('variant_id')} not found"]})
                for f in ("price", "total_discount"):
                    if f in li and not (isinstance(li[f], str) and MONEY.match(li[f])): return self._err(422, {f: ["must be a decimal string like \"104.98\""]})
                li.setdefault("price", v["price"]); li.setdefault("total_discount", "0.00"); li.setdefault("quantity", 1)
                li.update({"id": nid(), "product_id": v["product_id"], "sku": v["sku"], "variant_title": v["title"], "taxable": True, "requires_shipping": False, "fulfillment_status": None})
                li.setdefault("tax_lines", []); li.setdefault("properties", []); li.setdefault("discount_allocations", [])
            sub = sum(paise(li["price"]) * li["quantity"] for li in lis); disc = sum(paise(li["total_discount"]) for li in lis)
            tax = sum(paise(t["price"]) for li in lis for t in li["tax_lines"]); total = sub - disc
            txs = o.get("transactions") or []
            for t in txs:
                if not (isinstance(t.get("amount"), str) and MONEY.match(t["amount"])): return self._err(422, {"transactions": ["amount must be a decimal string"]})
                t.update({"id": nid(), "status": t.get("status", "success"), "kind": t.get("kind", "sale"), "created_at": now(), "parent_id": None, "currency": "INR"})
            paid = sum(paise(t["amount"]) for t in txs if t["kind"] == "sale" and t["status"] == "success")
            ib = o.get("inventory_behaviour", "bypass")
            if ib not in ("bypass", "decrement_ignoring_policy", "decrement_obeying_policy"): return self._err(422, {"inventory_behaviour": ["must be bypass, decrement_ignoring_policy or decrement_obeying_policy"]})
            if ib != "bypass":
                if not o.get("location_id"): return self._err(422, {"location_id": ["required when inventory_behaviour decrements stock"]})
                for li in lis:
                    v = VARIANTS[li["variant_id"]]
                    if v["inventory_item_id"] is None: continue
                    row = S["inventory"].get((v["inventory_item_id"], int(o["location_id"])))
                    if not row: return self._err(422, {"line_items": [f"{v['sku']} is not stocked at location {o['location_id']}"]})
                    if ib == "decrement_obeying_policy" and row["available"] < li["quantity"]: return self._err(422, {"line_items": [f"{v['sku']}: only {row['available']} available at location {o['location_id']}"]})
                    row["available"] -= li["quantity"]; row["updated_at"] = now()
            S["seq"]["order"] += 1; num = S["seq"]["order"]
            order = {"id": nid(), "order_number": num, "name": f"#{num}", "created_at": now(), "processed_at": now(),
                     "location_id": o.get("location_id"), "source_name": o.get("source_name", "api"), "customer": o.get("customer"),
                     "currency": "INR", "taxes_included": True, "financial_status": "paid" if paid >= total else ("partially_paid" if paid else "pending"),
                     "fulfillment_status": None, "subtotal_price": money(sub), "total_discounts": money(disc), "total_tax": money(tax),
                     "total_price": money(total), "total_line_items_price": money(sub), "tags": o.get("tags", ""), "note": o.get("note"),
                     "note_attributes": o.get("note_attributes", []), "discount_applications": o.get("discount_applications", []),
                     "line_items": lis, "transactions": txs, "refunds": [], "cancelled_at": None}
            S["orders"][order["id"]] = order; return self._send(201, {"order": order})
        m = re.match(r"orders/(\d+)/transactions\.json$", r)
        if m:
            o = S["orders"].get(int(m.group(1))); t = b.get("transaction")
            if not o: return self._err(404, "Not Found")
            if not (t and isinstance(t.get("amount"), str) and MONEY.match(t["amount"])): return self._err(422, {"amount": ["must be a decimal string"]})
            t.update({"id": nid(), "status": t.get("status", "success"), "created_at": now(), "currency": "INR"}); o["transactions"].append(t)
            paid = sum(paise(x["amount"]) for x in o["transactions"] if x["kind"] == "sale" and x["status"] == "success")
            o["financial_status"] = "paid" if paid >= paise(o["total_price"]) else "partially_paid"
            return self._send(201, {"transaction": t})
        m = re.match(r"orders/(\d+)/refunds(/calculate)?\.json$", r)
        if m:
            o = S["orders"].get(int(m.group(1))); rf = b.get("refund") or {}
            if not o: return self._err(404, "Not Found")
            li_by = {li["id"]: li for li in o["line_items"]}
            already = {}
            for prev in o["refunds"]:
                for rl in prev["refund_line_items"]: already[rl["line_item_id"]] = already.get(rl["line_item_id"], 0) + rl["quantity"]
            out, amt, taxamt = [], 0, 0
            for rl in rf.get("refund_line_items", []):
                li = li_by.get(rl.get("line_item_id"))
                if not li: return self._err(422, {"refund_line_items": [f"line_item_id {rl.get('line_item_id')} not on this order"]})
                qty = int(rl.get("quantity", 1))
                if already.get(li["id"], 0) + qty > li["quantity"]: return self._err(422, {"refund_line_items": [f"line {li['sku']} already refunded"]})
                rt = rl.get("restock_type", "no_restock")
                if rt not in ("return", "cancel", "no_restock"): return self._err(422, {"restock_type": ["must be return, cancel or no_restock"]})
                if rt != "no_restock" and not rl.get("location_id"): return self._err(422, {"location_id": ["required when restocking"]})
                gross = (paise(li["price"]) * li["quantity"] - paise(li["total_discount"])) * qty // li["quantity"]
                ltax = sum(paise(t["price"]) for t in li["tax_lines"]) * qty // li["quantity"]
                out.append({"line_item_id": li["id"], "quantity": qty, "restock_type": rt, "location_id": rl.get("location_id"), "subtotal": money(gross - ltax), "total_tax": money(ltax)})
                amt += gross; taxamt += ltax
            captured = sum(paise(t["amount"]) for t in o["transactions"] if t["kind"] == "sale" and t["status"] == "success")
            refunded = sum(paise(t["amount"]) for prev in o["refunds"] for t in prev["transactions"])
            if m.group(2):  # calculate
                return self._send(200, {"refund": {"refund_line_items": out, "transactions": [{"kind": "suggested_refund", "gateway": (o["transactions"] or [{}])[0].get("gateway"), "parent_id": (o["transactions"] or [{}])[0].get("id"), "amount": money(amt), "maximum_refundable": money(captured - refunded)}]}})
            txs = rf.get("transactions", [])
            for t in txs:
                if not (isinstance(t.get("amount"), str) and MONEY.match(t["amount"])): return self._err(422, {"transactions": ["amount must be a decimal string"]})
                if t.get("kind") != "refund": return self._err(422, {"transactions": ["kind must be refund"]})
            rsum = sum(paise(t["amount"]) for t in txs)
            if rsum > captured - refunded: return self._err(422, {"transactions": [f"refund {money(rsum)} exceeds maximum refundable {money(captured - refunded)}"]})
            for rl in out:
                if rl["restock_type"] != "no_restock":
                    v = VARIANTS[li_by[rl["line_item_id"]]["variant_id"]]
                    if v["inventory_item_id"] is None: return self._err(422, {"refund_line_items": [f"{v['sku']} is not inventory-tracked; use no_restock"]})
                    row = S["inventory"].get((v["inventory_item_id"], int(rl["location_id"])))
                    if not row: return self._err(422, {"location_id": ["item not stocked at that location"]})
                    row["available"] += rl["quantity"]; row["updated_at"] = now()
            for t in txs: t.update({"id": nid(), "status": "success", "created_at": now(), "currency": "INR"})
            refund = {"id": nid(), "order_id": o["id"], "created_at": now(), "note": rf.get("note"), "refund_line_items": out, "transactions": txs, "note_attributes": rf.get("note_attributes", [])}
            o["refunds"].append(refund)
            o["financial_status"] = "refunded" if refunded + rsum >= captured and captured else "partially_refunded"
            return self._send(201, {"refund": refund})
        self._err(404, "Not Found")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=8787); ap.add_argument("--no-flake", action="store_true"); ap.add_argument("--state-db")
    a = ap.parse_args(); FLAKE = not a.no_flake
    if a.state_db:
        STATE_DB = os.path.abspath(a.state_db)
        os.makedirs(os.path.dirname(STATE_DB), exist_ok=True)
        _load_state(STATE_DB)
    print(f"mock Shopify Admin + SMT + terminal on http://localhost:{a.port}  (flaky 429/503: {'off' if a.no_flake else 'on'})")
    HTTPServer(("127.0.0.1", a.port), H).serve_forever()
