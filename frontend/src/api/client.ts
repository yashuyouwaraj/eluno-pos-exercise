import type {
  CatalogVariant,
  AdminFrame,
  AdminCatalogProduct,
  AuthenticatedCustomer,
  AuthenticatedUser,
  BusinessDate,
  CheckoutRequest,
  CustomerRegistration,
  CustomerRegistrationRequest,
  CustomerOrder,
  StoreOrderDecision,
  StoreOrderQueueItem,
  DeliveryAddress,
  CustomerSearchResult,
  CustomerLookup,
  CustomerProfile,
  DayEndReport,
  LensValidation,
  Location,
  Offer,
  OptometristAssistanceRequest,
  Pricing,
  Prescription,
  PrescriptionApprover,
  RefundPreview,
  RefundResult,
  SaleResult,
  StaffAccount,
  TillSession,
} from "./types";

const REQUEST_TIMEOUT_MS = 10_000;
const CONNECTION_ERROR_MESSAGE =
  "Cannot reach the Studio API. For local use, run `python scripts/start_local.py` and refresh the page. In Vite development, ensure the local API is running on port 8790.";

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(path, {
      ...init,
      credentials: "same-origin",
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
    const text = await response.text();
    let payload: unknown;
    try {
      payload = text ? JSON.parse(text) : {};
    } catch {
      throw new ApiError("The server returned an unreadable response.", response.status);
    }
    if (!response.ok) {
      const body = payload as { message?: string; error?: string };
      if (response.status === 401 && !path.startsWith("/api/auth/") && !path.startsWith("/api/customer-auth/")) {
        window.dispatchEvent(new Event(path.startsWith("/api/customer/") ? "eluno:customer-unauthorized" : "eluno:unauthorized"));
      }
      throw new ApiError(body.message || body.error || `Request failed (${response.status}).`, response.status);
    }
    return payload as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError("The request timed out. Check the operation status before retrying.", 0);
    }
    throw new ApiError(CONNECTION_ERROR_MESSAGE, 0);
  } finally {
    window.clearTimeout(timeout);
  }
}

async function requestHtml(path: string): Promise<string> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(path, {
      credentials: "same-origin",
      signal: controller.signal,
      headers: { Accept: "text/html" },
    });
    const document = await response.text();
    if (!response.ok) {
      let message = `Request failed (${response.status}).`;
      try {
        const payload = JSON.parse(document) as { message?: string; error?: string };
        message = payload.message || payload.error || message;
      } catch { /* Keep the status message for a non-JSON error response. */ }
      if (response.status === 401) window.dispatchEvent(new Event("eluno:unauthorized"));
      throw new ApiError(message, response.status);
    }
    if (!response.headers.get("Content-Type")?.toLowerCase().includes("text/html")) {
      throw new ApiError("The server did not return an invoice document.", response.status);
    }
    return document;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError("The invoice request timed out. Keep the sale reference and retry.", 0);
    }
    throw new ApiError(CONNECTION_ERROR_MESSAGE, 0);
  } finally {
    window.clearTimeout(timeout);
  }
}

const json = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });

export const api = {
  authCurrent: () => request<{ user: AuthenticatedUser }>("/api/auth/me"),
  customerAuthCurrent: () => request<{ customer: AuthenticatedCustomer }>("/api/customer-auth/me"),
  login: (username: string, password: string) =>
    request<{ user: AuthenticatedUser }>("/api/auth/login", json({ username, password })),
  customerLogin: (phone: string, password: string) =>
    request<{ customer: AuthenticatedCustomer }>("/api/customer-auth/login", json({ phone, password })),
  customerRegister: (payload: { name: string; phone: string; email?: string; password: string }) =>
    request<{ customer: AuthenticatedCustomer; message: string }>("/api/customer-auth/register", json(payload)),
  customerLogout: () => request<void>("/api/customer-auth/logout", { method: "POST" }),
  updateCustomerSelf: (payload: { name: string; phone: string; email?: string | null; address: DeliveryAddress }) =>
    request<{ customer: AuthenticatedCustomer }>("/api/customer/profile", { method: "PATCH", body: JSON.stringify(payload) }),
  changeCustomerPassword: (current_password: string, new_password: string) =>
    request<void>("/api/customer/password", json({ current_password, new_password })),
  customerLocations: () => request<{ locations: Location[] }>("/api/customer/locations"),
  customerCatalog: () => request<{ variants: CatalogVariant[] }>("/api/customer/catalog"),
  customerPricing: (payload: {
    cart: { location_id: string; lines: Array<{ variant_id: string; quantity: number; pair_ref?: string; prescription_id?: string; needs_prescription_assistance?: boolean }> };
    offer_ids?: string[];
  }) => request<Pricing & { store_code: string; location_id: string; business_date: string }>("/api/customer/pricing", json(payload)),
  createCustomerOrder: (payload: {
    idempotency_key: string;
    cart: { location_id: string; lines: Array<{ variant_id: string; quantity: number; pair_ref?: string; prescription_id?: string; needs_prescription_assistance?: boolean }> };
    delivery_address: DeliveryAddress;
    offer_ids?: string[];
    payment_method_preference?: "upi" | "card";
  }) => request<{ order: CustomerOrder; duplicate: boolean }>("/api/customer/orders", json(payload)),
  customerOrders: (page = 1) =>
    request<{ orders: CustomerOrder[]; page: number; has_more: boolean }>(`/api/customer/orders?page=${page}`),
  storePendingOrders: () =>
    request<{ orders: StoreOrderQueueItem[] }>("/api/store/orders/pending"),
  decideStoreOrder: (orderId: string, decision: "confirm" | "deny", reason?: string) =>
    request<StoreOrderDecision>(
      `/api/store/orders/${encodeURIComponent(orderId)}/${decision}`,
      json(reason ? { reason } : {}),
    ),
  optometristAssistanceOrders: () =>
    request<{ orders: OptometristAssistanceRequest[] }>("/api/optometrist/orders/assistance"),
  logout: () => request<void>("/api/auth/logout", { method: "POST" }),
  staff: () => request<{ staff: StaffAccount[] }>("/api/admin/staff"),
  createStaff: (payload: { username: string; password: string; role: "admin" | "cashier" | "optometrist"; display_name: string; store_code?: string | null; external_staff_id?: string | null }) =>
    request<{ staff: StaffAccount }>("/api/admin/staff", json(payload)),
  updateStaff: (staffId: string, payload: { password?: string; is_active?: boolean; display_name?: string; role?: "admin" | "cashier"; store_code?: string | null }) =>
    request<{ staff: StaffAccount }>(`/api/admin/staff/${encodeURIComponent(staffId)}`, {
      ...json(payload),
      method: "PATCH",
    }),
  locations: () => request<{ locations: Location[] }>("/api/locations"),
  businessDate: () => request<BusinessDate>("/api/business-date"),
  adminFrames: (locationId: string) =>
    request<{ variants: AdminFrame[] }>(`/api/admin/catalog/frames?location_id=${encodeURIComponent(locationId)}`),
  adminProducts: (lineType?: "frame" | "lens", query = "") =>
    request<{ products: AdminCatalogProduct[] }>(
      `/api/admin/catalog/products${lineType || query ? `?${new URLSearchParams({
        ...(lineType ? { line_type: lineType } : {}),
        ...(query ? { q: query } : {}),
      })}` : ""}`,
    ),
  createProduct: (payload: Record<string, string | number | boolean>) =>
    request<{ product: Record<string, unknown> }>("/api/admin/catalog/products", json(payload)),
  updateProduct: (productId: string, payload: Record<string, string | number | boolean>) =>
    request<{ product: Record<string, unknown> }>(
      `/api/admin/catalog/products/${encodeURIComponent(productId)}`,
      { ...json(payload), method: "PATCH" },
    ),
  openTills: (storeCode: string) =>
    request<{ tills: TillSession[] }>(`/api/tills?store_code=${encodeURIComponent(storeCode)}`),
  adminTillSessions: (storeCode?: string) =>
    request<{ sessions: TillSession[] }>(
      `/api/admin/tills${storeCode ? `?store_code=${encodeURIComponent(storeCode)}` : ""}`,
    ),
  openTill: (payload: { store_code: string; location_id: string; opening_float_paise: number }) =>
    request<TillSession>("/api/tills", json(payload)),
  tillMovement: (sessionId: string, payload: { type: "paid_in" | "paid_out"; amount_paise: number; reason: string; ref: string }) =>
    request<TillSession>(`/api/tills/${encodeURIComponent(sessionId)}/movements`, json(payload)),
  closeTill: (sessionId: string, payload: { counted_cash_paise: number; review_note?: string }) =>
    request<TillSession>(`/api/tills/${encodeURIComponent(sessionId)}/close`, json(payload)),
  reviewTill: (sessionId: string, reviewNote: string) =>
    request<TillSession>(`/api/admin/tills/${encodeURIComponent(sessionId)}/review`, json({ review_note: reviewNote })),
  receiveStock: (payload: { inventory_item_id: number; location_id: number; quantity: number; actor: string; reference: string }) =>
    request<{ received_quantity: number; idempotent_replay: boolean }>("/api/admin/inventory/receipts", json(payload)),
  customer: (phone: string, businessDate: string) =>
    request<CustomerLookup>(`/api/customer-prescription?phone=${encodeURIComponent(phone)}&business_date=${encodeURIComponent(businessDate)}`),
  registerCustomer: (payload: CustomerRegistrationRequest) =>
    request<CustomerRegistration>("/api/customers", json(payload)),
  customerProfile: (customerId: string | number, page = 1) =>
    request<CustomerProfile>(`/api/customers/${encodeURIComponent(String(customerId))}?page=${page}`),
  searchCustomers: (query: string, page = 1) =>
    request<{ customers: CustomerSearchResult[]; page: number; has_more: boolean }>(
      `/api/customers/search?q=${encodeURIComponent(query)}&page=${page}`,
    ),
  updateCustomer: (customerId: string | number, payload: { first_name?: string; last_name?: string; email?: string | null }) =>
    request<{ customer: CustomerProfile["customer"] }>(`/api/customers/${encodeURIComponent(String(customerId))}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  anonymizeCustomer: (customerId: string | number, reason: string) =>
    request<{ customer: CustomerProfile["customer"]; message: string }>(
      `/api/customers/${encodeURIComponent(String(customerId))}/anonymize`,
      json({ reason }),
    ),
  prescriptions: (phone: string) =>
    request<{ prescriptions: Prescription[] }>(`/api/prescriptions?phone=${encodeURIComponent(phone)}`),
  prescriptionApprovers: (storeCode: string) =>
    request<{ approvers: PrescriptionApprover[] }>(`/api/prescription-approvers?store_code=${encodeURIComponent(storeCode)}`),
  createPrescription: (payload: {
    phone: string;
    store_code: string;
    optometrist_id: string;
    date: string;
    valid_until: string;
    od: { sph: number; cyl: number; axis: number | null; add: number | null };
    os: { sph: number; cyl: number; axis: number | null; add: number | null };
    optometrist_confirmed: true;
    idempotency_key: string;
  }) => request<{ prescription: Prescription; warnings: string[] }>("/api/prescriptions", json(payload)),
  catalog: (type: "frame" | "lens") =>
    request<{ variants: CatalogVariant[] }>(`/api/catalog/variants?line_type=${type}`),
  offers: (date: string) =>
    request<{ offers: Offer[] }>(`/api/offers?business_date=${encodeURIComponent(date)}`),
  validateLens: (lens: { lens_type: string; index: string; coating: string }, prescription: unknown) =>
    request<LensValidation>("/api/lens-validation", json({ lens, prescription })),
  price: (payload: CheckoutRequest) => request<Pricing>("/api/pricing-preview", json(payload)),
  checkout: (payload: CheckoutRequest) => request<SaleResult>("/api/checkouts", json(payload)),
  invoiceDocument: (saleId: string) =>
    requestHtml(`/api/invoices/${encodeURIComponent(saleId)}`),
  recoverSale: (saleId: string, operator: string) =>
    request<SaleResult>(`/api/checkouts/${encodeURIComponent(saleId)}/recover`, json({ operator })),
  refundPreview: (identifier: string, storeCode?: string) =>
    request<RefundPreview>(`/api/refunds/lookup?identifier=${encodeURIComponent(identifier)}${storeCode ? `&store_code=${encodeURIComponent(storeCode)}` : ""}`),
  refund: (payload: {
    order_id: string;
    invoice_no: string | null;
    store_code: string;
    operator: string;
    reason: string;
    idempotency_key: string;
    till_session_id?: string;
    items?: Array<{ line_item_id: string; quantity: number }>;
    return_item_ids?: string[];
  }) => request<RefundResult>("/api/refunds", json(payload)),
  refundStatus: (refundId: string) =>
    request<RefundResult>(`/api/refunds/${encodeURIComponent(refundId)}`),
  recoverRefund: (refundId: string) =>
    request<RefundResult>(`/api/refunds/${encodeURIComponent(refundId)}/recover`, json({})),
  dayEnd: (storeId: string, date: string) =>
    request<DayEndReport>(`/api/reports/day-end?store_id=${encodeURIComponent(storeId)}&date=${encodeURIComponent(date)}`),
  overallReport: (date: string) =>
    request<DayEndReport>(`/api/reports/overall?date=${encodeURIComponent(date)}`),
};
