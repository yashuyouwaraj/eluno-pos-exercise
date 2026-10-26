# Eluno POS

Foundation for the Eluno store till. This increment contains the local
application shell, integer-paise money and inclusive-GST utilities, customer
and prescription lookup, catalog lens access, prescription/lens validation,
cart construction, data-driven offers, final inclusive-GST cart pricing, and a
durable checkout path, full- and partial-refund handling, persistent cash-till
sessions, store-specific day-end reporting, and an interactive cashier CLI.

## Requirements

Python 3.8+; no packages need to be installed.

## Quick start

From the repository root, run:

```powershell
python scripts/start_local.py
```

This command starts the supplied mock in deterministic `--no-flake` mode,
waits for mock and POS health checks, and opens the cashier CLI in the same
terminal. Exit the CLI with option `0`, or press Ctrl+C, to stop both services.
It refuses occupied ports (`8787`, `8790`) and cleans up partial startup.
The local Shopify/SMT/terminal mock persists its state in
`eluno_pos_mock_state.sqlite3`; the POS sale ledger remains in its separate
`eluno_pos_state.sqlite3` database.

### Optional developer modes

`python -m eluno_pos` is API-only. Developers with an existing API can run
`python -m eluno_pos.cli`; `python scripts/start_local.py --no-cli` starts only
the mock and API.

The application defaults to the supplied mock URLs; set `ELUNO_SHOPIFY_BASE_URL`
and `ELUNO_SMT_BASE_URL` to point the two adapters at another local environment.
`ELUNO_TERMINAL_BASE_URL` and `ELUNO_STATE_DB` override the payment-terminal URL
and SQLite sale-ledger path.
Store business dates use the fixed `Asia/Kolkata` offset (UTC+05:30), not the
computer's local timezone.

## Scope and a practical sale

This version covers customer and prescription lookup, catalog-based frame/lens
sales, offer evaluation, inclusive GST, inventory checks before payment,
Shopify order creation, refunds, per-store invoices, till sessions, and
store/day reporting. The formal stretch feature is advisory billing-time
anomaly flags. Family billing, remakes, daily email, offer authoring, and
production authentication/integrations are not included. The CLI's manual
prescription-entry action is a local-mock utility requested during development;
it is not the brief's photo/free-text assist and is not a production SMT write
workflow.

For a first sale against a freshly started mock, use the seeded customer
Rahul Menon (`+919000000001`), whose valid prescription is `RX5002`. At the
cashier menu choose **1 New Sale** and enter store `HSR`, location `70000002`,
the default business date, channel `1`, Rahul's name and number, then sales
operator `S105`. Add a frame (choose variant `91000001`, which has one unit at
HSR in the fresh seed) and choose **1 Start a new pair**. Add a lens, choose
**1 Single Vision 1.50 standard** (`91000041`), then choose **2 Add to pair 1**.
Choose **0 Done**, accept the offer/pricing review, continue to payment, choose
**3 UPI**, and confirm. The CLI prints the invoice number and opens its page.
If the selected frame is no longer in stock, choose another listed frame with
stock at HSR.

## Cashier CLI

The CLI will not start servers itself. If the application is unavailable, it
prints the startup command rather than launching a duplicate process. The main
menu is **Cashier**, **Admin & Management**, and **System Status**. Cashier
contains New sale, customer/prescription and sale/invoice lookup, drawer
operations, optometrist-confirmed prescription entry for the local mock, and
**Browse Product Catalog**. Admin's **Catalog & Inventory** also includes
**Browse Product Catalog**, as well as adding a frame or made-to-order lens
and receiving tracked frame stock at an active store. New SKUs must be unique.
Frames begin with zero available stock; record a delivery with **Receive Frame
Stock** before checkout. Lens products are never given stock levels.
These writes go to the Shopify mock, which the cashier reads for product,
pricing, and availability; customers, orders, prescriptions, and mock payment
records share the mock's SQLite persistence. This local mock database is
distinct from the POS-owned SQLite sales/till/audit ledger.
Sales channel and payment method have numbered choices while still accepting
their text values. Admin contains refunds, day-end and channel reporting, anomaly and
unresolved-operation views, read-only audit history, and lens diagnostics.
New sale is one guided flow: enter a customer's name and mobile number, or leave
both blank for a walk-in. The POS first searches Shopify by the normalized
mobile number. If found, it uses that customer's saved details and latest valid
prescription, even if the entered name differs, and displays Shopify-verified
priority and `campaign:*` tags in a separate customer-flags section before item
entry; if not found, it registers the customer before sale entry. Caller-supplied
flags are ignored. Campaign walk-ins without a Shopify customer record cannot
be flagged: the supplied store-tool API exposes no queue/campaign-walk-in
lookup. The POS does not infer these flags. The customer is attached to the
sale and saved invoice snapshot. The flow offers numbered frame/lens/done
choices and lists active catalog variants filtered to the selected item type.
Use **Browse Product Catalog** from Cashier or Admin to view active frames and
lenses outside a sale. Select a variant by
its displayed number or ID; this prevents choosing a lens variant when adding a
frame. Lens specifications come from the selected catalog variant and are
validated against the customer's latest valid prescription before the line is
added. Pair references can be left unpaired, generated by choosing “Start a new
pair,” or selected from the existing pairs in the cart; cashiers do not need to
invent reference text. Frame-only and walk-in sales remain available. The cashier can keep
automatic best-offer selection, choose offers manually, or opt out of offers.
The server rechecks manual offer eligibility and stacking rules before payment.
The flow previews pricing, then asks for payment and final confirmation. The
draft retains its generated idempotency key after
cancellation or an ambiguous outcome. An uncertain draft blocks starting another
sale until it is reconciled; check its sale status and use the conservative
recovery action when a sale ID is available. Recovery does not retry payment or
Shopify order creation.

**Find Customer / Prescription** needs only the customer's mobile number. It
checks the prescription valid for the current Bengaluru business date, so the
cashier does not need to enter a date for a normal lookup.

For cash checkout, the CLI automatically selects the open till for the sale's
store and location. If none is open, it offers to open one in the checkout flow
and asks for the operator and actual opening float; later cash sales reuse that
open till without requiring its session ID. Use **Cash Drawer > open** at the
start of a shift when you want to open the till before the first sale.

Prescription entry is only enabled against the bundled local mock because the
provided real store-tool contract is read-only. The cashier enters the values
from the patient's prescription and an optometrist assigned to the selected
store must confirm the entry. The CLI matches an existing customer by mobile
number or registers a new named customer before accepting the prescription.
Enter a store code such as `MM` or `JN`, not the numeric Shopify location ID;
the optometrist must belong to that store. Sphere, cylinder, axis, ADD, and validity dates
are checked before the mock store-tool records it; a saved prescription is
available to subsequent lookups and lens validation. When cylinder is non-zero,
axis is required and must be an integer from 0 to 180 degrees in 5-degree
increments; a missing axis remains valid only when cylinder is zero. Invalid
values are rejected without rounding or changing the prescription. This mock confirmation is
not authenticated and must not be treated as a production clinical workflow.

System Status is read-only. It verifies the POS `/health` response and that the
local SQLite ledger can be read. It also calls `GET /api/system-status`, which
uses the already-configured adapters for safe `GET` probes: Shopify locations
and store-tool staff are `HEALTHY` only after their response envelopes validate.
The terminal performs a lookup of a reserved, non-charge diagnostic ID; the
mock's expected `404 unknown charge` is reported as `REACHABLE`, never
`HEALTHY`, because it does not prove a payment can be accepted. `UNKNOWN` means
a safe probe could not be verified, and `UNAVAILABLE` means a critical read
dependency could not be reached. Overall status is `READY` only when every
required capability for the detected environment is healthy; optional or
inapplicable checks do not block it. `DEGRADED` means a required capability is
reachable but unverified, unknown, or degraded, and `UNAVAILABLE` means a
critical dependency cannot be reached. The configuration summary redacts URL
credentials and does not display secrets. `python scripts/start_local.py`
positively configures the supplied
loopback mock. In that environment the optional **Test local mock payment flow**
action requires a separate confirmation and performs one fixed Rs. 1.00 UPI
charge against the mock, then reads it back. This creates a mock charge only; in the standard launcher it is retained in
the mock SQLite state file. It creates no sale, Shopify order, or POS ledger
entry and never contacts a production processor. The status
screen itself remains read-only.
The mock does not provide a payment-free readiness endpoint, so without this
explicit test payment-flow readiness is `UNKNOWN` and overall status is
`DEGRADED`. A successful test verifies only the configured mock at that time;
its result lasts only for the current POS process. A real terminal is never
marked ready from mock behavior, and production/unknown configurations remain
unverified.

Printable finalized documents are served at `/api/invoices/{sale_id}` and
`/api/credit-notes/{refund_id}`. The CLI displays their URLs; open one in a
browser and use the browser's Print command. The pre-checkout preview uses the
existing CartService and the supplied mock catalog; final price, tax, payment,
order, refund, and till actions remain the application's existing services.

## Failure verification and recovery

See [FAILURE_MATRIX.md](FAILURE_MATRIX.md) for reproducible evidence. Transport
loss for payments, Shopify orders, and refunds is unsafe to retry because the
mock offers no operation lookup or write idempotency; such records remain
unknown for operator reconciliation. Local integration tests use an isolated
no-flake mock; unsupported failure paths use deterministic test doubles. This
does not claim production reliability.

## API endpoints

The application runs at `http://127.0.0.1:8790`. All responses are JSON. `GET
/` describes the cashier API and points to the interactive startup command;
the separate mock dependency remains on port 8787 and intentionally does not
serve the cashier interface.

- `GET /` service information and supported endpoint groups
- `GET /health`
- `GET /api/system-status` (read-only) and the explicit
  `POST /api/system-status/mock-payment-test` local-mock-only diagnostic
- `GET /api/audit-events[?entity_id=...]` returns existing local audit events;
  the recorded actor is a caller-supplied operator ID, not authenticated identity.
- `GET /api/customer-prescription?phone=%2B919000000001&business_date=2026-10-26`
  returns one Shopify customer and the newest SMT prescription valid on that
  business date, including customer tags read from Shopify for cashier flags.
  Caller-provided tags are not used. It returns clear `404`, `409`, or `422`
  results for no customer, ambiguous customers, or no valid prescription.
- `GET /api/catalog/variants?line_type=frame` or `line_type=lens` lists active
  variants of that type for cashier selection, including catalog price and lens
  configuration metadata.
- `GET /api/admin/locations` and `GET /api/admin/catalog/frames` provide active
  stores and tracked frame variants to the local admin workflow.
- `POST /api/admin/catalog/products` creates one validated frame or made-to-order
  lens variant in the Shopify source. It requires a unique SKU, price and
  product-type metadata; lens configurations already in the catalog are rejected.
- `POST /api/admin/inventory/receipts` connects a tracked frame to an active store
  if needed, increases available units, and records the caller-supplied operator
  ID and goods-received reference in POS audit history. Repeating the same
  reference for the same item/store is idempotent; reusing it with a different
  quantity is rejected. Lens stock is rejected. As with the other local CLI
  operator fields, the ID is not authenticated.
- `GET /api/offers?business_date=2026-10-27` lists date-valid offers that can be
  chosen manually. Cart eligibility is verified by the offer engine before
  payment.
- `GET /api/invoices/lookup?invoice_no=ELU%2FMM%2FFY26%2F00001` resolves a saved
  invoice number to its sale ID and status so the invoice can be reopened.
- `GET /api/tills?store_code=MM` lists live open till sessions for a store;
  historical sample till IDs are not loaded into the POS database.
- `POST /api/customers` registers a customer in Shopify using `name` and `phone`.
  It accepts a 10-digit Indian mobile or `+91` number, returns the persisted
  customer ID and contact details, and safely resolves a repeat registration
  with the same name and mobile to that existing record. A mobile already
  associated with a different name returns `409`.
- `POST /api/prescriptions` records an optometrist-confirmed prescription in the
  bundled local mock only. It requires an existing Shopify customer, store code,
  prescription and validity dates, OD/OS values, an optometrist ID assigned to
  that store, explicit confirmation, and an idempotency key. It returns `503`
  against a non-local or unsupported real store-tool configuration.
- `POST /api/lens-validation` validates a lens configuration against the fetched
  lens matrix. The request body is:

```json
{
  "lens": {"lens_type": "progressive", "index": "1.60", "coating": "blue_cut"},
  "prescription": {
    "rx_id": "RX5001", "customer_id": "80001000", "phone": "+919000000000",
    "date": "2025-07-16", "valid_until": "2026-07-16",
    "od": {"sph": "-4.00", "cyl": "0.00", "axis": null, "add": "2.00"},
    "os": {"sph": "-3.75", "cyl": "0.00", "axis": null, "add": "2.00"}
  }
}
```

An invalid configuration returns `422` with staff-actionable errors. Right- and
left-eye axis values are independently validated against the inclusive 0–180
degree range and 5-degree grid. Fractional or malformed axes are rejected
rather than coerced; a missing axis is allowed only with zero cylinder.
Checkout uses this same validation before persisting a sale, initiating
payment, creating a Shopify order, or issuing an invoice. For progressive and
bifocal lenses, ADD is required and checked against the selected variant's
published ADD bounds (currently 0.75–3.00 in the supplied catalog).

## Checkout reliability

Before checkout creates a durable sale record or contacts the payment terminal,
the POS verifies the active store/location and checks each frame's Shopify
inventory item at that location. Checkout requests are serialized within the
running POS process from stock validation through Shopify order creation, so
two concurrent local checkouts cannot both sell the last unit. Missing,
malformed, unknown, or insufficient stock blocks checkout; made-to-order lens
variants are exempt. Shopify order creation independently decrements inventory
with `decrement_obeying_policy`. The mock has no reservation operation, so an
out-of-band stock change by another system after the pre-check can still make
order creation fail after payment; that outcome remains subject to conservative
reconciliation. Deployments with multiple POS application processes require a
shared reservation/locking mechanism for the same guarantee across processes.

`POST /api/checkouts` accepts checkout intent, not a trusted total. The server
re-resolves catalog data and recalculates offers, tax, and payable amount before
charging. Its JSON body contains `idempotency_key`, `business_date`,
`payment_method`, `channel`, `staff_id`, optional `customer_id` and
`customer_phone`, and a `cart` with `store_code`, `location_id`, and lines (`variant_id`,
`quantity`, optional `pair_ref` and `prescription_id`). An optional `offer_ids`
array requests a manual offer selection; omitted means the normal automatic
best-offer policy, while an empty array means no offers. The server verifies
eligibility and stacking from authoritative cart/customer facts before payment.

`customer_tags` supplied by a caller are ignored. When both customer ID and
phone are supplied, the server looks up the phone through Shopify and accepts
tags only if exactly one returned customer has that ID. The supplied mock has no
customer-by-ID endpoint, so missing, ambiguous, or unavailable phone lookup
fails closed for tag-dependent offers while non-tag offers can still proceed.

Cash checkout requires `till_session_id`. The session must be open and match
the checkout store and location. Only after payment and Shopify order creation
succeed does the POS post one `cash_sale` movement, using the durable sale ID
as its unique reference. Card and UPI checkouts never post to drawer cash.
If local cash posting fails, it remains pending for conservative local recovery
without initiating another payment. Day-end exceptions and anomaly monitoring
identify completed cash sales and refunds whose local till posting is still
pending; use the sale/refund recovery action to retry only the local movement.
These exceptions do not replay a payment, Shopify order, or Shopify refund.

Checkout state is persisted in standard-library SQLite before payment:
`PENDING_PAYMENT` -> `PAYMENT_APPROVED` -> `ORDER_PENDING` -> `COMPLETED`.
Durable failure/recovery states are `PAYMENT_DECLINED`, `PAYMENT_FAILED`,
`PAYMENT_UNKNOWN`, `ORDER_FAILED`, and `ORDER_UNKNOWN`.

The idempotency key is unique and bound to a canonical checkout-request hash.
Repeats return the original sale without another charge; different payloads with
the same key are rejected. SQLite immediate transactions serialize concurrent
sale and invoice-number allocation.

The mock's initial terminal 503 is retried with the stable sale ID as
`order_ref`; it does not actually map an already-created charge by that
reference, so declined payments are not retried through the same sale. Transport
timeouts are `PAYMENT_UNKNOWN` and never blindly charged again. Once captured,
the charge ID is persisted before Shopify creation. Shopify 429 is retried; a
timeout or 5xx becomes `ORDER_UNKNOWN` and is not retried automatically because
the mock has no order idempotency key or POS-reference search endpoint.

Invoice numbers are allocated per store before payment and never reused in the
local ledger. They are returned only after Shopify creation succeeds and the
invoice becomes `ISSUED`; declined payments retain a `VOID` allocation. The mock
cannot reconcile historical invoice numbers or define legal void handling, so
production needs the finance-approved reconciliation policy from the stakeholder
reply.

Operators can retrieve the persisted status with `GET /api/checkouts/{sale_id}`
and request conservative recovery with `POST /api/checkouts/{sale_id}/recover`.
Recovery only preserves/marks uncertain external work; the mock can look up a
terminal charge only when its charge ID is already known and cannot locate a
Shopify order by POS sale or invoice reference, so it never replays either
ambiguous operation.

## Run checks

```powershell
python starter-kit/validate_seed.py
python -m unittest discover -s tests -v
python -m compileall -q eluno_pos scripts starter-kit/mock-apis/mock_server.py
python -m unittest tests.test_local_mock_integration -v
```

The test suite uses Bengaluru business dates rather than the host's local
calendar date. On PowerShell, it can also be checked under both timezone
environment settings:

```powershell
$env:TZ = "UTC"; python -m unittest discover -s tests -v
$env:TZ = "Asia/Kolkata"; python -m unittest discover -s tests -v
```

`python starter-kit/generate_seed.py` is optional and rewrites
`starter-kit/data/*.json`; back up any manually edited seed data before running
it. It writes UTF-8 and atomically replaces each JSON file.

### Reproducing key outcomes

These focused commands exercise failure paths without editing seed data:

```powershell
python -m unittest tests.test_local_mock_integration.LocalMockIntegrationTests.test_checkout_api_rejects_progressive_without_add_before_payment_or_order -v
python -m unittest tests.test_local_mock_integration.LocalMockIntegrationTests.test_checkout_api_rejects_out_of_stock_frame_before_persisting_sale_or_order -v
python -m unittest tests.test_checkout.CheckoutTests.test_decline_creates_no_order_and_is_only_retried_explicitly -v
python -m unittest tests.test_local_mock_integration.LocalMockIntegrationTests.test_checkout_invoice_refund_restock_and_day_end_use_real_local_mock -v
```

The first two and final command exercise the checkout/API against the local
mock. The payment-decline path uses a deterministic terminal test double because
the supplied terminal has no configurable decline mode. Refund and day-end
test-double cases are also kept in `tests/test_operations.py` and
`tests/test_reporting.py`; no production service is contacted.

### Starter data ownership and runtime use

The generated files in `starter-kit/data/` have distinct owners and consumers:

| File(s) | Runtime use |
|---|---|
| `locations.json`, `customers.json`, `products.json`, `inventory_levels.json`, `orders_sample.json` | Loaded as initial seed state by the supplied Shopify mock. Inventory reads use `GET inventory_levels.json` with `inventory_item_ids` and `location_ids`; order creation decrements stock at its Shopify location. The standard local launcher persists later changes in `eluno_pos_mock_state.sqlite3`. |
| `prescriptions.json`, `staff.json` | Loaded as initial mock SMT state and served by the mock prescription/staff routes. Later customer and prescription writes are persisted by the standard local launcher. |
| `offers.json` | Read by POS runtime offer evaluation through `OfferBook.from_file` in `http_app.default_application()`. |
| `tax_rates.json` | Read by POS runtime tax calculation through `TaxRateTable.from_file` in `http_app.default_application()`. |
| `till_sessions_sample.json` | Historical/sample till fixture only. `generate_seed.py` writes it and `validate_seed.py` checks its sample arithmetic; the mock does not load it and POS runtime does not seed live tills from it. |

`validate_seed.py` reads all generated data files for consistency checks.
Live till sessions, movements, audit events, and refunds are POS-owned SQLite
state in `OperationsStore`; this is separate from Shopify-owned catalog,
customer, inventory and order data, SMT-owned prescriptions/staff records, and
historical/sample fixture files. The local mock's SQLite file stores its
simulated external-system records; it does not replace or duplicate the
POS-owned sales ledger.

## Refunds and tills

Completed documents can be opened in a browser and printed using its normal
print command: `GET /api/invoices/{sale_id}` and `GET /api/credit-notes/{refund_id}`
return self-contained HTML. An invoice is available only after the local sale is
`COMPLETED` and `ISSUED`; a credit note is available only after the refund is
`COMPLETED`. Pending or uncertain operations deliberately have no final document.
After a completed checkout, the cashier CLI opens the invoice in the default
browser and prints its URL. If needed, **Find Sale / Invoice** accepts either
the sale ID (`sale_...`) or the invoice number (`ELU/...`) and opens the saved
invoice page again. Customer lookup accepts a 10-digit mobile and normalizes it
to `+91` before searching. To view a till, enter its live POS-generated session
ID, or leave it blank to list current open sessions by store. Historical IDs in
`till_sessions_sample.json` are examples and are not live till sessions.
Invoices use a compact, fixed-width layout; use landscape orientation in the
browser print dialog (the invoice page also requests A4 landscape with print
margins). Reopening an invoice reads the existing sale and Shopify order and
does not create another checkout or invoice number. New completed sales retain
the relevant Shopify order response, verified customer details, store location,
and checkout-time per-line taxable values and descriptions. This lets reprints
survive a mock restart without fresh catalog/order lookups. Older sales are
recovered from Shopify by their saved order ID when available; if the order is
gone, the renderer uses only the saved checkout payload and shows unavailable
historical fields honestly.

`POST /api/refunds` accepts only `order_id`, `store_code`, `operator`, `reason`,
optional `till_session_id`, optional `items` (`line_item_id` and positive
`quantity`), and an optional `idempotency_key`; amount and tax are always
derived from the recorded Shopify order. Omitting `items` retains the full
refund workflow. Partial refunds track quantities already present in Shopify
refund records and reject requests above the remaining line quantity. `GET /api/refunds/{refund_id}` returns local status and
`POST /api/refunds/{refund_id}/recover` exposes unresolved outcomes without
replaying a refund. Frames are returned at the original order location in the
same supported Shopify refund request; lenses use `no_restock`.

`POST /api/tills` opens a store session with `store_code`, `location_id`,
`operator`, and `opening_float_paise`. Use `POST /api/tills/{id}/movements` for
positive `amount_paise` paid-ins or paid-outs only; both require a reason. Cash
sales and cash refunds are system-recorded only and are rejected from HTTP, and
`POST /api/tills/{id}/close` with `counted_cash_paise`. Expected cash is opening
float plus signed cash movements; card/UPI sales are never added automatically.

The mock does not offer idempotent refund creation or search-by-POS-refund.
After an ambiguous Shopify refund response, the local record remains
`REFUND_UNKNOWN` and recovery does not submit a second refund.

## Day-end reporting

`GET /api/reports/day-end?store_id=70000002&date=2026-10-26` returns a
read-only report for one active billing location. Sales are attributed by the
`Asia/Kolkata` business date in `processed_at`; refunds are attributed
separately by the `Asia/Kolkata` date in each refund's `created_at`. Naive
timestamps are interpreted in Bengaluru local time. Metrics include completed
bills, gross, discounts, net sales before/after refunds, taxable value, CGST,
SGST, sales/refund tax, and successful payment gateway totals, all in paise.
Missing or invalid refund timestamps are exceptions, not assigned to a date.

The mock order listing is bounded and supplies no cursor pagination, so reports
are marked bounded and may be incomplete above that limit. Cash checkout stores
its required till-session reference in the local sale ledger and posts a single
`cash_sale` movement with the sale ID as its reference after checkout completes.
If that local posting remains pending, day-end and anomaly reports identify the
sale, till session, and amount for reconciliation.
The day-end report exposes the relevant local till sessions; its Shopify-derived
sales metrics remain bounded by the mock listing. Day-end includes the closed
till sessions and any cash-variance alerts for that business date.

A cash variance alert is raised only when a closed till differs from expected
cash by **more than Rs. 200.00** (20,000 paise); exactly Rs. 200.00 is not an
alert. The close response, day-end report, and anomaly report include the
store, till session, expected/counted cash, signed variance, and Bengaluru
business date. Finance can retrieve it in Admin & Management > Anomaly
Monitoring (`GET /api/reports/anomalies`) or the day-end report. There is no
configured email/notification service, so this application does not send the
stakeholder-requested same-night email.

The report also includes a `channels` breakdown for `walk_in`,
`whatsapp_booked`, and `online_pickup`: completed bills, gross, discounts, net
sales before refunds, and refunds attributed to the refund business date.
Channel comes only from the persisted Shopify order metadata; unavailable or
unknown values are placed in the `unknown` bucket and listed as exceptions.

## Operational anomaly flags

`GET /api/reports/anomalies?store_id=70000002&date=2026-10-26` is a read-only,
deterministic operational reconciliation report, not a trained ML model or a
fraud claim. It flags invalid/mismatched recorded discounts, Shopify payment
total mismatches, local checkout versus Shopify total/offer-result mismatches,
unresolved checkout/refund outcomes, and repeated unresolved local refunds.
It never blocks or changes sales, payments, refunds, or source records. The
same bounded Shopify-order-list limitation applies: it reports only what that
mock endpoint returned. It also flags closed tills with absolute variance
greater than 20,000 paise, including store, session, expected/counted amounts,
signed variance and business date.

## Performance target

The founder's sub-10-second billing target is not measured or certified by the
exercise. The supplied local mock and isolated smoke tests verify workflow
completion, not representative store Wi-Fi latency or production Shopify,
SMT, and terminal response times. Benchmark with production-like systems before
making a latency commitment.

Billing-time flags are advisory and deterministic: a sphere change over 1.00 D
per eye against one unambiguous prior prescription, and a progressive
selection with complete available order history showing no prior progressive
lens. Axis-grid violations are rejected by prescription validation and are not
reported as advisory anomalies. Missing, tied, or bounded history is treated
as unknown rather than proof of first-time wear. Flags preserve the
prescription and do not block checkout.

## Audit events

The local SQLite state records append-only audit events for checkout submission,
outcomes and recovery; refund submission/outcomes/recovery; and till open,
movement, and close. Local state transitions and their audit event are committed
in the same SQLite transaction, so an audit-write failure rolls back that local
transition before external work begins. Each carries a timestamp, action, entity reference,
actor field, optional free-text reason, and minimal operational metadata. The
actor is an operator/staff ID supplied by the caller or `unattributed` for a
recovery request without one; this application does not authenticate users and
a reason is not an approved reason code.

## Tax convention

Amounts are integer paise in the domain. Taxable value is backed out of the
discounted, GST-inclusive line gross using half-up paise rounding. CGST is half
the resulting tax rounded half-up; SGST is the remaining paise. Rates are read
from `starter-kit/data/tax_rates.json` by HSN and effective date.

## Cart pricing and offers

`CartService` resolves each variant ID from the Shopify catalog projection; it
does not copy SKU, price, HSN, lens type, or sale-status data into the POS. A
lens line needs an Rx reference. Frame-only (plano), lens-only, and multiple
separate frame/lens pairs are supported. Unknown/inactive variants, invalid
quantities, and duplicate frame or lens lines in one `pair_ref` are rejected.
For `min_pairs_in_cart` offers, a pair is one distinct non-empty `pair_ref`
with both a frame and lens line. Lens-only lines, unpaired lines, and a larger
quantity on one pair reference do not create extra eligible pairs.
Automatic offer selection remains the default. Manual selection applies only
the chosen, currently eligible offers and still obeys stacking and discount
limits. Offers requiring manager approval or customer-tag eligibility are not
presented for manual selection; they remain governed by the existing automatic
rules and authority checks.

```python
from datetime import date
from pathlib import Path

from eluno_pos.adapters.shopify import ShopifyAdminClient
from eluno_pos.cart import CartService
from eluno_pos.models import Cart, CartLine
from eluno_pos.offers import OfferBook, OfferEngine
from eluno_pos.tax import TaxRateTable

root = Path.cwd()
catalog = ShopifyAdminClient()  # supplied local mock by default
service = CartService(
    catalog,
    OfferEngine(OfferBook.from_file(root / "starter-kit/data/offers.json")),
    TaxRateTable.from_file(root / "starter-kit/data/tax_rates.json"),
)
cart = Cart("HSR", "70000002", (
    CartLine("91000036", 1, "pair-1"),
    CartLine("91000049", 1, "pair-1", "RX5040"),
))
pricing = service.price(cart, date(2026, 10, 18), frozenset())
assert pricing.reconciles()
```

Offer rules are loaded from `offers.json`. The result includes eligible,
applied, and rejected offers (with reasons), plus per-line integer-paise
discount allocations. The policy is explicit:

- A non-stackable offer is exclusive. Offers combine only when both rules are
  stackable.
- Every compatible stackable subset and each non-stackable alternative is
  priced. The combination with the greatest customer discount wins.
- Exact discount ties use the greater sum of offer priorities, then ascending
  offer IDs. Priority therefore resolves only a true tie; it does not silently
  mean first rule wins or highest priority always wins.
- Winning stack offers apply in descending priority, then offer-ID order.
  Discounts cannot exceed a line's remaining value; percentage discounts use
  half-up paise rounding and catalog `max_discount_paise` caps are enforced.
- Pair flat discounts allocate to the frame line, matching seeded `BUNDLE300`.
  Cart flat discounts allocate in cart-line order.

`MANAGER_OVERRIDE` is rejected with an explicit explanation: its fixture
requires a manager role and reason code but does not define an approval/audit
workflow. No staff-approval workflow has been invented.
