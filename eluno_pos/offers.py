"""Data-driven offer evaluation, independent of Shopify response shapes."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import itertools
import json
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from .models import Cart, CatalogVariant, OfferDecision, OfferEvaluation


@dataclass(frozen=True)
class OfferDefinition:
    offer_id: str
    active: bool
    valid_from: date
    valid_to: date
    stackable: bool
    priority: int
    rule: Dict[str, Any]
    name: str = ""


class OfferBook:
    def __init__(self, offers: Iterable[OfferDefinition]) -> None:
        self.offers = tuple(offers)

    @classmethod
    def from_file(cls, path: Path) -> "OfferBook":
        with path.open(encoding="utf-8") as offer_file:
            rows = json.load(offer_file)
        if not isinstance(rows, list):
            raise ValueError("offers file must contain an array")
        offers = []
        for row in rows:
            try:
                offers.append(
                    OfferDefinition(
                        offer_id=str(row["offer_id"]),
                        active=bool(row["active"]),
                        valid_from=date.fromisoformat(str(row["valid_from"])),
                        valid_to=date.fromisoformat(str(row["valid_to"])),
                        stackable=bool(row["stackable"]),
                        priority=int(row["priority"]),
                        rule=dict(row["rule"]),
                        name=str(row.get("name") or row["offer_id"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("invalid offer definition") from exc
        return cls(offers)


class OfferEngine:
    """Choose a best valid offer set without relying on fixture order.

    An offer with ``stackable: false`` is exclusive. Every subset of eligible
    stackable offers and every eligible non-stackable offer is priced, then the
    greatest discount wins. Exact ties use total priority, then offer IDs.
    """

    def __init__(self, offer_book: OfferBook) -> None:
        self._offers = offer_book.offers

    def evaluate(
        self,
        cart: Cart,
        variants: Sequence[CatalogVariant],
        business_date: date,
        customer_tags: FrozenSet[str] = frozenset(),
        selected_offer_ids: Optional[Sequence[str]] = None,
    ) -> OfferEvaluation:
        eligible_offers: List[OfferDefinition] = []
        rejected: List[OfferDecision] = []
        eligible: List[OfferDecision] = []
        for offer in sorted(self._offers, key=lambda item: item.offer_id):
            reason = self._ineligible_reason(offer, cart, variants, business_date, customer_tags)
            if reason:
                rejected.append(OfferDecision(offer.offer_id, "rejected", reason))
            else:
                eligible_offers.append(offer)
                eligible.append(OfferDecision(offer.offer_id, "eligible", "Offer conditions are satisfied."))

        if selected_offer_ids is None:
            candidates = self._compatible_sets(eligible_offers)
        else:
            selected_ids = tuple(str(offer_id) for offer_id in selected_offer_ids)
            if len(selected_ids) != len(set(selected_ids)):
                raise ValueError("an offer cannot be selected more than once")
            offers_by_id = {offer.offer_id: offer for offer in eligible_offers}
            ineligible_by_id = {decision.offer_id: decision.reason for decision in rejected}
            invalid = [offer_id for offer_id in selected_ids if offer_id not in offers_by_id]
            if invalid:
                reasons = [
                    "{}: {}".format(offer_id, ineligible_by_id.get(offer_id, "Offer does not exist."))
                    for offer_id in invalid
                ]
                raise ValueError("selected offers are not eligible: {}".format("; ".join(reasons)))
            selected = tuple(offers_by_id[offer_id] for offer_id in selected_ids)
            if len(selected) > 1 and any(not offer.stackable for offer in selected):
                raise ValueError("selected offers cannot be combined because one is exclusive")
            candidates = [selected]
        outcomes = [self._price_offer_set(choice, cart, variants) for choice in candidates]
        # candidates is never empty: the no-offer baseline is always valid.
        selected_offers, selected_discounts, applied_amounts = sorted(
            outcomes,
            key=lambda outcome: (
                -sum(outcome[1]),
                -sum(offer.priority for offer in outcome[0]),
                tuple(sorted(offer.offer_id for offer in outcome[0])),
            ),
        )[0]
        selected_ids = {offer.offer_id for offer in selected_offers}
        if selected_offer_ids is not None:
            no_discount = [offer.offer_id for offer in selected_offers if not applied_amounts[offer.offer_id]]
            if no_discount:
                raise ValueError("selected offers provide no additional discount: {}".format(", ".join(no_discount)))
        applied = []
        for offer in sorted(selected_offers, key=lambda item: (-item.priority, item.offer_id)):
            amount = applied_amounts[offer.offer_id]
            if amount:
                reason = "Selected manually." if selected_offer_ids is not None else "Selected by offer policy."
                applied.append(OfferDecision(offer.offer_id, "applied", reason, amount))
            else:
                rejected.append(OfferDecision(offer.offer_id, "rejected", "No remaining eligible line value."))
        for offer in eligible_offers:
            if offer.offer_id not in selected_ids:
                reason = "Not selected manually." if selected_offer_ids is not None else "Not selected by the conflict policy."
                rejected.append(OfferDecision(offer.offer_id, "rejected", reason))
        return OfferEvaluation(
            eligible=tuple(eligible),
            applied=tuple(applied),
            rejected=tuple(sorted(rejected, key=lambda item: item.offer_id)),
            line_discounts_paise=selected_discounts,
        )

    def _ineligible_reason(
        self, offer: OfferDefinition, cart: Cart, variants: Sequence[CatalogVariant], business_date: date,
        customer_tags: FrozenSet[str],
    ) -> str:
        if not offer.active:
            return "Offer is inactive."
        if business_date < offer.valid_from or business_date > offer.valid_to:
            return "Offer is outside its effective date range."
        if offer.rule.get("requires"):
            return "Offer requires manager approval and a reason code; that workflow is not implemented."
        condition = offer.rule.get("condition", {})
        tag = condition.get("customer_tag")
        if tag and tag not in customer_tags:
            return "Customer does not have required tag {}.".format(tag)
        if condition.get("min_pairs_in_cart") is not None:
            complete_pair_count = len(self._complete_pair_refs(cart, variants))
            if complete_pair_count < int(condition["min_pairs_in_cart"]):
                return "Requires at least {} complete frame-and-lens pairs in the cart.".format(
                    condition["min_pairs_in_cart"]
                )
        if not self._targets(offer, cart, variants):
            return "No cart line satisfies this offer's product conditions."
        return ""

    def selectable_offers(self, business_date: date) -> Tuple[OfferDefinition, ...]:
        return tuple(
            offer for offer in sorted(self._offers, key=lambda item: item.offer_id)
            if offer.active
            and offer.valid_from <= business_date <= offer.valid_to
            and not offer.rule.get("requires")
            and not offer.rule.get("condition", {}).get("customer_tag")
        )

    @staticmethod
    def _compatible_sets(offers: Sequence[OfferDefinition]) -> List[Tuple[OfferDefinition, ...]]:
        stackable = [offer for offer in offers if offer.stackable]
        choices = [tuple()]
        for size in range(1, len(stackable) + 1):
            choices.extend(itertools.combinations(stackable, size))
        choices.extend((offer,) for offer in offers if not offer.stackable)
        return choices

    @staticmethod
    def _complete_pair_refs(cart: Cart, variants: Sequence[CatalogVariant]) -> Tuple[str, ...]:
        """Return only explicitly paired frame-and-lens references.

        A lens-only line is still a supported cart scenario, but schema.md
        defines it as a pair *without* a frame. It cannot satisfy an offer
        requiring a minimum number of complete frame-and-lens pairs. Quantity
        also cannot create extra pairs: each distinct pair_ref represents one.
        """
        members: Dict[str, set] = {}
        for line, variant in zip(cart.lines, variants):
            if not line.pair_ref:
                continue
            members.setdefault(line.pair_ref, set()).add(variant.line_type)
        return tuple(sorted(
            pair_ref for pair_ref, line_types in members.items()
            if "frame" in line_types and "lens" in line_types
        ))

    def _price_offer_set(
        self, offers: Sequence[OfferDefinition], cart: Cart, variants: Sequence[CatalogVariant],
    ) -> Tuple[Tuple[OfferDefinition, ...], Tuple[int, ...], Dict[str, int]]:
        discounts = [0] * len(cart.lines)
        applied_amounts: Dict[str, int] = {}
        for offer in sorted(offers, key=lambda item: (-item.priority, item.offer_id)):
            additions = self._discount_for_offer(offer, cart, variants, discounts)
            discounts = [old + extra for old, extra in zip(discounts, additions)]
            applied_amounts[offer.offer_id] = sum(additions)
        return tuple(offers), tuple(discounts), applied_amounts

    def _discount_for_offer(
        self, offer: OfferDefinition, cart: Cart, variants: Sequence[CatalogVariant], existing: Sequence[int],
    ) -> List[int]:
        targets = self._targets(offer, cart, variants)
        discount = offer.rule["discount"]
        result = [0] * len(cart.lines)
        if offer.rule.get("applies_to") == "pair":
            for target in targets:
                self._apply_discount(discount, (target,), cart, variants, existing, result)
        else:
            target = targets
            if discount.get("on") == "lowest_priced_lens_line":
                target = (min(target, key=lambda index: (self._gross(cart, variants, index), variants[index].variant_id, index)),)
            self._apply_discount(discount, target, cart, variants, existing, result)
        cap = offer.rule.get("max_discount_paise")
        if cap is not None:
            remaining_cap = int(cap)
            for index, allocation in enumerate(result):
                kept = min(allocation, remaining_cap)
                result[index] = kept
                remaining_cap -= kept
        return result

    def _apply_discount(
        self, discount: Dict[str, Any], targets: Sequence[int], cart: Cart, variants: Sequence[CatalogVariant],
        existing: Sequence[int], result: List[int],
    ) -> None:
        if discount["type"] == "percent":
            bps = int(discount.get("value_bps", 0))
            for index in targets:
                remaining = self._gross(cart, variants, index) - existing[index]
                result[index] += self._half_up_percent(remaining, bps)
            return
        if discount["type"] != "flat":
            raise ValueError("unsupported discount type {}".format(discount["type"]))
        remaining_amount = int(discount["value_paise"])
        for index in targets:
            available = self._gross(cart, variants, index) - existing[index] - result[index]
            allocation = min(available, remaining_amount)
            result[index] += allocation
            remaining_amount -= allocation
            if not remaining_amount:
                break

    def _targets(self, offer: OfferDefinition, cart: Cart, variants: Sequence[CatalogVariant]) -> Tuple[int, ...]:
        condition = offer.rule.get("condition", {})
        applies_to = offer.rule.get("applies_to")
        indexes = tuple(index for index, variant in enumerate(variants) if self._matches_line(condition, variant))
        if applies_to == "cart":
            return tuple(range(len(cart.lines)))
        if applies_to in ("frame", "lens"):
            return tuple(index for index in indexes if variants[index].line_type == applies_to)
        if applies_to == "pair":
            targets = []
            for pair_ref in sorted({line.pair_ref for line in cart.lines if line.pair_ref is not None}):
                pair_indexes = [index for index, line in enumerate(cart.lines) if line.pair_ref == pair_ref]
                has_frame = any(variants[index].line_type == "frame" for index in pair_indexes)
                has_lens = any(variants[index].line_type == "lens" for index in pair_indexes)
                if condition.get("has_frame_and_lens") and not (has_frame and has_lens):
                    continue
                # Historical BUNDLE300 allocates to the frame. This is also a
                # deterministic allocation for a pair-specific flat discount.
                targets.append(next(index for index in pair_indexes if variants[index].line_type == "frame") if has_frame else pair_indexes[0])
            return tuple(targets)
        raise ValueError("unsupported offer applies_to {}".format(applies_to))

    @staticmethod
    def _matches_line(condition: Dict[str, Any], variant: CatalogVariant) -> bool:
        return (
            (not condition.get("lens_type") or variant.lens_type == condition["lens_type"])
            and (condition.get("index_min") is None or (variant.index is not None and variant.index >= Decimal(str(condition["index_min"]))))
            and (not condition.get("coating") or variant.coating == condition["coating"])
        )

    @staticmethod
    def _gross(cart: Cart, variants: Sequence[CatalogVariant], index: int) -> int:
        return cart.lines[index].quantity * variants[index].price_paise

    @staticmethod
    def _half_up_percent(amount_paise: int, bps: int) -> int:
        return int((Decimal(amount_paise) * Decimal(bps) / Decimal(10000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
