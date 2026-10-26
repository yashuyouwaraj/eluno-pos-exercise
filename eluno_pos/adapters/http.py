"""Small retrying JSON HTTP client built on the Python standard library."""

import json
import time
from typing import Any, Callable, Dict, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


class HttpRequestError(RuntimeError):
    def __init__(self, status: Optional[int], message: str) -> None:
        super().__init__(message)
        self.status = status


class JsonHttpClient:
    """JSON client with bounded GET retries and caller-controlled POST retries."""

    def __init__(
        self,
        base_url: str,
        max_attempts: int = 3,
        timeout_seconds: float = 3.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self.base_url = base_url.rstrip("/")
        self.max_attempts = max_attempts
        self.timeout_seconds = timeout_seconds
        self._sleep = sleeper

    def get(self, path: str, query: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        return self._request("GET", path, query=query)

    def post(
        self,
        path: str,
        payload: Dict[str, Any],
        retry_statuses: Sequence[int] = (429, 503),
    ) -> Dict[str, Any]:
        """POST JSON with bounded retries only for caller-declared safe statuses.

        Transport failures are deliberately not retried: the remote side might
        have completed a non-idempotent request before the response was lost.
        """
        return self._request("POST", path, payload=payload, retry_statuses=retry_statuses)

    def _request(
        self,
        method: str,
        path: str,
        query: Optional[Dict[str, str]] = None,
        payload: Optional[Dict[str, Any]] = None,
        retry_statuses: Sequence[int] = (429, 503),
    ) -> Dict[str, Any]:
        url = self.base_url + "/" + path.lstrip("/")
        if query:
            url += "?" + urlencode(query)
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")

        for attempt in range(1, self.max_attempts + 1):
            try:
                headers = {"Accept": "application/json"}
                if body is not None:
                    headers["Content-Type"] = "application/json"
                request = Request(url, data=body, headers=headers, method=method)
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    return self._decode(response.read(), url, method)
            except HTTPError as exc:
                if exc.code in retry_statuses and attempt < self.max_attempts:
                    exc.close()
                    self._sleep(self._retry_delay(exc.headers.get("Retry-After"), attempt))
                    continue
                message = self._error_message(exc, url, method)
                exc.close()
                raise HttpRequestError(exc.code, message) from exc
            except URLError as exc:
                if method == "GET" and attempt < self.max_attempts:
                    self._sleep(self._retry_delay(None, attempt))
                    continue
                raise HttpRequestError(
                    None, "{} {} failed: {}".format(method, self._safe_url(url), exc.reason)
                ) from exc

        raise AssertionError("retry loop should always return or raise")

    @staticmethod
    def _decode(body: bytes, url: str, method: str = "GET") -> Dict[str, Any]:
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpRequestError(None, "{} {} returned invalid JSON".format(method, JsonHttpClient._safe_url(url))) from exc
        if not isinstance(decoded, dict):
            raise HttpRequestError(
                None,                 "{} {} returned a non-object JSON payload".format(method, JsonHttpClient._safe_url(url))
            )
        return decoded

    @staticmethod
    def _retry_delay(retry_after: Optional[str], attempt: int) -> float:
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return min(0.25 * attempt, 1.0)

    @staticmethod
    def _error_message(error: HTTPError, url: str, method: str = "GET") -> str:
        return "{} {} failed with HTTP {}".format(method, JsonHttpClient._safe_url(url), error.code)

    @staticmethod
    def _safe_url(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
