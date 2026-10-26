"""Shopify Admin adapter for supported customer, catalog, and order operations."""

from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .http import JsonHttpClient
from ..models import CatalogVariant, LensLimits, LensOption
from ..money import paise_from_decimal


DEFAULT_SHOPIFY_BASE_URL = "http://127.0.0.1:8787/admin/api/2026-10"


class CustomerRegistrationConflict(ValueError):
    pass


class ShopifyAdminClient:
    def __init__(self, http: Optional[JsonHttpClient] = None, base_url: str = DEFAULT_SHOPIFY_BASE_URL) -> None:
        self._http = http or JsonHttpClient(base_url)

    def search_customers_by_phone(self, phone: str) -> List[Dict[str, Any]]:
        payload = self._http.get("customers/search.json", {"query": "phone:" + phone})
        return self._list_envelope(payload, "customers")

    def register_customer(self, first_name: str, last_name: str, phone: str) -> Tuple[Dict[str, Any], bool]:
        """Create a customer once; repeated requests resolve by the unique phone."""
        matches = self.search_customers_by_phone(phone)
        if len(matches) > 1:
            raise CustomerRegistrationConflict("more than one customer already uses this mobile number")
        if matches:
            existing = matches[0]
            if (
                (existing.get("first_name") or "") != first_name
                or (existing.get("last_name") or "") != last_name
            ):
                raise CustomerRegistrationConflict("this mobile number already belongs to a different customer")
            return existing, False
        payload = self._http.post("customers.json", {
            "customer": {"first_name": first_name, "last_name": last_name, "phone": phone},
        }, retry_statuses=())
        customer = self._object_envelope(payload, "customer")
        if (
            customer.get("id") is None
            or customer.get("phone") != phone
            or customer.get("first_name") != first_name
            or customer.get("last_name") != last_name
        ):
            raise ValueError("Shopify returned incomplete or mismatched customer details")
        return customer, True

    def list_products(self, product_type: Optional[str] = None) -> List[Dict[str, Any]]:
        query = {"limit": "250"}
        if product_type:
            query["product_type"] = product_type
        payload = self._http.get("products.json", query)
        return self._list_envelope(payload, "products")

    def get_product(self, product_id: int) -> Dict[str, Any]:
        return self._object_envelope(self._http.get("products/{}.json".format(product_id)), "product")

    def get_variant(self, variant_id: int) -> Dict[str, Any]:
        return self._object_envelope(self._http.get("variants/{}.json".format(variant_id)), "variant")

    def list_inventory_levels(
        self, inventory_item_ids: List[int], location_id: int
    ) -> List[Dict[str, Any]]:
        """Read stock for item IDs at one Shopify location using the supported mock route."""
        if not inventory_item_ids:
            return []
        payload = self._http.get("inventory_levels.json", {
            "inventory_item_ids": ",".join(str(item_id) for item_id in inventory_item_ids),
            "location_ids": str(location_id),
        })
        return self._list_envelope(payload, "inventory_levels")

    def list_locations(self) -> List[Dict[str, Any]]:
        return self._list_envelope(self._http.get("locations.json"), "locations")

    def connect_inventory_level(self, inventory_item_id: int, location_id: int) -> Dict[str, Any]:
        payload = self._http.post("inventory_levels/connect.json", {
            "inventory_item_id": inventory_item_id,
            "location_id": location_id,
        }, retry_statuses=(429,))
        return self._object_envelope(payload, "inventory_level")

    def create_product(self, product: Dict[str, Any]) -> Dict[str, Any]:
        return self._object_envelope(
            self._http.post("products.json", {"product": product}, retry_statuses=(429,)), "product"
        )

    def adjust_inventory(
        self, inventory_item_id: int, location_id: int, adjustment: int,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], bool]:
        request: Dict[str, Any] = {
            "inventory_item_id": inventory_item_id,
            "location_id": location_id,
            "available_adjustment": adjustment,
        }
        if idempotency_key is not None:
            request["idempotency_key"] = idempotency_key
        payload = self._http.post("inventory_levels/adjust.json", request, retry_statuses=(429,))
        return self._object_envelope(payload, "inventory_level"), payload.get("duplicate") is True

    def create_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        # The mock's 429 is emitted before its create handler, so retrying it
        # is safe. A timeout/other result remains ambiguous and is never
        # retried here because Shopify has no idempotency key in this contract.
        return self._object_envelope(
            self._http.post("orders.json", {"order": order}, retry_statuses=(429,)), "order"
        )

    def get_order(self, order_id: int) -> Dict[str, Any]:
        return self._object_envelope(self._http.get("orders/{}.json".format(order_id)), "order")

    def list_orders(self, **query: str) -> List[Dict[str, Any]]:
        return self._list_envelope(self._http.get("orders.json", query), "orders")

    def create_refund(self, order_id: int, refund: Dict[str, Any]) -> Dict[str, Any]:
        return self._object_envelope(self._http.post("orders/{}/refunds.json".format(order_id), {"refund": refund}, retry_statuses=(429,)), "refund")

    def list_catalog_variants(self) -> List[CatalogVariant]:
        """Project Shopify's nested product response into catalog facts for carts."""
        variants = []
        for product in self.list_products():
            product_active = product.get("status") == "active"
            for variant in product.get("variants", []):
                metadata = variant.get("eluno") or {}
                try:
                    line_type = str(metadata["line_type"])
                    if line_type not in ("frame", "lens"):
                        continue
                    variants.append(
                        CatalogVariant(
                            variant_id=str(variant["id"]),
                            product_id=str(variant["product_id"]),
                            line_type=line_type,
                            hsn=str(metadata["hsn"]),
                            price_paise=paise_from_decimal(str(variant["price"])),
                            active=product_active,
                            lens_type=(str(metadata["lens_type"]) if line_type == "lens" else None),
                            index=(Decimal(str(metadata["index"])) if line_type == "lens" else None),
                            coating=(str(metadata["coating"]) if line_type == "lens" else None),
                            product_title=str(product.get("title") or ""),
                            variant_title=str(variant.get("title") or ""),
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("invalid catalog metadata for variant {}".format(variant.get("id"))) from exc
        return variants

    def list_lens_options(self) -> List[LensOption]:
        options = []
        for product in self.list_products("Lens"):
            for variant in product.get("variants", []):
                metadata = variant.get("eluno") or {}
                limits = metadata.get("rx_limits") or {}
                if metadata.get("line_type") != "lens":
                    continue
                try:
                    options.append(
                        LensOption(
                            variant_id=str(variant["id"]),
                            lens_type=str(metadata["lens_type"]),
                            index=Decimal(str(metadata["index"])),
                            coating=str(metadata["coating"]),
                            limits=LensLimits(
                                sph_min=Decimal(str(limits["sph_min"])),
                                sph_max=Decimal(str(limits["sph_max"])),
                                cyl_abs_max=Decimal(str(limits["cyl_abs_max"])),
                                add_required=bool(limits["add_required"]),
                                add_min=(
                                    Decimal(str(limits["add_min"]))
                                    if limits.get("add_min") is not None
                                    else None
                                ),
                                add_max=(
                                    Decimal(str(limits["add_max"]))
                                    if limits.get("add_max") is not None
                                    else None
                                ),
                            ),
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("invalid lens metadata for variant {}".format(variant.get("id"))) from exc
        return options

    @staticmethod
    def _list_envelope(payload: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
        value = payload.get(name)
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ValueError("Shopify response has invalid {} envelope".format(name))
        return value

    @staticmethod
    def _object_envelope(payload: Dict[str, Any], name: str) -> Dict[str, Any]:
        value = payload.get(name)
        if not isinstance(value, dict):
            raise ValueError("Shopify response has invalid {} envelope".format(name))
        return value
