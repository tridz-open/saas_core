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

# Allowed values for Gateway.supports, with their meaning. Billing code checks these BEFORE calling an optional
# method (see `require_capability`). A gateway must declare a capability if and only if it really implements it;
# the conformance kit (saas_core/gateways/conformance.py) enforces that.
CAPABILITIES = {
    "subscription": "Recurring billing at the provider: create_checkout_session can start a subscription and "
                    "cancel_subscription cancels it.",
    "order": "One-time payment (order/invoice mode): create_checkout_session can charge a single fixed amount.",
    "portal": "portal_link returns a provider-hosted page to manage payment methods (not just return_url).",
    "client_callback": "Payment completes in the browser and the page posts proof back: verify_client_callback "
                       "verifies it and returns a NormalizedEvent.",
    "refund": "refund_payment is implemented (refund part or all of a payment).",
    "refund_lookup": "list_payment_refunds is implemented (needed to retry a refund without double-refunding).",
    "payment_lookup": "get_payment is implemented (amount, amount_refunded, currency of a payment).",
    "plan_update": "update_subscription_plan is implemented (switch a provider subscription to another plan).",
    "invoice_lookup": "invoice_payment_id is implemented (find the payment that settled a provider invoice).",
    "payment_link": "create_payment_link is implemented (hosted one-time payment link for invoice-mode renewals).",
}

# capability -> the optional Gateway method it promises. Used by the conformance kit.
CAPABILITY_METHODS = {
    "client_callback": "verify_client_callback",
    "refund": "refund_payment",
    "refund_lookup": "list_payment_refunds",
    "payment_lookup": "get_payment",
    "plan_update": "update_subscription_plan",
    "invoice_lookup": "invoice_payment_id",
    "payment_link": "create_payment_link",
}


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


class GatewayCapabilityError(GatewayError, NotImplementedError):
    """The gateway does not declare a capability the caller needs. Subclasses GatewayError (so existing
    `except GatewayError` paths handle it) and NotImplementedError (legacy callers)."""

    def __init__(self, gateway: str, capability: str, what: str = ""):
        self.gateway, self.capability = gateway, capability
        super().__init__(f"gateway '{gateway}' does not support '{capability}'" + (f" ({what})" if what else ""),
                         code="capability_unsupported")


def has_capability(gateway: Any, capability: str) -> bool:
    """True when `gateway.supports` contains `capability`. Objects without a `supports` attribute (duck-typed
    test doubles) are judged by whether they define the capability's method."""
    sup = getattr(gateway, "supports", None)
    if sup is None:
        method = CAPABILITY_METHODS.get(capability)
        return method is None or callable(getattr(gateway, method, None))
    return capability in sup


def require_capability(gateway: Any, capability: str, what: str = "") -> None:
    """Raise GatewayCapabilityError unless `gateway` declares `capability`. Call before any optional method."""
    if not has_capability(gateway, capability):
        raise GatewayCapabilityError(getattr(gateway, "name", "") or type(gateway).__name__, capability, what)


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
    """Contract every payment gateway implements. Full rules: docs/gateways/CONTRACT.md.

    Subclass, set `name` and `supports`, implement the five abstract methods, and implement an optional method
    only when you also list its capability in `supports` (see CAPABILITIES). Register the class with the hook
    `saas_core_gateways = {"<name>": "<dotted.path.Class>"}` in your app's hooks.py.

    Rules that apply to every method:
    - Money is an integer in minor units (cents, paise) plus an ISO 4217 currency. Never float, never bool.
    - Raise `GatewayError` for any provider failure. Messages never contain key_id, key_secret, webhook_secret,
      tokens, signatures or full request bodies.
    - Never log secrets or raw webhook bodies. Use `frappe.log_error` with a short fixed message only.
    - Network calls go through `self.http.request(...)` so tests can inject a fake client.
    """

    name = ""
    # Declared capabilities: a frozenset of keys of CAPABILITIES, e.g. frozenset({"subscription", "refund"}).
    supports: frozenset = frozenset()

    def __init__(self, *, key_id: str = "", key_secret: str = "", webhook_secret: str = "",
                 http: Any = None, extra: dict | None = None):
        """Built by the registry from a Gateway Account: key_id (public), key_secret and webhook_secret
        (decrypted passwords), `extra` (JSON from the account). `http` is an injectable client with
        `request(method, url, *, headers, data, json_body, basic_auth, timeout) -> dict`."""
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
    def create_checkout_session(self, params: CheckoutParams) -> CheckoutResult:
        """Start a payment at the provider.

        Input: CheckoutParams. `params.metadata` carries server-side values: `currency` (ISO 4217, from the plan
        price row, never defaulted), `amount_minor` (int, server price), `interval`, `mode` ("order" or
        "subscription"). `params.gateway_price_id` is the provider's plan/price id (may be empty in order mode).
        Output: CheckoutResult(url, session_ref, raw). `session_ref` is the provider's id for the session, order
        or subscription and MUST be non-empty: billing stores it to match later events. `url` may be "" when the
        payment completes in the browser (see client_payload).
        Money: charge exactly `amount_minor` in `metadata["currency"]`. If the currency is missing raise
        GatewayError; never substitute another currency or price.
        Idempotency: the API layer wraps this call in an Idempotency-Key; still send a deterministic reference
        (tenant + plan) to the provider when it supports one.
        Errors: GatewayError. Never log the request body or keys."""

    @abstractmethod
    def portal_link(self, customer_ref: str, return_url: str) -> str:
        """URL where the customer manages payment methods. Return `return_url` when the provider has no portal
        (and then do not declare "portal"). Errors: GatewayError. Never log the URL (it may be a signed link)."""

    @abstractmethod
    def cancel_subscription(self, sub_ref: str, at_period_end: bool) -> None:
        """Cancel the provider subscription `sub_ref`. `at_period_end=True` keeps access until the paid period
        ends; False cancels now. Idempotent: cancelling an already-cancelled subscription must not raise.
        Gateways with no provider-side subscription (order mode only) make this a no-op.
        Errors: GatewayError."""

    @abstractmethod
    def verify_webhook(self, headers: dict, raw_body: bytes) -> bool:
        """Authenticate a webhook request. Return True only if the signature over the EXACT raw bytes is valid.

        Inputs: request headers (any case; use `lower_headers`) and the raw body bytes, before JSON parsing.
        Rules: return False (never raise) for a missing/malformed signature, an empty webhook secret, a wrong
        secret or a modified body. Compare with `safe_equal` (constant time). If the provider signs a
        timestamp, reject stale ones (replay window). Never log the body, signature or secret."""

    @abstractmethod
    def parse_event(self, payload: dict, headers: dict | None = None) -> NormalizedEvent:
        """Map a verified webhook payload to a NormalizedEvent with `kind` in KINDS.

        Rules: `event_id` is a non-empty string derived only from the signed body (so a replay collapses to one
        Payment Event); `amount` is an int in minor units (0 when unknown); `currency` is an ISO 4217 code (any
        case, billing upper-cases it) or None; unknown or irrelevant provider events return kind "ignored", never
        raise. Keep the provider payload in `raw`; it is stored as-is, so it must not contain secrets."""

    def client_payload(self, session: Any) -> dict:
        """Data the browser needs to finish payment client-side, built from a CheckoutResult. Must contain
        `provider` (== name). Only PUBLIC values (publishable key id, order id); never key_secret or
        webhook_secret. Default: {"provider": name}."""
        return {"provider": self.name}

    def verify_client_callback(self, payload: dict) -> Any:
        """Capability "client_callback". Verify proof of payment posted by the browser (e.g. an HMAC over
        payment_id|order_id with the key secret). Return a NormalizedEvent(kind="checkout.completed") whose `raw`
        holds only the signed fields. Raise GatewayError on any invalid or missing proof. Never trust
        client-supplied tenant, plan or amount."""
        raise GatewayCapabilityError(self.name or type(self).__name__, "client_callback",
                                     "does not support client callbacks")

    def refund_payment(self, payment_id: str, amount_minor: int, *, notes: dict | None = None,
                       receipt: str = "") -> dict:
        """Capability "refund". Refund `amount_minor` (positive int, minor units, in the payment's currency) of
        `payment_id`. Raise GatewayError for 0, negative, bool or non-int amounts.
        Idempotency: pass `receipt` (the caller's unique Refund name) to the provider as its idempotency key or
        reference, and copy `receipt` into `notes["refund_doc"]` if the provider has no receipt field.
        Returns {"id","status","amount","payment_id"}; status is "processed", "pending" or "failed".
        Errors: GatewayError."""
        raise GatewayCapabilityError(self.name or type(self).__name__, "refund")

    def list_payment_refunds(self, payment_id: str) -> list[dict]:
        """Capability "refund_lookup". Refunds already made against a payment:
        [{"id","status","amount","payment_id","receipt","notes"}], `receipt` echoing what refund_payment was given.
        Billing calls this before retrying a refund and adopts a match instead of creating a second refund."""
        raise GatewayCapabilityError(self.name or type(self).__name__, "refund_lookup")

    def ensure_captured(self, payment_id: str) -> dict:
        """Make sure a payment is captured before refunding or provisioning. Idempotent. Default: no-op for
        auto-capture gateways. Returns {"id","status", ...}. Errors: GatewayError."""
        return {"id": payment_id, "status": "captured"}

    def get_payment(self, payment_id: str) -> dict:
        """Capability "payment_lookup". Returns {"amount","amount_refunded","currency"}: ints in minor units and
        an ISO 4217 code (or None). Billing uses it to refuse a refund larger than the remaining balance."""
        raise GatewayCapabilityError(self.name or type(self).__name__, "payment_lookup")

    def update_subscription_plan(self, sub_ref: str, plan_id: str, *, at_cycle_end: bool = True) -> dict:
        """Capability "plan_update". Switch provider subscription `sub_ref` to provider plan/price `plan_id`,
        at the end of the cycle (True) or now. Returns the provider response dict. Errors: GatewayError."""
        raise GatewayCapabilityError(self.name or type(self).__name__, "plan_update")

    def invoice_payment_id(self, invoice_id: str) -> str | None:
        """Capability "invoice_lookup". Payment id that settled provider invoice `invoice_id`, or None if unpaid."""
        raise GatewayCapabilityError(self.name or type(self).__name__, "invoice_lookup")

    def create_payment_link(self, *, amount: int, currency: str, description: str, email: str | None,
                            notes: dict | None = None, callback_url: str = "", reference_id: str = "") -> dict:
        """Capability "payment_link". Create a hosted one-time payment link for `amount` (int, minor units) in
        `currency`. `reference_id` is unique per renewal period (idempotency). Returns {"id","short_url", ...}.
        Errors: GatewayError."""
        raise GatewayCapabilityError(self.name or type(self).__name__, "payment_link")
