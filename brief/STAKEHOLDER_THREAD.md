# Stakeholder thread

*Read this before Part A. It is a fictionalised version of how requirements arrive here; the people are invented, the situations are not. Treat every message as real input from a real colleague. Some of it conflicts, some is out of scope, some is urgent. Your reply turns it into something a small team can act on.*

---

**Subject: [Retail-Requirement] Replace Shopify POS in stores — live by 15 Nov**

**From: Rohan (Founder) · To: Product-Tech · Cc: Retail, Optometry, Finance, Ops**

We are replacing Shopify POS at the till. Shopify stays as the backend; everything must still show up in Shopify admin because finance reconciles there and the website runs on it. New till app live in all 7 stores by 15 Nov. Non-negotiables:

1. Bill should be done in under 10 seconds from the moment the customer says yes.
2. Offers must work from day one. Current Shopify discounts are a mess; half the stores apply them manually.
3. Priority customers and campaign walk-ins must be flagged on the billing screen, same as in the queue tool.
4. No more "system was slow" excuses from stores.

Priya will coordinate. Start now.

---

**From: Meena (Retail Head)**

Adding what the stores need:

- Offers should be **stackable**. Festive 10% + second pair 50% + priority ₹500 should all go together, that's how we pitch it on the floor.
- Store managers need a **manager discount** button, up to 10%, for walk-outs. No approval chain, it kills the sale.
- **Family billing**: one bill for a family of 3 with 3 different prescriptions. Today we make 3 bills and the customer hates it.
- I need to see **where each bill came from**: walk-in, WhatsApp-booked, or online order picked up in store. By store, daily.
- Stores take cash out of the drawer for courier and tea. That has to be recorded somewhere or finance shouts at us.
- Also the beverage question in the queue tool should be removed, it's annoying. And the footfall camera at Indiranagar is counting wrong again, someone please fix.

---

**From: Dr. Iyer (Optometry Manager)**

Hello Priya,

From the clinical side, three things that must be in the billing flow, otherwise we get remakes:

1. The system should **not allow** a lens outside the prescription range. Today staff pick 1.56 index for a −6.00 customer and it comes back from the lab.
2. Axis options should be in **5-degree intervals**. 1-degree options confuse the team and cause entry mistakes.
3. **Remake flow**: when a progressive customer can't adapt, or an external prescription was wrong, we remake the lens. The frame must not be billed again. Who bears the lab cost (us, the customer, or the external optometrist) is decided case by case right now.

Thanks & Regards,
Dr. Iyer

---

**From: Vikram (Finance)**

Requirements from compliance. These are not optional.

- GST per line by HSN. Frames, lenses and sunglasses have different rates. Rates change; last time they changed we had to re-issue invoices.
- **Invoice numbers sequential per store, no gaps.** Shopify's order numbers are one global series across stores and the website, so they cannot be our invoice numbers. A cancelled invoice stays in the series as cancelled.
- Refunds reverse the tax on the refunded lines with a credit note referencing the original invoice. The refund must exist in Shopify, not only in the till.
- **No manual discount without a reason code and the approver's identity.** Auditors ask for this.
- Prices on the floor are MRP inclusive of GST. The invoice shows taxable value and tax separately.
- **Day-end per store must match Shopify to the rupee.** Cash drawer counted at close; variance over ₹200 flagged to me the same night.

---

**From: Farhan (Ops / Fulfilment)**

From the lab and ops side:

1. On a refund, the **frame must restock to the store that billed it** in Shopify, not HQ. Lenses are made to order; they're scrapped, not restocked.
2. On a remake, don't create a new frame line and don't touch frame stock. We've had double-reservations lock up stock for days.
3. Store Wi-Fi drops mid-bill (Koramangala, every other week). The bill shouldn't vanish, and it definitely shouldn't get created twice in Shopify when the connection comes back.

---

**From: Rohan (Founder)**

Re Meena's points, yes to all. Re Finance: fine, but don't slow down billing. 10 seconds.

Also: every morning at 10 am I want an email with yesterday's bills by store, average bill value, and discount given per store. Dev, can you do this.

---

**From: Priya (Product)**

Team, acknowledged. I'll come back with a scoped plan for v1 and the questions we need answered before we build.

*(This is where you come in. Write that reply.)*
