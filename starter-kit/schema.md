# What lives where

```
SHOPIFY (system of record)              STORE TOOL (SMT)                 YOUR POS OWNS
Customer   id, phone, tags, total_spent  Prescription  rx_id, phone,     InvoiceSeries   store_code, prefix, next_no, cancelled[]
Location   id, eluno{store_code,...}                   od/os, pd, source, TillSession     session_id, location_id, float,
Product    id, product_type, variants[]                valid_until                        movements[], expected, counted, variance
Variant    id, sku, price, inventory_    Staff         staff_id, role,   OfferEvaluation cart -> [{offer_id, applied, amount, why}]
           item_id, eluno{hsn,                         store_code,       Channel         walk_in | whatsapp_booked | online_pickup
           rx_limits,...}                              badges            TaxRate         hsn, rate_bps, effective_from/to
InventoryLevel inventory_item_id,                                        Offer           offer_id, rule, stackable, priority, dates
           location_id, available                                        AuditEvent      who, what, when, reason_code, ref
Order      id, order_number (GLOBAL), line_items[], tax_lines, transactions[], refunds[], note_attributes[]
LineItem   id, variant_id, price, total_discount, tax_lines[], discount_allocations[], properties[]   <- prescription goes here
Transaction kind (sale|refund), amount, gateway, parent_id
Refund     refund_line_items[{line_item_id, quantity, restock_type, location_id}], transactions[]
```

Decisions the seed leaves to you:

- **Order vs invoice vs credit note.** Shopify gives you `order_number`; finance wants a gapless per-store series. Where is the series allocated, when, and what happens to a number if order creation fails after the terminal charged?
- **What is a pair?** A frame line and a lens line share a `pair_ref` property. A lens-only line is a pair without a frame. A remake is a lens line on a new order whose `note_attributes` carry `parent_invoice_no`.
- **Family billing.** One Shopify order has one customer. Lines can carry their own `rx_id` and could carry a `for_customer_id`. Decide and defend.
- **Idempotency.** The terminal is idempotent on `order_ref`. Shopify order create is not idempotent. What's your key, and where does it live?
- **Offer evaluation.** `priority` and `stackable` both exist. The semantics when they conflict, and whether you pick the best outcome for the customer or the first valid one, are yours to define and show.
- **Expired prescription.** `valid_until` exists. What happens at the till?
- **Day-end source of truth.** Finance says it must match Shopify. Your till also knows what happened. Which one does the report read from, and how do you show a mismatch?
