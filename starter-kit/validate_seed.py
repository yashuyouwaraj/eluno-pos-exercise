#!/usr/bin/env python3
"""Integrity and money checks for the seed data. Run: python3 validate_seed.py  (exit 0 = all good)."""
import json, os, sys
from decimal import Decimal, ROUND_HALF_UP

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
def load_json(name):
    with open(os.path.join(D, name), encoding="utf-8") as source:
        return json.load(source)
L = load_json
P = lambda s: int(Decimal(s) * 100)          # Shopify money string -> paise
errs = []
def check(cond, msg):
    if not cond: errs.append(msg)

locations, staff, customers, rx = L("locations.json"), L("staff.json"), L("customers.json"), L("prescriptions.json")
products, inv, tax, offers, orders, tills = L("products.json"), L("inventory_levels.json"), L("tax_rates.json"), L("offers.json"), L("orders_sample.json"), L("till_sessions_sample.json")

loc_ids = {l["id"] for l in locations}
store_codes = {l["eluno"]["store_code"] for l in locations if l["eluno"]["kind"] == "store"}
cust_ids = {c["id"] for c in customers}
staff_ids = {s["staff_id"] for s in staff}
variants = {v["id"]: v for p in products for v in p["variants"]}
inv_items = {v["inventory_item_id"] for v in variants.values() if v["inventory_item_id"]}
hsns = {r["hsn"] for r in tax["rates"]}
rx_by_id = {r["rx_id"]: r for r in rx}

for s in staff: check(s["location_id"] in loc_ids and s["store_code"] in store_codes, f"staff {s['staff_id']} bad location")
for c in customers:
    check(c["phone"].startswith("+9190000"), f"customer {c['id']} phone not in synthetic range")
    check(("priority" in c["tags"].split(",")) == (P(c["total_spent"]) > 500000), f"customer {c['id']} priority tag inconsistent with total_spent")
for r in rx:
    check(r["customer_id"] in cust_ids, f"rx {r['rx_id']} orphan customer")
    check(r["optometrist_id"] is None or r["optometrist_id"] in staff_ids, f"rx {r['rx_id']} bad optometrist")
    for eye in ("od", "os"):
        e = r[eye]
        for k in ("sph", "cyl", "add"):
            check(e[k] is None or Decimal(str(e[k])) % Decimal("0.25") == 0, f"rx {r['rx_id']} {eye}.{k}={e[k]} not on 0.25 step")
        check(e["cyl"] == 0 or e["axis"] is not None, f"rx {r['rx_id']} {eye} cyl without axis")
        if e["axis"] is not None and r["source"] == "in_store":
            check(e["axis"] % 5 == 0 and 0 <= e["axis"] <= 180, f"rx {r['rx_id']} {eye} in-store axis {e['axis']} not on 5° grid")
for v in variants.values():
    check(v["eluno"]["hsn"] in hsns, f"variant {v['sku']} hsn {v['eluno']['hsn']} has no tax rate")
    if v["eluno"]["line_type"] == "frame": check(v["inventory_item_id"] in inv_items and v["inventory_management"] == "shopify", f"frame {v['sku']} not inventory-tracked")
    else: check(v["inventory_item_id"] is None, f"lens {v['sku']} should not be inventory-tracked")
for row in inv: check(row["inventory_item_id"] in inv_items and row["location_id"] in loc_ids and row["available"] >= 0, f"inventory row {row} bad")
for o in offers: check(o["valid_from"] <= o["valid_to"], f"offer {o['offer_id']} bad dates")

def backout(gross, bps):
    g = Decimal(gross); taxable = (g * 10000 / (10000 + bps)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    t = g - taxable; c = (t / 2).quantize(Decimal("1"), rounding=ROUND_HALF_UP); return int(taxable), int(c), int(t - c)
def attr(o, name):
    return next((a["value"] for a in o["note_attributes"] if a["name"] == name), None)

seen_inv, seen_num = set(), set()
for o in orders:
    inv_no, sc = attr(o, "invoice_no"), attr(o, "store_code")
    check(o["location_id"] in loc_ids and o["customer"]["id"] in cust_ids and attr(o, "staff_id") in staff_ids, f"order {o['name']} bad refs")
    check(o["taxes_included"] is True, f"order {o['name']} must be taxes_included")
    check(o["order_number"] not in seen_num, f"duplicate order_number {o['order_number']}"); seen_num.add(o["order_number"])
    check(inv_no and inv_no not in seen_inv, f"order {o['name']} missing/duplicate invoice_no"); seen_inv.add(inv_no)
    check(inv_no.startswith(f"ELU/{sc}/"), f"order {o['name']} invoice prefix/store mismatch")
    check(attr(o, "channel") in ("walk_in", "whatsapp_booked", "online_pickup"), f"order {o['name']} bad channel")
    sub = disc = taxt = 0
    for li in o["line_items"]:
        v = variants.get(li["variant_id"]); check(v is not None and v["sku"] == li["sku"], f"order {o['name']} line {li['sku']} unknown variant")
        props = {p["name"]: p["value"] for p in li["properties"]}
        if props.get("line_type") == "lens":
            r = rx_by_id.get(props.get("rx_id")); check(r is not None, f"order {o['name']} lens line without rx_id property")
            if r and v:
                check(r["customer_id"] == o["customer"]["id"], f"order {o['name']} rx belongs to another customer")
                lim = v["eluno"]["rx_limits"]
                for eye in ("od", "os"):
                    e = r[eye]
                    check(lim["sph_min"] <= e["sph"] <= lim["sph_max"], f"order {o['name']} {eye} sph {e['sph']} outside {v['sku']} range")
                    check(abs(e["cyl"]) <= lim["cyl_abs_max"], f"order {o['name']} {eye} cyl outside lens range")
                    if lim["add_required"]: check(e["add"] is not None, f"order {o['name']} {v['eluno']['lens_type']} lens but rx has no ADD")
        gross = P(li["price"]) * li["quantity"] - P(li["total_discount"])
        bps = int(round(li["tax_lines"][0]["rate"] * 20000))
        tx, cg, sg = backout(gross, bps)
        got = (P(li["tax_lines"][0]["price"]), P(li["tax_lines"][1]["price"]))
        check(got == (cg, sg), f"order {o['name']} line {li['sku']} tax split {got} != {(cg, sg)}")
        check(tx + cg + sg == gross, f"order {o['name']} line {li['sku']} does not reconcile to the paisa")
        check(sum(P(a["amount"]) for a in li["discount_allocations"]) == P(li["total_discount"]), f"order {o['name']} line {li['sku']} discount allocations != total_discount")
        sub += P(li["price"]) * li["quantity"]; disc += P(li["total_discount"]); taxt += cg + sg
    check((P(o["subtotal_price"]), P(o["total_discounts"]), P(o["total_tax"]), P(o["total_price"])) == (sub, disc, taxt, sub - disc), f"order {o['name']} totals mismatch")
    sales = sum(P(t["amount"]) for t in o["transactions"] if t["kind"] == "sale" and t["status"] == "success")
    check(sales == P(o["total_price"]), f"order {o['name']} sale transactions {sales} != total")
    refunded = 0
    for rf in o["refunds"]:
        for rl in rf["refund_line_items"]:
            li = next((x for x in o["line_items"] if x["id"] == rl["line_item_id"]), None); check(li is not None, f"order {o['name']} refund line unknown")
            lt = {p["name"]: p["value"] for p in li["properties"]}.get("line_type") if li else None
            if lt == "frame": check(rl["restock_type"] == "return" and rl["location_id"] == o["location_id"], f"order {o['name']} frame refund must restock at the billing location")
            if lt == "lens": check(rl["restock_type"] == "no_restock", f"order {o['name']} lens must not restock")
            check(P(rl["subtotal"]) + P(rl["total_tax"]) == P(li["price"]) * li["quantity"] - P(li["total_discount"]), f"order {o['name']} refund line {li['sku']} does not reverse gross")
        for t in rf["transactions"]:
            check(t["kind"] == "refund" and t["parent_id"] in {x["id"] for x in o["transactions"]}, f"order {o['name']} refund transaction without parent sale")
            refunded += P(t["amount"])
        check(any(a["name"] == "credit_note_no" for a in rf["note_attributes"]), f"order {o['name']} refund without credit note number")
    check(refunded <= sales, f"order {o['name']} refunded more than captured")
    want = "refunded" if refunded and refunded == sales else ("partially_refunded" if refunded else "paid")
    check(o["financial_status"] == want, f"order {o['name']} financial_status {o['financial_status']} should be {want}")

for t in tills:
    check(t["location_id"] in loc_ids and t["opened_by"] in staff_ids and t["closed_by"] in staff_ids, f"till {t['session_id']} bad refs")
    exp = t["opening_float_paise"] + sum(m["amount_paise"] for m in t["movements"])
    check(exp == t["expected_cash_paise"], f"till {t['session_id']} expected cash {exp} != {t['expected_cash_paise']}")
    check(t["counted_cash_paise"] - t["expected_cash_paise"] == t["variance_paise"], f"till {t['session_id']} variance wrong")
    day = t["opened_at"][:10]
    cash_orders = [o for o in orders if o["location_id"] == t["location_id"] and o["created_at"].startswith(day) and attr(o, "payment_method") == "cash"]
    check(sum(P(o["total_price"]) for o in cash_orders) == sum(m["amount_paise"] for m in t["movements"] if m["type"] == "cash_sale"), f"till {t['session_id']} cash sales don't match Shopify cash orders")

print(f"{len(locations)} locations, {len(staff)} staff, {len(customers)} customers, {len(rx)} rx, {len(variants)} variants, {len(offers)} offers, {len(inv)} inventory rows, {len(orders)} orders, {len(tills)} till sessions")
if errs:
    print("\n".join("FAIL: " + e for e in errs)); sys.exit(1)
print("OK: seed data is internally consistent")
