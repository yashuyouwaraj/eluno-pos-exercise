"""Adapter for the mock Razorpay-like payment terminal."""

from typing import Any, Dict, Optional

from .http import JsonHttpClient


DEFAULT_TERMINAL_BASE_URL = "http://127.0.0.1:8787/terminal"


class PaymentTerminalClient:
    def __init__(self, http: Optional[JsonHttpClient] = None, base_url: str = DEFAULT_TERMINAL_BASE_URL) -> None:
        self._http = http or JsonHttpClient(base_url)

    def charge(self, amount_paise: int, method: str, order_ref: str) -> Dict[str, Any]:
        # In the supplied mock a 503 is emitted before charge creation and may
        # be retried. It does not persist an order_ref-to-charge mapping, so
        # callers must not use a later POST as a general payment retry.
        return self._http.post(
            "charges",
            {"amount_paise": amount_paise, "method": method, "order_ref": order_ref},
            retry_statuses=(429, 503),
        )

    def get_charge(self, charge_id: str) -> Dict[str, Any]:
        return self._http.get("charges/{}".format(charge_id))

    def refund(
        self, charge_id: str, amount_paise: int, reason: str, idempotency_key: str,
    ) -> Dict[str, Any]:
        return self._http.post(
            "refunds",
            {
                "charge_id": charge_id,
                "amount_paise": amount_paise,
                "reason": reason,
                "idempotency_key": idempotency_key,
            },
            retry_statuses=(429,),
        )
