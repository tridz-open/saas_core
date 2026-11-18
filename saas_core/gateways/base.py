"""Gateway ABC, shared dataclasses, HMAC helpers and the default (lazy) HTTP client."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

KINDS = (
    "checkout.completed", "payment.succeeded", "payment.failed",
    "subscription.canceled", "subscription.updated", "refund", "ignored",
)


@dataclass
class CheckoutParams:
    tenant: str
    plan: str
    gateway_price_id: str
    customer_email: str | None = None
    customer_ref: str | None = None
    success_url: str = ""
    cancel_url: str = ""
    trial_days: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass
class CheckoutResult:
    url: str
    session_ref: str
    raw: dict = field(default_factory=dict)
    payment_mocked: bool = False


@dataclass
class NormalizedEvent:
    event_id: str
    gateway: str
    kind: str
    subscription_ref: str | None = None
    customer_ref: str | None = None
    tenant_ref: str | None = None
    amount: int = 0
    currency: str | None = None
    period_end: datetime | None = None
    invoice_ref: str | None = None
    raw: dict = field(default_factory=dict)


class GatewayError(Exception):
    """Gateway call failed. Messages never contain secrets. HTTP failures carry `.status`, `.code`, `.description`."""

    def __init__(self, message: str = "", *, status: int | None = None, code: str = "", description: str = ""):
        super().__init__(message)
        self.status, self.code, self.description = status, code, description


def _provider_error(raw: bytes) -> tuple[str, str]:
    """(code, description) from a provider JSON error body (Razorpay error.code/description, Stripe
    error.code|type/message|detail), each truncated; ("","") when the body is not recognisable."""
    try:
        err = (json.loads(raw) or {}).get("error")
    except (ValueError, AttributeError, TypeError):
        return "", ""
    if isinstance(err, str):
        return "", err[:300]
    if not isinstance(err, dict):
        return "", ""
    code = str(err.get("code") or err.get("type") or "")
    desc = str(err.get("description") or err.get("message") or err.get("detail") or "")
    return code[:100], desc[:300]


class UrllibHttp:
    """Default HTTP client; only touches the network when `request` is called."""

    def request(self, method: str, url: str, *, headers: dict | None = None,
                data: dict | None = None, json_body: dict | None = None,
                basic_auth: tuple[str, str] | None = None, timeout: int = 20) -> dict:
        hdrs = dict(headers or {})
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            hdrs["Content-Type"] = "application/json"
        elif data is not None:
            body = urllib.parse.urlencode(_flatten(data)).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        if basic_auth:
            tok = base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode()).decode()
            hdrs["Authorization"] = f"Basic {tok}"
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                raw = r.read()
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except Exception:  # noqa: BLE001
                body = b""
            code, desc = _provider_error(body)
            msg = f"gateway HTTP {e.code} for {method} {urllib.parse.urlparse(url).path}"
            if code or desc:
                msg = f"{msg}: {code}: {desc}"[:400]
            raise GatewayError(msg, status=e.code, code=code, description=desc) from None
        except urllib.error.URLError:
            raise GatewayError("gateway unreachable") from None
        return json.loads(raw) if raw else {}


def _flatten(d: dict, prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for k, v in d.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            out.extend(_flatten(v, key))
        elif isinstance(v, (list, tuple)):
            for i, x in enumerate(v):
                out.extend(_flatten(x, f"{key}[{i}]") if isinstance(x, dict) else [(f"{key}[{i}]", str(x))])
        elif v is not None:
            out.append((key, str(v)))
    return out


def hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def safe_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def lower_headers(headers: dict | None) -> dict:
    return {str(k).lower(): v for k, v in (headers or {}).items()}


def from_ts(ts: Any) -> datetime | None:
    if ts in (None, "", 0):
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)


class Gateway(ABC):
    name = ""
    # Declared capabilities, e.g. {"subscription", "order", "client_callback"}.
    supports: frozenset = frozenset()

    def __init__(self, *, key_id: str = "", key_secret: str = "", webhook_secret: str = "",
                 http: Any = None, extra: dict | None = None):
        self._key_id = key_id
        self._key_secret = key_secret
        self._webhook_secret = webhook_secret
        self._http = http
        self.extra = extra or {}

    @property
    def http(self):
        if self._http is None:
            self._http = UrllibHttp()
        return self._http

    @abstractmethod
    def create_checkout_session(self, params: CheckoutParams) -> CheckoutResult: ...

    @abstractmethod
    def portal_link(self, customer_ref: str, return_url: str) -> str: ...

    @abstractmethod
    def cancel_subscription(self, sub_ref: str, at_period_end: bool) -> None: ...

    @abstractmethod
    def verify_webhook(self, headers: dict, raw_body: bytes) -> bool: ...

    @abstractmethod
    def parse_event(self, payload: dict, headers: dict | None = None) -> NormalizedEvent: ...

    def client_payload(self, session: Any) -> dict:
        """Data the browser needs to complete payment client-side (default: provider only)."""
        return {"provider": self.name}

    def verify_client_callback(self, payload: dict) -> Any:
        """Verify a client-side payment callback. Gateways that support it override this."""
        raise NotImplementedError(f"{self.name or type(self).__name__} does not support client callbacks")

    def refund_payment(self, payment_id: str, amount_minor: int, *, notes: dict | None = None,
                       receipt: str = "") -> dict:
        """Refund (part of) a payment. Returns {"id","status","amount","payment_id"}."""
        raise GatewayError(f"{self.name} does not support refunds")

    def list_payment_refunds(self, payment_id: str) -> list[dict]:
        """Refunds already made against a payment: [{"id","status","amount","receipt","notes"}]."""
        raise GatewayError(f"{self.name} does not support refund lookup")

    def ensure_captured(self, payment_id: str) -> dict:
        """Make sure a payment is captured (no-op for gateways that auto-capture)."""
        return {"id": payment_id, "status": "captured"}

    def get_payment(self, payment_id: str) -> dict:
        """{"amount","amount_refunded"} in minor units."""
        raise GatewayError(f"{self.name} does not support payment lookup")

    def update_subscription_plan(self, sub_ref: str, plan_id: str, *, at_cycle_end: bool = True) -> dict:
        """Switch a gateway subscription to another plan."""
        raise GatewayError(f"{self.name} does not support subscription plan updates")

    def invoice_payment_id(self, invoice_id: str) -> str | None:
        """Payment id that settled a gateway invoice, or None."""
        raise GatewayError(f"{self.name} does not support invoice payment lookup")
