# Re: Replace Shopify POS in stores — proposed v1 scope

Hi Rohan, Priya, Meena, Dr. Iyer, Vikram and Farhan,

I would make correctness and recoverability the v1 priorities. Shopify remains
the record for customers, catalog, inventory, orders, transactions and
refunds. The POS owns offer/tax configuration, per-store invoice allocation,
till state and the local record needed to recover an interrupted checkout.
The requested sub-10-second bill is a target, not a verified result: the local
mock tests do not represent store Wi-Fi or production-service latency, so I
would not promise it until the full path is measured under representative
conditions.

## Scope and trade-offs

**Must:** look up the customer and latest valid prescription; build a cart
from active catalog variants; validate lens range, required ADD, and the
prescribed axis in 5-degree increments; calculate
eligible offers and GST-inclusive line tax in paise; check frame stock at the
selected store; charge the terminal; create a Shopify order carrying its
transaction, tax, discount, prescription reference and POS invoice number;
support refunds with tax reversal, frame restock at the billing store and no
lens restock; and report sales/refunds by store and business date.

**Should work in this exercise:** cash till movements and close variance,
sales channels, partial refunds, printable documents and audit events. A
deterministic Finance alert is included when a closed till's absolute variance
is greater than ₹200 (20,000 paise). It appears in the close response,
day-end report and anomaly report with store, till, expected/counted cash,
signed variance and Bengaluru business date; exactly ₹200 does not trigger.
No email notification is implemented or claimed. These are not production
controls: operator IDs are caller-supplied, not authenticated.

For priority/campaign billing flags, the cashier resolves tags from the
authoritative Shopify customer lookup, ignores caller-supplied tags, and
displays priority and `campaign:*` tags separately from sales channel. The
provided Shopify mock has customer tags. The supplied store-tool API does not
expose queue state or campaign-walk-in flags, so anonymous campaign walk-ins
cannot safely be flagged; no flag is inferred.

**One formal stretch:** deterministic, advisory billing-time anomaly flags.
They ask staff to review a prescription change or first progressive selection;
they do not change the prescription or block checkout. Axis-grid validation is
mandatory rather than advisory: an off-grid or out-of-range value is rejected
before payment or Shopify order creation, without rounding or changing the
prescription. A missing axis is accepted only when cylinder is zero.
The CLI also has a manually entered, optometrist-confirmed prescription path
for the bundled mock only. That is a development utility requested during the
exercise—not the brief's photo/free-text extraction feature, and not a
production store-tool write integration.

Family billing, remakes, the daily email, offer authoring, queue beverage
changes and the footfall camera are deferred or outside the till. A remake
needs a separate order/stock policy because cost ownership is undecided.

Offers follow the supplied rules; I would not stack a non-stackable offer just
because a store requests it. The implementation compares eligible compatible
combinations, maximizes customer discount, then uses priority and offer ID as
tie-breakers. Manager overrides remain disabled until Finance and Product
define reason codes, approver identity and authorization.

## Outages and invoice handling

The POS saves a checkout intent and stable sale reference before external
work. It reuses that reference only for terminal responses documented as safe
to retry. After capture, it records the charge before creating the Shopify
order. A lost/ambiguous response is retained as unknown and is never blindly
charged or submitted again. The supplied Shopify mock has no order-operation
lookup or create idempotency, so an uncertain order needs operator
reconciliation outside this app.

Invoice numbers are allocated per store and never reused. A declined payment
leaves a void number; an invoice is presented as issued only after Shopify
order creation succeeds. Finance still needs to approve legal wording and the
process for reconciling gaps/uncertain outcomes.

## Questions before a real rollout

- **Product/Retail:** confirm offer priority/stacking policy, channel
  definitions and the family-billing customer/prescription model.
- **Finance/Product:** approve invoice and credit-note wording, void/recovery
  handling, manager-discount controls, refund reasons and the day-end
  reconciliation/variance owner.
- **Optometry/Ops:** confirm external axis handling, remake approvals and
  cost ownership, and the lab handoff. No remake should rebill or reserve a
  frame.
- **Retail/Ops:** define the Wi-Fi outage procedure, unresolved-sale owner and
  store-close escalation.
- **Retail/SMT:** expose an authenticated queue lookup or campaign-walk-in
  signal, with customer/visit identity, freshness and opt-out rules, before
  expecting the POS to flag campaign visitors who are not matched to a tagged
  Shopify customer.
- **Founder/Product/Finance:** define recipients and contents of the 10:00
  email, and whether it can run only after day-end figures are reconciled.

## Needed for launch

We need supported Shopify, store-tool and terminal APIs and credentials;
staff authentication/roles; approved store, tax and offer data; Finance's
invoice/refund policies; outage and cash-close procedures; monitoring and an
escalation owner. A pilot should measure latency on store-like Wi-Fi and
reconcile each store's orders, payments, refunds, stock and day-end totals
before rollout.

Regards,  
Product-Tech
