"""Store-management-tool adapter for prescription lookup."""

from typing import Any, Dict, List, Optional

from .http import JsonHttpClient


DEFAULT_SMT_BASE_URL = "http://127.0.0.1:8787/smt"


class SmtClient:
    def __init__(self, http: Optional[JsonHttpClient] = None, base_url: str = DEFAULT_SMT_BASE_URL) -> None:
        self._http = http or JsonHttpClient(base_url)

    def prescriptions_by_phone(self, phone: str) -> List[Dict[str, Any]]:
        payload = self._http.get("prescriptions", {"phone": phone})
        prescriptions = payload.get("prescriptions")
        if not isinstance(prescriptions, list) or not all(isinstance(item, dict) for item in prescriptions):
            raise ValueError("SMT response has invalid prescriptions envelope")
        return prescriptions

    def prescriptions_by_customer(self, customer_id: str) -> List[Dict[str, Any]]:
        payload = self._http.get("prescriptions", {"customer_id": str(customer_id)})
        prescriptions = payload.get("prescriptions")
        if not isinstance(prescriptions, list) or not all(isinstance(item, dict) for item in prescriptions):
            raise ValueError("SMT response has invalid prescriptions envelope")
        return prescriptions

    def list_staff(self, store: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return staff using the SMT's read-only staff endpoint."""
        query = {"store": store} if store else None
        payload = self._http.get("staff", query)
        staff = payload.get("staff")
        if not isinstance(staff, list) or not all(isinstance(item, dict) for item in staff):
            raise ValueError("SMT response has invalid staff envelope")
        return staff

    def create_prescription(self, prescription: Dict[str, Any], idempotency_key: str) -> Dict[str, Any]:
        payload = self._http.post(
            "prescriptions",
            {"prescription": prescription, "idempotency_key": idempotency_key},
            retry_statuses=(),
        )
        result = payload.get("prescription")
        if not isinstance(result, dict) or not result.get("rx_id"):
            raise ValueError("SMT response has invalid prescription envelope")
        return result
