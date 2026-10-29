"""Cart construction and final inclusive-GST pricing."""

from datetime import date
from typing import FrozenSet, Optional, Protocol, Sequence, Tuple

from .models import Cart, CartPricing, CatalogVariant, PricedLine
from .offers import OfferDefinition, OfferEngine
from .tax import TaxRateTable, calculate_line_tax


class CatalogSource(Protocol):
    def list_catalog_variants(self) -> Sequence[CatalogVariant]:
        ...


class CartError(ValueError):
    pass


class CartService:
    """Builds a cart from current catalog facts and prices it in paise."""

    def __init__(self, catalog: CatalogSource, offers: OfferEngine, tax_rates: TaxRateTable) -> None:
        self._catalog = catalog
        self._offers = offers
        self._tax_rates = tax_rates

    def resolve(self, cart: Cart) -> Tuple[CatalogVariant, ...]:
        if not cart.store_code or not cart.location_id:
            raise CartError("store_code and location_id are required")
        if not cart.lines:
            raise CartError("cart must contain at least one line")
        catalog = {variant.variant_id: variant for variant in self._catalog.list_catalog_variants()}
        resolved = []
        pair_members = {}
        for line in cart.lines:
            if isinstance(line.quantity, bool) or not isinstance(line.quantity, int) or line.quantity <= 0:
                raise CartError("quantity must be a positive integer for variant {}".format(line.variant_id))
            variant = catalog.get(str(line.variant_id))
            if variant is None:
                raise CartError("variant {} does not exist in the catalog".format(line.variant_id))
            if not variant.active:
                raise CartError("variant {} is not active for sale".format(line.variant_id))
            if variant.line_type == "lens" and not line.prescription_id:
                raise CartError("lens variant {} requires a prescription reference".format(line.variant_id))
            if variant.line_type == "frame" and line.prescription_id:
                raise CartError("frame variant {} cannot carry a prescription reference".format(line.variant_id))
            resolved.append(variant)
            if line.pair_ref:
                pair_members.setdefault(line.pair_ref, []).append(variant.line_type)
        for pair_ref, line_types in pair_members.items():
            if line_types.count("frame") > 1 or line_types.count("lens") > 1:
                raise CartError("pair {} may contain at most one frame line and one lens line".format(pair_ref))
        # Frame-only (plano), lens-only, and multiple separate pairs remain
        # supported; the brief and seeded order history use all of these.
        return tuple(resolved)

    def price(
        self,
        cart: Cart,
        business_date: date,
        customer_tags: FrozenSet[str] = frozenset(),
        selected_offer_ids: Optional[Sequence[str]] = None,
    ) -> CartPricing:
        variants = self.resolve(cart)
        offers = self._offers.evaluate(cart, variants, business_date, customer_tags, selected_offer_ids)
        priced_lines = []
        for cart_line, variant, discount_paise in zip(cart.lines, variants, offers.line_discounts_paise):
            gross_paise = cart_line.quantity * variant.price_paise
            rate = self._tax_rates.rate_for(variant.hsn, business_date)
            line_tax = calculate_line_tax(gross_paise // cart_line.quantity, cart_line.quantity, discount_paise, rate.rate_bps)
            priced_lines.append(
                PricedLine(
                    cart_line=cart_line,
                    catalog_variant=variant,
                    gross_paise=gross_paise,
                    discount_paise=discount_paise,
                    taxable_paise=line_tax.taxable_paise,
                    cgst_paise=line_tax.cgst_paise,
                    sgst_paise=line_tax.sgst_paise,
                    tax_paise=line_tax.tax_paise,
                    payable_paise=gross_paise - discount_paise,
                    rate_bps=rate.rate_bps,
                )
            )
        return CartPricing(
            lines=tuple(priced_lines),
            offers=offers,
            gross_paise=sum(line.gross_paise for line in priced_lines),
            discount_paise=sum(line.discount_paise for line in priced_lines),
            taxable_paise=sum(line.taxable_paise for line in priced_lines),
            cgst_paise=sum(line.cgst_paise for line in priced_lines),
            sgst_paise=sum(line.sgst_paise for line in priced_lines),
            tax_paise=sum(line.tax_paise for line in priced_lines),
            payable_paise=sum(line.payable_paise for line in priced_lines),
        )

    def selectable_offers(self, business_date: date) -> Tuple[OfferDefinition, ...]:
        return self._offers.selectable_offers(business_date)

    def offer_catalog(self) -> Tuple[OfferDefinition, ...]:
        return self._offers.offer_catalog()
