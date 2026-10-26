#!/usr/bin/env python3
"""Deterministic seed-data generator for the Eluno POS exercise.

Run:  python3 generate_seed.py          # writes ./data/*.json, identical on every run

Shapes:
  Shopify Admin REST shape  -> locations, customers, products (frames + lenses), inventory_levels, orders_sample
  SMT (store tool) shape    -> prescriptions, staff
  POS-owned                 -> tax_rates, offers, till_sessions_sample
Money inside this script is integer paise. Shopify money fields are decimal strings ("104.98"),
prices are MRP inclusive of GST (taxes_included = true).
"""
import json, os, random, tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

random.seed(42)
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data")
os.makedirs(OUT, exist_ok=True)

def dump(name, obj):
    target = os.path.join(OUT, name)
    fd, temporary = tempfile.mkstemp(prefix=name + ".", suffix=".tmp", dir=OUT)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def money(paise):            # paise -> Shopify decimal string
    sign = "-" if paise < 0 else ""; paise = abs(paise)
    return f"{sign}{paise // 100}.{paise % 100:02d}"

def q(x):                    # snap to the quarter-dioptre grid
    return round(round(float(x) * 4) / 4, 2)

def ts(d, hh=12, mm=30):
    return f"{d}T{hh:02d}:{mm:02d}:00+05:30"

# ------------------------------------------------------------------ locations (Shopify) + Eluno store info
STORES = [("MM", "Mantri Mall"), ("HSR", "HSR Layout"), ("BR", "Bel Road"), ("KMG", "Koramangala"),
          ("JN", "Jayanagar"), ("IN", "Indiranagar"), ("VC", "Vega City")]
locations = []
for i, (code, name) in enumerate(STORES):
    locations.append({
        "id": 70000001 + i, "name": f"Eluno {name}", "active": True, "legacy": False,
        "address1": f"{name}, Bengaluru", "city": "Bengaluru", "province_code": "KA", "country_code": "IN", "zip": "5600xx",
        "eluno": {"store_code": code, "invoice_prefix": f"ELU/{code}/FY26/", "state_code": "29", "kind": "store",
                  "opened_on": (date(2025, 1, 1) + timedelta(days=45 * i)).isoformat()},
    })
locations.append({"id": 70000008, "name": "Eluno HQ & Lab", "active": True, "legacy": False, "address1": "HQ, Bengaluru",
                  "city": "Bengaluru", "province_code": "KA", "country_code": "IN", "zip": "5600xx",
                  "eluno": {"store_code": "HQ", "invoice_prefix": None, "state_code": "29", "kind": "warehouse", "opened_on": "2024-06-01"}})
dump("locations.json", locations)
loc_by_code = {l["eluno"]["store_code"]: l for l in locations}
stores = [l for l in locations if l["eluno"]["kind"] == "store"]

# ------------------------------------------------------------------ staff (SMT)
first = ["Aarav","Diya","Rohan","Meera","Kabir","Ananya","Vikram","Sneha","Arjun","Pooja","Nikhil","Riya","Dev","Tara"]
staff, sid = [], 100
for s in stores:
    for r in ("sales", "sales", "optometrist", "store_manager"):
        sid += 1
        staff.append({"staff_id": f"S{sid}", "name": f"{random.choice(first)} {random.choice(['R.','S.','K.','M.','P.'])}",
                      "role": r, "store_code": s["eluno"]["store_code"], "location_id": s["id"],
                      "badges": (["progressive_expert"] if r == "optometrist" or random.random() < 0.25 else []),
                      "can_override_discount": r == "store_manager"})
dump("staff.json", staff)
optometrists = [x for x in staff if x["role"] == "optometrist"]

# ------------------------------------------------------------------ customers (Shopify)
cnames = ["Priya Nair","Rahul Menon","Anjali Rao","Suresh Kumar","Divya Iyer","Karthik Reddy","Neha Gupta","Amit Shah",
 "Lakshmi Venkat","Farhan Ali","Shreya Joshi","Manish Agarwal","Kavya Hegde","Rajesh Pillai","Isha Bhat","Varun Desai",
 "Sunita Patil","Deepak Verma","Nandini Krishnan","Imran Khan","Ritu Malhotra","Sanjay Rao","Bhavana Shetty","Gaurav Mehta",
 "Pallavi Kulkarni","Harish Babu","Megha Singh","Vivek Nambiar","Asha Thomas","Rohit Jain"]
cohort_pool = [[], [], [], ["campaign:progressive_oct26"], ["campaign:festive_frames"], ["family"], ["campaign:progressive_oct26", "family"]]
customers = []
for i, n in enumerate(cnames):
    ltv = random.choice([0, 0, 249900, 389900, 549900, 729900, 1199900, 1849900]) if i > 4 else 0
    tags = random.choice(cohort_pool) + (["priority"] if ltv > 500000 else [])
    if i == 5 and "campaign:progressive_oct26" not in tags:   # the worked-example customer is on the campaign
        tags.insert(0, "campaign:progressive_oct26")
    fn, ln = n.split(" ", 1)
    customers.append({
        "id": 80001000 + i, "first_name": fn, "last_name": ln, "phone": f"+9190000{i:05d}", "email": None,
        "tags": ",".join(tags), "total_spent": money(ltv), "orders_count": 0 if ltv == 0 else random.randint(1, 4),
        "accepts_marketing": random.random() < 0.8, "state": "enabled",
        "created_at": ts((date(2025, 2, 1) + timedelta(days=random.randint(0, 600))).isoformat(), 11, 0),
        "note": None, "default_address": {"city": "Bengaluru", "province_code": "KA", "country_code": "IN"},
        "eluno": {"dob": (date(1960, 1, 1) + timedelta(days=random.randint(0, 16000))).isoformat(),
                  "home_store": random.choice(stores)["eluno"]["store_code"]},
    })
dump("customers.json", customers)
cust_by_id = {c["id"]: c for c in customers}

# ------------------------------------------------------------------ prescriptions (SMT)
rx, rid = [], 5000
for c in customers:
    n_rx = random.choice([0, 1, 1, 1, 2, 2, 3])
    base_sph = q(random.uniform(-6.0, 3.0))
    age = 2026 - int(c["eluno"]["dob"][:4])
    for k in range(n_rx):
        rid += 1
        d = date(2024, 6, 1) + timedelta(days=random.randint(0, 850) + k * 120)
        sph = q(base_sph + random.choice([0, 0, -0.25, -0.5, 0.25]) * k)
        cyl = q(random.choice([0, 0, -0.5, -0.75, -1.25, -2.0]))
        axis = random.choice(range(0, 181, 5)) if cyl != 0 else None
        add = q(random.choice([0.75, 1.0, 1.5, 2.0, 2.5])) if age >= 42 and random.random() < 0.7 else None
        ext = random.random() < 0.25
        rx.append({"rx_id": f"RX{rid}", "customer_id": c["id"], "phone": c["phone"], "date": d.isoformat(),
                   "source": "external" if ext else "in_store",
                   "optometrist_id": None if ext else random.choice(optometrists)["staff_id"],
                   "od": {"sph": sph, "cyl": cyl, "axis": axis, "add": add},
                   "os": {"sph": q(sph + random.choice([0, 0.25, -0.25])), "cyl": cyl, "axis": axis, "add": add},
                   "pd": {"binocular": random.choice([58, 60, 62, 63, 64, 66]), "mono_r": None, "mono_l": None},
                   "valid_until": (d + timedelta(days=365)).isoformat(),
                   "notes": random.choice([None, None, "first-time progressive", "prefers lighter lens", "external Rx photo on file"])})
rx.append({"rx_id": "RX5999", "customer_id": 80001003, "phone": "+919000000003", "date": "2026-09-30", "source": "external",
           "optometrist_id": None, "od": {"sph": -2.25, "cyl": -0.75, "axis": 87, "add": None},
           "os": {"sph": -2.0, "cyl": -0.5, "axis": 92, "add": None}, "pd": {"binocular": 63, "mono_r": None, "mono_l": None},
           "valid_until": "2027-09-30", "notes": "Transcribed from a paper Rx. Axis values as written."})

# ------------------------------------------------------------------ products (Shopify): frames + lenses
products, pid, vid, iid = [], 90000000, 91000000, 92000000
brands = [("Eluno Atelier", 749900, 1299900), ("Eluno Core", 349900, 649900), ("Eluno Lite", 199900, 299900)]
shapes = ["Round","Square","Cat-eye","Aviator","Rectangle","Browline"]
materials = ["acetate","titanium","stainless steel","TR90"]
colours = ["Black","Tortoise","Crystal","Navy","Gunmetal","Olive"]
frame_variants = []
fid = 0
for b, lo, hi in brands:
    for _ in range(13 if b != "Eluno Lite" else 14):
        fid += 1; pid += 1; vid += 1; iid += 1
        cat = "sunglasses" if random.random() < 0.15 else "optical"
        v = {"id": vid, "product_id": pid, "sku": f"FR-{fid:04d}", "title": random.choice(colours),
             "option1": None, "price": money(random.randrange(lo, hi, 50000) + 99900),
             "inventory_item_id": iid, "inventory_management": "shopify", "inventory_policy": "deny",
             "requires_shipping": False, "taxable": True,
             "eluno": {"line_type": "frame", "hsn": "90049000" if cat == "sunglasses" else "90031900", "category": cat,
                       "material": random.choice(materials),
                       "size": f"{random.choice([48,50,52,54])}-{random.choice([17,18,19,20])}-{random.choice([140,145])}",
                       "rx_able": cat == "optical" or random.random() < 0.5}}
        v["option1"] = v["title"]
        p = {"id": pid, "title": f"{b} {random.choice(shapes)} {fid:02d}", "vendor": b, "product_type": "Frame",
             "status": "active", "tags": f"frame,{cat}", "variants": [v]}
        products.append(p); frame_variants.append(v)
products[-1]["status"] = "archived"   # discontinued, still in history
matrix = {"single_vision": [(1.50, 99900), (1.56, 149900), (1.60, 249900), (1.67, 399900), (1.74, 699900)],
          "bifocal": [(1.50, 199900), (1.56, 249900), (1.60, 349900)],
          "progressive": [(1.50, 499900), (1.56, 599900), (1.60, 849900), (1.67, 1199900), (1.74, 1799900)]}
coatings = [("standard", 0), ("anti_reflective", 50000), ("blue_cut", 100000), ("photochromic", 250000)]
limits = {1.50: (-4.0, 4.0, 2.0), 1.56: (-4.0, 4.0, 2.0), 1.60: (-8.0, 6.0, 4.0), 1.67: (-10.0, 8.0, 4.0), 1.74: (-14.0, 10.0, 6.0)}
lens_variants, lid = [], 0
for t, idxs in matrix.items():
    for idx, base in idxs:
        pid += 1
        p = {"id": pid, "title": f"{t.replace('_', ' ').title()} {idx:.2f}", "vendor": "Eluno Lab", "product_type": "Lens",
             "status": "active", "tags": f"lens,{t}", "variants": []}
        for cname, cadd in coatings:
            lid += 1; vid += 1
            lo, hi, cylmax = limits[idx]
            v = {"id": vid, "product_id": pid, "sku": f"LN-{lid:04d}", "title": cname, "option1": cname,
                 "price": money(base + cadd), "inventory_item_id": None, "inventory_management": None,
                 "requires_shipping": False, "taxable": True,
                 "eluno": {"line_type": "lens", "hsn": "90015000", "lens_type": t, "index": idx, "coating": cname,
                           "rx_limits": {"sph_min": lo, "sph_max": hi, "cyl_abs_max": cylmax,
                                         "add_required": t in ("progressive", "bifocal"), "add_min": 0.75, "add_max": 3.0},
                           "lab_tat_days": 3 if idx <= 1.56 else 6}}
            p["variants"].append(v); lens_variants.append(v)
        products.append(p)
dump("products.json", products)
var_by_sku = {v["sku"]: v for v in frame_variants + lens_variants}
var_by_id = {v["id"]: v for v in frame_variants + lens_variants}
SUN_SKU = next(v["sku"] for v in frame_variants if v["eluno"]["category"] == "sunglasses")

# ------------------------------------------------------------------ inventory levels (Shopify): frames at stores + HQ
inventory = []
for l in locations:
    for v in frame_variants:
        if l["eluno"]["kind"] == "warehouse":
            avail = random.choice([5, 8, 12, 20])
        else:
            avail = random.choice([0, 1, 1, 2, 3, 5])
        inventory.append({"inventory_item_id": v["inventory_item_id"], "location_id": l["id"], "available": avail,
                          "updated_at": ts("2026-10-20", 23, 0)})
dump("inventory_levels.json", inventory)

# ------------------------------------------------------------------ tax rates (POS-owned)
tax = {
    "about": "HSN -> GST rate in basis points, with effective dates. Rates in this kit are for the exercise. Prices are inclusive of GST; intra-state supply, so CGST = SGST = rate/2.",
    "rates": [
        {"hsn": "90031900", "description": "Spectacle frames", "rate_bps": 1800, "effective_from": "2025-09-22"},
        {"hsn": "90015000", "description": "Spectacle lenses (ophthalmic)", "rate_bps": 500, "effective_from": "2025-09-22"},
        {"hsn": "90049000", "description": "Sunglasses (non-corrective)", "rate_bps": 1800, "effective_from": "2025-09-22"},
        {"hsn": "90041000", "description": "Corrective spectacles sold as one unit", "rate_bps": 500, "effective_from": "2025-09-22"},
        {"hsn": "998599", "description": "Services: eye test / fitting", "rate_bps": 1800, "effective_from": "2025-09-22"},
        {"hsn": "90031900", "description": "Spectacle frames", "rate_bps": 1800, "effective_from": "2017-07-01", "effective_to": "2025-09-21"},
        {"hsn": "90015000", "description": "Spectacle lenses (ophthalmic)", "rate_bps": 1200, "effective_from": "2017-07-01", "effective_to": "2025-09-21"},
    ],
}
dump("tax_rates.json", tax)
def rate_for(hsn, on):
    return [r for r in tax["rates"] if r["hsn"] == hsn and r["effective_from"] <= on <= r.get("effective_to", "9999-12-31")][0]["rate_bps"]

# ------------------------------------------------------------------ offers (POS-owned)
offers = [
    {"offer_id":"FESTIVE10","name":"Festive 10% on frames","active":True,"valid_from":"2026-10-01","valid_to":"2026-11-15","stackable":False,"priority":10,
     "rule":{"applies_to":"frame","discount":{"type":"percent","value_bps":1000},"max_discount_paise":200000}},
    {"offer_id":"SECONDPAIR50","name":"50% off second pair lenses","active":True,"valid_from":"2026-01-01","valid_to":"2026-12-31","stackable":True,"priority":20,
     "rule":{"applies_to":"lens","condition":{"min_pairs_in_cart":2},"discount":{"type":"percent","value_bps":5000,"on":"lowest_priced_lens_line"}}},
    {"offer_id":"PRIORITY500","name":"Priority customer ₹500 off","active":True,"valid_from":"2026-01-01","valid_to":"2026-12-31","stackable":True,"priority":30,
     "rule":{"applies_to":"cart","condition":{"customer_tag":"priority"},"discount":{"type":"flat","value_paise":50000}}},
    {"offer_id":"PGX_CAMPAIGN15","name":"Progressive campaign 15% on progressive lenses","active":True,"valid_from":"2026-10-01","valid_to":"2026-10-31","stackable":False,"priority":15,
     "rule":{"applies_to":"lens","condition":{"lens_type":"progressive","customer_tag":"campaign:progressive_oct26"},"discount":{"type":"percent","value_bps":1500}}},
    {"offer_id":"FREE_BLUECUT_PGX","name":"Free blue-cut upgrade on progressive 1.60+","active":True,"valid_from":"2026-09-01","valid_to":"2026-12-31","stackable":True,"priority":25,
     "rule":{"applies_to":"lens","condition":{"lens_type":"progressive","index_min":1.60,"coating":"blue_cut"},"discount":{"type":"flat","value_paise":100000}}},
    {"offer_id":"BUNDLE300","name":"₹300 off when frame and lens billed together","active":True,"valid_from":"2026-01-01","valid_to":"2026-12-31","stackable":True,"priority":40,
     "rule":{"applies_to":"pair","condition":{"has_frame_and_lens":True},"discount":{"type":"flat","value_paise":30000}}},
    {"offer_id":"MANAGER_OVERRIDE","name":"Manager discretionary discount","active":True,"valid_from":"2026-01-01","valid_to":"2026-12-31","stackable":False,"priority":99,
     "rule":{"applies_to":"cart","requires":{"role":"store_manager","reason_code":True},"discount":{"type":"percent","value_bps_max":1000}}},
    {"offer_id":"MONSOON20","name":"Monsoon 20% (expired)","active":False,"valid_from":"2026-06-01","valid_to":"2026-08-31","stackable":False,"priority":10,
     "rule":{"applies_to":"frame","discount":{"type":"percent","value_bps":2000}}},
]
dump("offers.json", offers)

# ------------------------------------------------------------------ sample orders (Shopify shape, tax to the paisa)
def split_tax(gross, bps):
    g = Decimal(gross); taxable = (g * 10000 / (10000 + bps)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    t = g - taxable; c = (t / 2).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(taxable), int(c), int(t - c)

rx_by_cust = {}
for r in rx: rx_by_cust.setdefault(r["customer_id"], []).append(r)
def latest_rx_for(cust_id, lens_v):
    lst = sorted(rx_by_cust.get(cust_id, []), key=lambda r: r["date"])
    if not lst:
        global rid; rid += 1
        r = {"rx_id": f"RX{rid}", "customer_id": cust_id, "phone": cust_by_id[cust_id]["phone"], "date": "2026-09-20", "source": "in_store",
             "optometrist_id": optometrists[0]["staff_id"], "od": {"sph": -1.5, "cyl": -0.5, "axis": 90, "add": None},
             "os": {"sph": -1.25, "cyl": -0.5, "axis": 85, "add": None}, "pd": {"binocular": 62, "mono_r": None, "mono_l": None},
             "valid_until": "2027-09-20", "notes": None}
        rx.append(r); rx_by_cust.setdefault(cust_id, []).append(r); lst = [r]
    r = lst[-1]
    if lens_v["eluno"]["rx_limits"]["add_required"] and r["od"]["add"] is None:
        r["od"]["add"] = r["os"]["add"] = 1.5
    return r

orders, order_number, oid, liid, txid, rfid = [], 1000, 95000000, 96000000, 97000000, 98000000
inv_seq = {s["eluno"]["store_code"]: 0 for s in stores}
pos_orders = []   # (order, store_code, channel, cash?) for till sessions

def mk_order(store_code, cust_id, when, lines, offers_applied, channel="walk_in", method="upi", status="paid",
             refund=None, parent_invoice=None, note=None):
    global order_number, oid, liid, txid, rfid
    loc = loc_by_code[store_code]; order_number += 1; oid += 1; inv_seq[store_code] += 1
    cust = cust_by_id[cust_id]
    disc_apps, line_items = [], []
    for ln in lines:
        if ln["sku"] == "SUN": ln["sku"] = SUN_SKU
        v = var_by_sku[ln["sku"]]; mrp = int(Decimal(v["price"]) * 100); qty = ln.get("qty", 1)
        props = [{"name": "line_type", "value": v["eluno"]["line_type"]}, {"name": "hsn", "value": v["eluno"]["hsn"]}, {"name": "pair_ref", "value": str(ln.get("pair_ref", 1))}]
        if v["eluno"]["line_type"] == "lens":
            r = latest_rx_for(cust_id, v)
            props += [{"name": "rx_id", "value": r["rx_id"]},
                      {"name": "rx", "value": json.dumps({"od": r["od"], "os": r["os"], "pd": r["pd"]["binocular"]}, separators=(",", ":"))},
                      {"name": "lens_config", "value": f"{v['eluno']['lens_type']}/{v['eluno']['index']}/{v['eluno']['coating']}"}]
        disc = 0
        if "discount_bps" in ln: disc = int((Decimal(mrp) * ln["discount_bps"] / 10000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        elif "discount_paise" in ln: disc = ln["discount_paise"]
        gross = mrp * qty - disc
        bps = rate_for(v["eluno"]["hsn"], when)
        taxable, cgst, sgst = split_tax(gross, bps)
        liid += 1
        allocs = []
        if disc:
            disc_apps.append({"type": "manual", "title": ln.get("offer", offers_applied[0] if offers_applied else "DISCOUNT"),
                              "value": money(disc), "value_type": "fixed_amount", "allocation_method": "each", "target_selection": "explicit", "target_type": "line_item"})
            allocs.append({"amount": money(disc), "discount_application_index": len(disc_apps) - 1})
        line_items.append({"id": liid, "variant_id": v["id"], "product_id": v["product_id"], "sku": v["sku"],
                           "title": next(p["title"] for p in products if p["id"] == v["product_id"]), "variant_title": v["title"],
                           "quantity": qty, "price": v["price"], "total_discount": money(disc), "taxable": True,
                           "requires_shipping": False, "fulfillment_status": None,
                           "tax_lines": [{"title": "CGST", "rate": bps / 20000, "price": money(cgst)}, {"title": "SGST", "rate": bps / 20000, "price": money(sgst)}],
                           "discount_allocations": allocs, "properties": props,
                           "_paise": {"gross": gross, "taxable": taxable, "cgst": cgst, "sgst": sgst, "rate_bps": bps}})
    subtotal = sum(int(Decimal(li["price"]) * 100) * li["quantity"] for li in line_items)
    total_disc = sum(int(Decimal(li["total_discount"]) * 100) for li in line_items)
    total_tax = sum(li["_paise"]["cgst"] + li["_paise"]["sgst"] for li in line_items)
    total = subtotal - total_disc
    txid += 1
    txs = [{"id": txid, "kind": "sale", "status": "success", "gateway": {"upi": "razorpay_terminal", "card": "razorpay_terminal", "cash": "cash"}[method],
            "amount": money(total), "currency": "INR", "authorization": f"term_{txid}", "created_at": ts(when, 12, 31), "parent_id": None}] if total > 0 else []
    o = {"id": oid, "order_number": order_number, "name": f"#{order_number}", "created_at": ts(when), "processed_at": ts(when),
         "location_id": loc["id"], "source_name": "eluno-pos", "customer": {"id": cust["id"], "first_name": cust["first_name"], "last_name": cust["last_name"], "phone": cust["phone"]},
         "currency": "INR", "taxes_included": True, "financial_status": "refunded" if status == "refunded" else "paid",
         "fulfillment_status": None, "subtotal_price": money(subtotal), "total_discounts": money(total_disc),
         "total_tax": money(total_tax), "total_price": money(total), "total_line_items_price": money(subtotal),
         "tags": ",".join(["pos", channel] + ([f"parent:{parent_invoice}"] if parent_invoice else [])),
         "note": note,
         "note_attributes": [{"name": "invoice_no", "value": f"{loc['eluno']['invoice_prefix']}{inv_seq[store_code]:05d}"},
                             {"name": "store_code", "value": store_code}, {"name": "channel", "value": channel},
                             {"name": "staff_id", "value": next(x for x in staff if x["store_code"] == store_code and x["role"] == "sales")["staff_id"]},
                             {"name": "payment_method", "value": method},
                             {"name": "offer_codes", "value": ",".join(offers_applied)}] + ([{"name": "parent_invoice_no", "value": parent_invoice}] if parent_invoice else []),
         "discount_applications": disc_apps, "line_items": line_items, "transactions": txs, "refunds": []}
    if refund:
        rfid += 1; txid += 1
        rl, ramt, rtax = [], 0, 0
        for li in line_items:
            if li["sku"] in refund["skus"]:
                is_frame = li["properties"][0]["value"] == "frame"
                rl.append({"line_item_id": li["id"], "quantity": li["quantity"], "restock_type": "return" if is_frame else "no_restock",
                           "location_id": loc["id"] if is_frame else None, "subtotal": money(li["_paise"]["taxable"]), "total_tax": money(li["_paise"]["cgst"] + li["_paise"]["sgst"])})
                ramt += li["_paise"]["gross"]; rtax += li["_paise"]["cgst"] + li["_paise"]["sgst"]
        o["refunds"].append({"id": rfid, "order_id": oid, "created_at": refund["at"], "note": refund["note"],
                             "refund_line_items": rl,
                             "transactions": [{"id": txid, "kind": "refund", "status": "success", "gateway": txs[0]["gateway"], "amount": money(ramt), "parent_id": txs[0]["id"], "created_at": refund["at"]}],
                             "note_attributes": [{"name": "credit_note_no", "value": f"ELU/{store_code}/FY26/CN/{inv_seq[store_code]:05d}"}, {"name": "reason_code", "value": refund["reason"]}]})
        o["financial_status"] = "refunded" if ramt == total else "partially_refunded"
    for li in line_items: del li["_paise"]
    orders.append(o); pos_orders.append((o, store_code, method))
    return o

mk_order("HSR", 80001005, "2026-10-02", [{"sku": "FR-0003"}, {"sku": "LN-0043", "discount_bps": 1500, "offer": "PGX_CAMPAIGN15"}], ["PGX_CAMPAIGN15"], channel="whatsapp_booked")
mk_order("IN", 80001007, "2026-10-03", [{"sku": "FR-0015", "discount_bps": 1000, "offer": "FESTIVE10"}, {"sku": "LN-0002"}], ["FESTIVE10"], method="card")
mk_order("KMG", 80001010, "2026-10-05", [{"sku": "FR-0020"}, {"sku": "LN-0006"}, {"sku": "FR-0031", "pair_ref": 2}, {"sku": "LN-0001", "pair_ref": 2, "discount_bps": 5000, "offer": "SECONDPAIR50"}],
         ["SECONDPAIR50"], note="Two pairs, same Rx. Second pair lens at 50%.")
mk_order("JN", 80001012, "2026-10-06", [{"sku": "SUN"}], [], method="cash", note="Sunglasses only, no Rx.")
mk_order("MM", 80001015, "2026-10-07", [{"sku": "FR-0008", "discount_paise": 50000, "offer": "PRIORITY500"}, {"sku": "LN-0033"}], ["PRIORITY500"], note="Priority ₹500 applied on the frame line.")
mk_order("BR", 80001018, "2026-10-08", [{"sku": "LN-0003"}], [], method="cash", note="Lens only: customer's own frame, measured in store.")
mk_order("HSR", 80001020, "2026-10-09", [{"sku": "FR-0011"}, {"sku": "LN-0012"}], [], status="refunded",
         refund={"at": ts("2026-10-12", 16, 5), "note": "Full refund within window. Frame restocked at HSR; lens scrapped.", "reason": "CHANGED_MIND", "skus": ["FR-0011", "LN-0012"]})
o8 = mk_order("IN", 80001021, "2026-10-10", [{"sku": "FR-0025"}, {"sku": "LN-0043"}], [], channel="online_pickup", note="Progressive, first-time wearer. Non-adaptation reported later; see remake.")
mk_order("IN", 80001021, "2026-10-15", [{"sku": "LN-0043", "discount_paise": int(Decimal(var_by_sku["LN-0043"]["price"]) * 100), "offer": "REMAKE_GOODWILL"}], ["REMAKE_GOODWILL"],
         parent_invoice=o8["note_attributes"][0]["value"], note="Remake at zero charge (progressive non-adaptation). Frame not re-billed, not re-reserved. Lab cost ownership open.")
mk_order("VC", 80001025, "2026-10-18", [{"sku": "FR-0036", "discount_paise": 30000, "offer": "BUNDLE300"}, {"sku": "LN-0009"}], ["BUNDLE300"], method="cash")
# a store-day with several cash movements for the till sample: JN on 2026-10-06 already has a cash sale
mk_order("JN", 80001002, "2026-10-06", [{"sku": "FR-0022"}], [], method="cash", note="Walk-in, frame only (plano demo lens).")
mk_order("JN", 80001004, "2026-10-06", [{"sku": "FR-0019"}, {"sku": "LN-0005"}], [], method="upi")
dump("orders_sample.json", orders)
dump("prescriptions.json", rx)

# ------------------------------------------------------------------ till sessions (POS-owned)
def till(store_code, day, opened_by, closed_by, float_paise, extra_moves, counted_delta):
    loc = loc_by_code[store_code]
    moves = []
    for o, sc, method in pos_orders:
        if sc == store_code and o["created_at"].startswith(day) and method == "cash":
            moves.append({"type": "cash_sale", "amount_paise": int(Decimal(o["total_price"]) * 100), "ref": o["note_attributes"][0]["value"], "at": o["created_at"], "reason": None})
            for rf in o["refunds"]:
                if rf["created_at"].startswith(day):
                    moves.append({"type": "cash_refund", "amount_paise": -int(Decimal(rf["transactions"][0]["amount"]) * 100), "ref": rf["note_attributes"][0]["value"], "at": rf["created_at"], "reason": rf["note_attributes"][1]["value"]})
    moves += extra_moves
    expected = float_paise + sum(m["amount_paise"] for m in moves)
    return {"session_id": f"TILL-{store_code}-{day}", "location_id": loc["id"], "store_code": store_code,
            "opened_at": ts(day, 10, 2), "opened_by": opened_by, "opening_float_paise": float_paise, "movements": moves,
            "closed_at": ts(day, 21, 40), "closed_by": closed_by, "expected_cash_paise": expected,
            "counted_cash_paise": expected + counted_delta, "variance_paise": counted_delta,
            "variance_note": None if counted_delta == 0 else "Short; manager to review."}
jn_mgr = next(x["staff_id"] for x in staff if x["store_code"] == "JN" and x["role"] == "store_manager")
vc_mgr = next(x["staff_id"] for x in staff if x["store_code"] == "VC" and x["role"] == "store_manager")
tills = [
    till("JN", "2026-10-06", jn_mgr, jn_mgr, 300000,
         [{"type": "paid_out", "amount_paise": -12000, "ref": "PO-JN-0017", "at": ts("2026-10-06", 13, 10), "reason": "courier"},
          {"type": "paid_out", "amount_paise": -8000, "ref": "PO-JN-0018", "at": ts("2026-10-06", 17, 45), "reason": "tea/snacks"},
          {"type": "paid_in", "amount_paise": 100000, "ref": "PI-JN-0003", "at": ts("2026-10-06", 15, 0), "reason": "change from HQ"}], -25000),
    till("VC", "2026-10-18", vc_mgr, vc_mgr, 200000, [], 0),
]
dump("till_sessions_sample.json", tills)
print("wrote", len(os.listdir(OUT)), "files to", OUT)
