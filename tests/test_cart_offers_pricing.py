from datetime import date
import json
from pathlib import Path
import unittest

from eluno_pos.adapters.shopify import ShopifyAdminClient
from eluno_pos.cart import CartError, CartService
from eluno_pos.models import Cart, CartLine
from eluno_pos.offers import OfferBook, OfferDefinition, OfferEngine
from eluno_pos.tax import TaxRateTable


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "starter-kit" / "data"
PRODUCTS = json.loads((DATA / "products.json").read_text(encoding="utf-8"))
CUSTOMERS = json.loads((DATA / "customers.json").read_text(encoding="utf-8"))
ORDERS = json.loads((DATA / "orders_sample.json").read_text(encoding="utf-8"))


class FixtureHttp:
    def get(self, path, query=None):
        if path == "products.json":
            return {"products": PRODUCTS}
        raise AssertionError("unexpected catalog request {}".format(path))


class CartOfferPricingTests(unittest.TestCase):
    def setUp(self):
        self.catalog = ShopifyAdminClient(http=FixtureHttp())
        self.variants = self.catalog.list_catalog_variants()
        self.book = OfferBook.from_file(DATA / "offers.json")
        self.engine = OfferEngine(self.book)
        self.service = CartService(self.catalog, self.engine, TaxRateTable.from_file(DATA / "tax_rates.json"))
        self.frames = [variant for variant in self.variants if variant.line_type == "frame" and variant.active]
        self.lenses = [variant for variant in self.variants if variant.line_type == "lens" and variant.active]

    def cart(self, lines):
        return Cart("HSR", "70000002", tuple(lines))

    @staticmethod
    def decision_ids(decisions):
        return {decision.offer_id for decision in decisions}

    def test_catalog_backed_cart_allows_supported_frame_only_lens_only_and_pair(self):
        frame = self.frames[0]
        lens = self.lenses[0]
        self.assertEqual((frame,), self.service.resolve(self.cart([CartLine(frame.variant_id, 1, "frame-demo")])) )
        self.assertEqual((lens,), self.service.resolve(self.cart([CartLine(lens.variant_id, 1, "lens-only", "RX1")])) )
        self.assertEqual(
            (frame, lens),
            self.service.resolve(self.cart([CartLine(frame.variant_id, 1, "pair-1"), CartLine(lens.variant_id, 1, "pair-1", "RX1")])),
        )

    def test_cart_rejects_unknown_inactive_and_invalid_lens_lines(self):
        with self.assertRaisesRegex(CartError, "does not exist"):
            self.service.resolve(self.cart([CartLine("does-not-exist", 1)]))
        archived = next(variant for variant in self.variants if not variant.active)
        with self.assertRaisesRegex(CartError, "not active"):
            self.service.resolve(self.cart([CartLine(archived.variant_id, 1)]))
        with self.assertRaisesRegex(CartError, "requires a prescription"):
            self.service.resolve(self.cart([CartLine(self.lenses[0].variant_id, 1)]))

    def test_offer_date_boundaries_and_expiry_are_explainable(self):
        cart = self.cart([CartLine(self.frames[0].variant_id, 1)])
        on_start = self.engine.evaluate(cart, (self.frames[0],), date(2026, 10, 1))
        self.assertIn("FESTIVE10", self.decision_ids(on_start.eligible))
        on_end = self.engine.evaluate(cart, (self.frames[0],), date(2026, 11, 15))
        self.assertIn("FESTIVE10", self.decision_ids(on_end.eligible))
        after_end = self.engine.evaluate(cart, (self.frames[0],), date(2026, 11, 16))
        expired = {item.offer_id: item.reason for item in after_end.rejected}
        self.assertIn("outside its effective date range", expired["FESTIVE10"])
        self.assertEqual("Offer is inactive.", expired["MONSOON20"])

    def test_catalog_max_discount_cap_is_honoured(self):
        expensive = sorted(self.frames, key=lambda item: item.price_paise, reverse=True)[:2]
        cart = self.cart([CartLine(expensive[0].variant_id, 1), CartLine(expensive[1].variant_id, 1)])
        result = self.engine.evaluate(cart, tuple(expensive), date(2026, 10, 10))
        festive = next(item for item in result.applied if item.offer_id == "FESTIVE10")
        self.assertEqual(200000, festive.discount_paise)
        self.assertEqual(200000, result.total_discount_paise)

    def test_manual_offer_selection_is_eligible_and_respects_stacking(self):
        frame = self.frames[0]
        cart = self.cart([CartLine(frame.variant_id, 1)])
        manual = self.engine.evaluate(
            cart,(frame,),date(2026,10,10),selected_offer_ids=("FESTIVE10",),
        )
        self.assertEqual({"FESTIVE10"},self.decision_ids(manual.applied))
        self.assertGreater(manual.total_discount_paise,0)
        with self.assertRaisesRegex(ValueError,"not eligible"):
            self.engine.evaluate(
                cart,(frame,),date(2026,10,10),selected_offer_ids=("PRIORITY500",),
            )

        lens=self.lenses[0]
        paired_cart=self.cart([
            CartLine(frame.variant_id,1,"pair"),
            CartLine(lens.variant_id,1,"pair","RX1"),
        ])
        with self.assertRaisesRegex(ValueError,"exclusive"):
            self.engine.evaluate(
                paired_cart,(frame,lens),date(2026,10,10),
                selected_offer_ids=("FESTIVE10","BUNDLE300"),
            )
        with self.assertRaisesRegex(ValueError,"cannot be selected more than once"):
            self.engine.evaluate(
                cart,(frame,),date(2026,10,10),
                selected_offer_ids=("FESTIVE10","FESTIVE10"),
            )

    def test_flat_cart_discount_allocation_and_tax_are_invariant_to_line_order(self):
        frame = next(item for item in self.frames if item.hsn == "90031900")
        lens = next(item for item in self.lenses if item.hsn == "90015000")
        offer = OfferDefinition(
            "FLAT-CART",
            True,
            date(2026, 1, 1),
            date(2027, 1, 1),
            True,
            1,
            {"applies_to": "cart", "discount": {"type": "flat", "value_paise": 12345}},
        )
        service = CartService(
            self.catalog,
            OfferEngine(OfferBook((offer,))),
            TaxRateTable.from_file(DATA / "tax_rates.json"),
        )
        frame_line = CartLine(frame.variant_id, 1)
        lens_line = CartLine(lens.variant_id, 1, "pair", "RX1")
        forward = service.price(
            self.cart([frame_line, lens_line]), date(2026, 10, 10), selected_offer_ids=("FLAT-CART",)
        )
        reverse = service.price(
            self.cart([lens_line, frame_line]), date(2026, 10, 10), selected_offer_ids=("FLAT-CART",)
        )
        self.assertNotEqual(forward.lines[0].rate_bps, forward.lines[1].rate_bps)
        self.assertEqual(12345, forward.discount_paise)
        self.assertEqual(forward.discount_paise, reverse.discount_paise)
        self.assertEqual(forward.tax_paise, reverse.tax_paise)
        self.assertEqual(forward.payable_paise, reverse.payable_paise)
        self.assertEqual(
            {
                line.cart_line.variant_id: (line.discount_paise, line.tax_paise, line.payable_paise)
                for line in forward.lines
            },
            {
                line.cart_line.variant_id: (line.discount_paise, line.tax_paise, line.payable_paise)
                for line in reverse.lines
            },
        )

    def test_flat_cart_discount_residual_paisee_is_deterministic(self):
        variants = sorted(
            [next(item for item in self.frames if item.hsn == "90031900"),
             next(item for item in self.lenses if item.hsn == "90015000")],
            key=lambda item: item.variant_id,
        )
        cart_lines = [CartLine(item.variant_id, 1) for item in variants]
        offer = OfferDefinition(
            "FLAT-RESIDUAL",
            True,
            date(2026, 1, 1),
            date(2027, 1, 1),
            True,
            1,
            {"applies_to": "cart", "discount": {"type": "flat", "value_paise": 7}},
        )
        evaluation = OfferEngine(OfferBook((offer,))).evaluate(
            self.cart(cart_lines), tuple(variants), date(2026, 10, 10), selected_offer_ids=("FLAT-RESIDUAL",)
        )
        self.assertEqual(7, sum(evaluation.line_discounts_paise))
        reversed_evaluation = OfferEngine(OfferBook((offer,))).evaluate(
            self.cart(list(reversed(cart_lines))),
            tuple(reversed(variants)),
            date(2026, 10, 10),
            selected_offer_ids=("FLAT-RESIDUAL",),
        )
        self.assertEqual(
            dict(zip((item.variant_id for item in variants), evaluation.line_discounts_paise)),
            dict(zip((item.variant_id for item in reversed(variants)), reversed_evaluation.line_discounts_paise)),
        )

    def test_cohort_and_distinct_complete_pair_conditions(self):
        priority_cart = self.cart([CartLine(self.frames[0].variant_id, 1)])
        no_tag = self.engine.evaluate(priority_cart, (self.frames[0],), date(2026, 10, 10))
        self.assertIn("PRIORITY500", self.decision_ids(no_tag.rejected))
        priority = self.engine.evaluate(priority_cart, (self.frames[0],), date(2026, 10, 10), frozenset(("priority",)))
        self.assertIn("PRIORITY500", self.decision_ids(priority.eligible))

        one_pair_high_quantity = self.cart([
            CartLine(self.frames[0].variant_id, 2, "one"),
            CartLine(self.lenses[0].variant_id, 2, "one", "RX1"),
        ])
        one_pair = self.engine.evaluate(
            one_pair_high_quantity, (self.frames[0], self.lenses[0]), date(2026, 10, 10)
        )
        self.assertIn("SECONDPAIR50", self.decision_ids(one_pair.rejected))

        lens_only = self.cart([
            CartLine(self.lenses[0].variant_id, 1, "lens-one", "RX1"),
            CartLine(self.lenses[1].variant_id, 1, "lens-two", "RX2"),
        ])
        lens_only_result = self.engine.evaluate(lens_only, tuple(self.lenses[:2]), date(2026, 10, 10))
        lens_only_reason = next(item.reason for item in lens_only_result.rejected if item.offer_id == "SECONDPAIR50")
        self.assertIn("complete frame-and-lens pairs", lens_only_reason)

        unpaired = self.cart([
            CartLine(self.frames[0].variant_id, 1), CartLine(self.lenses[0].variant_id, 1, None, "RX1"),
            CartLine(self.frames[1].variant_id, 1), CartLine(self.lenses[1].variant_id, 1, None, "RX2"),
        ])
        unpaired_result = self.engine.evaluate(
            unpaired, (self.frames[0], self.lenses[0], self.frames[1], self.lenses[1]), date(2026, 10, 10)
        )
        self.assertIn("SECONDPAIR50", self.decision_ids(unpaired_result.rejected))

        two_pairs = self.cart([
            CartLine(self.frames[0].variant_id, 1, "one"), CartLine(self.lenses[0].variant_id, 1, "one", "RX1"),
            CartLine(self.frames[1].variant_id, 1, "two"), CartLine(self.lenses[1].variant_id, 1, "two", "RX2"),
        ])
        two_pair_result = self.engine.evaluate(
            two_pairs, (self.frames[0], self.lenses[0], self.frames[1], self.lenses[1]), date(2026, 10, 10)
        )
        self.assertIn("SECONDPAIR50", self.decision_ids(two_pair_result.eligible))
        second_pair = next(offer for offer in self.book.offers if offer.offer_id == "SECONDPAIR50")
        second_only = OfferEngine(OfferBook((second_pair,))).evaluate(
            two_pairs, (self.frames[0], self.lenses[0], self.frames[1], self.lenses[1]), date(2026, 10, 10)
        )
        self.assertIn("SECONDPAIR50", self.decision_ids(second_only.applied))
        lens_indexes = (1, 3)
        lower_index = min(lens_indexes, key=lambda index: (self.lenses[(index - 1) // 2].price_paise, index))
        self.assertGreater(second_only.line_discounts_paise[lower_index], 0)
        self.assertEqual(0, second_only.line_discounts_paise[3 if lower_index == 1 else 1])

    def test_stackable_combination_beats_non_stackable_offer_and_allocates_lines(self):
        frame = min(self.frames, key=lambda item: item.price_paise)
        lens = self.lenses[0]
        cart = self.cart([CartLine(frame.variant_id, 1, "one"), CartLine(lens.variant_id, 1, "one", "RX1")])
        result = self.engine.evaluate(cart, (frame, lens), date(2026, 10, 10), frozenset(("priority",)))
        self.assertEqual({"BUNDLE300", "PRIORITY500"}, self.decision_ids(result.applied))
        self.assertIn("FESTIVE10", self.decision_ids(result.rejected))
        self.assertEqual(80000, result.total_discount_paise)
        self.assertGreater(result.line_discounts_paise[0], 0)
        self.assertGreater(result.line_discounts_paise[1], 0)
        self.assertEqual(80000, sum(result.line_discounts_paise))

    def test_priority_breaks_discount_ties_and_result_is_fixture_order_independent(self):
        common_rule = {"applies_to": "cart", "discount": {"type": "flat", "value_paise": 100}}
        low = OfferDefinition("LOW", True, date(2026, 1, 1), date(2026, 12, 31), False, 1, common_rule)
        high = OfferDefinition("HIGH", True, date(2026, 1, 1), date(2026, 12, 31), False, 2, common_rule)
        cart = self.cart([CartLine(self.frames[0].variant_id, 1)])
        forward = OfferEngine(OfferBook((low, high))).evaluate(cart, (self.frames[0],), date(2026, 10, 10))
        reverse = OfferEngine(OfferBook((high, low))).evaluate(cart, (self.frames[0],), date(2026, 10, 10))
        self.assertEqual({"HIGH"}, self.decision_ids(forward.applied))
        self.assertEqual(forward, reverse)

    def test_seeded_bundle_order_matches_discount_and_inclusive_tax_to_the_paise(self):
        order = next(item for item in ORDERS if item["order_number"] == 1010)
        properties = lambda line: {item["name"]: item["value"] for item in line["properties"]}
        cart_lines = []
        for line in order["line_items"]:
            values = properties(line)
            cart_lines.append(CartLine(str(line["variant_id"]), line["quantity"], values.get("pair_ref"), values.get("rx_id")))
        pricing = self.service.price(self.cart(cart_lines), date(2026, 10, 18))
        self.assertEqual(30000, pricing.discount_paise)
        self.assertEqual(519700, pricing.payable_paise)
        self.assertEqual(53056, pricing.tax_paise)
        self.assertEqual(41156, pricing.lines[0].tax_paise)
        self.assertEqual(11900, pricing.lines[1].tax_paise)
        self.assertTrue(pricing.reconciles())

    def test_manager_offer_is_explicitly_deferred(self):
        result = self.engine.evaluate(self.cart([CartLine(self.frames[0].variant_id, 1)]), (self.frames[0],), date(2026, 10, 10))
        manager = next(item for item in result.rejected if item.offer_id == "MANAGER_OVERRIDE")
        self.assertIn("not implemented", manager.reason)


if __name__ == "__main__":
    unittest.main()
