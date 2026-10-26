# Eluno POS — final assignment audit

## Scope and evidence

The candidate brief and stakeholder thread define the assignment. This audit
checks implementation and tests in the repository; it does not treat the
stakeholder reply, a fixture, or a test name as proof by itself. The local
mock is the only external system exercised.

| Requirement | Implementation and test evidence | Gap or disposition |
|---|---|---|
| Customer by phone; latest valid prescription | `CustomerPrescriptionService.lookup`; `tests/test_customer_prescription.py`, checkout rejection integrations, and `test_prescription_entry_persists_and_is_available_after_mock_restart`. | **PASS** for supplied rules and standard launcher persistence. The workspace database contained no prescription for the reported customer; no production SMT tested. |
| Priority customer and campaign flags on cashier sale | `CustomerPrescriptionService` resolves Shopify tags; `/api/customer-prescription` returns those tags; cashier shows priority and `campaign:*` flags separately from sales channel. CLI and HTTP tests cover priority, campaign, missing tags and ambiguous lookup. | **PARTIAL**: customer tags are authoritative and caller tags are ignored. The supplied SMT API has no queue/campaign-walk-in signal, so anonymous campaign walk-ins cannot be flagged safely; no data is fabricated. |
| Cart and lens matrix validation | `CartService`, `LensValidator`, `CheckoutService._validate_lens_prescriptions`; `tests/test_lens_validation.py`, `tests/test_checkout.py`, `test_checkout_api_rejects_progressive_without_add_before_payment_or_order`, and the focused lens-validation HTTP test. | **PASS** for exact catalog variant, prescription identity, sphere/cylinder, required/bounded ADD, and independently enforced right/left axis range and 5-degree grid before sale persistence, payment, Shopify order creation or invoice issuance. Fractional/malformed axes are rejected without coercion; missing axis is allowed only for zero cylinder. |
| Offers by date, conditions and stacking | `OfferBook`, `OfferEngine`; `tests/test_cart_offers_pricing.py`. | **PASS** for supplied rules. Manager override is deferred because approver authentication/reason policy is unresolved. |
| Inclusive GST by HSN, integer-paise reconciliation | `TaxRateTable`, `CartService`, invoice/document serialization; `tests/test_tax.py`, `tests/test_cart_offers_pricing.py`, `tests/test_documents.py`. | **PASS** for supplied effective-dated rates and half-up per-line policy. |
| Terminal charge then Shopify order with transaction, tax, discount, Rx and POS invoice | `CheckoutService.checkout`, `_charge_then_create`, `_order_payload`, `SaleStore`; `tests/test_checkout.py`, `test_new_customer_checkout_invoice_round_trip_uses_real_local_mock`. | **PASS** for deterministic local path. **PARTIAL** on ambiguous Shopify create: supplied API has no lookup/idempotency; preserved as unknown for reconciliation, never blindly replayed. |
| Invoice series per store, persisted and unique | `SaleStore.create_or_get` uses SQLite `BEGIN IMMEDIATE`, unique idempotency key and per-store series; invoice/concurrency cases in `tests/test_checkout.py`. | **PASS** for persistence and concurrent allocation. Numbers allocated before payment are not reused; failed/declined attempts remain void/pending records, so strict gapless issued-only numbering is not promised. |
| Inventory checked before payment; frame refund restores original store; lenses not stocked | `CheckoutService._validate_frame_inventory`, `RefundService.refund`, Shopify inventory adapter; `tests/test_checkout.py`, `tests/test_operations.py`, out-of-stock and refund/restock local integrations. | **PASS** for pre-check and refund location/type. No reservation API exists, so external stock change after pre-check can cause a post-payment order failure. |
| Full/partial Shopify refunds, tax reversal and credit-note reference | `RefundService.refund`, local refund state and document rendering; `tests/test_operations.py`, `tests/test_documents.py`, frame-refund integration. | **PASS** for exercised behavior; uncertain Shopify refund writes are not replayed. The mock does not issue legally certified credit notes. End-to-end lens-only refund is a test gap; unit assertions verify no lens restock. |
| Day-end by store/date from Shopify, with payment/refund/tax totals | `DayEndReportService.report`; `tests/test_reporting.py`, `tests/test_reporting_regressions.py`, checkout/refund/day-end integration. | **PARTIAL** above the mock's 250-order non-paginated response bound. Time-zone and payment reconciliation exceptions are surfaced; production pagination is required for unbounded history. |
| Cash till and channel reporting (Should) | `OperationsStore`, `DayEndReportService`, CLI; `tests/test_operations.py`, `tests/test_reporting.py`, `tests/test_cli.py`. | **PASS** for local workflow. Cash checkout selects an open till automatically by store/location and can offer to open one inline if none exists. Completed cash sales/refunds whose till movement is pending are exposed as day-end exceptions and anomaly flags. Operator IDs are supplied by callers, not authenticated. |
| Finance cash variance threshold | `OperationsStore.cash_variance_alert`; close response, day-end and anomaly reports; CLI close/report views. Tests cover positive/negative variance, exact ₹200 boundary, over-threshold amounts, and business-date reporting. | **PASS** for deterministic alert when absolute variance is greater than 20,000 paise. No email/notification mechanism is configured or claimed; Finance must review Admin & Management > Anomaly Monitoring or day-end. |
| Sub-10-second bill target | No production-like latency benchmark; local mock and startup tests cover function only. | **UNVERIFIED**: store Wi-Fi and production Shopify/SMT/terminal latency have not been measured; no performance claim. |
| Partial refunds, printable invoice/credit note, audit history (Should) | `RefundService`, `DocumentService`, `/api/audit-events`; `tests/test_operations.py`, `tests/test_documents.py`, `tests/test_http_app.py`. | **PASS** for local mock/service behavior; legal document controls and authenticated audit identity remain launch requirements. |
| Advisory anomaly flags (one Stretch) | `AnomalyService`, admin report; `tests/test_anomalies.py`, reporting/API tests. | **PASS** for advisory sphere-change and first-progressive checks. Off-grid axis is no longer an advisory rule; mandatory lens validation rejects it before checkout side effects. |
| Admin creates frames/lenses and receives frame stock | `/api/admin/catalog/products`, `/api/admin/inventory/receipts`, Shopify adapter methods; CLI and admin mock integrations in `tests/test_cli.py` and `tests/test_local_mock_integration.py`. | **PASS** in bundled mock; duplicate SKU/configuration, invalid location, and untracked stock are rejected. Lens products have no inventory item. Writes are audited with caller-supplied operator and receipt reference; same-reference receipt retries are idempotent. |
| Catalog/inventory shared with cashier and durable across restart | Both flows read/write Shopify adapter/mock; launcher supplies `--state-db`; `test_mock_sqlite_persists_customers_products_and_inventory_after_restart`. | **PASS** for standard local launcher. Direct mock invocation without `--state-db` remains intentionally ephemeral. |
| One-command local startup, readiness, safe cleanup | `scripts/start_local.py` checks occupied ports and HTTP health, tracks only child processes it starts; isolated-port startup/CLI exit smoke. | **PASS** for tested local startup. Production deployment/authentication is outside this local CLI scope. |

## Inventory and checkout safety

`ShopifyAdminClient.list_inventory_levels` uses the mock-supported
`GET inventory_levels.json?inventory_item_ids=...&location_ids=...` contract.
Before creating a durable sale or charging, `CheckoutService._validate_frame_inventory`
validates the selected active location and every tracked frame's item ID,
response rows and available quantity. Missing/malformed stock, unknown items,
wrong location and insufficient quantity fail closed. Lenses are made to
order and are exempt. Evidence includes inventory cases in
`tests/test_checkout.py` and the API-level
`test_checkout_api_rejects_out_of_stock_frame_before_persisting_sale_or_order`,
which checks no sale row or Shopify order is created.

Checkout requests are serialized by the running `CheckoutService` from
inventory validation through Shopify order creation. This prevents two
concurrent checkout requests to the same POS process from both passing a
last-unit check; `test_concurrent_checkout_cannot_sell_the_same_last_frame_twice`
verifies that only one terminal charge and order are produced. SQLite
`BEGIN IMMEDIATE` also serializes per-store invoice allocation, covered by
`test_concurrent_invoice_allocations_are_unique_and_sequential`. Shopify
independently enforces/decrements stock on order creation. An out-of-band stock
change or another POS process remains outside the in-process lock and can still
cause an order failure after payment; reconciliation remains necessary.

Checkout keys are bound to request fingerprints and persisted before payment.
The terminal's documented pre-write transient response uses the stable sale
reference; ambiguous payment and Shopify outcomes are not blindly replayed.
The supplied terminal cannot safely retry a declined attempt with the same
sale, so a new checkout intent is required.

Business dates are computed against the fixed `Asia/Kolkata` offset (UTC+05:30)
for aware UTC timestamps and naive local timestamps. The existing full report
tests exercise sale/refund attribution across this conversion, and
`tests/test_business_time.py` checks the UTC midnight boundary explicitly.
`date.today()` is not used to choose integration-test business dates.

Cash variance is calculated as counted minus expected integer paise. A closed
till triggers a Finance alert only for `abs(variance_paise) > 20000`; the
close response, day-end data and anomaly report expose store, session,
expected/counted cash, signed variance and business date. There is no outbound
email configured. Finance accesses alerts in the CLI's Admin & Management >
Anomaly Monitoring flow or by querying the day-end/anomaly APIs.

## Should features, stretch, and deferred work

The repository also implements persistent till sessions/movements, channel
capture/reporting, partial refunds, printable invoice/credit-note pages and
audit events. Tests for till/refund behavior are primarily deterministic
service tests; the new local-mock integration covers checkout, invoice,
frame refund/restock and day-end reporting together.

The formal stretch feature selected is deterministic, advisory billing-time
anomaly flags. They do not change prescriptions or block checkout. Manual
prescription entry is an additional local-mock utility requested during
development; it is not the brief's photo/free-text extraction with confidence
scores, and it is not supported by the read-only real-SMT contract. The mock
checks the named optometrist's store/role but does not authenticate that
person.

Family billing, remakes, the 10:00 email, offer authoring, queue/camera changes
and real external-service integration remain out of scope. Operator/staff
identifiers across the CLI are caller-supplied, not authenticated identities.

## Starter data ownership and use

`mock_server.L` reads seed JSON as UTF-8; module-level `S` initializes mock
runtime state. Its `do_GET`/`do_POST` handlers serve or mutate that state.
`http_app.default_application` separately reads POS-owned offer and tax
configuration. `generate_seed.py` writes the datasets and `validate_seed.py`
checks them; the validator is not evidence that a file is used by the runtime.

| File under `starter-kit/data/` | Actual classification |
|---|---|
| `locations.json` | Loaded into mock Shopify state `S["locations"]`; served by the locations route and used by POS store/location checks. |
| `customers.json` | Loaded into `S["customers"]`; used by Shopify customer search and registration. |
| `products.json` | Loaded into `S["products"]`; used by mock product/variant routes and Shopify adapter catalog, price, lens and inventory-item lookup. |
| `inventory_levels.json` | Loaded into `S["inventory"]`; returned by inventory lookup and changed by supported inventory/order/refund mock routes. |
| `orders_sample.json` | Loaded into `S["orders"]` as historical Shopify mock state and establishes the mock's global order-number sequence; also used as sample/test data. |
| `prescriptions.json` | Loaded into `S["rx"]`; served by the SMT prescription route. |
| `staff.json` | Loaded into `S["staff"]`; served by the SMT staff route. |
| `offers.json` | Read by `http_app.default_application` through `OfferBook.from_file` for POS offer evaluation. |
| `tax_rates.json` | Read by `http_app.default_application` through `TaxRateTable.from_file` for POS tax calculation. |
| `till_sessions_sample.json` | Historical/sample fixture only. `generate_seed.py` creates it and `validate_seed.py` checks sample arithmetic; `mock_server` does not load it and POS does not seed live tills from it. Live till sessions/movements are persisted in POS-owned SQLite. |

Shopify owns customers, locations, products, inventory, orders, transactions
and refunds. SMT owns prescriptions and staff. The POS owns offers, tax
configuration, checkout/till/audit state and local document numbering. The
sample order/till data remains distinct from live POS state. All ten JSON
files are also inputs to seed validation.

## Documentation and repository hygiene

`README.md` is the primary run guide and documents the CLI walkthrough, test
commands, data ownership, API, failure reproductions and mock limits.
`.gitignore` excludes Python caches, SQLite databases and sidecars, virtual
environments, environment files, coverage/build output and common editor/OS
files. It also excludes `*.log`, local `.env.*` files (except `.env.example`),
private-key/certificate file extensions, and `secrets.*`/`credentials.*`.

The repository is a Git checkout on `main`, aligned with `origin/main`; before
this audit-document update the worktree was clean and all project files were
tracked. This document is the only intentional modification. No commit or
push was made. Two ignored SQLite database files and POS SQLite WAL/SHM
sidecars are present in the workspace; they were preserved and are excluded
by `.gitignore`. The POS-owned ledger may contain customer details and should
not be published. A scan of source/configuration paths found no credential
values or tracked database, cache, environment, or key files. Python bytecode
caches are ignored and are not deliverables.

The final launcher smoke used dynamically allocated ports and temporary POS
and mock databases. It reached both health checks, displayed the cashier menu,
accepted option `0`, and exited successfully. The workspace databases were
not used or changed by that smoke.

## Verification from this run

| Command / check | Result |
|---|---|
| `python -m unittest discover -s tests -v` with `TZ=UTC` | Exit 0; 205 tests passed. |
| `python -m unittest discover -s tests -v` with `TZ=Asia/Kolkata` | Exit 0; 205 tests passed. Business dates use explicit Bengaluru timezone conversion; boundary cases are tested. |
| `python -m unittest tests.test_start_local tests.test_cli tests.test_local_mock_integration -v` | Exit 0; 58 tests passed, including cashier/admin catalog navigation, product creation and stock receipt visibility, checkout eligibility, mock persistence, and startup behavior. |
| `python starter-kit/validate_seed.py` | Exit 0; 8 locations, 28 staff, 30 customers, 47 prescriptions, 92 variants, 8 offers, 320 inventory rows, 12 sample orders and 2 sample till sessions; seed consistent. |
| `python -m compileall -q eluno_pos scripts tests` | Exit 0. |
| Python 3.8 grammar compatibility | Python's `ast.parse(feature_version=(3, 8))` passed for all 45 Python files. Runtime tests used Python 3.14; a Python 3.8 runtime was not available. |
| `scripts/start_local.py` isolated smoke | Exit 0 with dynamically allocated ports and temporary databases. Both services passed launcher health checks, the CLI accepted option `0`, and the launcher stopped its child services. |
| Git hygiene | `git status --short --branch` now reports only `M FINAL_ASSIGNMENT_AUDIT.md`; `git diff --check` passes. Tracked-path scan found no local databases, caches, environment files, or key files. |

## Remaining launch risks

The local mock cannot verify production Shopify/SMT/terminal contracts,
authentication, legal invoice/credit-note controls, email delivery, or
high-volume pagination. The mock's order listing is bounded and non-paginated.
Checkout serialization prevents concurrent local checkouts in one application
process from overselling the same last unit, but does not coordinate separate
POS processes or other inventory writers. Ambiguous Shopify order/refund
writes require an operator to reconcile outside the app. Existing ignored
workspace databases and their sidecars were preserved; isolated startup
verification used temporary databases.

## Submission verdict

The assignment's local workflows and verification pass in this checkout,
including both timezone runs, the mandatory 5-degree axis validation, catalog
creation-to-checkout coverage, and isolated startup/shutdown. No source defect
requiring a fix was confirmed during this audit. **Ready to submit as an
exercise implementation**, with the mock/production limitations above
disclosed. This is not a production-readiness approval: the sub-10-second
performance target, real external integrations, and production controls
remain unverified.
