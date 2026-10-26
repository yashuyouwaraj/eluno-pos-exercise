# Eluno — AI-first Engineer exercise

**Role:** AI-first Engineer (product-minded software engineer)
**Time:** 48 hours from the email that sent you this. Plan for roughly a working day of effort inside that window.
**Submit:** a Git repo link and a short Loom. Details in §6.

This exercise is a slice of the first project the person we hire will own. We'd rather show you the real thing than a toy.

---

## 1. About Eluno

Eluno is a premium eyewear brand with seven stores in Bengaluru and an online store. A typical day across stores is around 350–400 walk-ins, about 120 bills, and an average bill near ₹5,400. Most bills are a frame plus a prescription lens configured for that customer's eyes. Lenses are made to order at our lab; frames are stocked per store.

Our stores run on tools we built: a store-management tool for the walk-in queue, eye tests, prescriptions and frame measurements; an order-management system for the lab, QC and dispatch; and a WhatsApp assistant for leads. Billing is the one piece still on **Shopify POS**, and it's holding us back: offers are applied by hand, there's no cash-drawer tracking, no sales-channel view, and no day-end report that finance trusts.

**Shopify itself stays.** It is our system of record for customers, products, inventory, orders, payments and refunds, and finance reconciles there. What changes is the app at the till.

## 2. Your mission

Build v1 of the till app that replaces Shopify POS in Eluno stores. Every order, payment transaction, refund and restock it produces must land in Shopify through the Admin API, in the shape Shopify expects. Prescriptions come from the store tool; cash till, invoice series, offers and day-end reporting are yours.

The starter kit gives you a local mock of Shopify Admin, the store tool, and a payment terminal, plus seed data in those shapes. You never need a Shopify account.

## 3. Scope

Build the **Must** tier end to end before anything else. A thin Must that works beats a wide Should that doesn't.

**Must**
1. Look up a customer by phone (Shopify) and fetch their latest valid prescription (store tool).
2. Build a cart with a frame and a lens. The lens comes from the lens matrix (type × index × coating) and is **validated against the prescription**: sphere and cylinder within the lens's limits, an ADD present for progressive/bifocal. Block invalid choices with a reason the sales staff can act on.
3. Apply offers from `offers.json` by their rules: dates, conditions, stackability.
4. Compute GST per line by HSN. Prices are inclusive of GST; the invoice shows taxable value, CGST and SGST per line, reconciled to the paisa.
5. Charge the amount on the terminal, then **create the order in Shopify** with line items, tax lines, discounts, the sale transaction, the prescription carried on the lens line, and a **per-store invoice number** that is not Shopify's order number.
6. **Full refund** through Shopify's refund flow: tax reversed per line, frame restocked at the store that billed it, lens not restocked, refund transaction recorded against the sale.
7. **Day-end report** for a store and a date, computed from Shopify orders: bill count, gross, taxable, tax, discounts, refunds, and a split by payment method.

A CLI or an HTTP API is fine. A screen is not required and is not scored.

If you run out of time inside Must, stop cleanly: leave the code runnable, and say in the README exactly where you stopped and why. That is graded; an unfinished tier is not penalised on its own.

**Should** (pick what you can justify)
- Cash till: open a session with a float, record cash sales, refunds, paid-ins and paid-outs with reasons, close with a counted amount, report the variance.
- Sales channel on every order (walk-in, WhatsApp-booked, online pickup) and a channel report by store.
- Partial refund (frame only, or lens only).
- Printable invoice (HTML or PDF).
- Audit trail: who did what, when, with what reason code.

**Stretch** (at most one, only after Must is solid)
- Offer authoring from plain English — "20% off the second progressive pair for priority customers till Diwali" — into a validated offer rule, with guardrails and a human review step.
- Prescription entry assist: a photo or free text of an external prescription into a structured prescription with per-field confidence, flagged for the optometrist to confirm.
- Billing-time anomaly flags: sphere changed by more than 1.00 D since the last prescription, an axis not on the 5° grid, a progressive lens for a first-time wearer.

## 4. Constraints

- Runs locally with one command after cloning. Document it; we will run it.
- Your domain logic must not depend on Shopify's types. Shopify is reached through an explicit adapter. We will ask what happens if Shopify is replaced in 2027.
- Tax rates and offers are data. Changing a rate or an offer must not require a code change.
- Money is integer paise inside your code; Shopify takes decimal strings at the boundary.
- The mock returns `429` and `503` sometimes. Handle them. Creating the same order twice after a timeout must not create two orders.
- Tests exist for tax, discount and refund math at minimum.
- Any language or framework. The mock server needs Python 3.8+; your own stack is up to you.

## 5. What you submit

**Part A — before writing code (about 45 minutes).** Read `STAKEHOLDER_THREAD.md`. Write `STAKEHOLDER_REPLY.md` in your repo:
1. The questions you'd ask, to whom, and why each matters for v1.
2. What you are building in this exercise and what you are deferring, with reasons.
3. Anything in the thread that is contradictory, risky or out of scope, and how you'd handle it.
4. What you'd need from other people and systems to ship v1 for real.

Keep it under two pages. Write it for the people on the thread. It carries as much weight as the code.

**Part B — the code.** Any architecture. We look at the domain model (prescription, lens configuration, cart, offers, invoice, refund, till), the boundary between your core and the Shopify adapter, how you handle rounding, stacking, retries and idempotency, your tests, and your README (how to run, what's built, what's next, known gaps).

**Part C — a Loom, up to 8 minutes.** Demo the Must flow end to end. Walk through two or three decisions you're glad you made and one you'd revisit. Say where AI tools helped you and where they misled you. Use them freely; we do. We'll ask about it.

## 6. Submission

- Push to a Git repo. If it's private, invite the address in the email.
- The repo contains `README.md`, `STAKEHOLDER_REPLY.md`, your code and tests.
- Reply to the email with the repo link and Loom link before the 48 hours are up.
- If something comes up and you need more time, say so as early as you can.
- Questions: reply to the email at any time during the window. We answer within two hours between 09:00 and 21:00 IST.
- Your code remains yours. We will not use it, in whole or in part, outside this evaluation.

## 7. What happens next

Every submission is read against the same written rubric: problem framing, scoping, domain model, engineering quality, judgement about where AI belongs, communication, initiative. Shortlisted candidates have a 60-minute call where we go through the submission together and extend it on a whiteboard, then a conversation with the founder.

If you join, this is your first project. Everything you learn here carries over.

Questions during the 48 hours are welcome and don't count against you. Asking good ones is part of the job.
