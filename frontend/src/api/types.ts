export interface Location {
  id: number | string;
  name: string;
  store_code: string;
}

export interface AuthenticatedUser {
  username: string;
  staff_id: string;
  operator_id?: string;
  store_code?: string | null;
  external_staff_id?: string | null;
  role: "admin" | "cashier" | "optometrist";
  display_name: string;
}

export interface AuthenticatedCustomer {
  customer_id: string;
  shopify_customer_id: string;
  name: string;
  phone: string;
  email: string | null;
  address: DeliveryAddress;
}

export interface DeliveryAddress {
  line1: string;
  line2: string;
  city: string;
  state: string;
  postal_code: string;
}

export interface CustomerOrder {
  order_id: string;
  status:
    | "awaiting_store_review"
    | "sold"
    | "partially_refunded"
    | "refunded"
    | "payment_failed"
    | "payment_reconciliation_required"
    | "cancelled";
  store_confirmation_status?: "pending" | "confirmed" | "denied";
  created_at: string;
  store_code: string;
  location_id: string;
  customer_name: string;
  customer_phone: string;
  delivery_address: DeliveryAddress;
  items: Array<{
    variant_id: string;
    title: string;
    line_type: "frame" | "lens";
    quantity: number;
    prescription_assistance_requested: boolean;
    gross_paise: number;
    discount_paise: number;
    taxable_paise: number;
    cgst_paise: number;
    sgst_paise: number;
    tax_paise: number;
    total_paise: number;
  }>;
  offers: Array<{ offer_id: string; discount_paise: number }>;
  gross_paise: number;
  discount_paise: number;
  taxable_paise: number;
  cgst_paise: number;
  sgst_paise: number;
  tax_paise: number;
  total_paise: number;
  sale_id?: string | null;
  shopify_order_id?: string | null;
  invoice_no?: string | null;
  refund_id?: string | null;
  refund_status?: "COMPLETED" | "PENDING" | "REFUND_UNKNOWN" | "REFUND_RECONCILING";
  refund_amount_paise?: number;
  credit_note?: string | null;
  payment_status: "awaiting_payment" | "paid" | "payment_failed" | "payment_reconciliation_required";
  payment_method_preference?: "upi" | "card" | null;
  fulfillment_status: "not_submitted";
  tracking_available: false;
  status_message: string;
}

export interface StoreOrderQueueItem {
  order_id: string;
  created_at: string;
  store_code: string;
  customer_name: string;
  items: Array<{
    title: string;
    quantity: number;
    prescription_assistance_requested: boolean;
  }>;
  total_paise: number;
  payment_status: "awaiting_payment" | "paid";
  payment_method_preference?: "upi" | "card" | null;
  invoice_no?: string | null;
  has_prescription_assistance: boolean;
  store_confirmation_status: "pending";
  status_message: string;
}

export interface StoreOrderDecision {
  order: {
    order_id: string;
    status: "sold" | "partially_refunded" | "refunded" | "cancelled";
    store_confirmation_status: "confirmed" | "denied";
    store_decision_by: string;
    store_decision_at: string;
    status_message: string;
    payment_status: "paid";
    invoice_no?: string | null;
    refund_id?: string;
    refund_status?: "COMPLETED";
    refund_amount_paise?: number;
  };
}

export interface OptometristAssistanceRequest {
  order_id: string;
  created_at: string;
  store_code: string;
  customer_id?: string;
  customer_name: string;
  customer_phone: string;
  items: Array<{ title: string; quantity: number }>;
  payment_status: "awaiting_payment" | "paid";
  store_confirmation_status: "confirmed";
  status_message: string;
}

export interface StaffAccount {
  staff_id: string;
  operator_id?: string;
  username: string;
  display_name: string;
  role: "admin" | "cashier" | "optometrist";
  is_active: boolean;
  created_at: string;
  updated_at: string;
  store_code?: string | null;
  external_staff_id?: string | null;
}

export interface AdminCatalogProduct {
  product_id: string;
  variant_id: string;
  line_type: "frame" | "lens";
  title: string;
  description: string;
  status: "active" | "archived";
  sku: string;
  variant_title: string;
  price_paise: number;
  category: string | null;
  material: string | null;
  size: string | null;
  rx_able: boolean | null;
  lens_type: string | null;
  index: number | null;
  coating: string | null;
}

export interface TillMovement {
  id: string;
  type: "paid_in" | "paid_out" | "cash_sale" | "cash_refund";
  amount_paise: number;
  reason: string | null;
  operator: string;
  ref: string;
  created_at: string;
}

export interface TillSession {
  session_id: string;
  store_code: string;
  location_id: string;
  operator: string;
  opened_by: string;
  opened_at: string;
  opening_paise: number;
  opening_float_paise: number;
  status: "OPEN" | "CLOSED";
  counted_paise: number | null;
  counted_cash_paise: number | null;
  expected_paise: number;
  variance_paise: number | null;
  closed_at: string | null;
  closed_by: string | null;
  review_note: string | null;
  review_status: "UNREVIEWED" | "CLEAR" | "REVIEW_REQUIRED" | "REVIEWED";
  is_fixture: boolean;
  movements: TillMovement[];
}

export interface BusinessDate {
  business_date: string;
  timezone: string;
}

export interface AdminFrame {
  variant_id: number | string;
  inventory_item_id: number | string;
  product_title: string;
  variant_title: string;
  sku: string;
  price_paise: number | null;
  category: string | null;
  material: string | null;
  size: string | null;
  available: number | null;
  location_id: number | null;
}

export interface Eye {
  sph: string | number | null;
  cyl: string | number | null;
  axis: number | null;
  add: string | number | null;
}

export interface Customer {
  id: number | string;
  phone: string;
  first_name: string;
  last_name: string;
  tags: string[];
}

export interface Prescription {
  rx_id: string;
  customer_id: number | string;
  phone: string;
  date: string;
  valid_until: string;
  od: Eye;
  os: Eye;
  optometrist_id?: string;
  store_code?: string;
  notes?: string;
}

export interface PrescriptionApprover {
  staff_id: string;
  display_name: string;
}

export interface CustomerProfile {
  customer: Customer & {
    email?: string | null;
    created_at?: string | null;
    note?: string | null;
    anonymized?: boolean;
  };
  purchase_count: number;
  lifetime_spend_paise: number;
  most_recent_purchase: string | null;
  history_complete: boolean;
  purchase_page?: number;
  has_more_purchases?: boolean;
  deletion_policy: string;
  prescriptions: Prescription[];
  purchases: Array<{
    order_id: string;
    shopify_reference: string;
    invoice_no: string | null;
    sale_id: string | null;
    date: string | null;
    store_code: string | null;
    items: Array<{ title: string; quantity: number; discount_paise: number; tax_paise: number; line_total_paise: number }>;
    gross_paise: number;
    discount_paise: number;
    tax_paise: number;
    cgst_paise: number;
    sgst_paise: number;
    total_paise: number;
    refunded_paise: number;
    payment_method: string | null;
    status: string;
    fulfillment_status: string;
    refunds: Array<{ id: string; created_at: string | null; amount_paise: number; tax_paise: number; reason: string | null }>;
  }>;
}

export interface CustomerSearchResult {
  id: string;
  name: string;
  phone: string | null;
  email: string | null;
  total_spent_paise: number;
  order_count: number;
  total_refunded_paise: number;
  most_recent_purchase: string | null;
  created_at: string | null;
}

export interface CustomerLookup {
  status: string;
  message: string;
  customer?: Customer;
  prescription?: Prescription;
}

export interface CustomerRegistrationRequest {
  name: string;
  phone: string;
}

export interface CustomerRegistration {
  customer: Omit<Customer, "tags"> & { tags?: string[] };
  created: boolean;
}

export interface CatalogVariant {
  variant_id: string;
  line_type: "frame" | "lens";
  product_title: string;
  variant_title: string;
  price_paise: number;
  lens_type: string | null;
  index: string | null;
  coating: string | null;
}

export interface Offer {
  offer_id: string;
  name: string;
  stackable: boolean;
}

export interface PricingLine {
  variant_id: string;
  quantity: number;
  gross_paise: number;
  discount_paise: number;
  taxable_paise: number;
  cgst_paise: number;
  sgst_paise: number;
  tax_paise: number;
  payable_paise: number;
}

export interface Pricing {
  gross_paise: number;
  discount_paise: number;
  taxable_paise: number;
  cgst_paise: number;
  sgst_paise: number;
  tax_paise: number;
  payable_paise: number;
  lines: PricingLine[];
  offers: Array<{ offer_id: string; discount_paise: number }>;
  offer_evaluation: OfferEvaluation[];
}

export interface OfferEvaluation {
  offer_id: string;
  name: string;
  stackable: boolean | null;
  status: "eligible" | "applied" | "rejected";
  reason: string;
  discount_paise: number;
}

export interface LensValidation {
  valid: boolean;
  errors: string[];
  warnings: string[];
  variant_id?: string;
}

export interface SaleResult {
  sale_id?: string;
  status: string;
  order_id?: string | null;
  invoice_no?: string | null;
  message?: string;
}

export interface RefundPreview {
  order_id: string;
  invoice_no: string | null;
  name: string | null;
  created_at?: string;
  customer_name: string | null;
  store_code: string;
  financial_status: string | null;
  total_paise: number;
  refunded_paise: number;
  remaining_refundable_paise: number;
  refund_count: number;
  payment_gateway: string | null;
  available_tills: Array<{ session_id: string; location_id: string; store_code: string }>;
  refund_eligible: boolean;
  ineligible_reasons: string[];
  payment_capability: string;
  lines: Array<{
    line_item_id: string;
    title: string;
    quantity: number;
    refunded_quantity: number;
    remaining_quantity: number;
    discount_paise: number;
    tax_paise: number;
    remaining_refundable_paise: number;
    remaining_tax_paise: number;
    line_refundable_paise: number;
    line_type: string | null;
    restockable: boolean;
  }>;
  previous_refunds: Array<{ refund_id: string; created_at: string | null; amount_paise: number; tax_paise: number }>;
}

export interface RefundResult {
  refund_id: string;
  terminal_refund_id?: string | null;
  order_id: string;
  invoice_no: string | null;
  status: string;
  credit_note?: string;
  amount_paise?: number;
  error?: string | null;
}

export interface DayEndReport {
  store_id: string;
  store_code: string;
  business_date: string;
  completed_bill_count: number;
  metrics: Record<string, number>;
  payment_methods_paise: Record<string, number>;
  channels: Record<string, Record<string, number>>;
  refund_count: number;
  exceptions: unknown[];
  stores?: DayEndReport[];
}

export interface CheckoutRequest {
  idempotency_key: string;
  cart: {
    store_code: string;
    location_id: string;
    lines: Array<{
      variant_id: string;
      quantity: number;
      pair_ref?: string;
      prescription_id?: string;
    }>;
  };
  business_date: string;
  payment_method: string;
  channel: string;
  staff_id: string;
  customer_id: string | number | null;
  customer_phone: string | null;
  offer_ids?: string[];
  reviewed_prescription_ids: string[];
}
