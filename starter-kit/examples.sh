#!/usr/bin/env bash
# A walkthrough of one bill against the mock: lookup -> prescription -> stock -> charge -> Shopify order -> refund -> day-end.
# This shows the calls and their shapes. It is not a reference implementation; your code should do better than curl in a loop.
# Usage:  python3 mock-apis/mock_server.py --no-flake &   then   bash examples.sh      (PORT=9000 bash examples.sh to override)
set -euo pipefail
PORT="${PORT:-8787}"; H="http://localhost:$PORT"; B="$H/admin/api/2026-10"
PHONE='%2B919000000005'; STORE=70000002; FRAME_VARIANT=91000003; FRAME_ITEM=92000003; LENS_VARIANT=91000083
j() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
step() { printf '\n== %s\n' "$*"; }

# POST with retry: honours Retry-After on 429 and retries 503 (terminal) with the same body, so the call is idempotent on our side.
post() { # url body
  local url="$1" body="$2" tries=0 code out
  while :; do
    out=$(curl -s -w '\n%{http_code}' -X POST "$url" -H 'Content-Type: application/json' -d "$body")
    code="${out##*$'\n'}"; out="${out%$'\n'*}"
    if [ "$code" = 429 ] || [ "$code" = 503 ]; then tries=$((tries+1)); [ $tries -gt 5 ] && { echo "$out"; return 1; }; sleep 1; continue; fi
    echo "$out"; [ "${code:0:1}" = 2 ] || return 1; return 0
  done
}

step "customer by phone (Shopify)";          CUST=$(curl -s "$B/customers/search.json?query=phone:$PHONE"); echo "$CUST" | j "d['customers'][0]['id'], d['customers'][0]['first_name'], d['customers'][0]['tags'], d['customers'][0]['total_spent']"
CUST_ID=$(echo "$CUST" | j "d['customers'][0]['id']")
step "latest prescription (store tool)";     RX=$(curl -s "$H/smt/prescriptions?phone=$PHONE"); echo "$RX" | j "d['prescriptions'][0]['rx_id'], d['prescriptions'][0]['od'], d['prescriptions'][0]['valid_until']"
RX_ID=$(echo "$RX" | j "d['prescriptions'][0]['rx_id']")
step "variants";                             curl -s "$B/variants/$FRAME_VARIANT.json" | j "d['variant']['sku'], d['variant']['price'], d['variant']['eluno']['hsn']"; curl -s "$B/variants/$LENS_VARIANT.json" | j "d['variant']['sku'], d['variant']['price'], d['variant']['eluno']['lens_type'], d['variant']['eluno']['rx_limits']"
step "stock at the billing store";           curl -s "$B/inventory_levels.json?inventory_item_ids=$FRAME_ITEM&location_ids=$STORE" | j "d['inventory_levels'][0]['available']"

# Money below is from orders_sample.json #1001 (already reconciled to the paisa). Your code computes these; here they are typed in.
TOTAL="19072.15"; INVOICE="ELU/HSR/FY26/00099"
step "charge on the terminal (retries once on 503)"; CH=$(post "$H/terminal/charges" "{\"amount_paise\":1907215,\"method\":\"upi\",\"order_ref\":\"$INVOICE\"}"); echo "$CH" | j "d['charge_id'], d['status']"
CHARGE_ID=$(echo "$CH" | j "d['charge_id']")

step "create the order in Shopify (string money, taxes_included, Rx on the lens line, invoice no in note_attributes, stock decremented here)"
ORDER=$(post "$B/orders.json" "{\"order\":{\"taxes_included\":true,\"location_id\":$STORE,\"inventory_behaviour\":\"decrement_obeying_policy\",\"source_name\":\"eluno-pos\",
 \"customer\":{\"id\":$CUST_ID},
 \"note_attributes\":[{\"name\":\"invoice_no\",\"value\":\"$INVOICE\"},{\"name\":\"store_code\",\"value\":\"HSR\"},{\"name\":\"channel\",\"value\":\"walk_in\"},{\"name\":\"staff_id\",\"value\":\"S105\"},{\"name\":\"payment_method\",\"value\":\"upi\"}],
 \"line_items\":[
   {\"variant_id\":$FRAME_VARIANT,\"quantity\":1,\"price\":\"10998.00\",\"total_discount\":\"0.00\",
    \"tax_lines\":[{\"title\":\"CGST\",\"rate\":0.09,\"price\":\"838.83\"},{\"title\":\"SGST\",\"rate\":0.09,\"price\":\"838.83\"}],
    \"properties\":[{\"name\":\"line_type\",\"value\":\"frame\"},{\"name\":\"hsn\",\"value\":\"90031900\"},{\"name\":\"pair_ref\",\"value\":\"1\"}]},
   {\"variant_id\":$LENS_VARIANT,\"quantity\":1,\"price\":\"9499.00\",\"total_discount\":\"1424.85\",
    \"tax_lines\":[{\"title\":\"CGST\",\"rate\":0.025,\"price\":\"192.24\"},{\"title\":\"SGST\",\"rate\":0.025,\"price\":\"192.24\"}],
    \"discount_allocations\":[{\"amount\":\"1424.85\",\"discount_application_index\":0}],
    \"properties\":[{\"name\":\"line_type\",\"value\":\"lens\"},{\"name\":\"hsn\",\"value\":\"90015000\"},{\"name\":\"pair_ref\",\"value\":\"1\"},{\"name\":\"rx_id\",\"value\":\"$RX_ID\"},{\"name\":\"lens_config\",\"value\":\"progressive/1.6/blue_cut\"}]}],
 \"discount_applications\":[{\"type\":\"manual\",\"title\":\"PGX_CAMPAIGN15\",\"value\":\"1424.85\",\"value_type\":\"fixed_amount\",\"allocation_method\":\"each\",\"target_selection\":\"explicit\",\"target_type\":\"line_item\"}],
 \"transactions\":[{\"kind\":\"sale\",\"status\":\"success\",\"gateway\":\"razorpay_terminal\",\"amount\":\"$TOTAL\",\"authorization\":\"$CHARGE_ID\"}]}}")
echo "$ORDER" | j "d['order']['id'], d['order']['name'], d['order']['financial_status'], d['order']['total_price'], d['order']['total_tax']"
OID=$(echo "$ORDER" | j "d['order']['id']"); FRAME_LINE=$(echo "$ORDER" | j "d['order']['line_items'][0]['id']"); LENS_LINE=$(echo "$ORDER" | j "d['order']['line_items'][1]['id']"); SALE_TX=$(echo "$ORDER" | j "d['order']['transactions'][0]['id']")
step "stock after create (one less)";        curl -s "$B/inventory_levels.json?inventory_item_ids=$FRAME_ITEM&location_ids=$STORE" | j "d['inventory_levels'][0]['available']"

step "refund calculate (what Shopify thinks is refundable)"
post "$B/orders/$OID/refunds/calculate.json" "{\"refund\":{\"refund_line_items\":[{\"line_item_id\":$FRAME_LINE,\"quantity\":1,\"restock_type\":\"return\",\"location_id\":$STORE},{\"line_item_id\":$LENS_LINE,\"quantity\":1,\"restock_type\":\"no_restock\"}]}}" | j "[(x['restock_type'], x['subtotal'], x['total_tax']) for x in d['refund']['refund_line_items']], d['refund']['transactions'][0]['maximum_refundable']"

step "refund on the terminal, then the refund in Shopify (frame returns to the billing store, lens does not restock)"
post "$H/terminal/refunds" "{\"charge_id\":\"$CHARGE_ID\",\"amount_paise\":1907215,\"reason\":\"CHANGED_MIND\"}" | j "d['refund_id'], d['status']"
post "$B/orders/$OID/refunds.json" "{\"refund\":{\"note\":\"CHANGED_MIND\",\"note_attributes\":[{\"name\":\"credit_note_no\",\"value\":\"ELU/HSR/FY26/CN/00099\"},{\"name\":\"reason_code\",\"value\":\"CHANGED_MIND\"}],
 \"refund_line_items\":[{\"line_item_id\":$FRAME_LINE,\"quantity\":1,\"restock_type\":\"return\",\"location_id\":$STORE},{\"line_item_id\":$LENS_LINE,\"quantity\":1,\"restock_type\":\"no_restock\"}],
 \"transactions\":[{\"kind\":\"refund\",\"gateway\":\"razorpay_terminal\",\"amount\":\"$TOTAL\",\"parent_id\":$SALE_TX}]}}" | j "d['refund']['id'], [(x['restock_type'], x['subtotal'], x['total_tax']) for x in d['refund']['refund_line_items']]"
step "stock after refund (back to where it started)"; curl -s "$B/inventory_levels.json?inventory_item_ids=$FRAME_ITEM&location_ids=$STORE" | j "d['inventory_levels'][0]['available']"
step "order status";                          curl -s "$B/orders/$OID.json" | j "d['order']['financial_status']"

step "day-end source: orders for Jayanagar on 2026-10-06 (status=any so refunded ones are included)"
curl -s "$B/orders.json?created_at_min=2026-10-06T00:00:00%2B05:30&created_at_max=2026-10-06T23:59:59%2B05:30&location_id=70000005&status=any" | j "[(o['name'], o['total_price'], o['total_tax'], [a['value'] for a in o['note_attributes'] if a['name']=='payment_method'][0]) for o in d['orders']]"
printf '\nDone. Restart the mock to reset the seed.\n'
