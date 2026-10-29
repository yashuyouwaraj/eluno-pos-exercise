import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const customer = {
  id: 7401,
  phone: "+919876543210",
  first_name: "Mira",
  last_name: "Rao",
  tags: ["retail"],
};
const prescription = {
  rx_id: "RX-7401",
  customer_id: 7401,
  phone: customer.phone,
  date: "2026-01-01",
  valid_until: "2027-01-01",
  od: { sph: "-1.00", cyl: "-0.50", axis: 90, add: null },
  os: { sph: "-1.00", cyl: "-0.50", axis: 90, add: null },
};
const frame = {
  variant_id: "frame-100",
  line_type: "frame",
  product_title: "North Frame",
  variant_title: "Black",
  price_paise: 120000,
  lens_type: null,
  index: null,
  coating: null,
};
const lens = {
  variant_id: "lens-200",
  line_type: "lens",
  product_title: "Single Vision",
  variant_title: "1.50 · standard",
  price_paise: 50000,
  lens_type: "single_vision",
  index: "1.50",
  coating: "standard",
};
const pricing = {
  gross_paise: 120000,
  discount_paise: 10000,
  taxable_paise: 98214,
  cgst_paise: 5893,
  sgst_paise: 5893,
  payable_paise: 110000,
  lines: [{
    variant_id: frame.variant_id,
    quantity: 1,
    gross_paise: 120000,
    discount_paise: 10000,
    taxable_paise: 98214,
    cgst_paise: 5893,
    sgst_paise: 5893,
    tax_paise: 11786,
    payable_paise: 110000,
  }],
  offers: [{ offer_id: "OFFER-10", discount_paise: 10000 }],
  offer_evaluation: [{
    offer_id: "OFFER-10",
    name: "Pair offer",
    stackable: true,
    status: "applied",
    reason: "Selected by offer policy.",
    discount_paise: 10000,
  }],
};

function installApi(overrides: Record<string, unknown> = {}) {
  const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
  let createdCustomer: typeof customer | null = null;
  let priceCount = 0;
  let storeOrders = (overrides.storeOrders as Array<Record<string, unknown>> | undefined) || [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
    calls.push({ url, body });
    let data: unknown = {};
    let status = 200;
    if (url === "/api/auth/me") {
      if (overrides.authRequired) {
        status = 401;
        data = { message: "Sign in to continue to Eluno." };
      } else data = { user: {
        username: "test-user", staff_id: overrides.staffId || "S105", operator_id: overrides.staffId || "S105",
        store_code: "HSR", role: overrides.userRole || "cashier", display_name: overrides.displayName || "Test User",
      } };
    }
    else if (url === "/api/customer-auth/me") {
      if (overrides.customerAuthenticated) data = { customer: {
        customer_id: "customer-1", shopify_customer_id: "7401",
        name: "Mira Rao", phone: customer.phone, email: "mira@example.com",
        address: overrides.customerAddress || {
          line1: "12 Retail Road", line2: "", city: "Bengaluru",
          state: "Karnataka", postal_code: "560001",
        },
      } };
      else { status = 401; data = { message: "Sign in to your customer account." }; }
    }
    else if (url === "/api/customer-auth/register") data = {
      message: "Customer account created. Sign in to continue.",
      customer: { customer_id: "customer-1", shopify_customer_id: "8402", name: body.name, phone: body.phone, email: body.email || null, address: {} },
    };
    else if (url === "/api/prescriptions" && init?.method === "POST") {
      status = 201;
      data = {
        prescription: {
          ...prescription,
          customer_id: createdCustomer?.id || customer.id,
          phone: createdCustomer?.phone || customer.phone,
          date: body.date,
          valid_until: body.valid_until,
          od: body.od,
          os: body.os,
          optometrist_id: body.optometrist_id,
          store_code: body.store_code,
        },
        warnings: [],
      };
    }
    else if (url.startsWith("/api/prescriptions")) data = { prescriptions: [] };
    else if (url === "/api/customer/locations") data = {
      locations: [{ id: 7, name: "HSR Store", store_code: "HSR" }],
    };
    else if (url === "/api/customer/catalog") data = { variants: overrides.customerCatalog || [frame, lens] };
    else if (url === "/api/customer/pricing") data = {
      ...pricing,
      ...(overrides.customerPricing as Partial<typeof pricing> | undefined),
      tax_paise: 11786,
      store_code: "HSR",
      location_id: "7",
      business_date: "2026-10-30",
    };
    else if (url === "/api/customer/orders" && init?.method === "POST") {
      data = { order: { order_id: "web-created", payment_status: "awaiting_payment" }, duplicate: false };
      status = 201;
    }
    else if (url.startsWith("/api/customer/orders")) data = {
      orders: overrides.customerOrders || [], page: 1, has_more: false,
    };
    else if (url === "/api/store/orders/pending") data = { orders: storeOrders };
    else if (url.startsWith("/api/store/orders/")) {
      const [, , , , orderId, decision] = url.split("/");
      data = { order: {
        order_id: orderId,
        status: decision === "deny" ? "cancelled" : "awaiting_payment",
        store_confirmation_status: decision === "deny" ? "denied" : "confirmed",
        store_decision_by: "S105",
        store_decision_at: "2026-10-30T12:00:00+00:00",
        status_message: decision === "deny" ? "No payment was captured." : "Payment remains pending.",
      } };
      storeOrders = storeOrders.filter((item) => item.order_id !== orderId);
    }
    else if (url === "/api/optometrist/orders/assistance") data = overrides.optometristAssistance || { orders: [] };
    else if (url === "/api/auth/login") data = {
      user: {
        username: String(body.username), staff_id: overrides.staffId || "S105", operator_id: overrides.staffId || "S105",
        store_code: "HSR", role: overrides.userRole || "cashier", display_name: overrides.displayName || "Test User",
      },
    };
    else if (url === "/api/business-date") data = { business_date: "2026-10-29", timezone: "Asia/Kolkata" };
    else if (url === "/api/auth/logout") data = {};
    else if (url === "/api/admin/staff" && init?.method === "POST") {
      status = 201;
      data = { staff: {
        staff_id: "staff-new", username: String(body.username), display_name: String(body.display_name),
        operator_id: "staff-new", store_code: body.store_code,
        role: body.role, is_active: true, created_at: "2026-10-29T12:00:00+00:00",
        updated_at: "2026-10-29T12:00:00+00:00",
      } };
    }
    else if (url.startsWith("/api/admin/staff/") && init?.method === "PATCH") {
      const staffId = decodeURIComponent(url.split("/").pop() || "");
      data = { staff: {
        staff_id: staffId, username: staffId === "staff-new" ? "new.cashier" : "cashier",
        display_name: staffId === "staff-new" ? "New Cashier" : "Demo Cashier",
        role: "cashier", is_active: body.is_active ?? true,
        operator_id: staffId, store_code: "HSR",
        created_at: "2026-10-29T12:00:00+00:00", updated_at: "2026-10-29T12:00:00+00:00",
      } };
    }
    else if (url === "/api/admin/staff") {
      if (overrides.staffUnauthorized) {
        status = 401;
        data = { message: "Your session expired." };
      } else data = { staff: [
        { staff_id: "staff-admin", username: "admin", display_name: "Demo Admin", role: "admin", is_active: true, created_at: "2026-10-29T12:00:00+00:00", updated_at: "2026-10-29T12:00:00+00:00" },
        { staff_id: "staff-cashier", username: "cashier", display_name: "Demo Cashier", role: "cashier", is_active: true, created_at: "2026-10-29T12:00:00+00:00", updated_at: "2026-10-29T12:00:00+00:00" },
      ] };
    }
    else if (url.startsWith("/api/admin/tills") || url.startsWith("/api/tills")) data = { sessions: [], tills: [] };
    else if (url.startsWith("/api/locations")) data = { locations: [{ id: 7, name: "HSR Store", store_code: "HSR" }] };
    else if (url.startsWith("/api/prescription-approvers")) data = {
      approvers: overrides.optometristApprovers || [{ staff_id: "S107", display_name: "Demo Optometrist" }],
    };
    else if (url.startsWith("/api/admin/catalog/frames")) data = { variants: [{
      variant_id: 501, inventory_item_id: 601, product_title: "North Frame",
      variant_title: "Black", sku: "NORTH-BLK", available: 2, location_id: 7,
    }] };
    else if (url === "/api/admin/catalog/products" && init?.method === "POST") data = { product: { id: 701 } };
    else if (url.startsWith("/api/admin/catalog/products/") && init?.method === "PATCH") {
      if (overrides.catalogUpdateFailure) {
        status = 422;
        data = { message: "Catalog update could not be saved." };
      } else data = { product: { id: 701, status: body.status || "active" } };
    }
    else if (url.startsWith("/api/admin/catalog/products")) data = {
      products: overrides.catalogProducts || [],
    };
    else if (url === "/api/admin/inventory/receipts") data = {
      received_quantity: body.quantity || 1, idempotent_replay: false,
    };
    else if (url.startsWith("/api/catalog/variants")) data = {
      variants: [url.includes("line_type=lens") ? lens : frame],
    };
    else if (url.startsWith("/api/offers")) data = { offers: [{ offer_id: "OFFER-10", name: "Pair offer", stackable: true }] };
    else if (url.startsWith("/api/customer-prescription")) {
      if (createdCustomer) data = { status: "no_valid_prescription", message: "No prescription on file.", customer: createdCustomer };
      else if (overrides.customerNotFound) {
        status = 404;
        data = { status: "customer_not_found", message: "No customer matches this mobile number." };
      } else data = {
        status: "ready",
        message: "Ready",
        customer,
        prescription: overrides.prescription || prescription,
      };
    }
    else if (url.startsWith("/api/customers/search")) data = {
      customers: overrides.customerSearchResults || [{
        id: "99000004", name: "Yashu Youwaraj", phone: "+917970629433",
        email: "yashu@example.com", total_spent_paise: 0, order_count: 0,
        total_refunded_paise: 0, most_recent_purchase: null, created_at: null,
      }],
      page: 1,
      has_more: false,
    };
    else if (url.startsWith("/api/customers/")) data = {
      customer: {
        ...customer, id: 99000004, first_name: "Yashu", last_name: "Youwaraj",
        phone: "+917970629433", email: "yashu@example.com",
      },
      purchase_count: 0, lifetime_spend_paise: 0, most_recent_purchase: null,
      history_complete: true, purchases: [], prescriptions: [],
      deletion_policy: "Retain transactional records.",
    };
    else if (url === "/api/customers") {
      if (overrides.registrationStatus) {
        status = Number(overrides.registrationStatus);
        data = { message: String(overrides.registrationMessage || "Customer registration failed.") };
      } else {
        createdCustomer = {
          ...customer,
          id: 8402,
          phone: String(body.phone),
          first_name: String(body.name).split(" ")[0],
          last_name: String(body.name).split(" ").slice(1).join(" "),
        };
        data = { customer: createdCustomer, created: true };
        status = 201;
      }
    }
    else if (url === "/api/lens-validation") data = {
      valid: true,
      errors: [],
      warnings: ["OD axis 91 is off the 5-degree grid and requires optometrist review."],
      variant_id: lens.variant_id,
    };
    else if (url === "/api/pricing-preview") {
      priceCount += 1;
      data = overrides.priceHandler
        ? await (overrides.priceHandler as (payload: Record<string, unknown>, index: number) => Promise<unknown> | unknown)(body, priceCount)
        : overrides.pricing || pricing;
    }
    else if (url === "/api/checkouts") data = overrides.checkout || {
      sale_id: "sale-12",
      status: "COMPLETED",
      order_id: "981237650001",
      invoice_no: "ELU/HSR/FY26/00012",
    };
    else if (url.startsWith("/api/invoices/")) {
      if (overrides.invoiceFailure) {
        status = 503;
        data = { message: "Saved invoice is temporarily unavailable." };
      } else {
        return new Response(
          "<!doctype html><title>Invoice ELU/HSR/FY26/00012</title><p>North Frame · Qty 1 · Discount ₹100.00 · CGST ₹59.00 · SGST ₹59.00 · Total ₹1,100.00</p>",
          { status, headers: { "Content-Type": "text/html; charset=utf-8" } },
        );
      }
    }
    else if (url.startsWith("/api/refunds/lookup")) data = overrides.refundPreview || {
      order_id: "981237650001",
      invoice_no: "ELU/HSR/FY26/00012",
      name: "#1042",
      customer_name: "Mira Rao",
      store_code: "HSR",
      financial_status: "paid",
      total_paise: 120000,
      refunded_paise: 0,
      remaining_refundable_paise: 120000,
      refund_count: 0,
      created_at: "2026-10-29T12:00:00+05:30",
      payment_gateway: "razorpay_terminal",
      previous_refunds: [],
      available_tills: [],
      refund_eligible: true,
      ineligible_reasons: [],
      payment_capability: "mock_only",
      lines: [{
        line_item_id: "line-1",
        title: "North Frame",
        quantity: 1,
        refunded_quantity: 0,
        remaining_quantity: 1,
        line_refundable_paise: 120000,
        discount_paise: 0,
        tax_paise: 0,
        remaining_tax_paise: 0,
        restockable: true,
      }],
    };
    else if (url === "/api/refunds") data = overrides.refundSubmission || { refund_id: "refund-44", order_id: "981237650001", invoice_no: "ELU/HSR/FY26/00012", status: "COMPLETED" };
    else if (url.startsWith("/api/refunds/") && url.endsWith("/recover")) data = overrides.refundRecovery || { refund_id: "refund-44", order_id: "981237650001", invoice_no: "ELU/HSR/FY26/00012", status: "COMPLETED" };
    else if (url.startsWith("/api/refunds/")) data = { refund_id: "refund-44", order_id: "981237650001", invoice_no: "ELU/HSR/FY26/00012", status: "REFUND_UNKNOWN" };
    else if (url.startsWith("/api/reports/day-end")) data = {
      store_id: "7",
      store_code: "HSR",
      business_date: "2026-10-29",
      completed_bill_count: 2,
      metrics: { gross_paise: 200000 },
      payment_methods_paise: { upi: 120000 },
      channels: { walk_in: 2 },
      refund_count: 1,
      exceptions: [],
    };
    else if (url.startsWith("/api/reports/overall")) data = {
      store_id: "all",
      store_code: "Overall operations",
      business_date: "2026-10-29",
      completed_bill_count: 3,
      metrics: { gross_sales_paise: 3699200, refund_amount_paise: 2259540, net_sales_after_refunds_paise: 1089720 },
      payment_methods_paise: { upi: 3349260 },
      channels: { whatsapp_booked: { completed_bill_count: 2, gross_sales_paise: 2499500, refund_amount_paise: 2259540 } },
      refund_count: 2,
      exceptions: [{}],
      stores: [
        { store_id: "7", store_code: "HSR", business_date: "2026-10-29", completed_bill_count: 2, metrics: { net_sales_before_refunds_paise: 300000 }, payment_methods_paise: {}, channels: {}, refund_count: 1, exceptions: [] },
        { store_id: "8", store_code: "KMG", business_date: "2026-10-29", completed_bill_count: 1, metrics: { net_sales_before_refunds_paise: 150000 }, payment_methods_paise: {}, channels: {}, refund_count: 1, exceptions: [] },
      ],
    };
    if (overrides.priceHandler && url === "/api/pricing-preview" && data && typeof data === "object" && "httpStatus" in data) {
      const failure = data as { httpStatus: number; body: unknown };
      status = failure.httpStatus;
      data = failure.body;
    }
    const response = new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
    return response;
  }));
  return calls;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("browser POS workflows", () => {
  it("shows an actionable message when the local API cannot be reached", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    render(<App />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Cannot reach the Studio API. For local use, run `python scripts/start_local.py` and refresh the page.",
    );
  });

  it("looks up a customer, prices a cart, and completes a checkout through the API", async () => {
    const user = userEvent.setup();
    const calls = installApi({ userRole: "cashier" });
    render(<App />);

    expect(await screen.findByAltText("Eluno optical retail")).toHaveAttribute("src", "/pos/images/eluno.png");
    expect(screen.getByRole("link", { name: "| Premium Eyeglasses, Sunglasses & Clip-On" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Search by name, mobile, email")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Search customers" })).not.toBeInTheDocument();
    await screen.findByRole("option", { name: /HSR Store/ });
    await user.type(screen.getByLabelText("Mobile number"), "9876543210");
    await user.click(screen.getByRole("button", { name: "Find customer" }));
    expect(await screen.findByText("Mira Rao")).toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: "Add" }));
    expect(await screen.findByText("Pair offer")).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText("₹1,100.00").length).toBeGreaterThan(1));
    const automaticPriceRequest = calls.find((call) => call.url === "/api/pricing-preview");
    expect(automaticPriceRequest?.body).not.toHaveProperty("offer_ids");
    await user.click(screen.getByRole("button", { name: "Review & charge" }));
    await user.click(await screen.findByRole("button", { name: "Confirm charge" }));

    expect(await screen.findByText("Sale complete")).toBeInTheDocument();
    expect(screen.getAllByText(/ELU\/HSR\/FY26\/00012/).length).toBeGreaterThan(0);
    const customerRequest = calls.find((call) => call.url.startsWith("/api/customer-prescription"));
    expect(customerRequest?.url).toContain(encodeURIComponent("+919876543210"));
    const checkoutRequest = calls.find((call) => call.url === "/api/checkouts");
    expect(checkoutRequest?.body).toMatchObject({
      customer_id: 7401,
      customer_phone: "+919876543210",
      cart: { store_code: "HSR", location_id: "7", lines: [{ variant_id: "frame-100", quantity: 1 }] },
    });

    expect(checkoutRequest?.body.offer_ids).toEqual(["OFFER-10"]);
    expect(screen.getByText(/Applied automatically/)).toBeInTheDocument();

    const popupDocument = { open: vi.fn(), write: vi.fn(), close: vi.fn() };
    const popup = { document: popupDocument, close: vi.fn(), opener: null } as unknown as Window;
    const openWindow = vi.spyOn(window, "open").mockReturnValue(popup);
    await user.click(screen.getByRole("button", { name: "View Bill / Open Invoice" }));
    await waitFor(() => expect(popupDocument.write).toHaveBeenLastCalledWith(expect.stringContaining(
      "<title>Invoice ELU/HSR/FY26/00012</title>",
    )));
    const invoiceHtml = popupDocument.write.mock.lastCall?.[0] as string;
    expect(invoiceHtml).toContain("North Frame");
    expect(invoiceHtml).toContain("Discount ₹100.00");
    expect(invoiceHtml).toContain("CGST ₹59.00");
    expect(invoiceHtml).toContain("SGST ₹59.00");
    expect(invoiceHtml).toContain("Total ₹1,100.00");
    expect(calls.some((call) => call.url === "/api/invoices/sale-12")).toBe(true);
    expect(openWindow).toHaveBeenCalledWith("", "_blank");
  });

  it("retains a successful sale reference and offers retry when the invoice cannot be opened", async () => {
    const user = userEvent.setup();
    installApi({ userRole: "cashier", invoiceFailure: true });
    const popup = {
      document: { open: vi.fn(), write: vi.fn(), close: vi.fn() },
      close: vi.fn(),
      opener: null,
    } as unknown as Window;
    vi.spyOn(window, "open").mockReturnValue(popup);
    render(<App />);
    await screen.findByRole("option", { name: /HSR Store/ });
    await user.click(await screen.findByRole("button", { name: "Add" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Review & charge" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Review & charge" }));
    await user.click(await screen.findByRole("button", { name: "Confirm charge" }));
    expect(await screen.findByText("Sale complete")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "View Bill / Open Invoice" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not open invoice.*sale-12.*retry with Open Invoice/i);
    expect(screen.getByRole("button", { name: "View Bill / Open Invoice" })).toBeInTheDocument();
    expect(screen.getAllByText(/ELU\/HSR\/FY26\/00012/).length).toBeGreaterThan(0);
  });

  it("keeps the saved sale reference usable when checkout omits the invoice number", async () => {
    const user = userEvent.setup();
    const calls = installApi({
      userRole: "cashier",
      checkout: { sale_id: "sale-12", status: "COMPLETED", order_id: "981237650001" },
    });
    const popup = {
      document: { open: vi.fn(), write: vi.fn(), close: vi.fn() },
      close: vi.fn(),
      opener: null,
    } as unknown as Window;
    vi.spyOn(window, "open").mockReturnValue(popup);
    render(<App />);
    await screen.findByRole("option", { name: /HSR Store/ });
    await user.click(await screen.findByRole("button", { name: "Add" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Review & charge" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Review & charge" }));
    await user.click(await screen.findByRole("button", { name: "Confirm charge" }));
    expect(await screen.findByText(/invoice details were not returned.*sale-12/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "View Bill / Open Invoice" }));
    await waitFor(() => expect(popup.document.write).toHaveBeenLastCalledWith(expect.stringContaining("<title>Invoice")));
    expect(calls.some((call) => call.url === "/api/invoices/sale-12")).toBe(true);
  });

  it("shows staff sign-in before loading the authenticated Studio workspace", async () => {
    const user = userEvent.setup();
    const calls = installApi({ authRequired: true, userRole: "cashier" });
    render(<App />);
    await user.type(await screen.findByLabelText("Username"), "cashier");
    await user.type(screen.getByLabelText("Password"), "test-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByRole("button", { name: "Find customer" });
    expect(calls.some((call) => call.url === "/api/auth/login")).toBe(true);
    expect(screen.getByRole("button", { name: "Returns & Refunds" })).toBeInTheDocument();
    expect(screen.queryByText(/Eluno Studios?/i)).not.toBeInTheDocument();
  });

  it("wraps long signed-in staff IDs inside the header controls", async () => {
    const longStaffId = "staff_ad2e3a3368114fb6aeec8f5b632376";
    installApi({ userRole: "admin", staffId: longStaffId, displayName: "Demo Admin" });
    render(<App />);
    const operator = await screen.findByLabelText("Operator");
    expect(operator).toHaveTextContent(`Demo Admin · admin · ID ${longStaffId}`);
    expect(operator).toHaveClass("session-operator");
  });

  it("keeps manual offers available after an incompatible selection is rejected", async () => {
    const user = userEvent.setup();
    const offerPricing = {
      ...pricing,
      offer_evaluation: [
        { offer_id: "OFFER-10", name: "Pair offer", stackable: false, status: "applied", reason: "Selected.", discount_paise: 10000 },
        { offer_id: "OFFER-20", name: "Frame offer", stackable: false, status: "rejected", reason: "Not selected by the conflict policy.", discount_paise: 0 },
      ],
    };
    const calls = installApi({
      pricing: offerPricing,
      priceHandler: (_body: Record<string, unknown>, index: number) => index >= 3
        ? { httpStatus: 400, body: { message: "Selected offers cannot be combined because one is exclusive." } }
        : offerPricing,
    });
    render(<App />);
    await screen.findByRole("option", { name: /HSR Store/ });
    await user.click(await screen.findByRole("button", { name: "Add" }));
    await screen.findByText("Pair offer");
    await user.click(screen.getByRole("button", { name: "Choose manually" }));
    const pairOffer = await screen.findByRole("checkbox", { name: "Select Pair offer" });
    const frameOffer = await screen.findByRole("checkbox", { name: "Select Frame offer" });
    expect(pairOffer).toBeChecked();
    await user.click(frameOffer);
    await waitFor(() => expect(calls.filter((call) => call.url === "/api/pricing-preview").length).toBeGreaterThanOrEqual(3));
    await screen.findByRole("alert");
    expect(screen.getByRole("checkbox", { name: "Select Frame offer" })).toBeChecked();
    await user.click(screen.getByRole("checkbox", { name: "Select Frame offer" }));
    expect(await screen.findByText("Pair offer")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Select Pair offer" })).toBeInTheDocument();
  });

  it("shows Studio, Sales, Returns & Refunds, Store Orders, and Customers to Cashiers", async () => {
    const user = userEvent.setup();
    installApi({ userRole: "cashier" });
    render(<App />);
    await screen.findByRole("button", { name: "Find customer" });
    expect(screen.getByRole("button", { name: "Studio" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sales" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Returns & Refunds" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Store Orders" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Customers" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Inventory" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Team" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reports" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Studio" }));
    expect(await screen.findByRole("heading", { name: "Welcome to the Studio" })).toBeInTheDocument();
    expect(screen.queryByText(/Eluno Studios?/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Customers" }));
    expect(screen.queryByLabelText("Search by name, mobile, email")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Search customers" })).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Mobile number"), "9876543210");
    await user.click(screen.getByRole("button", { name: "Find customer" }));
    expect(await screen.findByText("Mira Rao")).toBeInTheDocument();
    expect(screen.getByText("Prescription on file")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Store Orders" }));
    expect(await screen.findByRole("heading", { name: "Store Orders" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Returns & Refunds" }));
    expect(await screen.findByRole("heading", { name: "Returns & Refunds" })).toBeInTheDocument();
  });

  it("lets Cashiers make store-scoped order decisions without claiming online payment", async () => {
    const user = userEvent.setup();
    const calls = installApi({
      userRole: "cashier",
      storeOrders: [
        {
          order_id: "web_confirm", created_at: "2026-10-30T10:00:00+05:30",
          store_code: "HSR", customer_name: "Mira Rao",
          items: [{ title: "Optical Frame", quantity: 1, prescription_assistance_requested: true }],
          total_paise: 125000, payment_status: "awaiting_payment",
          store_confirmation_status: "pending", status_message: "Prescription assistance requested.",
        },
        {
          order_id: "web_deny", created_at: "2026-10-30T11:00:00+05:30",
          store_code: "HSR", customer_name: "Asha Rao",
          items: [{ title: "Lens", quantity: 2, prescription_assistance_requested: false }],
          total_paise: 50000, payment_status: "awaiting_payment",
          store_confirmation_status: "pending", status_message: "Awaiting store review.",
        },
      ],
    });

    render(<App />);
    const userNav = await screen.findByRole("button", { name: "Store Orders" });
    await waitFor(() => expect(userNav.querySelector(".nav-count")).toHaveTextContent("2"));
    await user.click(userNav);

    const confirmCard = (await screen.findByRole("heading", { name: "web_confirm" })).closest("article");
    expect(confirmCard).not.toBeNull();
    expect(within(confirmCard as HTMLElement).getByText("Mira Rao")).toBeInTheDocument();
    expect(within(confirmCard as HTMLElement).getByText(/Optometrist assistance requested/)).toBeInTheDocument();
    expect(within(confirmCard as HTMLElement).getByText(
      (_, element) => element?.tagName === "P" && element.textContent?.includes("Awaiting payment") === true,
    )).toBeInTheDocument();
    expect(within(confirmCard as HTMLElement).queryByText("+919876543210")).not.toBeInTheDocument();
    await user.click(within(confirmCard as HTMLElement).getByRole("button", { name: "Confirm store request" }));
    expect(await screen.findByText("web_confirm confirmed for store review. Payment is still pending.")).toBeInTheDocument();
    expect(calls.some((call) => call.url === "/api/store/orders/web_confirm/confirm")).toBe(true);

    const denyCard = (await screen.findByRole("heading", { name: "web_deny" })).closest("article");
    expect(denyCard).not.toBeNull();
    await user.type(within(denyCard as HTMLElement).getByLabelText("Reason if denying"), "Unable to source this lens");
    await user.click(within(denyCard as HTMLElement).getByRole("button", { name: "Deny request" }));
    expect(await screen.findByText("web_deny denied. No payment was collected.")).toBeInTheDocument();
    expect(calls.find((call) => call.url === "/api/store/orders/web_deny/deny")?.body)
      .toEqual({ reason: "Unable to source this lens" });
  });

  it("routes store-confirmed prescription-assistance requests to the assigned Optometrist workflow", async () => {
    const user = userEvent.setup();
    const calls = installApi({
      userRole: "optometrist",
      optometristAssistance: {
        orders: [{
          order_id: "web-rx-help", created_at: "2026-10-30T12:00:00+05:30",
          store_code: "HSR", customer_name: "Mira Rao", customer_phone: customer.phone,
          items: [{ title: "Single Vision Lens", quantity: 1 }],
          payment_status: "awaiting_payment", store_confirmation_status: "confirmed",
          status_message: "Prescription assistance requested.",
        }],
      },
    });
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Prescriptions" }));
    expect(await screen.findByRole("heading", { name: "Prescription assistance requests" })).toBeInTheDocument();
    expect(await screen.findByText("web-rx-help")).toBeInTheDocument();
    expect(calls.some((call) => call.url === "/api/optometrist/orders/assistance")).toBe(true);
    await user.click(screen.getByRole("button", { name: "Open prescription lookup" }));
    expect(await screen.findByDisplayValue("+919876543210")).toBeInTheDocument();
    await waitFor(() => expect(calls.some((call) => call.url.startsWith("/api/customer-prescription"))).toBe(true));
  });

  it("shows Studio, Inventory, Customers, Team, and Reports but not Sales for Admins", async () => {
    installApi({ userRole: "admin" });
    render(<App />);
    await screen.findByRole("heading", { name: "Welcome to the Studio" });
    expect(screen.queryByText(/Eluno Studios?/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sales" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Returns & Refunds" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Store Orders" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Customer & prescription" })).not.toBeInTheDocument();
    for (const label of ["Studio", "Inventory", "Customers", "Team", "Reports"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
    await userEvent.setup().click(screen.getByRole("button", { name: "Customers" }));
    expect(await screen.findByRole("heading", { name: "Customers" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Mobile number")).not.toBeInTheDocument();
  });

  it("lets Admins search by full name or email and select a record without the duplicate mobile lookup", async () => {
    const user = userEvent.setup();
    const calls = installApi({ userRole: "admin", displayName: "Demo Admin" });
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Customers" }));
    expect(screen.queryByLabelText("Mobile number")).not.toBeInTheDocument();
    const search = screen.getByLabelText("Search by name, mobile, email, or customer ID");
    await user.type(search, "Yashu Youwaraj");
    await user.click(screen.getByRole("button", { name: "Search customers" }));
    expect(await screen.findByText("Yashu Youwaraj")).toBeInTheDocument();
    expect(screen.getByText("₹0.00 · 0 orders · refunds ₹0.00")).toBeInTheDocument();
    expect(screen.queryByText(/unavailable/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Select" }));
    expect(await screen.findByRole("heading", { name: "Yashu Youwaraj" })).toBeInTheDocument();
    expect(calls.some((call) => call.url.includes("/api/customers/search?q=Yashu%20Youwaraj"))).toBe(true);
    expect(calls.some((call) => call.url === "/api/customers/99000004?page=1")).toBe(true);
  });

  it("shows confirmed store status in customer orders and can refresh the latest decision", async () => {
    const user = userEvent.setup();
    const calls = installApi({
      authRequired: true,
      customerAuthenticated: true,
      customerOrders: [{
        order_id: "web-confirmed", status: "awaiting_payment",
        store_confirmation_status: "confirmed", created_at: "2026-10-30T10:00:00+05:30",
        store_code: "HSR", customer_name: "Mira Rao",
        delivery_address: { line1: "12 Retail Road", line2: "", city: "Bengaluru", state: "Karnataka", postal_code: "560001" },
        items: [{ variant_id: "lens-200", title: "Single Vision", line_type: "lens", quantity: 1, prescription_assistance_requested: true }],
        total_paise: 50000, payment_status: "awaiting_payment",
        payment_method_preference: "upi", fulfillment_status: "not_submitted",
        tracking_available: false, status_message: "Store confirmed. Payment is still pending.",
      }],
    });
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "My Orders" }));

    expect(await screen.findByText(
      (_, element) => element?.tagName === "P" && element.textContent?.includes("Store: Confirmed") === true,
    )).toBeInTheDocument();
    expect(screen.getByText(
      (_, element) => element?.tagName === "P" && element.textContent?.includes("online payment is unavailable") === true,
    )).toBeInTheDocument();
    expect(screen.getByText("Store confirmed. Payment is still pending.")).toBeInTheDocument();
    const initialRequestCount = calls.filter((call) => call.url.startsWith("/api/customer/orders")).length;
    await user.click(screen.getByRole("button", { name: "Refresh orders" }));
    await waitFor(() => expect(
      calls.filter((call) => call.url.startsWith("/api/customer/orders")).length,
    ).toBeGreaterThan(initialRequestCount));
    expect(screen.queryByRole("button", { name: /Pay now/i })).not.toBeInTheDocument();
  });

  it("opens role-appropriate till views for Admins and Cashiers", async () => {
    for (const role of ["admin", "cashier"] as const) {
      const user = userEvent.setup();
      const calls = installApi({ userRole: role });
      const rendered = render(<App />);
      await screen.findByRole("button", { name: "Studio" });
      await user.click(screen.getByRole("button", { name: "Studio" }));
      await user.click(await screen.findByRole("button", { name: "Till" }));
      expect(await screen.findByRole("heading", {
        name: role === "admin" ? "Till reconciliation" : "Till session",
      })).toBeInTheDocument();
      expect(await screen.findByText(/No till sessions are recorded/)).toBeInTheDocument();
      expect(calls.some((call) => call.url.startsWith(
        role === "admin" ? "/api/admin/tills" : "/api/tills",
      ))).toBe(true);
      rendered.unmount();
    }
  });

  it("allows an Admin to create staff, reset passwords, and deactivate an account", async () => {
    const user = userEvent.setup();
    const calls = installApi({ userRole: "admin" });
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Team" }));
    await screen.findByText("Demo Admin");

    const adminRow = screen.getByText("Demo Admin").closest("article");
    expect(within(adminRow as HTMLElement).getByRole("button", { name: "Disable" })).toBeDisabled();
    expect(screen.queryByText(/password_hash/i)).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("Username"), "new.cashier");
    await user.type(screen.getByLabelText("Display name"), "New Cashier");
    await user.selectOptions(screen.getByLabelText("Assigned store"), "HSR");
    await user.type(screen.getByLabelText("Initial password"), "New-Cashier-Password-2026!");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    const newRow = await screen.findByText("New Cashier");
    const row = newRow.closest("article") as HTMLElement;
    expect(within(row).getByText(/new.cashier · cashier · Operator ID staff-new/)).toBeInTheDocument();
    expect(calls.find((call) => call.url === "/api/admin/staff" && call.body.username === "new.cashier")?.body)
      .toMatchObject({ role: "cashier", display_name: "New Cashier", store_code: "HSR" });

    await user.click(within(row).getByRole("button", { name: "Reset password" }));
    await user.type(screen.getByLabelText("New password"), "Updated-Cashier-Password-2026!");
    await user.click(screen.getByRole("button", { name: "Save password" }));
    await waitFor(() => expect(calls.some((call) =>
      call.url.endsWith("/staff/staff-new") && call.body.password === "Updated-Cashier-Password-2026!",
    )).toBe(true));

    await user.click(within(row).getByRole("button", { name: "Disable" }));
    expect(await screen.findByText("new.cashier is now disabled.")).toBeInTheDocument();
  });

  it("automatically assigns the only clinical identity when creating an Optometrist", async () => {
    const user = userEvent.setup();
    const calls = installApi({
      userRole: "admin",
      optometristApprovers: [{ staff_id: "S107", display_name: "Demo Optometrist" }],
    });
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Team" }));
    await user.selectOptions(screen.getByLabelText("Role"), "optometrist");
    await user.selectOptions(screen.getByLabelText("Assigned store"), "HSR");

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Clinical identity assigned: Demo Optometrist · S107",
    );
    await user.type(screen.getByLabelText("Username"), "new.optometrist");
    await user.type(screen.getByLabelText("Display name"), "New Optometrist");
    await user.type(screen.getByLabelText("Initial password"), "New-Optometrist-Password-2026!");
    await user.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => expect(calls.find((call) =>
      call.url === "/api/admin/staff" && call.body.username === "new.optometrist",
    )?.body).toMatchObject({
      role: "optometrist",
      store_code: "HSR",
      external_staff_id: "S107",
    }));
  });

  it("requires a manual clinical identity choice when multiple Optometrists match the store", async () => {
    const user = userEvent.setup();
    installApi({
      userRole: "admin",
      optometristApprovers: [
        { staff_id: "S107", display_name: "Demo Optometrist" },
        { staff_id: "S108", display_name: "Second Optometrist" },
      ],
    });
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Team" }));
    await user.selectOptions(screen.getByLabelText("Role"), "optometrist");
    await user.selectOptions(screen.getByLabelText("Assigned store"), "HSR");

    const identity = await screen.findByLabelText("Clinical staff identity");
    expect(identity).toHaveValue("");
    expect(screen.getByRole("button", { name: "Create account" })).toBeDisabled();
    await user.selectOptions(identity, "S108");
    expect(screen.getByRole("button", { name: "Create account" })).toBeEnabled();
  });

  it("returns to the login screen when an authenticated request reports an invalid session", async () => {
    const user = userEvent.setup();
    installApi({ userRole: "admin", staffUnauthorized: true });
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Team" }));
    expect(await screen.findByRole("heading", { name: "Staff sign in" })).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent(/session expired or was disabled/i);
  });

  it("creates catalog products in a dialog and uses the store-scoped stock receipt API", async () => {
    const user = userEvent.setup();
    const calls = installApi({ userRole: "admin" });
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Inventory" }));
    await screen.findByText("Tracked frame/lens stock at selected store");
    const inventoryRequestsBeforeFocus = calls.filter((call) => call.url.startsWith("/api/admin/catalog/frames?location_id=")).length;
    fireEvent.focus(window);
    await waitFor(() => {
      expect(calls.filter((call) => call.url.startsWith("/api/admin/catalog/frames?location_id=")).length)
        .toBeGreaterThan(inventoryRequestsBeforeFocus);
    });
    expect(screen.queryByLabelText("Product name")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create catalog item" }));
    expect(screen.getByRole("dialog", { name: "Create catalog item" })).toBeInTheDocument();
    await user.type(screen.getByLabelText("Product name"), "Harbor Frame");
    await user.type(screen.getByLabelText("SKU"), "HARBOR-01");
    await user.type(screen.getByLabelText("Variant name"), "Tortoise");
    await user.type(screen.getByLabelText("Price (INR)"), "1599.50");
    await user.type(screen.getByLabelText("Material"), "Acetate");
    await user.type(screen.getByLabelText("Size"), "52-18-140");
    await user.click(screen.getByRole("button", { name: "Create item" }));
    await screen.findByText("Harbor Frame was created in the Shopify catalog.");
    expect(calls.find((call) => call.url === "/api/admin/catalog/products" && call.body.line_type === "frame")?.body).toMatchObject({
      line_type: "frame",
      sku: "HARBOR-01",
      price_paise: 159950,
      actor: "S105",
    });
    await user.type(screen.getByLabelText("Delivery / goods-received reference"), "GRN-2026-001");
    await user.click(screen.getByRole("button", { name: "Record stock receipt" }));
    expect(calls.find((call) => call.url === "/api/admin/inventory/receipts")?.body).toMatchObject({
      inventory_item_id: 601,
      location_id: 7,
      quantity: 1,
      reference: "GRN-2026-001",
    });
  });

  it("edits optical catalog details through the Admin API without exposing the editor to Cashiers", async () => {
    const user = userEvent.setup();
    const calls = installApi({
      userRole: "admin",
      catalogProducts: [{
        product_id: "701", variant_id: "801", line_type: "frame",
        title: "North Frame", description: "Original description", status: "active",
        sku: "NORTH-BLK", variant_title: "Black", price_paise: 499900,
        category: "optical", material: "TR90", size: "52-18-140", rx_able: true,
        lens_type: null, index: null, coating: null,
      }],
    });
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Inventory" }));
    await user.click(await screen.findByRole("button", { name: "Edit North Frame" }));
    const productName = screen.getByLabelText("Product name");
    await user.clear(productName);
    await user.type(productName, "North Frame Updated");
    await user.click(screen.getByRole("button", { name: "Save catalog changes" }));
    expect(await screen.findByText("North Frame was updated. Inventory quantities were not changed.")).toBeInTheDocument();
    expect(calls.find((call) => call.url === "/api/admin/catalog/products/701")?.body).toMatchObject({
      variant_id: "801", title: "North Frame Updated", price_paise: 499900,
    });
    expect(calls.filter((call) => call.url.startsWith("/api/admin/catalog/frames?location_id=")).length).toBeGreaterThan(1);
    expect(calls.filter((call) => call.url.startsWith("/api/admin/catalog/products")).length).toBeGreaterThan(1);
  });

  it("shows make-to-order lenses without implying tracked store quantities", async () => {
    const user = userEvent.setup();
    installApi({
      userRole: "admin",
      catalogProducts: [{
        product_id: "702", variant_id: "802", line_type: "lens",
        title: "Single Vision", description: "", status: "active", sku: "SV-150",
        variant_title: "1.50 Standard", price_paise: 50000, category: null,
        material: null, size: null, rx_able: null, lens_type: "single_vision",
        index: 1.5, coating: "standard",
      }],
    });
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Inventory" }));
    await user.click(screen.getByRole("button", { name: "Lenses" }));
    expect(await screen.findByText("Make-to-order · not tracked")).toBeInTheDocument();
    expect(screen.getByText(/Store-level lens quantities are not tracked/)).toBeInTheDocument();
    expect(screen.queryByText("Available at selected store")).not.toBeInTheDocument();
  });

  it("requires a separate confirmation before archiving and keeps stock edits separate", async () => {
    const user = userEvent.setup();
    const calls = installApi({
      userRole: "admin",
      catalogProducts: [{
        product_id: "701", variant_id: "801", line_type: "frame",
        title: "North Frame", description: "Original description", status: "active",
        sku: "NORTH-BLK", variant_title: "Black", price_paise: 499900,
        category: "optical", material: "TR90", size: "52-18-140", rx_able: true,
        lens_type: null, index: null, coating: null,
      }],
    });
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Inventory" }));
    await user.click(await screen.findByRole("button", { name: "Edit North Frame" }));
    await user.click(screen.getByRole("button", { name: "Archive product…" }));
    expect(screen.getByRole("heading", { name: "Archive North Frame?" })).toBeInTheDocument();
    expect(calls.some((call) => call.url === "/api/admin/catalog/products/701" && call.body.status === "archived")).toBe(false);
    await user.click(screen.getByRole("button", { name: "Confirm archive" }));
    expect(await screen.findByText("North Frame was archived. Inventory quantities were not changed.")).toBeInTheDocument();
    expect(calls.find((call) => call.url === "/api/admin/catalog/products/701")?.body).toEqual({
      variant_id: "801", status: "archived",
    });
  });

  it("keeps the edit dialog open and reports a catalog update failure", async () => {
    const user = userEvent.setup();
    installApi({
      userRole: "admin",
      catalogUpdateFailure: true,
      catalogProducts: [{
        product_id: "701", variant_id: "801", line_type: "frame",
        title: "North Frame", description: "", status: "active", sku: "NORTH-BLK",
        variant_title: "Black", price_paise: 499900, category: "optical",
        material: "TR90", size: "52-18-140", rx_able: true,
        lens_type: null, index: null, coating: null,
      }],
    });
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Inventory" }));
    await user.click(await screen.findByRole("button", { name: "Edit North Frame" }));
    await user.click(screen.getByRole("button", { name: "Save catalog changes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Catalog update could not be saved.");
    expect(screen.getByRole("dialog", { name: "Edit North Frame" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save catalog changes" })).toBeEnabled();
  });

  it("creates and selects a real customer record for checkout", async () => {
    const user = userEvent.setup();
    const calls = installApi({ customerNotFound: true, userRole: "cashier" });
    render(<App />);
    await screen.findByRole("option", { name: /HSR Store/ });
    await user.type(screen.getByLabelText("Mobile number"), "9876543210");
    await user.click(screen.getByRole("button", { name: "Find customer" }));
    await user.click(await screen.findByRole("button", { name: "Create new customer" }));
    await user.type(screen.getByLabelText("Full name"), "Asha Rao");
    await user.click(screen.getByRole("button", { name: "Save and select customer" }));

    expect(await screen.findByText("Asha Rao")).toBeInTheDocument();
    expect(await screen.findByText(/Enter the prescription details and select the assigned Optometrist/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add prescription" }));
    expect(await screen.findByLabelText("Verified by Optometrist")).toHaveValue("S107");
    await user.type(screen.getByLabelText("OD sphere"), "-1.00");
    await user.type(screen.getByLabelText("OD cylinder"), "-0.50");
    await user.type(screen.getByLabelText("OS sphere"), "-1.00");
    await user.type(screen.getByLabelText("OS cylinder"), "-0.50");
    await user.click(screen.getByLabelText("I confirm these prescription values were verified by the selected Optometrist."));
    await user.click(screen.getByRole("button", { name: "Save Prescription" }));
    expect(await screen.findByText(/Prescription RX-7401 saved and linked to Asha Rao/)).toBeInTheDocument();
    const prescriptionCall = calls.find((call) => call.url === "/api/prescriptions" && call.body.optometrist_id);
    expect(prescriptionCall?.body).toMatchObject({
      phone: "+919876543210",
      store_code: "HSR",
      optometrist_id: "S107",
      optometrist_confirmed: true,
    });
    await user.click(screen.getByRole("button", { name: "Lenses" }));
    await user.click(await screen.findByRole("button", { name: "Add" }));
    await user.click(await screen.findByLabelText("Optometrist review was completed."));
    await user.click(screen.getByRole("button", { name: "Acknowledge and add lens" }));
    expect(await screen.findByText(/Single Vision 1.50 · standard/)).toBeInTheDocument();
    const createCall = calls.find((call) => call.url === "/api/customers");
    expect(createCall?.body).toEqual({ name: "Asha Rao", phone: "+919876543210" });
    await user.click(screen.getByRole("button", { name: "Frames" }));
    await user.click(await screen.findByRole("button", { name: "Add" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Review & charge" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Review & charge" }));
    await user.click(await screen.findByRole("button", { name: "Confirm charge" }));
    expect(await screen.findByText("Sale complete")).toBeInTheDocument();
    const checkoutRequest = calls.find((call) => call.url === "/api/checkouts");
    expect(checkoutRequest?.body).toMatchObject({ customer_id: 8402, customer_phone: "+919876543210" });
  });

  it("validates a new customer name before sending and reports duplicate conflicts", async () => {
    const user = userEvent.setup();
    const calls = installApi({ userRole: "cashier", customerNotFound: true, registrationStatus: 409, registrationMessage: "This mobile number already belongs to a different customer." });
    render(<App />);
    await screen.findByRole("option", { name: /HSR Store/ });
    await user.type(screen.getByLabelText("Mobile number"), "9876543210");
    await user.click(screen.getByRole("button", { name: "Find customer" }));
    await user.click(await screen.findByRole("button", { name: "Create new customer" }));
    await user.type(screen.getByLabelText("Full name"), "12345");
    await user.click(screen.getByRole("button", { name: "Save and select customer" }));
    expect(await screen.findByText(/at least one letter/)).toBeInTheDocument();
    expect(calls.some((call) => call.url === "/api/customers")).toBe(false);

    await user.clear(screen.getByLabelText("Full name"));
    await user.type(screen.getByLabelText("Full name"), "Asha Rao");
    const newCustomerPhone = screen.getAllByLabelText("Mobile number")[1];
    await user.clear(newCustomerPhone);
    await user.type(newCustomerPhone, "123");
    await user.click(screen.getByRole("button", { name: "Save and select customer" }));
    expect(await screen.findByText(/valid 10-digit Indian mobile/)).toBeInTheDocument();
    expect(calls.some((call) => call.url === "/api/customers")).toBe(false);
    await user.clear(newCustomerPhone);
    await user.type(newCustomerPhone, "9876543210");
    await user.click(screen.getByRole("button", { name: "Save and select customer" }));
    expect(await screen.findByText(/already belongs to a different customer/)).toBeInTheDocument();
    expect(screen.queryByText("Asha Rao")).not.toBeInTheDocument();
  });

  it("resolves invoice lookup to the real Shopify order ID and confirms before refunding", async () => {
    const user = userEvent.setup();
    const calls = installApi({ userRole: "cashier" });
    render(<App />);
    await screen.findByRole("option", { name: /HSR Store/ });
    await user.click(screen.getByRole("button", { name: "Returns & Refunds" }));
    await user.type(screen.getByLabelText("Invoice number or Shopify order ID"), "ELU/HSR/FY26/00012");
    await user.click(screen.getByRole("button", { name: "Find transaction" }));
    expect(await screen.findByRole("heading", { name: "#1042" })).toBeInTheDocument();
    expect(screen.getByText(/Shopify order ID:/).parentElement).toHaveTextContent("981237650001");
    await user.selectOptions(screen.getByLabelText("Refund reason"), "CUSTOMER_RETURN");
    const confirmation = screen.getByLabelText(/I confirm transaction ELU\/HSR\/FY26\/00012/);
    await user.click(confirmation);
    await user.click(screen.getByRole("button", { name: "Confirm full refund" }));
    expect(await screen.findByText("Refund completed")).toBeInTheDocument();
    const refundCall = calls.find((call) => call.url === "/api/refunds");
    expect(refundCall?.body).toMatchObject({
      order_id: "981237650001",
      invoice_no: "ELU/HSR/FY26/00012",
      store_code: "HSR",
    });
  });

  it("offers reconciliation for an unknown refund without submitting another refund", async () => {
    const user = userEvent.setup();
    const calls = installApi({
      userRole: "cashier",
      refundSubmission: { refund_id: "refund-44", order_id: "981237650001", invoice_no: "ELU/HSR/FY26/00012", status: "REFUND_UNKNOWN" },
      refundRecovery: { refund_id: "refund-44", order_id: "981237650001", invoice_no: "ELU/HSR/FY26/00012", status: "COMPLETED" },
    });
    render(<App />);
    await screen.findByRole("option", { name: /HSR Store/ });
    await user.click(screen.getByRole("button", { name: "Returns & Refunds" }));
    await user.type(screen.getByLabelText("Invoice number or Shopify order ID"), "ELU/HSR/FY26/00012");
    await user.click(screen.getByRole("button", { name: "Find transaction" }));
    await user.selectOptions(screen.getByLabelText("Refund reason"), "CUSTOMER_RETURN");
    await user.click(screen.getByLabelText(/I confirm transaction ELU\/HSR\/FY26\/00012/));
    await user.click(screen.getByRole("button", { name: "Confirm full refund" }));
    expect(await screen.findByText("Refund refund_unknown")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reconcile refund" }));
    expect(await screen.findByText("Refund completed")).toBeInTheDocument();
    expect(calls.filter((call) => call.url === "/api/refunds")).toHaveLength(1);
    expect(calls.some((call) => call.url === "/api/refunds/refund-44/recover")).toBe(true);
  });

  it("clears a resolved refund order as soon as the lookup identifier changes", async () => {
    const user = userEvent.setup();
    installApi({ userRole: "cashier" });
    render(<App />);
    await screen.findByRole("option", { name: /HSR Store/ });
    await user.click(screen.getByRole("button", { name: "Returns & Refunds" }));
    const identifier = screen.getByLabelText("Invoice number or Shopify order ID");
    await user.type(identifier, "ELU/HSR/FY26/00012");
    await user.click(screen.getByRole("button", { name: "Find transaction" }));
    expect(await screen.findByRole("heading", { name: "#1042" })).toBeInTheDocument();

    await user.type(identifier, "x");
    expect(screen.queryByRole("heading", { name: "#1042" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm full refund" })).not.toBeInTheDocument();
  });

  it("loads the selected store's day-end report", async () => {
    const user = userEvent.setup();
    const calls = installApi({ userRole: "admin" });
    render(<App />);
    await screen.findByRole("option", { name: /HSR Store/ });
    await user.click(screen.getByRole("button", { name: "Reports" }));
    await user.click(screen.getByRole("button", { name: "Load report" }));
    expect(await screen.findByRole("heading", { name: "HSR · 29 October 2026" })).toBeInTheDocument();
    expect(screen.getByText("Completed Bills")).toBeInTheDocument();
    expect(calls.some((call) => call.url.startsWith("/api/reports/day-end?store_id=7"))).toBe(true);
  });

  it("loads the all-store Overall operations report", async () => {
    const user = userEvent.setup();
    const calls = installApi({ userRole: "admin" });
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Reports" }));
    await user.selectOptions(screen.getByLabelText("Report scope"), "overall");
    expect(screen.queryByLabelText("Store")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Load report" }));

    expect(await screen.findByRole("heading", { name: "Overall operations · 29 October 2026" })).toBeInTheDocument();
    expect(screen.getByText("Completed Bills")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("UPI: ₹33,492.60")).toBeInTheDocument();
    expect(screen.getByText(/whatsapp booked: 2 bills · gross ₹24,995.00 · refunded ₹22,595.40/)).toBeInTheDocument();
    expect(screen.getByText(
      (_, element) => element?.tagName === "SPAN" && element.textContent?.includes("2 bills") === true,
    )).toBeInTheDocument();
    expect(calls.some((call) => call.url.startsWith("/api/reports/overall?date=2026-10-29"))).toBe(true);
  });

  it("clears the selected customer when its mobile number is edited", async () => {
    const user = userEvent.setup();
    installApi({ userRole: "cashier" });
    render(<App />);
    await screen.findByRole("option", { name: /HSR Store/ });
    const mobile = screen.getByLabelText("Mobile number");
    await user.type(mobile, "9876543210");
    await user.click(screen.getByRole("button", { name: "Find customer" }));
    expect(await screen.findByText("Mira Rao")).toBeInTheDocument();

    await user.type(mobile, "1");
    expect(screen.queryByText("Mira Rao")).not.toBeInTheDocument();
  });

  it("reevaluates customer-tag offers when the selected customer changes", async () => {
    const user = userEvent.setup();
    const calls = installApi({ userRole: "cashier" });
    render(<App />);
    await screen.findByRole("option", { name: /HSR Store/ });
    await user.click(await screen.findByRole("button", { name: "Add" }));
    await user.type(screen.getByLabelText("Mobile number"), "9876543210");
    await user.click(screen.getByRole("button", { name: "Find customer" }));
    await waitFor(() => expect(calls.some((call) =>
      call.url === "/api/pricing-preview" && call.body.customer_id === 7401,
    )).toBe(true));

    const mobile = screen.getByLabelText("Mobile number");
    await user.clear(mobile);
    await user.type(mobile, "9876543211");
    await waitFor(() => expect(calls.some((call) =>
      call.url === "/api/pricing-preview" && call.body.customer_id === null,
    )).toBe(true));
  });

  it("reevaluates offers when the cart quantity changes and permits manual selection", async () => {
    const user = userEvent.setup();
    const calls = installApi({ userRole: "cashier" });
    render(<App />);
    await screen.findByRole("option", { name: /HSR Store/ });
    await user.click(await screen.findByRole("button", { name: "Add" }));
    await waitFor(() => expect(calls.some((call) => call.url === "/api/pricing-preview")).toBe(true));
    const quantity = screen.getByLabelText("Quantity for frame-100");
    fireEvent.change(quantity, { target: { value: "2" } });
    await waitFor(() => expect(calls.some((call) => call.url === "/api/pricing-preview" && (call.body.cart as { lines: Array<{ quantity: number }> }).lines[0].quantity === 2)).toBe(true));

    await user.click(await screen.findByRole("button", { name: "Choose manually" }));
    await waitFor(() => expect(calls.some((call) => call.url === "/api/pricing-preview" && Array.isArray(call.body.offer_ids))).toBe(true));
    expect(screen.getByLabelText("Select Pair offer")).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Review & charge" }));
    await user.click(await screen.findByRole("button", { name: "Confirm charge" }));
    const checkoutRequest = calls.find((call) => call.url === "/api/checkouts");
    expect(checkoutRequest?.body.offer_ids).toEqual(["OFFER-10"]);
  });

  it("shows rejected offers without applying their discounts", async () => {
    const rejectedPricing = {
      ...pricing,
      discount_paise: 0,
      payable_paise: 120000,
      offers: [],
      offer_evaluation: [{
        offer_id: "OFFER-10",
        name: "Pair offer",
        stackable: true,
        status: "rejected",
        reason: "Customer does not have required tag priority.",
        discount_paise: 0,
      }],
    };
    const user = userEvent.setup();
    installApi({ userRole: "cashier", pricing: rejectedPricing });
    render(<App />);
    await screen.findByRole("option", { name: /HSR Store/ });
    await user.click(await screen.findByRole("button", { name: "Add" }));
    expect(await screen.findByText(/Rejected: Customer does not have required tag priority/)).toBeInTheDocument();
    expect(screen.getAllByText("₹1,200.00").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Review & charge" })).toBeEnabled();
  });

  it("does not let an older pricing response overwrite the latest cart state", async () => {
    const user = userEvent.setup();
    let resolveFirst: ((value: unknown) => void) | undefined;
    let resolveSecond: ((value: unknown) => void) | undefined;
    const calls = installApi({
      userRole: "cashier",
      priceHandler: (_payload: Record<string, unknown>, index: number) => new Promise((resolve) => {
        if (index === 1) resolveFirst = resolve;
        else resolveSecond = resolve;
      }),
    });
    render(<App />);
    await screen.findByRole("option", { name: /HSR Store/ });
    await user.click(await screen.findByRole("button", { name: "Add" }));
    await waitFor(() => expect(resolveFirst).toBeDefined());
    const quantity = screen.getByLabelText("Quantity for frame-100");
    fireEvent.change(quantity, { target: { value: "2" } });
    await waitFor(() => expect(resolveSecond).toBeDefined());
    resolveSecond?.({ ...pricing, payable_paise: 222222 });
    await waitFor(() => expect(screen.getByText("₹2,222.22")).toBeInTheDocument());
    resolveFirst?.({ ...pricing, payable_paise: 111111 });
    await waitFor(() => expect(calls.filter((call) => call.url === "/api/pricing-preview").length).toBe(2));
    expect(screen.getByText("₹2,222.22")).toBeInTheDocument();
    expect(screen.queryByText("₹1,111.11")).not.toBeInTheDocument();
  });

  it("keeps off-grid axis warnings visible and sends explicit optometrist review acknowledgement", async () => {
    const user = userEvent.setup();
    const offGridPrescription = {
      ...prescription,
      od: { ...prescription.od, axis: 91 },
    };
    const calls = installApi({ prescription: offGridPrescription, userRole: "cashier" });
    render(<App />);
    await screen.findByRole("option", { name: /HSR Store/ });
    await user.type(screen.getByLabelText("Mobile number"), "9876543210");
    await user.click(screen.getByRole("button", { name: "Find customer" }));
    expect(await screen.findByText(/OD axis 91 is off the 5-degree grid/)).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Add" }));
    await user.click(screen.getByRole("button", { name: "Lenses" }));
    await screen.findByText(/Single Vision/);
    await user.click(screen.getByRole("button", { name: "Add" }));
    await user.click(await screen.findByLabelText("Optometrist review was completed."));
    await user.click(screen.getByRole("button", { name: "Acknowledge and add lens" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Review & charge" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Review & charge" }));
    await user.click(await screen.findByRole("button", { name: "Confirm charge" }));
    expect(await screen.findByText("Sale complete")).toBeInTheDocument();
    expect(calls.some((call) => call.url.startsWith("/api/customer-prescription"))).toBe(true);
    const checkoutRequest = calls.find((call) => call.url === "/api/checkouts");
    expect(checkoutRequest?.body).toMatchObject({
      reviewed_prescription_ids: ["RX-7401"],
      cart: { lines: expect.arrayContaining([expect.objectContaining({ variant_id: "lens-200", prescription_id: "RX-7401" })]) },
    });
  });

  it("does not display a declined payment as a successful sale", async () => {
    const user = userEvent.setup();
    installApi({ userRole: "cashier", checkout: { sale_id: "sale-13", status: "PAYMENT_FAILED", message: "Card declined." } });
    render(<App />);
    await screen.findByRole("option", { name: /HSR Store/ });
    await user.click(await screen.findByRole("button", { name: "Add" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Review & charge" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Review & charge" }));
    await user.click(await screen.findByRole("button", { name: "Confirm charge" }));
    expect(await screen.findByText("Sale payment_failed")).toBeInTheDocument();
    expect(screen.queryByText("Sale complete")).not.toBeInTheDocument();
  });

  it("offers separate staff and customer login and customer registration entry points", async () => {
    const user = userEvent.setup();
    const calls = installApi({ authRequired: true });
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Staff sign in" })).toBeInTheDocument();
    expect(screen.queryByText(/Eluno Studios?/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Customer Login" }));
    expect(screen.getByRole("heading", { name: "Customer sign in" })).toBeInTheDocument();
    expect(screen.getByLabelText("Mobile number")).toBeRequired();
    await user.click(screen.getByRole("button", { name: "Create Customer Account" }));
    expect(screen.getByLabelText("Full name")).toBeRequired();
    await user.type(screen.getByLabelText("Full name"), "Mira Rao");
    await user.type(screen.getByLabelText("Mobile number"), "9000000002");
    await user.type(screen.getByLabelText("Email (optional)"), "mira@example.com");
    await user.type(screen.getByLabelText("Password"), "Mira-Customer-Password-2026!");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Account created. Sign in with your mobile number and password.");
    expect(calls.find((call) => call.url === "/api/customer-auth/register")?.body).toMatchObject({
      name: "Mira Rao",
      phone: "+919000000002",
      email: "mira@example.com",
    });
  });

  it("restores a customer session into the customer-owned portal, not the staff workspace", async () => {
    const user = userEvent.setup();
    const calls = installApi({ authRequired: true, customerAuthenticated: true });
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Browse eyewear" })).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Add frame" }));
    expect(await screen.findByRole("heading", { name: "Offers" })).toBeInTheDocument();
    const customerPrice = calls.find((call) => call.url === "/api/customer/pricing");
    expect(customerPrice?.body).toMatchObject({
      cart: { location_id: "7", lines: [{ variant_id: "frame-100", quantity: 1 }] },
    });
    expect(customerPrice?.body).not.toHaveProperty("customer_id");
    await user.click(screen.getByRole("button", { name: "My Orders" }));
    expect(await screen.findByRole("heading", { name: "My orders" })).toBeInTheDocument();
    expect(calls.some((call) => call.url.startsWith("/api/customer/orders"))).toBe(true);
    expect(screen.queryByRole("navigation", { name: "Workspace" })).not.toBeInTheDocument();
  });

  it("shows only eligible customer offers and supports manual selection", async () => {
    const user = userEvent.setup();
    const offerEvaluation = [
      { offer_id: "OFFER-10", name: "Pair offer", stackable: true, status: "applied" as const, reason: "Selected.", discount_paise: 10000 },
      { offer_id: "OFFER-20", name: "Frame offer", stackable: true, status: "eligible" as const, reason: "Conditions met.", discount_paise: 0 },
      { offer_id: "OFFER-30", name: "Inactive offer", stackable: true, status: "rejected" as const, reason: "Inactive.", discount_paise: 0 },
    ];
    const calls = installApi({
      authRequired: true,
      customerAuthenticated: true,
      customerPricing: { offer_evaluation: offerEvaluation },
    });
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Add frame" }));

    expect(await screen.findByText("Pair offer")).toBeInTheDocument();
    expect(screen.getByText("Frame offer")).toBeInTheDocument();
    expect(screen.queryByText("Inactive offer")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Choose manually" }));
    await user.click(screen.getByRole("checkbox", { name: "Select Frame offer" }));
    await waitFor(() => {
      expect(calls.some((call) =>
        call.url === "/api/customer/pricing" &&
        JSON.stringify(call.body.offer_ids) === JSON.stringify(["OFFER-10", "OFFER-20"]),
      )).toBe(true);
    });

    await user.click(screen.getByRole("button", { name: "Use automatic selection" }));
    await waitFor(() => {
      const lastPricingCall = calls.filter((call) => call.url === "/api/customer/pricing").at(-1);
      expect(lastPricingCall?.body).not.toHaveProperty("offer_ids");
    });
  });

  it("switches the customer catalog between scrollable Frames and Lenses lists", async () => {
    const user = userEvent.setup();
    const frames = Array.from({ length: 7 }, (_, index) => ({
      ...frame, variant_id: `frame-${index}`, product_title: `Frame ${index + 1}`,
    }));
    installApi({ authRequired: true, customerAuthenticated: true, customerCatalog: [...frames, lens] });
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Frames" })).toBeInTheDocument();
    const firstFrame = await screen.findByText("Frame 1 · Black");
    expect(await screen.findByText("Frame 7 · Black")).toBeInTheDocument();
    expect(firstFrame.closest(".customer-catalog-list")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Lenses" }));
    expect(screen.getByRole("heading", { name: "Lenses" })).toBeInTheDocument();
    expect(screen.getByText("Single Vision")).toBeInTheDocument();
    expect(screen.queryByText("Frame 1 · Black")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Frames" }));
    expect(screen.getByRole("heading", { name: "Frames" })).toBeInTheDocument();
  });

  it("records a UPI or card preference without suggesting that online payment was collected", async () => {
    const user = userEvent.setup();
    const calls = installApi({ authRequired: true, customerAuthenticated: true });
    render(<App />);
    await screen.findByRole("heading", { name: "Browse eyewear" });
    await user.click(screen.getByRole("button", { name: "Lenses" }));
    const catalogSearch = screen.getByLabelText("Search eyewear");
    await user.type(catalogSearch, "Single Vision");
    expect(await screen.findByRole("button", { name: "Add with assistance" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add frame" })).not.toBeInTheDocument();
    await user.clear(catalogSearch);
    await user.click(screen.getByRole("button", { name: "Frames" }));
    await user.click(await screen.findByRole("button", { name: "Add frame" }));
    await user.click(await screen.findByRole("button", { name: "Increase quantity of North Frame" }));
    await waitFor(() => {
      const priceRequests = calls.filter((call) => call.url === "/api/customer/pricing");
      expect(priceRequests.at(-1)?.body.cart).toMatchObject({
        lines: [{ variant_id: "frame-100", quantity: 2 }],
      });
    });
    expect(await screen.findByRole("region", { name: "Checkout summary" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Checkout · ₹1,100.00" })).toBeEnabled();
    expect(screen.getByRole("heading", { name: "Offers" })).toBeInTheDocument();
    expect(screen.getByText("Pair offer")).toBeInTheDocument();
    expect(screen.getAllByText("−₹100.00")).toHaveLength(2);
    await user.selectOptions(await screen.findByLabelText("Preferred payment method"), "card");
    expect(screen.getByText(/No UPI or card payment is collected online/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Checkout · ₹1,100.00" }));
    expect(await screen.findByText(/Order request web-created saved. Payment has not been taken/)).toBeInTheDocument();
    const request = calls.find((call) => call.url === "/api/customer/orders" && call.body.payment_method_preference);
    expect(request?.body.payment_method_preference).toBe("card");
    expect(request?.body.cart).toMatchObject({
      lines: [{ variant_id: "frame-100", quantity: 2 }],
    });
  });
});
