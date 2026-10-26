# External failure matrix

| Scenario | Expected durable/API behavior | Retry / recovery | Evidence | Status |
|---|---|---|---|---|
| Dependency connection refused | `HttpRequestError` with no HTTP status; checkout cannot continue | GET retries are bounded; writes are not retried after transport loss | `test_local_mock_integration` | tested against an unused loopback port |
| Terminal 503 before charge | bounded adapter retry using the stable `order_ref` | safe only for documented 429/503 pre-write responses | `test_adapters`, `test_checkout` | tested using test double |
| Terminal response loss | `PAYMENT_UNKNOWN`, no second charge | operator inspection only; never blind retry | `test_checkout` | tested using test double |
| Shopify 429 / 503 | bounded adapter retry only where adapter declares it safe | 429 create retry; no retry after transport loss | `test_adapters` | tested using test double |
| Shopify order response loss / restart | `ORDER_UNKNOWN`, payment remains persisted | operator reconciliation; no order replay | `test_checkout` | tested using test double |
| Refund response loss | `REFUND_UNKNOWN` | never replayed; recovery reports state | `test_operations` | tested using test double |
| Duplicate refund key / partial quantity | same local record / reject excess quantity | idempotent locally, quantity checked | `test_operations` | tested using test double |
| Cash refund posting failure | refund remains completed with local posting pending | retries cash movement only, never Shopify refund | `test_operations` | tested using test double |
| Completed cash sale/refund has pending till posting | day-end exceptions and anomaly report identify the sale/refund and till session | recovery retries only the local cash movement; never payment or Shopify writes | `test_reporting`, `test_anomalies`, `test_http_app` | deterministic service/API tests |
| Customer/SMT unavailable or malformed | lookup fails closed; no bill built from unverified data | operator resolves dependency | adapter/service tests | tested using test double |
| No-flake local mock charge | captured charge can be read back | normal success | `test_terminal_charge_and_lookup_use_real_mock` | tested against local mock |
| Invalid progressive prescription | checkout rejects missing ADD before sale persistence, payment or Shopify order | correct/confirm the prescription before adding the lens | `test_checkout_api_rejects_progressive_without_add_before_payment_or_order` | checkout/API path against local mocks; sentinels verify charge/order are not called |
| Missing, malformed, out-of-range or off-grid lens axis | right and left eyes are validated independently; axis must be an integer in the inclusive 0–180 range and divisible by 5 when present; a missing axis is allowed only for zero cylinder. Checkout rejects invalid axes before sale persistence, payment, Shopify order creation or invoice issuance; no rounding occurs. | correct or confirm the source prescription with the optometrist | `test_lens_validation.py`, `test_checkout.py`, `test_lens_validation_endpoint_rejects_off_grid_and_fractional_axis` | focused validator, checkout and HTTP tests; charge/order spies and absent sale record verify early rejection |
| Out-of-stock frame | checkout rejects insufficient stock before sale persistence, payment or Shopify order | check stock at the selected store and choose an available frame | `test_checkout_api_rejects_out_of_stock_frame_before_persisting_sale_or_order` | checkout/API path against local mock |
| Successful sale, invoice, refund, restock and day-end | saved invoice and Shopify order/refund are consistent; report includes sale and refund | reopen by sale ID/invoice number and review report exceptions | `test_checkout_invoice_refund_restock_and_day_end_use_real_local_mock` | full frame-refund round-trip against local mock with isolated POS SQLite |
| Opt-in local mock payment readiness | fixed Rs. 1.00 UPI mock charge is read back; no sale/order/ledger write | run status checks after uncertain result; do not repeat the charge blindly | `test_http_app`, `test_cli`, deterministic startup smoke | verified only for the supplied loopback mock |
| Admin creates frame or made-to-order lens | Shopify-shaped catalog record is validated and immediately appears in cashier catalog; duplicate SKU/configuration is rejected | resolve any ambiguous creation by searching the SKU before retrying | `test_admin_catalog_creation_and_stock_receipt_are_visible_to_cashier_catalog` | local mock integration |
| Admin receives frame stock | item is connected to an active store, quantity increases in shared inventory source, and POS audit records operator/reference; same item/store/reference retries are idempotent and mismatched reuse is rejected | reuse the exact delivery reference on retry; inspect quantity if the outcome is ambiguous | `test_admin_catalog_creation_and_stock_receipt_are_visible_to_cashier_catalog`, `test_mock_sqlite_persists_customers_products_and_inventory_after_restart` | local mock integration |
| Till closes more than Rs. 200.00 out of balance | close response, day-end and anomaly report include an alert with store, session, expected/counted cash, signed variance, and Bengaluru business date; exactly Rs. 200.00 is not alerted | Finance reviews Admin & Management > Anomaly Monitoring or the day-end report; no email is sent | `test_cash_variance_alert_strictly_exceeds_two_hundred_rupees`, `test_closed_till_cash_variance_is_returned_in_close_day_end_and_anomaly_reports` | service and local API integration |
| Anomaly-report Shopify dependency unavailable | controlled HTTP 502 response; no uncaught handler failure | retry after checking the Shopify connection | `test_anomaly_report_returns_controlled_error_when_shopify_is_unavailable` | tested using service test double |
| Mock state across restart | customer, catalog and inventory records reload from mock SQLite | preserve `eluno_pos_mock_state.sqlite3`; omit `--state-db` only for a disposable in-memory mock | `test_mock_sqlite_persists_customers_products_and_inventory_after_restart` | verified with an isolated temporary database |
| Order/refund accepted then response lost | mock has no deterministic control for this | unsafe to retry | not available | unsupported by supplied mock |
| SQLite disk/full corruption | no injectable mock/storage fault contract | no claim made | not available | not tested |

The standard launcher gives the supplied mock a SQLite file; running
`mock_server.py` directly without `--state-db` remains in-memory. Persistence
does not add Shopify operation lookup or idempotent Shopify order/refund
creation. Local mock results are evidence only for this exercise, not proof of
production reliability. Inventory receipts are stock increases, not a
production purchasing/receiving integration; after an ambiguous remote write,
inspect current quantities before attempting another receipt.
