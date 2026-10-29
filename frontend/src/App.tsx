import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { ApiError, api } from "./api/client";
import type {
  CatalogVariant,
  AdminFrame,
  AdminCatalogProduct,
  AuthenticatedCustomer,
  AuthenticatedUser,
  CheckoutRequest,
  Customer,
  CustomerProfile,
  CustomerLookup,
  CustomerSearchResult,
  CustomerOrder,
  DeliveryAddress,
  DayEndReport,
  LensValidation,
  Location,
  Pricing,
  Prescription,
  PrescriptionApprover,
  RefundPreview,
  RefundResult,
  SaleResult,
  OptometristAssistanceRequest,
  StoreOrderQueueItem,
  StaffAccount,
  TillSession,
} from "./api/types";

type View = "studio" | "checkout" | "admin" | "customers" | "prescriptions" | "staff" | "refund" | "report" | "till" | "store-orders";
type CartLine = { variant: CatalogVariant; quantity: number; pairRef?: string };

const currency = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" });
const reportMetricLabels: Record<string, string> = {
  gross_sales_paise: "Gross Sales",
  discounts_paise: "Discounts",
  net_sales_before_refunds_paise: "Net Sales Before Refunds",
  taxable_sales_paise: "Taxable Sales",
  cgst_paise: "CGST",
  sgst_paise: "SGST",
  tax_sales_paise: "Total Tax",
  refund_amount_paise: "Refund Amount",
  refund_tax_paise: "Refund Tax",
  net_sales_after_refunds_paise: "Net Sales After Refunds",
  net_tax_after_refunds_paise: "Net Tax After Refunds",
};
const money = (paise: number | undefined) => currency.format((paise || 0) / 100);
const parsePaise = (value: string) => {
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(value.trim());
  if (!match) throw new Error("Enter a nonnegative amount with up to two decimal places.");
  const paise = BigInt(match[1]) * 100n + BigInt((match[2] || "").padEnd(2, "0") || "0");
  if (paise > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("Amount is too large.");
  return Number(paise);
};
const allocatePaise = (amount: number, quantity: number, totalQuantity: number) =>
  Number(BigInt(amount) * BigInt(quantity) / BigInt(totalQuantity));
const makeKey = () => globalThis.crypto?.randomUUID?.() || `eluno-${Date.now()}-${Math.random()}`;
const strongPassword = (value: string) =>
  value.length >= 12 &&
  /[a-z]/.test(value) &&
  /[A-Z]/.test(value) &&
  /\d/.test(value) &&
  /[^A-Za-z0-9]/.test(value);
const normalizePhone = (value: string) => {
  const compact = value.trim().replace(/[ -]/g, "");
  const normalized = compact.length === 10 && /^\d+$/.test(compact) ? `+91${compact}` : compact;
  if (!/^\+91[6-9]\d{9}$/.test(normalized)) {
    throw new Error("Enter a valid 10-digit Indian mobile number, optionally prefixed with +91.");
  }
  return normalized;
};
const customerName = (customer: Customer) =>
  [customer.first_name, customer.last_name].filter(Boolean).join(" ") || "Customer";
const rxAxisWarnings = (rx?: Prescription | null) =>
  rx
    ? (["OD", "OS"] as const)
        .map((eye) => {
          const value = rx[eye.toLowerCase() as "od" | "os"].axis;
          return value != null && value % 5 !== 0
            ? `${eye} axis ${value} is off the 5-degree grid and requires optometrist review.`
            : "";
        })
        .filter(Boolean)
    : [];

export default function App() {
  const [user, setUser] = useState<AuthenticatedUser | null>(null);
  const [customer, setCustomer] = useState<AuthenticatedCustomer | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [authMessage, setAuthMessage] = useState("");

  useEffect(() => {
    let active = true;
    const restoreSession = async () => {
      try {
        const { user: current } = await api.authCurrent();
        if (active) setUser(current);
      } catch (staffError) {
        if (!(staffError instanceof ApiError) || staffError.status !== 401) {
          if (active) setAuthMessage(reportAuthError(staffError));
        } else {
          try {
            const { customer: current } = await api.customerAuthCurrent();
            if (active) setCustomer(current);
          } catch (customerError) {
            if (!(customerError instanceof ApiError) || customerError.status !== 401) {
              if (active) setAuthMessage(reportAuthError(customerError));
            }
          }
        }
      } finally {
        if (active) setAuthLoading(false);
      }
    };
    void restoreSession();
    return () => { active = false; };
  }, []);

  useEffect(() => {
    const handleUnauthorized = () => {
      setUser(null);
      setAuthMessage("Your Studio session expired or was disabled. Sign in again.");
    };
    const handleCustomerUnauthorized = () => {
      setCustomer(null);
      setAuthMessage("Your customer session expired. Sign in again.");
    };
    window.addEventListener("eluno:unauthorized", handleUnauthorized);
    window.addEventListener("eluno:customer-unauthorized", handleCustomerUnauthorized);
    return () => {
      window.removeEventListener("eluno:unauthorized", handleUnauthorized);
      window.removeEventListener("eluno:customer-unauthorized", handleCustomerUnauthorized);
    };
  }, []);

  if (authLoading) return <main className="auth-shell"><p role="status">Checking Studio session…</p></main>;
  if (customer) return <CustomerPortal customer={customer} onCustomerChange={setCustomer} onLogout={() => setCustomer(null)} />;
  if (!user) return <LoginScreen message={authMessage} onLogin={setUser} onCustomerLogin={setCustomer} />;
  return <Workspace key={user.username} user={user} onLogout={() => setUser(null)} />;
}

const reportAuthError = (error: unknown) =>
  error instanceof Error ? error.message : "The Studio sign-in service is unavailable.";

type CustomerCartLine = {
  variant: CatalogVariant;
  quantity: number;
  pairRef?: string;
  needsAssistance: boolean;
};

function CustomerPortal({
  customer,
  onCustomerChange,
  onLogout,
}: {
  customer: AuthenticatedCustomer;
  onCustomerChange: (customer: AuthenticatedCustomer) => void;
  onLogout: () => void;
}) {
  const [section, setSection] = useState<"shop" | "orders" | "profile">("shop");
  const [locations, setLocations] = useState<Location[]>([]);
  const [catalog, setCatalog] = useState<CatalogVariant[]>([]);
  const [catalogQuery, setCatalogQuery] = useState("");
  const [catalogType, setCatalogType] = useState<"frame" | "lens">("frame");
  const [locationId, setLocationId] = useState("");
  const [cart, setCart] = useState<CustomerCartLine[]>([]);
  const [pricing, setPricing] = useState<(Pricing & { store_code: string; location_id: string; business_date: string }) | null>(null);
  const [pricingLoading, setPricingLoading] = useState(false);
  const [manualOfferIds, setManualOfferIds] = useState<string[] | null>(null);
  const [orders, setOrders] = useState<CustomerOrder[]>([]);
  const [orderPage, setOrderPage] = useState(1);
  const [hasMoreOrders, setHasMoreOrders] = useState(false);
  const [profile, setProfile] = useState<{
    name: string;
    phone: string;
    email: string;
    address: DeliveryAddress;
  }>({
    name: customer.name,
    phone: customer.phone,
    email: customer.email || "",
    address: {
      line1: customer.address?.line1 || "",
      line2: customer.address?.line2 || "",
      city: customer.address?.city || "",
      state: customer.address?.state || "",
      postal_code: customer.address?.postal_code || "",
    },
  });
  const [profileError, setProfileError] = useState("");
  const [profileSaved, setProfileSaved] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [needsAssistance, setNeedsAssistance] = useState(true);
  const [paymentMethodPreference, setPaymentMethodPreference] = useState<"upi" | "card">("upi");
  const [passwordDraft, setPasswordDraft] = useState({ current: "", next: "" });
  const orderKey = useRef(makeKey());
  const selectedLocation = locations.find((location) => String(location.id) === locationId);
  const customerCatalog = catalog.filter((item) =>
    [item.product_title, item.variant_title, item.lens_type, item.index, item.coating]
      .filter(Boolean)
      .join(" ")
      .toLocaleLowerCase()
      .includes(catalogQuery.trim().toLocaleLowerCase()),
  );

  useEffect(() => {
    let active = true;
    Promise.all([api.customerLocations(), api.customerCatalog()])
      .then(([locationResult, catalogResult]) => {
        if (!active) return;
        setLocations(locationResult.locations);
        setCatalog(catalogResult.variants);
        setLocationId((current) => current || String(locationResult.locations[0]?.id || ""));
      })
      .catch((requestError: unknown) => { if (active) setError(reportAuthError(requestError)); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    const fetchPricing = async () => {
      if (!locationId || !cart.length) {
        setPricing(null);
        return;
      }
      setPricingLoading(true);
      setError("");
      try {
        const result = await api.customerPricing({
          cart: {
            location_id: locationId,
            lines: cart.map((line) => ({
              variant_id: line.variant.variant_id,
              quantity: line.quantity,
              pair_ref: line.pairRef,
              needs_prescription_assistance: line.needsAssistance,
            })),
          },
          ...(manualOfferIds !== null ? { offer_ids: manualOfferIds } : {}),
        });
        if (active) setPricing(result);
      } catch (requestError) {
        if (active) {
          setPricing(null);
          setError(reportAuthError(requestError));
        }
      } finally {
        if (active) setPricingLoading(false);
      }
    };
    void fetchPricing();
    return () => { active = false; };
  }, [cart, locationId, manualOfferIds]);

  const loadOrders = useCallback(async (page: number) => {
    try {
      const result = await api.customerOrders(page);
      setOrders(result.orders);
      setOrderPage(result.page);
      setHasMoreOrders(result.has_more);
      setError("");
    } catch (requestError) {
      setError(reportAuthError(requestError));
    }
  }, []);

  useEffect(() => {
    if (section !== "orders") return;
    void loadOrders(orderPage);
    const timer = window.setInterval(() => void loadOrders(orderPage), 15_000);
    const refreshOnFocus = () => void loadOrders(orderPage);
    window.addEventListener("focus", refreshOnFocus);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", refreshOnFocus);
    };
  }, [section, orderPage, loadOrders]);

  const addFrame = (variant: CatalogVariant) => {
    const pairRef = `pair-${makeKey()}`;
    setCart((current) => [...current, { variant, quantity: 1, pairRef, needsAssistance: false }]);
    setManualOfferIds(null);
    setPricing(null);
    setNotice("");
    orderKey.current = makeKey();
  };
  const addLens = (variant: CatalogVariant) => {
    const frame = [...cart].reverse().find((line) => line.variant.line_type === "frame");
    const pairRef = frame?.pairRef || `pair-${makeKey()}`;
    setCart((current) => [...current, {
      variant, quantity: 1, pairRef, needsAssistance,
    }]);
    setManualOfferIds(null);
    setPricing(null);
    setNotice("");
    orderKey.current = makeKey();
  };
  const removeLine = (index: number) => {
    setCart((current) => current.filter((_, position) => position !== index));
    setManualOfferIds(null);
    setPricing(null);
    orderKey.current = makeKey();
  };
  const updateLineQuantity = (index: number, change: -1 | 1) => {
    setCart((current) => current.map((line, position) =>
      position === index ? { ...line, quantity: Math.max(1, line.quantity + change) } : line,
    ));
    setManualOfferIds(null);
    setPricing(null);
    orderKey.current = makeKey();
  };
  const toggleCustomerOffer = (offerId: string) => {
    const availableOffers = pricing?.offer_evaluation.filter((offer) => offer.status !== "rejected") || [];
    const offer = availableOffers.find((item) => item.offer_id === offerId);
    if (!offer) return;
    setManualOfferIds((current) => {
      const selected = current || [];
      if (selected.includes(offerId)) return selected.filter((id) => id !== offerId);
      const selectedOffers = availableOffers.filter((item) => selected.includes(item.offer_id));
      if (offer.stackable !== true || selectedOffers.some((item) => item.stackable !== true)) {
        return [offerId];
      }
      return [...selected, offerId];
    });
  };

  const saveProfile = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setProfileError("");
    setProfileSaved("");
    try {
      const result = await api.updateCustomerSelf({
        ...profile,
        email: profile.email.trim() || null,
      });
      onCustomerChange(result.customer);
      setProfile({
        name: result.customer.name,
        phone: result.customer.phone,
        email: result.customer.email || "",
        address: {
          line1: result.customer.address?.line1 || "",
          line2: result.customer.address?.line2 || "",
          city: result.customer.address?.city || "",
          state: result.customer.address?.state || "",
          postal_code: result.customer.address?.postal_code || "",
        },
      });
      setProfileSaved("Profile and delivery address saved.");
    } catch (requestError) {
      setProfileError(reportAuthError(requestError));
    } finally {
      setBusy(false);
    }
  };

  const placeOrder = async () => {
    if (!selectedLocation || !pricing || busy) return;
    if (!profile.address.line1 || !profile.address.city || !profile.address.state || !profile.address.postal_code) {
      setSection("profile");
      setProfileError("Add a complete delivery address before placing an order.");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api.createCustomerOrder({
        idempotency_key: orderKey.current,
        cart: {
          location_id: locationId,
          lines: cart.map((line) => ({
            variant_id: line.variant.variant_id,
            quantity: line.quantity,
            pair_ref: line.pairRef,
            needs_prescription_assistance: line.needsAssistance,
          })),
        },
        delivery_address: profile.address,
        offer_ids: pricing.offers.map((offer) => offer.offer_id),
        payment_method_preference: paymentMethodPreference,
      });
      setNotice(`Order request ${result.order.order_id} saved. Payment has not been taken; the order is awaiting store confirmation.`);
      setCart([]);
      setSection("orders");
      setOrderPage(1);
      orderKey.current = makeKey();
    } catch (requestError) {
      setError(reportAuthError(requestError));
    } finally {
      setBusy(false);
    }
  };

  const changePassword = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!strongPassword(passwordDraft.next)) {
      setProfileError("New password must be at least 12 characters with uppercase and lowercase letters, a number, and a symbol.");
      return;
    }
    setBusy(true);
    try {
      await api.changeCustomerPassword(passwordDraft.current, passwordDraft.next);
      onLogout();
    } catch (requestError) {
      setProfileError(reportAuthError(requestError));
    } finally {
      setBusy(false);
    }
  };

  const logout = async () => {
    try {
      await api.customerLogout();
      onLogout();
    } catch (requestError) {
      setError(reportAuthError(requestError));
    }
  };

  return <div className="customer-portal">
    <header className="customer-portal-header">
      <a className="brand" href="/" aria-label=" | Premium Eyeglasses, Sunglasses & Clip-On">
        <span className="brand-mark"><img className="brand-logo" src="/pos/images/eluno.png" alt="Eluno optical retail" /></span>
        <span className="brand-tagline">| Premium Eyeglasses, Sunglasses &amp; Clip-On</span>
      </a>
      <div><strong>{customer.name}</strong><span>{customer.phone}</span><button className="button secondary" type="button" onClick={() => void logout()}>Sign out</button></div>
    </header>
    <nav className="customer-portal-nav" aria-label="Customer account">
      <button type="button" className={`button ${section === "shop" ? "primary" : "secondary"}`} onClick={() => setSection("shop")}>Shop</button>
      <button type="button" className={`button ${section === "orders" ? "primary" : "secondary"}`} onClick={() => { setSection("orders"); setOrderPage(1); }}>My Orders</button>
      <button type="button" className={`button ${section === "profile" ? "primary" : "secondary"}`} onClick={() => setSection("profile")}>Profile & Delivery</button>
    </nav>
    <main className={`customer-portal-main${section === "shop" && cart.length ? " has-customer-checkout-bar" : ""}`}>
      {notice && <p className="notice success" role="status">{notice}</p>}
      {error && <p className="notice error" role="alert">{error}</p>}
      {section === "shop" && <div className="customer-shop-grid">
        <section className="panel">
          <h1>Browse eyewear</h1>
          <p className="muted small">Prices and offers are calculated by Eluno's server. Stock is checked at the selected store when you review and submit.</p>
          <label className="field">Store
            <select value={locationId} onChange={(event) => { setLocationId(event.target.value); setManualOfferIds(null); setPricing(null); orderKey.current = makeKey(); }}>
              {locations.map((location) => <option key={location.id} value={String(location.id)}>{location.store_code} — {location.name}</option>)}
            </select>
          </label>
          <label className="field">Search eyewear
            <input type="search" value={catalogQuery} onChange={(event) => setCatalogQuery(event.target.value)} placeholder="Search frames, lens type, or coating" />
          </label>
          <div className="segmented customer-catalog-tabs" role="group" aria-label="Eyewear type">
            <button type="button" className={catalogType === "frame" ? "selected" : ""} aria-pressed={catalogType === "frame"} onClick={() => setCatalogType("frame")}>Frames</button>
            <button type="button" className={catalogType === "lens" ? "selected" : ""} aria-pressed={catalogType === "lens"} onClick={() => setCatalogType("lens")}>Lenses</button>
          </div>
          {catalogType === "frame" ? <>
          <h2>Frames</h2>
          <div className="customer-catalog-list">
            {customerCatalog.filter((item) => item.line_type === "frame").map((item) => <article key={item.variant_id} className="catalog-row">
              <div><strong>{[item.product_title, item.variant_title].filter(Boolean).join(" · ")}</strong><small>{item.variant_id}</small></div>
              <strong>{money(item.price_paise)}</strong>
              <button className="button add" type="button" onClick={() => addFrame(item)}>Add frame</button>
            </article>)}
            {!customerCatalog.some((item) => item.line_type === "frame") && <p className="muted small">No frames match this search.</p>}
          </div>
          </> : <>
          <h2>Lenses</h2>
          <label className="check-line"><input type="checkbox" checked={needsAssistance} onChange={(event) => setNeedsAssistance(event.target.checked)} />Request optometrist assistance with my prescription</label>
          {!needsAssistance && <p className="notice">Online prescription selection is not enabled yet. Choose assistance to have the store confirm the prescription before payment.</p>}
          <div className="customer-catalog-list">
            {customerCatalog.filter((item) => item.line_type === "lens").map((item) => <article key={item.variant_id} className="catalog-row">
              <div><strong>{item.product_title}</strong><small>{[item.lens_type, item.index, item.coating].filter(Boolean).join(" · ") || item.variant_title} · {item.variant_id}</small></div>
              <strong>{money(item.price_paise)}</strong>
              <button className="button add" type="button" onClick={() => addLens(item)} disabled={!needsAssistance}>Add with assistance</button>
            </article>)}
            {!customerCatalog.some((item) => item.line_type === "lens") && <p className="muted small">No lenses match this search.</p>}
          </div>
          </>}
        </section>
        <aside className="panel customer-cart">
          <h2>Your selection</h2>
          {!cart.length && <p className="muted">Your selection is empty.</p>}
          {cart.map((line, index) => <div key={`${line.variant.variant_id}-${index}`} className="customer-cart-line">
            <div><strong>{line.variant.product_title}</strong><span>{line.variant.variant_title}</span>
              <span className="customer-quantity-control">
                <button type="button" className="button text" aria-label={`Decrease quantity of ${line.variant.product_title}`} disabled={line.quantity <= 1} onClick={() => updateLineQuantity(index, -1)}>−</button>
                <span>Qty {line.quantity}</span>
                <button type="button" className="button text" aria-label={`Increase quantity of ${line.variant.product_title}`} onClick={() => updateLineQuantity(index, 1)}>+</button>
              </span>
            </div>
            <button type="button" className="button text" onClick={() => removeLine(index)} aria-label={`Remove ${line.variant.product_title}`}>Remove</button>
          </div>)}
          {pricingLoading && <p role="status" className="muted">Rechecking price, offers, and stock…</p>}
          {pricing && !pricingLoading && <>
            <dl className="customer-total-list">
              <div><dt>Subtotal</dt><dd>{money(pricing.gross_paise)}</dd></div>
              <div><dt>Discounts</dt><dd>−{money(pricing.discount_paise)}</dd></div>
              <div><dt>GST included</dt><dd>{money(pricing.tax_paise)}</dd></div>
              <div className="grand-total"><dt>Total</dt><dd>{money(pricing.payable_paise)}</dd></div>
            </dl>
            <section className="customer-offers" aria-label="Offers and savings">
              <div className="section-title">
                <h3>Offers</h3>
                {manualOfferIds === null
                  ? <button type="button" className="button text" onClick={() => setManualOfferIds(pricing.offers.map((offer) => offer.offer_id))}>Choose manually</button>
                  : <button type="button" className="button text" onClick={() => setManualOfferIds(null)}>Use automatic selection</button>}
              </div>
              {pricing.offer_evaluation.some((offer) => offer.status !== "rejected")
                ? <ul>{pricing.offer_evaluation.filter((offer) => offer.status !== "rejected").map((offer) => <li className="customer-offer" key={offer.offer_id}>
                  {manualOfferIds !== null
                    ? <input
                      type="checkbox"
                      aria-label={`Select ${offer.name}`}
                      checked={manualOfferIds.includes(offer.offer_id)}
                      onChange={() => toggleCustomerOffer(offer.offer_id)}
                    />
                    : <span className="offer-status applied" aria-label={offer.status}>
                      {offer.status === "applied" ? "✓" : "○"}
                    </span>}
                  <span className="customer-offer-details">
                    <strong>{offer.name}</strong>
                    <small>{manualOfferIds === null
                      ? offer.status === "applied" ? "Applied automatically." : "Eligible for selection."
                      : offer.status === "applied" ? "Currently selected." : "Eligible to select."}</small>
                  </span>
                  {offer.discount_paise > 0 && <strong className="customer-offer-savings">−{money(offer.discount_paise)}</strong>}
                </li>)}</ul>
                : <p className="muted small">No eligible offers for this selection.</p>}
              <p className="muted small">Offers and GST are calculated by Eluno for the selected store.</p>
            </section>
            <label className="field">Preferred payment method
              <select value={paymentMethodPreference} onChange={(event) => setPaymentMethodPreference(event.target.value as "upi" | "card")}>
                <option value="upi">UPI</option><option value="card">Card</option>
              </select>
            </label>
            <button type="button" className="button primary full-width" disabled={busy || pricingLoading || !cart.length} onClick={() => void placeOrder()}>{busy ? "Saving order…" : "Place order request"}</button>
            <p className="muted small">Your preference is saved for the store. No UPI or card payment is collected online; the request remains unpaid until a supported payment flow is available.</p>
          </>}
        </aside>
      </div>}
      {section === "shop" && cart.length > 0 && <div className="customer-checkout-bar" role="region" aria-label="Checkout summary">
        <div className="customer-checkout-bar-content">
          <div className="customer-checkout-total">
            <span>{cart.reduce((quantity, line) => quantity + line.quantity, 0)} items · Checkout total</span>
            {pricing && !pricingLoading
              ? <><strong>{money(pricing.payable_paise)}</strong>
                <small>{pricing.discount_paise > 0
                  ? `Offers save ${money(pricing.discount_paise)}`
                  : "Offers checked · GST included"}</small></>
              : <strong>{pricingLoading ? "Calculating total…" : "Total unavailable"}</strong>}
          </div>
          <button
            type="button"
            className="button primary"
            disabled={busy || pricingLoading || !pricing}
            onClick={() => void placeOrder()}
          >
            {busy ? "Submitting…" : pricing ? `Checkout · ${money(pricing.payable_paise)}` : "Checkout"}
          </button>
        </div>
      </div>}
      {section === "orders" && <section className="panel">
        <h1>My orders</h1>
        {!orders.length && <p className="muted">No online order requests are saved to this account yet.</p>}
        <div className="section-title">
          <span className="muted small">Order updates refresh automatically.</span>
          <button type="button" className="button secondary" onClick={() => void loadOrders(orderPage)}>Refresh orders</button>
        </div>
        <div className="customer-orders-list">{orders.map((order) => <article key={order.order_id} className="customer-order-card">
          <header><div><strong>{order.order_id}</strong><span>{order.created_at} · {order.store_code}</span></div><strong>{money(order.total_paise)}</strong></header>
          <p>{order.status_message}</p>
          <p className="small"><strong>Store:</strong> {order.store_confirmation_status === "confirmed" ? "Confirmed" : order.store_confirmation_status === "denied" ? "Declined" : "Awaiting confirmation"} · <strong>Payment:</strong> {order.store_confirmation_status === "confirmed" ? "Pending — contact the store to arrange payment; online payment is unavailable" : order.store_confirmation_status === "denied" ? "Not required — request declined" : "Pending store confirmation"}{order.payment_method_preference ? ` · Preferred ${order.payment_method_preference.toUpperCase()}` : ""} · <strong>Fulfilment:</strong> Not submitted</p>
          <p className="small"><strong>Delivery:</strong> {[order.delivery_address.line1, order.delivery_address.city, order.delivery_address.state, order.delivery_address.postal_code].filter(Boolean).join(", ")}</p>
          <ul>{order.items.map((item, index) => <li key={`${item.variant_id}-${index}`}>{item.title} × {item.quantity}{item.prescription_assistance_requested ? " — prescription assistance requested" : ""}</li>)}</ul>
          {!order.tracking_available && <p className="muted small">Tracking is not available until this order is confirmed and shipped.</p>}
        </article>)}</div>
        <div className="inline-actions">
          <button type="button" className="button secondary" disabled={orderPage <= 1} onClick={() => setOrderPage((page) => page - 1)}>Previous</button>
          <span>Page {orderPage}</span>
          <button type="button" className="button secondary" disabled={!hasMoreOrders} onClick={() => setOrderPage((page) => page + 1)}>Next</button>
        </div>
      </section>}
      {section === "profile" && <div className="customer-profile-grid">
        <form className="panel" onSubmit={(event) => void saveProfile(event)}>
          <h1>Profile & delivery</h1>
          <label className="field">Full name<input required maxLength={100} autoComplete="name" value={profile.name} onChange={(event) => setProfile((current) => ({ ...current, name: event.target.value }))} /></label>
          <label className="field">Mobile number<input required autoComplete="tel" value={profile.phone} onChange={(event) => setProfile((current) => ({ ...current, phone: event.target.value }))} /></label>
          <label className="field">Email (optional)<input type="email" autoComplete="email" value={profile.email} onChange={(event) => setProfile((current) => ({ ...current, email: event.target.value }))} /></label>
          <h2>Delivery address</h2>
          {([
            ["line1", "Address line 1"],
            ["line2", "Address line 2 (optional)"],
            ["city", "City"],
            ["state", "State"],
            ["postal_code", "6-digit postal code"],
          ] as const).map(([field, label]) => <label className="field" key={field}>{label}<input required={field !== "line2"} autoComplete={field === "postal_code" ? "postal-code" : undefined} value={profile.address[field]} onChange={(event) => setProfile((current) => ({ ...current, address: { ...current.address, [field]: event.target.value } }))} /></label>)}
          {profileError && <p className="notice error" role="alert">{profileError}</p>}
          {profileSaved && <p className="notice success" role="status">{profileSaved}</p>}
          <button type="submit" className="button primary" disabled={busy}>{busy ? "Saving…" : "Save profile"}</button>
        </form>
        <form className="panel" onSubmit={(event) => void changePassword(event)}>
          <h2>Change password</h2>
          <label className="field">Current password<input type="password" autoComplete="current-password" required value={passwordDraft.current} onChange={(event) => setPasswordDraft((current) => ({ ...current, current: event.target.value }))} /></label>
          <label className="field">New password<input type="password" autoComplete="new-password" minLength={12} required value={passwordDraft.next} onChange={(event) => setPasswordDraft((current) => ({ ...current, next: event.target.value }))} /></label>
          <button type="submit" className="button secondary" disabled={busy}>Update password</button>
          <p className="muted small">You will be signed out on all devices after changing your password.</p>
        </form>
      </div>}
    </main>
  </div>;
}

function LoginScreen({
  message,
  onLogin,
  onCustomerLogin,
}: {
  message: string;
  onLogin: (user: AuthenticatedUser) => void;
  onCustomerLogin: (customer: AuthenticatedCustomer) => void;
}) {
  const [mode, setMode] = useState<"staff" | "customer" | "register">("staff");
  const [username, setUsername] = useState("");
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(message);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError("");
    try {
      if (mode === "staff") {
        const result = await api.login(username, password);
        onLogin(result.user);
      } else if (mode === "customer") {
        const result = await api.customerLogin(normalizePhone(phone), password);
        onCustomerLogin(result.customer);
      } else {
        await api.customerRegister({
          name,
          phone: normalizePhone(phone),
          email: email.trim() || undefined,
          password,
        });
        setMode("customer");
        setPassword("");
        setError("Account created. Sign in with your mobile number and password.");
      }
    } catch (requestError) {
      setError(reportAuthError(requestError));
    } finally {
      setSubmitting(false);
    }
  };

  return <main className="auth-shell">
    <div className="auth-panel">
      <a className="brand login-brand" href="/" aria-label=" | Premium Eyeglasses, Sunglasses & Clip-On">
        <span className="brand-mark"><img className="brand-logo" src="/pos/images/eluno.png" alt="Eluno optical retail" /></span>
        <span className="brand-tagline">| Premium Eyeglasses, Sunglasses &amp; Clip-On</span>
      </a>
      <div className="customer-auth-tabs" aria-label="Account type">
        <button type="button" className={`button ${mode === "staff" ? "primary" : "secondary"}`} onClick={() => { setMode("staff"); setError(""); }}>Staff Login</button>
        <button type="button" className={`button ${mode === "customer" ? "primary" : "secondary"}`} onClick={() => { setMode("customer"); setError(""); }}>Customer Login</button>
        <button type="button" className={`button ${mode === "register" ? "primary" : "secondary"}`} onClick={() => { setMode("register"); setError(""); }}>Create Customer Account</button>
      </div>
      <form onSubmit={submit}>
        <h1>{mode === "staff" ? "Staff sign in" : mode === "customer" ? "Customer sign in" : "Create your account"}</h1>
        <p className="muted small">{mode === "staff"
          ? "Use your assigned Studio account. Access is determined by the server."
          : "Customer accounts are separate from staff access."}</p>
        {mode === "staff" ? <>
          <label className="field">Username<input autoComplete="username" required value={username} onChange={(event) => { setUsername(event.target.value); setError(""); }} /></label>
        </> : <>
          {mode === "register" && <label className="field">Full name<input autoComplete="name" required maxLength={100} value={name} onChange={(event) => { setName(event.target.value); setError(""); }} /></label>}
          <label className="field">Mobile number<input type="tel" autoComplete="tel" required value={phone} onChange={(event) => { setPhone(event.target.value); setError(""); }} /></label>
          {mode === "register" && <label className="field">Email (optional)<input type="email" autoComplete="email" value={email} onChange={(event) => { setEmail(event.target.value); setError(""); }} /></label>}
        </>}
        <label className="field">Password<input type="password" autoComplete={mode === "register" ? "new-password" : "current-password"} minLength={mode === "register" ? 12 : undefined} required value={password} onChange={(event) => { setPassword(event.target.value); setError(""); }} /></label>
        {mode === "register" && <p className="muted small">Use at least 12 characters with uppercase and lowercase letters, a number, and a symbol. Existing customer records require staff-assisted verification before account linking.</p>}
        {error && <p className={error.startsWith("Account created") ? "notice success" : "notice error"} role={error.startsWith("Account created") ? "status" : "alert"}>{error}</p>}
        <button className="button primary" type="submit" disabled={submitting}>{submitting ? "Please wait…" : mode === "register" ? "Create account" : "Sign in"}</button>
      </form>
    </div>
  </main>;
}

function PrescriptionEntryForm({
  customer,
  storeCode,
  businessDate,
  optometristId,
  onCancel,
  onSaved,
}: {
  customer: Customer;
  storeCode: string;
  businessDate: string;
  optometristId: string;
  onCancel: () => void;
  onSaved: (prescription: Prescription, warnings: string[]) => void;
}) {
  const [approvers, setApprovers] = useState<PrescriptionApprover[]>([]);
  const [approversLoading, setApproversLoading] = useState(true);
  const [optometristError, setOptometristError] = useState("");
  const [selectedOptometristId, setSelectedOptometristId] = useState(optometristId);
  const [prescribedOn, setPrescribedOn] = useState(businessDate);
  const [validUntil, setValidUntil] = useState(() => {
    const date = new Date(`${businessDate}T12:00:00`);
    date.setFullYear(date.getFullYear() + 1);
    return date.toISOString().slice(0, 10);
  });
  const [draft, setDraft] = useState({
    odSph: "", odCyl: "", odAxis: "", odAdd: "",
    osSph: "", osCyl: "", osAxis: "", osAdd: "",
  });
  const [confirmed, setConfirmed] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [requestKey, setRequestKey] = useState(makeKey);
  const selectedOptometrist = approvers.find((item) => item.staff_id === selectedOptometristId);

  useEffect(() => {
    let active = true;
    setApproversLoading(true);
    setOptometristError("");
    api.prescriptionApprovers(storeCode).then(({ approvers: available }) => {
      if (!active) return;
      setApprovers(available);
      setSelectedOptometristId((current) => {
        if (available.some((item) => item.staff_id === current)) return current;
        if (available.some((item) => item.staff_id === optometristId)) return optometristId;
        return available.length === 1 ? available[0].staff_id : "";
      });
    }).catch((requestError: unknown) => {
      if (active) {
        setApprovers([]);
        setSelectedOptometristId("");
        setOptometristError(reportAuthError(requestError));
      }
    }).finally(() => {
      if (active) setApproversLoading(false);
    });
    return () => { active = false; };
  }, [storeCode, optometristId]);

  const changeDraft = (field: keyof typeof draft, value: string) => {
    setDraft((current) => ({ ...current, [field]: value }));
    setRequestKey(makeKey());
  };
  const parseEye = (side: "od" | "os") => {
    const sphText = draft[`${side}Sph` as keyof typeof draft];
    const cylText = draft[`${side}Cyl` as keyof typeof draft];
    const axisText = draft[`${side}Axis` as keyof typeof draft];
    const addText = draft[`${side}Add` as keyof typeof draft];
    const sph = Number(sphText);
    const cyl = Number(cylText);
    const axis = axisText === "" ? null : Number(axisText);
    const add = addText === "" ? null : Number(addText);
    if (!sphText || !cylText || !Number.isFinite(sph) || !Number.isFinite(cyl) ||
      axis !== null && (!Number.isInteger(axis) || axis < 0 || axis > 180) ||
      add !== null && (!Number.isFinite(add) || add < 0 || add > 5)) {
      throw new Error(`${side.toUpperCase()} values are incomplete or outside the supported range.`);
    }
    return { sph, cyl, axis, add };
  };
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (loading) return;
    setLoading(true);
    setError("");
    try {
      if (!selectedOptometristId || !confirmed) throw new Error("Choose the Optometrist who verified this prescription and confirm the details.");
      const od = parseEye("od");
      const os = parseEye("os");
      const result = await api.createPrescription({
        phone: customer.phone,
        store_code: storeCode,
        optometrist_id: selectedOptometristId,
        date: prescribedOn,
        valid_until: validUntil,
        od,
        os,
        optometrist_confirmed: true,
        idempotency_key: requestKey,
      });
      onSaved(result.prescription, result.warnings);
      setRequestKey(makeKey());
    } catch (requestError) {
      setError(reportAuthError(requestError));
    } finally {
      setLoading(false);
    }
  };
  const eyeField = (side: "od" | "os", part: "Sph" | "Cyl" | "Axis" | "Add", label: string, required = false) => {
    const key = `${side}${part}` as keyof typeof draft;
    return <label className="field">{side.toUpperCase()} {label}<input
      type="number"
      step={part === "Axis" ? "1" : "0.25"}
      min={part === "Sph" ? "-30" : part === "Cyl" ? "-10" : "0"}
      max={part === "Sph" ? "30" : part === "Cyl" ? "0" : part === "Axis" ? "180" : "5"}
      required={required}
      value={draft[key]}
      onChange={(event) => changeDraft(key, event.target.value)}
    /></label>;
  };
  return <form className="prescription-form panel" onSubmit={submit}>
    <h3>Add Prescription</h3>
    <p className="small muted">Enter the customer's prescription and select the assigned Optometrist who verified it. The saved record is linked to {customerName(customer)}. Non-5° axis values are retained and flagged for review.</p>
    {error && <p className="notice error" role="alert">{error}</p>}
    {approversLoading && <p className="small muted" role="status">Loading Optometrists assigned to {storeCode}…</p>}
    {optometristError && <p className="notice error" role="alert">{optometristError}</p>}
    {!approversLoading && !optometristError && !approvers.length && <p className="notice warning" role="status">No assigned Optometrists are available for this store. The prescription cannot be saved until the directory is configured.</p>}
    <label className="field">Verified by Optometrist
      <select required value={selectedOptometristId} onChange={(event) => {
        setSelectedOptometristId(event.target.value);
        setRequestKey(makeKey());
      }} disabled={approversLoading || !approvers.length}>
        <option value="">Choose the verifying Optometrist</option>
        {approvers.map((item) => <option key={item.staff_id} value={item.staff_id}>{item.display_name} · {item.staff_id}</option>)}
      </select>
    </label>
    {selectedOptometrist && <p className="small muted">Verified by {selectedOptometrist.display_name} · {selectedOptometrist.staff_id} at {storeCode}</p>}
    <div className="prescription-grid">
      {eyeField("od", "Sph", "sphere", true)}
      {eyeField("od", "Cyl", "cylinder", true)}
      {eyeField("od", "Axis", "axis")}
      {eyeField("od", "Add", "ADD")}
      {eyeField("os", "Sph", "sphere", true)}
      {eyeField("os", "Cyl", "cylinder", true)}
      {eyeField("os", "Axis", "axis")}
      {eyeField("os", "Add", "ADD")}
    </div>
    <div className="prescription-grid">
      <label className="field">Prescription date<input type="date" required value={prescribedOn} onChange={(event) => { setPrescribedOn(event.target.value); setRequestKey(makeKey()); }} /></label>
      <label className="field">Valid through<input type="date" required min={prescribedOn} value={validUntil} onChange={(event) => { setValidUntil(event.target.value); setRequestKey(makeKey()); }} /></label>
    </div>
    <label className="review-control"><input type="checkbox" required checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /> I confirm these prescription values were verified by the selected Optometrist.</label>
    <div className="dialog-actions">
      <button className="button secondary" type="button" onClick={onCancel} disabled={loading}>Cancel</button>
      <button className="button primary" type="submit" disabled={loading || approversLoading || !selectedOptometristId || !!optometristError}>{loading ? "Saving…" : "Save Prescription"}</button>
    </div>
  </form>;
}

function CustomerSearch({
  allowCustomerId,
  onSelect,
}: {
  allowCustomerId: boolean;
  onSelect: (customer: CustomerSearchResult) => void;
}) {
  const [query, setQuery] = useState("");
  const [customers, setCustomers] = useState<CustomerSearchResult[]>([]);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const search = async (nextPage = 1) => {
    if (loading) return;
    setLoading(true);
    setError("");
    try {
      const result = await api.searchCustomers(query.trim(), nextPage);
      setCustomers((current) => nextPage === 1 ? result.customers : [...current, ...result.customers]);
      setPage(result.page);
      setHasMore(result.has_more);
    } catch (requestError) {
      setError(reportAuthError(requestError));
      if (nextPage === 1) setCustomers([]);
    } finally {
      setLoading(false);
    }
  };

  return <section className="customer-search" aria-label="Customer search">
    <form className="inline-form" onSubmit={(event) => { event.preventDefault(); void search(); }}>
      <label className="field grow">Search by name, mobile, email{allowCustomerId ? ", or customer ID" : ""}<input
        autoComplete="off"
        value={query}
        onChange={(event) => { setQuery(event.target.value); setError(""); }}
        placeholder={allowCustomerId ? "Name, phone, email or customer ID" : "Name, phone or email"}
      /></label>
      <button className="button secondary" type="submit" disabled={loading || query.trim().length < 2}>
        {loading ? "Searching…" : "Search customers"}
      </button>
    </form>
    {error && <p className="notice error" role="alert">{error}</p>}
    {customers.length > 0 && <div className="customer-search-results" role="list">
      {customers.map((item) => <article className="customer-search-result" key={item.id} role="listitem">
        <div><strong>{item.name}</strong><small>{item.phone || "No phone"}{item.email ? ` · ${item.email}` : ""} · ID {item.id}</small></div>
        <span>
          {money(item.total_spent_paise)} · {item.order_count} orders
          {` · refunds ${money(item.total_refunded_paise)}`}
          {item.most_recent_purchase ? ` · last purchase ${new Date(item.most_recent_purchase).toLocaleDateString("en-IN")}` : ""}
        </span>
        <button className="button text" type="button" onClick={() => onSelect(item)}>Select</button>
      </article>)}
      {hasMore && <button className="button secondary" type="button" onClick={() => void search(page + 1)} disabled={loading}>
        {loading ? "Loading…" : "Load more customers"}
      </button>}
    </div>}
    {!loading && query.trim().length >= 2 && customers.length === 0 && !error && <p className="empty">No matching customer records were found.</p>}
  </section>;
}

function OptometristAssistanceInbox({
  onOpenCustomer,
}: {
  onOpenCustomer: (phone: string) => void;
}) {
  const [orders, setOrders] = useState<OptometristAssistanceRequest[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await api.optometristAssistanceOrders();
      setOrders(result.orders);
    } catch (requestError) {
      setError(reportAuthError(requestError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 30_000);
    window.addEventListener("focus", load);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", load);
    };
  }, [load]);

  return <section className="assistance-inbox" aria-labelledby="assistance-inbox-title">
    <div className="section-title">
      <div><span className="eyebrow">ASSIGNED STORE</span><h2 id="assistance-inbox-title">Prescription assistance requests</h2></div>
      <button type="button" className="button secondary" onClick={() => void load()} disabled={loading}>
        {loading ? "Refreshing…" : "Refresh"}
      </button>
    </div>
    <p className="muted small">Store-confirmed customer requests that asked for Optometrist assistance. Open a customer to review the prescription workflow; prescription records identify the Optometrist who verified them.</p>
    {error && <p className="notice error" role="alert">{error}</p>}
    {loading && <p className="muted" role="status">Loading assistance requests…</p>}
    {!loading && !error && !orders.length && <p className="empty">No confirmed requests are awaiting prescription assistance.</p>}
    <div className="assistance-order-list">{orders.map((order) => <article className="assistance-order-card" key={order.order_id}>
      <header><div><strong>{order.order_id}</strong><span>{order.store_code} · {new Date(order.created_at).toLocaleString("en-IN")}</span></div></header>
      <p><strong>{order.customer_name || "Customer name unavailable"}</strong> · {order.customer_phone}</p>
      <ul>{order.items.map((item, index) => <li key={`${order.order_id}-${index}`}>{item.title} × {item.quantity}</li>)}</ul>
      <button type="button" className="button primary" onClick={() => onOpenCustomer(order.customer_phone)}>Open prescription lookup</button>
    </article>)}</div>
  </section>;
}

function Workspace({
  user,
  onLogout,
}: {
  user: AuthenticatedUser;
  onLogout: () => void;
}) {
  const [view, setView] = useState<View>(user.role === "cashier" ? "checkout" : "studio");
  const [locations, setLocations] = useState<Location[]>([]);
  const [locationId, setLocationId] = useState("");
  const operator = user.operator_id || user.staff_id;
  const [businessDate, setBusinessDate] = useState("");
  const [reportDate, setReportDate] = useState("");
  const reportDatePinned = useRef(false);
  const [notice, setNotice] = useState<{ text: string; tone: "error" | "success" | "warning" }>();
  const [busy, setBusy] = useState<string | null>(null);
  const [customer, setCustomer] = useState<Customer | null>(null);
  const [prescription, setPrescription] = useState<Prescription | null>(null);
  const [prescriptionEntryOpen, setPrescriptionEntryOpen] = useState(false);
  const [phone, setPhone] = useState("");
  const [customerLookupMiss, setCustomerLookupMiss] = useState(false);
  const [customerFormOpen, setCustomerFormOpen] = useState(false);
  const [newCustomerName, setNewCustomerName] = useState("");
  const [newCustomerPhone, setNewCustomerPhone] = useState("");
  const [customerProfile, setCustomerProfile] = useState<CustomerProfile | null>(null);
  const [customerProfileLoading, setCustomerProfileLoading] = useState(false);
  const [customerEditOpen, setCustomerEditOpen] = useState(false);
  const [customerEditName, setCustomerEditName] = useState("");
  const [customerEditEmail, setCustomerEditEmail] = useState("");
  const [anonymizeReason, setAnonymizeReason] = useState("");
  const [walkIn, setWalkIn] = useState(false);
  const [lineType, setLineType] = useState<"frame" | "lens">("frame");
  const [variants, setVariants] = useState<CatalogVariant[]>([]);
  const [search, setSearch] = useState("");
  const [cart, setCart] = useState<CartLine[]>([]);
  const [manualOfferIds, setManualOfferIds] = useState<string[] | null>(null);
  const [pricingRefresh, setPricingRefresh] = useState(0);
  const [pricing, setPricing] = useState<Pricing | null>(null);
  const [offerEvaluation, setOfferEvaluation] = useState<Pricing["offer_evaluation"]>([]);
  const [pricingLoading, setPricingLoading] = useState(false);
  const [pricingError, setPricingError] = useState<string | null>(null);
  const [lensWarning, setLensWarning] = useState<LensValidation | null>(null);
  const [pendingLens, setPendingLens] = useState<CatalogVariant | null>(null);
  const [reviewAck, setReviewAck] = useState(false);
  const [reviewedRx, setReviewedRx] = useState<string[]>([]);
  const [checkoutKey, setCheckoutKey] = useState(makeKey);
  const [sale, setSale] = useState<SaleResult | null>(null);
  const [invoiceError, setInvoiceError] = useState("");
  const [paymentReview, setPaymentReview] = useState(false);
  const [paymentMethod, setPaymentMethod] = useState("upi");
  const [channel, setChannel] = useState("walk_in");
  const [refundIdentifier, setRefundIdentifier] = useState("");
  const [refundPreview, setRefundPreview] = useState<RefundPreview | null>(null);
  const [refundAck, setRefundAck] = useState(false);
  const [refundMode, setRefundMode] = useState<"full" | "partial">("full");
  const [refundReason, setRefundReason] = useState("");
  const [refundOtherReason, setRefundOtherReason] = useState("");
  const [refundQuantities, setRefundQuantities] = useState<Record<string, number>>({});
  const [returnItemIds, setReturnItemIds] = useState<string[]>([]);
  const [refundTill, setRefundTill] = useState("");
  const [refundKey, setRefundKey] = useState(makeKey);
  const [refundResult, setRefundResult] = useState<RefundResult | null>(null);
  const [storeOrders, setStoreOrders] = useState<StoreOrderQueueItem[]>([]);
  const [storeOrdersLoading, setStoreOrdersLoading] = useState(false);
  const [storeOrdersError, setStoreOrdersError] = useState("");
  const [storeOrderReasons, setStoreOrderReasons] = useState<Record<string, string>>({});
  const [storeOrderBusy, setStoreOrderBusy] = useState<string | null>(null);
  const [report, setReport] = useState<DayEndReport | null>(null);
  const [reportScope, setReportScope] = useState<"store" | "overall">("store");
  const [adminFrames, setAdminFrames] = useState<AdminFrame[]>([]);
  const [adminFramesLoading, setAdminFramesLoading] = useState(false);
  const adminFramesRequest = useRef(0);
  const [staffAccounts, setStaffAccounts] = useState<StaffAccount[]>([]);
  const [staffLoading, setStaffLoading] = useState(false);
  const [staffError, setStaffError] = useState("");
  const [staffUsername, setStaffUsername] = useState("");
  const [staffDisplayName, setStaffDisplayName] = useState("");
  const [staffRole, setStaffRole] = useState<"admin" | "cashier" | "optometrist">("cashier");
  const [staffStoreCode, setStaffStoreCode] = useState("");
  const [staffExternalId, setStaffExternalId] = useState("");
  const [optometristDirectory, setOptometristDirectory] = useState<PrescriptionApprover[]>([]);
  const [optometristDirectoryLoading, setOptometristDirectoryLoading] = useState(false);
  const [optometristDirectoryError, setOptometristDirectoryError] = useState("");
  const [staffStoreDrafts, setStaffStoreDrafts] = useState<Record<string, string>>({});
  const [catalogProducts, setCatalogProducts] = useState<AdminCatalogProduct[]>([]);
  const [catalogSearch, setCatalogSearch] = useState("");
  const [inventoryType, setInventoryType] = useState<"frame" | "lens">("frame");
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState("");
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [editingProduct, setEditingProduct] = useState<AdminCatalogProduct | null>(null);
  const [archiveConfirmation, setArchiveConfirmation] = useState(false);
  const [tillSessions, setTillSessions] = useState<TillSession[]>([]);
  const [tillLoading, setTillLoading] = useState(false);
  const [tillOpenFloat, setTillOpenFloat] = useState("");
  const [tillCounted, setTillCounted] = useState("");
  const [tillReviewNote, setTillReviewNote] = useState("");
  const [tillMovementType, setTillMovementType] = useState<"paid_in" | "paid_out">("paid_in");
  const [tillMovementAmount, setTillMovementAmount] = useState("");
  const [tillMovementReason, setTillMovementReason] = useState("");
  const [tillAdminReview, setTillAdminReview] = useState<Record<string, string>>({});
  const [staffPassword, setStaffPassword] = useState("");
  const [resetStaffId, setResetStaffId] = useState("");
  const [resetPassword, setResetPassword] = useState("");
  const [productType, setProductType] = useState<"frame" | "lens">("frame");
  const [productTitle, setProductTitle] = useState("");
  const [productSku, setProductSku] = useState("");
  const [variantTitle, setVariantTitle] = useState("");
  const [unitPrice, setUnitPrice] = useState("");
  const [frameCategory, setFrameCategory] = useState<"optical" | "sunglasses">("optical");
  const [frameMaterial, setFrameMaterial] = useState("");
  const [frameSize, setFrameSize] = useState("");
  const [rxAble, setRxAble] = useState(true);
  const [lensType, setLensType] = useState("single_vision");
  const [lensIndex, setLensIndex] = useState("1.50");
  const [lensCoating, setLensCoating] = useState("standard");
  const [receiptVariantId, setReceiptVariantId] = useState("");
  const [receiptQuantity, setReceiptQuantity] = useState("1");
  const [receiptReference, setReceiptReference] = useState("");
  const [adminLoadingError, setAdminLoadingError] = useState("");
  const pricingRequest = useRef(0);

  const location = locations.find((item) => String(item.id) === locationId);
  const filteredVariants = useMemo(
    () =>
      variants.filter((item) =>
        [item.product_title, item.variant_title, item.variant_id, item.lens_type, item.coating]
          .filter(Boolean)
          .join(" ")
          .toLowerCase()
          .includes(search.toLowerCase()),
      ),
    [variants, search],
  );
  const resetSale = (clearPricing = true) => {
    setCheckoutKey(makeKey());
    setSale(null);
    setInvoiceError("");
    if (clearPricing) setPricing(null);
  };
  const reportError = (error: unknown) =>
    error instanceof Error ? error.message : "The request failed unexpectedly.";
  const run = async (name: string, operation: () => Promise<void>) => {
    if (busy) return;
    setBusy(name);
    setNotice(undefined);
    try {
      await operation();
    } catch (error) {
      if (name === "checkout" && error instanceof ApiError && error.status === 0) {
        setSale({ status: "UNKNOWN", message: "The server did not confirm the result. Do not start a new charge; reconcile using the same checkout key." });
      }
      setNotice({ text: reportError(error), tone: error instanceof ApiError && error.status === 0 ? "warning" : "error" });
    } finally {
      setBusy(null);
    }
  };

  const loadCatalog = useCallback(async (type: "frame" | "lens") => {
    setLineType(type);
    setVariants([]);
    if (!locationId) return;
    const result = await api.catalog(type);
    setVariants(result.variants);
  }, [locationId]);

  const loadAdminFrames = useCallback(async () => {
    const requestId = ++adminFramesRequest.current;
    if (!locationId) {
      setAdminFrames([]);
      setReceiptVariantId("");
      setAdminLoadingError("");
      setAdminFramesLoading(false);
      return;
    }
    setAdminFramesLoading(true);
    setAdminFrames([]);
    setAdminLoadingError("");
    try {
      const result = await api.adminFrames(locationId);
      if (requestId !== adminFramesRequest.current) return;
      setAdminFrames(result.variants);
      setReceiptVariantId((current) =>
        result.variants.some((item) => String(item.variant_id) === current)
          ? current
          : String(result.variants[0]?.variant_id ?? ""),
      );
    } catch (error) {
      if (requestId === adminFramesRequest.current) setAdminLoadingError(reportError(error));
    } finally {
      if (requestId === adminFramesRequest.current) setAdminFramesLoading(false);
    }
  }, [locationId]);

  const loadCatalogProducts = useCallback(async () => {
    setCatalogLoading(true);
    setCatalogProducts([]);
    setCatalogError("");
    try {
      const result = await api.adminProducts(undefined, catalogSearch.trim());
      setCatalogProducts(result.products);
    } catch (error) {
      setCatalogError(reportError(error));
    } finally {
      setCatalogLoading(false);
    }
  }, [catalogSearch]);

  const loadTillSessions = useCallback(async () => {
    setTillLoading(true);
    try {
      const storeCode = locations.find((item) => String(item.id) === locationId)?.store_code;
      const result = user.role === "admin"
        ? await api.adminTillSessions(storeCode)
        : await api.openTills(storeCode || user.store_code || "");
      setTillSessions("sessions" in result ? result.sessions : result.tills);
    } catch (error) {
      setNotice({ text: reportError(error), tone: "error" });
    } finally {
      setTillLoading(false);
    }
  }, [locationId, locations, user.role, user.store_code]);

  const loadStaff = useCallback(async () => {
    setStaffLoading(true);
    setStaffError("");
    try {
      const result = await api.staff();
      setStaffAccounts(result.staff);
    } catch (error) {
      setStaffError(reportError(error));
    } finally {
      setStaffLoading(false);
    }
  }, []);

  const loadStoreOrders = useCallback(async () => {
    setStoreOrdersLoading(true);
    setStoreOrdersError("");
    try {
      const result = await api.storePendingOrders();
      setStoreOrders(result.orders);
    } catch (error) {
      setStoreOrdersError(reportError(error));
    } finally {
      setStoreOrdersLoading(false);
    }
  }, []);

  const decideStoreOrder = async (
    order: StoreOrderQueueItem,
    decision: "confirm" | "deny",
  ) => {
    const reason = storeOrderReasons[order.order_id]?.trim() || "";
    if (decision === "deny" && !reason) {
      setStoreOrdersError("Enter a reason before denying this order.");
      return;
    }
    setStoreOrderBusy(order.order_id);
    setStoreOrdersError("");
    try {
      const result = await api.decideStoreOrder(order.order_id, decision, reason || undefined);
      setNotice({
        text: decision === "confirm"
          ? `${result.order.order_id} confirmed for store review. Payment is still pending.`
          : `${result.order.order_id} denied. No payment was collected.`,
        tone: "success",
      });
      setStoreOrderReasons((current) => {
        const next = { ...current };
        delete next[order.order_id];
        return next;
      });
      await loadStoreOrders();
    } catch (error) {
      setStoreOrdersError(`${reportError(error)} Refresh the queue to check whether the decision was saved.`);
    } finally {
      setStoreOrderBusy(null);
    }
  };

  useEffect(() => {
    let active = true;
    if (staffRole !== "optometrist" || !staffStoreCode) {
      setOptometristDirectory([]);
      setStaffExternalId("");
      setOptometristDirectoryLoading(false);
      setOptometristDirectoryError("");
      return () => { active = false; };
    }
    setOptometristDirectoryLoading(true);
    setOptometristDirectoryError("");
    api.prescriptionApprovers(staffStoreCode).then(({ approvers }) => {
      if (!active) return;
      setOptometristDirectory(approvers);
      setStaffExternalId((current) => {
        if (approvers.length === 1) return approvers[0].staff_id;
        return approvers.some((item) => item.staff_id === current) ? current : "";
      });
      setOptometristDirectoryLoading(false);
    }).catch((error: unknown) => {
      if (active) {
        setOptometristDirectory([]);
        setStaffExternalId("");
        setOptometristDirectoryLoading(false);
        setOptometristDirectoryError(reportError(error));
      }
    });
    return () => { active = false; };
  }, [staffRole, staffStoreCode]);

  useEffect(() => {
    let active = true;
    api.locations().then(({ locations: result }) => {
      if (!active) return;
      setLocations(result);
      const savedId = window.localStorage.getItem("eluno:selected-store");
      const assigned = user.store_code
        ? result.find((item) => item.store_code === user.store_code)
        : undefined;
      const saved = user.role === "admin"
        ? result.find((item) => String(item.id) === savedId)
        : undefined;
      const selected = assigned || saved || (user.role === "admin" ? result[0] : undefined);
      setLocationId(selected ? String(selected.id) : "");
    }).catch((error: unknown) => {
      if (active) setNotice({ text: reportError(error), tone: "error" });
    });
    return () => { active = false; };
  }, [user.role, user.store_code]);

  useEffect(() => {
    let active = true;
    const refresh = () => {
      api.businessDate().then(({ business_date: current }) => {
        if (!active) return;
        setBusinessDate(current);
        setReportDate((previous) => {
          if (!previous || !reportDatePinned.current) return current;
          return previous;
        });
      }).catch((error: unknown) => {
        if (active) setNotice({ text: reportError(error), tone: "error" });
      });
    };
    refresh();
    const timer = window.setInterval(refresh, 30_000);
    window.addEventListener("focus", refresh);
    return () => {
      active = false;
      window.clearInterval(timer);
      window.removeEventListener("focus", refresh);
    };
  }, []);

  useEffect(() => {
    if (!locationId) return;
    let active = true;
    api.catalog(lineType).then((catalog) => {
      if (active) setVariants(catalog.variants);
    }).catch((error: unknown) => {
      if (active) setNotice({ text: reportError(error), tone: "error" });
    });
    return () => { active = false; };
  }, [locationId, lineType, businessDate]);

  useEffect(() => {
    if (view !== "admin" || user.role !== "admin") return;
    const refreshInventory = () => void loadAdminFrames();
    refreshInventory();
    const timer = window.setInterval(refreshInventory, 10_000);
    window.addEventListener("focus", refreshInventory);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", refreshInventory);
    };
  }, [view, user.role, loadAdminFrames]);

  useEffect(() => {
    if (view === "admin" && user.role === "admin") void loadCatalogProducts();
  }, [view, user.role, loadCatalogProducts]);

  useEffect(() => {
    if (view === "staff" && user.role === "admin") void loadStaff();
  }, [view, user.role, loadStaff]);

  useEffect(() => {
    if (view === "till") void loadTillSessions();
  }, [view, loadTillSessions]);

  useEffect(() => {
    if (user.role !== "cashier") return;
    void loadStoreOrders();
    const timer = window.setInterval(() => void loadStoreOrders(), 30_000);
    window.addEventListener("focus", loadStoreOrders);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", loadStoreOrders);
    };
  }, [user.role, loadStoreOrders]);

  const checkoutPayload = (forPricing = false): CheckoutRequest => ({
    idempotency_key: checkoutKey,
    cart: {
      store_code: location?.store_code || "",
      location_id: locationId,
      lines: cart.map((line) => ({
        variant_id: line.variant.variant_id,
        quantity: line.quantity,
        ...(line.pairRef ? { pair_ref: line.pairRef } : {}),
        ...(line.variant.line_type === "lens" && prescription ? { prescription_id: prescription.rx_id } : {}),
      })),
    },
    business_date: businessDate,
    payment_method: paymentMethod,
    channel,
    staff_id: operator.trim(),
    customer_id: customer?.id ?? null,
    customer_phone: customer?.phone ?? null,
    ...(manualOfferIds !== null
      ? { offer_ids: manualOfferIds }
      : forPricing
        ? {}
        : { offer_ids: pricing?.offers.map((offer) => offer.offer_id) || [] }),
    reviewed_prescription_ids: reviewedRx,
  });

  useEffect(() => {
    const requestId = ++pricingRequest.current;
    if (!cart.length || !location) {
      setPricing(null);
      setOfferEvaluation([]);
      setPricingLoading(false);
      setPricingError(null);
      return;
    }
    setPricingLoading(true);
    setPricingError(null);
    const timer = window.setTimeout(() => {
      api.price(checkoutPayload(true)).then((result) => {
        if (requestId === pricingRequest.current) {
          setPricing(result);
          setOfferEvaluation(result.offer_evaluation);
          setPricingLoading(false);
        }
      }).catch((error: unknown) => {
        if (requestId !== pricingRequest.current) return;
        setPricing(null);
        setPricingLoading(false);
        setPricingError(reportError(error));
        setNotice({ text: reportError(error), tone: "error" });
      });
    }, 180);
    return () => {
      window.clearTimeout(timer);
      if (requestId === pricingRequest.current) pricingRequest.current += 1;
    };
  }, [cart, location, locationId, businessDate, manualOfferIds, customer, prescription, reviewedRx, checkoutKey, pricingRefresh]);

  const lookupCustomer = (phoneValue = phone) => run("customer", async () => {
    if (!businessDate) throw new Error("Wait for the current business date to load.");
    setCustomerLookupMiss(false);
    setCustomerFormOpen(false);
    const normalized = normalizePhone(phoneValue);
    try {
      const result: CustomerLookup = await api.customer(normalized, businessDate);
      setCustomerLookupMiss(false);
      setCustomer(result.customer || null);
      setPrescription(result.prescription || null);
      setCustomerProfile(null);
      setCustomerEditOpen(false);
      setPrescriptionEntryOpen(false);
      setWalkIn(false);
      setReviewedRx([]);
      resetSale();
      setNotice({
        text: result.message || (result.customer ? "Customer selected." : "No customer matched that number."),
        tone: result.status === "ready" ? "success" : "warning",
      });
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        setCustomerLookupMiss(true);
        setNewCustomerPhone(normalized);
      }
      throw error;
    }
  });

  const selectSearchedCustomer = (result: CustomerSearchResult) => run("customer-select", async () => {
    if (user.role === "admin") {
      const profile = await api.customerProfile(result.id);
      setCustomer({
        ...profile.customer,
        phone: profile.customer.phone || "",
      });
      setCustomerProfile(profile);
      setPrescription(null);
      setPhone(profile.customer.phone || "");
      setCustomerLookupMiss(false);
      setCustomerFormOpen(false);
      setPrescriptionEntryOpen(false);
      setWalkIn(false);
      resetSale();
      setNotice({ text: `${customerName(profile.customer)} selected.`, tone: "success" });
      return;
    }
    if (!businessDate) throw new Error("Wait for the current business date to load.");
    if (!result.phone) {
      throw new Error("This customer record has no mobile number and cannot be selected for a sale.");
    }
    const lookup = await api.customer(result.phone, businessDate);
    if (!lookup.customer || String(lookup.customer.id) !== result.id) {
      throw new Error("The customer record changed during lookup. Search again before selecting.");
    }
    setPhone(result.phone);
    setCustomer(lookup.customer);
    setPrescription(lookup.prescription || null);
    setCustomerProfile(null);
    setCustomerLookupMiss(false);
    setCustomerFormOpen(false);
    setPrescriptionEntryOpen(false);
    setWalkIn(false);
    setReviewedRx([]);
    resetSale();
    setNotice({
      text: lookup.message || `${customerName(lookup.customer)} selected.`,
      tone: lookup.prescription ? "success" : "warning",
    });
  });

  const createCustomer = () => run("customer-create", async () => {
    const name = newCustomerName.trim();
    if (!name || name.length > 100 || !/\p{L}/u.test(name)) {
      throw new Error("Enter a customer name with at least one letter (maximum 100 characters).");
    }
    const normalized = normalizePhone(newCustomerPhone);
    const result = await api.registerCustomer({ name, phone: normalized });
    const selected: Customer = {
      ...result.customer,
      tags: result.customer.tags || [],
    };
    setCustomer(selected);
    setCustomerProfile(null);
    setPrescription(null);
    setPhone(selected.phone);
    setCustomerLookupMiss(false);
    setCustomerFormOpen(false);
    setWalkIn(false);
    setReviewedRx([]);
    setPrescriptionEntryOpen(false);
    resetSale();
    try {
      const lookup = await api.customer(selected.phone, businessDate);
      setPrescription(lookup.prescription || null);
    } catch (error) {
      setNotice({
        text: `Customer ${customerName(selected)} was saved and selected, but prescription lookup failed: ${reportError(error)}`,
        tone: "warning",
      });
      return;
    }
    setNotice({
      text: result.created
        ? `Customer ${customerName(selected)} was saved and selected.`
        : `This customer already existed and has been selected.`,
      tone: result.created ? "success" : "warning",
    });
  });

  const loadCustomerProfile = (page = 1) => run("customer-profile", async () => {
    if (!customer) throw new Error("Find a customer before opening their profile.");
    setCustomerProfileLoading(true);
    try {
      const result = await api.customerProfile(customer.id, page);
      setCustomerProfile((current) => page > 1 && current
        ? { ...result, purchases: [...current.purchases, ...result.purchases] }
        : result);
      if (page === 1) {
        setCustomerEditName([result.customer.first_name, result.customer.last_name].filter(Boolean).join(" "));
        setCustomerEditEmail(result.customer.email || "");
      }
      setNotice({ text: `Loaded ${customerName(result.customer)} and ${result.purchase_count} saved purchases.`, tone: "success" });
    } finally {
      setCustomerProfileLoading(false);
    }
  });

  const saveCustomerProfile = () => run("customer-update", async () => {
    if (!customerProfile) throw new Error("Open a customer profile before editing.");
    const parts = customerEditName.trim().split(/\s+/);
    if (!parts[0] || customerEditName.trim().length > 100) throw new Error("Enter a customer name of at most 100 characters.");
    const result = await api.updateCustomer(customerProfile.customer.id, {
      first_name: parts[0],
      last_name: parts.slice(1).join(" "),
      email: customerEditEmail.trim() || null,
    });
    const updated = { ...customerProfile, customer: result.customer };
    setCustomerProfile(updated);
    setCustomer(result.customer);
    setCustomerEditOpen(false);
    setNotice({ text: "Customer profile updated.", tone: "success" });
  });

  const anonymizeCustomer = () => run("customer-anonymize", async () => {
    if (!customerProfile) throw new Error("Open a customer profile before starting the privacy workflow.");
    const reason = anonymizeReason.trim();
    if (!reason) throw new Error("Enter the reason for anonymizing this customer.");
    if (!window.confirm("This removes customer contact details from Shopify but retains invoices, orders, prescriptions, and audit history. Continue?")) return;
    const result = await api.anonymizeCustomer(customerProfile.customer.id, reason);
    setCustomerProfile((current) => current ? { ...current, customer: result.customer } : current);
    setCustomer(result.customer);
    setAnonymizeReason("");
    setNotice({ text: result.message, tone: "success" });
  });

  const prescriptionSaved = (saved: Prescription, warnings: string[]) => {
    setPrescription(saved);
    setPrescriptionEntryOpen(false);
    setReviewedRx([]);
    resetSale();
    if (customerProfile) {
      setCustomerProfile({ ...customerProfile, prescriptions: [saved, ...customerProfile.prescriptions] });
    }
    setNotice({
      text: warnings.length
        ? `Prescription ${saved.rx_id} saved. ${warnings.join(" ")}`
        : `Prescription ${saved.rx_id} saved and linked to ${customer ? customerName(customer) : "the customer"}.`,
      tone: warnings.length ? "warning" : "success",
    });
  };

  const selectWalkIn = () => {
    setCustomer(null);
    setPrescription(null);
    setCustomerProfile(null);
    setPrescriptionEntryOpen(false);
    setPhone("");
    setCustomerLookupMiss(false);
    setCustomerFormOpen(false);
    setWalkIn(true);
    setReviewedRx([]);
    resetSale();
    setNotice({ text: "Walk-in selected. Lens items still require a valid prescription.", tone: "success" });
  };

  const addFrameOrValidatedLens = (variant: CatalogVariant, pairRef?: string) => {
    const ref = pairRef || (variant.line_type === "frame" ? makeKey() : undefined);
    setCart((items) => [...items, { variant, quantity: 1, pairRef: ref }]);
    resetSale();
  };

  const addVariant = (variant: CatalogVariant) => run("lens", async () => {
    if (variant.line_type === "frame") {
      addFrameOrValidatedLens(variant);
      return;
    }
    if (!customer || !prescription) throw new Error("Select a customer with a current prescription before adding lenses.");
    const result = await api.validateLens(
      { lens_type: variant.lens_type || "", index: variant.index || "", coating: variant.coating || "" },
      prescription,
    );
    if (!result.valid) throw new Error(result.errors.join(" "));
    if (result.warnings.length && !reviewedRx.includes(prescription.rx_id)) {
      setPendingLens(variant);
      setLensWarning(result);
      setReviewAck(false);
      return;
    }
    const paired = [...cart].reverse().find((line) => line.variant.line_type === "frame" && line.pairRef);
    addFrameOrValidatedLens(variant, paired?.pairRef);
    setLensWarning(null);
  });

  const confirmPrescriptionReview = () => {
    if (!prescription || !reviewAck) return;
    setReviewedRx((items) => [...new Set([...items, prescription.rx_id])]);
    if (pendingLens) {
      const paired = [...cart].reverse().find((line) => line.variant.line_type === "frame" && line.pairRef);
      addFrameOrValidatedLens(pendingLens, paired?.pairRef);
    }
    setPendingLens(null);
    setLensWarning(null);
    setNotice({ text: "Optometrist review acknowledged for this prescription. Checkout will include that review state.", tone: "success" });
  };

  const changeQuantity = (index: number, quantity: number) => {
    if (!Number.isInteger(quantity) || quantity < 1) return;
    setCart((items) => items.map((item, current) => current === index ? { ...item, quantity } : item));
    resetSale();
  };
  const removeLine = (index: number) => {
    setCart((items) => items.filter((_, current) => current !== index));
    resetSale();
  };
  const toggleOffer = (offerId: string) => {
    setManualOfferIds((items) => {
      const current = items ?? offerEvaluation.filter((offer) => offer.status === "applied").map((offer) => offer.offer_id);
      return current.includes(offerId)
        ? current.filter((id) => id !== offerId)
        : [...current, offerId];
    });
    resetSale();
  };

  const openPaymentReview = () => {
    if (!cart.length || !location) throw new Error("Select a store and add items before checkout.");
    if (lensWarning?.warnings.length && !reviewedRx.includes(prescription?.rx_id || "")) {
      throw new Error("Optometrist review acknowledgement is required before lens checkout.");
    }
    if (!pricing) {
      setNotice({ text: "Wait for server pricing before payment confirmation.", tone: "warning" });
      return;
    }
    setPaymentReview(true);
  };
  const checkout = () => run("checkout", async () => {
    setPaymentReview(false);
    const result = await api.checkout(checkoutPayload());
    setSale(result);
    setInvoiceError("");
    if (result.status === "COMPLETED" && result.sale_id && result.invoice_no) {
      setNotice({ text: `Checkout completed. Invoice ${result.invoice_no}.`, tone: "success" });
    } else if (result.status === "COMPLETED") {
      setNotice({
        text: result.sale_id
          ? `Checkout completed, but invoice details were not returned. Sale reference: ${result.sale_id}. Use Open Invoice to retrieve the saved bill if available.`
          : "Checkout completed, but no sale or invoice reference was returned. Do not retry the charge; contact an Admin to reconcile the transaction.",
        tone: "warning",
      });
    } else {
      setNotice({ text: result.message || `Checkout status: ${result.status}. Do not charge again until this sale is reconciled.`, tone: "warning" });
    }
  });

  const openInvoice = () => {
    if (!sale || sale.status !== "COMPLETED" || !sale.sale_id) {
      setInvoiceError("A completed sale reference is required before opening an invoice.");
      return;
    }
    const invoiceWindow = window.open("", "_blank");
    if (!invoiceWindow) {
      setInvoiceError(`The invoice window was blocked. Allow pop-ups and retry for sale ${sale.sale_id}.`);
      return;
    }
    invoiceWindow.opener = null;
    invoiceWindow.document.write("<!doctype html><title>Opening Eluno invoice</title><p>Loading the saved invoice…</p>");
    invoiceWindow.document.close();
    setInvoiceError("");
    void api.invoiceDocument(sale.sale_id).then((document) => {
      invoiceWindow.document.open();
      invoiceWindow.document.write(document);
      invoiceWindow.document.close();
    }).catch((error: unknown) => {
      invoiceWindow.close();
      setInvoiceError(`Could not open invoice${sale.invoice_no ? ` ${sale.invoice_no}` : ""} for sale ${sale.sale_id}: ${reportError(error)}. Retry with Open Invoice.`);
    });
  };

  const recoverCheckout = () => run("recover", async () => {
    if (!sale?.sale_id) throw new Error("No saved sale reference is available for recovery.");
    const result = await api.recoverSale(sale.sale_id, operator);
    setSale(result);
    setNotice({
      text: result.status === "COMPLETED" ? `Sale reconciled. Invoice ${result.invoice_no}.` : result.message || `Sale remains ${result.status}; do not create a new charge.`,
      tone: result.status === "COMPLETED" ? "success" : "warning",
    });
  });
  const reconcileUnknownSale = () => run("checkout", async () => {
    const result = await api.checkout(checkoutPayload());
    setSale(result);
    setNotice({
      text: result.status === "COMPLETED"
        ? `Sale reconciled. Invoice ${result.invoice_no}.`
        : result.message || `Sale remains ${result.status}; do not create a new charge.`,
      tone: result.status === "COMPLETED" ? "success" : "warning",
    });
  });
  const resetFailedAttempt = () => {
    resetSale();
    setNotice({ text: "Verified payment failure. A new idempotent checkout attempt is ready.", tone: "warning" });
  };

  const lookupRefund = () => run("refund-lookup", async () => {
    setRefundPreview(null);
    setRefundAck(false);
    setRefundResult(null);
    if (!refundIdentifier.trim()) throw new Error("Enter an invoice number or Shopify order ID.");
    const result = await api.refundPreview(refundIdentifier.trim());
    setRefundPreview(result);
    setRefundTill(result.available_tills[0]?.session_id || "");
    setRefundMode("full");
    setRefundReason("");
    setRefundOtherReason("");
    setRefundQuantities({});
    setReturnItemIds([]);
    setRefundAck(false);
    setRefundResult(null);
    setRefundKey(makeKey());
  });
  const submitRefund = () => run("refund", async () => {
    if (!refundPreview || !refundAck || !refundPreview.refund_eligible) throw new Error("Review and confirm an eligible order before refunding.");
    if (!refundReason) throw new Error("Choose a reason for the refund.");
    if (refundReason === "OTHER" && !refundOtherReason.trim()) throw new Error("Explain the refund reason when Other is selected.");
    const reason = refundReason === "OTHER"
      ? `OTHER: ${refundOtherReason.trim()}`
      : refundReason.replaceAll("_", " ");
    const items = refundMode === "partial"
      ? refundPreview.lines
        .filter((line) => (refundQuantities[line.line_item_id] || 0) > 0)
        .map((line) => ({ line_item_id: line.line_item_id, quantity: refundQuantities[line.line_item_id] }))
      : undefined;
    if (refundMode === "partial" && !items?.length) throw new Error("Select at least one item quantity for a partial refund.");
    const selectedIds = new Set(items?.map((item) => item.line_item_id) || refundPreview.lines
      .filter((line) => line.remaining_quantity > 0)
      .map((line) => line.line_item_id));
    const result = await api.refund({
      order_id: refundPreview.order_id,
      invoice_no: refundPreview.invoice_no,
      store_code: refundPreview.store_code,
      operator: operator.trim(),
      reason,
      idempotency_key: refundKey,
      ...(items ? { items } : {}),
      return_item_ids: returnItemIds.filter((id) => selectedIds.has(id)),
      ...(refundPreview.payment_gateway === "cash" && refundTill ? { till_session_id: refundTill } : {}),
    });
    setRefundResult(result);
    setNotice({
      text: result.status === "COMPLETED"
        ? `Refund recorded as ${result.refund_id}. Verify provider settlement separately; this integration is a local simulation.`
        : `Refund status is ${result.status}. Check status before retrying.`,
      tone: result.status === "COMPLETED" ? "success" : "warning",
    });
  });
  const loadRefundStatus = () => run("refund-status", async () => {
    if (!refundResult) return;
    const result = await api.refundStatus(refundResult.refund_id);
    setRefundResult(result);
  });
  const reconcileRefund = () => run("refund-reconcile", async () => {
    if (!refundResult) return;
    const result = await api.recoverRefund(refundResult.refund_id);
    setRefundResult(result);
    setNotice({
      text: result.status === "COMPLETED"
        ? `Refund ${result.refund_id} was reconciled with Shopify. Verify provider settlement separately; this integration is a local simulation.`
        : `Refund remains ${result.status}. Do not submit a new refund until this outcome is resolved.`,
      tone: result.status === "COMPLETED" ? "success" : "warning",
    });
  });

  const selectedPartialRefund = refundPreview?.lines.reduce((total, line) => {
    const quantity = Math.min(line.remaining_quantity, Math.max(0, refundQuantities[line.line_item_id] || 0));
    if (!quantity || !line.quantity) return total;
    const before = allocatePaise(line.line_refundable_paise, line.refunded_quantity, line.quantity);
    const after = allocatePaise(line.line_refundable_paise, line.refunded_quantity + quantity, line.quantity);
    return total + after - before;
  }, 0) || 0;
  const selectedRefundAmount = refundMode === "full"
    ? refundPreview?.remaining_refundable_paise || 0
    : selectedPartialRefund;

  const loadReport = () => run("report", async () => {
    setReport(null);
    if (reportScope === "store" && !locationId) throw new Error("Select a store first.");
    const result = reportScope === "overall"
      ? await api.overallReport(reportDate)
      : await api.dayEnd(locationId, reportDate);
    setReport(result);
  });

  const createAdminProduct = () => run("admin-product", async () => {
    const priceParts = unitPrice.trim().match(/^(\d+)(?:\.(\d{1,2}))?$/);
    if (!priceParts) throw new Error("Enter a product price with up to two decimal places.");
    const pricePaise = Number(priceParts[1]) * 100 + Number((priceParts[2] || "").padEnd(2, "0"));
    if (!Number.isSafeInteger(pricePaise) || pricePaise <= 0) throw new Error("Enter a valid positive product price.");
    const common = {
      line_type: productType,
      title: productTitle.trim(),
      sku: productSku.trim(),
      variant_title: variantTitle.trim(),
      price_paise: pricePaise,
      actor: operator,
    };
    const payload = productType === "frame"
      ? { ...common, category: frameCategory, material: frameMaterial.trim(), size: frameSize.trim(), rx_able: rxAble }
      : { ...common, lens_type: lensType, index: lensIndex, coating: lensCoating };
    await api.createProduct(payload);
    setNotice({ text: `${productTitle.trim()} was created in the Shopify catalog.`, tone: "success" });
    setCreateDialogOpen(false);
    setProductTitle("");
    setProductSku("");
    setVariantTitle("");
    setUnitPrice("");
    await Promise.all([loadAdminFrames(), loadCatalogProducts()]);
  });

  const receiveFrameStock = () => run("stock-receipt", async () => {
    const selected = adminFrames.find((item) => String(item.variant_id) === receiptVariantId);
    const quantity = Number(receiptQuantity);
    if (!selected || !location || !Number.isInteger(quantity) || quantity <= 0 || !receiptReference.trim()) {
      throw new Error("Choose a frame, enter a positive whole quantity, and provide the goods-received reference.");
    }
    const result = await api.receiveStock({
      inventory_item_id: Number(selected.inventory_item_id),
      location_id: Number(location.id),
      quantity,
      actor: operator,
      reference: receiptReference.trim(),
    });
    setNotice({
      text: result.idempotent_replay
        ? `Receipt ${receiptReference.trim()} was already recorded; no additional stock was added.`
        : `${result.received_quantity} units received against ${receiptReference.trim()}.`,
      tone: "success",
    });
    await loadAdminFrames();
  });

  const saveCatalogProduct = (product: AdminCatalogProduct, form: HTMLFormElement) => run(
    `catalog-${product.variant_id}`,
    async () => {
      const values = new FormData(form);
      const payload: Record<string, string | number | boolean> = {
        variant_id: product.variant_id,
        title: String(values.get("title") || ""),
        description: String(values.get("description") || ""),
        status: String(values.get("status") || ""),
        sku: String(values.get("sku") || ""),
        variant_title: String(values.get("variant_title") || ""),
        price_paise: parsePaise(String(values.get("price") || "")),
      };
      if (product.line_type === "frame") {
        payload.category = String(values.get("category") || "");
        payload.material = String(values.get("material") || "");
        payload.size = String(values.get("size") || "");
        payload.rx_able = values.get("rx_able") === "on";
      } else {
        payload.lens_type = String(values.get("lens_type") || "");
        payload.index = String(values.get("index") || "");
        payload.coating = String(values.get("coating") || "");
      }
      await api.updateProduct(product.product_id, payload);
      setNotice({ text: `${product.title} was updated. Inventory quantities were not changed.`, tone: "success" });
      setEditingProduct(null);
      setArchiveConfirmation(false);
      await Promise.all([loadCatalogProducts(), loadAdminFrames()]);
    },
  );

  const archiveCatalogProduct = (product: AdminCatalogProduct) => run(
    `archive-${product.variant_id}`,
    async () => {
      await api.updateProduct(product.product_id, {
        variant_id: product.variant_id,
        status: "archived",
      });
      setNotice({ text: `${product.title} was archived. Inventory quantities were not changed.`, tone: "success" });
      setEditingProduct(null);
      setArchiveConfirmation(false);
      await Promise.all([loadCatalogProducts(), loadAdminFrames()]);
    },
  );

  const openTillSession = () => run("till-open", async () => {
    const selected = locations.find((item) => String(item.id) === locationId);
    if (!selected) throw new Error("Choose an assigned store before opening a till.");
    const opened = await api.openTill({
      store_code: selected.store_code,
      location_id: String(selected.id),
      opening_float_paise: parsePaise(tillOpenFloat),
    });
    setTillOpenFloat("");
    setNotice({ text: `Till opened for ${selected.store_code}.`, tone: "success" });
    setTillSessions((current) => [opened, ...current.filter((item) => item.session_id !== opened.session_id)]);
    await loadTillSessions();
  });

  const addTillMovement = (session: TillSession) => run("till-movement", async () => {
    await api.tillMovement(session.session_id, {
      type: tillMovementType,
      amount_paise: parsePaise(tillMovementAmount),
      reason: tillMovementReason.trim(),
      ref: `manual-${globalThis.crypto?.randomUUID?.() || Date.now()}`,
    });
    setTillMovementAmount("");
    setTillMovementReason("");
    setNotice({ text: `${tillMovementType === "paid_in" ? "Paid-in" : "Paid-out"} movement recorded.`, tone: "success" });
    await loadTillSessions();
  });

  const closeTillSession = (session: TillSession) => run("till-close", async () => {
    const closed = await api.closeTill(session.session_id, {
      counted_cash_paise: parsePaise(tillCounted),
      review_note: tillReviewNote.trim() || undefined,
    });
    setTillCounted("");
    setTillReviewNote("");
    setNotice({
      text: `Till closed. Expected ${money(closed.expected_paise)}; counted ${money(closed.counted_cash_paise ?? undefined)}; variance ${money(closed.variance_paise ?? undefined)}.`,
      tone: closed.variance_paise ? "warning" : "success",
    });
    await loadTillSessions();
  });

  const reviewTillSession = (session: TillSession) => run(`till-review-${session.session_id}`, async () => {
    const note = tillAdminReview[session.session_id] || "";
    const result = await api.reviewTill(session.session_id, note);
    setTillAdminReview((current) => ({ ...current, [session.session_id]: "" }));
    setNotice({ text: `${result.session_id} marked reviewed.`, tone: "success" });
    await loadTillSessions();
  });

  const createStaffAccount = () => run("staff-create", async () => {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$/.test(staffUsername.trim())) {
      throw new Error("Username must be 3–32 characters using letters, numbers, dot, underscore or hyphen.");
    }
    if (!strongPassword(staffPassword)) {
      throw new Error("Password must be 12+ characters and include uppercase and lowercase letters, a number, and a symbol.");
    }
    if (staffRole !== "admin" && !staffStoreCode) {
      throw new Error("Assign each Cashier and Optometrist to a store.");
    }
    if (staffRole === "optometrist" && !staffExternalId) {
      throw new Error("Link the account to an Optometrist in the connected clinical directory.");
    }
    const result = await api.createStaff({
      username: staffUsername.trim(),
      display_name: staffDisplayName.trim(),
      role: staffRole,
      password: staffPassword,
      store_code: staffStoreCode || null,
      ...(staffRole === "optometrist" ? { external_staff_id: staffExternalId } : {}),
    });
    setStaffAccounts((current) => [...current, result.staff].sort((a, b) => a.display_name.localeCompare(b.display_name)));
    setStaffUsername("");
    setStaffDisplayName("");
    setStaffPassword("");
    setStaffStoreCode("");
    setStaffExternalId("");
    setNotice({ text: `${result.staff.display_name} (${result.staff.role}) was created.`, tone: "success" });
  });

  const assignStaffStore = (account: StaffAccount) => run("staff-store", async () => {
    const storeCode = staffStoreDrafts[account.staff_id] ?? account.store_code ?? "";
    if (account.role === "cashier" && !storeCode) {
      throw new Error("Cashier accounts must remain assigned to a store.");
    }
    const result = await api.updateStaff(account.staff_id, { store_code: storeCode || null });
    setStaffAccounts((current) => current.map((item) => item.staff_id === account.staff_id ? result.staff : item));
    setNotice({ text: `${account.username}'s store assignment was updated.`, tone: "success" });
  });

  const resetStaffPassword = (staffId: string) => run("staff-password", async () => {
    if (!strongPassword(resetPassword)) {
      throw new Error("Password must be 12+ characters and include uppercase and lowercase letters, a number, and a symbol.");
    }
    const result = await api.updateStaff(staffId, { password: resetPassword });
    setStaffAccounts((current) => current.map((item) => item.staff_id === staffId ? result.staff : item));
    setResetPassword("");
    setResetStaffId("");
    if (staffId === user.staff_id) {
      onLogout();
      return;
    }
    setNotice({ text: `Password reset for ${result.staff.username}.`, tone: "success" });
  });

  const toggleStaffActive = (account: StaffAccount) => run("staff-status", async () => {
    const result = await api.updateStaff(account.staff_id, { is_active: !account.is_active });
    setStaffAccounts((current) => current.map((item) => item.staff_id === account.staff_id ? result.staff : item));
    if (account.staff_id === user.staff_id && !result.staff.is_active) {
      onLogout();
      return;
    }
    setNotice({ text: `${account.username} is now ${result.staff.is_active ? "active" : "disabled"}.`, tone: "success" });
  });

  const customerWarnings = rxAxisWarnings(prescription);
  const payable = pricing?.payable_paise ?? 0;
  const isComplete = sale?.status === "COMPLETED";
  const canCharge = cart.length > 0 && !!location && !!pricing && !busy && !sale &&
    (!prescription || customerWarnings.length === 0 || reviewedRx.includes(prescription.rx_id));

  return (
    <>
      <header className="topbar">
        <a className="brand" href="/" aria-label=" | Premium Eyeglasses, Sunglasses & Clip-On"><span className="brand-mark"><img className="brand-logo" src="/pos/images/eluno.png" alt="Eluno optical retail" /></span><span className="brand-tagline">| Premium Eyeglasses, Sunglasses &amp; Clip-On</span></a>
        <div className="session-controls">
          {user.role === "admin" ? <label>Active store<select aria-label="Active store" value={locationId} onChange={(event) => {
            const selected = event.target.value;
            setLocationId(selected);
            window.localStorage.setItem("eluno:selected-store", selected);
            resetSale();
            setRefundPreview(null);
            setRefundAck(false);
            setRefundResult(null);
          }}>
            {!locations.length && <option value="">Loading stores…</option>}
            {locations.map((item) => <option key={item.id} value={item.id}>{item.store_code} · {item.name}</option>)}
          </select></label> : <label>Assigned store<select aria-label="Assigned store" value={locationId} disabled>
            {!locations.length && <option value="">No store assigned</option>}
            {locations.map((item) => <option key={item.id} value={item.id}>{item.store_code} · {item.name}</option>)}
          </select></label>}
          <label>Signed in as<span className="session-operator" aria-label="Operator">{user.display_name || user.username} · {user.role} · ID {operator}</span></label>
          <label>Business date<span className="session-operator" aria-label="Business date" aria-live="polite">{businessDate || "Loading…"}</span></label>
          <button className="button header-logout" onClick={() => {
            void api.logout().then(onLogout).catch((error: unknown) =>
              setNotice({ text: reportError(error), tone: "error" }),
            );
          }}>Sign out</button>
        </div>
      </header>
      <nav className="workspace-nav" aria-label="Studio workspace">
        {(user.role === "admin"
          ? ["studio", "admin", "customers", "staff", "report", "till"] as const
          : user.role === "cashier"
            ? ["studio", "checkout", "refund", "store-orders", "customers", "till"] as const
            : ["studio", "customers", "prescriptions"] as const).map((item) =>
          <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>
            {item === "studio" ? "Studio" : item === "checkout" ? "Sales" : item === "admin" ? "Inventory" : item === "customers" ? "Customers" : item === "prescriptions" ? "Prescriptions" : item === "staff" ? "Team" : item === "refund" ? "Returns & Refunds" : item === "store-orders" ? "Store Orders" : item === "till" ? "Till" : "Reports"}
            {item === "store-orders" && storeOrders.length > 0 && <span className="nav-count" aria-hidden="true">{storeOrders.length}</span>}
          </button>,
        )}
      </nav>
      <main>
        {notice && <div className={`notice ${notice.tone}`} role="status" aria-live="polite">{notice.text}<button aria-label="Dismiss message" onClick={() => setNotice(undefined)}>×</button></div>}
        {view === "studio" && <section className="studio-home" aria-labelledby="studio-title">
          <div className="studio-intro">
            <span className="eyebrow">{location ? `${location.store_code} · ${location.name}` : "| Premium Eyeglasses, Sunglasses & Clip-On"}</span>
            <h1 id="studio-title">Welcome to the Studio</h1>
            <p className="muted">A considered space for eyewear, service, and store operations.</p>
          </div>
          <div className="studio-actions">
            {user.role === "cashier" && <button className="studio-action" onClick={() => setView("checkout")}><strong>Sales</strong><span>Build a selection and complete a sale</span></button>}
            <button className="studio-action" onClick={() => setView("customers")}><strong>Customers</strong><span>Find a customer and review prescription details</span></button>
            {user.role === "optometrist" && <button className="studio-action" onClick={() => setView("prescriptions")}><strong>Prescriptions</strong><span>Record customer prescriptions for your assigned store</span></button>}
            {user.role === "admin" && <>
              <button className="studio-action" onClick={() => setView("admin")}><strong>Inventory</strong><span>Manage the catalog and store stock</span></button>
              <button className="studio-action" onClick={() => setView("staff")}><strong>Team</strong><span>Manage Studio accounts and access</span></button>
              <button className="studio-action" onClick={() => setView("report")}><strong>Reports</strong><span>Review store performance and day-end activity</span></button>
            </>}
            {user.role === "cashier" && <button className="studio-action" onClick={() => setView("refund")}><strong>Returns &amp; Refunds</strong><span>Find a transaction and review return eligibility</span></button>}
            {user.role === "cashier" && <button className="studio-action" onClick={() => setView("store-orders")}><strong>Store Orders</strong><span>Review customer online order requests for this store</span></button>}
            {user.role !== "optometrist" && <button className="studio-action" onClick={() => setView("till")}><strong>Till</strong><span>Open, reconcile, and review cash movements</span></button>}
          </div>
        </section>}
        {view === "store-orders" && user.role === "cashier" && <section className="panel focused-view" aria-labelledby="store-orders-title">
          <div className="section-title">
            <div><span className="eyebrow">{location?.store_code || user.store_code || "ASSIGNED STORE"}</span><h1 id="store-orders-title">Store Orders</h1></div>
            <button type="button" className="button secondary" onClick={() => void loadStoreOrders()} disabled={storeOrdersLoading}>
              {storeOrdersLoading ? "Refreshing…" : "Refresh"}
            </button>
          </div>
          <p className="muted">Review customer requests assigned to your store. Confirming records the store decision only; no online payment is collected and no fulfilment is submitted.</p>
          {storeOrdersError && <p className="notice error" role="alert">{storeOrdersError}</p>}
          {storeOrdersLoading && <p role="status" className="muted">Loading store requests…</p>}
          {!storeOrdersLoading && !storeOrdersError && !storeOrders.length && <p className="empty">There are no pending customer order requests for this store.</p>}
          <div className="store-order-list">{storeOrders.map((order) => <article className="store-order-card" key={order.order_id}>
            <header><div><h2>{order.order_id}</h2><span>{new Date(order.created_at).toLocaleString("en-IN")} · {order.store_code}</span></div><strong>{money(order.total_paise)}</strong></header>
            <p><strong>Customer:</strong> {order.customer_name || "Name unavailable"}</p>
            <p><strong>Payment:</strong> {order.payment_status === "paid" ? "Marked paid" : "Awaiting payment"}{order.payment_method_preference ? ` · Preferred ${order.payment_method_preference.toUpperCase()} (not collected)` : ""} · <strong>Store decision:</strong> Pending</p>
            <ul>{order.items.map((item, index) => <li key={`${order.order_id}-${index}`}>{item.title} × {item.quantity}{item.prescription_assistance_requested ? " · Optometrist assistance requested" : ""}</li>)}</ul>
            {order.status_message && <p className="muted small">{order.status_message}</p>}
            <label className="field">Reason if denying
              <input maxLength={400} value={storeOrderReasons[order.order_id] || ""} onChange={(event) => setStoreOrderReasons((current) => ({ ...current, [order.order_id]: event.target.value }))} />
            </label>
            <div className="inline-actions">
              <button type="button" className="button primary" disabled={storeOrderBusy !== null || order.payment_status === "paid"} onClick={() => void decideStoreOrder(order, "confirm")}>
                {storeOrderBusy === order.order_id ? "Saving…" : "Confirm store request"}
              </button>
              <button type="button" className="button secondary" disabled={storeOrderBusy !== null || order.payment_status === "paid"} onClick={() => void decideStoreOrder(order, "deny")}>Deny request</button>
            </div>
            {order.payment_status === "paid" && <p className="notice warning">This request shows a captured payment; it cannot be denied until a supported refund is recorded.</p>}
          </article>)}</div>
        </section>}
        {(view === "customers" || view === "prescriptions") && <section className="panel focused-view" aria-labelledby="customers-title">
          <div className="section-title"><div><span className="eyebrow">{view === "prescriptions" ? "CLINICAL WORKFLOW" : "CUSTOMER CARE"}</span><h1 id="customers-title">{view === "prescriptions" ? "Prescriptions" : "Customers"}</h1></div></div>
          {view === "prescriptions" && user.role === "optometrist" && <OptometristAssistanceInbox onOpenCustomer={(mobile) => {
            setPhone(mobile);
            void lookupCustomer(mobile);
          }} />}
          <p className="muted small">{user.role === "admin"
            ? "Search customer records by name, mobile, email, or customer ID and review purchase history."
            : user.role === "cashier"
              ? "Search for a customer for checkout. Customer profile management is restricted."
              : "Find the customer and record a prescription for your assigned store."}</p>
          {user.role !== "cashier" && <CustomerSearch allowCustomerId={user.role === "admin"} onSelect={selectSearchedCustomer} />}
          {user.role !== "admin" && <div className="inline-form">
            <label className="field grow">Mobile number<input inputMode="tel" autoComplete="tel" value={phone} onChange={(event) => {
              setPhone(event.target.value);
              setCustomerLookupMiss(false);
              setCustomerFormOpen(false);
              setCustomerProfile(null);
              setPrescriptionEntryOpen(false);
              if (customer || prescription || walkIn) {
                setCustomer(null);
                setPrescription(null);
                setReviewedRx([]);
                setWalkIn(false);
                resetSale();
              }
            }} placeholder="Search by mobile" onKeyDown={(event) => event.key === "Enter" && lookupCustomer()} /></label>
            <button className="button secondary" onClick={() => lookupCustomer()} disabled={busy !== null}>{busy === "customer" ? "Searching…" : "Find customer"}</button>
          </div>}
          {user.role !== "admin" && (customer ? <div className="customer-record"><strong>{customerName(customer)}</strong><span>{customer.phone}</span></div>
            : customerLookupMiss ? <p className="empty">No customer record found for {phone.trim()}.</p>
              : <p className="empty">Search by mobile number to find a customer record.</p>)}
          {customer && <div className="customer-actions">
            {user.role === "admin" && <button className="button secondary" onClick={() => loadCustomerProfile(1)} disabled={busy !== null}>
              {busy === "customer-profile" ? "Loading profile…" : "View profile & purchase history"}
            </button>}
            {(user.role === "optometrist" || user.role === "cashier" && !prescription) && <button className="button secondary" onClick={() => setPrescriptionEntryOpen((open) => !open)} disabled={!location}>
            {prescriptionEntryOpen ? "Close prescription form" : "Add Prescription"}
            </button>}
          </div>}
          {prescriptionEntryOpen && customer && location && <PrescriptionEntryForm
            customer={customer}
            storeCode={location.store_code}
            businessDate={businessDate}
            optometristId=""
            onCancel={() => setPrescriptionEntryOpen(false)}
            onSaved={prescriptionSaved}
          />}
          {prescription && <div className="rx-record">
            <div className="rx-heading"><strong>Prescription on file</strong><span>Valid through {prescription.valid_until}</span></div>
            <div className="rx-values"><span>OD · SPH {prescription.od.sph ?? "—"} · CYL {prescription.od.cyl ?? "—"} · AXIS {prescription.od.axis ?? "—"}</span><span>OS · SPH {prescription.os.sph ?? "—"} · CYL {prescription.os.cyl ?? "—"} · AXIS {prescription.os.axis ?? "—"}</span></div>
            {customerWarnings.length > 0 && <p className="review-warning">{customerWarnings.join(" ")}</p>}
          </div>}
          {user.role === "admin" && customerProfileLoading && <p role="status">Loading saved customer records…</p>}
          {user.role === "admin" && customerProfile && <section className="customer-profile panel" aria-labelledby="customer-profile-title">
            <div className="section-title"><div><span className="eyebrow">CUSTOMER PROFILE</span><h2 id="customer-profile-title">{customerName(customerProfile.customer)}</h2></div>
              <span className="status-chip">ID {customerProfile.customer.id}</span></div>
            <dl className="profile-facts">
              <div><dt>Phone</dt><dd>{customerProfile.customer.phone || "Anonymized"}</dd></div>
              <div><dt>Email</dt><dd>{customerProfile.customer.email || "Not recorded"}</dd></div>
              <div><dt>Created</dt><dd>{customerProfile.customer.created_at ? new Date(customerProfile.customer.created_at).toLocaleDateString("en-IN") : "Not recorded"}</dd></div>
              <div><dt>Purchases</dt><dd>{customerProfile.purchase_count}</dd></div>
              <div><dt>Lifetime spend after refunds</dt><dd>{money(customerProfile.lifetime_spend_paise)}</dd></div>
              <div><dt>Most recent purchase</dt><dd>{customerProfile.most_recent_purchase ? new Date(customerProfile.most_recent_purchase).toLocaleString("en-IN") : "None"}</dd></div>
              <div><dt>Shopify customer ID</dt><dd>{customerProfile.customer.id}</dd></div>
            </dl>
            {customerProfile.customer.note && <p className="small"><strong>Notes:</strong> {customerProfile.customer.note}</p>}
            {!!customerProfile.customer.tags.length && <div className="tags">{customerProfile.customer.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>}
            {customerProfile.prescriptions.length > 0 ? <section aria-labelledby="profile-rx-title">
              <h3 id="profile-rx-title">Prescription history</h3>
              {customerProfile.prescriptions.map((rx) => <div className="rx-record" key={rx.rx_id}>
                <div className="rx-heading"><strong>{rx.rx_id}</strong><span>{rx.date} · valid through {rx.valid_until}</span></div>
                <div className="rx-values"><span>OD {rx.od.sph ?? "—"} / {rx.od.cyl ?? "—"} × {rx.od.axis ?? "—"}</span><span>OS {rx.os.sph ?? "—"} / {rx.os.cyl ?? "—"} × {rx.os.axis ?? "—"}</span></div>
                <small className="muted">Optometrist {rx.optometrist_id || "Not recorded"} · {rx.store_code || "Store not recorded"}</small>
              </div>)}
            </section> : <p className="empty">No prescription records were found for this customer.</p>}
            <h3>Purchase history</h3>
            {!customerProfile.history_complete && <p className="notice warning">History is limited by the connected Shopify API to its latest 250 orders.</p>}
            {!customerProfile.purchases.length ? <p className="empty">This customer has no saved purchases.</p> : <div className="purchase-history">
              {customerProfile.purchases.map((purchase) => <article className="purchase-card" key={purchase.order_id}>
                <div className="purchase-heading"><div><strong>{purchase.invoice_no || purchase.shopify_reference}</strong><small>{purchase.date ? new Date(purchase.date).toLocaleString("en-IN") : "Date unavailable"} · {purchase.store_code || "Store not recorded"}</small></div>
                  <strong>{money(purchase.total_paise)}</strong></div>
                <p>{purchase.shopify_reference} · payment {purchase.status} · fulfillment {purchase.fulfillment_status} · {purchase.payment_method || "Payment method unavailable"}</p>
                <ul>{purchase.items.map((item, index) => <li key={`${purchase.order_id}-${index}`}>{item.title} × {item.quantity} · {money(item.line_total_paise)} · discount {money(item.discount_paise)} · tax {money(item.tax_paise)}</li>)}</ul>
                <p>Tax: CGST {money(purchase.cgst_paise)} · SGST {money(purchase.sgst_paise)} · Refunded {money(purchase.refunded_paise)}</p>
                {purchase.refunds.map((refund) => <small key={refund.id} className="muted">Refund {refund.id}: {money(refund.amount_paise)} · tax adjustment {money(refund.tax_paise)} · {refund.reason || "Reason not recorded"}</small>)}
                {purchase.sale_id && <button className="button secondary" onClick={() => window.open(`/api/invoices/${encodeURIComponent(purchase.sale_id || "")}`, "_blank", "noopener,noreferrer")}>Open saved invoice</button>}
              </article>)}
            </div>}
            {customerProfile.has_more_purchases && <button className="button secondary" onClick={() => loadCustomerProfile((customerProfile.purchase_page || 1) + 1)} disabled={customerProfileLoading || busy !== null}>
              {customerProfileLoading ? "Loading purchases…" : "Load more purchase history"}
            </button>}
            {user.role === "admin" && <div className="customer-privacy-actions">
              <button className="button secondary" onClick={() => setCustomerEditOpen((open) => !open)}>Edit customer</button>
              {!customerProfile.customer.anonymized && <form className="anonymize-form" onSubmit={(event) => { event.preventDefault(); anonymizeCustomer(); }}>
                <label className="field">Privacy request / anonymization reason<textarea required maxLength={500} value={anonymizeReason} onChange={(event) => setAnonymizeReason(event.target.value)} /></label>
                <button className="button danger" type="submit" disabled={busy !== null}>{busy === "customer-anonymize" ? "Anonymizing…" : "Delete Customer (anonymize)"}</button>
              </form>}
              <p className="small muted">{customerProfile.deletion_policy}</p>
            </div>}
            {user.role === "admin" && customerEditOpen && <form className="customer-edit-form" onSubmit={(event) => { event.preventDefault(); saveCustomerProfile(); }}>
              <label className="field">Full name<input required maxLength={100} value={customerEditName} onChange={(event) => setCustomerEditName(event.target.value)} /></label>
              <label className="field">Email<input type="email" maxLength={254} value={customerEditEmail} onChange={(event) => setCustomerEditEmail(event.target.value)} /></label>
              <p className="small muted">Phone is not editable because it is the supported customer and prescription lookup key.</p>
              <button className="button primary" type="submit" disabled={busy !== null}>{busy === "customer-update" ? "Saving…" : "Save changes"}</button>
            </form>}
          </section>}
          {customerLookupMiss && !customerFormOpen && <div className="customer-create-prompt" role="status">
            <p>No customer record was found for {phone.trim()}.</p>
            <button className="button secondary" onClick={() => { setNewCustomerPhone(phone.trim()); setCustomerFormOpen(true); }}>Create new customer</button>
          </div>}
          {customerFormOpen && <form className="customer-create-form" onSubmit={(event) => { event.preventDefault(); createCustomer(); }}>
            <h2>Create new customer</h2>
            <label className="field">Full name<input autoComplete="name" required maxLength={100} value={newCustomerName} onChange={(event) => setNewCustomerName(event.target.value)} /></label>
            <label className="field">Mobile number<input type="tel" inputMode="tel" autoComplete="tel" required value={newCustomerPhone} onChange={(event) => setNewCustomerPhone(event.target.value)} /></label>
            <div className="dialog-actions">
              <button className="button secondary" type="button" onClick={() => setCustomerFormOpen(false)}>Cancel</button>
              <button className="button primary" type="submit" disabled={busy !== null}>{busy === "customer-create" ? "Saving…" : "Save and select customer"}</button>
            </div>
          </form>}
        </section>}
        {view === "checkout" && user.role === "cashier" && (
          <div className="checkout-layout">
            <div className="work-column">
              <section className="panel customer-panel" aria-labelledby="customer-title">
                <div className="section-title"><div><span className="eyebrow">01 · CUSTOMER</span><h1 id="customer-title">Customer & prescription</h1></div><span className={`status-chip ${customer ? "selected" : ""}`}>{customer ? "Selected" : walkIn ? "Walk-in" : "Not selected"}</span></div>
                <div className="inline-form">
                  <label className="field grow">Mobile number<input inputMode="tel" autoComplete="tel" value={phone} onChange={(event) => {
                    setPhone(event.target.value);
                    setCustomerLookupMiss(false);
                    setCustomerFormOpen(false);
                    setCustomerProfile(null);
                    setPrescriptionEntryOpen(false);
                    if (customer || prescription || walkIn) {
                      setCustomer(null);
                      setPrescription(null);
                      setReviewedRx([]);
                      setWalkIn(false);
                      resetSale();
                    }
                  }} placeholder="Search by mobile" onKeyDown={(event) => event.key === "Enter" && lookupCustomer()} /></label>
                  <button className="button secondary" onClick={() => lookupCustomer()} disabled={busy !== null}>{busy === "customer" ? "Searching…" : "Find customer"}</button>
                  <button className="button text" onClick={selectWalkIn}>Walk-in sale</button>
                </div>
                {customer ? <div className="customer-record"><strong>{customerName(customer)}</strong><span>{customer.phone}</span><div className="tags">{(customer.tags || []).map((tag) => <span key={tag}>{tag}</span>)}</div></div> : <p className="muted small">{walkIn ? "Walk-in selected. Prescription-dependent items require a verified customer." : "Look up a customer or select a walk-in sale."}</p>}
                {customerLookupMiss && !customerFormOpen && <div className="customer-create-prompt" role="status">
                  <p>No customer record was found for {newCustomerPhone}.</p>
                  <button className="button secondary" onClick={() => {
                    setCustomerFormOpen(true);
                    setNotice(undefined);
                  }}>Create new customer</button>
                </div>}
                {customerFormOpen && <form className="customer-create-form" onSubmit={(event) => {
                  event.preventDefault();
                  createCustomer();
                }}>
                  <h2>Create new customer</h2>
                  <label className="field">Full name<input autoComplete="name" required maxLength={100} value={newCustomerName} onChange={(event) => setNewCustomerName(event.target.value)} /></label>
                  <label className="field">Mobile number<input type="tel" inputMode="tel" autoComplete="tel" required value={newCustomerPhone} onChange={(event) => setNewCustomerPhone(event.target.value)} aria-describedby="new-customer-phone-help" /></label>
                  <small id="new-customer-phone-help" className="muted">Indian mobile, 10 digits; +91 prefix accepted. This lookup supports phone only.</small>
                  <div className="dialog-actions">
                    <button className="button secondary" type="button" onClick={() => setCustomerFormOpen(false)}>Cancel</button>
                    <button className="button primary" type="submit" disabled={busy !== null}>{busy === "customer-create" ? "Saving…" : "Save and select customer"}</button>
                  </div>
                </form>}
                {prescription && <div className={`rx-record ${customerWarnings.length ? "review" : "valid"}`}>
                  <div className="rx-heading"><strong>{prescription.rx_id}</strong><span>{customerWarnings.length ? "OPTOMETRIST REVIEW REQUIRED" : "VALID PRESCRIPTION"}</span></div>
                  <div className="rx-values"><span>OD {prescription.od.sph ?? "—"} / {prescription.od.cyl ?? "—"} × {prescription.od.axis ?? "—"} / Add {prescription.od.add ?? "—"}</span><span>OS {prescription.os.sph ?? "—"} / {prescription.os.cyl ?? "—"} × {prescription.os.axis ?? "—"} / Add {prescription.os.add ?? "—"}</span></div>
                  {customerWarnings.map((warning) => <p key={warning} className="review-warning">{warning}</p>)}
                  {customerWarnings.length > 0 && <label className="review-control"><input type="checkbox" checked={reviewedRx.includes(prescription.rx_id)} onChange={(event) => {
                    if (event.target.checked) setReviewedRx((items) => [...new Set([...items, prescription.rx_id])]);
                    else setReviewedRx((items) => items.filter((id) => id !== prescription.rx_id));
                    resetSale();
                  }} /> Optometrist has reviewed this prescription; acknowledge for this sale.</label>}
                </div>}
                {customer && !prescription && <div className="callout">
                  <p>No valid prescription is on file. Enter the prescription details and select the assigned Optometrist who verified them before adding lenses.</p>
                  <button type="button" className="button secondary" onClick={() => setPrescriptionEntryOpen((open) => !open)} disabled={!location}>
                    {prescriptionEntryOpen ? "Close prescription form" : "Add prescription"}
                  </button>
                </div>}
                {prescriptionEntryOpen && customer && location && <PrescriptionEntryForm
                  customer={customer}
                  storeCode={location.store_code}
                  businessDate={businessDate}
                  optometristId=""
                  onCancel={() => setPrescriptionEntryOpen(false)}
                  onSaved={prescriptionSaved}
                />}
              </section>

              <section className="panel catalog-panel" aria-labelledby="catalog-title">
                <div className="section-title"><div><span className="eyebrow">02 · SELECTION</span><h2 id="catalog-title">Catalog</h2></div></div>
                <div className="catalog-tools">
                  <div className="segmented" aria-label="Catalog item type">
                    <button className={lineType === "frame" ? "selected" : ""} onClick={() => loadCatalog("frame")}>Frames</button>
                    <button className={lineType === "lens" ? "selected" : ""} onClick={() => loadCatalog("lens")}>Lenses</button>
                  </div>
                  <input aria-label="Search catalog" type="search" placeholder="Search catalog…" value={search} onChange={(event) => setSearch(event.target.value)} />
                  <button className="button text" onClick={() => loadCatalog(lineType)} disabled={busy !== null}>Refresh</button>
                </div>
                <div className="catalog-list" aria-live="polite">
                  {!filteredVariants.length ? <p className="empty">No catalog items match this search.</p> : filteredVariants.map((variant) => (
                    <div className="catalog-row" key={variant.variant_id}>
                      <div><strong>{[variant.product_title, variant.variant_title].filter(Boolean).join(" · ") || variant.variant_id}</strong><small>{variant.variant_id}{variant.lens_type ? ` · ${variant.lens_type} · ${variant.index} · ${variant.coating}` : ""}</small></div>
                      <strong>{money(variant.price_paise)}</strong>
                      {variant.line_type === "lens" && customer && !prescription
                        ? <button className="button secondary" onClick={() => setPrescriptionEntryOpen(true)} disabled={!location}>
                          Add prescription first
                        </button>
                        : <button className="button add" onClick={() => addVariant(variant)} disabled={busy !== null}>Add</button>}
                    </div>
                  ))}
                </div>
                {lensWarning && <div className="lens-warning" role="alert">
                  <strong>Lens validation</strong>
                  {lensWarning.errors.map((error) => <p key={error}>{error}</p>)}
                  {lensWarning.warnings.map((warning) => <p key={warning}>{warning}</p>)}
                  {lensWarning.warnings.length > 0 && <label className="review-control"><input type="checkbox" checked={reviewAck} onChange={(event) => setReviewAck(event.target.checked)} /> Optometrist review was completed.</label>}
                  <button className="button primary" disabled={!reviewAck} onClick={confirmPrescriptionReview}>Acknowledge and add lens</button>
                </div>}
              </section>
              <section className="panel cart-panel" aria-labelledby="cart-title">
                <div className="section-title"><div><span className="eyebrow">03 · ORDER</span><h2 id="cart-title">Current cart</h2></div><span className="status-chip">{cart.reduce((sum, item) => sum + item.quantity, 0)} items</span></div>
                {!cart.length ? <p className="empty">Select a frame or validated lens to begin.</p> : <div className="cart-list">{cart.map((line, index) => {
                  const priced = pricing?.lines.find((_, idx) => idx === index);
                  return <div className="cart-row" key={`${line.variant.variant_id}-${index}`}>
                    <div><strong>{line.variant.product_title} {line.variant.variant_title}</strong><small>{line.variant.variant_id}{line.variant.line_type === "lens" && prescription ? ` · RX ${prescription.rx_id}` : ""}{line.pairRef ? " · paired set" : ""}</small></div>
                    <label className="qty-label">Qty<input aria-label={`Quantity for ${line.variant.variant_id}`} type="number" min="1" value={line.quantity} onChange={(event) => changeQuantity(index, Number(event.target.value))} /></label>
                    <strong>{money(priced?.payable_paise ?? line.variant.price_paise * line.quantity)}</strong>
                    <button className="remove" aria-label={`Remove ${line.variant.variant_id}`} onClick={() => removeLine(index)}>×</button>
                  </div>;
                })}</div>}
              </section>
            </div>
            <aside className="summary panel" aria-labelledby="summary-title">
              <div className="section-title"><div><span className="eyebrow">04 · PAYMENT</span><h2 id="summary-title">Order summary</h2></div></div>
              <dl className="totals">
                <div><dt>Gross</dt><dd>{money(pricing?.gross_paise)}</dd></div>
                <div><dt>Offer discounts</dt><dd>−{money(pricing?.discount_paise)}</dd></div>
                <div><dt>Taxable amount</dt><dd>{money(pricing?.taxable_paise)}</dd></div>
                <div><dt>CGST</dt><dd>{money(pricing?.cgst_paise)}</dd></div>
                <div><dt>SGST</dt><dd>{money(pricing?.sgst_paise)}</dd></div>
              </dl>
              <div className="grand-total"><span>Amount due</span><strong>{money(payable)}</strong></div>
              <section className="offers" aria-live="polite">
                <div className="offer-title">
                  <strong>Offers</strong>
                  <div>
                    <button className="button text" onClick={() => {
                      setPricing(null);
                      setPricingRefresh((value) => value + 1);
                    }} disabled={busy !== null}>Refresh evaluation</button>
                    {manualOfferIds === null
                      ? <button className="button text" onClick={() => {
                        setManualOfferIds(offerEvaluation.filter((offer) => offer.status === "applied").map((offer) => offer.offer_id));
                        resetSale();
                      }} disabled={!pricing || pricingLoading}>Choose manually</button>
                      : <button className="button text" onClick={() => {
                        setManualOfferIds(null);
                        resetSale();
                      }}>Use automatic selection</button>}
                  </div>
                </div>
                {!cart.length
                  ? <p className="small muted">Add items to evaluate eligible offers.</p>
                  : pricingLoading
                    ? <p className="small muted" role="status">Evaluating eligible offers and refreshing server pricing…</p>
                    : !offerEvaluation.length
                      ? <p className="small muted">No offers apply to this cart. Checkout remains available at regular pricing.</p>
                      : offerEvaluation.map((decision) => {
                        return <div className="offer-option" key={decision.offer_id}>
                          {manualOfferIds !== null
                            ? <input type="checkbox" aria-label={`Select ${decision.name}`} checked={manualOfferIds.includes(decision.offer_id)} onChange={() => toggleOffer(decision.offer_id)} />
                            : <span className={`offer-status ${decision.status}`} aria-label={`Offer ${decision.status}`}>{decision.status === "applied" ? "✓" : decision.status === "eligible" ? "○" : "—"}</span>}
                          <span>
                            <strong>{decision.name}</strong>
                            <small>{decision.offer_id}{decision.stackable ? " · combinable when rules allow" : ""}</small>
                            <small>{decision.status === "applied"
                              ? `${manualOfferIds === null ? "Applied automatically" : "Applied by manual selection"} · ${money(decision.discount_paise)} discount`
                              : decision.status === "eligible"
                                ? `${manualOfferIds === null ? "Eligible; not selected automatically." : "Eligible for manual selection."} ${decision.reason}`
                                : decision.reason.startsWith("Not selected")
                                  ? `Not selected: ${decision.reason}`
                                  : `Rejected: ${decision.reason}`}</small>
                          </span>
                        </div>;
                      })}
                {pricingError && <p className="small error-text" role="alert">Latest server pricing is unavailable: {pricingError} The displayed offers are the last successful evaluation; adjust the selection or refresh before checkout.</p>}
              </section>
              <label className="field">Payment method<select value={paymentMethod} onChange={(event) => {
                setPaymentMethod(event.target.value);
                resetSale(false);
              }}><option value="upi">UPI</option><option value="card">Card</option><option value="cash">Cash</option></select></label>
              <label className="field">Sales channel<select value={channel} onChange={(event) => {
                setChannel(event.target.value);
                resetSale(false);
              }}><option value="walk_in">Walk-in</option><option value="whatsapp_booked">WhatsApp booked</option><option value="online_pickup">Online pickup</option></select></label>
              <button className="button primary charge" onClick={() => {
                try { openPaymentReview(); } catch (error) { setNotice({ text: reportError(error), tone: "error" }); }
              }} disabled={!canCharge}>Review &amp; charge</button>
              <p className="small muted">The bundled local terminal is simulated; external provider capabilities are not verified here. Confirm the customer has approved the amount.</p>
              {sale && <div className={`sale-result ${isComplete ? "success" : "warning"}`} role="status">
                <strong>{isComplete ? "Sale complete" : `Sale ${sale.status.toLowerCase()}`}</strong>
                {sale.invoice_no && <p>Invoice {sale.invoice_no}</p>}
                {sale.order_id && <p>Shopify order {sale.order_id}</p>}
                {sale.message && <p>{sale.message}</p>}
                {isComplete && sale.sale_id && <button className="button primary" onClick={openInvoice}>View Bill / Open Invoice</button>}
                {invoiceError && <p className="notice error" role="alert">{invoiceError}</p>}
                {sale.sale_id && !isComplete && <button className="button secondary" onClick={recoverCheckout} disabled={busy !== null}>Check sale status</button>}
                {sale.status === "UNKNOWN" && <button className="button secondary" onClick={reconcileUnknownSale} disabled={busy !== null}>Reconcile with same checkout key</button>}
                {["PAYMENT_FAILED", "PAYMENT_DECLINED"].includes(sale.status) && <button className="button secondary" onClick={resetFailedAttempt}>Start a new attempt</button>}
              </div>}
              {cart.length > 0 && !pricing && <p className="small muted" role="status">Requesting server-priced totals…</p>}
            </aside>
          </div>
        )}
        {view === "admin" && user.role === "admin" && <section className="admin-workspace" aria-labelledby="admin-title">
          <div className="section-title">
            <div><span className="eyebrow">ADMINISTRATION</span><h1 id="admin-title">Inventory</h1></div>
            <div className="inventory-actions">
              <button className="button secondary" onClick={() => void Promise.all([loadAdminFrames(), loadCatalogProducts()])} disabled={adminFramesLoading || catalogLoading}>Refresh inventory</button>
              <button className="button primary" onClick={() => { setNotice(undefined); setCreateDialogOpen(true); }}>Create catalog item</button>
            </div>
          </div>
          <p className="muted small">Stock is shown for the selected store only. Frames have tracked store stock; lenses are make-to-order and no lens quantity is reported.</p>
          {adminLoadingError && <p className="notice error" role="alert">{adminLoadingError}</p>}
          <div className="admin-grid admin-grid-single">
            <section className="panel" aria-labelledby="receive-stock-title">
              <h2 id="receive-stock-title">Receive frame stock</h2>
              <p className="small muted">{location ? `${location.store_code} · ${location.name}` : "Choose a store above."}</p>
              {adminFramesLoading ? <p role="status">Loading frame inventory…</p> : !adminFrames.length ? <p className="empty">No active tracked frame variants are available for this store.</p> : <>
                <label className="field">Frame<select value={receiptVariantId} onChange={(event) => setReceiptVariantId(event.target.value)}>{adminFrames.map((item) => <option key={item.variant_id} value={item.variant_id}>{item.product_title} · {item.variant_title} · stock {item.available ?? "unknown"}</option>)}</select></label>
                <form className="admin-form" onSubmit={(event) => { event.preventDefault(); receiveFrameStock(); }}>
                  <label className="field">Received quantity<input type="number" min="1" step="1" required value={receiptQuantity} onChange={(event) => setReceiptQuantity(event.target.value)} /></label>
                  <label className="field">Delivery / goods-received reference<input required maxLength={120} value={receiptReference} onChange={(event) => setReceiptReference(event.target.value)} /></label>
                  <button className="button primary" type="submit" disabled={busy !== null}>{busy === "stock-receipt" ? "Recording…" : "Record stock receipt"}</button>
                </form>
              </>}
            </section>
          </div>
          <section className="panel admin-catalog-list" aria-labelledby="catalog-stock-title">
            <div className="section-title">
              <div><h2 id="catalog-stock-title">Tracked frame/lens stock at selected store</h2><p className="small muted">{location ? `${location.store_code} · ${location.name}` : "Choose a store above."}</p></div>
              <div className="segmented" role="group" aria-label="Inventory type">
                <button type="button" className={inventoryType === "frame" ? "selected" : ""} aria-pressed={inventoryType === "frame"} onClick={() => setInventoryType("frame")}>Frames</button>
                <button type="button" className={inventoryType === "lens" ? "selected" : ""} aria-pressed={inventoryType === "lens"} onClick={() => setInventoryType("lens")}>Lenses</button>
              </div>
            </div>
            <form className="inline-form inventory-search" onSubmit={(event) => { event.preventDefault(); void loadCatalogProducts(); }}>
              <label className="field">Search catalog<input value={catalogSearch} onChange={(event) => setCatalogSearch(event.target.value)} maxLength={100} /></label>
              <button className="button secondary" type="submit" disabled={catalogLoading}>{catalogLoading ? "Searching…" : "Search"}</button>
            </form>
            {catalogError && <p className="notice error" role="alert">{catalogError}</p>}
            {(catalogLoading || (inventoryType === "frame" && adminFramesLoading))
              ? <p role="status">Loading {inventoryType === "frame" ? "tracked frame stock" : "lens catalog"}…</p>
              : inventoryType === "frame"
                ? (() => {
                    const rows = catalogProducts.filter((item) =>
                      item.line_type === "frame" &&
                      [item.title, item.variant_title, item.sku, item.category, item.material, item.size]
                        .join(" ").toLowerCase().includes(catalogSearch.trim().toLowerCase()),
                    );
                    return !rows.length ? <p className="empty">No tracked frame variants match this search for the selected store.</p> : <div className="admin-table">
                      <div className="admin-table-row frame-inventory-row heading"><span>Frame</span><span>SKU</span><span>Price (INR)</span><span>Category</span><span>Material</span><span>Size</span><span>Available at selected store</span><span>Actions</span></div>
                      {rows.map((item) => {
                        const storeStock = adminFrames.find((entry) => String(entry.variant_id) === String(item.variant_id));
                        const availability = storeStock
                          ? storeStock.available ?? "Unknown"
                          : item.status === "archived" ? "Not tracked (archived)" : "Not returned by store stock API";
                        return <div className="admin-table-row frame-inventory-row" key={item.variant_id}>
                          <span>{item.title}<small>{item.variant_title} · {item.status}</small></span>
                          <span>{item.sku || "Not recorded"}</span>
                          <span>{money(item.price_paise)}</span>
                          <span>{item.category || "Not recorded"}</span>
                          <span>{item.material || "Not recorded"}</span>
                          <span>{item.size || "Not recorded"}</span>
                          <strong>{availability}</strong>
                          <span><button className="button secondary" onClick={() => { setNotice(undefined); setArchiveConfirmation(false); setEditingProduct(item); }} aria-label={`Edit ${item.title}`}>Edit</button></span>
                        </div>;
                      })}
                    </div>;
                  })()
                : (() => {
                    const rows = catalogProducts.filter((item) =>
                      item.line_type === "lens" &&
                      [item.title, item.variant_title, item.sku, item.lens_type, item.index, item.coating]
                        .join(" ").toLowerCase().includes(catalogSearch.trim().toLowerCase()),
                    );
                    return !rows.length ? <p className="empty">No lens catalog items match this search.</p> : <>
                      <p className="small muted">Lenses are make-to-order. Store-level lens quantities are not tracked by the available inventory API.</p>
                      <div className="admin-table">
                        <div className="admin-table-row lens-inventory-row heading"><span>Lens</span><span>SKU</span><span>Price (INR)</span><span>Type</span><span>Index</span><span>Coating</span><span>Inventory</span><span>Actions</span></div>
                        {rows.map((item) => <div className="admin-table-row lens-inventory-row" key={item.variant_id}>
                          <span>{item.title}<small>{item.variant_title} · {item.status}</small></span>
                          <span>{item.sku || "Not recorded"}</span>
                          <span>{money(item.price_paise)}</span>
                          <span>{item.lens_type || "Not recorded"}</span>
                          <span>{item.index == null ? "Not recorded" : Number(item.index).toFixed(2)}</span>
                          <span>{item.coating || "Not recorded"}</span>
                          <span>Make-to-order · not tracked</span>
                          <span><button className="button secondary" onClick={() => { setNotice(undefined); setArchiveConfirmation(false); setEditingProduct(item); }} aria-label={`Edit ${item.title}`}>Edit</button></span>
                        </div>)}
                      </div>
                    </>;
                  })()}
          </section>
        </section>}
        {view === "staff" && user.role === "admin" && <section className="admin-workspace" aria-labelledby="staff-title">
          <div className="section-title">
            <div><span className="eyebrow">ADMINISTRATION</span><h1 id="staff-title">Team</h1></div>
            <button className="button secondary" onClick={() => void loadStaff()} disabled={staffLoading}>Refresh staff</button>
          </div>
          <p className="muted small">Accounts are stored in the local Studio database. Passwords are never displayed or returned.</p>
          {staffError && <p className="notice error" role="alert">{staffError}</p>}
          <section className="panel" aria-labelledby="create-staff-title">
            <h2 id="create-staff-title">Create staff account</h2>
            <form className="staff-create-form" onSubmit={(event) => { event.preventDefault(); createStaffAccount(); }}>
              <label className="field">Username<input required minLength={3} maxLength={32} pattern="[A-Za-z0-9][A-Za-z0-9._-]{2,31}" autoComplete="off" value={staffUsername} onChange={(event) => setStaffUsername(event.target.value)} /></label>
              <label className="field">Display name<input required maxLength={100} value={staffDisplayName} onChange={(event) => setStaffDisplayName(event.target.value)} /></label>
              <label className="field">Role<select value={staffRole} onChange={(event) => setStaffRole(event.target.value as "admin" | "cashier" | "optometrist")}><option value="cashier">Cashier</option><option value="optometrist">Optometrist</option><option value="admin">Admin</option></select></label>
              <label className="field">Assigned store<select required={staffRole !== "admin"} value={staffStoreCode} onChange={(event) => setStaffStoreCode(event.target.value)}>
                <option value="">{staffRole === "admin" ? "All stores (Admin)" : "Choose a store"}</option>
                {locations.map((item) => <option key={item.id} value={item.store_code}>{item.store_code} · {item.name}</option>)}
              </select></label>
              {staffRole === "optometrist" && <>
                {optometristDirectoryLoading ? <p className="small muted" role="status">Loading clinical identities for this store…</p>
                  : optometristDirectory.length === 1 ? <p className="small" role="status">
                    Clinical identity assigned: {optometristDirectory[0].display_name} · {optometristDirectory[0].staff_id}
                  </p>
                    : optometristDirectory.length > 1 ? <label className="field">Clinical staff identity<select required value={staffExternalId} onChange={(event) => setStaffExternalId(event.target.value)}>
                      <option value="">Choose an Optometrist record</option>
                      {optometristDirectory.map((item) => <option key={item.staff_id} value={item.staff_id}>{item.display_name} · {item.staff_id}</option>)}
                    </select></label> : null}
                {optometristDirectoryError && <p className="notice error" role="alert">{optometristDirectoryError}</p>}
                {!optometristDirectoryLoading && !optometristDirectory.length && !optometristDirectoryError && staffStoreCode && <p className="small muted" role="status">No clinical Optometrist identities found for this store.</p>}
              </>}
              <label className="field">Initial password<input required type="password" autoComplete="new-password" minLength={12} maxLength={1024} value={staffPassword} onChange={(event) => setStaffPassword(event.target.value)} aria-describedby="staff-password-help" /></label>
              <small id="staff-password-help" className="muted">At least 12 characters with uppercase and lowercase letters, a number, and a symbol.</small>
              <button className="button primary" type="submit" disabled={busy !== null || (staffRole === "optometrist" && (optometristDirectoryLoading || !staffExternalId))}>{busy === "staff-create" ? "Creating…" : "Create account"}</button>
            </form>
          </section>
          <section className="panel staff-list-panel" aria-labelledby="staff-list-title">
            <h2 id="staff-list-title">Staff accounts</h2>
            {staffLoading ? <p role="status">Loading staff accounts…</p> : staffAccounts.length === 0 ? <p className="empty">No staff accounts were returned.</p> : <div className="staff-list">
              {staffAccounts.map((account) => {
                const isLastActiveAdmin = account.role === "admin" && account.is_active &&
                  staffAccounts.filter((item) => item.role === "admin" && item.is_active).length === 1;
                return <article className="staff-row" key={account.staff_id}>
                  <div className="staff-identity">
                    <strong>{account.display_name}</strong>
                    <span>{account.username} · {account.role} · Operator ID {account.operator_id || account.staff_id}</span>
                    {account.external_staff_id && <small>Clinical staff ID {account.external_staff_id}</small>}
                    <small>Created {new Date(account.created_at).toLocaleString()}</small>
                  </div>
                  <span className={`status-chip ${account.is_active ? "selected" : ""}`}>{account.is_active ? "Active" : "Disabled"}</span>
                  <div className="staff-store-assignment">
                    <label className="field">Store assignment<select
                      aria-label={`Store assignment for ${account.username}`}
                      value={staffStoreDrafts[account.staff_id] ?? account.store_code ?? ""}
                      onChange={(event) => setStaffStoreDrafts((current) => ({ ...current, [account.staff_id]: event.target.value }))}>
                      {account.role === "admin" && <option value="">All stores</option>}
                      {locations.map((item) => <option key={item.id} value={item.store_code}>{item.store_code} · {item.name}</option>)}
                    </select></label>
                    <button className="button secondary" onClick={() => assignStaffStore(account)} disabled={busy !== null}>
                      {busy === "staff-store" ? "Saving…" : "Save store"}
                    </button>
                  </div>
                  <div className="staff-actions">
                    <button className="button secondary" onClick={() => {
                      setResetStaffId((current) => current === account.staff_id ? "" : account.staff_id);
                      setResetPassword("");
                    }}>Reset password</button>
                    <button className="button secondary" disabled={busy !== null || isLastActiveAdmin}
                      title={isLastActiveAdmin ? "At least one active Admin must remain." : undefined}
                      onClick={() => toggleStaffActive(account)}>{account.is_active ? "Disable" : "Activate"}</button>
                  </div>
                  {resetStaffId === account.staff_id && <form className="staff-reset-form" onSubmit={(event) => {
                    event.preventDefault();
                    resetStaffPassword(account.staff_id);
                  }}>
                    <label className="field">New password<input required type="password" autoComplete="new-password" minLength={12} maxLength={1024} value={resetPassword} onChange={(event) => setResetPassword(event.target.value)} /></label>
                    <button className="button primary" type="submit" disabled={busy !== null}>{busy === "staff-password" ? "Updating…" : "Save password"}</button>
                    <button className="button text" type="button" onClick={() => { setResetStaffId(""); setResetPassword(""); }}>Cancel</button>
                  </form>}
                </article>;
              })}
            </div>}
          </section>
        </section>}
        {view === "refund" && user.role === "cashier" && <section className="panel focused-view">
          <div className="section-title"><div><span className="eyebrow">AFTER-SALES</span><h1>Returns &amp; Refunds</h1></div></div>
          <div className="refund-search">
            <label className="field">Invoice number or Shopify order ID<input value={refundIdentifier} onChange={(event) => {
              setRefundIdentifier(event.target.value);
              setRefundPreview(null);
              setRefundAck(false);
              setRefundResult(null);
            }} placeholder="ELU/HSR/FY26/00003 or Shopify order ID" /></label>
            <button className="button secondary" onClick={lookupRefund} disabled={busy !== null}>{busy === "refund-lookup" ? "Looking up…" : "Find transaction"}</button>
          </div>
          {refundPreview ? <div className="refund-details">
            <div className="refund-order"><div><span className="eyebrow">RESOLVED ORDER</span><h2>{refundPreview.name || `Order ${refundPreview.order_id}`}</h2><p>{refundPreview.customer_name || "Customer not listed"} · {refundPreview.store_code}</p></div><strong>{money(refundPreview.total_paise)}</strong></div>
            <p>Shopify order ID: <strong>{refundPreview.order_id}</strong>{refundPreview.invoice_no && <> · Invoice: <strong>{refundPreview.invoice_no}</strong></>}</p>
            <p>Transaction date: {refundPreview.created_at ? new Date(refundPreview.created_at).toLocaleString("en-IN") : "Not recorded"} · Financial status: {refundPreview.financial_status || "Unknown"} · Payment method: {refundPreview.payment_gateway || "Unknown"}</p>
            <p>Original amount {money(refundPreview.total_paise)} · Previously refunded {money(refundPreview.refunded_paise)} · Remaining refundable {money(refundPreview.remaining_refundable_paise)}</p>
            {!!refundPreview.previous_refunds.length && <section><h3>Previous refunds</h3><ul>{refundPreview.previous_refunds.map((item) => <li key={item.refund_id}>{item.created_at ? new Date(item.created_at).toLocaleString("en-IN") : "Date unavailable"} · {money(item.amount_paise)} · tax adjustment {money(item.tax_paise)}</li>)}</ul></section>}
            <h3>Original items and available quantities</h3>
            <div className="refund-lines">
              {refundPreview.lines.map((line) => {
                const selectedQty = refundMode === "full"
                  ? line.remaining_quantity
                  : Math.min(line.remaining_quantity, Math.max(0, refundQuantities[line.line_item_id] || 0));
                const before = line.quantity ? allocatePaise(line.line_refundable_paise, line.refunded_quantity, line.quantity) : 0;
                const after = line.quantity ? allocatePaise(line.line_refundable_paise, line.refunded_quantity + selectedQty, line.quantity) : 0;
                const lineAmount = after - before;
                const taxBefore = line.quantity ? allocatePaise(line.tax_paise, line.refunded_quantity, line.quantity) : 0;
                const taxAfter = line.quantity ? allocatePaise(line.tax_paise, line.refunded_quantity + selectedQty, line.quantity) : 0;
                return <article className="refund-line" key={line.line_item_id}>
                  <div><strong>{line.title}</strong><small>Original qty {line.quantity} · refunded qty {line.refunded_quantity} · remaining {line.remaining_quantity}</small><small>Discount {money(line.discount_paise)} · original tax {money(line.tax_paise)} · remaining refund tax {money(line.remaining_tax_paise)}</small></div>
                  {refundMode === "partial" ? <label className="qty-label">Return quantity<input aria-label={`Refund quantity for ${line.title}`} type="number" min="0" max={line.remaining_quantity} step="1" value={refundQuantities[line.line_item_id] || 0} onChange={(event) => {
                    const quantity = Math.min(line.remaining_quantity, Math.max(0, Number(event.target.value) || 0));
                    setRefundQuantities((current) => ({ ...current, [line.line_item_id]: quantity }));
                    setRefundKey(makeKey());
                    setRefundAck(false);
                  }} /></label> : <strong>{line.remaining_quantity} available</strong>}
                  <span>{money(lineAmount)}{selectedQty > 0 ? ` · tax ${money(taxAfter - taxBefore)}` : ""}</span>
                  {line.restockable && line.remaining_quantity > 0 && <label className="review-control"><input type="checkbox" checked={returnItemIds.includes(line.line_item_id)} onChange={(event) => {
                    setReturnItemIds((current) => event.target.checked
                      ? [...new Set([...current, line.line_item_id])]
                      : current.filter((id) => id !== line.line_item_id));
                    setRefundKey(makeKey());
                    setRefundAck(false);
                  }} /> Physical frame returned for restocking</label>}
                </article>;
              })}
            </div>
            <div className="refund-options">
              <fieldset><legend>Refund amount</legend>
                <label className="offer-option"><input type="radio" name="refund-mode" checked={refundMode === "full"} onChange={() => { setRefundMode("full"); setRefundKey(makeKey()); setRefundAck(false); }} /> Full remaining refund · {money(refundPreview.remaining_refundable_paise)}</label>
                <label className="offer-option"><input type="radio" name="refund-mode" checked={refundMode === "partial"} onChange={() => { setRefundMode("partial"); setRefundKey(makeKey()); setRefundAck(false); }} /> Partial refund by item quantity</label>
              </fieldset>
              <label className="field">Refund reason<select required value={refundReason} onChange={(event) => { setRefundReason(event.target.value); setRefundKey(makeKey()); setRefundAck(false); }}>
                <option value="">Choose a reason</option>
                <option value="CUSTOMER_RETURN">Customer return</option>
                <option value="WRONG_PRODUCT">Wrong product</option>
                <option value="DAMAGED_DEFECTIVE">Damaged / defective product</option>
                <option value="INCORRECT_PRESCRIPTION_ORDER">Incorrect prescription or order</option>
                <option value="DUPLICATE_PAYMENT">Duplicate payment</option>
                <option value="OTHER">Other</option>
              </select></label>
              {refundReason === "OTHER" && <label className="field">Explanation<textarea required maxLength={400} value={refundOtherReason} onChange={(event) => { setRefundOtherReason(event.target.value); setRefundKey(makeKey()); setRefundAck(false); }} /></label>}
            </div>
            <p className="refund-total">Refund to original payment: <strong>{money(selectedRefundAmount)}</strong></p>
            <p className="small muted">Merchandise return and money refund are separate decisions. Frames are restocked only when you mark the physical frame as returned; lenses are not restocked. Tax adjustments follow the saved Shopify line-tax amounts.</p>
            {refundPreview.ineligible_reasons.length > 0 && <div className="callout error">{refundPreview.ineligible_reasons.map((reason) => <p key={reason}>{reason}</p>)}</div>}
            <div className="callout warning"><p>{refundPreview.payment_capability}</p><p>Bundled local mode uses simulated payment and Shopify services; it does not move real money.</p></div>
            {refundPreview.payment_gateway === "cash" && <label className="field">Open cash till<select value={refundTill} onChange={(event) => setRefundTill(event.target.value)}><option value="">Select an open till</option>{refundPreview.available_tills.map((till) => <option key={till.session_id} value={till.session_id}>{till.session_id} · location {till.location_id}</option>)}</select></label>}
            <label className="review-control"><input type="checkbox" checked={refundAck} onChange={(event) => setRefundAck(event.target.checked)} /> I confirm transaction {refundPreview.invoice_no || refundPreview.order_id} and the displayed refund amount.</label>
            <button className="button danger" onClick={submitRefund} disabled={!refundAck || !refundPreview.refund_eligible || !refundReason || (refundReason === "OTHER" && !refundOtherReason.trim()) || selectedRefundAmount <= 0 || (refundPreview.payment_gateway === "cash" && !refundTill) || busy !== null}>{busy === "refund" ? "Submitting…" : `Confirm ${refundMode} refund`}</button>
          </div> : <p className="empty">Search for an invoice or Shopify order. No refund can be submitted before order details are verified.</p>}
          {refundResult && <div className={`sale-result ${refundResult.status === "COMPLETED" ? "success" : "warning"}`} role="status">
            <strong>Refund {refundResult.status.toLowerCase()}</strong><p>Refund {refundResult.refund_id} · order {refundResult.order_id}</p>
            {refundResult.terminal_refund_id && <p>Simulated terminal refund reference: {refundResult.terminal_refund_id}</p>}
            {refundResult.status !== "COMPLETED" && <div className="dialog-actions">
              <button className="button secondary" onClick={loadRefundStatus} disabled={busy !== null}>Check refund status</button>
              {["PENDING", "REFUND_UNKNOWN", "REFUND_RECONCILING"].includes(refundResult.status) && <button className="button secondary" onClick={reconcileRefund} disabled={busy !== null || refundResult.status === "REFUND_RECONCILING"}>Reconcile refund</button>}
            </div>}
          </div>}
        </section>}
        {view === "till" && user.role !== "optometrist" && <section className="panel focused-view" aria-labelledby="till-title">
          <div className="section-title"><div><span className="eyebrow">CASH OPERATIONS</span><h1 id="till-title">{user.role === "admin" ? "Till reconciliation" : "Till session"}</h1></div>
            <button className="button secondary" onClick={() => void loadTillSessions()} disabled={tillLoading}>Refresh</button></div>
          <p className="muted small">Cash movements are persisted in paise. Cash sales come only from completed cash checkouts; paid-in and paid-out entries require a reason.</p>
          {user.role === "admin" && <p className="small muted">Historical fixture sessions are labelled and kept separate from operating sessions.</p>}
          {locations.length > 0 && !tillSessions.some((session) => session.status === "OPEN") && <form className="inline-form" onSubmit={(event) => { event.preventDefault(); openTillSession(); }}>
            <label className="field">Opening float (INR)<input required inputMode="decimal" pattern="[0-9]+([.][0-9]{1,2})?" value={tillOpenFloat} onChange={(event) => setTillOpenFloat(event.target.value)} /></label>
            <button className="button primary" type="submit" disabled={busy !== null}>{busy === "till-open" ? "Opening…" : "Open till"}</button>
          </form>}
          {tillLoading ? <p role="status">Loading till sessions…</p> : !tillSessions.length
            ? <p className="empty">No till sessions are recorded for this store. Open a till to begin cash operations.</p>
            : <div className="till-session-list">{tillSessions.map((session) => <article className="panel till-session" key={session.session_id}>
              <div className="section-title"><div><h2>{session.store_code} · {session.session_id}</h2><p className="small muted">{new Date(session.opened_at).toLocaleString("en-IN")} · location {session.location_id}</p></div>
                <span className={`status-chip ${session.status === "OPEN" ? "selected" : ""}`}>{session.status}{session.is_fixture ? " · HISTORICAL FIXTURE" : ""}</span></div>
              <dl className="profile-facts">
                <div><dt>Opened by</dt><dd>{session.opened_by}</dd></div>
                <div><dt>Opening float</dt><dd>{money(session.opening_float_paise)}</dd></div>
                <div><dt>Expected cash</dt><dd>{money(session.expected_paise)}</dd></div>
                {session.status === "CLOSED" && <>
                  <div><dt>Closed by</dt><dd>{session.closed_by || "Not recorded"}</dd></div>
                  <div><dt>Counted cash</dt><dd>{money(session.counted_cash_paise ?? undefined)}</dd></div>
                  <div><dt>Variance</dt><dd>{money(session.variance_paise ?? undefined)}</dd></div>
                  <div><dt>Review status</dt><dd>{session.review_status}</dd></div>
                </>}
              </dl>
              {session.review_note && <p className="notice warning">{session.review_note}</p>}
              {session.movements.length > 0 && <div className="till-movements">
                <h3>Cash movements</h3>
                <ul>{session.movements.map((movement) => <li key={movement.id}>
                  {movement.type.replaceAll("_", " ")} · {money(movement.amount_paise)} · {movement.ref}
                  {movement.reason ? ` · ${movement.reason}` : ""} · {movement.operator}
                </li>)}</ul>
              </div>}
              {session.status === "OPEN" && <>
                <form className="inline-form" onSubmit={(event) => { event.preventDefault(); addTillMovement(session); }}>
                  <label className="field">Movement<select value={tillMovementType} onChange={(event) => setTillMovementType(event.target.value as "paid_in" | "paid_out")}><option value="paid_in">Paid in</option><option value="paid_out">Paid out</option></select></label>
                  <label className="field">Amount (INR)<input required inputMode="decimal" pattern="[0-9]+([.][0-9]{1,2})?" value={tillMovementAmount} onChange={(event) => setTillMovementAmount(event.target.value)} /></label>
                  <label className="field">Reason<input required maxLength={400} value={tillMovementReason} onChange={(event) => setTillMovementReason(event.target.value)} /></label>
                  <button className="button secondary" type="submit" disabled={busy !== null}>{busy === "till-movement" ? "Recording…" : "Record movement"}</button>
                </form>
                <form className="inline-form" onSubmit={(event) => { event.preventDefault(); closeTillSession(session); }}>
                  <label className="field">Counted cash (INR)<input required inputMode="decimal" pattern="[0-9]+([.][0-9]{1,2})?" value={tillCounted} onChange={(event) => setTillCounted(event.target.value)} /></label>
                  <label className="field">Closing review note (optional)<input maxLength={400} value={tillReviewNote} onChange={(event) => setTillReviewNote(event.target.value)} /></label>
                  <button className="button primary" type="submit" disabled={busy !== null}>{busy === "till-close" ? "Closing…" : "Close and reconcile"}</button>
                </form>
              </>}
              {user.role === "admin" && session.status === "CLOSED" && session.review_status === "REVIEW_REQUIRED" && <form className="inline-form" onSubmit={(event) => { event.preventDefault(); reviewTillSession(session); }}>
                <label className="field">Admin review note<input required maxLength={400} value={tillAdminReview[session.session_id] || ""} onChange={(event) => setTillAdminReview((current) => ({ ...current, [session.session_id]: event.target.value }))} /></label>
                <button className="button secondary" type="submit" disabled={busy !== null}>{busy === `till-review-${session.session_id}` ? "Saving…" : "Mark reviewed"}</button>
              </form>}
            </article>)}</div>}
        </section>}
        {view === "report" && user.role === "admin" && <section className="panel focused-view">
          <div className="section-title"><div><span className="eyebrow">STORE OPERATIONS</span><h1>Reports</h1></div></div>
          <div className="report-controls">
            <label className="field">Report scope<select value={reportScope} onChange={(event) => { setReportScope(event.target.value as "store" | "overall"); setReport(null); }}>
              <option value="store">Single store</option><option value="overall">Overall operations · all stores</option>
            </select></label>
            {reportScope === "store" && <label className="field">Store<select value={locationId} onChange={(event) => setLocationId(event.target.value)}>{locations.map((item) => <option key={item.id} value={item.id}>{item.store_code} · {item.name}</option>)}</select></label>}
            <label className="field">Historical report date<input type="date" value={reportDate} onChange={(event) => {
              reportDatePinned.current = true;
              setReportDate(event.target.value);
              setReport(null);
            }} /></label>
            <button className="button primary" onClick={loadReport} disabled={busy !== null}>{busy === "report" ? "Loading…" : "Load report"}</button>
          </div>
          {report ? <div className="report-content">
            <h2>{report.store_code} · {new Intl.DateTimeFormat("en-IN", { dateStyle: "long", timeZone: "Asia/Kolkata" }).format(new Date(`${report.business_date}T00:00:00+05:30`))}</h2>
            <div className="report-metrics">
              <div><span>Completed Bills</span><strong>{report.completed_bill_count}</strong></div>
              {Object.entries(report.metrics).map(([name, value]) => <div key={name}><span>{reportMetricLabels[name] || "Other metric"}</span><strong>{money(value)}</strong></div>)}
            </div>
            <p className="small muted">Sales totals include tax. Gross Sales are line-item prices before discounts; Net Sales Before Refunds are after discounts. Taxable Sales exclude GST. Refunds are counted on their refund date; payment-method totals show captured sales before refunds.</p>
            <h3>Payment Methods</h3><ul>{Object.entries(report.payment_methods_paise).map(([method, value]) => <li key={method}>{method.toUpperCase()}: {money(value)}</li>)}</ul>
            <h3>Sales Channels</h3><ul>{Object.entries(report.channels).map(([name, value]) => <li key={name}>{name.replaceAll("_", " ")}: {value.completed_bill_count ?? 0} bills · gross {money(value.gross_sales_paise)} · refunded {money(value.refund_amount_paise)}</li>)}</ul>
            <p>Refunds: {report.refund_count} · Reconciliation exceptions: {report.exceptions.length}</p>
            {!!report.exceptions.length && <ul className="exceptions">{report.exceptions.map((item, index) => {
              const record = item && typeof item === "object" ? item as { type?: string } : {};
              return <li key={`${record.type || "exception"}-${index}`}>{(record.type || "Reconciliation issue").replaceAll("_", " ")}</li>;
            })}</ul>}
            {report.stores && <><h3>Store breakdown</h3><div className="report-store-list">{report.stores.map((store) => <article key={store.store_id}>
              <strong>{store.store_code}</strong><span>{store.completed_bill_count} bills · Net sales {money(store.metrics.net_sales_before_refunds_paise)} · Refunds {money(store.metrics.refund_amount_paise)}</span>
            </article>)}</div></>}
          </div> : <p className="empty">Choose a report scope and date to load the server report.</p>}
        </section>}
      </main>
      {createDialogOpen && view === "admin" && <div className="dialog-scrim inventory-dialog-scrim" role="presentation">
        <section className="admin-dialog" role="dialog" aria-modal="true" aria-labelledby="create-product-title">
          <div className="section-title"><div><span className="eyebrow">CATALOG MANAGEMENT</span><h2 id="create-product-title">Create catalog item</h2></div>
            <button className="button text" type="button" aria-label="Close create catalog item dialog" onClick={() => setCreateDialogOpen(false)}>×</button></div>
          {notice?.tone === "error" && <p className="notice error" role="alert">{notice.text}</p>}
          <form className="admin-form admin-dialog-form" onSubmit={(event) => { event.preventDefault(); createAdminProduct(); }}>
            <label className="field">Item type<select value={productType} onChange={(event) => setProductType(event.target.value as "frame" | "lens")}><option value="frame">Frame</option><option value="lens">Lens</option></select></label>
            <label className="field">Product name<input required maxLength={120} value={productTitle} onChange={(event) => setProductTitle(event.target.value)} /></label>
            <label className="field">SKU<input required maxLength={120} value={productSku} onChange={(event) => setProductSku(event.target.value)} /></label>
            <label className="field">Variant name<input required maxLength={120} value={variantTitle} onChange={(event) => setVariantTitle(event.target.value)} /></label>
            <label className="field">Price (INR)<input required inputMode="decimal" pattern="[0-9]+([.][0-9]{1,2})?" value={unitPrice} onChange={(event) => setUnitPrice(event.target.value)} /></label>
            {productType === "frame" ? <>
              <label className="field">Frame category<select value={frameCategory} onChange={(event) => setFrameCategory(event.target.value as "optical" | "sunglasses")}><option value="optical">Optical</option><option value="sunglasses">Sunglasses</option></select></label>
              <label className="field">Material<input required maxLength={80} value={frameMaterial} onChange={(event) => setFrameMaterial(event.target.value)} /></label>
              <label className="field">Size<input required maxLength={40} value={frameSize} onChange={(event) => setFrameSize(event.target.value)} /></label>
              <label className="review-control"><input type="checkbox" checked={rxAble} onChange={(event) => setRxAble(event.target.checked)} /> Prescription-compatible frame</label>
            </> : <>
              <label className="field">Lens type<select value={lensType} onChange={(event) => setLensType(event.target.value)}><option value="single_vision">Single vision</option><option value="bifocal">Bifocal</option><option value="progressive">Progressive</option></select></label>
              <label className="field">Index<select value={lensIndex} onChange={(event) => setLensIndex(event.target.value)}>{["1.50", "1.56", "1.60", "1.67", "1.74"].map((item) => <option key={item}>{item}</option>)}</select></label>
              <label className="field">Coating<select value={lensCoating} onChange={(event) => setLensCoating(event.target.value)}><option value="standard">Standard</option><option value="anti_reflective">Anti-reflective</option><option value="blue_cut">Blue cut</option><option value="photochromic">Photochromic</option></select></label>
            </>}
            <div className="dialog-actions">
              <button className="button secondary" type="button" onClick={() => setCreateDialogOpen(false)}>Cancel</button>
              <button className="button primary" type="submit" disabled={busy !== null}>{busy === "admin-product" ? "Creating…" : "Create item"}</button>
            </div>
          </form>
        </section>
      </div>}
      {editingProduct && view === "admin" && <div className="dialog-scrim inventory-dialog-scrim" role="presentation">
        <section className="admin-dialog" role="dialog" aria-modal="true" aria-labelledby="edit-product-title" key={`${editingProduct.product_id}-${editingProduct.variant_id}`}>
          <div className="section-title"><div><span className="eyebrow">CATALOG MANAGEMENT · {editingProduct.line_type.toUpperCase()}</span><h2 id="edit-product-title">Edit {editingProduct.title}</h2></div>
            <button className="button text" type="button" aria-label="Close edit catalog item dialog" onClick={() => { setEditingProduct(null); setArchiveConfirmation(false); }}>×</button></div>
          {notice?.tone === "error" && <p className="notice error" role="alert">{notice.text}</p>}
          {archiveConfirmation ? <div className="archive-confirmation" role="alert">
            <h3>Archive {editingProduct.title}?</h3>
            <p>This removes the product from active catalog selection. Existing stock quantities are not changed.</p>
            <div className="dialog-actions">
              <button className="button secondary" type="button" onClick={() => setArchiveConfirmation(false)} disabled={busy !== null}>Keep product active</button>
              <button className="button danger" type="button" onClick={() => archiveCatalogProduct(editingProduct)} disabled={busy !== null}>{busy === `archive-${editingProduct.variant_id}` ? "Archiving…" : "Confirm archive"}</button>
            </div>
          </div> : <form className="admin-form admin-dialog-form" onSubmit={(event) => { event.preventDefault(); void saveCatalogProduct(editingProduct, event.currentTarget); }}>
            <label className="field">Product name<input name="title" required maxLength={120} defaultValue={editingProduct.title} /></label>
            <label className="field">Description<textarea name="description" maxLength={5000} defaultValue={editingProduct.description} /></label>
            <label className="field">SKU<input name="sku" required maxLength={120} defaultValue={editingProduct.sku} /></label>
            <label className="field">Variant name<input name="variant_title" required maxLength={120} defaultValue={editingProduct.variant_title} /></label>
            <label className="field">Selling price (INR)<input name="price" required inputMode="decimal" pattern="[0-9]+([.][0-9]{1,2})?" defaultValue={(editingProduct.price_paise / 100).toFixed(2)} /></label>
            <p className="small muted">Status: {editingProduct.status}. Product edits do not change store stock.</p>
            {editingProduct.line_type === "frame" ? <>
              <label className="field">Category<select name="category" defaultValue={editingProduct.category || "optical"}><option value="optical">Optical</option><option value="sunglasses">Sunglasses</option></select></label>
              <label className="field">Material<input name="material" required maxLength={80} defaultValue={editingProduct.material || ""} /></label>
              <label className="field">Size<input name="size" required maxLength={40} defaultValue={editingProduct.size || ""} /></label>
              <label className="review-control"><input name="rx_able" type="checkbox" defaultChecked={editingProduct.rx_able === true} /> Prescription-compatible frame</label>
            </> : <>
              <label className="field">Lens type<select name="lens_type" defaultValue={editingProduct.lens_type || "single_vision"}><option value="single_vision">Single vision</option><option value="bifocal">Bifocal</option><option value="progressive">Progressive</option></select></label>
              <label className="field">Index<select name="index" defaultValue={String(editingProduct.index ?? 1.5)}>{["1.50", "1.56", "1.60", "1.67", "1.74"].map((index) => <option key={index} value={index}>{index}</option>)}</select></label>
              <label className="field">Coating<select name="coating" defaultValue={editingProduct.coating || "standard"}><option value="standard">Standard</option><option value="anti_reflective">Anti-reflective</option><option value="blue_cut">Blue cut</option><option value="photochromic">Photochromic</option></select></label>
            </>}
            <div className="dialog-actions">
              {editingProduct.status === "active" && <button className="button danger" type="button" onClick={() => setArchiveConfirmation(true)} disabled={busy !== null}>Archive product…</button>}
              <button className="button secondary" type="button" onClick={() => setEditingProduct(null)}>Cancel</button>
              <button className="button primary" type="submit" disabled={busy !== null}>{busy === `catalog-${editingProduct.variant_id}` ? "Saving…" : "Save catalog changes"}</button>
            </div>
          </form>}
        </section>
      </div>}
      {paymentReview && <div className="dialog-scrim" role="presentation">
        <section className="payment-dialog" role="dialog" aria-modal="true" aria-labelledby="payment-title">
          <div className="section-title"><div><span className="eyebrow">PAYMENT CONFIRMATION</span><h2 id="payment-title">Confirm charge</h2></div></div>
          <p>Charge <strong>{money(payable)}</strong> using <strong>{paymentMethod.toUpperCase()}</strong>.</p>
          <p className="small muted">Confirm only after the customer approves this amount. The local terminal is simulated.</p>
          <div className="dialog-actions"><button className="button secondary" onClick={() => setPaymentReview(false)}>Cancel</button><button className="button primary" onClick={checkout} disabled={busy !== null}>{busy === "checkout" ? "Processing…" : "Confirm charge"}</button></div>
        </section>
      </div>}
    </>
  );
}
